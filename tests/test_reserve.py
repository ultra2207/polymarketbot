"""The endogenous reserve is derived from a measured fit, never a constant."""

import inspect
import json
import math

import numpy as np
import pytest

from polymarketbot.strategy.reward import reserve as reserve_module
from polymarketbot.strategy.reward.reserve import (
    RESERVE_CALIBRATION_ARTIFACT,
    ReserveCalibration,
    calculate_endogenous_reserve,
    load_reserve_calibration,
)

CALIBRATION = ReserveCalibration(
    jump_shock_factor=0.20,
    diversification_weight=0.50,
    base_gas_actions=3.0,
    per_market_gas_actions=2.0,
    gas_floor_usd=0.01,
)


def test_calibration_requires_every_fitted_field():
    with pytest.raises(TypeError):
        ReserveCalibration(jump_shock_factor=0.2)  # type: ignore[call-arg]


def test_loader_returns_none_when_no_fit_exists(tmp_path):
    assert load_reserve_calibration(tmp_path / "missing.json") is None


def test_loader_rejects_a_partial_fit(tmp_path):
    path = tmp_path / "reserve_calibration.json"
    path.write_text(json.dumps({"jump_shock_factor": 0.2}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing required fields"):
        load_reserve_calibration(path)


def test_the_shipped_artifact_carries_every_field():
    calibration = load_reserve_calibration(RESERVE_CALIBRATION_ARTIFACT)
    assert calibration is not None
    assert calibration.diversification_weight > 0.0
    assert calibration.gas_floor_usd > 0.0
    assert calibration.base_gas_actions >= calibration.per_market_gas_actions


def test_reserve_is_the_measured_formula():
    allocations = [
        {"capital": 100.0, "mid": 0.5},
        {"capital": 100.0, "mid": 0.5},
    ]
    result = calculate_endogenous_reserve(
        allocations,
        1000.0,
        base_gas_buffer_usd=0.0,
        calibration=CALIBRATION,
    )
    # h_i = 50 each; sqrt(sum h^2) = 70.71, weighted by 0.5; tail = 50 * 0.2;
    # gas = 2 markets * (0.01 * 2 actions).
    expected = math.sqrt(2 * 50.0**2) * 0.50 + 50.0 * 0.20 + 2 * (0.01 * 2.0)
    assert result == pytest.approx(round(expected, 2))


def test_overrides_replace_the_fit_without_becoming_defaults():
    allocations = [
        {"capital": 100.0, "mid": 0.5},
        {"capital": 100.0, "mid": 0.5},
    ]
    jump_only = calculate_endogenous_reserve(
        allocations,
        1000.0,
        base_gas_buffer_usd=0.0,
        jump_shock_factor=1.0,
        calibration=CALIBRATION,
    )
    expected = math.sqrt(2 * 50.0**2) * 0.50 + 50.0 * 1.0 + 2 * 0.01 * 2.0
    assert jump_only == pytest.approx(round(expected, 2))


def test_a_missing_fit_refuses_to_size_the_reserve(monkeypatch):
    monkeypatch.setattr(reserve_module, "load_reserve_calibration", lambda *a, **k: None)
    with pytest.raises(ValueError, match="no reserve calibration"):
        calculate_endogenous_reserve([{"capital": 100.0, "mid": 0.5}], 1000.0)


def test_the_signature_defaults_are_not_numbers():
    signature = inspect.signature(calculate_endogenous_reserve)
    assert signature.parameters["jump_shock_factor"].default is None
    assert signature.parameters["diversification_weight"].default is None
    assert signature.parameters["calibration"].default is None


def test_jump_factor_is_the_stated_tail_of_measured_shocks():
    from polymarketbot.tools.fit_reserve import jump_shock_quantile

    shocks = np.array([0.01, -0.02, 0.05, -0.4])
    for probability in (0.01, 0.05, 0.25):
        assert jump_shock_quantile(shocks, probability) == pytest.approx(
            float(np.quantile(np.abs(shocks), 1.0 - probability))
        )


def test_diversification_weight_is_one_minus_measured_correlation():
    from polymarketbot.tools.fit_reserve import mean_pairwise_correlation

    perfect = mean_pairwise_correlation(
        {
            "a": {0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0, 4: 5.0},
            "b": {0: 2.0, 1: 4.0, 2: 6.0, 3: 8.0, 4: 10.0},
        },
        max_assets=10,
    )
    assert perfect["measurable"] is True
    assert perfect["mean_correlation"] == pytest.approx(1.0)
    assert 1.0 - perfect["mean_correlation"] == pytest.approx(0.0)

    opposite = mean_pairwise_correlation(
        {
            "a": {0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0, 4: 5.0},
            "b": {0: -1.0, 1: -2.0, 2: -3.0, 3: -4.0, 4: -5.0},
        },
        max_assets=10,
    )
    assert opposite["mean_correlation"] == pytest.approx(-1.0)
    assert 1.0 - opposite["mean_correlation"] == pytest.approx(2.0)
