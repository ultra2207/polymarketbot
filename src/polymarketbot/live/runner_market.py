"""Market resolution and opportunistic rewards-market switching.

``resolve_market_details`` resolves the traded market, its real token ids and
execution metadata. The switching helpers decide whether an auto-selected
market should migrate and rebind the engine when it does. The command-line
coercion helpers they share live here too; ``polymarketbot.live.runner``
re-exports the names it uses.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from polymarketbot.fees import FeeScheduleUnreadable, schedule_for_market
from polymarketbot.gamma import GammaClient
from polymarketbot.logs import get_logger
from polymarketbot.market import TokenConfig
from polymarketbot.strategy.reward.scanner import (
    MarketOpportunity,
    OpportunityScanner,
    find_optimal_lp_markets,
    validate_market_for_live,
)
from polymarketbot.strategy.reward.switching import (
    OpportunisticSwitchingEngine,
    SwitchingConfig,
)
from polymarketbot.strategy.venue.clob import ClobAPIError
from polymarketbot.strategy.venue.feed import LivePolymarketFeed

if TYPE_CHECKING:  # the engine only appears in the rebind annotation
    from polymarketbot.live.engine import RealMoneyTradingEngine

log = get_logger("real_money_trader")

NUMERIC_TOKEN_PATTERN = re.compile(r"^\d+$")
# Market lifecycle and the opportunistic rewards-universe re-scan share this cadence.
MARKET_STATUS_INTERVAL = 60.0


# ---------------------------------------------------------------------------
# Small coercion helpers (never invent a value)
# ---------------------------------------------------------------------------
def _coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _numeric_token_id(value: Any) -> str | None:
    """Return a CLOB token id (plain decimal digits) or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if NUMERIC_TOKEN_PATTERN.fullmatch(stripped):
            return stripped
    return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_bool(*values: Any) -> bool | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1", "yes"):
                return True
            if lowered in ("false", "0", "no"):
                return False
    return None


def _payload_is_neg_risk(payload: dict[str, Any] | None) -> bool:
    """Whether a payload states neg-risk; an absent flag is not a yes.

    Auto-selection skips a market only on a stated ``true``. A payload that
    omits the field is left in place and the startup/switch gate decides with
    the authoritative CLOB value.
    """
    if not payload:
        return False
    return _first_bool(payload.get("neg_risk"), payload.get("negRisk")) is True


def _reward_rate(raw_market: dict[str, Any]) -> float:
    config = raw_market.get("rewards_config")
    if isinstance(config, (list, tuple)) and config:
        first = config[0]
        if isinstance(first, dict):
            rate = _coerce_float(first.get("rate_per_day"))
            if rate is not None:
                return rate
    return _coerce_float(raw_market.get("rewards_daily_rate")) or 0.0


def _market_category(question: str) -> str:
    lowered = question.lower()
    if any(k in lowered for k in ("senate", "house", "president", "election", "democrat", "republican", "tax", "vote")):
        return "politics"
    if any(k in lowered for k in ("btc", "eth", "crypto", "solana")):
        return "crypto"
    if any(k in lowered for k in ("nba", "nfl", "champion", "match", "cup")):
        return "sports"
    return "default"


def _settlement_available(ctf_client: Any) -> bool:
    enabled = getattr(ctf_client, "is_settlement_enabled", False)
    if callable(enabled):
        enabled = enabled()
    return bool(enabled)


# ---------------------------------------------------------------------------
# Opportunistic rewards-market switching
# ---------------------------------------------------------------------------
class MarketSwitchBlocked(RuntimeError):
    """The old market cannot be abandoned safely, so the migration is deferred."""


def market_opportunity(market: dict[str, Any], net_expected_yield: float) -> MarketOpportunity:
    """Describe the runner's active market as a scanner opportunity.

    The switching engine compares ``net_expected_yield`` against scanner-scored
    candidates, so the value must be the measured $/day of the active market.
    """
    mid = float(market.get("mid_price") or 0.5)
    return MarketOpportunity(
        condition_id=str(market["condition_id"]),
        question=str(market.get("question") or ""),
        rate_per_day=float(market.get("daily_reward") or 0.0),
        rewards_min_size=float(market.get("min_size") or 0.0),
        rewards_max_spread=float(market.get("max_spread") or 0.0),
        mid_price=mid,
        category=str(market.get("category") or "default"),
        tokens=[
            TokenConfig(token_id=str(market["token_yes"]), outcome="YES", price=mid),
            TokenConfig(token_id=str(market["token_no"]), outcome="NO"),
        ],
        event_id=market.get("event_id"),
        daily_reward_payout=float(market.get("daily_reward") or 0.0),
        net_expected_yield=float(net_expected_yield),
        passed_payout_floor=True,
    )


