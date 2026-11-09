"""Unit tests for MarginalSettlementAllocator in Domain 2 (Strategy 2).

Verifies:
1. Exact mathematical formulation of marginal profit and annualized velocity.
2. Diminishing marginal return across multi-tier CLOB ask ladders.
3. Multi-candidate water-filling / KKT marginal velocity equalization.
4. Preference for high-velocity (short duration) deep-discount markets over low-velocity markets.
5. Strict outperformance over uniform tranche sizing.
6. SCIP solver and analytical Water-Filling solver parity.
7. Constraint compliance (min ticket, max market cap, slot limits, budget).
"""

from __future__ import annotations

import pytest

from polymarketbot.trading.domain2.settlement_allocator import (
    DEFAULT_SETTLEMENT_GAS_USD,
    SECONDS_PER_YEAR,
    MarginalSettlementAllocator,
    SettlementAllocation,
    SettlementCandidate,
    SettlementOptimizationResult,
)


def test_orderbook_ladder_consumption():
    """Verify that compute_shares_and_cost consumes levels in price-ascending order."""
    ladder = [
        (0.950, 100.0),  # $95.00 cost
        (0.960, 200.0),  # $192.00 cost
        (0.980, 500.0),  # $490.00 cost
    ]

    # Partial consumption of level 0: $47.50 -> 50 shares @ 0.950
    shs, cost, eff_px, marg_px, lvls, fills = MarginalSettlementAllocator.compute_shares_and_cost(ladder, 47.50)
    assert abs(shs - 50.0) < 1e-4
    assert abs(cost - 47.50) < 1e-4
    assert abs(eff_px - 0.950) < 1e-4
    assert abs(marg_px - 0.950) < 1e-4
    assert lvls == 1
    assert len(fills) == 1

    # Full consumption of level 0 ($95.00) + partial of level 1 ($96.00) = $191.00
    # Level 1 gives 96.0 / 0.960 = 100 shares. Total shares = 100 + 100 = 200 shares
    shs, cost, eff_px, marg_px, lvls, fills = MarginalSettlementAllocator.compute_shares_and_cost(ladder, 191.00)
    assert abs(shs - 200.0) < 1e-4
    assert abs(cost - 191.00) < 1e-4
    assert abs(eff_px - (191.0 / 200.0)) < 1e-4  # 0.955
    assert abs(marg_px - 0.960) < 1e-4
    assert lvls == 2
    assert len(fills) == 2


def test_marginal_velocity_diminishes_with_capital():
    """Verify that marginal velocity strictly decreases as capital sweeps up the ask ladder."""
    cand = SettlementCandidate(
        condition_id="0xTEST1",
        token_id="tok1",
        question="Will Event X settle Yes?",
        target_settle_sec=60.0,  # 1 minute to resolution
        ask_ladder=[
            (0.950, 100.0),  # level 0: $95.00
            (0.970, 100.0),  # level 1: $97.00
            (0.990, 100.0),  # level 2: $99.00
        ],
    )

    # Initial marginal velocity at C = 0: (1 - 0.95) / 0.95 * (31,536,000 / 60)
    vel_0 = cand.initial_marginal_velocity
    expected_vel_0 = ((1.000 - 0.950) / 0.950) * (SECONDS_PER_YEAR / 60.0)
    assert abs(vel_0 - expected_vel_0) < 1e-2

    # Deploying $50 stays on level 0 (marginal px = 0.950)
    pnl_50, arr_50, vel_50 = MarginalSettlementAllocator.candidate_profit(cand, 50.0)
    assert abs(vel_50 - expected_vel_0) < 1e-2

    # Deploying $150 sweeps into level 1 (marginal px = 0.970)
    pnl_150, arr_150, vel_150 = MarginalSettlementAllocator.candidate_profit(cand, 150.0)
    expected_vel_1 = ((1.000 - 0.970) / 0.970) * (SECONDS_PER_YEAR / 60.0)
    assert abs(vel_150 - expected_vel_1) < 1e-2
    assert vel_150 < vel_50  # Strictly diminishing marginal return!

    # Deploying $250 sweeps into level 2 (marginal px = 0.990)
    pnl_250, arr_250, vel_250 = MarginalSettlementAllocator.candidate_profit(cand, 250.0)
    expected_vel_2 = ((1.000 - 0.990) / 0.990) * (SECONDS_PER_YEAR / 60.0)
    assert abs(vel_250 - expected_vel_2) < 1e-2
    assert vel_250 < vel_150  # Even lower marginal return!


