"""Dynamic Opportunity Scanner & Yield Optimization for Polymarket Liquidity Rewards.

Implements:
1. Dynamic REST querying across all available head reward pools.
2. Net Expected Marginal Yield calculation for micro-capital ($300 bankroll):
       Y_net = Daily Reward Payout($300, v, s, D_comp) + E[Spread Capture] - E[Adverse Selection] - Gas Fees
3. Strict enforcement of the $1.00 daily payout floor (Y_gross < $1.00 => $0.00).
4. Real-time ranking of markets by net yield per dollar.
"""

from __future__ import annotations

import copy
import math
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import requests

from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback
from polymarketbot.paper_trading.models import MarketConfig, TokenConfig
from polymarketbot.rewards.engine import CATEGORY_REGIMES
from polymarketbot.rewards.scoring import (
    DEFAULT_COMPETITOR_LADDER,
    compute_competitor_ladder_score,
    quadratic_order_score,
)

logger = get_logger("rewards_scanner")

CLOB_REST_URL = "https://clob.polymarket.com"


@dataclass
class ScannerTelemetry:
    """Telemetry, hit rates, and error tracking for the opportunity scanner."""

    total_scans: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    http_429_errors: int = 0
    network_errors: int = 0
    total_book_queries: int = 0
    last_scan_timestamp: float = 0.0
    last_error_time: float = 0.0
    last_error_message: str = ""
    cached_candidates_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        hit_ratio = (self.cache_hits / (self.total_scans or 1)) * 100.0
        return {
            "total_scans": self.total_scans,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_ratio_pct": round(hit_ratio, 1),
            "http_429_errors": self.http_429_errors,
            "network_errors": self.network_errors,
            "total_book_queries": self.total_book_queries,
            "last_scan_timestamp": self.last_scan_timestamp,
            "last_error_time": self.last_error_time,
            "last_error_message": self.last_error_message,
            "cached_candidates_count": self.cached_candidates_count,
        }


GLOBAL_SCANNER_TELEMETRY = ScannerTelemetry()
_SCANNER_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_BOOK_CACHE: dict[str, tuple[float, tuple[float, float, float]]] = {}
SCANNER_CACHE_TTL_SECONDS: float = 5.0


def get_scanner_telemetry() -> ScannerTelemetry:
    """Return the global scanner telemetry instance."""
    return GLOBAL_SCANNER_TELEMETRY


def reset_scanner_telemetry() -> None:
    """Reset scanner telemetry and caches (useful for test isolation)."""
    global GLOBAL_SCANNER_TELEMETRY, _SCANNER_CACHE, _BOOK_CACHE
    GLOBAL_SCANNER_TELEMETRY = ScannerTelemetry()
    _SCANNER_CACHE.clear()
    _BOOK_CACHE.clear()



@dataclass
class MarketOpportunity:
    """Represents a scored liquidity reward market opportunity."""

    condition_id: str
    question: str
    rate_per_day: float
    rewards_min_size: float
    rewards_max_spread: float  # in cents (v)
    mid_price: float = 0.50
    category: str = "default"
    competing_depth: float = 3000.0
    competing_score: float = 0.0
    tokens: list[TokenConfig] = field(default_factory=list)
    event_id: Optional[str] = None

    # Scored yield components
    maker_score: float = 0.0
    gross_reward_payout: float = 0.0
    daily_reward_payout: float = 0.0  # Floored at $1.00
    expected_spread_capture: float = 0.0
    expected_adverse_selection: float = 0.0
    expected_gas_fees: float = 0.0
    net_expected_yield: float = 0.0  # Y_net
    net_yield_per_dollar: float = 0.0  # Y_net / allocated_capital
    passed_payout_floor: bool = False

    def to_market_config(self) -> MarketConfig:
        """Convert to MarketConfig for engine registration."""
        return MarketConfig(
            condition_id=self.condition_id,
            question=self.question,
            rate_per_day=self.rate_per_day,
            rewards_min_size=self.rewards_min_size,
            rewards_max_spread=self.rewards_max_spread,
            tokens=self.tokens,
            event_id=self.event_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "question": self.question,
            "rate_per_day": round(self.rate_per_day, 2),
            "rewards_min_size": self.rewards_min_size,
            "rewards_max_spread": self.rewards_max_spread,
            "event_id": self.event_id,
            "mid_price": round(self.mid_price, 4),
            "category": self.category,
            "gross_reward_payout": round(self.gross_reward_payout, 2),
            "daily_reward_payout": round(self.daily_reward_payout, 2),
            "expected_spread_capture": round(self.expected_spread_capture, 2),
            "expected_adverse_selection": round(self.expected_adverse_selection, 2),
            "expected_gas_fees": round(self.expected_gas_fees, 4),
            "net_expected_yield": round(self.net_expected_yield, 2),
            "net_yield_per_dollar": round(self.net_yield_per_dollar, 4),
            "passed_payout_floor": self.passed_payout_floor,
        }


