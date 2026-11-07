"""Tests for Robust Capital Velocity & Dynamic Hazard Modeling."""

import pytest
from polymarketbot.trading.domain2.robust_hazard import (
    calculate_cycle_velocity,
    BayesianCategoryState,
    BayesianDisputeTracker,
    RobustHazardModel,
)


def test_cycle_velocity_prevents_infinite_asymptote():
    """Verify that physical latency prevents infinite turnover multiplier as tau -> 0."""
    # Near-zero horizon (e.g. 3 minutes = 0.05h)
    rate_fast, ann_mult_fast = calculate_cycle_velocity(
        hours_to_resolution=0.05,
        settlement_latency_hours=2.0,
        reinvestment_latency_hours=1.0,
    )
    # T_cycle = 0.05 + 2.0 + 1.0 = 3.05 hours
    assert 0.32 < rate_fast < 0.33
    assert 2800.0 < ann_mult_fast < 2900.0  # Bounded at ~2872x, NOT 175,200x!

    # 24-hour horizon
    rate_slow, ann_mult_slow = calculate_cycle_velocity(
        hours_to_resolution=24.0,
        settlement_latency_hours=2.0,
        reinvestment_latency_hours=1.0,
    )
    # T_cycle = 24.0 + 2.0 + 1.0 = 27.0 hours
    assert 0.036 < rate_slow < 0.038
    assert 320.0 < ann_mult_slow < 330.0


def test_bayesian_dispute_tracker_self_healing():
    """Verify that Bayesian tracker updates dynamically upon observing dispute events."""
    tracker = BayesianDisputeTracker()
    initial_hazard = tracker.get_credible_upper_hazard("politics")

    # Initial baseline should be around 0.1% to 0.3%
    assert 0.001 <= initial_hazard <= 0.005

    # Simulate a sudden wave of 5 disputes in 'politics'
    for _ in range(5):
        tracker.record_outcome("politics", reversed_or_disputed=True)

    updated_hazard = tracker.get_credible_upper_hazard("politics")
    # Hazard must increase to protect capital
    assert updated_hazard > initial_hazard
    assert updated_hazard > 0.005

    # Now observe 200 clean settlements
    for _ in range(200):
        tracker.record_outcome("politics", reversed_or_disputed=False)

    recovered_hazard = tracker.get_credible_upper_hazard("politics")
    # Should self-heal downward
    assert recovered_hazard < updated_hazard


def test_crypto_fpt_barrier_hazard():
    """Verify that continuous diffusion correctly scales hazard with distance to strike."""
    model = RobustHazardModel()

    # Case 1: Spot is barely $50 above $80,000 strike (high danger)
    h_danger = model.calculate_diffusion_fpt_hazard(
        spot=80050.0,
        strike=80000.0,
        vol_annual=0.60,
        hours_to_resolution=1.0,
    )
    # Case 2: Spot is $3,000 above strike (deep safety)
    h_safe = model.calculate_diffusion_fpt_hazard(
        spot=83000.0,
        strike=80000.0,
        vol_annual=0.60,
        hours_to_resolution=1.0,
    )

    assert h_danger > 0.35   # > 35% probability of crossing strike within 1 hour
    assert h_safe < 0.01     # < 1% probability for 3k buffer
    assert h_danger > 10 * h_safe


def test_minimax_worst_case_evaluation():
    """Verify that RobustHazardModel returns conservative minimax bounds."""
    model = RobustHazardModel()

    # Candidate with market price 0.94 (implying 6% discount)
    p_win, h_robust, hourly_vel = model.evaluate_candidate(
        category="economics",
        price=0.940,
        hours_to_resolution=2.0,
        is_uma_proposed=False,
    )

    # Implied hazard from 6% discount (~4.5% after liquidity spread) dominates
    assert h_robust >= 0.040
    assert p_win == 1.0 - h_robust
    assert hourly_vel > 0.0