def test_velocity_favors_short_duration():
    """Verify that a candidate with shorter duration has massively higher velocity for the same discount."""
    cand_short = SettlementCandidate(
        condition_id="0xSHORT",
        token_id="tok_short",
        question="Resolves in 60s",
        target_settle_sec=60.0,
        ask_ladder=[(0.980, 500.0)],
    )

    cand_long = SettlementCandidate(
        condition_id="0xLONG",
        token_id="tok_long",
        question="Resolves in 3600s",
        target_settle_sec=3600.0,
        ask_ladder=[(0.960, 500.0)],  # Larger nominal discount (4% vs 2%), but 60x longer
    )

    vel_short = cand_short.initial_marginal_velocity
    vel_long = cand_long.initial_marginal_velocity

    # 2% in 60s gives (0.02 / 0.98) * (31536000 / 60) = 10,726.5
    # 4% in 3600s gives (0.04 / 0.96) * (31536000 / 3600) = 365.0
    assert vel_short > 25.0 * vel_long


def test_water_filling_equalizes_marginal_velocity():
    """Verify that water-filling allocates capital such that marginal velocity is balanced across markets."""
    allocator = MarginalSettlementAllocator(solver_backend="water_filling")

    # Market A: fast duration (60s), 2 levels:
    # Level 0: 0.960 (100 shs = $96) -> vel = (0.04/0.96)*(31536000/60) = 21,900
    # Level 1: 0.980 (200 shs = $196) -> vel = (0.02/0.98)*(31536000/60) = 10,726.5
    cand_a = SettlementCandidate(
        condition_id="0xA",
        token_id="tokA",
        question="Fast Market A",
        target_settle_sec=60.0,
        ask_ladder=[(0.960, 100.0), (0.980, 200.0)],
    )

    # Market B: moderate duration (120s), 1 level:
    # Level 0: 0.960 (200 shs = $192) -> vel = (0.04/0.96)*(31536000/120) = 10,950
    cand_b = SettlementCandidate(
        condition_id="0xB",
        token_id="tokB",
        question="Moderate Market B",
        target_settle_sec=120.0,
        ask_ladder=[(0.960, 200.0)],
    )

    # Budget $150:
    # First: Cand A Level 0 ($96, vel 21,900)
    # Next: remaining $54 goes to Cand B Level 0 (vel 10,950 > Cand A Level 1 vel 10,726.5)!
    res = allocator.allocate([cand_a, cand_b], usable_capital=150.0)

    alloc_map = {a.condition_id: a for a in res.allocations}
    assert "0xA" in alloc_map
    assert "0xB" in alloc_map

    assert abs(alloc_map["0xA"].allocated_usd - 96.00) < 1.0
    assert abs(alloc_map["0xB"].allocated_usd - 54.00) < 1.0
    assert res.total_allocated_usd <= 150.0


def test_marginal_strictly_beats_uniform_tranche_sizing():
    """Prove that Marginal Allocation strictly dominates uniform tranche sizing in ARR and net profit."""
    allocator = MarginalSettlementAllocator(solver_backend="auto")

    # 3 candidates with varying discount and duration
    candidates = [
        # Candidate 1: High velocity, deep discount
        SettlementCandidate(
            condition_id="0xFAST_DEEP",
            token_id="tok1",
            question="Fast Deep Discount",
            target_settle_sec=60.0,
            ask_ladder=[(0.960, 300.0)],  # $288 capacity
        ),
        # Candidate 2: Medium velocity, medium discount
        SettlementCandidate(
            condition_id="0xMED",
            token_id="tok2",
            question="Medium Velocity",
            target_settle_sec=180.0,
            ask_ladder=[(0.975, 300.0)],  # $292.5 capacity
        ),
        # Candidate 3: Slow velocity, shallow discount
        SettlementCandidate(
            condition_id="0xSLOW_SHALLOW",
            token_id="tok3",
            question="Slow Shallow Discount",
            target_settle_sec=1800.0,
            ask_ladder=[(0.990, 500.0)],  # $495 capacity
        ),
    ]

    # Uniform baseline: equal tranche per market from the same $500 budget.
    uniform_alloc = 0.0
    uniform_profit = 0.0
    uniform_arr_numerator = 0.0
    slot_alloc = 500.0 / len(candidates)
    for cand in candidates:
        deploy = min(slot_alloc, cand.total_depth_usd)
        shs, cost, _eff, _marg, _lvls, fills = MarginalSettlementAllocator.compute_shares_and_cost(
            cand.ask_ladder, deploy
        )
        net = cand.p_win * shs - cost - cand.fee_for_fills(fills) - cand.gas_usd
        uniform_alloc += cost
        uniform_profit += net
        uniform_arr_numerator += (net / cost) * (SECONDS_PER_YEAR / cand.target_settle_sec) * cost

    uniform_arr = uniform_arr_numerator / uniform_alloc if uniform_alloc > 0 else 0.0

    res = allocator.allocate(candidates, usable_capital=500.0)
    marg_arr = res.expected_portfolio_arr
    marg_profit = res.expected_portfolio_profit_usd

    # Marginal allocation must produce strictly higher ARR and net profit
    assert marg_arr > uniform_arr
    assert marg_profit > uniform_profit
    assert marg_arr / uniform_arr > 1.20
    assert marg_profit / uniform_profit > 1.20


