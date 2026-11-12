"""Tests for Order Book Microstructure Sentinel."""

import math

import pytest

from polymarketbot.strategy.reward.microstructure import (
    MicrostructureEngine,
    MicrostructureState,
    MultiAssetMicrostructureSentinel,
    compute_dynamic_drift_threshold_cents,
    compute_dynamic_ofi_threshold,
)

# Tape-fitted coefficients shared by these tests. Production callers load the
# same values from the fit artifact; the engine has no defaults.
OFI_FIT = {
    "c_depth": 1.5,
    "z_score": 3.0,
    "v_ref": 50_000.0,
    "beta_vol": 0.15,
}
PULL_WIDEN_FIT = {"pull_multiplier": 1.5, "widen_offset_multiplier": 1.5}


def _engine(**overrides):
    options = {**OFI_FIT, **PULL_WIDEN_FIT}
    options.update(overrides)
    return MicrostructureEngine(**options)


def test_initial_state():
    """Initial update should produce balanced OFI event = 0.0."""
    engine = _engine()
    state = engine.update(
        best_bid_price=0.50,
        best_bid_size=100.0,
        best_ask_price=0.52,
        best_ask_size=100.0,
    )

    assert state.mid_price == 0.51
    assert state.micro_price == 0.51
    assert state.drift_cents == 0.0
    assert state.imbalance_ratio == 0.0
    assert state.ofi_10_events == 0.0
    assert not state.is_toxic
    assert state.toxicity_reason == "unmeasured"
    assert state.measured is False
    assert math.isnan(state.ofi_threshold)
    assert math.isnan(state.drift_threshold_cents)


def test_ofi_same_price_depth_changes():
    """Test OFI when prices stay identical and size changes."""
    engine = _engine()
    # Initial
    engine.update(0.50, 100.0, 0.52, 100.0)

    # Bid size increases +50, ask size decreases -30
    # delta_b = 150 - 100 = 50
    # delta_a = 70 - 100 = -30
    # OFI = 50 - (-30) = +80
    state = engine.update(0.50, 150.0, 0.52, 70.0)
    assert state.ofi_10_events == 80.0


def test_ofi_price_improvements_and_drops():
    """Test Cont-Kukanov-Stoikov delta calculation when price levels change."""
    engine = _engine()
    engine.update(0.50, 100.0, 0.52, 100.0)

    # 1. Higher bid price (improvement): delta_b = Q_b_new (200.0)
    # Ask stays same: delta_a = 0.0
    # OFI = 200.0
    state1 = engine.update(0.51, 200.0, 0.52, 100.0)
    assert state1.ofi_10_events == 200.0

    # 2. Lower ask price (improvement): delta_a = Q_a_new (150.0)
    # Bid drops price (worsened): delta_b = -Q_b_old (-200.0)
    # OFI = -200 - 150 = -350
    state2 = engine.update(0.50, 80.0, 0.51, 150.0)
    # Rolling sum: 200 + (-350) = -150
    assert state2.ofi_10_events == -150.0


def test_ofi_rolling_window_10_events():
    """Test that OFI maintains exactly a 10-event rolling window."""
    engine = _engine(window_size=10)
    engine.update(0.50, 100.0, 0.52, 100.0)

    # Push 10 identical events with +100 OFI each
    for i in range(1, 11):
        # same price, bid size increases by 100 each time
        engine.update(0.50, 100.0 + i * 100.0, 0.52, 100.0)

    # 10 updates with +100 each = 1000 OFI
    assert engine.latest_state.ofi_10_events == 1000.0

    # 11th event: bid size unchanged (+0), ask size increases by 100 (delta_a = 100, OFI = -100)
    # The oldest +100 drops off, replaced by -100
    state11 = engine.update(0.50, 1100.0, 0.52, 200.0)
    assert state11.ofi_10_events == 800.0


def test_stoikov_micro_price_and_drift():
    """Verify Stoikov micro-price formula: (Q_b*P_a + Q_a*P_b)/(Q_b+Q_a)."""
    engine = _engine()

    # P_b = 0.40, P_a = 0.60
    # Q_b = 300, Q_a = 100
    # Micro-price = (300 * 0.60 + 100 * 0.40) / 400 = (180 + 40) / 400 = 220 / 400 = 0.55
    # Mid-price = (0.40 + 0.60) / 2 = 0.50
    # Drift = |0.55 - 0.50| * 100 = 5.0 cents
    state = engine.update(
        best_bid_price=0.40,
        best_bid_size=300.0,
        best_ask_price=0.60,
        best_ask_size=100.0,
    )

    assert pytest.approx(state.mid_price, 1e-6) == 0.50
    assert pytest.approx(state.micro_price, 1e-6) == 0.55
    assert pytest.approx(state.drift_cents, 1e-6) == 5.0
    assert pytest.approx(state.imbalance_ratio, 1e-6) == (300 - 100) / 400  # 0.50
    assert pytest.approx(state.bid_ratio, 1e-6) == 0.75


