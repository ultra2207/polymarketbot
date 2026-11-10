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

import requests

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
GAS_ORACLE = "https://gasstation.polygon.technology/v2"
POL_PRICE_APIS = (
    "https://api.binance.com/api/v3/ticker/price?symbol=POLUSDT",
    "https://api.binance.com/api/v3/ticker/price?symbol=MATICUSDT",
)


def _live_gas_price_gwei(http: requests.Session) -> Optional[float]:
    """Polygon gas price in Gwei from a live oracle, else from a live RPC head."""
    try:
        resp = http.get(GAS_ORACLE, timeout=8.0)
        resp.raise_for_status()
        fast = resp.json()["fast"]
        for key in ("maxFee", "maxPriorityFee"):
            value = fast.get(key)
            if value is not None and float(value) > 0.0:
                return float(value)
    except Exception as exc:  # noqa: BLE001 - the RPC fallback is next
        log.debug(f"gas oracle unavailable ({exc}); falling back to RPC gas price")
    try:
        from polymarketbot.chain import ChainClient

        raw = ChainClient()._rpc("eth_gasPrice", [])
        wei = int(raw, 16) if isinstance(raw, str) else int(raw)
        if wei > 0:
            return wei / 1e9
    except Exception as exc:  # noqa: BLE001 - no measurement means no gas estimate
        log.debug(f"RPC gas price unavailable ({exc})")
    return None


def _live_pol_usd(http: requests.Session) -> Optional[float]:
    """POL/USD from a live ticker; a guessed price would silently price gas wrong."""
    for url in POL_PRICE_APIS:
        try:
            resp = http.get(url, timeout=8.0)
            resp.raise_for_status()
            price = float(resp.json()["price"])
            if price > 0.0:
                return price
        except Exception as exc:  # noqa: BLE001 - try the next symbol
            log.debug(f"POL price unavailable at {url} ({exc})")
    return None


def measure_gas_usd(http: requests.Session) -> Optional[float]:
    """Measure the dollar cost of one on-chain action, or return None.

    None is a refusal, never a zero: an unpriced action must not be treated as
    free, and the pillars that need it stay off until both inputs are live.
    """
    gwei = _live_gas_price_gwei(http)
    pol_usd = _live_pol_usd(http)
    if gwei is None or pol_usd is None:
        return None
    usd = GAS_UNITS_PER_ONCHAIN_ACTION * gwei * 1e-9 * pol_usd
    log.info(
        f"Measured on-chain gas cost: {GAS_UNITS_PER_ONCHAIN_ACTION:,} units x "
        f"{gwei:.2f} Gwei x ${pol_usd:.5f}/POL = ${usd:.5f}"
    )
    return round(usd, 6)


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
        gas_usd = measure_gas_usd(http)
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
        gas_probe=lambda: measure_gas_usd(http),
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
