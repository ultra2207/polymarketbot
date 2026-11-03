"""Dynamic Opportunity Scanner & Yield Optimization for Polymarket Liquidity Rewards.

Implements:
1. Dynamic REST querying across all available head reward pools.
2. Net Expected Marginal Yield calculation for micro-capital ($300 bankroll):
       Y_net = Daily Reward Payout($300, v, s, D_comp) + E[Spread Capture] - E[Adverse Selection] - Gas Fees
3. Strict enforcement of the $1.00 daily payout floor (Y_gross < $1.00 => $0.00).
4. Real-time ranking of markets by net yield per dollar.
"""

from __future__ import annotations

import math
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
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "question": self.question,
            "rate_per_day": round(self.rate_per_day, 2),
            "rewards_min_size": self.rewards_min_size,
            "rewards_max_spread": self.rewards_max_spread,
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


import re

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
    merge_threshold: float = 30.0,
    gas_fee_per_merge: float = 0.015,
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

    # 5. Expected Gas Fees
    filled_shares_daily = daily_fills * min(quote_size, 50.0) * 0.5
    expected_merges = filled_shares_daily / max(10.0, merge_threshold)
    gas_fees = expected_merges * gas_fee_per_merge

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
    url = f"{CLOB_REST_URL}/book"
    try:
        r = session.get(url, params={"token_id": token_id}, timeout=timeout)
        if r.status_code != 200:
            return 0.0, 0.0, 0.50
        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids and not asks:
            return 0.0, 0.0, 0.50

        best_bid = max(float(b.get("price", 0.0)) for b in bids) if bids else 0.0
        best_ask = min(float(a.get("price", 1.0)) for a in asks) if asks else 1.0
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

        return comp_score, total_qual_shares, mid
    except Exception as e:
        logger.debug(f"Error fetching order book for {token_id[:14]}: {e}")
        return 0.0, 0.0, 0.50


def find_optimal_lp_markets(
    bankroll_usd: float = 150.0,
    min_days_to_expiry: float = 7.0,
    min_mid_price: float = 0.12,
    max_mid_price: float = 0.88,
    max_candidates: int = 250,
    limit_output: int = 15,
) -> list[dict[str, Any]]:
    """Scan all reward pools and rank by Net Daily ROI (%) on deployed capital."""
    session = requests.Session()
    session.headers.update({"User-Agent": "polymarketbot/optimal-selector"})

    url = f"{CLOB_REST_URL}/rewards/markets/multi"
    params = {"order_by": "rate_per_day", "position": "DESC", "page_size": min(500, max_candidates)}

    logger.info(f"Querying top {max_candidates} reward pools from Polymarket CLOB...")
    try:
        resp = session.get(url, params=params, timeout=15.0)
        resp.raise_for_status()
        raw_markets = resp.json().get("data", [])
    except Exception as e:
        logger.error(f"Failed to fetch reward pools from CLOB: {e}")
        return []

    logger.info(f"Received {len(raw_markets)} markets. Applying risk & viability filters...")

    candidates = []
    now_utc = datetime.now(timezone.utc)

    for m in raw_markets:
        cid = m.get("condition_id")
        q = m.get("question", "Unknown")
        rewards_cfg = m.get("rewards_config", [])
        daily_pool = float(rewards_cfg[0].get("rate_per_day", 0.0)) if rewards_cfg else 0.0
        max_spread = float(m.get("rewards_max_spread", 3.0))
        min_size = float(m.get("rewards_min_size", 50.0))

        # Accept all active pools that have at least $1.00 daily budget
        if daily_pool < 1.0:
            continue

        tokens = m.get("tokens", [])
        if len(tokens) < 2:
            continue

        mid_yes = 0.50
        yes_token = None
        no_token = None
        for t in tokens:
            outcome = str(t.get("outcome", "")).upper()
            tid = t.get("token_id")
            if outcome == "YES":
                mid_yes = float(t.get("price", 0.50) or 0.50)
                yes_token = tid
            elif outcome == "NO":
                no_token = tid

        # 1. Divergence / Extreme Probability Filter (avoid markets where 1 side has already won)
        if not (min_mid_price <= mid_yes <= max_mid_price):
            continue

        # 2. Expiry / Resolution Filter (avoid markets ending soon with settlement jump risk)
        end_date_str = m.get("end_date_iso")
        days_left = 999.0
        if end_date_str:
            try:
                dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                days_left = (dt - now_utc).total_seconds() / 86400.0
                if days_left < min_days_to_expiry:
                    continue
            except Exception:
                pass

        # 3. Capital Sizing Feasibility Check
        # Deploy 75% for two-sided quoting, retain 25% cash buffer
        allocated_capital = bankroll_usd * 0.75
        quote_size = max(min_size, allocated_capital / 1.0)
        capital_required = quote_size * 1.0  # cost of buying YES + NO

        if capital_required > bankroll_usd * 0.95:
            # Requires more capital than our bankroll allows
            continue

        our_score = quadratic_order_score(max_spread, 0.50, quote_size)

        candidates.append({
            "condition_id": cid,
            "question": q,
            "daily_pool": daily_pool,
            "max_spread": max_spread,
            "min_size": min_size,
            "mid_yes": mid_yes,
            "days_left": days_left,
            "quote_size": quote_size,
            "capital_required": capital_required,
            "our_score": our_score,
            "yes_token": yes_token,
            "no_token": no_token,
            "raw_market": m,
        })

    logger.info(f"Screened {len(candidates)} high-safety markets. Auditing full L2 depth in parallel...")

    def audit_market(cand: dict[str, Any]) -> dict[str, Any]:
        s = requests.Session()
        s.headers.update({"User-Agent": "polymarketbot/optimal-selector"})
        comp_score, qual_shares, real_mid = get_real_l2_score(
            session=s,
            token_id=cand["yes_token"],
            max_spread_cents=cand["max_spread"],
        )
        cand["competing_score"] = comp_score
        cand["qualifying_shares"] = qual_shares
        cand["real_mid"] = real_mid

        total_score = comp_score + cand["our_score"]
        if total_score > 0 and cand["daily_pool"] > 0:
            raw_payout = cand["daily_pool"] * (cand["our_score"] / total_score)
        else:
            raw_payout = 0.0

        cand["raw_daily_payout"] = raw_payout
        # Strict enforcement of $1.00 daily payout floor
        cand["clears_floor"] = raw_payout >= 1.00
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

    return scored[:limit_output]