def test_toxicity_triggers():
    """Test OFI threshold and drift threshold triggers."""
    engine = _engine(
        ofi_threshold=850.0,
        drift_threshold_cents=0.45,
    )

    # Normal state
    s0 = engine.update(0.50, 100.0, 0.51, 100.0)
    assert not s0.is_toxic
    assert s0.toxicity_reason == ""

    # 1. Trigger via OFI only (drift = 0.0)
    engine.reset()
    engine.update(0.50, 100.0, 0.51, 100.0)
    engine.update(0.50, 1000.0, 0.51, 1000.0)
    # delta_b = 900, delta_a = 900 -> OFI = 0
    # Now trigger single large one-sided move
    s_ofi2 = engine.update(0.50, 1950.0, 0.51, 1000.0)  # delta_b = +950, OFI = +950 > 850
    assert abs(s_ofi2.ofi_10_events) > 850.0
    assert s_ofi2.is_toxic
    assert s_ofi2.toxicity_reason in ("OFI", "BOTH")

    # 2. Trigger via Drift only
    engine.reset()
    # Balanced OFI, but severe size skew causing drift > 0.45c
    # P_b = 0.50, P_a = 0.51 (spread = 1c)
    # Q_b = 9000, Q_a = 1000 -> Micro = (9000*0.51 + 1000*0.50)/10000 = 0.509
    # Mid = 0.505. Drift = (0.509 - 0.505)*100 = 0.40c
    # With Q_b = 9800, Q_a = 200 -> Micro = (9800*0.51 + 200*0.50)/10000 = 0.5098
    # Drift = (0.5098 - 0.505)*100 = 0.48c > 0.45c
    s_drift = engine.update(0.50, 9800.0, 0.51, 200.0)
    assert s_drift.drift_cents > 0.45
    assert s_drift.is_toxic


def test_quote_action_evaluation_toxic_buying():
    """When flow is toxic buying (positive OFI / upward drift), asks should be widened/pulled."""
    engine = _engine(
        ofi_threshold=850.0,
        drift_threshold_cents=0.45,
    )
    # Establish base
    engine.update(0.50, 500.0, 0.51, 500.0)
    # Huge buying pressure: OFI = +900
    engine.update(0.50, 1400.0, 0.51, 500.0)

    state = engine.latest_state
    assert state.is_toxic

    # Ask evaluation: toxic buyers will lift asks -> protect asks!
    ask_action = engine.evaluate_quote_action("SELL")
    assert ask_action.action in ("WIDEN", "PULL")
    assert ask_action.should_widen is True
    assert engine.should_widen_quote("SELL") is True

    # Bid evaluation: market is moving upwards away from bids -> bids are safe
    bid_action = engine.evaluate_quote_action("BUY")
    assert bid_action.action == "MAINTAIN"
    assert bid_action.should_pull is False
    assert engine.should_pull_quote("BUY") is False


def test_quote_action_evaluation_toxic_selling():
    """When flow is toxic selling (negative OFI / downward drift), bids should be widened/pulled."""
    engine = _engine(
        ofi_threshold=850.0,
        drift_threshold_cents=0.45,
    )
    # Establish base
    engine.update(0.50, 500.0, 0.51, 500.0)
    # Huge selling pressure: OFI = -900
    engine.update(0.50, 500.0, 0.51, 1400.0)

    state = engine.latest_state
    assert state.is_toxic

    # Bid evaluation: toxic sellers will hit bids -> protect bids!
    bid_action = engine.evaluate_quote_action("BID")
    assert bid_action.action in ("WIDEN", "PULL")
    assert bid_action.should_widen is True
    assert engine.should_widen_quote("BID") is True

    # Ask evaluation: market is moving downwards away from asks -> asks are safe
    ask_action = engine.evaluate_quote_action("ASK")
    assert ask_action.action == "MAINTAIN"
    assert ask_action.should_pull is False
    assert engine.should_pull_quote("ASK") is False


