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

The pieces live next to this module: ``runner_cli`` parses the flags and holds
the live gate, ``runner_market`` resolves/switches the market, ``runner_ui``
renders the terminal, and the ``supervisor`` package runs the phases of one
supervision run (startup, sizing, feed, switching, lifecycle, loop/shutdown).
This module is the entry point and the façade: it parses the flags, holds the
live gate, binds the collaborators the phases may be overridden on, and calls
the phases in the order the single-coroutine runner always used.

Usage::

    uv run python -m polymarketbot.live.runner --live --yes --budget-pct 0.20
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, List, Optional

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.scanner import find_optimal_lp_markets, get_real_l2_score
from polymarketbot.strategy.reward.switching import (
    OpportunisticSwitchingEngine,
    SwitchDecision,
    SwitchEvent,
)
from polymarketbot.strategy.sizing import derive_quote_size, optimize_spread_offset
from polymarketbot.strategy.venue.clob import ClobAuthCredentials, LiveClobClient
from polymarketbot.strategy.onchain.ctf import PolygonCtfClient
from polymarketbot.live.engine import RealMoneyTradingEngine
from polymarketbot.live.settlement import (
    LiveSettlementDesk,
    held_exposures,
    load_market,
)
from polymarketbot.strategy.latency import ActionBook, RttProbe
from polymarketbot.strategy.venue.feed import LivePolymarketFeed
from polymarketbot.strategy.venue.reads import CLOB

from polymarketbot.live.runner_cli import parse_args, require_live_confirmation
from polymarketbot.live.runner_market import (
    MARKET_STATUS_INTERVAL,
    MarketSwitchBlocked,
    _coerce_float,
    _settlement_available,
    build_switching_engine,
    rebind_engine_to_market,
    resolve_market_details as _resolve_market_details,
)
from polymarketbot.live.runner_ui import render_dashboard, write_banner, write_final_summary
from polymarketbot.live.supervisor import feed as _feed_phases
from polymarketbot.live.supervisor import loop as _loop
from polymarketbot.live.supervisor import sizing as _sizing
from polymarketbot.live.supervisor import startup as _startup
from polymarketbot.live.supervisor import switch as _switch
from polymarketbot.live.supervisor.context import Deps, RunContext
from polymarketbot.live.supervisor.desk import build_settlement_desk, settlement_tick
from polymarketbot.live.supervisor.helpers import _close_engine_quietly, _thread_optional
from polymarketbot.live.supervisor.lifecycle import REDEEM_RETRY_INTERVAL
from polymarketbot.live.supervisor.loop import (
    BALANCE_SYNC_INTERVAL,
    EXIT_ORDERS_UNCONFIRMED,
    LOOP_SLEEP_SECONDS,
    ORDERS_SYNC_INTERVAL,
)
from polymarketbot.live.supervisor.markets import (
    _compute_competing_score,
    _format_open_order_ids,
)

log = get_logger("real_money_trader")

# The phases moved; these names did not. Operator harnesses and the gating tests
# import them from this module (and override the collaborators right here), so
# the façade keeps exactly the surface the single-coroutine runner had.
__all__ = [
    "ActionBook",
    "BALANCE_SYNC_INTERVAL",
    "CANCEL_RETRY_ATTEMPTS",
    "CANCEL_RETRY_BACKOFF_SECONDS",
    "CLOB",
    "ClobAuthCredentials",
    "EXIT_ORDERS_UNCONFIRMED",
    "LiveClobClient",
    "LivePolymarketFeed",
    "LiveSettlementDesk",
    "LOOP_SLEEP_SECONDS",
    "MARKET_STATUS_INTERVAL",
    "MIN_APR_SAMPLES",
    "MarketSwitchBlocked",
    "ORDERS_SYNC_INTERVAL",
    "OpportunisticSwitchingEngine",
    "PolygonCtfClient",
    "REDEEM_RETRY_INTERVAL",
    "RealMoneyTradingEngine",
    "RttProbe",
    "SwitchDecision",
    "SwitchEvent",
    "_close_engine_quietly",
    "_coerce_float",
    "_compute_competing_score",
    "_format_open_order_ids",
    "_settlement_available",
    "_thread_optional",
    "build_settlement_desk",
    "build_switching_engine",
    "derive_quote_size",
    "find_optimal_lp_markets",
    "get_real_l2_score",
    "held_exposures",
    "load_market",
    "log",
    "main",
    "optimize_spread_offset",
    "parse_args",
    "rebind_engine_to_market",
    "render_dashboard",
    "require_live_confirmation",
    "resolve_market_details",
    "run_live_runner_async",
    "settlement_tick",
    "write_banner",
    "write_final_summary",
]

