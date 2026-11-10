#!/usr/bin/env python3
"""Live paper soak entry point: RewardHarvester, SettlementHarvester.

The engine charges the measured venue round trip for every state change, writes
every trade to ``data/paper_engine.db``, and logs a dashboard line per interval.
This file is deliberately thin: decisions live in ``polymarketbot.paper``.
"""

from __future__ import annotations

import argparse
import signal
import sys
from typing import Optional

from polymarketbot.gamma import GammaClient
from polymarketbot.logs import get_logger
from polymarketbot.paper import BookFeed, PaperEngine, RttProbe, session
from polymarketbot.paper.engine import STRATEGIES

log = get_logger(__name__)

DEFAULT_DB = "data/paper_engine.db"
CLOB = "https://clob.polymarket.com"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Two-pillar live paper soak (reward + settlement)")
    parser.add_argument("--capital", type=float, default=3000.0,
                        help="Total virtual bankroll split evenly across the two pillars")
    parser.add_argument("--duration", type=float, default=None,
                        help="Stop after N seconds (default: run until interrupted)")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="Tick and snapshot period in seconds")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite journal path")
    parser.add_argument("--fresh", action="store_true",
                        help="Ignore the last durable snapshot and start from --capital")
    parser.add_argument("--gas-usd", type=float, default=None,
                        help="Override the measured on-chain cost per merge/redeem in USD. "
                             "Unset means measure it live; if the live measurement fails, "
                             "on-chain settlement stays disabled.")
    return parser


# mergePositions/redeemPositions gas, a documented protocol constant (not a price).
GAS_UNITS_PER_ONCHAIN_ACTION = 78_000


def measure_gas_usd(chain_client=None) -> Optional[float]:
    """Dollar cost of one on-chain action, from the shared measured path.

    This used to carry its own oracle lookup, which read ``fast.maxFee`` - a
    cap recommendation with ~60 % headroom - and fell back to
    ``fast.maxPriorityFee``, the *tip alone*. Both are wrong as a price: the
    first overstated by 1.6x (381.34 Gwei against a measured 236.58 on
    2026-11-10), the second would have dropped the base fee and understated by
    ~9x. The paper entry and the live entry now read the same measurement.

    None is a refusal, never a zero: an unpriced action must not be treated as
    free, and the pillars that need it stay off until it is measurable.
    """
    from polymarketbot.strategy.onchain.ctf.gas import estimate_merge_cost_usd

    usd = estimate_merge_cost_usd(
        gas_units=GAS_UNITS_PER_ONCHAIN_ACTION, chain_client=chain_client
    )
    if usd is None:
        log.warning(
            "on-chain gas could not be measured; on-chain settlement stays disabled"
        )
        return None
    log.info(
        f"Measured on-chain gas cost: {GAS_UNITS_PER_ONCHAIN_ACTION:,} units -> ${usd:.6f}"
    )
    return usd


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

    gas_usd = args.gas_usd
    if gas_usd is None:
        gas_usd = measure_gas_usd()
    if gas_usd is None:
        log.warning(
            "Gas cost unmeasured (live gas price and POL/USD both required): "
            "on-chain settlement is disabled rather than priced "
            "with an invented zero."
        )

    engine = PaperEngine(
        cash,
        http=http,
        gamma=GammaClient(session=http),
        feed=BookFeed(http),
        rtt=rtt,
        db_path=args.db,
        interval=args.interval,
        duration=args.duration,
        gas_usd=gas_usd,
        gas_probe=measure_gas_usd,
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