def test_ofi_fit_and_multipliers_have_no_defaults():
    """The caller must supply fitted coefficients and pull/widen multipliers."""
    with pytest.raises(TypeError):
        compute_dynamic_ofi_threshold(bid_size=100.0, ask_size=100.0)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        MicrostructureEngine(**OFI_FIT)  # missing pull/widen multipliers
    with pytest.raises(TypeError):
        MicrostructureEngine(**PULL_WIDEN_FIT)  # missing OFI fit


def test_multi_asset_microstructure_sentinel():
    """Verify sentinel tracks independent state across different assets."""
    sentinel = MultiAssetMicrostructureSentinel(**OFI_FIT, **PULL_WIDEN_FIT)

    s_asset1 = sentinel.update(
        asset_id="asset_yes",
        best_bid_price=0.40,
        best_bid_size=100.0,
        best_ask_price=0.42,
        best_ask_size=100.0,
        realized_volatility=0.02,
    )
    s_asset2 = sentinel.update(
        asset_id="asset_no",
        best_bid_price=0.58,
        best_bid_size=200.0,
        best_ask_price=0.60,
        best_ask_size=200.0,
        realized_volatility=0.02,
    )

    assert s_asset1.mid_price == pytest.approx(0.41)
    assert s_asset2.mid_price == pytest.approx(0.59)
    assert sentinel.get_engine("asset_yes").latest_state.mid_price == pytest.approx(0.41)
    assert sentinel.get_engine("asset_no").latest_state.mid_price == pytest.approx(0.59)
    # Each asset fits its own thresholds from its own book.
    assert s_asset1.measured is True
    assert s_asset2.measured is True
    assert s_asset1.ofi_threshold == pytest.approx(
        compute_dynamic_ofi_threshold(bid_size=100.0, ask_size=100.0, **OFI_FIT)
    )
    assert s_asset2.ofi_threshold == pytest.approx(
        compute_dynamic_ofi_threshold(bid_size=200.0, ask_size=200.0, **OFI_FIT)
    )
    assert s_asset1.ofi_threshold != s_asset2.ofi_threshold


def test_explicit_thresholds_used_verbatim():
    """Explicit constructor thresholds are measurements and are used as given."""
    engine = _engine(ofi_threshold=1234.5, drift_threshold_cents=0.77)
    state = engine.update(0.50, 100.0, 0.51, 100.0, realized_volatility=0.5)

    assert state.measured is True
    assert state.ofi_threshold == 1234.5
    assert state.drift_threshold_cents == 0.77


def test_dynamic_thresholds_fitted_from_book_and_realized_volatility():
    """With no explicit thresholds, the book and realised volatility set them."""
    engine = _engine()
    vol = 0.03
    state = engine.update(
        best_bid_price=0.50,
        best_bid_size=1200.0,
        best_ask_price=0.53,
        best_ask_size=800.0,
        realized_volatility=vol,
    )

    mid = (0.50 + 0.53) / 2.0
    assert state.measured is True
    assert state.mid_price == pytest.approx(mid)
    assert state.ofi_threshold == pytest.approx(
        compute_dynamic_ofi_threshold(bid_size=1200.0, ask_size=800.0, **OFI_FIT)
    )
    assert state.drift_threshold_cents == pytest.approx(
        compute_dynamic_drift_threshold_cents(
            mid_price=mid, spread_cents=3.0, volatility=vol
        )
    )


def test_dynamic_thresholds_track_book_shape():
    """Two different books must produce two different thresholds (live dynamic path)."""
    thin = _engine().update(
        best_bid_price=0.50,
        best_bid_size=100.0,
        best_ask_price=0.51,
        best_ask_size=100.0,
        realized_volatility=0.02,
    )
    deep = _engine().update(
        best_bid_price=0.50,
        best_bid_size=50_000.0,
        best_ask_price=0.55,
        best_ask_size=50_000.0,
        realized_volatility=0.02,
    )

    assert thin.measured is True
    assert deep.measured is True
    assert thin.ofi_threshold < deep.ofi_threshold
    assert thin.drift_threshold_cents != deep.drift_threshold_cents


