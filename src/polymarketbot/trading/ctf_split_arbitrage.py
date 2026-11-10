"""CTF complete-set fee and gas primitives.

The live parity path (``MarginalCtfAllocator``) prices every market directly
from the measured L2 ladder, so the standalone cross-split engine that used to
live here had no consumer and its hand-set caps (``max_mint_size_usd``,
``min_gross_spread_bps``, fixed ``min_net_edge_usd``) were removed with it.

What remains are the primitives the allocator and the venue adapters share:

* ``VIP_TIER_REBATES`` - published taker fee discount tiers.
* ``compute_leg_taker_fee`` - gross fee, VIP rebate credit, and net fee paid.
* ``estimate_polygon_eip1559_gas_usd`` - exact EIP-1559 gas math. It takes
  measured inputs only; there are no hand-set gas-price/POL-price defaults.
* ``load_measured_merge_gas_pol`` - per-condition measured merge gas from the
  repo's gas tape (``data/derived/market_scorecard.csv:merge_gas_pol``).
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Optional

from polymarketbot.fees import FeeSchedule, taker_fee
from polymarketbot.logs import get_logger

logger = get_logger("ctf_split_arbitrage")

# GAP: the repo has no per-transaction gas measurement for ``splitPosition``
# (data/derived/onchain/events-*.parquet carries no gas_used). 78,000 units is
# measured for mergePositions by the gas tape; this split estimate stays a
# documented fallback until a split receipt measurement exists.
TYPICAL_SPLIT_GAS_UNITS = 85_000

# Polymarket VIP Taker Rebate Tiers (live 2026-05-28)
VIP_TIER_REBATES: dict[str, float] = {
    "none": 0.00,
    "bronze": 0.03,    # 3% taker fee discount
    "silver": 0.08,    # 8% taker fee discount
    "gold": 0.18,      # 18% taker fee discount
    "platinum": 0.32,  # 32% taker fee discount
    "diamond": 0.44,   # 44% taker fee discount
    "obsidian": 0.50,  # 50% taker fee discount
}

# src/polymarketbot/trading/ctf_split_arbitrage.py -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCORECARD_PATH = _REPO_ROOT / "data" / "derived" / "market_scorecard.csv"


_MERGE_GAS_CACHE: dict[str, tuple[float, int, dict[str, float]]] = {}


def load_measured_merge_gas_pol(path: str = str(DEFAULT_SCORECARD_PATH)) -> dict[str, float]:
    """Per-condition measured merge gas in POL from the repo scorecard.

    Source: ``data/derived/market_scorecard.csv:merge_gas_pol`` - the gas tape's
    window-median base fee (``data/tape/polygon_gas``) priced through the venue's
    merge gas estimate. USD is this value times the measured POL/USD oracle, so
    no fixed dollar or POL value is baked in here.

    The scorecard is rebuilt from the ever-updating gas tape, so the parse is
    cached against the file's ``(mtime, size)`` rather than for the lifetime of
    the process: a refreshed tape must reach the next priced merge instead of a
    base fee frozen at import time.
    """
    try:
        stat = os.stat(path)
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    cached = _MERGE_GAS_CACHE.get(path)
    if cached is not None and stamp is not None and cached[:2] == stamp:
        return dict(cached[2])

    out: dict[str, float] = {}
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                condition_id = (row.get("condition_id") or "").strip().lower()
                raw = (row.get("merge_gas_pol") or "").strip()
                if not condition_id or not raw:
                    continue
                try:
                    gas_pol = float(raw)
                except ValueError:
                    continue
                if gas_pol > 0.0:
                    out[condition_id] = gas_pol
    except OSError as exc:
        logger.warning(
            f"CTF gas scorecard {path} unreadable; falling back to the documented "
            f"gas-unit estimate for every market: {exc}"
        )
    if stamp is not None:
        _MERGE_GAS_CACHE[path] = (stamp[0], stamp[1], dict(out))
    return out


def estimate_polygon_eip1559_gas_usd(
    gas_units: int,
    gas_price_gwei: float,
    pol_usd_price: float,
) -> float:
    """Calculate exact on-chain Polygon PoS EIP-1559 gas cost in USD.

    Formula:
        gas_pol = gas_units * (gas_price_gwei * 1e-9)
        gas_usd = gas_pol * pol_usd_price

    All three inputs are measured: gas units from the gas tape, gas price from
    the live EIP-1559 oracle, POL/USD from the live exchange oracle.
    """
    gas_pol = gas_units * (gas_price_gwei * 1e-9)
    return round(gas_pol * pol_usd_price, 6)


def compute_leg_taker_fee(
    shares: float,
    price: float,
    fee_schedule: Optional[FeeSchedule],
    vip_tier: str = "bronze",
) -> Optional[tuple[float, float, float]]:
    """Gross taker fee, VIP rebate credit, and net taker fee paid.

    Returns ``(gross_taker_fee_usd, vip_rebate_usd, net_taker_fee_usd)``, or
    ``None`` when the market published no fee schedule.  A market whose fee
    schedule Gamma did not supply has an *unknown* taker fee; inventing a rate
    (the removed ``FeeSchedule(rate=0.02, exponent=1.0)`` fallback) would price
    an edge off a number nobody measured, so the caller must refuse the leg
    instead.  Zero shares is a zero fee on any schedule.
    """
    if shares <= 0:
        return 0.0, 0.0, 0.0
    if not (0.0 <= price <= 1.0):
        raise ValueError(f"Price out of range [0.0, 1.0]: {price}")
    if fee_schedule is None:
        return None

    gross_fee = taker_fee(shares, price, fee_schedule)
    vip_rebate_rate = VIP_TIER_REBATES.get(vip_tier.lower(), 0.0)
    vip_rebate = round(gross_fee * vip_rebate_rate, 5)
    net_fee = round(gross_fee - vip_rebate, 5)
    return gross_fee, vip_rebate, net_fee
