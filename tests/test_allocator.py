"""Unit tests for production-grade MINLP/MILP Capital Allocator."""

import pytest
from polymarketbot.rewards.allocator import LiquidityRewardsAllocator, MarketAllocation, PortfolioAllocationResult


@pytest.fixture
def allocator():
    return LiquidityRewardsAllocator(reserve_pct=0.25, default_spread_offset_cents=1.50)


@pytest.fixture
def sample_candidates():
    return [
        {
            "condition_id": "0x_senate_48",
            "question": "Will the Republican Party hold 48 or more Senate seats?",
            "daily_pool": 565.0,
            "competing_score": 3400.0,
            "max_spread": 4.5,
            "min_size": 20.0,
            "real_mid": 0.435,
        },
        {
            "condition_id": "0x_gemini_carbon",
            "question": "Will Gemini Carbon be released between Nov 9 and Nov 15?",
            "daily_pool": 323.0,
            "competing_score": 1684.0,
            "max_spread": 4.5,
            "min_size": 20.0,
            "real_mid": 0.745,
        },
        {
            "condition_id": "0x_iran_diplomacy",
            "question": "Next US-Iran senior diplomatic meeting by Dec 31?",
            "daily_pool": 500.0,
            "competing_score": 9999.0,
            "max_spread": 4.5,
            "min_size": 20.0,
            "real_mid": 0.445,
        },
        {
            "condition_id": "0x_ca_wealth_tax",
            "question": "California one-time wealth tax passes?",
            "daily_pool": 2000.0,
            "competing_score": 405000.0,
            "max_spread": 3.0,
            "min_size": 50.0,
            "real_mid": 0.235,
        },
    ]


def test_calculate_min_floor_capital(allocator):
    # P = 565, D = 3400, max_spread = 4.5, s = 1.5 => alpha = ((4.5 - 1.5)/4.5)^2 = (3.0/4.5)^2 = 4/9 = 0.4444
    alpha = allocator.calculate_alpha(max_spread_cents=4.5, spread_offset_cents=1.5)
    assert abs(alpha - 0.4444) < 1e-3

    c_floor = allocator.calculate_min_floor_capital(daily_pool=565.0, competing_score=3400.0, alpha=alpha)
    # c_floor = 3400 / (0.4444 * 564) = 3400 / 250.666 = 13.56
    assert 13.0 < c_floor < 14.0


def test_micro_capital_allocation_concentrates(allocator, sample_candidates):
    # Bankroll $150 -> usable $112.50 (25% reserve = $37.50)
    result = allocator.optimize_allocation_milp(candidates=sample_candidates, bankroll_usd=150.0)

    assert result.bankroll_usd == 150.0
    assert result.usable_capital == 112.50
    assert result.cash_reserve == 37.50
    assert result.active_markets_count == 1
    assert result.total_allocated <= 112.50
    assert result.total_daily_payout > 5.0
    assert result.allocations[0].clears_floor is True
    # Guaranteed to pick the top sweet spot market
    assert result.allocations[0].condition_id in ("0x_senate_48", "0x_gemini_carbon")


def test_large_capital_allocation_diversifies(allocator, sample_candidates):
    # Bankroll $2,000 -> usable $1,500.00
    result = allocator.optimize_allocation_milp(
        candidates=sample_candidates,
        bankroll_usd=2000.0,
        max_active_markets=3,
    )

    assert result.bankroll_usd == 2000.0
    assert result.usable_capital == 1500.0
    assert result.cash_reserve == 500.0
    # Because of diminishing returns, large capital must diversify into multiple markets
    assert result.active_markets_count >= 2
    assert result.total_allocated <= 1500.0
    for alloc in result.allocations:
        assert alloc.clears_floor is True
        assert alloc.allocated_capital >= alloc.min_floor_capital


def test_unviable_pool_rejection(allocator):
    unviable = [
        {
            "condition_id": "0x_dead_pool",
            "question": "Dead pool",
            "daily_pool": 0.50,  # Below $1.00 total pool!
            "competing_score": 1000.0,
            "max_spread": 3.0,
            "min_size": 20.0,
        }
    ]
    result = allocator.optimize_allocation_milp(candidates=unviable, bankroll_usd=150.0)
    assert result.active_markets_count == 0
    assert result.total_daily_payout == 0.0