def test_unmeasured_gate_without_thresholds_or_volatility():
    """No explicit thresholds and no volatility -> unmeasured, never a constant."""
    engine = _engine()
    state = engine.update(
        best_bid_price=0.50,
        best_bid_size=10_000.0,
        best_ask_price=0.51,
        best_ask_size=100.0,
    )

    assert state.measured is False
    assert state.is_toxic is False
    assert state.toxicity_reason == "unmeasured"
    assert math.isnan(state.ofi_threshold)
    assert math.isnan(state.drift_threshold_cents)

    for side in ("BUY", "SELL"):
        action = engine.evaluate_quote_action(side)
        assert action.action == "MAINTAIN"
        assert action.should_pull is False
        assert action.should_widen is False
        assert "unmeasured" in action.reason


def test_evaluate_quote_action_uses_state_thresholds():
    """Extremity is judged against the thresholds that produced the signal."""
    engine = _engine(
        ofi_threshold=1.0,
        drift_threshold_cents=0.01,
        pull_multiplier=1.5,
    )
    state = MicrostructureState(
        mid_price=0.50,
        micro_price=0.50,
        ofi_10_events=500.0,
        imbalance_ratio=0.0,
        is_toxic=True,
        ofi_threshold=10_000.0,
        drift_threshold_cents=100.0,
        measured=True,
        drift_cents=0.0,
        toxicity_reason="OFI",
    )

    action = engine.evaluate_quote_action("SELL", state=state)
    # 500 < 10_000 * 1.5 (state threshold) -> WIDEN, not the PULL the engine's
    # own 1.0 threshold would imply.
    assert action.action == "WIDEN"
    assert action.should_pull is False


# ---------------------------------------------------------------------------
# Dynamic Microstructure and Volatility Circuit Breaker Tests
# ---------------------------------------------------------------------------

def test_ofi_thin_vs_deep_pool():
    # 1. Thin pool: 100 shares depth each side
    thin_thresh = compute_dynamic_ofi_threshold(
        bid_size=100.0, ask_size=100.0, volume_24h=5_000.0, **OFI_FIT
    )
    assert 90.0 < thin_thresh < 120.0
    # In thin pool, an OFI of 850 is a huge toxic sweep (8x threshold!)
    assert 850.0 > thin_thresh * 7.0

    # 2. Deep pool: 50,000 shares depth each side
    deep_thresh = compute_dynamic_ofi_threshold(
        bid_size=50_000.0, ask_size=50_000.0, volume_24h=500_000.0, **OFI_FIT
    )
    assert deep_thresh >= 15_000.0
    # In deep pool, an OFI of 850 is harmless noise (< 10% of threshold)
    assert 850.0 < deep_thresh * 0.10


def test_ofi_asymmetric_depth_harmonic_mean():
    # Asymmetric book: Bid has 10,000 shares, Ask has only 50 shares
    # Harmonic mean should correctly reflect the fragile ask side: ~99.5 shares
    asym_thresh = compute_dynamic_ofi_threshold(bid_size=10_000.0, ask_size=50.0, **OFI_FIT)
    assert asym_thresh < 250.0


def test_drift_threshold_feasibility_on_different_spreads():
    # 1-cent spread (s = 1.0c) -> half spread = 0.50c
    drift_1c = compute_dynamic_drift_threshold_cents(
        mid_price=0.50, spread_cents=1.0, volatility=0.02
    )
    assert drift_1c < 0.50
    assert drift_1c >= 0.35  # ~ 0.80 * 0.50 = 0.40c

    # 5-cent spread (s = 5.0c) -> half spread = 2.50c
    drift_5c = compute_dynamic_drift_threshold_cents(
        mid_price=0.50, spread_cents=5.0, volatility=0.02
    )
    assert drift_5c > 1.50
    assert drift_5c < 2.50


def test_drift_threshold_state_space_scaling_p_1_minus_p():
    drift_50 = compute_dynamic_drift_threshold_cents(
        mid_price=0.50, spread_cents=2.0, volatility=0.02
    )
    drift_05 = compute_dynamic_drift_threshold_cents(
        mid_price=0.05, spread_cents=2.0, volatility=0.02
    )
    # Tail contract threshold MUST be significantly tighter than 50-cent contract
    assert drift_05 < drift_50 * 0.50


def test_drift_threshold_responds_to_volatility():
    """The volatility input must move the threshold, not be a no-op."""
    low = compute_dynamic_drift_threshold_cents(
        mid_price=0.50, spread_cents=4.0, volatility=0.001
    )
    reference = compute_dynamic_drift_threshold_cents(
        mid_price=0.50, spread_cents=4.0, volatility=0.02
    )
    high = compute_dynamic_drift_threshold_cents(
        mid_price=0.50, spread_cents=4.0, volatility=0.64
    )

    assert low < reference < high
    assert low != high


