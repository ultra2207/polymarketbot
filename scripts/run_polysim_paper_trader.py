#!/usr/bin/env python3
"""Run real-time cloud paper trading via PolySimulator (api.polysimulator.com).

Connects with POLYSIMULATOR_API_KEY from .env, trades against the $100 simulated
API wallet, and matches against live Polymarket CLOB book and trades in the cloud.

Usage:
    # 1. Check account balance and test market without placing orders:
    uv run python scripts/run_polysim_paper_trader.py --dry-run

    # 2. Run paper trading for 60 seconds with 50-share quotes:
    uv run python scripts/run_polysim_paper_trader.py --duration 60 --quote-size 50

    # 3. Emergency cancel all resting orders:
    uv run python scripts/run_polysim_paper_trader.py --cancel-all
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

from polymarketbot.logs import get_logger
from polymarketbot.paper_trading.polysim_client import PolySimulatorClient
from polymarketbot.paper_trading.polysim_trader import PolySimulatorTrader

log = get_logger("polysim_runner")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PolySimulator Cloud-Hosted Polymarket Autonomous Paper Trader"
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=100.0,
        help="Fraction of balance unlocked for trading in USD (default: $100.00).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Run duration in seconds (default: continuous until Ctrl+C).",
    )
    parser.add_argument(
        "--quote-size",
        type=float,
        default=None,
        help="Optional manual override for order size in shares (default: autonomous based on capital).",
    )
    parser.add_argument(
        "--spread-offset",
        type=float,
        default=0.75,
        help="Half-spread offset from mid in cents (default: 0.75c, inside touch).",
    )
    parser.add_argument(
        "--filter",
        type=str,
        default=None,
        help="Optional filter keyword to select target market (e.g. 'Netanyahu', 'Fed').",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=3.5,
        help="Poll interval in seconds (default: 3.5s).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check credentials and market books without submitting virtual orders.",
    )
    parser.add_argument(
        "--cancel-all",
        action="store_true",
        help="Immediately cancel all resting orders on PolySimulator and exit.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = PolySimulatorClient()

    if not client.api_key:
        log.error("POLYSIMULATOR_API_KEY is not set in environment or .env file.")
        return 1

    if args.cancel_all:
        log.info("Cancelling all resting orders on PolySimulator...")
        res = client.cancel_all()
        log.info(f"Cancel result: {res}")
        return 0

    log.info("Connecting to PolySimulator (api.polysimulator.com)...")
    try:
        bal = client.get_balance()
        log.info(
            f"PolySimulator Account Connected: Balance=${bal.balance:.2f} {bal.currency} | "
            f"Total Equity=${bal.total_value:.2f} | Unrealized PnL=${bal.unrealized_pnl:.2f}"
        )
    except Exception as exc:
        log.error(f"Failed to authenticate with PolySimulator: {exc}")
        return 1

    trader = PolySimulatorTrader(
        unlocked_capital_usd=args.capital,
        client=client,
        spread_offset_cents=args.spread_offset,
        poll_interval_seconds=args.poll_interval,
        quote_size=args.quote_size,
    )

    if not trader.select_market(filter_keyword=args.filter):
        log.error("Failed to select an active market.")
        return 1

    if args.dry_run:
        log.info("Dry-run mode enabled: verified account and market. Exiting without placing orders.")
        mid = client.get_midpoint(trader.yes_token_id)
        yes_bid, no_bid = trader.calculate_binary_quotes(mid or 0.50)
        log.info(f"Calculated Binary Quotes: BUY YES @ ${yes_bid:.3f} | BUY NO @ ${no_bid:.3f} (Mid: ${mid})")
        return 0

    log.info(
        f"Starting autonomous paper trading (Unlocked Capital=${args.capital:.2f}, "
        f"Auto Quote Size={trader.quote_size:.0f} shares, Duration={args.duration}s)..."
    )
    trader.run_loop(duration_seconds=args.duration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
