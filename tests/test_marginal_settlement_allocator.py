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

from polymarketbot.strategy.settlement.allocator import (
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
        p_win=1.0,  # no dispute hazard under test
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
        p_win=1.0,  # no dispute hazard under test
        condition_id="0xSHORT",
        token_id="tok_short",
        question="Resolves in 60s",
        target_settle_sec=60.0,
        ask_ladder=[(0.980, 500.0)],
    )

    cand_long = SettlementCandidate(
        p_win=1.0,  # no dispute hazard under test
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
        p_win=1.0,  # no dispute hazard under test
        condition_id="0xA",
        token_id="tokA",
        question="Fast Market A",
        target_settle_sec=60.0,
        ask_ladder=[(0.960, 100.0), (0.980, 200.0)],
    )

    # Market B: moderate duration (120s), 1 level:
    # Level 0: 0.960 (200 shs = $192) -> vel = (0.04/0.96)*(31536000/120) = 10,950
    cand_b = SettlementCandidate(
        p_win=1.0,  # no dispute hazard under test
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
            p_win=1.0,  # no dispute hazard under test
            condition_id="0xFAST_DEEP",
            token_id="tok1",
            question="Fast Deep Discount",
            target_settle_sec=60.0,
            ask_ladder=[(0.960, 300.0)],  # $288 capacity
        ),
        # Candidate 2: Medium velocity, medium discount
        SettlementCandidate(
            p_win=1.0,  # no dispute hazard under test
            condition_id="0xMED",
            token_id="tok2",
            question="Medium Velocity",
            target_settle_sec=180.0,
            ask_ladder=[(0.975, 300.0)],  # $292.5 capacity
        ),
        # Candidate 3: Slow velocity, shallow discount
        SettlementCandidate(
            p_win=1.0,  # no dispute hazard under test
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
    marg_arr = (
        sum(a.annualized_arr * a.allocated_usd for a in res.allocations)
        / res.total_allocated_usd
        if res.total_allocated_usd > 0.0
        else 0.0
    )
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
            p_win=1.0,  # no dispute hazard under test
            condition_id="0xC1",
            token_id="t1",
            question="Candidate 1",
            target_settle_sec=90.0,
            ask_ladder=[(0.960, 100.0), (0.970, 150.0)],
        ),
        SettlementCandidate(
            p_win=1.0,  # no dispute hazard under test
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

    assert len(res_scip.allocations) == len(res_wf.allocations)
    # Total allocated should match within rounding tolerance
    assert abs(res_scip.total_allocated_usd - res_wf.total_allocated_usd) < 1.0
    # Both achieve high portfolio ARR
    arr_scip = sum(a.annualized_arr * a.allocated_usd for a in res_scip.allocations) / res_scip.total_allocated_usd
    arr_wf = sum(a.annualized_arr * a.allocated_usd for a in res_wf.allocations) / res_wf.total_allocated_usd
    assert abs(arr_scip - arr_wf) / arr_scip < 0.05


def test_constraints_and_edge_cases():
    """Verify venue-minimum, zero budget, empty candidates, and ladder-depth-only caps."""
    allocator = MarginalSettlementAllocator()

    # Empty candidate list
    res_empty = allocator.allocate([], usable_capital=500.0)
    assert res_empty.allocations == []
    assert res_empty.total_allocated_usd == 0.0

    # A venue minimum order of 20 shares costs $19.20 at 0.960, so $15 is
    # below the executable minimum and must be pruned, not rounded up.
    cand_min = SettlementCandidate(
        p_win=1.0,  # no dispute hazard under test
        condition_id="0xC",
        token_id="t",
        question="Q",
        target_settle_sec=100.0,
        ask_ladder=[(0.960, 50.0)],
        min_order_shares=20.0,
    )
    res_low = allocator.allocate([cand_min], usable_capital=15.0)
    assert res_low.allocations == []

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
    assert res.allocations == []


def test_fill_at_landing_reprices_the_decision_ladder():
    """A landing fill consumes the live ladder, clamped to depth and the price limit."""
    landing_ladder = [(0.950, 100.0), (0.960, 200.0)]

    shs, cost, vwap, fills, reason = MarginalSettlementAllocator.fill_at_landing(
        landing_ladder, 190.0, min_order_shares=1.0, limit_price=0.999
    )
    assert reason == ""
    # 100 shares at 0.950 ($95) then 95/0.960 shares at 0.960 ($95).
    assert shs == pytest.approx(100.0 + 95.0 / 0.960)
    assert cost == pytest.approx(190.0)
    assert vwap == pytest.approx(190.0 / (100.0 + 95.0 / 0.960))
    assert fills[0][0] == pytest.approx(0.950)
    assert fills[0][1] == pytest.approx(100.0)


def test_fill_at_landing_refuses_a_price_above_the_edge_limit():
    """Levels above the p_win/fee limit are never crossed, and an order with no
    marketable level is unfilled rather than booked at the decision price."""
    shs, cost, vwap, fills, reason = MarginalSettlementAllocator.fill_at_landing(
        [(0.990, 500.0)], 100.0, limit_price=0.950
    )
    assert (shs, cost, vwap, fills) == (0.0, 0.0, 0.0, [])
    assert "at or below the limit" in reason

    # A ladder that is cheap at the top and expensive below it fills the
    # marketable part instead of losing the whole tranche.
    shs, cost, vwap, fills, reason = MarginalSettlementAllocator.fill_at_landing(
        [(0.900, 100.0), (0.990, 500.0)], 500.0, limit_price=0.950
    )
    assert reason == ""
    assert shs == pytest.approx(100.0)
    assert cost == pytest.approx(90.0)
    assert vwap == pytest.approx(0.900)
    assert [f[0] for f in fills] == [0.900]


def test_fill_at_landing_refuses_missing_depth_and_sub_minimum_orders():
    """No resting depth, or a fill below the venue minimum, cannot move cash."""
    shs, cost, vwap, fills, reason = MarginalSettlementAllocator.fill_at_landing(
        [], 100.0, limit_price=0.999
    )
    assert (shs, cost, vwap, fills) == (0.0, 0.0, 0.0, [])
    assert "no resting ask depth" in reason

    shs, cost, vwap, fills, reason = MarginalSettlementAllocator.fill_at_landing(
        [(0.900, 2.0)], 100.0, min_order_shares=5.0, limit_price=0.999
    )
    assert (shs, cost, vwap, fills) == (0.0, 0.0, 0.0, [])
    assert "below venue minimum" in reason


def test_gas_aware_fundability_rejects_a_ticket_the_fixed_charge_eats():
    """A market whose *smallest* ticket cannot clear gas and fees is never started.

    Gas is a fixed charge per redemption, so the minimum order is the least
    profitable tranche; the marginal velocity alone cannot express that.
    """
    dust = SettlementCandidate(
        condition_id="0xDUST",
        token_id="tok",
        question="Dust ticket",
        target_settle_sec=7200.0,
        ask_ladder=[(0.985, 1.0)],
        p_win=0.9874,
        min_order_shares=1.0,
        gas_usd=0.02,
    )
    assert dust.ticket_net_profit(1.0) < 0.0
    assert dust.is_fundable() is False
    assert MarginalSettlementAllocator("water_filling").allocate([dust], 100.0).allocations == []


def test_venue_minimum_ticket_is_upsized_instead_of_dropped():
    """A profitable tranche below the venue minimum is lifted to the minimum."""
    cand = SettlementCandidate(
        condition_id="0xUP",
        token_id="tok",
        question="Small but profitable",
        target_settle_sec=7200.0,
        ask_ladder=[(0.900, 2.0), (0.950, 100.0)],
        p_win=0.940,
        min_order_shares=5.0,
        gas_usd=0.0023,
    )
    assert cand.is_fundable() is True
    result = MarginalSettlementAllocator("water_filling").allocate([cand], 1000.0)
    assert len(result.allocations) == 1
    alloc = result.allocations[0]
    assert alloc.shares == pytest.approx(5.0)
    assert alloc.allocated_usd == pytest.approx(4.65)
    assert alloc.net_profit_usd > 0.0


def test_candidate_priced_below_its_ask_is_not_funded():
    """The pillar's old failure mode: p_win at or below the ask funds nothing."""
    cand = SettlementCandidate(
        condition_id="0xNOPE",
        token_id="tok",
        question="No edge",
        target_settle_sec=7200.0,
        ask_ladder=[(0.970, 1000.0)],
        p_win=0.970,
        min_order_shares=1.03,
    )
    assert cand.is_fundable() is False
    assert MarginalSettlementAllocator("water_filling").allocate([cand], 1000.0).allocations == []


# ---------------------------------------------------------------------------
# Paper pillar <-> research allocator agreement
#
# The live pillar funds an ask when ``p_win - ask - taker_fee(1 share, ask) > 0``
# (src/polymarketbot/strategy/settlement/decision.py).  The allocator must rank the
# same market with the same number, or "the optimizer says buy" and "the paper
# trader bought nothing" describe two different economies.  The market below is
# the one live verdict with an open book in the 2026-11-10 10:10 window:
# 0xfd5168...3751d7, UMA proposed NO (p_win = 1 - 1.2614 % hazard), 3 % fee
# schedule, best bid 0.999 and no ask at all.
# ---------------------------------------------------------------------------
P_WIN_F1 = 0.9873864453988181  # 1 - measured 1.2614 % upper-95 dispute hazard
F1_ASK = 0.999                 # best bid; the book held no ask in the window


def _f1_market():
    from polymarketbot.fees import FeeSchedule
    from polymarketbot.strategy.venue.reads import Market

    return Market(
        condition_id="0xfd516863c645e716dd507bd159267c85362b9b9b4a8bc9af340c2dda3b3751d7",
        question="Will Ferrari be the 2026 F1 Constructors' Champion?",
        outcomes=("Yes", "No"),
        token_ids=("96912874793126282354908962872291658704737497095823887605278933606056778248314",
                   "18878472349414286485242586468071886466912267679955194690093488106481296139200"),
        tick=0.001,
        min_order=5.0,
        daily_reward=0.0,
        reward_max_spread_cents=0.0,
        reward_min_size=0.0,
        fee_schedule=FeeSchedule(rate=0.03, exponent=1.0, taker_only=True,
                                 rebate_rate=0.25, source="gamma"),
        neg_risk=False,
        closed=False,
        uma_status="resolved",
        outcome_prices=(0.0005, 0.9995),
        category="settlement",
    )


def test_paper_pillar_and_allocator_agree_on_the_marginal_edge():
    """One ask, one p_win, one edge number in both code paths."""
    import polymarketbot.strategy.settlement.decision as paper
    from polymarketbot.strategy.settlement.fair_value import OracleVerdict

    market = _f1_market()
    verdict = OracleVerdict(winning_index=1, p_win=P_WIN_F1, source="uma_tape",
                            observed_ts=0.0)
    cand = SettlementCandidate(
        p_win=P_WIN_F1,
        condition_id=market.condition_id,
        token_id=market.token_ids[1],
        question=market.question,
        target_settle_sec=600.0,
        ask_ladder=[(F1_ASK, 4135.0)],
        fee_schedule=market.fee_schedule,
    )

    inputs = paper.edge_inputs(market, verdict, F1_ASK)

    assert inputs is not None
    # The allocator's marginal edge is the exact one-share edge, so the two differ
    # only by the 5 dp fee rounding (<= 5e-6/share), never in sign.
    assert abs(inputs.edge_per_share - cand.marginal_edge(F1_ASK)) <= 1e-5
    assert cand.marginal_edge(F1_ASK) < 0.0
    assert inputs.edge_per_share < 0.0
    # The pillar's own gate agrees with ``fundable_legs``.
    assert paper.fundable_legs(market, verdict, [(F1_ASK, 4135.0)], 1_000.0) == []


def test_the_break_even_ask_is_the_price_that_would_have_funded():
    """The only number that distinguishes a correctly idle radar from a blind one."""
    import polymarketbot.strategy.settlement.decision as paper
    from polymarketbot.strategy.settlement.fair_value import OracleVerdict

    market = _f1_market()
    verdict = OracleVerdict(winning_index=1, p_win=P_WIN_F1, source="uma_tape",
                            observed_ts=0.0)
    inputs = paper.edge_inputs(market, verdict, F1_ASK)

    assert inputs is not None
    break_even = inputs.break_even_ask
    assert break_even is not None
    # p_win - ask - 0.03*ask*(1-ask) = 0  =>  ask ~ 0.98701 at this verdict.
    assert abs(break_even - 0.9870064453988181) < 1e-9
    # Zero edge at the break-even, positive one tick below, negative one above.
    assert abs(paper.marginal_edge(market, verdict, break_even + 1e-12)) < 1e-9
    assert paper.marginal_edge(market, verdict, break_even - market.tick) > 0.0
    assert paper.marginal_edge(market, verdict, break_even + market.tick) < 0.0
    # And the live ask is 1.2 cents above it: the book, not the gate, is why
    # this market funded nothing.
    assert F1_ASK - break_even > 0.01


def test_candidate_requires_a_measured_survival_probability():
    """An unknown p_win must not default to par: that is the pending-status shortcut."""
    with pytest.raises(TypeError):
        SettlementCandidate(  # type: ignore[call-arg]
            condition_id="0xNOPWIN",
            token_id="t",
            question="Missing survival probability",
            target_settle_sec=60.0,
            ask_ladder=[(0.90, 100.0)],
        )


# ---------------------------------------------------------------------------
# Tail-budget allocation: expected profit inside a measured loss budget
#
# Measured 2026-11-10 14:25 (soak window 9): water-filling put $997.67 and
# 1,049.1 shares into ONE condition (0xbcd8bf70c6...) at prices 0.81-0.98.
# The ladder below is that fill tape: [(price, shares)] of the eight landed
# levels with Gamma's own 5 % taker schedule on this market.
# ---------------------------------------------------------------------------
WIN9_LADDER = [
    (0.8100, 20.0),
    (0.8300, 20.0),
    (0.8600, 25.0),
    (0.8700, 84.61),
    (0.8800, 83.33),
    (0.9600, 19.83),
    (0.9700, 300.0),
    (0.9800, 496.32),
]
WIN9_P_WIN = 0.98947   # 1 - measured 1.0530 % upper-95 dispute hazard
WIN9_EQUITY = 1000.0
# The measured tail (data/derived/uma/outcome_accuracy.json, 2026-11-10):
# 158 of 134,664 closed markets went through a dispute, so the reversal rate -
# which cannot exceed the challenge rate - is bounded at 0.001335 (95 %).
MEASURED_Q = 0.00133477
# A stated appetite: $150 of loss in the worst 5 % band.
APPETITE = 150.0
CONFIDENCE = 0.95


def _fee_schedule():
    from polymarketbot.fees import FeeSchedule

    # Rate/exponent read back from the window's own fees: 20 sh @ 0.81 paid
    # $0.1539 = 20 * 0.81 * 0.19 * 0.05.
    return FeeSchedule(rate=0.05, exponent=1.0)


def _evidence(challenges: int = 158, trials: int = 134664, reversals: int = 0,
              direct_trials: int = 70):
    from polymarketbot.strategy.settlement.tail_evidence import evidence_from_counts

    return evidence_from_counts(
        direct_trials,
        reversals,
        disputed=0,
        source="test",
        challenge_trials=trials,
        challenges=challenges,
    )


def _risk_candidate(condition_id="0xbcd8bf70c6", *,
                    event_key="evt-win9", exhaustive=True,
                    ladder=None, p_win=WIN9_P_WIN, token_id=None):
    return SettlementCandidate(
        condition_id=condition_id,
        token_id=token_id or f"tok-{condition_id}",
        question=f"question {condition_id}",
        target_settle_sec=7200.0,
        p_win=p_win,
        ask_ladder=list(WIN9_LADDER if ladder is None else ladder),
        fee_schedule=_fee_schedule(),
        min_order_shares=5.0,
        event_key=event_key,
        event_exhaustive=exhaustive,
    )


def _allocate(candidates, capital, *, appetite=APPETITE, confidence=CONFIDENCE,
              evidence=None, prior=()):
    return MarginalSettlementAllocator("tail_budget").allocate(
        candidates,
        capital,
        risk_appetite_usd=appetite,
        tail_confidence=confidence,
        tail_evidence=evidence or _evidence(),
        prior=prior,
        equity_usd=WIN9_EQUITY,
    )


def test_tail_budget_requires_a_stated_appetite_and_a_measured_tail():
    """An absent appetite, band or measurement refuses; none of them defaults."""
    from polymarketbot.strategy.settlement.tail_evidence import UnmeasuredTail

    cand = _risk_candidate()
    allocator = MarginalSettlementAllocator("tail_budget")
    with pytest.raises(ValueError):
        allocator.allocate([cand], WIN9_EQUITY, tail_confidence=CONFIDENCE,
                           tail_evidence=_evidence())
    with pytest.raises(ValueError):
        allocator.allocate([cand], WIN9_EQUITY, risk_appetite_usd=APPETITE,
                           tail_evidence=_evidence())
    with pytest.raises((UnmeasuredTail, ValueError)):
        allocator.allocate([cand], WIN9_EQUITY, risk_appetite_usd=APPETITE,
                           tail_confidence=CONFIDENCE)
    with pytest.raises(ValueError):
        allocator.allocate([cand], WIN9_EQUITY, risk_appetite_usd=None,
                           tail_confidence=CONFIDENCE, tail_evidence=_evidence())


def test_tail_budget_is_exactly_the_measured_expected_shortfall():
    """ES at the stated band is sum_c q_c L_c / (1 - alpha), to the cent.

    With the measured tail (0.1335 %) and a $150 appetite at 95 % the budget
    admits 0.05 * 150 / 0.001335 = $5,618 of notional, so a $1,000 treasury is
    limited by CASH, not by risk - and the shortfall statement still has to
    equal the formula on the dollars actually deployed.
    """
    result = _allocate([_risk_candidate()], WIN9_EQUITY)
    report = result.tail_budget
    assert report is not None
    assert report.tail_pd == pytest.approx(MEASURED_Q, rel=1e-5)
    assert "cash" in report.binding_constraint
    assert result.total_allocated_usd <= WIN9_EQUITY + 1e-9
    all_in = sum(row.all_in_usd for row in result.allocations)
    expected = report.tail_pd * all_in
    assert report.expected_loss_usd == pytest.approx(expected, rel=1e-6)
    assert report.expected_shortfall_usd == pytest.approx(expected / 0.05, rel=1e-6)
    assert report.expected_shortfall_usd <= report.risk_appetite_usd
    for row in result.allocations:
        assert row.notional_at_risk_usd == pytest.approx(row.all_in_usd)
        assert row.tail_charge_usd == pytest.approx(row.all_in_usd * report.tail_pd / 0.05)


def test_a_tighter_appetite_binds_the_budget_instead_of_the_cash():
    """Shrinking the appetite shrinks the book until the tail budget binds.

    ``L * q / (1 - alpha) = appetite`` is a number, not a percentage cap: the
    size falls to exactly what the stated appetite pays for.
    """
    cand = _risk_candidate()
    # The appetite that pays for exactly $300 of notional:
    # appetite = q * L / (1 - alpha).
    tight = MEASURED_Q * 300.0 / 0.05
    result = _allocate([cand], WIN9_EQUITY, appetite=tight)
    report = result.tail_budget
    assert report is not None
    assert report.binding_constraint == "tail budget"
    assert report.expected_shortfall_usd == pytest.approx(tight, rel=1e-3)
    assert result.total_allocated_usd < WIN9_EQUITY
    # L * q / (1 - alpha) <= appetite, on the dollars actually deployed.
    all_in = sum(row.all_in_usd for row in result.allocations)
    assert all_in * MEASURED_Q / 0.05 <= tight * (1.0 + 1e-6)


def test_a_2x_tail_error_changes_the_size_without_flipping_the_decision():
    """The least-measured input is allowed to move the size, never the sign.

    Doubling the measured tail rate halves the notional a fixed appetite
    admits, while every funded tranche keeps a positive expected profit and the
    same participation decision.  A Kelly solve sign-flips on the same change.
    """
    from polymarketbot.strategy.settlement.allocator import shortfall_usd

    cand = _risk_candidate()
    # An appetite that pays for exactly $500 of notional at the measured tail:
    # at twice the rate it pays for half as much.
    appetite = MEASURED_Q * 500.0 / 0.05
    base = _allocate([cand], WIN9_EQUITY, appetite=appetite)
    doubled = _allocate([cand], WIN9_EQUITY, appetite=appetite,
                        evidence=_evidence(challenges=316))
    assert base.total_allocated_usd > 0.0
    assert doubled.total_allocated_usd > 0.0
    assert doubled.tail_budget.tail_pd > base.tail_budget.tail_pd
    # Rate doubles, so the admissible notional halves (same appetite, same q/L).
    assert doubled.tail_budget.expected_shortfall_usd <= appetite * (1.0 + 1e-6)
    ratio = doubled.total_allocated_usd / base.total_allocated_usd
    assert 0.4 <= ratio <= 0.6
    # The sign of the decision is unchanged: both books are funded and both
    # keep a positive expected profit per dollar.
    for result in (base, doubled):
        assert result.expected_portfolio_profit_usd > 0.0
        assert all(row.net_profit_usd > 0.0 for row in result.allocations)
    assert shortfall_usd(1.0, 0.95) == pytest.approx(20.0)


def test_two_markets_on_one_resolution_source_are_not_independent():
    """Same UMA question / negRisk event -> ONE cluster, charged once in full.

    Two markets that share a resolution source fail together, so their
    notionals add to one cluster loss instead of each getting its own budget.
    Two markets on distinct sources carry two clusters, and the shortfall is
    the sum over clusters - which is why a shared source can never be used to
    diversify.
    """
    shared = _allocate(
        [_risk_candidate("0xaaa", event_key="evt-1", exhaustive=False),
         _risk_candidate("0xbbb", event_key="evt-1", exhaustive=False)],
        WIN9_EQUITY,
    )
    distinct = _allocate(
        [_risk_candidate("0xaaa", event_key="evt-1"),
         _risk_candidate("0xbbb", event_key="evt-2")],
        WIN9_EQUITY,
    )
    assert shared.tail_budget is not None and distinct.tail_budget is not None
    assert len(shared.tail_budget.cluster_notional_usd) == 1
    assert len(distinct.tail_budget.cluster_notional_usd) == 2
    # Total notional is the same in both books; only the clustering differs.
    shared_notional = sum(shared.tail_budget.cluster_notional_usd.values())
    distinct_notional = sum(distinct.tail_budget.cluster_notional_usd.values())
    assert shared_notional == pytest.approx(distinct_notional, rel=1e-6)
    # One cluster carries the whole loss, so no exposure is credited as diversified.
    only = next(iter(shared.tail_budget.cluster_notional_usd.values()))
    assert only == pytest.approx(shared_notional)


def test_same_token_double_count_is_charged_not_hedged():
    """Two lots on ONE token are rho = +1, never a hedge against each other."""
    from polymarketbot.strategy.settlement.allocator import (
        LINK_COMPLEMENT,
        LINK_SAME_TOKEN,
        Position,
        ResolutionClusterBook,
    )

    a = Position("0xc1", "tok-yes", "evt-1")
    b = Position("0xc1", "tok-yes", "evt-1")
    c = Position("0xc1", "tok-no", "evt-1")
    other = Position("0xc2", "tok-z", "evt-1")
    assert ResolutionClusterBook.link(a, b) == LINK_SAME_TOKEN
    assert ResolutionClusterBook.link(a, c) == LINK_COMPLEMENT
    # A different condition in the same event is the same SOURCE, not a hedge.
    assert ResolutionClusterBook.link(a, other) != LINK_COMPLEMENT


def test_a_held_position_is_charged_without_being_respent():
    """Prior inventory consumes the budget; its dollars are not spent twice."""
    from polymarketbot.strategy.settlement.allocator import HeldExposure

    cand = _risk_candidate()
    prior = [
        HeldExposure(
            condition_id=cand.condition_id,
            token_id=cand.token_id,
            shares=600.0,
            cost_usd=588.0,
            claimed_token_id=cand.token_id,
            cluster_key="evt-win9",
        )
    ]
    held = _allocate([cand], 400.0, prior=prior)
    fresh = _allocate([cand], 400.0)
    assert held.total_allocated_usd <= 400.0 + 1e-9
    assert held.tail_budget is not None
    assert held.tail_budget.prior_expected_loss_usd == pytest.approx(MEASURED_Q * 588.0, rel=1e-5)
    assert (
        held.tail_budget.expected_loss_usd
        > fresh.tail_budget.expected_loss_usd
    )


def test_a_book_already_over_its_appetite_refuses_to_add():
    """When the held book alone breaches the budget, nothing new is funded."""
    from polymarketbot.strategy.settlement.allocator import HeldExposure

    cand = _risk_candidate()
    prior = [
        HeldExposure(
            condition_id=cand.condition_id,
            token_id=cand.token_id,
            shares=1000.0,
            cost_usd=980.0,
            claimed_token_id=cand.token_id,
            cluster_key="evt-win9",
        )
    ]
    result = _allocate([cand], 500.0, appetite=1.0, prior=prior)
    assert result.allocations == []
    assert result.tail_budget is not None
    assert result.tail_budget.refusal
    assert "already held carries" in result.tail_budget.refusal


def test_the_stated_appetite_has_one_named_place_and_no_default(tmp_path):
    """The policy loader refuses a missing, empty or unusable statement."""
    from polymarketbot.paper.settlement_risk import (
        UnmeasuredRiskAppetite,
        load_settlement_tail_policy,
    )

    missing = tmp_path / "absent.yaml"
    with pytest.raises(UnmeasuredRiskAppetite):
        load_settlement_tail_policy(str(missing))

    empty = tmp_path / "empty.yaml"
    empty.write_text("other_section:\n  x: 1\n", encoding="utf-8")
    with pytest.raises(UnmeasuredRiskAppetite):
        load_settlement_tail_policy(str(empty))

    partial = tmp_path / "partial.yaml"
    partial.write_text(
        "settlement_harvester:\n  risk_appetite_usd: 150.0\n", encoding="utf-8"
    )
    with pytest.raises(UnmeasuredRiskAppetite):
        load_settlement_tail_policy(str(partial))

    bad_band = tmp_path / "bad.yaml"
    bad_band.write_text(
        "settlement_harvester:\n  risk_appetite_usd: 150.0\n  tail_confidence: 1.0\n",
        encoding="utf-8",
    )
    with pytest.raises(UnmeasuredRiskAppetite):
        load_settlement_tail_policy(str(bad_band))

    good = tmp_path / "good.yaml"
    good.write_text(
        "settlement_harvester:\n  risk_appetite_usd: 150.0\n  tail_confidence: 0.995\n",
        encoding="utf-8",
    )
    policy = load_settlement_tail_policy(str(good))
    assert policy.risk_appetite_usd == pytest.approx(150.0)
    assert policy.tail_confidence == pytest.approx(0.995)

    # The repository's own statement must load: the engine reads only this one.
    from polymarketbot.paper.settlement_risk import load_settlement_tail_policy as repo

    stated = repo()
    assert stated.risk_appetite_usd > 0.0
    assert 0.0 < stated.tail_confidence < 1.0