def infer_market_category(question: str) -> str:
    """Infer risk regime category from market question."""
    q = question.lower()
    if any(k in q for k in ["tax", "president", "senate", "election", "biden", "trump", "democrat", "republican", "gov", "vote"]):
        return "politics"
    elif any(k in q for k in ["btc", "eth", "crypto", "bitcoin", "solana"]):
        return "crypto"
    elif any(re.search(rf"\b{re.escape(k)}\b", q) for k in ["war", "israel", "iran", "ceasefire", "china", "taiwan", "russia", "ukraine", "un"]):
        return "geopolitics"
    elif any(re.search(rf"\b{re.escape(k)}\b", q) for k in ["nba", "nfl", "champion", "cup", "game", "match"]):
        return "sports"
    return "default"


def calculate_expected_yield(
    daily_rate: float,
    max_spread_cents: float,
    min_size: float,
    bankroll_usd: float = 300.0,
    concentration_pct: float = 0.75,
    spread_cents: float = 0.5,
    mid_price: float = 0.50,
    competing_depth: float = 3000.0,
    competing_score: Optional[float] = None,
    category: str = "default",
    merge_threshold: Optional[float] = None,
    gas_fee_per_merge: Optional[float] = None,
) -> dict[str, Any]:
    """Calculate the Net Expected Marginal Yield for an allocated bankroll:

    Y_net = Daily Reward Payout($300, v, s, D_comp) + E[Spread Capture] - E[Adverse Selection] - Gas Fees

    Strictly enforces:
    - $1.00 daily payout floor: Y_gross < $1.00 => $0.00
    """
    v = max_spread_cents
    s = min(spread_cents, v * 0.9)
    if v <= 0.0 or daily_rate <= 0.0 or bankroll_usd <= 0.0:
        return {
            "maker_score": 0.0,
            "gross_reward_payout": 0.0,
            "daily_reward_payout": 0.0,
            "expected_spread_capture": 0.0,
            "expected_adverse_selection": 0.0,
            "expected_gas_fees": 0.0,
            "net_expected_yield": 0.0,
            "net_yield_per_dollar": 0.0,
            "passed_payout_floor": False,
        }

    # Capital committed to two-sided quoting on this market
    allocated_capital = bankroll_usd * concentration_pct
    quote_size = max(min_size, allocated_capital / (2.0 * max(0.05, min_price := mid_price)))

    # 1. Quadratic Maker Score
    maker_score = quadratic_order_score(
        max_spread_cents=v,
        spread_cents=s,
        size=quote_size,
    )

    # 2. Competing Liquidity Score (D_comp)
    if competing_score is not None and competing_score > 0.0:
        d_comp = competing_score
    else:
        comp_side_depth = max(100.0, competing_depth * 0.5)
        d_comp = compute_competitor_ladder_score(
            max_spread_cents=v,
            qualifying_depth=comp_side_depth,
            ladder=DEFAULT_COMPETITOR_LADDER,
        )

    total_score = maker_score + d_comp
    reward_share = (maker_score / total_score) if total_score > 0.0 else 0.0

    # Gross payout
    gross_payout = reward_share * daily_rate

    # Strict $1.00 floor enforcement
    passed_floor = gross_payout >= 1.00
    daily_reward_payout = gross_payout if passed_floor else 0.0

    # 3. Expected Spread Capture E[Spread Capture]
    regime = CATEGORY_REGIMES.get(category.lower(), CATEGORY_REGIMES["default"])
    trades_per_day = regime["trade_rate_per_min"] * 1440.0
    touch_depth = 1000.0
    fill_share = min(0.30, quote_size / (touch_depth + quote_size))
    # Daily maker fills scaled to pool rate/activity
    rate_scale = min(1.0, daily_rate / 500.0)
    daily_fills = min(25.0, trades_per_day * fill_share * rate_scale)
    spread_capture = daily_fills * min(quote_size, 50.0) * (s / 100.0)

    # 4. Expected Adverse Selection E[Adverse Selection]
    jumps_per_day = regime["jump_prob_1h"] * 24.0
    jump_loss_cents = max(0.0, regime["jump_mean_cents"] - s) * regime["decay_300s_ratio"]
    adverse_selection = jumps_per_day * quote_size * (jump_loss_cents / 100.0) * fill_share * rate_scale

    # 5. Expected Gas Fees (dynamic merge cost and optimal threshold)
    if gas_fee_per_merge is None or gas_fee_per_merge == 0.015:
        try:
            from polymarketbot.trading.ctf import estimate_merge_cost_usd
            eff_gas_fee = float(estimate_merge_cost_usd())
        except Exception:
            eff_gas_fee = 0.015
    else:
        eff_gas_fee = float(gas_fee_per_merge)

    if merge_threshold is None:
        try:
            from polymarketbot.trading.ctf import calculate_optimal_merge_threshold
            eff_merge_threshold = float(calculate_optimal_merge_threshold(gas_cost_usd=eff_gas_fee))
        except Exception:
            eff_merge_threshold = 30.0
    else:
        eff_merge_threshold = float(merge_threshold)

    filled_shares_daily = daily_fills * min(quote_size, 50.0) * 0.5
    expected_merges = filled_shares_daily / max(1.0, eff_merge_threshold)
    gas_fees = expected_merges * eff_gas_fee

    # Net Yield
    net_yield = daily_reward_payout + spread_capture - adverse_selection - gas_fees
    net_yield_per_dollar = net_yield / max(1.0, allocated_capital)

    return {
        "maker_score": maker_score,
        "gross_reward_payout": gross_payout,
        "daily_reward_payout": daily_reward_payout,
        "expected_spread_capture": spread_capture,
        "expected_adverse_selection": adverse_selection,
        "expected_gas_fees": gas_fees,
        "net_expected_yield": net_yield,
        "net_yield_per_dollar": net_yield_per_dollar,
        "passed_payout_floor": passed_floor,
    }



