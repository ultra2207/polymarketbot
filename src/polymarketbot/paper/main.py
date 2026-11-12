#!/usr/bin/env python3
"""Live paper soak entry point: RewardHarvester.

The engine charges the measured venue round trip for every state change, writes
every trade to ``data/paper_engine.db``, and logs a dashboard line per interval.
This file is deliberately thin: decisions live in ``polymarketbot.paper``.
"""

from __future__ import annotations

import argparse
import signal
import sys

from polymarketbot.gamma import GammaClient
from polymarketbot.logs import get_logger
from polymarketbot.paper import BookFeed, PaperEngine, RttProbe, session
from polymarketbot.paper.engine import STRATEGIES

log = get_logger(__name__)

DEFAULT_DB = "data/paper_engine.db"
CLOB = "https://clob.polymarket.com"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Live paper soak (reward)")
    parser.add_argument("--capital", type=float, default=3000.0,
                        help="Total virtual bankroll split evenly across the pillars")
    parser.add_argument("--duration", type=float, default=None,
                        help="Stop after N seconds (default: run until interrupted)")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="Tick and snapshot period in seconds")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite journal path")
    parser.add_argument("--fresh", action="store_true",
                        help="Ignore the last durable snapshot and start from --capital")
    parser.add_argument(
        "--legacy-one-sided",
        action="store_true",
        help=(
            "Retired research path: run the one-sided RewardHarvester paper pillar. "
            "The live engine quotes both legs; without this flag the paper entry "
            "point refuses to run a one-sided book."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.legacy_one_sided:
        log.critical(
            "The one-sided RewardHarvester paper pillar is retired: it quoted one "
            "leg per market, which is a directional bet, not a hedged pair. The "
            "live engine quotes both legs at one share count. For a two-sided "
            "quote/risk soak use: python -m polymarketbot.live.runner --dry-run "
            "--initial-cash 3000 --duration 1800 --db-path data/paper_engine.db. "
            "The legacy one-sided research path requires --legacy-one-sided."
        )
        return 2
    if args.capital <= 0.0:
        log.error("--capital must be positive")
        return 2
    per_strategy = args.capital / len(STRATEGIES)
    cash = {name: per_strategy for name in STRATEGIES}

    http = session()
    rtt = RttProbe(CLOB, http)
    measured = rtt.measure()
    if measured is None:
        log.error("No measured CLOB round trip; refusing to run zero-latency paper actions")
        return 1
    log.info(f"Measured CLOB round trip p50={measured * 1000.0:.1f}ms")

    engine = PaperEngine(
        cash,
        http=http,
        gamma=GammaClient(session=http),
        feed=BookFeed(http),
        rtt=rtt,
        db_path=args.db,
        interval=args.interval,
        duration=args.duration,
        resume=not args.fresh,
    )

    def _stop(signum: int, _frame: object) -> None:
        log.info(f"Signal {signum}: stopping the paper soak")
        engine.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, AttributeError):
            pass

    try:
        engine.run()
    finally:
        equity = engine.ledger.snapshot()
        log.info(f"Final equity: {equity}")
        engine.ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
