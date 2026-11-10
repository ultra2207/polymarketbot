"""Unit tests for the surviving CTF split primitives.

The cross-split arbitrage engine, its config and its execution dataclasses were
removed as dead code (nothing constructed them in production or in the soak).
Only the EIP-1559 gas estimator and the per-leg taker-fee/VIP-rebate primitive
survive, so only their tests remain.
"""

from __future__ import annotations

import pytest

from polymarketbot.fees import FeeSchedule
from polymarketbot.trading.ctf_split_arbitrage import (
    compute_leg_taker_fee,
    estimate_polygon_eip1559_gas_usd,
)

# A schedule the venue actually published for a fee-paying market.  The
# primitive takes it as an argument: there is no built-in default rate, because
# a market with no published schedule has an unknown fee.
SCHEDULE = FeeSchedule(rate=0.02, exponent=1.0)


# ---------------------------------------------------------------------------
# 1. Polygon PoS EIP-1559 Gas Friction Tests
# ---------------------------------------------------------------------------
def test_estimate_polygon_eip1559_gas_calculation():
    # 85,000 gas units, 40 Gwei, $0.40 POL/USD:
    # 85,000 * (40 * 1e-9) = 0.0034 POL
    # 0.0034 * $0.40 = $0.001360 USD
    gas_usd = estimate_polygon_eip1559_gas_usd(
        gas_units=85_000,
        gas_price_gwei=40.0,
        pol_usd_price=0.40,
    )
    assert gas_usd == 0.00136

    # Gas surge test: 150 Gwei, $0.65 POL/USD:
    # 85,000 * (150 * 1e-9) = 0.01275 POL
    # 0.01275 * $0.65 = $0.008288 USD
    surge_gas_usd = estimate_polygon_eip1559_gas_usd(
        gas_units=85_000,
        gas_price_gwei=150.0,
        pol_usd_price=0.65,
    )
    assert surge_gas_usd == 0.008288


# ---------------------------------------------------------------------------
# 2. Taker Fee & VIP Rebate Integration Tests
# ---------------------------------------------------------------------------
def test_compute_leg_taker_fee_with_vip_discount():
    # 1,000 shares @ p=0.50, fee_rate=0.02, exponent=1.0:
    # curve = 0.50 * 0.50 = 0.25
    # gross_fee = 1,000 * 0.02 * 0.25 = 5.00000 USDC
    # VIP Bronze (3% rebate): rebate = 5.00 * 0.03 = 0.15000 USDC
    # net_fee = 5.00 - 0.15 = 4.85000 USDC
    gross, rebate, net = compute_leg_taker_fee(
        shares=1000.0,
        price=0.50,
        fee_schedule=SCHEDULE,
        vip_tier="bronze",
    )
    assert gross == 5.00000
    assert rebate == 0.15000
    assert net == 4.85000

    # Obsidian tier (50% rebate):
    # gross = 5.00, rebate = 2.50, net = 2.50
    gross_obs, rebate_obs, net_obs = compute_leg_taker_fee(
        shares=1000.0,
        price=0.50,
        fee_schedule=SCHEDULE,
        vip_tier="obsidian",
    )
    assert gross_obs == 5.00000
    assert rebate_obs == 2.50000
    assert net_obs == 2.50000


def test_compute_leg_taker_fee_zero_or_invalid():
    assert compute_leg_taker_fee(0.0, 0.50, SCHEDULE) == (0.0, 0.0, 0.0)

    with pytest.raises(ValueError, match="Price out of range"):
        compute_leg_taker_fee(100.0, -0.05, SCHEDULE)

    with pytest.raises(ValueError, match="Price out of range"):
        compute_leg_taker_fee(100.0, 1.05, SCHEDULE)


def test_compute_leg_taker_fee_refuses_an_unpublished_schedule():
    """An unknown fee schedule is missing data, never a 2% rate."""
    assert compute_leg_taker_fee(1000.0, 0.50, None) is None
    assert compute_leg_taker_fee(1000.0, 0.50, None, vip_tier="obsidian") is None