class OpportunityScanner:
    """Scans Polymarket CLOB head reward pools dynamically and scores net yield."""

    def __init__(
        self,
        rest_url: str = CLOB_REST_URL,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.rest_url = rest_url
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "polymarketbot/rewards-scanner"})
        install_doh_fallback()

    def fetch_raw_reward_pools(self, limit: int = 50) -> list[dict[str, Any]]:
        """Query all available head reward pools dynamically without hardcoded filters."""
        url = f"{self.rest_url}/rewards/markets/multi"
        params = {
            "order_by": "rate_per_day",
            "position": "DESC",
            "page_size": max(limit, 20),
        }
        logger.info(f"Scanning CLOB REST for reward pools: {url} (limit={limit})")
        resp = self.session.get(url, params=params, timeout=15.0)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        logger.info(f"Retrieved {len(data)} raw reward market candidates.")
        return data

    def score_market_candidate(
        self,
        raw_mkt: dict[str, Any],
        bankroll_usd: float = 300.0,
        concentration_pct: float = 0.75,
        spread_cents: float = 0.5,
    ) -> MarketOpportunity:
        """Parse raw market payload and score net expected yield."""
        cid = raw_mkt.get("condition_id", "")
        q = raw_mkt.get("question", "Unknown")
        rewards_cfg = raw_mkt.get("rewards_config", [])
        rate = float(rewards_cfg[0].get("rate_per_day", 0.0)) if rewards_cfg else 0.0
        min_size = float(raw_mkt.get("rewards_min_size", 50.0))
        max_spread = float(raw_mkt.get("rewards_max_spread", 3.0))

        tokens: list[TokenConfig] = []
        mid = 0.50
        for tok in raw_mkt.get("tokens", []):
            t_id = tok.get("token_id")
            outcome = tok.get("outcome", "unknown")
            p = float(tok.get("price", 0.50))
            if t_id:
                tokens.append(TokenConfig(token_id=t_id, outcome=outcome, price=p))
                if outcome.upper() == "YES":
                    mid = p

        category = infer_market_category(q)

        # Estimate competing depth from reward pool rate if book is not loaded
        competing_depth = max(1000.0, rate * 40.0)

        yield_metrics = calculate_expected_yield(
            daily_rate=rate,
            max_spread_cents=max_spread,
            min_size=min_size,
            bankroll_usd=bankroll_usd,
            concentration_pct=concentration_pct,
            spread_cents=spread_cents,
            mid_price=mid,
            competing_depth=competing_depth,
            category=category,
        )

        return MarketOpportunity(
            condition_id=cid,
            question=q,
            rate_per_day=rate,
            rewards_min_size=min_size,
            rewards_max_spread=max_spread,
            mid_price=mid,
            category=category,
            competing_depth=competing_depth,
            tokens=tokens,
            event_id=str(raw_mkt.get("event_id")) if raw_mkt.get("event_id") is not None else None,
            maker_score=yield_metrics["maker_score"],
            gross_reward_payout=yield_metrics["gross_reward_payout"],
            daily_reward_payout=yield_metrics["daily_reward_payout"],
            expected_spread_capture=yield_metrics["expected_spread_capture"],
            expected_adverse_selection=yield_metrics["expected_adverse_selection"],
            expected_gas_fees=yield_metrics["expected_gas_fees"],
            net_expected_yield=yield_metrics["net_expected_yield"],
            net_yield_per_dollar=yield_metrics["net_yield_per_dollar"],
            passed_payout_floor=yield_metrics["passed_payout_floor"],
        )

    def scan_and_rank_markets(
        self,
        bankroll_usd: float = 300.0,
        concentration_pct: float = 0.75,
        limit: int = 50,
        raw_markets: Optional[list[dict[str, Any]]] = None,
    ) -> list[MarketOpportunity]:
        """Fetch or receive raw pools, score them, and rank by net yield per dollar descending."""
        if raw_markets is None:
            raw_markets = self.fetch_raw_reward_pools(limit=limit)

        scored: list[MarketOpportunity] = []
        for m in raw_markets:
            opp = self.score_market_candidate(
                m,
                bankroll_usd=bankroll_usd,
                concentration_pct=concentration_pct,
            )
            # Only consider active pools with positive daily rate
            if opp.rate_per_day > 0.0:
                scored.append(opp)

        # Rank descending by net yield per dollar (and net yield)
        scored.sort(key=lambda o: (o.passed_payout_floor, o.net_yield_per_dollar, o.net_expected_yield), reverse=True)
        logger.info(
            f"Ranked {len(scored)} active reward markets. Top market: "
            f"'{scored[0].question[:40]}' with net yield ${scored[0].net_expected_yield:.2f}/day "
            f"({scored[0].net_yield_per_dollar * 100:.2f}%/day per dollar)"
            if scored else "No active markets found."
        )
        return scored