def test_scip_and_water_filling_parity():
    """Verify that both SCIP MILP solver and Water-Filling solver produce concordant allocations."""
    candidates = [
        SettlementCandidate(
            condition_id="0xC1",
            token_id="t1",
            question="Candidate 1",
            target_settle_sec=90.0,
            ask_ladder=[(0.960, 100.0), (0.970, 150.0)],
        ),
        SettlementCandidate(
            condition_id="0xC2",
            token_id="t2",
            question="Candidate 2",
            target_settle_sec=120.0,
            ask_ladder=[(0.965, 120.0), (0.980, 200.0)],
        ),
    ]

    alloc_scip = MarginalSettlementAllocator(solver_backend="scip")
    alloc_wf = MarginalSettlementAllocator(solver_backend="water_filling")

    res_scip = alloc_scip.allocate(candidates, usable_capital=300.0)
    res_wf = alloc_wf.allocate(candidates, usable_capital=300.0)

    assert res_scip.active_markets_count == res_wf.active_markets_count
    # Total allocated should match within rounding tolerance
    assert abs(res_scip.total_allocated_usd - res_wf.total_allocated_usd) < 1.0
    # Both achieve high portfolio ARR
    assert abs(res_scip.expected_portfolio_arr - res_wf.expected_portfolio_arr) / res_scip.expected_portfolio_arr < 0.05


def test_constraints_and_edge_cases():
    """Verify venue-minimum, zero budget, empty candidates, and ladder-depth-only caps."""
    allocator = MarginalSettlementAllocator()

    # Empty candidate list
    res_empty = allocator.allocate([], usable_capital=500.0)
    assert res_empty.active_markets_count == 0
    assert res_empty.total_allocated_usd == 0.0

    # A venue minimum order of 20 shares costs $19.20 at 0.960, so $15 is
    # below the executable minimum and must be pruned, not rounded up.
    cand_min = SettlementCandidate(
        condition_id="0xC",
        token_id="t",
        question="Q",
        target_settle_sec=100.0,
        ask_ladder=[(0.960, 50.0)],
        min_order_shares=20.0,
    )
    res_low = allocator.allocate([cand_min], usable_capital=15.0)
    assert res_low.active_markets_count == 0

    # There is no bankroll-percentage cap: the entire real ladder depth is the
    # only per-market bound ($48 here).
    res_full = allocator.allocate([cand_min], usable_capital=500.0)
    assert abs(res_full.total_allocated_usd - 48.0) < 0.51


def test_negative_survival_edge_is_not_funded():
    """A candidate whose survival probability is below its ask plus fees gets zero capital."""
    allocator = MarginalSettlementAllocator(solver_backend="water_filling")
    cand = SettlementCandidate(
        condition_id="0xHAZARD",
        token_id="t",
        question="Hazardous outcome",
        target_settle_sec=600.0,
        ask_ladder=[(0.960, 100.0)],
        p_win=0.95,  # 5% hazard exceeds the 4% discount
    )
    res = allocator.allocate([cand], usable_capital=100.0)
    assert res.total_allocated_usd == 0.0
    assert res.active_markets_count == 0
