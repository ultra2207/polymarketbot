#!/usr/bin/env python3
"""Live paper trader entry point: the dual-sided reward pair.

The trader rests one bid on each outcome token of a binary market at the same
share count, prices the pair's venue ``Q_min`` once, and replays the measured
tape into fills on both legs. The engine charges the measured venue round trip
for every state change, writes every trade to ``data/paper_engine.db``, and
logs a dashboard line per interval. This file is deliberately thin: decisions
live in ``polymarketbot.paper``.
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
    parser = argparse.ArgumentParser(description="Live paper trader (reward pair)")
    parser.add_argument("--capital", type=float, default=3000.0,
                        help="Total virtual bankroll split evenly across the pillars")
    parser.add_argument("--duration", type=float, default=None,
                        help="Stop after N seconds (default: run until interrupted)")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="Tick and snapshot period in seconds")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite journal path")
    parser.add_argument("--fresh", action="store_true",
                        help="Ignore the last durable snapshot and start from --capital")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        log.info(f"Signal {signum}: stopping the paper trader")
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