def test_drift_threshold_requires_volatility():
    """Volatility has no default: omitting it is a TypeError, not a constant."""
    with pytest.raises(TypeError):
        compute_dynamic_drift_threshold_cents(mid_price=0.50, spread_cents=1.0)


def test_volatility_breaker_jump_scaling_by_price_level():
    from polymarketbot.strategy.reward.microstructure import (
        compute_dynamic_volatility_breaker_threshold,
    )

    jump_50 = compute_dynamic_volatility_breaker_threshold(mid_price=0.50, spread_cents=1.0)
    jump_05 = compute_dynamic_volatility_breaker_threshold(mid_price=0.05, spread_cents=1.0)
    # 5c contract must trigger breaker on a tighter absolute jump than 50c contract
    assert jump_05 < jump_50


def test_cooldown_duration_fast_vs_macro_market():
    from polymarketbot.strategy.reward.microstructure import (
        compute_dynamic_cooldown_duration,
    )

    # 15-minute market: remaining seconds = 900s
    # Strictly capped <= 5% of lifetime (45s max)
    cooldown_15m = compute_dynamic_cooldown_duration(
        jump_cents_observed=2.5,
        breaker_threshold_cents=2.0,
        time_to_resolution_seconds=900.0,
    )
    assert cooldown_15m <= 45.0
    assert cooldown_15m >= 15.0

    # 6-month macro market: remaining seconds = 15,552,000s
    cooldown_macro = compute_dynamic_cooldown_duration(
        jump_cents_observed=5.0,
        breaker_threshold_cents=2.0,
        time_to_resolution_seconds=15_552_000.0,
    )
    assert cooldown_macro > 300.0


def test_cooldown_severity_scaling():
    from polymarketbot.strategy.reward.microstructure import (
        compute_dynamic_cooldown_duration,
    )

    cd_marginal = compute_dynamic_cooldown_duration(
        jump_cents_observed=2.1,
        breaker_threshold_cents=2.0,
        time_to_resolution_seconds=86_400.0,
    )
    cd_catastrophic = compute_dynamic_cooldown_duration(
        jump_cents_observed=10.0,
        breaker_threshold_cents=2.0,
        time_to_resolution_seconds=86_400.0,
    )
    assert cd_catastrophic > cd_marginal * 3.0




def test_tape_ofi_matches_engine_arithmetic():
    """The fitter's OFI must be the engine's own event arithmetic."""
    import numpy as np

    from polymarketbot.tools.fit_risk_calibration import _ofi_events

    states = {
        "ts": np.array([1, 2, 3, 4], dtype=np.int64),
        "asset": np.array(["a"] * 4, dtype=object),
        "bid": np.array([0.40, 0.41, 0.40, 0.40]),
        "qb": np.array([100.0, 50.0, 30.0, 30.0]),
        "ask": np.array([0.42, 0.42, 0.42, 0.43]),
        "qa": np.array([80.0, 80.0, 100.0, 60.0]),
    }
    events = _ofi_events(states)
    # The first state has no previous touch, so its event is unmeasured.
    # bid up: dQb = +50, ask unchanged: dQa = 0 -> +50
    # bid down: dQb = -50, ask depth +20: dQa = +20 -> -70
    # bid unchanged: 0, ask up: dQa = -100 -> +100
    assert math.isnan(events["ofi"][0])
    assert list(events["ofi"][1:]) == [50.0, -70.0, 100.0]

    engine = _engine()
    engine.update(0.40, 100.0, 0.42, 80.0, timestamp=1.0)
    state = engine.update(0.41, 50.0, 0.42, 80.0, timestamp=2.0)
    assert state.ofi_10_events == 50.0


