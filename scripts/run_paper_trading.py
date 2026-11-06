#!/usr/bin/env python3
"""Run real-time paper trading forward-test for Polymarket liquidity reward harvesting.

Usage:
    uv run python scripts/run_paper_trading.py --markets 3 --duration 60
    uv run python scripts/run_paper_trading.py --filter "Wealth Tax" --duration 120
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from polymarketbot.logs import get_logger
from polymarketbot.paper_trading import PaperTradingEngine, PolymarketLiveClient
from polymarketbot.paper_trading.kalshi_signal import (
    DEFAULT_MAPPING,
    KalshiBasisMonitor,
)
from polymarketbot.paper_trading.orchestrator import (
    OrchestratorConfig,
    QuoteOrchestrator,
)
from polymarketbot.trading.session_recorder import PaperSessionRecorder

log = get_logger("paper_trading")


def _build_orchestrator(args: argparse.Namespace) -> QuoteOrchestrator | None:
    if not args.orchestrator:
        return None
    config = OrchestratorConfig(
        enabled=True,
        policy=args.orchestrator_policy,
        signal=args.orchestrator_signal,
        gate_action=args.orchestrator_gate_action,
        min_adverse_queue_ratio=args.orchestrator_min_adverse_ratio,
        shrink_scale=args.orchestrator_shrink_scale,
        kalshi_basis_threshold=args.orchestrator_kalshi_threshold,
    )
    log.info(
        f"Orchestrator: policy={config.policy} signal={config.signal} "
        f"gate_action={config.gate_action} "
        f"min_adverse_ratio={config.min_adverse_queue_ratio:.2f} "
        f"shrink_scale={config.shrink_scale:.2f} "
        f"kalshi_basis_threshold={config.kalshi_basis_threshold:.2f} "
        f"(not measured; 0 = off)"
    )
    return QuoteOrchestrator(config)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket Real-Time Paper Trading & Reward Harvester"
    )
    parser.add_argument(
        "--markets",
        type=int,
        default=3,
        help="Number of head reward pools to select (default: 3)",
    )
    parser.add_argument(
        "--filter",
        type=str,
        default=None,
        help="Filter keyword to prioritize (e.g. 'Wealth Tax', 'Senate')",
    )
    parser.add_argument(
        "--spread-offset",
        type=float,
        default=0.75,
        help="Base spread offset in cents (default: 0.75c, range 0.5c - 1.0c)",
    )
    parser.add_argument(
        "--quote-size",
        type=float,
        default=100.0,
        help="Default virtual quote size in shares (default: 100.0)",
    )
    parser.add_argument(
        "--max-inventory",
        type=float,
        default=1000.0,
        help="Maximum position inventory per token before throttling (default: 1000.0)",
    )
    parser.add_argument(
        "--max-skew",
        type=float,
        default=0.5,
        help="Maximum quote spread skew in cents for inventory balancing (default: 0.5c)",
    )
    parser.add_argument(
        "--initial-cash",
        type=float,
        default=10000.0,
        help="Virtual paper trading starting cash balance in USD (default: $10,000.00)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Run duration in seconds (default: continuous until interrupted)",
    )
    parser.add_argument(
        "--snapshot-interval",
        type=float,
        default=60.0,
        help="Seconds between paper session snapshots (default: 60)",
    )
    parser.add_argument(
        "--epoch-interval",
        type=float,
        default=60.0,
        help="Reward sampling epoch interval in seconds (default: 60.0 for 1,440 daily epochs)",
    )
    parser.add_argument(
        "--ledger-path",
        type=str,
        default="data/processed/paper_trading/ledger.json",
        help="File path to persist the state ledger (default: data/processed/paper_trading/ledger.json)",
    )
    parser.add_argument(
        "--microstructure-gate",
        action="store_true",
        help="Enable the live OFI/micro-price pull-or-widen gate on resting quotes",
    )
    parser.add_argument(
        "--orchestrator",
        action="store_true",
        help="Enable the queue-aware quote orchestrator (supersedes --microstructure-gate)",
    )
    parser.add_argument(
        "--orchestrator-policy",
        choices=("reactionary", "side_selection", "queue_aware", "reward_first"),
        default="queue_aware",
        help="Orchestrator policy (default: queue_aware)",
    )
    parser.add_argument(
        "--orchestrator-signal",
        choices=("ofi", "binance", "kalshi"),
        default="ofi",
        help=(
            "Adverse-flow signal the orchestrator reacts to (default: ofi). "
            "'kalshi' requires the Kalshi basis monitor attached by this script."
        ),
    )
    parser.add_argument(
        "--orchestrator-gate-action",
        choices=("shrink", "pull"),
        default="shrink",
        help="Threatened-side action at the back of the queue (default: shrink)",
    )
    parser.add_argument(
        "--orchestrator-min-adverse-ratio",
        type=float,
        default=0.60,
        help="Queue ratio at/below which both sides are kept (default: 0.60)",
    )
    parser.add_argument(
        "--orchestrator-shrink-scale",
        type=float,
        default=0.25,
        help="Size multiplier applied when shrinking a threatened side (default: 0.25)",
    )
    parser.add_argument(
        "--orchestrator-kalshi-threshold",
        type=float,
        default=0.0,
        help=(
            "Cross-venue Kalshi basis threshold in logit points that must be "
            "exceeded before a side is shrunk/pulled. NOT YET MEASURED; the "
            "kalshi signal is off unless selected, and 0.0 is a no-op floor "
            "(default: 0.0)"
        ),
    )
    parser.add_argument(
        "--kalshi-mapping",
        type=str,
        default=str(DEFAULT_MAPPING),
        help=(
            "Polymarket -> Kalshi mapping parquet used by the basis monitor "
            f"(default: {DEFAULT_MAPPING})"
        ),
    )
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    log.info("Initializing Polymarket Paper Trading Forward-Test Engine...")
    log.info(
        f"Config: markets={args.markets}, filter={args.filter}, spread_offset={args.spread_offset}c, "
        f"quote_size={args.quote_size}, epoch_interval={args.epoch_interval}s"
    )

    engine = PaperTradingEngine(
        initial_cash_usd=args.initial_cash,
        base_spread_offset_cents=args.spread_offset,
        default_quote_size=args.quote_size,
        max_inventory_shares=args.max_inventory,
        max_skew_cents=args.max_skew,
        ledger_path=args.ledger_path,
        microstructure_gate=args.microstructure_gate,
        orchestrator=_build_orchestrator(args),
    )

    client = PolymarketLiveClient(
        engine=engine,
        epoch_interval_seconds=args.epoch_interval,
    )

    # 1. Fetch head reward pools dynamically
    markets = client.fetch_dynamic_reward_pools(
        limit=args.markets,
        filter_keyword=args.filter,
    )
    if not markets:
        log.error("No qualifying reward markets retrieved. Exiting.")
        return

    # The Kalshi basis monitor exists only when the orchestrator is asked for
    # that signal. Its polled conditions come from the engine's registrations,
    # so it is attached after the reward pools have been registered.
    kalshi_monitor: KalshiBasisMonitor | None = None
    if (
        engine.orchestrator is not None
        and engine.orchestrator.config.signal == "kalshi"
    ):
        kalshi_monitor = KalshiBasisMonitor(mapping_path=args.kalshi_mapping)
        engine.attach_kalshi_monitor(kalshi_monitor)
        kalshi_monitor.start()
        log.info(
            f"Kalshi basis monitor polling {len(kalshi_monitor.mapped_tickers())} "
            f"mapped tickers from {args.kalshi_mapping}"
        )

    # 2. Fetch initial order books
    client.fetch_initial_order_books()

    # 3. Perform initial epoch sample (t=0 baseline)
    log.info("Performing initial epoch reward sample (baseline)...")
    engine.run_epoch_sample()

    summary = engine.get_portfolio_summary()
    log.info(
        f"Initial State: Cash=${summary['current_cash_usd']:,.2f} | "
        f"Projected Daily Rewards=${summary['projected_daily_rewards_usd']:,.2f} | "
        f"Active Tokens={len(client.subscribed_token_ids)}"
    )

    # 4. Start live WebSocket streaming and epoch simulation
    recorder = PaperSessionRecorder(
        engine,
        engine_params={
            "markets": args.markets,
            "filter": args.filter,
            "quote_size": args.quote_size,
            "spread_offset_cents": args.spread_offset,
            "microstructure_gate": args.microstructure_gate,
            "orchestrator": args.orchestrator,
            "orchestrator_policy": args.orchestrator_policy,
            "orchestrator_signal": args.orchestrator_signal,
            "orchestrator_gate_action": args.orchestrator_gate_action,
            "orchestrator_min_adverse_ratio": args.orchestrator_min_adverse_ratio,
            "orchestrator_shrink_scale": args.orchestrator_shrink_scale,
            "orchestrator_kalshi_threshold": args.orchestrator_kalshi_threshold,
            "kalshi_mapping": args.kalshi_mapping,
        },
    )
    recorder.start()

    async def _snapshot_loop(stop_after: float | None) -> None:
        """Persist state as the session runs, so a kill loses one interval."""
        while True:
            await asyncio.sleep(min(60.0, args.snapshot_interval))
            try:
                recorder.snapshot()
            except Exception as exc:  # noqa: BLE001 - never kill the session
                log.warning(f"periodic snapshot failed: {exc}")

    snapshot_task = asyncio.create_task(_snapshot_loop(args.duration))
    try:
        await client.run(duration_seconds=args.duration)
    finally:
        if kalshi_monitor is not None:
            kalshi_monitor.close()
        snapshot_task.cancel()
        recorder.stop("run_complete")


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        log.info("Interrupted by user. Exiting cleanly.")
    except Exception as e:
        log.exception(f"Fatal error in paper trading run: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
