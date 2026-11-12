"""Raw reward-market payload readers and the live-trading gate."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from polymarketbot.logs import get_logger

logger = get_logger("rewards_scanner")


_NUMERIC_TOKEN_ID_PATTERN = re.compile(r"[0-9]+")
_MIN_REWARD_SPREAD_CENTS = 0.1
_MAX_REWARD_SPREAD_CENTS = 10.0
_STATE_BOOLEAN_FIELDS = ("closed", "archived")


def _is_numeric_token_id(value: Any) -> bool:
    """True only for a CLOB token id: a plain decimal digit string."""
    return isinstance(value, str) and _NUMERIC_TOKEN_ID_PATTERN.fullmatch(value.strip()) is not None


def _coerce_float(value: Any) -> float | None:
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


def _coerce_bool(value: Any) -> bool | None:
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
    """Read the execution metadata the live runner needs from a raw CLOB payload.

    ``tick_size`` is ``None`` when the payload does not publish one: the tick is
    the grid every quote and every ``alpha`` distance is measured on, so a
    stand-in (the old ``0.01``) silently re-priced the market. Callers refuse
    the payload instead.
    """
    tick_size = _coerce_float(_first_present(raw_market, "tick_size", "minimum_tick_size"))
    if tick_size is not None and tick_size <= 0.0:
        tick_size = None
    fee_rate = _coerce_float(_first_present(raw_market, "fee_rate_bps", "feeRateBps"))
    fee_rate_bps = round(fee_rate) if fee_rate is not None and fee_rate >= 0.0 else 0
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
) -> tuple[str | None, str | None, float | None]:
    """Return (YES token id, NO token id, YES mid price) without inventing values.

    ``mid_yes`` is ``None`` when no YES token publishes a price in (0, 1): the
    old ``0.50`` stand-in priced every unreadable market at 50c.
    """
    yes_token: str | None = None
    no_token: str | None = None
    mid_yes: float | None = None
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


def _extract_daily_pool(raw_market: dict[str, Any]) -> float | None:
    """The market's published daily reward pool, summed over its campaigns.

    Returns ``None`` when the payload publishes no rate at all. The old
    version read only the first campaign (a market can carry several, and the
    universe walk sums them) and returned ``0.0`` for a missing rate, which is
    a pool of nothing rather than an unknown pool.
    """
    rewards_cfg = raw_market.get("rewards_config")
    campaigns = list(rewards_cfg) if isinstance(rewards_cfg, (list, tuple)) else []
    total: float | None = None
    for campaign in campaigns:
        if not isinstance(campaign, dict):
            continue
        rate = _coerce_float(campaign.get("rate_per_day"))
        if rate is None or rate < 0.0:
            continue
        total = rate if total is None else total + rate
    if total is not None:
        return total
    return _coerce_float(raw_market.get("rewards_daily_rate"))


def _parse_iso_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _market_state_rejection(raw_market: dict[str, Any]) -> str | None:
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
    now: datetime | None = None,
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
            ("rewards_max_spread: must be within "
            f"[{_MIN_REWARD_SPREAD_CENTS}, {_MAX_REWARD_SPREAD_CENTS}] cents, "
            f"got {raw_market.get('rewards_max_spread')!r}"),
        )

    state_reason = _market_state_rejection(raw_market)
    if state_reason is not None:
        return False, state_reason

    end_date_raw = raw_market.get("end_date_iso")
    end_date = _parse_iso_datetime(end_date_raw)
    if end_date is None:
        return False, f"end_date_iso: missing or unparseable ({end_date_raw!r})"
    now_utc = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    if end_date <= now_utc:
        return False, f"end_date_iso: market already ended at {end_date_raw}"

    return True, "ok"


def _pair_cost_usd(raw_market: dict[str, Any], mid_yes: float) -> float:
    """USDC cost of one complete pair: one YES share plus one NO share.

    Measured from the payload's two token prices when both are published. A
    complete set redeems for exactly $1.00, so the two mids sum to that par
    when only one side is quoted; the fallback is the redemption value, not a
    share of the bankroll.
    """
    prices: dict[str, float] = {}
    for token in raw_market.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        outcome = str(token.get("outcome", "")).upper()
        price = _coerce_float(token.get("price"))
        if outcome in ("YES", "NO") and price is not None and 0.0 < price < 1.0:
            prices[outcome] = price
    if "YES" in prices and "NO" in prices:
        return prices["YES"] + prices["NO"]
    return 1.0