# Shutdown cancellation must be proven; otherwise the operator gets exit code 3.
# Read into the phase dependencies at run entry, so an override here reaches the
# shutdown loop.
CANCEL_RETRY_ATTEMPTS = 3
CANCEL_RETRY_BACKOFF_SECONDS = 1.5
# APR is only shown once the reward sampler has at least this many epochs.
MIN_APR_SAMPLES = 60


# ---------------------------------------------------------------------------
# Market resolution
# ---------------------------------------------------------------------------
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

    Delegates to :mod:`polymarketbot.live.runner_market`. The rewards-universe
    scan is passed explicitly so overriding ``find_optimal_lp_markets`` on this
    module still redirects the scan.
    """
    return _resolve_market_details(
        feed,
        clob,
        requested_cid,
        requested_yes,
        requested_no,
        bankroll,
        min_size,
        max_spread,
        daily_reward,
        find_markets=find_optimal_lp_markets,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _phase_deps() -> Deps:
    """Bind the collaborators this module may be overridden on, at run entry.

    The phases call through this record instead of importing the classes
    directly, so ``runner.LiveClobClient``/``runner.RealMoneyTradingEngine``/
    ``runner.resolve_market_details`` overrides keep redirecting them.
    """
    return Deps(
        credentials_cls=ClobAuthCredentials,
        clob_cls=LiveClobClient,
        ctf_cls=PolygonCtfClient,
        feed_cls=LivePolymarketFeed,
        engine_cls=RealMoneyTradingEngine,
        resolve_market_details=resolve_market_details,
        derive_quote_size=derive_quote_size,
        optimize_spread_offset=optimize_spread_offset,
        cancel_attempts=CANCEL_RETRY_ATTEMPTS,
        cancel_backoff=CANCEL_RETRY_BACKOFF_SECONDS,
    )


async def run_live_runner_async(args: argparse.Namespace) -> int:
    """Async supervision loop for the real-money engine."""
    dry_run = not bool(args.live)
    if args.live and args.dry_run:
        log.warning("Both --live and --dry-run were supplied: forcing dry-run. No real orders.")
        dry_run = True

    ctx = RunContext(args=args, deps=_phase_deps(), dry_run=dry_run)

    abort = await _startup.prepare_venue(ctx)
    if abort is not None:
        return abort
    abort = await _startup.resolve_wallet(ctx)
    if abort is not None:
        return abort
    abort = _sizing.apply_sizing_cap(ctx)
    if abort is not None:
        return abort
    _feed_phases.create_feed(ctx)
    abort = await _startup.resolve_market(ctx)
    if abort is not None:
        return abort
    abort = _sizing.apply_quote_sizing(ctx)
    if abort is not None:
        return abort
    _startup.build_engine(ctx)
    abort = await _startup.run_startup_check(ctx)
    if abort is not None:
        return abort
    await _startup.start_recorder(ctx)
    await _startup.resolve_order_version(ctx)
    _startup.write_run_banner(ctx)
    _feed_phases.wire_feed(ctx)
    await _feed_phases.seed_books(ctx)
    abort = await _feed_phases.finalize_equity_baseline(ctx)
    if abort is not None:
        return abort
    _loop.init_state(ctx)
    _switch.init_switcher(ctx)
    _feed_phases.install_signal_handlers(ctx)
    return await _loop.run_loop(ctx)


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
