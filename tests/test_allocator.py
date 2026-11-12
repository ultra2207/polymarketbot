"""Unit tests for production-grade MINLP/MILP Capital Allocator."""

import pytest

from polymarketbot.strategy.reward.allocator import (
    LiquidityRewardsAllocator,
)


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

    # Endogenous reserve mode: the reserve is the measured fit, not the 25 %
    # preference, so it may be larger or smaller than the fixed mode. What the
    # solver must guarantee is that it is funded from the same bankroll and the
    # allocation never exceeds the usable remainder.
    result_endog = allocator.optimize_allocation_milp(candidates=sample_candidates, bankroll_usd=150.0, use_endogenous_reserve=True)
    assert result_endog.active_markets_count == 1
    assert 0.0 < result_endog.cash_reserve < 150.0
    assert result_endog.usable_capital == pytest.approx(
        150.0 - result_endog.cash_reserve, abs=0.01
    )
    assert result_endog.total_allocated <= result_endog.usable_capital
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
    assert 0.0 < result_endog.cash_reserve < 2000.0
    assert result_endog.usable_capital == pytest.approx(
        2000.0 - result_endog.cash_reserve, abs=0.01
    )
    assert result_endog.total_allocated <= result_endog.usable_capital
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
    from polymarketbot.strategy.reward.reserve import ReserveCalibration

    # The reserve is now sized from the fitted coefficients; these are fixture
    # measurements stated by the test, not production defaults.
    calibration = ReserveCalibration(
        jump_shock_factor=0.20,
        diversification_weight=0.15,
        base_gas_actions=3.0,
        per_market_gas_actions=2.0,
        gas_floor_usd=0.01,
    )
    # Single market: reserve should be between 10% and 35% of bankroll
    single_alloc = [{"capital": 100.0, "mid": 0.50}]
    res_single = allocator.calculate_endogenous_reserve(
        single_alloc,
        bankroll_usd=150.0,
        base_gas_buffer_usd=0.0,
        calibration=calibration,
    )
    assert 10.0 <= res_single <= 45.0

    # Multi-market diversified portfolio (5 markets): square-root scaling frees up cash
    multi_alloc = [{"capital": 200.0, "mid": 0.50} for _ in range(5)]
    res_multi = allocator.calculate_endogenous_reserve(
        multi_alloc,
        bankroll_usd=1500.0,
        base_gas_buffer_usd=0.0,
        calibration=calibration,
    )
    # Total capital = 1000, diversified reserve should be <= 350 and > 40
    assert res_multi <= 1500.0 * 0.35
    assert res_multi >= 40.0


def test_continuous_optimization_governor(allocator, sample_candidates):
    from polymarketbot.strategy.reward.allocator import ContinuousOptimizationGovernor

    gov = ContinuousOptimizationGovernor(allocator=allocator, min_dwell_time_seconds=300.0)
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


def test_governor_maintain_does_not_invent_a_target(allocator, sample_candidates):
    """A MAINTAIN with no optimizer allocation has no target, not 0.75x bankroll."""
    from polymarketbot.strategy.reward.allocator import ContinuousOptimizationGovernor

    gov = ContinuousOptimizationGovernor(
        allocator=allocator, min_dwell_time_seconds=300.0
    )
    first = gov.evaluate_governor_cycle(
        current_active_markets=[],
        live_bankroll_usd=150.0,
        scanned_universe=sample_candidates,
        current_time=1000.0,
    )
    cid = first[0].condition_id

    # Every pool now sits below the $1/day floor, so the optimizer funds
    # nothing. The dwell gate still blocks an exit, so the only legal action is
    # MAINTAIN - and it must not claim a size it did not measure.
    unfundable = [dict(candidate, daily_pool=0.5) for candidate in sample_candidates]
    actions = gov.evaluate_governor_cycle(
        current_active_markets=[cid],
        live_bankroll_usd=150.0,
        scanned_universe=unfundable,
        current_time=1010.0,
    )
    assert [action.action_type for action in actions] == ["MAINTAIN"]
    assert actions[0].target_capital is None
    assert actions[0].target_quote_size is None


def test_allocator_refuses_without_a_bankroll(allocator, sample_candidates):
    """No bankroll means no allocation: the $150 default is gone."""
    with pytest.raises(ValueError, match="bankroll_usd is required"):
        allocator.optimize_allocation_milp(candidates=sample_candidates)
    with pytest.raises(ValueError, match="bankroll_usd must be finite"):
        allocator.optimize_allocation_milp(
            candidates=sample_candidates, bankroll_usd=float("nan")
        )