def get_real_l2_score(
    session: requests.Session,
    token_id: str,
    max_spread_cents: float,
    timeout: float = 4.0,
) -> tuple[float, float, float]:
    """Fetch real L2 order book and calculate true competing score inside qualifying spread.

    Returns:
        (competing_score, total_qualifying_shares, current_mid_price)
    """
    now = time.time()
    # Check 5-second in-memory book cache
    if token_id in _BOOK_CACHE:
        cached_ts, cached_res = _BOOK_CACHE[token_id]
        if (now - cached_ts) < SCANNER_CACHE_TTL_SECONDS:
            return cached_res

    effective_spread = _coerce_float(max_spread_cents)
    if effective_spread is None or effective_spread <= 0.0:
        logger.debug(
            f"[L2 BOOK INVALID SPREAD] Refusing to score {str(token_id)[:14]} "
            f"with max_spread_cents={max_spread_cents!r}"
        )
        return 0.0, 0.0, 0.50
    max_spread_cents = effective_spread

    GLOBAL_SCANNER_TELEMETRY.total_book_queries += 1
    url = f"{CLOB_REST_URL}/book"
    try:
        r = session.get(url, params={"token_id": token_id}, timeout=timeout)
        if r.status_code == 429:
            GLOBAL_SCANNER_TELEMETRY.http_429_errors += 1
            GLOBAL_SCANNER_TELEMETRY.last_error_time = now
            GLOBAL_SCANNER_TELEMETRY.last_error_message = (
                f"HTTP 429 Too Many Requests on /book for token {token_id[:14]}"
            )
            logger.warning(f"[L2 BOOK 429 RATE LIMIT] Rate limited on /book for token {token_id[:14]}")
            if token_id in _BOOK_CACHE:
                return _BOOK_CACHE[token_id][1]
            return 0.0, 0.0, 0.50
        elif r.status_code != 200:
            return 0.0, 0.0, 0.50

        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids and not asks:
            res = (0.0, 0.0, 0.50)
            _BOOK_CACHE[token_id] = (now, res)
            return res

        best_bid = max(float(b.get("price", 0.0)) for b in bids) if bids else 0.0
        best_ask = min(float(a.get("price", 1.0)) for a in asks) if asks else 1.0

        # A crossed book (best_bid >= best_ask) cannot be scored without inventing a midpoint.
        if bids and asks and best_bid >= best_ask:
            logger.debug(
                f"[L2 BOOK CROSSED] bid {best_bid} >= ask {best_ask} for "
                f"{str(token_id)[:14]}; refusing to score"
            )
            if token_id in _BOOK_CACHE:
                return _BOOK_CACHE[token_id][1]
            return 0.0, 0.0, 0.50

        if best_bid > 0.0 and best_ask < 1.0 and best_bid < best_ask:
            mid = (best_bid + best_ask) / 2.0
        elif best_bid > 0.0 and best_ask > best_bid:
            mid = (best_bid + best_ask) / 2.0
        else:
            mid = 0.50

        comp_score = 0.0
        total_qual_shares = 0.0

        for b in bids:
            p = float(b.get("price", 0.0))
            sz = float(b.get("size", 0.0))
            spread_c = (mid - p) * 100.0
            if 0.0 <= spread_c <= max_spread_cents:
                comp_score += quadratic_order_score(max_spread_cents, spread_c, sz)
                total_qual_shares += sz

        for a in asks:
            p = float(a.get("price", 0.0))
            sz = float(a.get("size", 0.0))
            spread_c = (p - mid) * 100.0
            if 0.0 <= spread_c <= max_spread_cents:
                comp_score += quadratic_order_score(max_spread_cents, spread_c, sz)
                total_qual_shares += sz

        res = (comp_score, total_qual_shares, mid)
        _BOOK_CACHE[token_id] = (now, res)
        return res
    except requests.exceptions.RequestException as e:
        GLOBAL_SCANNER_TELEMETRY.network_errors += 1
        GLOBAL_SCANNER_TELEMETRY.last_error_time = now
        GLOBAL_SCANNER_TELEMETRY.last_error_message = f"Network error on /book for {token_id[:14]}: {e}"
        logger.debug(f"Network error fetching order book for {token_id[:14]}: {e}")
        if token_id in _BOOK_CACHE:
            return _BOOK_CACHE[token_id][1]
        return 0.0, 0.0, 0.50
    except Exception as e:
        logger.debug(f"Error fetching order book for {token_id[:14]}: {e}")
        return 0.0, 0.0, 0.50


