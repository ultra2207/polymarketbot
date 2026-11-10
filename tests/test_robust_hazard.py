"""Tests for Robust Capital Velocity & Dynamic Hazard Modeling."""

import pytest
from polymarketbot.trading.domain2.robust_hazard import (
    calculate_cycle_velocity,
    calculate_physical_cycle_time,
    BayesianCategoryState,
    BayesianDisputeTracker,
    RobustHazardModel,
    measured_dispute_counts,
)


def test_measured_dispute_counts_join_the_same_request_population(tmp_path):
    """A dispute is charged to the proposal it answers, never to a stranger.

    The tape's disputed ``Settle`` rows belong to proposals made months earlier,
    so counting every ``disputed`` row against every ``ProposePrice`` row mixed
    two populations and read 10/331 instead of the measured 0/331.  The join is
    the OO request key ``(requester, identifier, request_timestamp)``.
    """
    import duckdb

    tape = tmp_path / "uma.parquet"
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE t (event VARCHAR, requester VARCHAR, identifier VARCHAR, "
            "request_timestamp BIGINT, disputed BOOLEAN, disputer VARCHAR)"
        )
        rows = [
            # two proposals, one of which is disputed later
            ("ProposePrice", "0xreq", "0xid", 1, False, None),
            ("ProposePrice", "0xreq", "0xid", 2, False, None),
            # a Settle for the first proposal, by a real disputer
            ("Settle", "0xreq", "0xid", 1, True, "0xdisputer"),
            # a disputed Settle for a request this tape never saw
            ("Settle", "0xreq", "0xid", 99, True, "0xdisputer"),
        ]
        con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?)", rows)
        con.execute(f"COPY t TO '{tape.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
    counts = measured_dispute_counts(
        report_path=str(tmp_path / "missing.json"), tape_glob=str(tape)
    )
    assert counts is not None
    assert counts.proposals == 2
    assert counts.disputes == 1


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

