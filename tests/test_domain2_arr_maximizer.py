"""Institutional Verification Suite for Domain 2 ARR Maximizer & Capital Cycle Velocity.

Verifies:
1. Physical Capital Cycle Time:
   T_cycle = t_event_remaining + t_proposal_lag + t_liveness_remaining + t_redeem_latency + t_reinvest
   - Regime A (UMA-proposed): t_event=0, t_prop=0, T_cycle in [0.5h, 2.2h].
   - Regime B (Track 6 verified): t_event=0, T_cycle in [2.0h, 2.5h] (~2.45h).
   - In-play unconcluded: includes remaining event time + pipeline latency.
2. Objective SCIP ARR Maximization:
   - Regime A wins when close to expiration (t_live = 0.3h) with high ARR despite smaller raw discount.
   - Regime B wins when Track 6 verifies real-world conclusion (p=1.000, h=0.000) with wide discount (6%) and ~2.5h cycle.
   - Toxic in-play markets with high hazard and 48h duration are mathematically rejected (0 allocation).
3. Seamless Track 6 Exogenous Oracle Integration (GroundTruthOutcome).
"""

from __future__ import annotations

import math
import pytest

from polymarketbot.trading.domain2.types import (
    GroundTruthOutcome,
    OracleCategory,
    OracleStatus,
)
from polymarketbot.rewards.queue_valuation import PolygonPoSEnvironment
from polymarketbot.trading.domain2.ortools_allocator import (
    Domain2Allocation,
    Domain2Candidate,
    Domain2OptimizationResult,
    Domain2OrtoolsAllocator,
)
from polymarketbot.trading.domain2.robust_hazard import (
    DEFAULT_PROPOSAL_LAG_HOURS,
    DEFAULT_REDEEM_LATENCY_HOURS,
    DEFAULT_REINVESTMENT_LATENCY_HOURS,
    DEFAULT_UMA_LIVENESS_HOURS,
    HOURS_PER_YEAR,
    calculate_cycle_velocity,
    calculate_physical_cycle_time,
)


def test_physical_capital_cycle_time_regimes():
    """Verify physical cycle time T_cycle under on-chain pipeline physics across all regimes."""
    # 1. Regime A: Already UMA-proposed, 0.3h of liveness remaining
    t_regime_a_acute = calculate_physical_cycle_time(
        hours_to_resolution=0.3,
        is_uma_proposed=True,
    )
    # Expected: 0.0 (event) + 0.0 (prop) + 0.3 (liveness) + 0.05 (redeem) + 0.15 (reinvest) = 0.50h
    assert pytest.approx(t_regime_a_acute, abs=1e-3) == 0.50

    # Regime A: Just proposed, full 2.0h liveness remaining
    t_regime_a_fresh = calculate_physical_cycle_time(
        hours_to_resolution=2.0,
        is_uma_proposed=True,
    )
    # Expected: 0.0 + 0.0 + 2.0 + 0.05 + 0.15 = 2.20h
    assert pytest.approx(t_regime_a_fresh, abs=1e-3) == 2.20
    assert 0.5 <= t_regime_a_acute <= 2.2
    assert 0.5 <= t_regime_a_fresh <= 2.2

    # 2. Regime B: Event concluded in real world (Track 6), not yet proposed
    t_regime_b = calculate_physical_cycle_time(
        hours_to_resolution=10.0,  # nominal market expiry
        is_uma_proposed=False,
        is_deterministic_conclusion=True,
    )
    # Expected: 0.0 (event concluded!) + 0.25 (prop lag) + 2.0 (liveness) + 0.05 (redeem) + 0.15 (reinvest) = 2.45h
    assert pytest.approx(t_regime_b, abs=1e-3) == 2.45
    assert 2.0 <= t_regime_b <= 2.5

    # 3. In-Play Unconcluded Market: 48h to event resolution
    t_inplay = calculate_physical_cycle_time(
        hours_to_resolution=48.0,
        is_uma_proposed=False,
        is_deterministic_conclusion=False,
    )
    # Expected: 48.0 (event) + 0.25 (prop) + 2.0 (liveness) + 0.05 (redeem) + 0.15 (reinvest) = 50.45h
    assert pytest.approx(t_inplay, abs=1e-3) == 50.45