def test_competing_window_resets_at_utc_midnight():
    """The window mean never mixes two UTC days around the venue's epoch boundary."""
    from polymarketbot.strategy.reward.competition import CompetingScoreWindow

    midnight = 86_400.0  # POSIX 1970-01-02T00:00:00Z
    window = CompetingScoreWindow()
    window.observe("c1", 900.0, now=midnight - 120.0)
    window.observe("c1", 1_100.0, now=midnight - 60.0)
    before = window.estimate("c1", now=midnight - 60.0)
    assert before is not None
    assert before.samples == 2
    assert before.mean == pytest.approx(1_000.0)

    after = window.observe("c1", 1_200.0, now=midnight + 60.0)
    assert after.samples == 1
    assert after.mean == pytest.approx(1_200.0)


def test_scanner_tables_are_never_observed_half_loaded(monkeypatch):
    """Threads racing the scanner load get the table, never the pre-assignment None."""
    import threading
    import time

    from polymarketbot.strategy.reward import measurement
    from polymarketbot.strategy.reward.scanner import pricing

    sentinel = object()

    def slow_load():
        time.sleep(0.05)
        return sentinel

    monkeypatch.setattr(measurement, "load_quote_fill_horizon", slow_load)
    monkeypatch.setattr(pricing, "_FILL_HORIZON", None)
    monkeypatch.setattr(pricing, "_FILL_HORIZON_TRIED", False)

    results: list = []
    lock = threading.Lock()

    def worker() -> None:
        value = pricing._fill_horizon_table()
        with lock:
            results.append(value)

    workers = [threading.Thread(target=worker) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=10)

    assert len(results) == 8
    assert all(result is sentinel for result in results), (
        f"{sum(result is None for result in results)} of {len(results)} readers "
        "saw a half-loaded pricing table"
    )


def test_unmeasured_markout_is_not_a_zero_cost():
    """A missing markout is missing; 0.0 would claim the fill had no adverse move."""
    from polymarketbot.strategy.reward.fill_measurement import markout_cost_usd

    assert markout_cost_usd(0.0, None) == 0.0
    assert markout_cost_usd(100.0, -0.209) == pytest.approx(-0.209)
    assert markout_cost_usd(100.0, None) is None


def test_water_filling_never_spends_more_than_the_budget():
    """Activation lots that do not all fit must be funded by marginal yield, not all.

    Two markets whose lots ($60 each) together exceed a $100 budget: the old
    fallback funded every starved lot and returned $120 allocated against a
    $100 budget, so the cash invariant broke after the decision was taken.
    """
    from polymarketbot.strategy.reward.allocator_candidates import parse_candidates
    from polymarketbot.strategy.reward.waterfill import solve_water_filling

    candidates = [
        {
            "condition_id": "0x_tight",
            "question": "tight",
            "daily_pool": 20.0,
            "competing_score": 500.0,  # high marginal yield at the lot
            "max_spread": 4.0,
            "min_size": 100.0,
            "real_mid": 0.60,
            "spread_offset_cents": 1.0,
        },
        {
            "condition_id": "0x_thin",
            "question": "thin",
            "daily_pool": 20.0,
            "competing_score": 1_000.0,  # low marginal yield at the lot
            "max_spread": 4.0,
            "min_size": 100.0,
            "real_mid": 0.60,
            "spread_offset_cents": 1.0,
        },
    ]
    parsed, _unmeasured = parse_candidates(candidates, usable_capital=100.0, floor_target=1.0)
    assert len(parsed) == 2
    for market in parsed:
        assert market["c_min"] <= 100.0
    assert sum(market["c_min"] for market in parsed) > 100.0

    result = solve_water_filling(parsed, bankroll_usd=200.0, usable_capital=100.0, cash_reserve=100.0)

    assert result.total_allocated <= 100.0 + 1e-9
    assert sum(allocation.allocated_capital for allocation in result.allocations) <= 100.0 + 1e-9
    # The budget can carry exactly one lot, and it goes to the higher-yield one.
    assert [allocation.condition_id for allocation in result.allocations] == ["0x_tight"]
    for allocation in result.allocations:
        assert allocation.allocated_capital >= allocation.min_floor_capital


def test_allocation_quote_size_uses_pair_cost_not_the_leg_mid():
    """Dollar capital -> shares per leg must divide by the pair cost.

    ``alpha`` is score per dollar of *pair* capital, so a $38 allocation at a
    $0.95 pair is 40 shares per leg. Dividing by the 0.475 leg mid would tell
    the governor 80 shares, i.e. $76 of pair capital.
    """
    from polymarketbot.strategy.reward.marginal import allocation_for_capital

    m = {
        "P": 500.0,
        "D": 100.0,
        "alpha": 1.0,
        "c_floor": 10.0,
        "mid": 0.475,
        "capital_per_share": 0.95,
        "cand": {"condition_id": "0xabc", "question": "q"},
        "fill_side": (1.0, 0.0),
    }
    allocation, _ = allocation_for_capital(m, 38.0)
    assert allocation.quote_size == pytest.approx(40.0)

    m.pop("capital_per_share")
    allocation, _ = allocation_for_capital(m, 38.0)
    # No stated pair cost: the definitional $1.00 upper bound, never the mid.
    assert allocation.quote_size == pytest.approx(38.0)