def test_fit_ofi_recovers_synthetic_trigger_coefficients():
    """The fit must invert the logistic trigger it is asked to measure."""
    import numpy as np
    import pandas as pd

    from polymarketbot.tools.fit_risk_calibration import fit_ofi

    rng = np.random.default_rng(7)
    n = 60_000
    volume = np.exp(rng.uniform(np.log(100.0), np.log(1_000_000.0), n))
    depth = np.exp(rng.uniform(np.log(10.0), np.log(5_000.0), n))
    sign = rng.choice([-1.0, 1.0], n)
    ofi = sign * rng.uniform(1.0, 5_000.0, n)
    tick = np.where(rng.random(n) < 0.5, 0.001, 0.01)
    v_ref = float(np.exp(np.mean(np.log(volume))))
    beta = 0.25
    a, b, c = -1.2, 0.35, -0.0875  # beta = -c/b
    x = np.abs(ofi) / depth
    logit = a + b * np.log(x) + c * np.log(volume)
    probability = 1.0 / (1.0 + np.exp(-logit))
    hit = rng.random(n) < probability
    move = np.where(hit, sign * tick, 0.0)
    frame = pd.DataFrame(
        {
            "asset": [f"t{i}" for i in range(n)],
            "tick": tick,
            "d_harm": depth,
            "ofi_sum": ofi,
            "mid0": 0.5,
            "mid1": 0.5 + move,
            "volume_24h": volume,
        }
    )
    fit = fit_ofi(frame)
    expected_depth = float(np.exp(-a / b + beta * np.log(v_ref)))
    assert fit["identified"] is True
    assert fit["beta_vol"] == pytest.approx(beta, rel=0.1)
    assert fit["c_depth"] == pytest.approx(expected_depth, rel=0.1)
    assert fit["v_ref"] == pytest.approx(v_ref, rel=1e-9)
    low, high = fit["c_depth_ci"]
    assert low < expected_depth < high
    assert fit["trigger_pseudo_r2"] > 0.0


def test_standardised_quantile_is_the_measured_quantile():
    import numpy as np

    from polymarketbot.tools.fit_risk_calibration import standardised_quantile

    values = np.arange(1, 101, dtype=np.float64)
    assert standardised_quantile(values, 0.05) == pytest.approx(
        float(np.quantile(values, 0.95))
    )
    assert standardised_quantile([], 0.05) is None


def test_streak_threshold_is_the_measured_run_length():
    from polymarketbot.tools.fit_risk_calibration import streak_analysis

    # Ninety fills for one token in (loss, loss, win) triplets.  Every fill
    # after exactly one loss is itself a loss, so the conditional mean turns
    # negative at a run length of one, with the minimum sample behind it.
    records = []
    for triplet in range(30):
        for offset, net in enumerate((-0.02, -0.03, 0.05)):
            records.append(
                {
                    "asset": "a",
                    "ts_ns": float(triplet * 3 + offset),
                    "tick": 0.01,
                    "capture": 0.01,
                    "markout": 0.01 - net,
                }
            )
    result = streak_analysis(records)
    assert result["fills"] == 90
    by_length = {row["streak"]: row for row in result["run_length"]}
    assert by_length[1]["n"] == 30
    assert by_length[1]["mean_fill_net"] < 0.0
    assert result["threshold"] == 1


def test_streak_threshold_refuses_a_thin_sample():
    from polymarketbot.tools.fit_risk_calibration import streak_analysis

    records = [
        {
            "asset": "a",
            "ts_ns": float(i),
            "tick": 0.01,
            "capture": 0.0,
            "markout": 0.02,
        }
        for i in range(5)
    ]
    assert streak_analysis(records)["threshold"] is None


def test_risk_calibration_artifact_loads_through_the_live_loader(tmp_path):
    import json

    from polymarketbot.live.base import load_risk_calibration

    path = tmp_path / "risk_calibration.json"
    path.write_text(
        json.dumps(
            {
                "window": {"start": "2026-11-06T05:00:00Z", "end": "2026-11-06T06:00:00Z"},
                "sample_size": {"windows": 10},
                "ofi": {
                    "c_depth": 0.4,
                    "z_score": 2.37,
                    "v_ref": 1_200.0,
                    "beta_vol": 0.12,
                    "pull_multiplier": 2.0,
                    "widen_offset_multiplier": 1.0,
                    "consecutive_fills_threshold": 3,
                },
            }
        ),
        encoding="utf-8",
    )
    calibration = load_risk_calibration(path)
    assert calibration is not None
    engine = calibration.flow_factory()()
    assert engine.c_depth == pytest.approx(0.4)
    assert engine.z_score == pytest.approx(2.37)
    assert engine.v_ref == pytest.approx(1_200.0)
    assert engine.beta_vol == pytest.approx(0.12)
    assert engine.pull_multiplier == pytest.approx(2.0)
    assert engine.widen_offset_multiplier == pytest.approx(1.0)
    assert calibration.consecutive_fills_threshold == 3