def test_regime_a_wins_near_expiration_high_arr_smaller_raw_discount():
    """Verify Regime A wins when close to expiration (t_live = 0.3h) with high ARR despite smaller raw discount."""
    allocator = Domain2OrtoolsAllocator(
        max_single_bet_pct=0.10,
        use_dynamic_sizing=False,  # Test pure SCIP objective ARR ranking
        use_fixed_ticket_cost=False,
    )

    # Candidate A: Regime A (UMA proposed), only 0.3h liveness left, 0.5% discount ($0.995 price)
    # T_cycle = 0.50h -> Annualized multiplier = 8760 / 0.50 = 17,520 cycles/yr!
    # Expected ARR = 17520 * (0.005 / 0.995) ~ 8804% ARR
    cand_regime_a = Domain2Candidate(
        condition_id="0xREGIME_A_ACUTE",
        token_id="tok_a",
        question="Senate Vote Cloture - Proposed",
        category="politics",
        price=0.995,
        hours_to_resolution=0.3,
        depth_usd=10000.0,
        is_uma_proposed=True,
        p_hat=1.000,
    )

    # Candidate Slow: Unproposed in-play market, 24.0h left, 3.0% discount ($0.970 price)
    # 6x larger raw discount! But T_cycle ~ 26.45h -> Annualized multiplier = 8760 / 26.45 ~ 331 cycles/yr
    # Expected ARR = 331 * (0.030 / 0.970) ~ 1024% ARR
    cand_slow = Domain2Candidate(
        condition_id="0xSLOW_WIDE",
        token_id="tok_slow",
        question="Macro Inflation Release",
        category="macro",
        price=0.970,
        hours_to_resolution=24.0,
        depth_usd=10000.0,
        is_uma_proposed=False,
        p_hat=1.000,
    )

    res = allocator.optimize_allocation([cand_regime_a, cand_slow], bankroll_usd=10000.0)

    assert res.solver_status == "OPTIMAL"
    alloc_map = {a.condition_id: a for a in res.allocations}

    assert "0xREGIME_A_ACUTE" in alloc_map
    assert "0xSLOW_WIDE" in alloc_map

    alloc_a = alloc_map["0xREGIME_A_ACUTE"]
    alloc_slow = alloc_map["0xSLOW_WIDE"]

    # Candidate A's annualized ARR is dramatically higher than Candidate Slow
    # despite having 6x smaller raw discount!
    assert alloc_a.t_cycle_hours == pytest.approx(0.50, abs=1e-2)
    assert alloc_a.annualized_arr_pct > 7000.0  # > 7,000% ARR
    assert alloc_slow.annualized_arr_pct < 1500.0  # ~ 1,000% ARR
    assert alloc_a.annualized_arr_pct > alloc_slow.annualized_arr_pct * 5.0


def test_regime_b_wins_track6_concluded_wide_discount():
    """Verify Regime B wins when a real-world conclusion is verified by Track 6 (p=1.000, h=0.000) with wide discount."""
    allocator = Domain2OrtoolsAllocator(
        max_single_bet_pct=0.10,
        use_dynamic_sizing=False,
        use_fixed_ticket_cost=False,
    )

    # Candidate B: Verified concluded in real life by Track 6 Oracle, wide 6% discount ($0.940 price)
    # T_cycle = 2.45h -> Annual multiplier = 8760 / 2.45 = 3575.5 cycles/yr
    # Expected ARR = 3575.5 * (0.060 / 0.940) ~ 22,822% ARR!
    oracle_b = GroundTruthOutcome(
        event_id="nba_final_celtics_lakers",
        category=OracleCategory.SPORTS,
        status=OracleStatus.CONCLUDED,
        concluded=True,
        winning_outcome="YES",
        p_hat=1.000,
        h_reversal=0.000,
        confidence=1.000,
    )
    cand_regime_b = Domain2Candidate(
        condition_id="0xTRACK6_CONCLUDED",
        token_id="tok_b",
        question="Celtics Win Game 7 (Final Whistle)",
        category="sports",
        price=0.940,
        hours_to_resolution=6.0,  # nominal clock, but already concluded
        depth_usd=15000.0,
        oracle_outcome=oracle_b,
        is_uma_proposed=False,
    )

    # Candidate A: Proposed UMA market with small 0.2% discount ($0.998 price) and 1.8h liveness left
    # T_cycle = 1.8 + 0.20 = 2.0h -> Annual multiplier = 8760 / 2.0 = 4380 cycles/yr
    # Expected ARR = 4380 * (0.002 / 0.998) ~ 877% ARR
    cand_regime_a = Domain2Candidate(
        condition_id="0xREGIME_A_TINY_MARGIN",
        token_id="tok_a_tiny",
        question="Standard Proposed Market",
        category="crypto",
        price=0.998,
        hours_to_resolution=1.8,
        depth_usd=15000.0,
        is_uma_proposed=True,
        p_hat=1.000,
    )

    res = allocator.optimize_allocation([cand_regime_b, cand_regime_a], bankroll_usd=10000.0)

    assert res.solver_status == "OPTIMAL"
    alloc_map = {a.condition_id: a for a in res.allocations}

    alloc_b = alloc_map["0xTRACK6_CONCLUDED"]
    alloc_a = alloc_map["0xREGIME_A_TINY_MARGIN"]

    # Candidate B's T_cycle is 2.45h (reflecting post-conclusion UMA pipeline latency)
    assert alloc_b.t_cycle_hours == pytest.approx(2.45, abs=1e-2)
    # Candidate B's ARR dominates Candidate A's ARR by over 20x!
    assert alloc_b.annualized_arr_pct > 20000.0  # > 20,000% ARR
    assert alloc_a.annualized_arr_pct < 1500.0
    assert alloc_b.annualized_arr_pct > alloc_a.annualized_arr_pct * 20.0


