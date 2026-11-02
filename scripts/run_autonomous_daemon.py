#!/usr/bin/env python3
"""Run the Autonomous Liquidity Rewards Harvester Daemon in headless mode.

Takes only ONE setting: `--capital` (unlocked trading capital cap, default: $100.00).
The bot autonomously selects markets, sizes quote quantities, places quotes on PolySimulator,
and logs real-time quadratic rewards and reasoning to SQLite (data/polysim_paper_trading.db).

Usage:
    uv run python scripts/run_autonomous_daemon.py --capital 100
"""

from __future__ import annotations

import argparse
import sys
import time

from polymarketbot.logs import get_logger
from polymarketbot.paper_trading.autonomous_daemon import AutonomousHarvesterDaemon

log = get_logger("autonomous_runner")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Autonomous Polymarket Liquidity Rewards Harvester"
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
        help="Optional run duration in seconds (default: continuous until Ctrl+C).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log.info(f"Initializing Autonomous Harvester Daemon with ${args.capital:.2f} unlocked capital...")

    daemon = AutonomousHarvesterDaemon.get_instance(unlocked_capital_usd=args.capital)
    daemon.start()

    start_time = time.time()
    try:
        while daemon.is_running():
            time.sleep(1.0)
            if args.duration and (time.time() - start_time) >= args.duration:
                log.info(f"Target duration of {args.duration}s reached.")
                break
    except KeyboardInterrupt:
        log.info("Interrupted by user. Shutting down daemon...")
    finally:
        daemon.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
