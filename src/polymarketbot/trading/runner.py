"""Operator runner for the real-money Polymarket reward market-making engine.

This runner does exactly one job: connect the CLOB/CTF clients and the live
market feed to ``RealMoneyTradingEngine`` and supervise the loop. It does not
compound capital or derive APR from a single sample. An auto-selected rewards
market is re-scanned on the market-lifecycle cadence and migrated only when the
measured yield improvement clears ``rewards.switching``'s own switching cost
(gas + merge gas + crossing friction); a pinned ``--condition-id`` never moves.

Safety model:
1. Real orders are placed only when ``--live`` is supplied. Without it the run
   is a dry-run even when credentials are configured.
2. ``--live`` without ``--yes`` requires an interactive TTY and the exact
   confirmation string ``START LIVE``; anything else aborts with exit code 2.
3. After startup reconciliation and preflight, a live run aborts with exit
   code 1 when preflight is not ready. The runner never downgrades to dry-run.
4. The CLOB collateral balance is the wallet. ``--budget-pct``/``--max-budget``
   only produce a sizing cap, never cash. On-chain USDC/POL reads are gas
   diagnostics only.
5. Token ids are never fabricated. A market that cannot be resolved aborts.
6. Quoting halts when the feed goes stale or the market stops accepting orders;
   resting orders are reported as still live because a halt does not cancel them.
7. A market migration is refused unless the old market's quotes are confirmed
   cancelled and its outcome-token inventory is verified flat after a complete-set
   merge; the single-market engine would otherwise misattribute those shares.

Exit codes: 0 = clean shutdown with cancellation confirmed by the venue;
1 = aborted (configuration, balance, preflight or API failure); 2 = live
confirmation or market gate refused; 3 = shutdown cancellation could NOT be
confirmed, so orders may still be live.

Usage::

    uv run python -m polymarketbot.trading.runner --live --yes --budget-pct 0.20
"""

from __future__ import annotations

import argparse
import asyncio
import math
import re
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

import requests

from polymarketbot.logs import IST, get_logger
from polymarketbot.market import TokenConfig
from polymarketbot.rewards.scanner import (
    MarketOpportunity,
    OpportunityScanner,
    find_optimal_lp_markets,
    get_real_l2_score,
    validate_market_for_live,
)
from polymarketbot.rewards.switching import (
    OpportunisticSwitchingEngine,
    SwitchDecision,
    SwitchEvent,
    SwitchingConfig,
)
from polymarketbot.strategy.sizing import derive_quote_size, optimize_spread_offset
from polymarketbot.trading.client import ClobAuthCredentials, LiveClobClient
from polymarketbot.trading.ctf import PolygonCtfClient
from polymarketbot.trading.engine import MarkoutTracker, RealMoneyTradingEngine
from polymarketbot.trading.feed import LivePolymarketFeed

log = get_logger("real_money_trader")