def build_switching_engine(
    market: dict[str, Any],
    *,
    bankroll_usd: float,
    live_yield: float,
) -> OpportunisticSwitchingEngine | None:
    """Build the rewards re-scan engine for an auto-selected market.

    A pinned ``--condition-id`` is an operator decision and never migrates.
    The hurdle and its amortisation horizon are not configured here: the engine
    solves both from measured quantities (round-trip cost per dollar moved and
    the payback period). This function supplies the one measurement the engine
    cannot take itself - the persisted CLOB round trip that prices the unquoted
    interval.
    """
    if not market.get("auto_selected"):
        return None
    from polymarketbot.live.engine.deskew import persisted_clob_rtt_s

    config = SwitchingConfig(
        scan_interval_seconds=MARKET_STATUS_INTERVAL,
        round_trip_latency_seconds=persisted_clob_rtt_s(),
    )
    switcher = OpportunisticSwitchingEngine(
        scanner=OpportunityScanner(),
        config=config,
        bankroll_usd=bankroll_usd,
        concentration_pct=1.0,
    )
    switcher.set_active_market(market_opportunity(market, net_expected_yield=live_yield))
    return switcher


def rebind_engine_to_market(
    engine: RealMoneyTradingEngine,
    details: dict[str, Any],
    *,
    quote_size: float,
    spread_offset_cents: float,
) -> float:
    """Migrate a live engine from its current market to ``details``.

    The caller cancels the old quotes and halts quoting first; this function
    merges any complete sets it can, swaps every market-scoped field and clears
    book/microstructure state so the old market cannot price the new one.
    Returns the USDC unlocked by the merge (0.0 when there was nothing to merge).

    Raises:
        MarketSwitchBlocked: when the target market's fee schedule cannot be
            read, or when outcome-token inventory remains after the merge
            attempt. ``RealMoneyTradingEngine`` tracks one market's inventory
            at a time, so migrating with old shares would mark them against the
            new market.
    """
    # The switch is refused before anything on the engine is mutated: a market
    # with an unreadable fee curve cannot be priced, and the runner keeps the
    # old market rather than quoting the new one with stale fees.
    raw_market = details.get("raw_market")
    try:
        if not isinstance(raw_market, dict):
            raise FeeScheduleUnreadable(
                f"market {details.get('condition_id')!r} carries no raw market payload"
            )
        fee_schedule = schedule_for_market(raw_market)
    except FeeScheduleUnreadable as exc:
        log.error(f"[MARKET SWITCH] Refusing switch: {exc}")
        raise MarketSwitchBlocked(
            f"fee schedule for the target market is unreadable: {exc}"
        ) from exc

    unlocked_cash = 0.0
    try:
        merged = engine.trigger_ctf_merge()
    except Exception as exc:  # noqa: BLE001 - a failed merge must not leave quoting halted
        merged = None
        log.error(f"[MARKET SWITCH] Merge of the old market failed: {exc}")
    if merged is not None:
        unlocked_cash = float(getattr(merged, "usdc_unlocked", 0.0) or 0.0)

    remaining_yes = float(getattr(engine, "shares_yes", 0.0) or 0.0)
    remaining_no = float(getattr(engine, "shares_no", 0.0) or 0.0)
    if remaining_yes > 1e-9 or remaining_no > 1e-9:
        raise MarketSwitchBlocked(
            f"the old market still holds YES {remaining_yes:.6f} / "
            f"NO {remaining_no:.6f} after the complete-set merge"
        )

    old_cid = str(engine.condition_id)

    # Old-market fills queued for markout must stay NULL rather than be priced
    # against the new market's book.
    tracker = getattr(engine, "_markout_tracker", None)
    if tracker is not None:
        tracker.stop()

    engine.condition_id = details["condition_id"]
    engine.asset_id_yes = details["token_yes"]
    engine.asset_id_no = details["token_no"]
    engine.daily_reward_rate = float(details["daily_reward"])
    engine.rewards_max_spread = float(details["max_spread"])
    engine.rewards_min_size = float(details["min_size"])
    engine.quote_size = float(quote_size)
    engine.spread_offset_cents = float(spread_offset_cents)
    engine.tick_size = float(details["tick_size"])
    engine.quote_tick_size = engine.tick_size
    engine.neg_risk = bool(details["neg_risk"])
    engine.event_id = details.get("event_id")
    engine.fee_rate_bps = int(details["fee_rate_bps"])
    engine.min_order_usd = float(details["order_min_usd"])
    # The target market's measured fee curve replaces the old market's; the
    # cached de-skew optimizer embeds that curve and its thresholds, so it must
    # be rebuilt against the new market on next use.
    engine.fee_schedule = fee_schedule
    engine._deskew_optimizer = None
    engine.days_to_resolution = float(details["days_to_resolution"])
    engine.competing_score_override = details.get("competing_score_override")
    engine.market_closed = False
    # The old market is verified flat, so its cost basis must not carry over.
    engine.avg_cost_yes = 0.0
    engine.avg_cost_no = 0.0

    engine._best_bids.clear()
    engine._best_asks.clear()
    engine._book_ts = {"YES": 0.0, "NO": 0.0}
    engine._rolling_price_ticks.clear()
    engine._volatility_cooldown_until = 0.0
    engine._trigger_instant_scan = False
    engine._deskew_order = None
    engine._last_deskew_attempt_ts = 0.0
    engine.verified_scoring_orders.clear()
    engine.micro_risk.reset()
    engine.micro_risk.tick_size = engine.tick_size
    engine.latest_switching_cost = None
    engine.latest_hurdle_decision = None
    engine.governor.market_entry_times.pop(old_cid, None)
    engine.governor.market_entry_times[engine.condition_id] = time.time()
    engine.governor.current_allocations.pop(old_cid, None)
    # Imported here so the market module never depends on the engine module at
    # import time; ``live.engine`` re-exports MarkoutTracker for this path.
    from polymarketbot.live.engine import MarkoutTracker

    engine._markout_tracker = MarkoutTracker(
        conn=engine._conn,
        mid_getter=engine._fresh_mid,
        lock=engine._lock,
    )
    return unlocked_cash