_NUMERIC_TOKEN_ID_PATTERN = re.compile(r"[0-9]+")
_MIN_REWARD_SPREAD_CENTS = 0.1
_MAX_REWARD_SPREAD_CENTS = 10.0
_STATE_BOOLEAN_FIELDS = ("closed", "archived")


def _is_numeric_token_id(value: Any) -> bool:
    """True only for a CLOB token id: a plain decimal digit string."""
    return isinstance(value, str) and _NUMERIC_TOKEN_ID_PATTERN.fullmatch(value.strip()) is not None


def _coerce_float(value: Any) -> Optional[float]:
    """Parse a finite float, or return None instead of guessing a value."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _coerce_bool(value: Any) -> Optional[bool]:
    """Parse a payload boolean flag, or return None when it cannot be interpreted."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes"):
            return True
        if normalized in ("false", "0", "no"):
            return False
    return None


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _extract_market_metadata(raw_market: dict[str, Any]) -> dict[str, Any]:
    """Read the execution metadata the live runner needs from a raw CLOB payload."""
    tick_size = _coerce_float(_first_present(raw_market, "tick_size", "minimum_tick_size"))
    if tick_size is None or tick_size <= 0.0:
        tick_size = 0.01
    fee_rate = _coerce_float(_first_present(raw_market, "fee_rate_bps", "feeRateBps"))
    fee_rate_bps = int(round(fee_rate)) if fee_rate is not None and fee_rate >= 0.0 else 0
    return {
        "neg_risk": bool(_coerce_bool(_first_present(raw_market, "neg_risk", "negRisk"))),
        "tick_size": tick_size,
        "fee_rate_bps": fee_rate_bps,
        "end_date_iso": raw_market.get("end_date_iso"),
        "accepting_orders": _coerce_bool(
            _first_present(raw_market, "accepting_orders", "acceptingOrders")
        ),
    }


def _extract_market_tokens(
    raw_market: dict[str, Any],
) -> tuple[Optional[str], Optional[str], float]:
    """Return (YES token id, NO token id, YES mid price) without inventing token ids."""
    yes_token: Optional[str] = None
    no_token: Optional[str] = None
    mid_yes = 0.50
    tokens = raw_market.get("tokens")
    if not isinstance(tokens, (list, tuple)):
        return yes_token, no_token, mid_yes
    for token in tokens:
        if not isinstance(token, dict):
            continue
        outcome = str(token.get("outcome", "")).strip().upper()
        if outcome == "YES" and yes_token is None:
            yes_token = token.get("token_id")
            price = _coerce_float(token.get("price"))
            if price is not None and 0.0 < price < 1.0:
                mid_yes = price
        elif outcome == "NO" and no_token is None:
            no_token = token.get("token_id")
    return yes_token, no_token, mid_yes


def _extract_daily_pool(raw_market: dict[str, Any]) -> float:
    rewards_cfg = raw_market.get("rewards_config")
    if isinstance(rewards_cfg, (list, tuple)) and rewards_cfg:
        first = rewards_cfg[0]
        if isinstance(first, dict):
            rate = _coerce_float(first.get("rate_per_day"))
            if rate is not None:
                return rate
    rate = _coerce_float(raw_market.get("rewards_daily_rate"))
    return rate if rate is not None else 0.0


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _market_state_rejection(raw_market: dict[str, Any]) -> Optional[str]:
    """Return a reason when the payload says the market cannot accept orders."""
    for field_name in _STATE_BOOLEAN_FIELDS:
        raw_value = raw_market.get(field_name)
        if raw_value is None:
            continue
        flag = _coerce_bool(raw_value)
        if flag is None:
            return f"market_state: {field_name} flag is not a boolean ({raw_value!r})"
        if flag:
            return f"market_state: market is {field_name}"
    accepting = _first_present(raw_market, "accepting_orders", "acceptingOrders")
    if accepting is not None:
        flag = _coerce_bool(accepting)
        if flag is None:
            return f"market_state: accepting_orders is not a boolean ({accepting!r})"
        if not flag:
            return "market_state: market is not accepting orders"
    return None


