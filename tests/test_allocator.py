"""Unit tests for production-grade MINLP/MILP Capital Allocator."""

import pytest
from polymarketbot.rewards.allocator import LiquidityRewardsAllocator, MarketAllocation, PortfolioAllocationResult


@pytest.fixture
def allocator():
    return LiquidityRewardsAllocator(reserve_pct=0.25)


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
            "spread_offset_cents": 1.5,
        },
        {
            "condition_id": "0x_gemini_carbon",
            "question": "Will Gemini Carbon be released between Nov 9 and Nov 15?",
            "daily_pool": 323.0,
            "competing_score": 1684.0,
            "max_spread": 4.5,
            "min_size": 20.0,
            "real_mid": 0.745,
            "spread_offset_cents": 1.5,
        },
        {
            "condition_id": "0x_iran_diplomacy",
            "question": "Next US-Iran senior diplomatic meeting by Dec 31?",
            "daily_pool": 500.0,
            "competing_score": 9999.0,
            "max_spread": 4.5,
            "min_size": 20.0,
            "real_mid": 0.445,
            "spread_offset_cents": 1.5,
        },
        {
            "condition_id": "0x_ca_wealth_tax",
            "question": "California one-time wealth tax passes?",
            "daily_pool": 2000.0,
            "competing_score": 405000.0,
            "max_spread": 3.0,
            "min_size": 50.0,
            "real_mid": 0.235,
            "spread_offset_cents": 1.5,
        },
    ]


def test_calculate_min_floor_capital(allocator):
    # P = 565, D = 3400, max_spread = 4.5, s = 1.5 => alpha = ((4.5 - 1.5)/4.5)^2 = (3.0/4.5)^2 = 4/9 = 0.4444
    alpha = allocator.calculate_alpha(max_spread_cents=4.5, spread_offset_cents=1.5)
    assert abs(alpha - 0.4444) < 1e-3

    c_floor_100 = allocator.calculate_min_floor_capital(daily_pool=565.0, competing_score=3400.0, alpha=alpha, floor_target=1.00)
    # c_floor = 3400 / (0.4444 * 564) = 3400 / 250.666 = 13.56
    assert 13.0 < c_floor_100 < 14.0

    # With safety buffer floor_target=1.35
    c_floor_135 = allocator.calculate_min_floor_capital(daily_pool=565.0, competing_score=3400.0, alpha=alpha, floor_target=1.35)
    assert 18.0 < c_floor_135 < 19.0


def test_micro_capital_allocation_concentrates(allocator, sample_candidates):
    # Fixed reserve mode: Bankroll $150 -> usable $112.50 (25% reserve = $37.50)
    result_fixed = allocator.optimize_allocation_milp(candidates=sample_candidates, bankroll_usd=150.0, use_endogenous_reserve=False)
    assert result_fixed.bankroll_usd == 150.0
    assert result_fixed.usable_capital == 112.50
    assert result_fixed.cash_reserve == 37.50
    assert result_fixed.active_markets_count == 1
    assert result_fixed.total_allocated <= 112.50
    assert result_fixed.total_daily_payout > 5.0
    assert result_fixed.allocations[0].clears_floor is True

    # Endogenous reserve mode: frees up more capital for quoting
    result_endog = allocator.optimize_allocation_milp(candidates=sample_candidates, bankroll_usd=150.0, use_endogenous_reserve=True)
    assert result_endog.active_markets_count == 1
    assert result_endog.cash_reserve < 37.50
    assert result_endog.allocations[0].condition_id in ("0x_senate_48", "0x_gemini_carbon")


def test_large_capital_allocation_diversifies(allocator, sample_candidates):
    # Fixed reserve mode: Bankroll $2,000 -> usable $1,500.00
    result_fixed = allocator.optimize_allocation_milp(
        candidates=sample_candidates,
        bankroll_usd=2000.0,
        max_active_markets=3,
        use_endogenous_reserve=False,
    )
    assert result_fixed.bankroll_usd == 2000.0
    assert result_fixed.usable_capital == 1500.0
    assert result_fixed.cash_reserve == 500.0
    assert result_fixed.active_markets_count >= 2
    assert result_fixed.total_allocated <= 1500.0

    # Endogenous reserve mode: frees up idle cash above static 25%
    result_endog = allocator.optimize_allocation_milp(
        candidates=sample_candidates,
        bankroll_usd=2000.0,
        max_active_markets=3,
        use_endogenous_reserve=True,
    )
    assert result_endog.active_markets_count >= 2
    assert result_endog.cash_reserve < 500.0
    for alloc in result_endog.allocations:
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


def test_endogenous_reserve_scaling(allocator):
    # Single market: reserve should be between 10% and 35% of bankroll
    single_alloc = [{"capital": 100.0, "mid": 0.50}]
    res_single = allocator.calculate_endogenous_reserve(single_alloc, bankroll_usd=150.0)
    assert 10.0 <= res_single <= 45.0

    # Multi-market diversified portfolio (5 markets): square-root scaling frees up cash
    multi_alloc = [{"capital": 200.0, "mid": 0.50} for _ in range(5)]
    res_multi = allocator.calculate_endogenous_reserve(multi_alloc, bankroll_usd=1500.0)
    # Total capital = 1000, diversified reserve should be <= 350 and > 40
    assert res_multi <= 1500.0 * 0.35
    assert res_multi >= 40.0


def test_continuous_optimization_governor(allocator, sample_candidates):
    from polymarketbot.rewards.allocator import ContinuousOptimizationGovernor

    gov = ContinuousOptimizationGovernor(allocator=allocator, hurdle_pct=0.15, min_dwell_time_seconds=300.0)
    actions = gov.evaluate_governor_cycle(
        current_active_markets=[],
        live_bankroll_usd=150.0,
        scanned_universe=sample_candidates,
        current_time=1000.0,
    )
    assert len(actions) >= 1
    assert actions[0].action_type == "SWITCH_MARKET"
    active_cid = actions[0].condition_id

    # Second cycle with active market in optimal set -> should emit MAINTAIN
    actions_2 = gov.evaluate_governor_cycle(
        current_active_markets=[active_cid],
        live_bankroll_usd=150.0,
        scanned_universe=sample_candidates,
        current_time=1030.0,
    )
    assert len(actions_2) == 1
    assert actions_2[0].action_type == "MAINTAIN"
