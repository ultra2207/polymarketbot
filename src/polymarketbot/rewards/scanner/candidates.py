"""Candidate records and their construction from a screened raw market."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from polymarketbot.logs import get_logger
from polymarketbot.market import MarketConfig, TokenConfig
from polymarketbot.rewards.allocator import PROTOCOL_MIN_DAILY_PAYOUT_USD
from polymarketbot.rewards.scoring import quadratic_order_score

from .payload import (
    _MAX_REWARD_SPREAD_CENTS,
    _MIN_REWARD_SPREAD_CENTS,
    _coerce_float,
    _extract_daily_pool,
    _extract_market_metadata,
    _extract_market_tokens,
    _is_numeric_token_id,
    _market_state_rejection,
    _pair_cost_usd,
    _parse_iso_datetime,
)

logger = get_logger("rewards_scanner")



@dataclass
class MarketOpportunity:
    """Represents a scored liquidity reward market opportunity."""

    condition_id: str
    question: str
    rate_per_day: float
    rewards_min_size: float
    rewards_max_spread: float  # in cents (v)
    mid_price: Optional[float] = None
    """Measured YES mid; ``None`` = unmeasured (never a 0.50 stand-in)."""
    category: str = "default"
    competing_depth: Optional[float] = None
    """Measured in-band competing score of both token books; ``None`` = unmeasured."""
    competing_score: Optional[float] = None
    """Same measurement, kept for callers that price the payout share."""
    tokens: list[TokenConfig] = field(default_factory=list)
    event_id: Optional[str] = None

    # Scored yield components
    maker_score: float = 0.0
    gross_reward_payout: float = 0.0
    daily_reward_payout: Optional[float] = None  # None = unmeasured, never a stand-in
    expected_spread_capture: float = 0.0
    expected_adverse_selection: float = 0.0
    expected_gas_fees: float = 0.0
    net_expected_yield: float = 0.0  # Y_net
    net_yield_per_dollar: float = 0.0  # Y_net / allocated_capital
    passed_payout_floor: bool = False
    # False when the merge gas cost could not be queried: the market has no
    # measurable net yield and must not be adopted on an invented number.
    gas_measured: bool = True

    # Capital the **caller** allocated to this market (allocator.py sizes it),
    # the quote lifetime the fill table was read at, and the market's published
    # tick. All three are inputs, never defaults.
    allocated_capital_usd: Optional[float] = None
    quote_horizon_s: Optional[float] = None
    tick_size: Optional[float] = None
    # The measured quantities this score was built from: horizon fill
    # probability, required flow, signed markout, quote size, fills per day and
    # the market's tape regime (with its window and print count).
    measured_inputs: dict[str, Any] = field(default_factory=dict)

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
            "mid_price": round(self.mid_price, 4) if self.mid_price is not None else None,
            "category": self.category,
            "gross_reward_payout": round(self.gross_reward_payout, 2),
            "daily_reward_payout": (
                round(self.daily_reward_payout, 2)
                if self.daily_reward_payout is not None
                else None
            ),
            "expected_spread_capture": round(self.expected_spread_capture, 2),
            "expected_adverse_selection": round(self.expected_adverse_selection, 2),
            "expected_gas_fees": round(self.expected_gas_fees, 4),
            "net_expected_yield": round(self.net_expected_yield, 2),
            "net_yield_per_dollar": round(self.net_yield_per_dollar, 4),
            "passed_payout_floor": self.passed_payout_floor,
            "allocated_capital_usd": self.allocated_capital_usd,
            "quote_horizon_s": self.quote_horizon_s,
            "tick_size": self.tick_size,
            "measured_inputs": self.measured_inputs,
        }


def infer_market_category(question: str) -> str:
    """Descriptive label for a market question.

    This is a label only. It used to select ``scoring.CATEGORY_REGIMES``, a table
    of hand-set volatility, jump and trade-rate constants with no source and no
    window, which priced every market in a category off the same invented
    microstructure. Nothing in the yield calculation reads this any more: the
    regime is measured per market from the tape.
    """
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
    if daily_pool is None:
        return None, "daily_pool: unmeasured (no published campaign rate)"
    if daily_pool < PROTOCOL_MIN_DAILY_PAYOUT_USD:
        return None, (
            f"daily_pool: ${daily_pool:.2f}/day is below the "
            f"${PROTOCOL_MIN_DAILY_PAYOUT_USD:.2f} payout floor"
        )

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
    if mid_yes is None:
        return None, "mid_price: unmeasured (no published YES price)"
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

    # Capital sizing: the screen quotes both legs, and a complete pair costs
    # exactly its two leg prices. The pair's mids sum to the $1.00 it redeems
    # at, so ``bankroll / pair_cost`` shares per leg is what the bankroll can
    # actually fund. The old ``0.75`` deployment / ``0.95`` headroom pair was a
    # hand-set cash buffer applied per market on top of the allocator's own
    # endogenous reserve, which sizes the same reserve from measured activation
    # lots; keeping both double-counted it. The refusal below is now exact: the
    # venue's minimum lot does not fit the bankroll, or it does.
    pair_cost = _pair_cost_usd(raw_market, mid_yes)
    quote_size = max(min_size, bankroll_usd / pair_cost)
    capital_required = quote_size * pair_cost  # cost of buying YES + NO
    if capital_required > bankroll_usd:
        return None, (
            f"capital: min lot {min_size:.0f} shares needs ${capital_required:.2f} "
            f"> ${bankroll_usd:.2f} bankroll"
        )

    metadata = _extract_market_metadata(raw_market)
    tick_size = metadata["tick_size"]
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
        # Our quote rests at ``best_bid + tick``, which on a tick grid is at
        # least half a tick from the mid; ``alpha(tick/2)`` is therefore the
        # upper bound on the score this plan can earn, and the tick is the one
        # the venue published for this market.
        # The reward-campaign payload publishes no tick, so this is filled in
        # by the caller with the venue's own published value (see
        # ``fetch_tick_size``) before the payout is scored. ``None`` here means
        # "not yet resolved", never "assume a tick".
        "our_score": (
            quadratic_order_score(max_spread, tick_size * 100.0 / 2.0, quote_size)
            if tick_size is not None
            else None
        ),
        "quote_sides": 2,
        "yes_token": yes_token,
        "no_token": no_token,
        # Execution metadata required by the live runner; read, never invented.
        "neg_risk": metadata["neg_risk"],
        "tick_size": tick_size,
        "fee_rate_bps": metadata["fee_rate_bps"],
        "end_date_iso": metadata["end_date_iso"],
        "accepting_orders": metadata["accepting_orders"],
        "raw_market": raw_market,
    }, ""