def validate_market_for_live(
    raw_market: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """Validate a raw reward-market payload is safe to quote live.

    Checks that both token ids are real numeric CLOB ids, the reward config is
    usable, the market is open, and its expiry is a real future timestamp.

    Returns:
        (True, "ok") when the market may be traded, else (False, human-readable reason).
    """
    if not isinstance(raw_market, dict):
        return False, "payload: market payload is not a mapping"

    condition_id = raw_market.get("condition_id")
    if not isinstance(condition_id, str) or not condition_id.strip():
        return False, "condition_id: missing condition_id"

    yes_token, no_token, _ = _extract_market_tokens(raw_market)
    if not _is_numeric_token_id(yes_token):
        return False, f"token_yes: YES token id is missing or not numeric ({yes_token!r})"
    if not _is_numeric_token_id(no_token):
        return False, f"token_no: NO token id is missing or not numeric ({no_token!r})"

    min_size = _coerce_float(raw_market.get("rewards_min_size"))
    if min_size is None or min_size <= 0.0:
        return (
            False,
            f"rewards_min_size: must be > 0, got {raw_market.get('rewards_min_size')!r}",
        )

    max_spread = _coerce_float(raw_market.get("rewards_max_spread"))
    if max_spread is None or not (
        _MIN_REWARD_SPREAD_CENTS <= max_spread <= _MAX_REWARD_SPREAD_CENTS
    ):
        return (
            False,
            "rewards_max_spread: must be within "
            f"[{_MIN_REWARD_SPREAD_CENTS}, {_MAX_REWARD_SPREAD_CENTS}] cents, "
            f"got {raw_market.get('rewards_max_spread')!r}",
        )

    state_reason = _market_state_rejection(raw_market)
    if state_reason is not None:
        return False, state_reason

    end_date_raw = raw_market.get("end_date_iso")
    end_date = _parse_iso_datetime(end_date_raw)
    if end_date is None:
        return False, f"end_date_iso: missing or unparseable ({end_date_raw!r})"
    now_utc = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    if end_date <= now_utc:
        return False, f"end_date_iso: market already ended at {end_date_raw}"

    return True, "ok"


def _screen_lp_candidate(
    raw_market: dict[str, Any],
    *,
    now_utc: datetime,
    min_days_to_expiry: float,
    min_mid_price: float,
    max_mid_price: float,
    bankroll_usd: float,
) -> tuple[Optional[dict[str, Any]], str]:
    """Filter one raw reward pool. Returns (candidate, "") or (None, "code: reason")."""
    condition_id = raw_market.get("condition_id")
    if not isinstance(condition_id, str) or not condition_id.strip():
        return None, "condition_id: missing condition_id"
    question = raw_market.get("question") or "Unknown"

    daily_pool = _extract_daily_pool(raw_market)
    if daily_pool < 1.0:
        return None, f"daily_pool: ${daily_pool:.2f}/day is below the $1.00 payout floor"

    state_reason = _market_state_rejection(raw_market)
    if state_reason is not None:
        return None, state_reason

    yes_token, no_token, mid_yes = _extract_market_tokens(raw_market)
    if not _is_numeric_token_id(yes_token):
        return None, f"token_yes: token id is missing or not numeric ({yes_token!r})"
    if not _is_numeric_token_id(no_token):
        return None, f"token_no: token id is missing or not numeric ({no_token!r})"

    min_size = _coerce_float(raw_market.get("rewards_min_size"))
    if min_size is None or min_size <= 0.0:
        return None, f"rewards_min_size: must be > 0, got {raw_market.get('rewards_min_size')!r}"

    max_spread = _coerce_float(raw_market.get("rewards_max_spread"))
    if max_spread is None or not (
        _MIN_REWARD_SPREAD_CENTS <= max_spread <= _MAX_REWARD_SPREAD_CENTS
    ):
        return None, (
            "rewards_max_spread: must be within "
            f"[{_MIN_REWARD_SPREAD_CENTS}, {_MAX_REWARD_SPREAD_CENTS}] cents, "
            f"got {raw_market.get('rewards_max_spread')!r}"
        )

    # Divergence / extreme probability filter (avoid markets where one side has already won)
    if not (min_mid_price <= mid_yes <= max_mid_price):
        return None, (
            f"mid_price: YES mid {mid_yes:.4f} outside "
            f"[{min_mid_price}, {max_mid_price}]"
        )

    # Expiry / resolution filter (avoid markets ending soon with settlement jump risk).
    # A missing end_date_iso keeps the historical "distant expiry" ranking behaviour;
    # validate_market_for_live() is the stricter live-trading gate.
    end_date_raw = raw_market.get("end_date_iso")
    end_date = _parse_iso_datetime(end_date_raw)
    if end_date is not None:
        days_left = (end_date - now_utc).total_seconds() / 86400.0
        if days_left < min_days_to_expiry:
            return None, (
                f"end_date_iso: expires in {days_left:.1f} days "
                f"(< {min_days_to_expiry:.1f})"
            )
    elif end_date_raw:
        return None, f"end_date_iso: unparseable ({end_date_raw!r})"
    else:
        days_left = 999.0
        logger.debug(
            f"[SCANNER] {condition_id!r} has no end_date_iso; ranking with distant expiry"
        )

    # Capital sizing feasibility: deploy 75% for two-sided quoting, retain 25% buffer
    allocated_capital = bankroll_usd * 0.75
    quote_size = max(min_size, allocated_capital / 1.0)
    capital_required = quote_size * 1.0  # cost of buying YES + NO
    if capital_required > bankroll_usd * 0.95:
        return None, (
            f"capital: requires ${capital_required:.2f} "
            f"> ${bankroll_usd * 0.95:.2f} available"
        )

    metadata = _extract_market_metadata(raw_market)
    return {
        "condition_id": condition_id,
        "question": question,
        "daily_pool": daily_pool,
        "max_spread": max_spread,
        "min_size": min_size,
        "mid_yes": mid_yes,
        "days_left": days_left,
        "quote_size": quote_size,
        "capital_required": capital_required,
        "our_score": quadratic_order_score(max_spread, 0.50, quote_size),
        "yes_token": yes_token,
        "no_token": no_token,
        # Execution metadata required by the live runner; read, never invented.
        "neg_risk": metadata["neg_risk"],
        "tick_size": metadata["tick_size"],
        "fee_rate_bps": metadata["fee_rate_bps"],
        "end_date_iso": metadata["end_date_iso"],
        "accepting_orders": metadata["accepting_orders"],
        "raw_market": raw_market,
    }, ""


def find_optimal_lp_markets(
    bankroll_usd: float = 150.0,
    min_days_to_expiry: float = 7.0,
    min_mid_price: float = 0.12,
    max_mid_price: float = 0.88,
    max_candidates: int = 250,
    limit_output: int = 15,
) -> list[dict[str, Any]]:
    """Scan all reward pools and rank by Net Daily ROI (%) on deployed capital."""
    GLOBAL_SCANNER_TELEMETRY.total_scans += 1
    now = time.time()
    cache_key = f"{round(bankroll_usd, 2)}_{min_days_to_expiry}_{min_mid_price}_{max_mid_price}_{max_candidates}_{limit_output}"

    # 1. 5-Second TTL In-Memory Opportunity Cache Check
    if cache_key in _SCANNER_CACHE:
        cached_ts, cached_markets = _SCANNER_CACHE[cache_key]
        cache_age = now - cached_ts
        if cache_age < SCANNER_CACHE_TTL_SECONDS:
            GLOBAL_SCANNER_TELEMETRY.cache_hits += 1
            logger.info(
                f"[SCANNER CACHE HIT] Served {len(cached_markets)} candidate markets from 5s TTL cache "
                f"(Age: {cache_age:.2f}s < {SCANNER_CACHE_TTL_SECONDS:.1f}s | "
                f"Hits: {GLOBAL_SCANNER_TELEMETRY.cache_hits}/{GLOBAL_SCANNER_TELEMETRY.total_scans})"
            )
            return [dict(m) for m in cached_markets]

    GLOBAL_SCANNER_TELEMETRY.cache_misses += 1
    session = requests.Session()
    session.headers.update({"User-Agent": "polymarketbot/optimal-selector"})

    url = f"{CLOB_REST_URL}/rewards/markets/multi"
    params = {"order_by": "rate_per_day", "position": "DESC", "page_size": min(500, max_candidates)}

    logger.info(f"Querying top {max_candidates} reward pools from Polymarket CLOB...")
    try:
        resp = session.get(url, params=params, timeout=15.0)
        if resp.status_code == 429:
            GLOBAL_SCANNER_TELEMETRY.http_429_errors += 1
            GLOBAL_SCANNER_TELEMETRY.last_error_time = now
            GLOBAL_SCANNER_TELEMETRY.last_error_message = "HTTP 429 Too Many Requests on /rewards/markets/multi"
            logger.error("[SCANNER HTTP 429] Rate limit reached on CLOB multi-rewards endpoint.")
            if cache_key in _SCANNER_CACHE:
                logger.warning("[SCANNER STALE CACHE FALLBACK] Returning stale cached candidates due to HTTP 429.")
                return [dict(m) for m in _SCANNER_CACHE[cache_key][1]]
            return []
        resp.raise_for_status()
        raw_markets = resp.json().get("data", [])
    except requests.exceptions.RequestException as e:
        GLOBAL_SCANNER_TELEMETRY.network_errors += 1
        GLOBAL_SCANNER_TELEMETRY.last_error_time = now
        GLOBAL_SCANNER_TELEMETRY.last_error_message = f"Network error on /rewards/markets/multi: {e}"
        logger.error(f"[SCANNER NETWORK ERROR] Failed to fetch reward pools from CLOB: {e}")
        if cache_key in _SCANNER_CACHE:
            logger.warning("[SCANNER STALE CACHE FALLBACK] Returning stale cached candidates due to network error.")
            return [dict(m) for m in _SCANNER_CACHE[cache_key][1]]
        return []
    except Exception as e:
        logger.error(f"Failed to fetch reward pools from CLOB: {e}")
        if cache_key in _SCANNER_CACHE:
            return [dict(m) for m in _SCANNER_CACHE[cache_key][1]]
        return []

    logger.info(f"Received {len(raw_markets)} markets. Applying risk & viability filters...")

    candidates: list[dict[str, Any]] = []
    rejection_counts: Counter[str] = Counter()
    now_utc = datetime.now(timezone.utc)

    for raw_market in raw_markets:
        if not isinstance(raw_market, dict):
            rejection_counts["payload"] += 1
            logger.debug(f"[SCANNER REJECT] non-mapping market payload: {raw_market!r}")
            continue
        candidate, reason = _screen_lp_candidate(
            raw_market,
            now_utc=now_utc,
            min_days_to_expiry=min_days_to_expiry,
            min_mid_price=min_mid_price,
            max_mid_price=max_mid_price,
            bankroll_usd=bankroll_usd,
        )
        if candidate is None:
            rejection_counts[reason.split(":", 1)[0]] += 1
            logger.debug(
                f"[SCANNER REJECT] {raw_market.get('condition_id')!r} "
                f"({str(raw_market.get('question') or 'Unknown')[:60]!r}): {reason}"
            )
            continue
        candidates.append(candidate)

    rejected_total = sum(rejection_counts.values())
    rejection_summary = ", ".join(
        f"{code}={count}" for code, count in rejection_counts.most_common()
    ) or "none"
    logger.info(
        f"Screened {len(candidates)}/{len(raw_markets)} reward markets "
        f"(rejected {rejected_total}: {rejection_summary}). Auditing full L2 depth in parallel..."
    )

    def audit_market(cand: dict[str, Any]) -> dict[str, Any]:
        s = requests.Session()
        s.headers.update({"User-Agent": "polymarketbot/optimal-selector"})
        score_yes, qual_yes, real_mid_yes = get_real_l2_score(
            session=s,
            token_id=cand["yes_token"],
            max_spread_cents=cand["max_spread"],
        )
        score_no, qual_no = 0.0, 0.0
        if cand.get("no_token"):
            score_no, qual_no, _ = get_real_l2_score(
                session=s,
                token_id=cand["no_token"],
                max_spread_cents=cand["max_spread"],
            )

        # Polymarket rewards score combines qualifying depth across both tokens
        audited_comp_score = score_yes + score_no
        qual_shares = qual_yes + qual_no

        # Realistic competing depth floor: prevent 0.0 singularities
        # where the solver assumes a tiny quote captures 100% of the pool due to momentary order book flicker.
        # Only the payout model sees this floor; reported depth stays the audited sum.
        min_depth_floor = max(100.0, float(cand.get("min_size", 20.0)) * 5.0)
        comp_score = max(audited_comp_score, min_depth_floor)

        cand["competing_score"] = comp_score
        cand["qualifying_shares"] = qual_shares
        cand["competing_score_floor_applied"] = comp_score > audited_comp_score
        cand["real_mid"] = real_mid_yes

        total_score = comp_score + cand["our_score"]
        if total_score > 0 and cand["daily_pool"] > 0:
            raw_payout = cand["daily_pool"] * (cand["our_score"] / total_score)
        else:
            raw_payout = 0.0

        cand["raw_daily_payout"] = raw_payout
        # Strict enforcement of daily payout floor with safety buffer (>= $1.20)
        cand["clears_floor"] = raw_payout >= 1.20
        cand["real_daily_payout"] = raw_payout if cand["clears_floor"] else 0.0
        cand["daily_roi_pct"] = (cand["real_daily_payout"] / cand["capital_required"]) * 100.0
        cand["monthly_roi_pct"] = cand["daily_roi_pct"] * 30.0
        return cand

    scored: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(audit_market, c) for c in candidates]
        for f in as_completed(futures):
            res = f.result()
            # Enforce that audited real_mid is within safe probability bounds (including zero-competition pools)
            if res["competing_score"] >= 0.0 and (min_mid_price <= res["real_mid"] <= max_mid_price):
                scored.append(res)

    # Sort primarily by floor clearance, then Daily ROI descending
    scored.sort(
        key=lambda x: (x["clears_floor"], x["daily_roi_pct"], x["raw_daily_payout"]),
        reverse=True,
    )

    final_candidates = scored[:limit_output]
    # Store in 5-second TTL in-memory cache
    _SCANNER_CACHE[cache_key] = (now, final_candidates)
    GLOBAL_SCANNER_TELEMETRY.cached_candidates_count = len(final_candidates)
    GLOBAL_SCANNER_TELEMETRY.last_scan_timestamp = now

    return final_candidates


