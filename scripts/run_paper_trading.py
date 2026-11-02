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

log = get_logger("paper_trading")


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
    await client.run(duration_seconds=args.duration)


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