def test_toxic_in_play_market_mathematically_rejected():
    """Verify long-tail in-play markets with high hazard and long duration (48h) are mathematically rejected."""
    allocator = Domain2OrtoolsAllocator(
        max_single_bet_pct=0.10,
        use_dynamic_sizing=True,
        variance_penalty_lambda=3.0,
    )

    # Candidate Toxic: 48 hours to resolution, looks like a "cheap" 10% discount at 0.900,
    # but has a severe downside hazard h = 0.150 (e.g. coin flip drift or unresolved tail).
    # Risk-adjusted return:
    # raw_edge = (0.85 - 0.90) / 0.90 = -5.5% (or even if p=0.92, raw_edge = +2.2%)
    # variance_penalty = 3.0 * (0.15 / 0.90^2) = 3.0 * 0.185 = 55.5% penalty!
    # Net risk-adjusted edge = -53.3% < 0
    # Annualized ARR coefficient < 0 -> SCIP strictly sets w_toxic = 0.0, z_toxic = 0
    cand_toxic = Domain2Candidate(
        condition_id="0xTOXIC_IN_PLAY",
        token_id="tok_toxic",
        question="Unresolved Crypto Volatility Strike in 48h",
        category="crypto",
        price=0.900,
        hours_to_resolution=48.0,
        depth_usd=8000.0,
        p_hat=0.880,  # 12% hazard
        is_uma_proposed=False,
    )

    # Candidate Clean: Clean near-expiry settlement basis carry
    cand_clean = Domain2Candidate(
        condition_id="0xCLEAN_BASIS",
        token_id="tok_clean",
        question="Clean Proposed Resolution",
        category="politics",
        price=0.985,
        hours_to_resolution=0.8,
        depth_usd=10000.0,
        is_uma_proposed=True,
        p_hat=0.999,
    )

    res = allocator.optimize_allocation([cand_toxic, cand_clean], bankroll_usd=10000.0)

    assert res.solver_status == "OPTIMAL"
    allocated_cids = {a.condition_id for a in res.allocations}

    # Clean candidate is funded
    assert "0xCLEAN_BASIS" in allocated_cids
    # Toxic 48h candidate is mathematically rejected with 0 allocation
    assert "0xTOXIC_IN_PLAY" not in allocated_cids


def test_oracle_outcome_dynamic_override():
    """Verify that attaching GroundTruthOutcome overrides p_hat=1.000, h=0.000 and calculates post-conclusion cycle."""
    oracle_outcome = GroundTruthOutcome(
        event_id="bls_cpi_march_2026",
        category=OracleCategory.MACRO,
        status=OracleStatus.CONCLUDED,
        concluded=True,
        winning_outcome="YES",
        p_hat=1.000,
        h_reversal=0.000,
    )

    cand = Domain2Candidate(
        condition_id="0xCPI_PRINT_CONCLUDED",
        token_id="tok_cpi",
        question="US Core CPI YoY <= 2.6%",
        category="macro",
        price=0.950,
        hours_to_resolution=18.0,  # Scheduled nominal expiry was 18 hours away
        depth_usd=5000.0,
        oracle_outcome=oracle_outcome,
        is_uma_proposed=False,
    )

    # 1. Candidate properties
    assert cand.is_concluded is True
    assert cand.estimated_win_prob == 1.000
    # Cycle time should reflect post-conclusion UMA pipeline latency (~2.45h), NOT 18.0h!
    assert pytest.approx(cand.physical_cycle_time, abs=1e-2) == 2.45

    # 2. Optimization allocates capital based on post-conclusion velocity
    allocator = Domain2OrtoolsAllocator(max_single_bet_pct=0.10)
    res = allocator.optimize_allocation([cand], bankroll_usd=10000.0)

    assert res.solver_status == "OPTIMAL"
    assert len(res.allocations) == 1
    alloc = res.allocations[0]
    assert alloc.condition_id == "0xCPI_PRINT_CONCLUDED"
    assert alloc.t_cycle_hours == pytest.approx(2.45, abs=1e-2)
    assert alloc.annualized_arr_pct > 15000.0  # ~18,000% ARR
    assert res.expected_portfolio_arr_usd > 0.0
    assert res.expected_portfolio_arr_pct > 0.0