# ---------------------------------------------------------------------------
# Market resolution
# ---------------------------------------------------------------------------
def _resolve_token(requested: str | None, discovered: str | None, label: str, cid: str) -> str:
    """Resolve one token id, refusing a flag that disagrees with the CLOB.

    The CLOB market detail is authoritative: a stale ``--token-*`` (for example
    a Gamma snapshot taken before a market migration) must not silently place
    orders on a different asset. An explicit flag is allowed only when it
    matches the discovered token, or when the payload genuinely has none.
    """
    requested_token = _numeric_token_id(requested) if requested is not None else None
    if requested is not None and requested_token is None:
        raise SystemExit(
            f"--token-{label.lower()} is not a numeric CLOB token id: {requested!r}"
        )
    if discovered is not None:
        if requested_token is not None and requested_token != discovered:
            raise SystemExit(
                f"--token-{label.lower()} {requested_token} does not match the CLOB "
                f"{label} token {discovered} for {cid}; refusing to trade a stale token. "
                f"Omit --token-{label.lower()} to use the CLOB token."
            )
        return discovered
    if requested_token is not None:
        return requested_token
    raise SystemExit(
        f"Market {cid} has no numeric {label} token id in its payload. "
        f"Supply --token-{label.lower()} explicitly; token ids are never fabricated."
    )