# ANSI color codes for high-density terminal telemetry
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_CYAN = "\033[36m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_WHITE = "\033[37m"

LIVE_CONFIRMATION_PHRASE = "START LIVE"
NUMERIC_TOKEN_PATTERN = re.compile(r"^\d+$")

# Seconds between blocking maintenance tasks (all executed off the event loop).
ORDERS_SYNC_INTERVAL = 5.0
BALANCE_SYNC_INTERVAL = 15.0
# Market lifecycle and the opportunistic rewards-universe re-scan share this cadence.
MARKET_STATUS_INTERVAL = 60.0
LOOP_SLEEP_SECONDS = 0.2
REDEEM_RETRY_INTERVAL = 60.0
# Shutdown cancellation must be proven; otherwise the operator gets exit code 3.
CANCEL_RETRY_ATTEMPTS = 3
CANCEL_RETRY_BACKOFF_SECONDS = 1.5
EXIT_ORDERS_UNCONFIRMED = 3
# APR is only shown once the reward sampler has at least this many epochs.
MIN_APR_SAMPLES = 60


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Polymarket real-money reward market-making runner. Dry-run unless "
            "--live is supplied; --live additionally requires a ready preflight."
        )
    )
    parser.add_argument(
        "--condition-id",
        type=str,
        default=None,
        help="Polymarket condition ID to trade (default: auto-select a rewards market).",
    )
    parser.add_argument(
        "--token-yes",
        type=str,
        default=None,
        help="CLOB token id for the YES outcome (required when the condition id is not in the rewards list).",
    )
    parser.add_argument(
        "--token-no",
        type=str,
        default=None,
        help="CLOB token id for the NO outcome (required when the condition id is not in the rewards list).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Place REAL orders. Without this flag the runner is a dry-run even with credentials.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive START LIVE confirmation (only meaningful with --live).",
    )
    parser.add_argument(
        "--record-live-data",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Write the private session (orders, fills, quotes, account, risk, rewards) "
            "to data/live_tape. Default: on for live, off for dry-run."
        ),
    )
    parser.add_argument(
        "--budget-pct",
        type=float,
        default=1.0,
        help="Fraction of the CLOB wallet used as a sizing cap (0.20 or 20 for 20%%, default: 1.0).",
    )
    parser.add_argument(
        "--max-budget",
        type=float,
        default=None,
        help="Absolute sizing-cap ceiling in USD (default: none).",
    )
    parser.add_argument(
        "--initial-cash",
        type=float,
        default=100.0,
        help="Dry-run only: simulated wallet balance in USD (default: 100.0). Ignored when live.",
    )
    parser.add_argument(
        "--quote-size",
        type=float,
        default=None,
        help="Maker quote size in shares (default: derived from the sizing cap).",
    )
    parser.add_argument(
        "--spread-offset",
        type=float,
        default=None,
        help="Half-spread offset from mid in cents (default: optimized for the market).",
    )
    parser.add_argument(
        "--daily-reward",
        type=float,
        default=None,
        help="Daily reward pool in USD (default: from the market rewards config).",
    )
    parser.add_argument(
        "--max-spread",
        type=float,
        default=None,
        help="Qualifying max spread in cents for rewards (default: from the market).",
    )
    parser.add_argument(
        "--min-size",
        type=float,
        default=None,
        help="Qualifying minimum order size in shares (default: from the market).",
    )
    parser.add_argument(
        "--competing-score",
        type=float,
        default=None,
        help="Hardcoded competing L2 score override (default: audited from live CLOB L2 depth).",
    )
    parser.add_argument(
        "--auto-merge-threshold",
        type=float,
        default=10.0,
        help="Complete-set threshold before the engine attempts a CTF merge (default: 10.0).",
    )
    parser.add_argument(
        "--legacy-mode",
        action="store_true",
        help="Quote BUY YES + SELL YES instead of dual-cash BUY YES + BUY NO.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Duration to run in seconds (default: until Ctrl+C).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Dashboard render interval in seconds (default: 1.0s).",
    )
    parser.add_argument(
        "--reward-refresh-interval",
        type=float,
        default=60.0,
        help="L2 competing-score and reward-epoch refresh interval in seconds (default: 60.0s).",
    )
    parser.add_argument(
        "--max-feed-staleness",
        type=float,
        default=30.0,
        help="Seconds without a feed frame before quoting is halted (default: 30.0s).",
    )
    parser.add_argument(
        "--redeem-on-close",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Redeem resolved positions after the market closes (default: enabled).",
    )
    parser.add_argument(
        "--allow-no-settlement",
        action="store_true",
        help=(
            "Allow live quoting when on-chain CTF settlement is unavailable (proxy wallet "
            "or no signing key). Inventory can then only be exited by selling on the CLOB."
        ),
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="data/real_money_trading.db",
        help="SQLite database path for state persistence.",
    )
    parser.add_argument(
        "--ledger-path",
        type=str,
        default="data/real_money_trading_ledger.json",
        help="JSON ledger path for the audit trail.",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Disable ANSI screen clearing for log-friendly streaming.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run even if --live is also supplied.",
    )
    parser.add_argument(
        "--rpc-url",
        type=str,
        default=None,
        help="Polygon JSON-RPC endpoint URL override.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Live gate
# ---------------------------------------------------------------------------
def require_live_confirmation(
    live: bool,
    yes: bool,
    stdin_is_tty: bool,
    ask: Callable[[str], str] = input,
) -> bool:
    """Return True only when real orders are allowed to be placed.

    Dry-run runs never need confirmation. ``--yes`` acknowledges ``--live``
    non-interactively. Otherwise the operator must have a TTY and type the exact
    phrase ``START LIVE``.
    """
    if not live:
        return True
    if yes:
        return True
    if not stdin_is_tty:
        log.error(
            "Refusing to trade live: stdin is not a terminal, so the START LIVE "
            "confirmation cannot be requested. Re-run with --yes only if you "
            "intend to place real orders."
        )
        return False
    try:
        answer = ask(f"Type '{LIVE_CONFIRMATION_PHRASE}' to place REAL orders on Polymarket: ")
    except (EOFError, KeyboardInterrupt):
        log.error("Live confirmation aborted: no answer was read from stdin.")
        return False
    return answer.strip() == LIVE_CONFIRMATION_PHRASE


# ---------------------------------------------------------------------------
# Small coercion helpers (never invent a value)
# ---------------------------------------------------------------------------
def _coerce_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _numeric_token_id(value: Any) -> Optional[str]:
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


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first_string(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_bool(*values: Any) -> Optional[bool]:
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
) -> Optional[OpportunisticSwitchingEngine]:
    """Build the rewards re-scan engine for an auto-selected market.

    A pinned ``--condition-id`` is an operator decision and never migrates.
    ``switch_hurdle_pct`` is zero: the module's measured switching cost (gas +
    merge gas + crossing friction, amortized over the holding horizon) is the
    entire hurdle, so the runner contributes no economic constant of its own.
    """
    if not market.get("auto_selected"):
        return None
    config = SwitchingConfig(
        scan_interval_seconds=MARKET_STATUS_INTERVAL,
        switch_hurdle_pct=0.0,
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
        MarketSwitchBlocked: when outcome-token inventory remains after the
            merge attempt. ``RealMoneyTradingEngine`` tracks one market's
            inventory at a time, so migrating with old shares would mark them
            against the new market.
    """
    unlocked_cash = 0.0
    try:
        merged = engine.trigger_ctf_merge()
    except Exception as exc:  # a failed merge must not leave quoting halted forever
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
    engine.microstructure_yes.reset()
    engine.microstructure_no.reset()
    engine.latest_switching_cost = None
    engine.latest_hurdle_decision = None
    engine.governor.market_entry_times.pop(old_cid, None)
    engine.governor.market_entry_times[engine.condition_id] = time.time()
    engine.governor.current_allocations.pop(old_cid, None)
    engine._markout_tracker = MarkoutTracker(
        conn=engine._conn,
        mid_getter=engine._fresh_mid,
        lock=engine._lock,
    )
    return unlocked_cash



# ---------------------------------------------------------------------------
# Market resolution
# ---------------------------------------------------------------------------
def _resolve_token(requested: Optional[str], discovered: Optional[str], label: str, cid: str) -> str:
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
    requested_cid: Optional[str] = None,
    requested_yes: Optional[str] = None,
    requested_no: Optional[str] = None,
    bankroll: float = 150.0,
    min_size: Optional[float] = None,
    max_spread: Optional[float] = None,
    daily_reward: Optional[float] = None,
) -> dict[str, Any]:
    """Resolve the market, real token ids and execution metadata.

    Raises:
        SystemExit: when a market or token cannot be resolved. Nothing is
            fabricated to keep the loop running.
    """
    log.info("Resolving market details from Polymarket CLOB API...")

    markets = feed.get_rewards_markets_sync() or []
    target_market: Optional[dict[str, Any]] = None
    competing_score_override: Optional[float] = None
    daily_reward_override: Optional[float] = None

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
            optimal_markets = find_optimal_lp_markets(bankroll_usd=bankroll, limit_output=5)
        except Exception as exc:  # network failure is not fatal: fall back to rate sort
            log.warning(f"Optimal market scan failed ({exc}); falling back to the rewards rate sort.")
            optimal_markets = []
        if optimal_markets:
            best = optimal_markets[0]
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
            for market in sorted(markets, key=_reward_rate, reverse=True):
                if market.get("closed") is True or market.get("archived") is True:
                    continue
                tokens = market.get("tokens") or []
                if len(tokens) >= 2:
                    target_market = market
                    break
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
        except Exception as exc:
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
    discovered_yes: Optional[str] = None
    discovered_no: Optional[str] = None
    payload_mid: Optional[float] = None
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
        except Exception as exc:
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
    fee_rate_bps = int(round(fee_value)) if fee_value is not None else 0

    end_date_iso = _first_string(raw_market.get("end_date_iso"), detail.get("end_date_iso"))

    # Validate the payload we will actually trade, with resolved token ids.
    validated_payload: dict[str, Any] = dict(raw_market)
    validated_payload["tokens"] = [
        {"outcome": "YES", "token_id": token_yes, "price": mid_price},
        {"outcome": "NO", "token_id": token_no},
    ]
    validated_payload["end_date_iso"] = end_date_iso
    ok, reason = validate_market_for_live(validated_payload)
    if not ok:
        raise SystemExit(f"Market {cid} is not safe to quote live: {reason}")

    end_dt = _parse_iso_datetime(end_date_iso)
    if end_dt is None:
        raise SystemExit(f"Market {cid} end_date_iso is unparseable: {end_date_iso!r}")
    now_utc = datetime.now(timezone.utc)
    days_to_resolution = max((end_dt - now_utc).total_seconds() / 86400.0, 1.0 / 24.0)

    resolved_min_size = _coerce_float(min_size)
    if resolved_min_size is None:
        resolved_min_size = _coerce_float(validated_payload.get("rewards_min_size"))
    if resolved_min_size is None:
        raise SystemExit(f"Market {cid} has no rewards_min_size; supply --min-size.")
    if resolved_min_size <= 0.0:
        raise SystemExit(f"Market {cid} resolved a non-positive min size: {resolved_min_size}.")
    resolved_max_spread = _coerce_float(max_spread)
    if resolved_max_spread is None:
        resolved_max_spread = _coerce_float(validated_payload.get("rewards_max_spread"))
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
        "end_date_iso": end_date_iso,
        "end_timestamp": end_dt.timestamp(),
        "days_to_resolution": days_to_resolution,
        "category": _market_category(question),
        "competing_score_override": competing_score_override,
        "raw_market": validated_payload,
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def _fmt_value(value: Any, spec: str = ".2f", na: str = "n/a") -> str:
    """Format a numeric value, or return ``na`` for anything non-numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return na
    try:
        return format(value, spec)
    except (TypeError, ValueError):
        return na


def _num(telemetry: dict[str, Any], key: str, spec: str = ".2f", na: str = "n/a") -> str:
    return _fmt_value(telemetry.get(key), spec, na)


def _text(telemetry: dict[str, Any], key: str, na: str = "n/a") -> str:
    value = telemetry.get(key)
    if value is None or value == "":
        return na
    return str(value)


def build_dashboard_lines(
    telemetry: dict[str, Any],
    market_info: dict[str, Any],
    elapsed: float,
    now_str: str,
) -> List[str]:
    """Render telemetry into terminal lines. Missing keys render as ``n/a``."""
    t = telemetry or {}
    question = str(market_info.get("question", "n/a"))
    token_yes = str(market_info.get("token_yes", "n/a"))
    token_no = str(market_info.get("token_no", "n/a"))

    mode = _text(t, "mode")
    if mode == "REAL_MONEY":
        mode_str = f"{C_GREEN}[LIVE REAL-MONEY TRADING]{C_RESET}"
    elif mode == "DRY_RUN":
        mode_str = f"{C_YELLOW}[DRY-RUN / SAFETY INSPECTION]{C_RESET}"
    else:
        mode_str = f"{C_DIM}[MODE n/a]{C_RESET}"

    total_pnl = t.get("total_pnl")
    pnl_color = C_GREEN if isinstance(total_pnl, (int, float)) and total_pnl >= 0 else C_RED
    net_q = t.get("net_exposure")
    if isinstance(net_q, (int, float)):
        q_color = C_GREEN if abs(net_q) <= 15.0 else (C_YELLOW if abs(net_q) <= 50.0 else C_RED)
    else:
        q_color = C_DIM

    enabled = t.get("trading_enabled")
    if enabled is True:
        trading_str = f"{C_GREEN}ENABLED{C_RESET}"
    elif enabled is False:
        trading_str = f"{C_RED}HALTED{C_RESET}"
    else:
        trading_str = f"{C_DIM}n/a{C_RESET}"
    halt_reason = _text(t, "halt_reason", "-")

    book_stale = t.get("book_stale")
    if book_stale is True:
        book_str = f"{C_RED}STALE{C_RESET}"
    elif book_stale is False:
        book_str = f"{C_GREEN}FRESH{C_RESET}"
    else:
        book_str = f"{C_DIM}n/a{C_RESET}"
    feed_age = _num(market_info, "feed_age_seconds", ".1f")

    equity = t.get("equity")
    initial_equity = t.get("initial_equity")
    if isinstance(equity, (int, float)) and isinstance(initial_equity, (int, float)):
        equity_delta = f"{equity - initial_equity:+.2f}"
        equity_color = C_GREEN if equity >= initial_equity else C_RED
    else:
        equity_delta = "n/a"
        equity_color = C_DIM
    equity_value = _fmt_value(equity, ".2f")

    deskew = t.get("latest_deskew") or {}
    if not isinstance(deskew, dict):
        deskew = {}
    pathway = str(deskew.get("pathway", "HOLD_STABLE"))
    urgency = str(deskew.get("urgency_level", "LOW"))
    urgency_color = C_GREEN if urgency == "LOW" else (C_YELLOW if urgency == "MEDIUM" else C_RED)
    savings = _num(deskew, "synthetic_savings_cents", ".2f")

    epoch_index = t.get("epoch_index")
    modelled_rewards = t.get("modelled_rewards", t.get("total_rewards"))
    modelled_value = _fmt_value(modelled_rewards, ".4f")
    run_rate = t.get("projected_daily_usd")
    if isinstance(run_rate, (int, float)) and run_rate > 0.0:
        proj_daily_str = f"${float(run_rate):,.2f} (run-rate)"
    else:
        proj_daily_str = "n/a"
    accrued_daily = t.get("accrued_daily_usd")
    floor_str = (
        f"${float(accrued_daily):,.4f} / $1.00"
        if isinstance(accrued_daily, (int, float))
        else "n/a"
    )

    user_score = 0.0
    quotes = t.get("active_quotes")
    if not isinstance(quotes, list):
        quotes = []
    for quote in quotes:
        if isinstance(quote, dict):
            score = quote.get("score")
            if isinstance(score, (int, float)):
                user_score += score

    lines: List[str] = []
    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================{C_RESET}")
    lines.append(f"{C_BOLD}{C_WHITE}[MM] POLYMARKET REAL-MONEY TRADING ENGINE | CLOB & ON-CHAIN CTF {mode_str}{C_RESET}")
    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================{C_RESET}")
    lines.append(f"{C_BOLD}Market:{C_RESET} {question[:76]}")
    lines.append(
        f"{C_DIM}Condition:{C_RESET} {str(t.get('condition_id') or market_info.get('condition_id') or 'n/a')[:18]}... | "
        f"{C_DIM}Time:{C_RESET} {now_str} | "
        f"{C_DIM}Runtime:{C_RESET} {elapsed:.1f}s | "
        f"{C_DIM}Order version:{C_RESET} {_num(t, 'order_version', 'd')} | "
        f"{C_BOLD}Size:{C_RESET} {_num(market_info, 'quote_size', '.1f')}sh | "
        f"{C_BOLD}Offset:{C_RESET} {_num(market_info, 'spread_offset_cents', '.2f')}c"
    )
    lines.append(f"{C_DIM}Tokens:{C_RESET} YES: {token_yes[:18]}... | NO: {token_no[:18]}...")
    lines.append(f"{C_BOLD}Trading:{C_RESET} {trading_str} | {C_BOLD}Halt reason:{C_RESET} {halt_reason[:40]} | "
                 f"{C_BOLD}Market closed:{C_RESET} {_text(t, 'market_closed')} | "
                 f"{C_BOLD}Book:{C_RESET} {book_str} | "
                 f"{C_BOLD}Feed age:{C_RESET} {feed_age}s | "
                 f"{C_BOLD}Breakers:{C_RESET} {_text(t, 'breaker_status')}")
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[WALLET & BALANCES] CLOB COLLATERAL (NOT ON-CHAIN USDC){C_RESET}")
    lines.append(
        f"  {C_BOLD}Total Wallet:{C_RESET}   ${_num(t, 'total_wallet_balance', '8.2f')}    "
        f"{C_BOLD}Free Cash:{C_RESET}      ${_num(t, 'free_cash', '8.2f')}    "
        f"{C_BOLD}Locked Cash:{C_RESET}    ${_num(t, 'locked_cash', '8.2f')}"
    )
    lines.append(
        f"  {C_BOLD}Equity:{C_RESET}         {equity_color}${equity_value:>8s}{C_RESET} "
        f"({equity_delta} vs start ${_num(t, 'initial_equity', '.2f')})    "
        f"{C_BOLD}Sizing Cap:{C_RESET} ${_num(t, 'trading_bankroll', '.2f')} "
        f"({_num(t, 'budget_pct', '.1f')}% of wallet, cap not cash)"
    )
    lines.append(
        f"  {C_BOLD}POL Gas:{C_RESET}        {_num(t, 'pol_balance', '.4f')} POL    "
        f"{C_BOLD}Fees Paid:{C_RESET}      ${_num(t, 'fees_paid', '.4f')}    "
        f"{C_BOLD}Gas (CTF):{C_RESET}      ${_num(t, 'total_gas_fees', '.3f')}"
    )
    lines.append(
        f"  {C_BOLD}Shares YES:{C_RESET}     {_num(t, 'shares_yes', '8.2f')}    "
        f"{C_BOLD}Shares NO:{C_RESET}       {_num(t, 'shares_no', '8.2f')}    "
        f"{C_BOLD}Complete Sets:{C_RESET}  {_num(t, 'complete_sets', '8.2f')}    "
        f"{C_BOLD}Net Exposure:{C_RESET}   {q_color}{_num(t, 'net_exposure', '+8.2f')}{C_RESET}"
    )
    lines.append(
        f"  {C_BOLD}TOTAL PnL:{C_RESET}      {pnl_color}${_num(t, 'total_pnl', '+8.4f')}{C_RESET}    "
        f"{C_BOLD}Realized:{C_RESET}       ${_num(t, 'realized_pnl', '.4f')}    "
        f"{C_BOLD}Cash Reserve:{C_RESET}   ${_num(t, 'reserve_cash_usd', '.2f')}"
    )
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[DE-SKEW] 2-TIER DYNAMIC DE-SKEW OPTIMIZER{C_RESET}")
    lines.append(
        f"  {C_BOLD}Pathway:{C_RESET}        [{C_BOLD}{pathway:<16s}{C_RESET}]   "
        f"{C_BOLD}Urgency:{C_RESET} {urgency_color}{urgency:<10s}{C_RESET}   "
        f"{C_BOLD}Synthetic Savings:{C_RESET} {savings}c/sh"
    )
    if deskew.get("reason"):
        lines.append(f"  {C_DIM}Sentinel Action:{C_RESET} {str(deskew['reason'])[:74]}")
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[REWARDS] POLYMARKET QUADRATIC REWARDS (1,440 Daily Epochs){C_RESET}")
    lines.append(
        f"  {C_BOLD}UTC Epoch:{C_RESET}  {_num(t, 'epoch_index', 'd')} / 1440    "
        f"{C_BOLD}User Score:{C_RESET}     {user_score:>8.2f}    "
        f"{C_BOLD}Daily Pool:{C_RESET}     ${_num(market_info, 'daily_reward', '7.2f')}"
    )
    actual_rewards_value = t.get("actual_rewards_usd", t.get("actual_rewards"))
    actual_rebates_value = t.get("actual_rebates_usd", t.get("actual_rebates"))
    actual_daily_value = t.get("actual_daily_rewards_usd", t.get("actual_daily_rewards"))
    actual_payout_value = t.get("actual_total_payout_usd", t.get("actual_total_payout"))
    lines.append(
        f"  {C_YELLOW}Modelled{C_RESET} rewards: ${modelled_value:>8s} (not settled)    "
        f"{C_GREEN}Actual{C_RESET} rewards:   ${_fmt_value(actual_rewards_value, '.4f')}    "
        f"{C_BOLD}Rebates:{C_RESET}        ${_fmt_value(actual_rebates_value, '.4f')}"
    )
    lines.append(
        f"  {C_BOLD}Actual Daily:{C_RESET}   ${_fmt_value(actual_daily_value, '.4f')}    "
        f"{C_BOLD}Actual Payout:{C_RESET}  ${_fmt_value(actual_payout_value, '.4f')}    "
        f"{C_BOLD}Proj. Daily:{C_RESET}    {proj_daily_str}    "
        f"{C_BOLD}Floor accrued:{C_RESET} {floor_str}"
    )
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[ON-CHAIN CTF] POLYGON POS SETTLEMENT & COMPLETE-SET MERGES{C_RESET}")
    lines.append(
        f"  {C_BOLD}Total Merges:{C_RESET}   {_num(t, 'total_merges', 'd')}          "
        f"{C_BOLD}Total Splits:{C_RESET}   {_num(t, 'total_splits', 'd')}          "
        f"{C_BOLD}Total Fills:{C_RESET}    {_num(t, 'fills_count', 'd')}          "
        f"{C_BOLD}Traded Volume:{C_RESET}  ${_num(t, 'total_volume_usd', '.2f')}"
    )
    lines.append(f"{C_CYAN}----------------------------------------------------------------------------------------{C_RESET}")

    lines.append(f"{C_BOLD}[CLOB QUOTES] ACTIVE SIGNED ORDERS ON POLYMARKET CLOB{C_RESET}")
    lines.append(f"  {'Side':<6s} {'Outcome':<8s} {'Price':<8s} {'Size':<8s} {'Order ID':<20s} {'Score S(v,s)':<14s} {'Status'}")
    if quotes:
        for quote in quotes:
            if not isinstance(quote, dict):
                continue
            side = str(quote.get("side", "n/a"))
            side_color = C_GREEN if side == "BUY" else C_RED
            order_id = str(quote.get("order_id", "N/A"))[:18]
            lines.append(
                f"  {side_color}{side:<6s}{C_RESET} "
                f"{str(quote.get('token_side', 'n/a')):<8s} "
                f"${_num(quote, 'price', '.3f'):<7s} "
                f"{_num(quote, 'size', '.1f'):<8s} "
                f"{order_id:<20s} "
                f"{_num(quote, 'score', '.4f'):<14s} "
                f"{C_GREEN}{quote.get('status', 'LIVE')}{C_RESET}"
            )
    else:
        lines.append(f"  {C_DIM}No active resting orders on CLOB.{C_RESET}")
    lines.append(f"{C_BOLD}{C_CYAN}========================================================================================{C_RESET}")
    return lines


def render_dashboard(
    telemetry: dict[str, Any],
    market_info: dict[str, Any],
    start_time: float,
    clear_screen: bool = True,
) -> None:
    """Write the dashboard from a telemetry dict (never reads client secrets)."""
    elapsed = time.time() - start_time
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    lines = build_dashboard_lines(telemetry or {}, market_info, elapsed, now_str)
    if clear_screen:
        lines.insert(0, "\033[H\033[J")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def write_banner(
    *,
    dry_run: bool,
    order_version: Optional[int],
    signature_type: int,
    settlement_available: bool,
    neg_risk: bool,
    total_wallet: float,
    trading_bankroll: float,
    budget_pct: float,
    max_budget: Optional[float],
    condition_id: str,
) -> None:
    mode = (
        f"{C_YELLOW}DRY-RUN — --live was not supplied. NO real orders will be placed.{C_RESET}"
        if dry_run
        else f"{C_RED}LIVE — --live confirmed. Orders are REAL money.{C_RESET}"
    )
    lines = [
        f"{C_BOLD}{C_CYAN}================================================================================{C_RESET}",
        f"{C_BOLD}MODE:{C_RESET} {mode}",
        f"  Condition: {condition_id} | neg_risk: {neg_risk}",
        f"  CLOB order version: {order_version if order_version is not None else 'n/a'}",
        f"  Settlement path: "
        + (
            f"{C_GREEN}EOA-only CTF merge/redeem ENABLED{C_RESET}"
            if settlement_available
            else f"{C_YELLOW}CTF merge/redeem REFUSED (proxy wallet, no key, or dry-run){C_RESET}"
        ),
        f"  CLOB wallet: ${total_wallet:,.2f} | sizing cap: ${trading_bankroll:,.2f} "
        f"({budget_pct * 100.0:.1f}% of wallet"
        + (f", max ${max_budget:,.2f}" if max_budget is not None else "")
        + ") — the cap is not cash",
    ]
    if dry_run:
        lines.append(
            f"  {C_DIM}DRY-RUN: on-chain merges/redemptions are simulated; no CTF "
            "transaction is broadcast.{C_RESET}"
        )
    if neg_risk:
        lines.append(
            f"  {C_YELLOW}NEG-RISK MARKET:{C_RESET} CTF mergePositions does not settle "
            "neg-risk positions; live trading is blocked on this market."
        )
    if signature_type != 0:
        lines.append(
            f"  {C_YELLOW}ON-CHAIN SETTLEMENT UNAVAILABLE:{C_RESET} "
            f"signature_type={signature_type} (proxy/Safe). mergePositions/redeemPositions "
            "cannot be broadcast by this signer; inventory can only be exited by selling "
            "on the CLOB."
        )
    elif not dry_run and not settlement_available:
        lines.append(
            f"  {C_YELLOW}ON-CHAIN SETTLEMENT UNAVAILABLE:{C_RESET} no usable signing key; "
            "mergePositions/redeemPositions will refuse. Inventory can only be exited by "
            "selling on the CLOB."
        )
    lines.append(f"{C_BOLD}{C_CYAN}================================================================================{C_RESET}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def write_final_summary(telemetry: Optional[dict[str, Any]]) -> None:
    lines = [
        f"{C_BOLD}{C_CYAN}================================================================================{C_RESET}",
        f"{C_BOLD}SHUTDOWN SUMMARY{C_RESET}",
    ]
    if not isinstance(telemetry, dict):
        lines.append("  Final inventory state unavailable: telemetry did not return a dict.")
        lines.append("  Do not assume the account is flat.")
        lines.append(f"{C_CYAN}================================================================{C_RESET}")
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        return

    shares_yes = telemetry.get("shares_yes")
    shares_no = telemetry.get("shares_no")
    net_exposure = telemetry.get("net_exposure")
    lines.append(f"  Equity: ${_num(telemetry, 'equity', '.2f')} | free cash: ${_num(telemetry, 'free_cash', '.2f')} "
                 f"| locked cash: ${_num(telemetry, 'locked_cash', '.2f')} | cash: ${_num(telemetry, 'cash', '.2f')}")
    lines.append(f"  Positions: YES {_num(telemetry, 'shares_yes', '.2f')} / NO {_num(telemetry, 'shares_no', '.2f')} "
                 f"| complete sets {_num(telemetry, 'complete_sets', '.2f')} "
                 f"| net exposure {_num(telemetry, 'net_exposure', '+.2f')}")
    if (isinstance(shares_yes, (int, float)) and shares_yes != 0) or (
        isinstance(shares_no, (int, float)) and shares_no != 0
    ) or (isinstance(net_exposure, (int, float)) and net_exposure != 0):
        lines.append(
            f"  {C_RED}{C_BOLD}WARNING: OPEN INVENTORY REMAINS ON THE VENUE.{C_RESET} "
            "It was NOT liquidated. Exit manually by selling on the CLOB, or redeem "
            "after resolution if on-chain settlement is available."
        )
    elif not isinstance(shares_yes, (int, float)) or not isinstance(shares_no, (int, float)):
        lines.append(
            "  Position figures are n/a; the account is NOT confirmed flat."
        )
    lines.append(f"{C_CYAN}================================================================{C_RESET}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Blocking helpers routed through asyncio.to_thread
# ---------------------------------------------------------------------------
def _format_open_order_ids(orders: Any) -> str:
    """Name the venue's open orders for the operator, or say why they are unknown."""
    if orders is None:
        return "unavailable (open-order read failed)"
    if not isinstance(orders, list):
        return f"unavailable (unexpected payload {type(orders).__name__})"
    ids: List[str] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        order_id = order.get("id") or order.get("order_id") or order.get("orderId")
        ids.append(str(order_id) if order_id else "<no id in payload>")
    return ", ".join(ids) if ids else "none listed by the venue"


async def _thread_optional(label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run blocking work in a thread; return None (logged) on failure."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:
        log.warning(f"{label} failed: {exc}")
        return None


async def _close_engine_quietly(engine: Any) -> None:
    """Close the engine off the event loop, never masking the caller's abort reason."""
    try:
        await asyncio.to_thread(engine.close)
    except Exception as exc:
        log.error(f"Engine close failed: {exc}")


def _compute_competing_score(
    session: requests.Session, token_yes: str, max_spread_cents: float
) -> Optional[float]:
    try:
        score, _, _ = get_real_l2_score(session, token_yes, max_spread_cents)
    except Exception as exc:
        log.warning(f"L2 competing-score refresh failed: {exc}")
        return None
    return score if score is not None and score > 0 else None


# ---------------------------------------------------------------------------
# Async loop
# ---------------------------------------------------------------------------
async def run_live_runner_async(args: argparse.Namespace) -> int:
    """Async supervision loop for the real-money engine."""
    dry_run = not bool(args.live)
    if args.live and args.dry_run:
        log.warning("Both --live and --dry-run were supplied: forcing dry-run. No real orders.")
        dry_run = True

    creds = ClobAuthCredentials.from_env()
    clob_client = LiveClobClient(credentials=creds, dry_run=dry_run)
    ctf_client = PolygonCtfClient(
        rpc_url=args.rpc_url,
        private_key=creds.private_key,
        dry_run=dry_run,
        signature_type=creds.signature_type,
    )
    settlement_available = _settlement_available(ctf_client)
    log.info(
        "[SETTLEMENT] On-chain CTF merge/redeem path: "
        + (
            "ENABLED (EOA signer)."
            if settlement_available
            else "REFUSED (proxy wallet, no signing key, or dry-run)."
        )
    )
    if not dry_run and not settlement_available and not args.allow_no_settlement:
        log.critical(
            "Refusing live trading: CTF merge/redeem is EOA-only and this account cannot "
            f"settle (signature_type={creds.signature_type}). Complete sets would hit the "
            "risk caps with no merge path. Use an EOA signer, or re-run with "
            "--allow-no-settlement to quote anyway (inventory can then only be exited by "
            "selling on the CLOB)."
        )
        return 2
    if not dry_run and not settlement_available:
        log.critical(
            "[SETTLEMENT] Live quoting allowed by --allow-no-settlement, but on-chain "
            "merge/redeem is unavailable; inventory can only be exited by selling on the CLOB."
        )

    # -- Wallet: the CLOB collateral balance is the wallet. -----------------
    if dry_run:
        dry_initial = _coerce_float(args.initial_cash)
        if dry_initial is None or dry_initial <= 0.0:
            log.critical(
                f"--initial-cash must be a positive number for dry-run, got {args.initial_cash!r}."
            )
            return 1
        total_wallet = dry_initial
        log.info(
            f"[DRY-RUN WALLET] Using --initial-cash ${total_wallet:.2f} as the simulated "
            "wallet. No CLOB collateral is queried for sizing."
        )
    else:
        try:
            balance = await asyncio.to_thread(clob_client.get_balance)
        except Exception as exc:
            log.critical(f"Cannot read the CLOB collateral balance; refusing to trade live: {exc}")
            return 1
        available_cash = _coerce_float((balance or {}).get("available"))
        locked_raw = _coerce_float((balance or {}).get("locked"))
        if available_cash is None:
            log.critical(
                f"CLOB /balance returned no usable 'available' amount: {balance!r}. Refusing to size."
            )
            return 1
        locked_cash = locked_raw or 0.0
        total_wallet = available_cash
        log.info(
            f"[LIVE WALLET] CLOB available collateral ${available_cash:.2f} "
            f"(locked ${locked_cash:.2f})."
        )

    # On-chain reads are gas diagnostics only; they never size the position.
    wallet_addr = str(getattr(clob_client, "maker_address", "") or "")
    if wallet_addr and not dry_run:
        pol_balance, onchain_usdc = await asyncio.gather(
            _thread_optional("POL balance read", ctf_client.get_pol_balance, wallet_addr),
            _thread_optional("on-chain USDC read", ctf_client.get_usdc_balance, wallet_addr),
        )
        log.info(
            f"[GAS DIAGNOSTICS] POL {pol_balance} | on-chain USDC.e ${onchain_usdc} "
            "(diagnostics only; not used for sizing)."
        )

    # -- Sizing cap (never cash) --------------------------------------------
    raw_budget_pct = _coerce_float(args.budget_pct)
    if raw_budget_pct is None:
        log.critical(f"--budget-pct must be a finite number, got {args.budget_pct!r}. Aborting.")
        return 1
    if raw_budget_pct > 1.0:
        raw_budget_pct /= 100.0
    budget_pct = max(0.01, min(1.0, raw_budget_pct))
    max_budget = _coerce_float(args.max_budget) if args.max_budget is not None else None
    if args.max_budget is not None and max_budget is None:
        log.critical(f"--max-budget must be a finite number, got {args.max_budget!r}. Aborting.")
        return 1
    if max_budget is not None and max_budget <= 0.0:
        log.critical(f"--max-budget must be positive, got {max_budget:.2f}. Aborting.")
        return 1

    trading_bankroll = total_wallet * budget_pct
    if max_budget is not None:
        trading_bankroll = min(trading_bankroll, max_budget)
    trading_bankroll = round(max(0.0, trading_bankroll), 2)
    if trading_bankroll <= 0.0:
        log.critical(
            f"Sizing cap is ${trading_bankroll:.2f} from a ${total_wallet:.2f} wallet at "
            f"{budget_pct * 100.0:.1f}%. Nothing can be quoted; aborting."
        )
        return 1
    log.info(
        f"[SIZING CAP] ${trading_bankroll:.2f} = {budget_pct * 100.0:.1f}% of "
        f"${total_wallet:.2f} wallet"
        + (f", capped at --max-budget ${max_budget:.2f}" if max_budget is not None else "")
        + ". This is a sizing limit, not cash."
    )
    if dry_run:
        clob_client._mock_balance_usd = total_wallet

    feed = LivePolymarketFeed(fallback_poll_interval=2.0)
    market = await asyncio.to_thread(
        resolve_market_details,
        feed,
        clob_client,
        args.condition_id,
        args.token_yes,
        args.token_no,
        trading_bankroll,
        args.min_size,
        args.max_spread,
        args.daily_reward,
    )

    if market["neg_risk"] and not dry_run:
        log.critical(
            "Refusing live trading on a neg-risk market: CTF mergePositions does not "
            "settle neg-risk positions (PolygonCtfClient has no NegRiskAdapter path). "
            f"Condition {market['condition_id']} has neg_risk=True. Pick a non-neg-risk "
            "market or run --dry-run."
        )
        return 2

    daily_rew = args.daily_reward if args.daily_reward is not None else market["daily_reward"]
    max_spr = args.max_spread if args.max_spread is not None else market["max_spread"]
    min_sz = args.min_size if args.min_size is not None else market["min_size"]
    competing_override = args.competing_score is not None
    comp_score = args.competing_score if competing_override else market.get("competing_score_override")

    if args.quote_size is not None:
        quote_sz = float(args.quote_size)
    else:
        quote_sz = derive_quote_size(
            initial_cash=trading_bankroll,
            mid_price=market["mid_price"],
            min_size=min_sz,
        )
    if quote_sz <= 0.0:
        log.critical(f"Derived quote size is {quote_sz}. Aborting rather than quoting zero.")
        return 1

    if args.spread_offset is not None:
        spread_off = float(args.spread_offset)
    else:
        spread_off, _ = optimize_spread_offset(
            max_spread_cents=max_spr,
            market_spread_cents=market["market_spread_cents"],
            tick_size_cents=market.get("tick_size_cents", 1.0),
            daily_reward_pool=daily_rew,
            quote_size=quote_sz,
            category=market.get("category", "default"),
        )
    # Sizing telemetry for the dashboard (not engine state).
    market["quote_size"] = quote_sz
    market["spread_offset_cents"] = spread_off

    engine = RealMoneyTradingEngine(
        condition_id=market["condition_id"],
        asset_id_yes=market["token_yes"],
        asset_id_no=market["token_no"],
        clob_client=clob_client,
        ctf_client=ctf_client,
        # Sizing seed for RiskConfig / quote sizing only. reconcile_startup()
        # replaces cash and the equity baseline with the venue's figures in live.
        initial_cash=trading_bankroll,
        total_wallet_balance=total_wallet,
        budget_pct=budget_pct,
        max_budget=max_budget,
        quote_size=quote_sz,
        spread_offset_cents=spread_off,
        daily_reward_rate=daily_rew,
        rewards_max_spread_cents=max_spr,
        rewards_min_size=min_sz,
        auto_merge_threshold=args.auto_merge_threshold,
        db_path=args.db_path,
        ledger_path=args.ledger_path,
        institutional_mode=not args.legacy_mode,
        competing_score_override=comp_score,
        tick_size=market["tick_size"],
        neg_risk=market["neg_risk"],
        event_id=market.get("event_id"),
        fee_rate_bps=market["fee_rate_bps"],
        days_to_resolution=market["days_to_resolution"],
        book_stale_seconds=args.max_feed_staleness,
        dry_run=dry_run,
    )

    # -- Startup reconciliation (venue truth) -------------------------------
    try:
        await asyncio.to_thread(engine.reconcile_startup)
    except Exception as exc:
        if dry_run:
            log.warning(f"Startup reconciliation failed in dry-run ({exc}); continuing to inspect.")
        else:
            log.critical(f"Startup reconciliation failed; refusing to trade live: {exc}")
            await _close_engine_quietly(engine)
            return 1

    preflight = await asyncio.to_thread(engine.preflight_check)
    sys.stdout.write(f"\n{preflight.summary()}\n")
    sys.stdout.flush()
    if not dry_run and not preflight.ready_for_live:
        log.critical(
            "Preflight is NOT ready for live trading. Aborting without placing orders; "
            "the runner does not downgrade to dry-run."
        )
        await _close_engine_quietly(engine)
        return 1

    # -- Private session recording (non-blocking; the quote loop never waits) --
    recorder = None
    record_live_data = (
        args.record_live_data if args.record_live_data is not None else not dry_run
    )
    if record_live_data:
        try:
            from polymarketbot.trading.session_recorder import LiveSessionRecorder

            recorder = LiveSessionRecorder(
                db_path=args.db_path,
                clob=clob_client,
                market=market,
                dry_run=dry_run,
                ctf_client=ctf_client,
                feed_age_fn=lambda: feed.last_message_age_seconds,
                engine_params={
                    "quote_size": quote_sz,
                    "spread_offset_cents": spread_off,
                    "bankroll_usd": trading_bankroll,
                    "budget_pct": budget_pct,
                    "institutional_mode": not args.legacy_mode,
                },
            )
            await asyncio.to_thread(recorder.start)
            engine.event_hook = recorder.record_event
            log.info(f"Live session recording -> {recorder.out_dir}")
        except Exception as exc:
            log.error(f"Live session recorder failed to start; continuing without it: {exc}")
            recorder = None

    order_version: Optional[int] = None
    try:
        if dry_run:
            # Never trigger GET /version in a dry-run: previews stay offline.
            cached_version = getattr(clob_client, "_order_version", None)
            order_version = int(cached_version) if isinstance(cached_version, int) else None
        else:
            order_version = await asyncio.to_thread(lambda: clob_client.order_version)
    except Exception as exc:
        log.warning(f"Could not resolve the CLOB order version ({exc}); orders will resolve it on first use.")
    write_banner(
        dry_run=dry_run,
        order_version=order_version,
        signature_type=int(getattr(creds, "signature_type", 0) or 0),
        settlement_available=settlement_available,
        neg_risk=bool(market["neg_risk"]),
        total_wallet=total_wallet,
        trading_bankroll=trading_bankroll,
        budget_pct=budget_pct,
        max_budget=max_budget,
        condition_id=market["condition_id"],
    )

    # -- Feed plumbing -------------------------------------------------------
    def on_book(snapshot: Any) -> None:
        token = "YES" if snapshot.asset_id == engine.asset_id_yes else ("NO" if snapshot.asset_id == engine.asset_id_no else None)
        if token:
            engine.on_book_update(token, snapshot.bids, snapshot.asks)

    def on_trade(trade: Any) -> None:
        token = "YES" if trade.asset_id == engine.asset_id_yes else ("NO" if trade.asset_id == engine.asset_id_no else None)
        if token:
            engine.on_trade_update(token, trade.price, trade.size, trade.side, trade.timestamp)

    feed.add_book_listener(on_book)
    feed.add_trade_listener(on_trade)
    feed.subscribe(market["token_yes"])
    feed.subscribe(market["token_no"])

    # Seed the books with REST marks, but do NOT quote yet: the engine must not
    # place an order before the WebSocket session is up.
    engine.trading_enabled = False
    for label, token in (("YES", market["token_yes"]), ("NO", market["token_no"])):
        try:
            snapshot = await asyncio.to_thread(feed.get_book_sync, token)
        except Exception as exc:
            log.warning(f"Initial {label} book sync failed: {exc}")
            continue
        if snapshot is not None and (snapshot.bids or snapshot.asks):
            await asyncio.to_thread(engine.on_book_update, label, snapshot.bids, snapshot.asks)

    # Re-baseline equity after the books are seeded and before the first quote.
    finalize_baseline = getattr(engine, "finalize_initial_baseline", None)
    if callable(finalize_baseline):
        if not finalize_baseline():
            log.critical(
                "Refusing to quote: the initial equity baseline could not be finalized "
                "(inventory is held but a fresh book or cost basis is unavailable)."
            )
            await _close_engine_quietly(engine)
            return 2

    l2_session = requests.Session()
    start_time = time.time()
    last_orders_sync = 0.0
    last_balance_sync = 0.0
    last_reward_refresh = 0.0
    last_market_check = 0.0
    # The rewards re-scan reuses the market-lifecycle cadence (60 s) and waits
    # one full interval before its first pass so startup selection is not redone.
    last_market_switch_scan = start_time
    last_render = 0.0
    last_quote_eval = 0.0
    last_redeem_attempt = 0.0
    feed_halt_active = False
    market_halt_done = False
    quotes_cancelled = False
    redeem_done = False
    redeem_skip_warned = False
    awaiting_ws_ready = False
    running = True

    switcher: Optional[OpportunisticSwitchingEngine] = None
    # Candidates the live engine refused (neg-risk, invalid, unsizable) are
    # filtered from later scans so the next-best market can be considered.
    switch_excluded_conditions: set[str] = set()
    if market.get("auto_selected"):
        try:
            live_yield = float(engine.get_current_daily_yield())
        except Exception as exc:
            live_yield = 0.0
            log.warning(f"[MARKET SWITCH] Live yield measurement failed: {exc}")
        switcher = build_switching_engine(
            market,
            bankroll_usd=trading_bankroll,
            live_yield=live_yield,
        )
        if switcher is not None:
            log.info(
                f"[MARKET SWITCH] Rewards universe re-scan every "
                f"{MARKET_STATUS_INTERVAL:.0f}s on the market-lifecycle timer. "
                "The measured switching cost is the hurdle; no extra margin is applied."
            )

    def handle_signal(*_: Any) -> None:
        # Signal handlers must not touch the network or SQLite.
        nonlocal running
        log.info("Shutdown signal received; stopping the supervision loop.")
        running = False

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except Exception:
            pass

    final_telemetry: Optional[dict[str, Any]] = None
    exit_code = 0
    try:
        await feed.start()
        try:
            ws_ready = bool(feed.is_ws_connected)
        except Exception as exc:
            ws_ready = False
            log.warning(f"Could not read feed connection state: {exc}")
        awaiting_ws_ready = not ws_ready
        if ws_ready:
            engine.trading_enabled = True
            log.info("[STARTUP] WebSocket connected; engine quoting enabled.")
        else:
            log.warning(
                "[STARTUP] WebSocket is not connected yet; engine stays disabled "
                "until the session is up."
            )
        while running:
            now = time.time()

            if args.duration and (now - start_time) >= args.duration:
                log.info(f"Target duration of {args.duration:.1f}s reached.")
                break

            # -- Feed liveness -------------------------------------------------
            try:
                feed_stale = feed.is_stale(args.max_feed_staleness)
            except Exception as exc:
                feed_stale = False
                log.warning(f"Feed staleness check failed: {exc}")
            if not feed_stale:
                # A quiet market may send no book frame for minutes; while the
                # feed connection is alive its last snapshot is still current.
                refresh_liveness = getattr(engine, "refresh_book_liveness", None)
                if callable(refresh_liveness):
                    refresh_liveness(now)
                # Quote from the seeded book even when no new event arrives
                # (startup after the WS gate, resume after a feed halt, quiet book).
                if engine.trading_enabled and (now - last_quote_eval) >= 5.0:
                    try:
                        await asyncio.to_thread(engine.evaluate_quotes, now)
                    except Exception as exc:
                        log.error(f"Periodic quote evaluation failed: {exc}")
                    last_quote_eval = now
            if feed_stale and engine.trading_enabled and not feed_halt_active:
                # Deliberate: quotes cannot be repriced without the feed, but the
                # venue-side resting orders are still valid and are left live.
                engine.halt_trading("feed stale", cancel=False)
                feed_halt_active = True
                resting = await _thread_optional(
                    "Open-order read after stale-feed halt",
                    clob_client.get_open_orders,
                    market=market["condition_id"],
                )
                log.critical(
                    f"[FEED STALE] No frame for >{args.max_feed_staleness:.1f}s. Quoting halted. "
                    "DELIBERATE: cancel=False, so RESTING ORDERS REMAIN LIVE ON THE VENUE. "
                    f"Open order ids: {_format_open_order_ids(resting)}. "
                    "They stay exposed until the feed recovers or an operator cancels them."
                )
            elif not feed_stale and feed_halt_active:
                halt_reason = getattr(engine, "halt_reason", None)
                if halt_reason in (None, "", "feed stale"):
                    resume = getattr(engine, "resume_trading", None)
                    if callable(resume):
                        if resume():
                            log.info("[FEED RECOVERED] Quoting resumed.")
                        else:
                            log.warning(
                                "[FEED RECOVERED] The engine refused to resume; a risk "
                                "breaker or kill switch is still active."
                            )
                    else:
                        engine.trading_enabled = True
                        log.info("[FEED RECOVERED] Quoting resumed.")
                else:
                    log.warning(f"[FEED RECOVERED] Quoting stays halted for another reason: {halt_reason}")
                feed_halt_active = False

            # Startup gate: enable quoting only once the WebSocket is actually up.
            if awaiting_ws_ready:
                if market_halt_done:
                    awaiting_ws_ready = False
                else:
                    try:
                        ws_connected = bool(feed.is_ws_connected)
                    except Exception:
                        ws_connected = False
                    if ws_connected:
                        engine.trading_enabled = True
                        awaiting_ws_ready = False
                        log.info("[STARTUP] WebSocket connected; engine quoting enabled.")

            # -- Orders & fills (every 5 s) ------------------------------------
            if now - last_orders_sync >= ORDERS_SYNC_INTERVAL:
                try:
                    await asyncio.to_thread(engine.sync_live_orders_and_fills, timestamp=now)
                except Exception as exc:
                    log.error(f"Order/fill sync failed; keeping the last confirmed state: {exc}")
                last_orders_sync = now

            # -- Balances (every 15 s) -----------------------------------------
            if now - last_balance_sync >= BALANCE_SYNC_INTERVAL:
                try:
                    await asyncio.to_thread(engine.sync_live_balances)
                except Exception as exc:
                    log.error(f"Balance sync failed; keeping the last confirmed state: {exc}")
                last_balance_sync = now

            # -- Rewards + competing score ---------------------------------------
            if now - last_reward_refresh >= args.reward_refresh_interval:
                if not competing_override:
                    refreshed = await asyncio.to_thread(
                        _compute_competing_score, l2_session, market["token_yes"], max_spr
                    )
                    if refreshed is not None:
                        comp_score = refreshed
                try:
                    await asyncio.to_thread(
                        engine.process_reward_epoch,
                        timestamp=now,
                        competing_score=float(comp_score or 0.0),
                    )
                except Exception as exc:
                    log.error(f"Reward epoch processing failed: {exc}")
                last_reward_refresh = now

            # -- Rewards re-scan & opportunistic switch (every 60 s) -------------
            if switcher is not None and now - last_market_switch_scan >= MARKET_STATUS_INTERVAL:
                last_market_switch_scan = now
                if feed_stale or market_halt_done:
                    log.debug(
                        "[MARKET SWITCH] Re-scan skipped while quoting is halted "
                        f"(feed_stale={feed_stale}, market_halt_done={market_halt_done})."
                    )
                else:
                    try:
                        live_yield = float(engine.get_current_daily_yield())
                    except Exception as exc:
                        live_yield = None
                        log.warning(f"[MARKET SWITCH] Live yield measurement failed: {exc}")
                    try:
                        decision: Optional[SwitchDecision] = await asyncio.to_thread(
                            switcher.rescan_and_evaluate,
                            live_yield,
                            switch_excluded_conditions,
                        )
                    except Exception as exc:
                        decision = None
                        log.error(
                            "[MARKET SWITCH] Rewards re-scan failed; keeping "
                            f"{market['condition_id'][:12]}: {exc}"
                        )
                    if decision is not None:
                        log.info(
                            f"[MARKET SWITCH] {'SWITCH' if decision.should_switch else 'HOLD'} "
                            f"'{decision.from_question[:40]}' -> '{decision.to_question[:40]}' "
                            f"({decision.from_condition_id or 'none'} -> "
                            f"{decision.to_condition_id or 'none'}) | "
                            f"expected gain ${decision.expected_gain_per_day:+.2f}/day | "
                            f"switching cost ${decision.switching_cost_usd:.4f} "
                            f"(${decision.amortized_cost_per_day:.2f}/day amortized) | "
                            f"{decision.reason}"
                        )
                        candidate = decision.candidate
                        if (
                            decision.should_switch
                            and candidate is not None
                            and candidate.condition_id != market["condition_id"]
                        ):
                            try:
                                new_details: Optional[dict[str, Any]] = await asyncio.to_thread(
                                    resolve_market_details,
                                    feed,
                                    clob_client,
                                    candidate.condition_id,
                                    None,
                                    None,
                                    trading_bankroll,
                                    None,
                                    None,
                                    None,
                                )
                            except SystemExit as exc:
                                new_details = None
                                switch_excluded_conditions.add(candidate.condition_id)
                                log.warning(
                                    f"[MARKET SWITCH] Candidate {candidate.condition_id[:12]} "
                                    f"could not be resolved; keeping the current market: {exc}"
                                )
                            except Exception as exc:
                                new_details = None
                                log.error(
                                    f"[MARKET SWITCH] Candidate resolution failed; keeping "
                                    f"the current market: {exc}"
                                )
                            if new_details is not None and new_details["neg_risk"] and not dry_run:
                                log.warning(
                                    "[MARKET SWITCH] Candidate "
                                    f"{new_details['condition_id'][:12]} is neg-risk and the "
                                    "live engine cannot settle it; keeping the current market."
                                )
                                switch_excluded_conditions.add(candidate.condition_id)
                                new_details = None
                            if new_details is not None:
                                new_quote_size = (
                                    float(args.quote_size)
                                    if args.quote_size is not None
                                    else derive_quote_size(
                                        initial_cash=trading_bankroll,
                                        mid_price=new_details["mid_price"],
                                        min_size=new_details["min_size"],
                                    )
                                )
                                if new_quote_size <= 0.0:
                                    switch_excluded_conditions.add(candidate.condition_id)
                                    log.warning(
                                        "[MARKET SWITCH] Candidate quote size resolved to "
                                        f"{new_quote_size}; keeping the current market."
                                    )
                                else:
                                    if args.spread_offset is not None:
                                        new_spread_offset = float(args.spread_offset)
                                    else:
                                        new_spread_offset, _ = optimize_spread_offset(
                                            max_spread_cents=new_details["max_spread"],
                                            market_spread_cents=new_details["market_spread_cents"],
                                            tick_size_cents=new_details.get("tick_size_cents", 1.0),
                                            daily_reward_pool=new_details["daily_reward"],
                                            quote_size=new_quote_size,
                                            category=new_details.get("category", "default"),
                                        )
                                    old_cid = market["condition_id"]
                                    old_question = market["question"]
                                    old_yes = market["token_yes"]
                                    old_no = market["token_no"]
                                    was_trading = bool(engine.trading_enabled)
                                    engine.halt_trading("market switch", cancel=False)
                                    try:
                                        cancelled = bool(
                                            await asyncio.to_thread(engine.cancel_all_quotes)
                                        )
                                    except Exception as exc:
                                        cancelled = False
                                        log.error(
                                            f"[MARKET SWITCH] Quote cancellation failed: {exc}"
                                        )
                                    if not cancelled:
                                        log.warning(
                                            "[MARKET SWITCH] Quote cancellation is NOT confirmed "
                                            "by the venue; keeping the current market and "
                                            "deferring the migration."
                                        )
                                        if was_trading:
                                            engine.resume_trading()
                                        elif feed_stale:
                                            engine.halt_reason = "feed stale"
                                        else:
                                            engine.halt_reason = None
                                    else:
                                        try:
                                            unlocked_cash = await asyncio.to_thread(
                                                rebind_engine_to_market,
                                                engine,
                                                new_details,
                                                quote_size=new_quote_size,
                                                spread_offset_cents=new_spread_offset,
                                            )
                                        except MarketSwitchBlocked as exc:
                                            log.warning(
                                                "[MARKET SWITCH] Migration deferred; keeping "
                                                f"{old_cid[:12]}: {exc}."
                                            )
                                            if was_trading:
                                                engine.resume_trading()
                                            elif feed_stale:
                                                engine.halt_reason = "feed stale"
                                            else:
                                                engine.halt_reason = None
                                        except Exception as exc:
                                            log.critical(
                                                "[MARKET SWITCH] Rebinding the engine failed after "
                                                "quoting was halted; stopping instead of quoting an "
                                                f"inconsistent engine: {exc}"
                                            )
                                            running = False
                                        else:
                                            unsubscribe = getattr(feed, "unsubscribe", None)
                                            if callable(unsubscribe):
                                                unsubscribe([old_yes, old_no])
                                            feed.subscribe(
                                                [new_details["token_yes"], new_details["token_no"]]
                                            )
                                            for label, token in (
                                                ("YES", new_details["token_yes"]),
                                                ("NO", new_details["token_no"]),
                                            ):
                                                try:
                                                    snapshot = await asyncio.to_thread(
                                                        feed.get_book_sync, token
                                                    )
                                                except Exception as exc:
                                                    log.warning(
                                                        f"[MARKET SWITCH] {label} book sync failed: {exc}"
                                                    )
                                                    continue
                                                if snapshot is not None and (
                                                    snapshot.bids or snapshot.asks
                                                ):
                                                    await asyncio.to_thread(
                                                        engine.on_book_update,
                                                        label,
                                                        snapshot.bids,
                                                        snapshot.asks,
                                                    )
                                            refresh = getattr(engine, "refresh_book_liveness", None)
                                            if callable(refresh):
                                                refresh(time.time())

                                            market.update(new_details)
                                            market["auto_selected"] = True
                                            market["quote_size"] = new_quote_size
                                            market["spread_offset_cents"] = new_spread_offset
                                            daily_rew = new_details["daily_reward"]
                                            max_spr = new_details["max_spread"]
                                            min_sz = new_details["min_size"]
                                            quote_sz = new_quote_size
                                            spread_off = new_spread_offset
                                            competing_override = args.competing_score is not None
                                            comp_score = (
                                                args.competing_score
                                                if competing_override
                                                else new_details.get("competing_score_override")
                                            )
                                            if recorder is not None:
                                                recorder.market = dict(market)
                                            switcher.refresh_active_market(candidate)
                                            switcher.history.append(
                                                SwitchEvent(
                                                    timestamp=time.time(),
                                                    from_condition_id=old_cid,
                                                    from_question=old_question,
                                                    to_condition_id=new_details["condition_id"],
                                                    to_question=new_details["question"],
                                                    y_net_from=decision.y_net_from,
                                                    y_net_to=decision.y_net_to,
                                                    switching_cost=decision.switching_cost_usd,
                                                    hurdle_threshold=decision.hurdle_threshold,
                                                    reason=decision.reason,
                                                    unlocked_cash_usd=unlocked_cash,
                                                )
                                            )
                                            if was_trading:
                                                if not engine.resume_trading():
                                                    log.warning(
                                                        "[MARKET SWITCH] Quoting stays halted: a risk "
                                                        "breaker or kill switch is active."
                                                    )
                                            elif feed_stale:
                                                engine.halt_reason = "feed stale"
                                            else:
                                                engine.halt_reason = None
                                            log.info(
                                                f"[MARKET SWITCH] Quoting {market['question'][:40]} "
                                                f"({market['condition_id'][:12]}) with quote size "
                                                f"{new_quote_size:.2f} @ offset {new_spread_offset:.2f}c. "
                                                f"Unlocked ${unlocked_cash:.2f} USDC from {old_cid[:12]}."
                                            )

            # -- Market lifecycle (every 60 s) ----------------------------------
            if now - last_market_check >= MARKET_STATUS_INTERVAL:
                last_market_check = now
                status = await _thread_optional(
                    "Market status check", clob_client.get_market, market["condition_id"]
                )
                end_passed = now >= float(market["end_timestamp"])
                closed = bool((status or {}).get("closed"))
                accepting = (status or {}).get("accepting_orders")
                reason: Optional[str] = None
                if closed:
                    reason = "market closed"
                elif accepting is False:
                    reason = "market not accepting orders"
                elif end_passed:
                    reason = "market end date passed"
                if reason is not None:
                    if not market_halt_done:
                        # cancel=False keeps the blocking venue cancel off the event loop.
                        engine.halt_trading(reason, cancel=False)
                        engine.market_closed = True
                        market_halt_done = True
                        log.warning(f"[MARKET LIFECYCLE] {reason}. Quoting halted.")
                    elif engine.trading_enabled:
                        # Never let a breaker reset re-enable quoting on a dead market.
                        engine.halt_trading(reason, cancel=False)
                    if not quotes_cancelled:
                        quotes_cancelled = bool(await asyncio.to_thread(engine.cancel_all_quotes))
                        if quotes_cancelled:
                            log.info("[MARKET LIFECYCLE] Quotes cancelled; the venue confirmed empty.")
                        else:
                            log.critical(
                                "[MARKET LIFECYCLE] Quote cancellation is NOT confirmed by the venue."
                            )

                # Redemption: once a minute until it succeeds or the run ends.
                if (
                    market_halt_done
                    and args.redeem_on_close
                    and not redeem_done
                    and now - last_redeem_attempt >= REDEEM_RETRY_INTERVAL
                ):
                    last_redeem_attempt = now
                    if not settlement_available:
                        if not redeem_skip_warned:
                            redeem_skip_warned = True
                            log.warning(
                                "--redeem-on-close: on-chain redemption is unavailable for this "
                                f"account (signature_type={getattr(creds, 'signature_type', 0)}, "
                                "dry-run, or no signing key). Positions can only be exited by "
                                "selling on the CLOB."
                            )
                    else:
                        state = await _thread_optional("Telemetry read", engine.get_telemetry)
                        shares = 0.0
                        if isinstance(state, dict):
                            shares = float(_coerce_float(state.get("shares_yes")) or 0.0) + float(
                                _coerce_float(state.get("shares_no")) or 0.0
                            )
                        if shares <= 0.0:
                            log.info("[REDEEM] No outcome-token inventory remains; nothing to redeem.")
                            redeem_done = True
                        else:
                            result = await _thread_optional(
                                "CTF redeems",
                                ctf_client.redeem_positions,
                                market["condition_id"],
                                index_sets=[1, 2],
                            )
                            if result is not None:
                                if bool(getattr(result, "success", False)):
                                    log.info(
                                        f"[REDEEM] Positions redeemed on-chain: "
                                        f"tx={getattr(result, 'tx_hash', '') or 'n/a'}."
                                    )
                                    redeem_done = True
                                else:
                                    log.warning(
                                        f"[REDEEM] Redemption failed (retrying in "
                                        f"{REDEEM_RETRY_INTERVAL:.0f}s): "
                                        f"{getattr(result, 'error', None) or 'unknown error'}."
                                    )

            # -- Dashboard ------------------------------------------------------
            if now - last_render >= args.poll_interval:
                try:
                    market["feed_age_seconds"] = feed.last_message_age_seconds
                except Exception:
                    market["feed_age_seconds"] = None
                telemetry = await asyncio.to_thread(engine.get_telemetry)
                final_telemetry = telemetry
                render_dashboard(
                    telemetry,
                    market,
                    start_time,
                    clear_screen=not args.no_clear,
                )
                last_render = now

            await asyncio.sleep(LOOP_SLEEP_SECONDS)

    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutdown requested by operator or system interrupt.")
    finally:
        try:
            final_telemetry = await asyncio.to_thread(engine.get_telemetry)
        except Exception as exc:
            log.warning(f"Final telemetry read failed: {exc}")

        # Cancellation must be proven before the feed is stopped or the run returns.
        confirmed = False
        last_cancel_error: Optional[str] = None
        for attempt in range(1, CANCEL_RETRY_ATTEMPTS + 1):
            try:
                if await asyncio.to_thread(engine.cancel_all_quotes):
                    confirmed = True
                    break
                log.warning(
                    f"[SHUTDOWN] Quote cancellation not confirmed by the venue "
                    f"(attempt {attempt}/{CANCEL_RETRY_ATTEMPTS})."
                )
            except Exception as exc:
                last_cancel_error = str(exc)
                log.error(
                    f"[SHUTDOWN] Cancel attempt {attempt}/{CANCEL_RETRY_ATTEMPTS} failed: {exc}"
                )
            if attempt < CANCEL_RETRY_ATTEMPTS and CANCEL_RETRY_BACKOFF_SECONDS > 0.0:
                await asyncio.sleep(CANCEL_RETRY_BACKOFF_SECONDS)

        if confirmed:
            log.info("All resting quotes cancelled; the venue reports no open orders.")
        else:
            survivors = await _thread_optional(
                "Open-order read at shutdown",
                clob_client.get_open_orders,
                market=market["condition_id"],
            )
            log.critical(
                f"[SHUTDOWN] QUOTE CANCELLATION UNCONFIRMED after {CANCEL_RETRY_ATTEMPTS} "
                "attempts"
                + (f" (last error: {last_cancel_error})" if last_cancel_error else "")
                + f". Orders may still be live: {_format_open_order_ids(survivors)}. "
                f"Exiting with code {EXIT_ORDERS_UNCONFIRMED}; verify and cancel manually."
            )
            exit_code = EXIT_ORDERS_UNCONFIRMED

        try:
            await asyncio.to_thread(engine.flush_ledger)
        except Exception as exc:
            log.error(f"Ledger flush failed: {exc}")
        if recorder is not None:
            try:
                await asyncio.to_thread(recorder.stop, "shutdown")
            except Exception as exc:
                log.error(f"Live session recorder stop failed: {exc}")
        # Cancellation is already proven above; stop the feed before closing
        # the engine so a late book callback cannot touch a closed ledger.
        try:
            await feed.stop()
        except Exception as exc:
            log.error(f"Feed stop failed: {exc}")
        await _close_engine_quietly(engine)
        try:
            l2_session.close()
        except Exception:
            pass
        write_final_summary(final_telemetry)
        if exit_code != 0:
            log.critical(
                "[SHUTDOWN] Inventory/orders are NOT confirmed safe; exit code "
                f"{exit_code} signals that orders may still be live."
            )
        log.info("Real-Money Trading Engine shut down.")

    return exit_code


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.yes and not args.live:
        log.warning("--yes has no effect without --live; running dry-run.")
    live_requested = bool(args.live) and not bool(args.dry_run)
    if not require_live_confirmation(live_requested, args.yes, sys.stdin.isatty()):
        log.critical("Live trading aborted: live confirmation was not given.")
        return 2
    return asyncio.run(run_live_runner_async(args))


if __name__ == "__main__":
    sys.exit(main())