def resolve_market_details(
    feed: LivePolymarketFeed,
    clob: Any,
    requested_cid: str | None = None,
    requested_yes: str | None = None,
    requested_no: str | None = None,
    bankroll: float = 150.0,
    min_size: float | None = None,
    max_spread: float | None = None,
    daily_reward: float | None = None,
    *,
    find_markets: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Resolve the market, real token ids and execution metadata.

    ``find_markets`` lets the runner pass its own rewards-universe scan so an
    operator/test override of ``find_optimal_lp_markets`` on the runner module
    keeps working after the split.

    Raises:
        SystemExit: when a market or token cannot be resolved. Nothing is
            fabricated to keep the loop running.
    """
    scan_markets = find_markets or find_optimal_lp_markets
    log.info("Resolving market details from Polymarket CLOB API...")

    markets = feed.get_rewards_markets_sync() or []
    # ``clob.dry_run`` is False only in a real-money run. The neg-risk skip is
    # live-only: a dry-run has no settlement path to refuse.
    live = not bool(getattr(clob, "dry_run", True))
    target_market: dict[str, Any] | None = None
    competing_score_override: float | None = None
    daily_reward_override: float | None = None

    if requested_cid:
        requested_lower = requested_cid.strip().lower()
        for market in markets:
            if str(market.get("condition_id", "")).strip().lower() == requested_lower:
                target_market = market
                break
        if target_market is None and not (requested_yes and requested_no):
            raise SystemExit(
                f"Condition id {requested_cid!r} is not in the CLOB rewards list. "
                "Pass both --token-yes and --token-no to quote it anyway."
            )
    else:
        log.info(
            f"Auto-scanning the Polymarket universe for a market fitting a "
            f"${bankroll:.2f} sizing cap..."
        )
        try:
            optimal_markets = scan_markets(bankroll_usd=bankroll, limit_output=5)
        except Exception as exc:  # noqa: BLE001 - caller-supplied scan; fall back to rate sort
            log.warning(f"Optimal market scan failed ({exc}); falling back to the rewards rate sort.")
            optimal_markets = []
        if optimal_markets:
            # Live trading cannot settle a neg-risk market (no NegRiskAdapter),
            # so choosing one only guarantees a startup refusal. Skip a stated
            # neg-risk candidate and let the next one through.
            best = next(
                (
                    candidate
                    for candidate in optimal_markets
                    if not (
                        live
                        and _payload_is_neg_risk(candidate.get("raw_market"))
                    )
                ),
                None,
            )
            if best is not None:
                target_market = best.get("raw_market")
                competing_score_override = _coerce_float(best.get("competing_score"))
                daily_reward_override = _coerce_float(best.get("daily_pool"))
                log.info(
                    f"[OPTIMAL SWEET SPOT DETECTED] Auto-selected: '{best.get('question', 'n/a')}' "
                    f"(Pool: ${daily_reward_override or 0.0:,.2f}/day, "
                    f"Competing depth: {best.get('qualifying_shares', 0.0):,.0f} sh, "
                    f"Expected payout: ${best.get('real_daily_payout', 0.0):.2f}/day)"
                )
        if target_market is None and markets:
            skipped_neg_risk = 0
            for market in sorted(markets, key=_reward_rate, reverse=True):
                if market.get("closed") is True or market.get("archived") is True:
                    continue
                if live and _payload_is_neg_risk(market):
                    skipped_neg_risk += 1
                    continue
                tokens = market.get("tokens") or []
                if len(tokens) >= 2:
                    target_market = market
                    break
            if skipped_neg_risk:
                log.warning(
                    f"[MARKET SELECT] Skipped {skipped_neg_risk} neg-risk market(s): "
                    "the live engine cannot settle them (no NegRiskAdapter path)."
                )
        if target_market is None:
            raise SystemExit(
                "No open rewards market could be resolved from the CLOB rewards list. "
                "Supply --condition-id with --token-yes and --token-no."
            )

    detail: dict[str, Any] = {}
    if target_market is not None:
        raw_market: dict[str, Any] = dict(target_market)
        cid = _first_string(raw_market.get("condition_id"), requested_cid)
        if cid is None:
            raise SystemExit("Resolved market payload has no condition_id.")
        try:
            detail = clob.get_market(cid) or {}
        except (ClobAPIError, ValueError) as exc:
            log.warning(
                f"GET /markets/{cid} failed ({exc}); using the rewards payload metadata. "
                "tick size / neg risk may be resolved per-token instead."
            )
    else:
        assert requested_cid is not None  # guarded above
        cid = requested_cid.strip()
        try:
            detail = clob.get_market(cid) or {}
        except Exception as exc:
            raise SystemExit(f"Cannot resolve market {cid!r} from the CLOB: {exc}") from exc
        question = _first_string((detail.get("raw") or {}).get("question"), detail.get("question"))
        raw_market = {
            "condition_id": cid,
            "question": question or f"Condition {cid}",
            "tokens": [
                {"outcome": "YES", "token_id": requested_yes},
                {"outcome": "NO", "token_id": requested_no},
            ],
            "rewards_min_size": min_size,
            "rewards_max_spread": max_spread,
            "rewards_daily_rate": daily_reward if daily_reward is not None else 0.0,
            "closed": detail.get("closed"),
            "accepting_orders": detail.get("accepting_orders"),
            "end_date_iso": detail.get("end_date_iso"),
            "neg_risk": detail.get("neg_risk"),
            "tick_size": detail.get("tick_size"),
            "fee_rate_bps": detail.get("fee_rate_bps"),
        }

    # The reward payload carries the event id; when the market came from a
    # bare CLOB lookup, fall back to the universe metadata table so the
    # correlated-event risk cap still applies.
    if not raw_market.get("event_id"):
        try:
            from polymarketbot.data.universe_metadata import load_rows

            raw_market["event_id"] = (
                load_rows().get(str(cid).lower()) or {}
            ).get("event_id")
        except Exception:  # noqa: BLE001 - metadata is advisory, not a startup gate
            raw_market["event_id"] = None

    # The CLOB market detail is authoritative for token ids. A rewards-payload
    # token or a CLI flag can be stale (Gamma snapshot, market migration).
    detail_raw = detail.get("raw") if isinstance(detail, dict) else None
    detail_tokens = detail_raw.get("tokens") if isinstance(detail_raw, dict) else None
    if not detail_tokens and isinstance(detail, dict):
        detail_tokens = detail.get("tokens")
    if detail_tokens:
        raw_market["tokens"] = [
            {
                "outcome": str(token.get("outcome", "")).strip().upper(),
                "token_id": token.get("token_id"),
                "price": token.get("price"),
            }
            for token in detail_tokens
            if isinstance(token, dict)
        ]

    # Token ids: the market payload is authoritative; an explicit flag must match.
    discovered_yes: str | None = None
    discovered_no: str | None = None
    payload_mid: float | None = None
    for token in raw_market.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        outcome = str(token.get("outcome", "")).strip().upper()
        if outcome == "YES":
            discovered_yes = _numeric_token_id(token.get("token_id"))
            price = _coerce_float(token.get("price"))
            if price is not None and 0.0 < price < 1.0:
                payload_mid = price
        elif outcome == "NO":
            discovered_no = _numeric_token_id(token.get("token_id"))

    token_yes = _resolve_token(requested_yes, discovered_yes, "YES", cid)
    token_no = _resolve_token(requested_no, discovered_no, "NO", cid)

    mid_price = payload_mid
    if mid_price is None:
        try:
            book = feed.get_book_sync(token_yes)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            book = None
            log.warning(f"Could not fetch an order book for {token_yes}: {exc}")
        if book is not None and book.bids and book.asks:
            mid_price = (max(level.price for level in book.bids) + min(level.price for level in book.asks)) / 2.0
    if mid_price is None:
        raise SystemExit(
            f"No YES mid price available for token {token_yes}: the market payload "
            "carries no price and the order book is empty."
        )

    # Per-token fallback only when neither the payload nor GET /markets supplied metadata.
    tick_size = _coerce_float(raw_market.get("tick_size"))
    if tick_size is None:
        tick_size = _coerce_float(raw_market.get("minimum_tick_size"))
    if tick_size is None:
        tick_size = _coerce_float(detail.get("tick_size"))
    if tick_size is None or tick_size <= 0.0:
        try:
            tick_size = float(clob.get_tick_size(token_yes))
        except Exception as exc:
            raise SystemExit(f"Cannot resolve tick size for token {token_yes}: {exc}") from exc
    if tick_size is None or tick_size <= 0.0:
        raise SystemExit(f"Cannot resolve a positive tick size for token {token_yes}.")

    # The venue's own order minimum (Gamma ``orderMinSize``, in USDC). Required:
    # a sub-minimum order is rejected by the CLOB after a full signing and
    # submit round trip, and a guessed minimum would size the quote instead.
    min_order_usd = GammaClient.min_order_size(raw_market)
    if min_order_usd is None:
        min_order_usd = GammaClient.min_order_size(detail.get("raw") or {})
    if min_order_usd is None:
        min_order_usd = _coerce_float(detail.get("order_min_size"))
    if min_order_usd is None or min_order_usd <= 0.0:
        raise SystemExit(
            f"Market {cid} publishes no usable orderMinSize; refusing to size an "
            "order against an unknown venue minimum."
        )

    neg_risk = _first_bool(raw_market.get("neg_risk"), detail.get("neg_risk"))
    if neg_risk is None:
        try:
            neg_risk = bool(clob.get_neg_risk(token_yes))
        except Exception as exc:
            raise SystemExit(f"Cannot resolve neg_risk for token {token_yes}: {exc}") from exc

    fee_rate_bps_raw = raw_market.get("fee_rate_bps")
    if fee_rate_bps_raw is None:
        fee_rate_bps_raw = raw_market.get("taker_base_fee")
    if fee_rate_bps_raw is None:
        fee_rate_bps_raw = detail.get("fee_rate_bps")
    fee_value = _coerce_float(fee_rate_bps_raw)
    if fee_value is not None and fee_value < 0.0:
        raise SystemExit(f"Market {cid} reports a negative fee rate: {fee_rate_bps_raw!r}")
    fee_rate_bps = round(fee_value) if fee_value is not None else 0

    # The CLOB rewards payload publishes ``end_date``; gamma/CLOB detail may
    # publish either. Reading only ``end_date_iso`` rejected every rewarded
    # market the rewards list supplied.
    end_date_iso = _first_string(
        raw_market.get("end_date_iso"),
        raw_market.get("end_date"),
        detail.get("end_date_iso"),
        detail.get("end_date"),
    )

    # Effective reward config: a documented ``--min-size`` / ``--max-spread``
    # override wins over the payload, and the payload is validated with those
    # effective values so an override can rescue a market whose raw payload
    # would be rejected by the gates wrong-side-out.
    #
    # Validate the payload we will actually trade, with resolved token ids and
    # the effective overrides.
    validated_payload: dict[str, Any] = dict(raw_market)
    validated_payload["tokens"] = [
        {"outcome": "YES", "token_id": token_yes, "price": mid_price},
        {"outcome": "NO", "token_id": token_no},
    ]
    validated_payload["end_date_iso"] = end_date_iso
    resolved_min_size = _coerce_float(min_size)
    if resolved_min_size is None:
        resolved_min_size = _coerce_float(validated_payload.get("rewards_min_size"))
    if resolved_min_size is not None:
        validated_payload["rewards_min_size"] = resolved_min_size
    resolved_max_spread = _coerce_float(max_spread)
    if resolved_max_spread is None:
        resolved_max_spread = _coerce_float(validated_payload.get("rewards_max_spread"))
    if resolved_max_spread is not None:
        validated_payload["rewards_max_spread"] = resolved_max_spread
    ok, reason = validate_market_for_live(validated_payload)
    if not ok:
        raise SystemExit(f"Market {cid} is not safe to quote live: {reason}")

    end_dt = _parse_iso_datetime(end_date_iso)
    if end_dt is None:
        raise SystemExit(f"Market {cid} end_date_iso is unparseable: {end_date_iso!r}")
    now_utc = datetime.now(UTC)
    days_to_resolution = max((end_dt - now_utc).total_seconds() / 86400.0, 1.0 / 24.0)

    if resolved_min_size is None:
        raise SystemExit(f"Market {cid} has no rewards_min_size; supply --min-size.")
    if resolved_min_size <= 0.0:
        raise SystemExit(f"Market {cid} resolved a non-positive min size: {resolved_min_size}.")
    if resolved_max_spread is None:
        raise SystemExit(f"Market {cid} has no rewards_max_spread; supply --max-spread.")
    if resolved_max_spread <= 0.0:
        raise SystemExit(f"Market {cid} resolved a non-positive max spread: {resolved_max_spread}.")

    if daily_reward is not None:
        resolved_daily_reward = float(daily_reward)
    elif daily_reward_override is not None:
        resolved_daily_reward = daily_reward_override
    else:
        resolved_daily_reward = _reward_rate(raw_market)
    if resolved_daily_reward <= 0.0:
        log.warning(
            f"Market {cid} reports no daily reward pool; reward modelling will stay at zero."
        )

    question = _first_string(raw_market.get("question"), f"Condition {cid}")
    assert question is not None

    payload_spread_cents = _coerce_float(raw_market.get("spread"))
    if payload_spread_cents is not None:
        market_spread_cents = payload_spread_cents * 100.0
    else:
        market_spread_cents = tick_size * 100.0
        log.debug(f"No market spread in the payload for {cid}; using one tick as the estimate.")

    return {
        "condition_id": cid,
        "question": question,
        "token_yes": token_yes,
        "token_no": token_no,
        "auto_selected": requested_cid is None,
        "daily_reward": resolved_daily_reward,
        "max_spread": resolved_max_spread,
        "min_size": resolved_min_size,
        "mid_price": mid_price,
        "market_spread_cents": market_spread_cents,
        "tick_size": tick_size,
        "tick_size_cents": tick_size * 100.0,
        "neg_risk": bool(neg_risk),
        "event_id": raw_market.get("event_id"),
        "fee_rate_bps": fee_rate_bps,
        "order_min_usd": min_order_usd,
        "end_date_iso": end_date_iso,
        "end_timestamp": end_dt.timestamp(),
        "days_to_resolution": days_to_resolution,
        "category": _market_category(question),
        "competing_score_override": competing_score_override,
        "raw_market": validated_payload,
    }
