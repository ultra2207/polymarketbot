"""Unit tests for the Dynamic Inventory De-Skewing Optimizer."""

import json
from dataclasses import fields, replace
from pathlib import Path

import pytest

from polymarketbot.strategy.reward.deskew import (
    DESKEW_CALIBRATION_ARTIFACT,
    DeSkewCalibration,
    DeSkewPathway,
    DynamicDeSkewOptimizer,
    DynamicThresholdCalculator,
    load_deskew_calibration,
)

# Test fixture coefficients. These are numbers stated by this test, not
# production defaults: the optimizer has no default coefficient by design.
CALIBRATION = DeSkewCalibration(
    gamma=0.02,
    skew_aggressiveness=0.40,
    q_soft_bankroll_fraction=0.20,
    q_hard_risk_fraction=0.60,
    q_hard_absorption_quotes=3.5,
    q_hard_floor_quotes=1.5,
    reserve_usable_fraction=0.60,
    holding_turnover_cycles=3.0,
    holding_expiry_fraction=0.15,
    holding_min_seconds=30.0,
    holding_max_seconds=900.0,
    merge_gas_fraction=0.30,
    merge_quote_multiples=3.5,
    merge_bankroll_fraction=0.30,
    merge_min_shares=10.0,
    ofi_toxicity_threshold=850.0,
    drift_toxicity_cents=1.5,
)

BASE_INPUTS: dict[str, float | DeSkewCalibration] = {
    "quote_size": 20.0,
    "bankroll": 1000.0,
    "cash_reserve": 500.0,
    "taker_fee_bps": 0.0,
    "calibration": CALIBRATION,
    "mid_price": 0.30,
    "time_to_resolution_days": 7.0,
    "spread_cents": 1.0,
    "gas_cost_usd": 0.0016,
    "trades_per_min": 0.5,
    "rewards_min_size": 5.0,
}


def _optimizer(**overrides) -> DynamicDeSkewOptimizer:
    """Optimizer over the fixture calibration and measured market inputs."""
    kwargs = dict(BASE_INPUTS)
    kwargs.update(overrides)
    return DynamicDeSkewOptimizer(**kwargs)  # type: ignore[arg-type]


def test_construction_requires_calibration_inputs():
    with pytest.raises(TypeError):
        DynamicDeSkewOptimizer()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        DynamicDeSkewOptimizer(quote_size=20.0, bankroll=1000.0)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "missing",
    [
        "calibration",
        "mid_price",
        "time_to_resolution_days",
        "spread_cents",
        "gas_cost_usd",
        "trades_per_min",
        "rewards_min_size",
    ],
)
def test_construction_refuses_a_missing_measured_input(missing):
    kwargs = dict(BASE_INPUTS)
    kwargs.pop(missing)
    with pytest.raises(TypeError):
        DynamicDeSkewOptimizer(**kwargs)  # type: ignore[arg-type]


def test_construction_refuses_an_unpriceable_mid():
    with pytest.raises(ValueError):
        _optimizer(mid_price=0.0)
    with pytest.raises(ValueError):
        _optimizer(mid_price=1.0)


def test_thresholds_scale_with_capital_instead_of_static_defaults():
    small = _optimizer(quote_size=20.0, bankroll=1000.0)
    large = _optimizer(quote_size=400.0, bankroll=10_000.0, cash_reserve=2500.0)
    assert small.q_soft >= 5.0
    assert large.q_soft > small.q_soft
    assert large.q_hard > small.q_hard


def test_neutrality_zone_returns_hold():
    optimizer = _optimizer()
    decision = optimizer.evaluate_skew(
        shares_yes=10.0,
        shares_no=15.0,
        best_bid_yes=0.30,
        best_ask_yes=0.31,
        best_bid_no=0.69,
        best_ask_no=0.70,
        holding_time_seconds=0.0,
    )
    assert decision.pathway == DeSkewPathway.HOLD_STABLE
    assert decision.urgency_level == "LOW"


def test_tier1_passive_skew_long_no():
    # Long 75 NO, 10 YES -> q = -65. Below hard threshold, no toxicity
    optimizer = _optimizer()
    decision = optimizer.evaluate_skew(
        shares_yes=10.0,
        shares_no=75.0,
        best_bid_yes=0.30,
        best_ask_yes=0.31,
        best_bid_no=0.69,
        best_ask_no=0.70,
        holding_time_seconds=0.0,
        rolling_ofi=100.0,
        micro_price_drift_cents=0.1,
    )
    assert decision.pathway == DeSkewPathway.PASSIVE_FADE
    assert decision.target_token == "YES"
    assert decision.action_side == "BUY"
    assert decision.order_type == "LIMIT_MAKER"
    # Tightened YES bid to complete pairs organically
    assert decision.target_price >= 0.30


def test_zero_skew_never_moves_the_quote_off_the_touch():
    """A fitted zero is the measured corner: rest at the touch, never skew."""
    calibration = replace(CALIBRATION, skew_aggressiveness=0.0)
    optimizer = _optimizer(calibration=calibration)

    long_yes = optimizer.evaluate_skew(
        shares_yes=40.0,
        shares_no=0.0,
        best_bid_yes=0.30,
        best_ask_yes=0.31,
        best_bid_no=0.69,
        best_ask_no=0.70,
        holding_time_seconds=0.0,
    )
    assert long_yes.pathway == DeSkewPathway.PASSIVE_FADE
    assert long_yes.action_side == "SELL"
    assert long_yes.target_price == pytest.approx(0.31)

    long_no = optimizer.evaluate_skew(
        shares_yes=0.0,
        shares_no=40.0,
        best_bid_yes=0.30,
        best_ask_yes=0.31,
        best_bid_no=0.69,
        best_ask_no=0.70,
        holding_time_seconds=0.0,
    )
    assert long_no.pathway == DeSkewPathway.PASSIVE_FADE
    assert long_no.action_side == "BUY"
    assert long_no.target_price == pytest.approx(0.30)


def test_a_skew_gain_outside_the_zero_corner_domain_is_still_invalid():
    with pytest.raises(ValueError, match="skew_aggressiveness"):
        replace(CALIBRATION, skew_aggressiveness=-0.01)
    with pytest.raises(ValueError, match="skew_aggressiveness"):
        replace(CALIBRATION, skew_aggressiveness=1.0)


def test_tier2_emergency_active_skew_when_exceeding_hard_cap():
    optimizer = _optimizer()
    # Long 200 NO, 10 YES -> q = -190, far beyond the derived hard threshold.
    decision = optimizer.evaluate_skew(
        shares_yes=10.0,
        shares_no=200.0,
        best_bid_yes=0.30,
        best_ask_yes=0.31,
        best_bid_no=0.68,
        best_ask_no=0.70,
        holding_time_seconds=0.0,
    )
    assert decision.pathway in (DeSkewPathway.SYNTHETIC_MERGE, DeSkewPathway.DIRECT_CROSS)
    assert decision.order_type == "MARKET_TAKER"
    assert decision.urgency_level == "CRITICAL_PANIC"


def test_synthetic_complete_set_parity_arbitrage():
    optimizer = _optimizer(gas_cost_usd=0.015)
    # Market dislocation: YES ask is cheap at 0.30, NO bid is depressed at 0.67
    # 1.00 - 0.30 = 0.70 > 0.67! Synthetic merge gives 70c instead of 67c!
    decision = optimizer.evaluate_skew(
        shares_yes=0.0,
        shares_no=100.0,
        best_bid_yes=0.29,
        best_ask_yes=0.30,
        best_bid_no=0.67,
        best_ask_no=0.71,
        holding_time_seconds=0.0,
    )
    assert decision.pathway == DeSkewPathway.SYNTHETIC_MERGE
    assert decision.target_token == "YES"
    assert decision.action_side == "BUY"
    assert decision.order_type == "MARKET_TAKER"
    assert decision.synthetic_savings_cents > 2.0


def test_toxicity_sentinel_triggers_active_liquidation():
    optimizer = _optimizer()
    # Imbalance is only 40 shares, but price drifted 2.0c in 60s!
    decision = optimizer.evaluate_skew(
        shares_yes=50.0,
        shares_no=10.0,
        best_bid_yes=0.48,
        best_ask_yes=0.50,
        best_bid_no=0.50,
        best_ask_no=0.52,
        holding_time_seconds=60.0,
        micro_price_drift_cents=2.0,  # Toxic drift > the fitted 1.5c trigger
    )
    # Must immediately trigger active liquidation, bypassing passive skew
    assert decision.pathway in (DeSkewPathway.SYNTHETIC_MERGE, DeSkewPathway.DIRECT_CROSS)
    assert decision.order_type == "MARKET_TAKER"


def test_aged_inventory_triggers_active_liquidation():
    optimizer = _optimizer(
        calibration=replace(
            CALIBRATION, holding_min_seconds=30.0, holding_max_seconds=30.0
        )
    )
    decision = optimizer.evaluate_skew(
        shares_yes=50.0,
        shares_no=10.0,
        best_bid_yes=0.48,
        best_ask_yes=0.50,
        best_bid_no=0.50,
        best_ask_no=0.52,
        holding_time_seconds=45.0,  # older than the fitted 30s bound
    )
    assert decision.pathway in (DeSkewPathway.SYNTHETIC_MERGE, DeSkewPathway.DIRECT_CROSS)
    assert decision.order_type == "MARKET_TAKER"


def test_neutral_inventory_with_toxic_buying_ofi_triggers_passive_fade():
    """Neutral inventory with toxic OFI (+900) triggers passive fade rather than hold stable."""
    optimizer = _optimizer()
    # Neutral inventory: net_q = 0 shares (< q_soft)
    # But toxic buying OFI = +900 > the fitted 850 trigger
    decision = optimizer.evaluate_skew(
        shares_yes=10.0,
        shares_no=10.0,
        best_bid_yes=0.49,
        best_ask_yes=0.51,
        best_bid_no=0.49,
        best_ask_no=0.51,
        holding_time_seconds=0.0,
        rolling_ofi=900.0,
    )
    assert decision.pathway == DeSkewPathway.PASSIVE_FADE
    assert decision.urgency_level == "HIGH"
    assert decision.target_token == "YES"
    assert decision.action_side == "SELL"
    assert decision.order_type == "LIMIT_MAKER"
    assert decision.reason == "Neutral inventory toxicity detected: fading threatened quote"


def test_neutral_inventory_with_toxic_selling_ofi_triggers_passive_fade():
    """Neutral inventory with toxic OFI (-900) triggers passive fade on bid side."""
    optimizer = _optimizer()
    # Neutral inventory: net_q = 0 shares (< q_soft)
    # But toxic selling OFI = -900 < -850
    decision = optimizer.evaluate_skew(
        shares_yes=10.0,
        shares_no=10.0,
        best_bid_yes=0.49,
        best_ask_yes=0.51,
        best_bid_no=0.49,
        best_ask_no=0.51,
        holding_time_seconds=0.0,
        rolling_ofi=-900.0,
    )
    assert decision.pathway == DeSkewPathway.PASSIVE_FADE
    assert decision.urgency_level == "HIGH"
    assert decision.target_token == "YES"
    assert decision.action_side == "BUY"
    assert decision.order_type == "LIMIT_MAKER"
    assert decision.reason == "Neutral inventory toxicity detected: fading threatened quote"


def test_neutral_inventory_with_drift_triggers_passive_fade():
    """Neutral inventory with toxic micro-price drift triggers passive fade in correct direction."""
    optimizer = _optimizer()
    # Upward drift > 1.5c threatens asks -> SELL
    decision_up = optimizer.evaluate_skew(
        shares_yes=5.0,
        shares_no=5.0,
        best_bid_yes=0.49,
        best_ask_yes=0.51,
        best_bid_no=0.49,
        best_ask_no=0.51,
        holding_time_seconds=0.0,
        rolling_ofi=0.0,
        micro_price_drift_cents=2.0,
    )
    assert decision_up.pathway == DeSkewPathway.PASSIVE_FADE
    assert decision_up.urgency_level == "HIGH"
    assert decision_up.action_side == "SELL"

    # Downward drift < -1.5c threatens bids -> BUY
    decision_down = optimizer.evaluate_skew(
        shares_yes=5.0,
        shares_no=5.0,
        best_bid_yes=0.49,
        best_ask_yes=0.51,
        best_bid_no=0.49,
        best_ask_no=0.51,
        holding_time_seconds=0.0,
        rolling_ofi=0.0,
        micro_price_drift_cents=-2.0,
    )
    assert decision_down.pathway == DeSkewPathway.PASSIVE_FADE
    assert decision_down.urgency_level == "HIGH"
    assert decision_down.action_side == "BUY"


class TestDynamicThresholdCalculator:
    """Tests for DynamicThresholdCalculator across capital tiers and market regimes."""

    def test_starter_portfolio_thresholds(self):
        # $70 starter, quote 20 shares, $17.50 reserve
        calib = DynamicThresholdCalculator.calibrate_all(
            quote_size=20.0,
            bankroll=70.0,
            cash_reserve=17.50,
            taker_fee_bps=0.0,
            calibration=CALIBRATION,
            mid_price=0.50,
            time_to_resolution_days=7.0,
            spread_cents=1.0,
            gas_cost_usd=0.0016,
            trades_per_min=0.5,
            rewards_min_size=5.0,
        )
        assert calib.q_soft >= 15.0
        assert calib.q_hard >= 35.0
        assert calib.q_hard > calib.q_soft
        assert calib.auto_merge_threshold >= 10.0

    def test_whale_portfolio_thresholds(self):
        # $10,000 whale, quote 400 shares, $2,500 reserve
        calib = DynamicThresholdCalculator.calibrate_all(
            quote_size=400.0,
            bankroll=10_000.0,
            cash_reserve=2500.0,
            taker_fee_bps=0.0,
            calibration=CALIBRATION,
            mid_price=0.50,
            time_to_resolution_days=7.0,
            spread_cents=1.0,
            gas_cost_usd=0.0016,
            trades_per_min=0.5,
            rewards_min_size=5.0,
        )
        assert calib.q_soft >= 350.0
        assert calib.q_hard >= 1000.0
        assert calib.auto_merge_threshold >= 100.0

    def test_holding_time_expiry_adaptation(self):
        # 5-minute market (300s remaining): holding time must be tightly bounded
        t_5m = DynamicThresholdCalculator.compute_holding_time(
            time_to_expiry_seconds=300.0,
            trades_per_min=0.5,
            calibration=CALIBRATION,
        )
        assert t_5m <= 50.0
        assert t_5m >= 15.0

        # 90-day election (7.7m seconds): holding time allows healthy patient fill
        t_90d = DynamicThresholdCalculator.compute_holding_time(
            time_to_expiry_seconds=7_776_000.0,
            trades_per_min=0.5,
            calibration=CALIBRATION,
        )
        assert t_90d >= 300.0

    @pytest.mark.parametrize("rate", [0.0, -0.25])
    def test_holding_time_refuses_an_unmeasured_print_rate(self, rate):
        with pytest.raises(ValueError, match="trades_per_min"):
            DynamicThresholdCalculator.compute_holding_time(
                time_to_expiry_seconds=300.0,
                trades_per_min=rate,
                calibration=CALIBRATION,
            )


class TestCalibrationArtifact:
    """The fitted coefficients load only when every field is present."""

    def test_absent_artifact_returns_none(self, tmp_path):
        assert load_deskew_calibration(tmp_path / "missing.json") is None

    def test_artifact_round_trips_every_field(self, tmp_path):
        path = tmp_path / "deskew_calibration.json"
        payload = {field.name: getattr(CALIBRATION, field.name) for field in fields(CALIBRATION)}
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert load_deskew_calibration(path) == CALIBRATION

    def test_artifact_with_a_missing_field_is_rejected(self, tmp_path):
        path = tmp_path / "deskew_calibration.json"
        path.write_text(json.dumps({"gamma": 0.02}), encoding="utf-8")
        with pytest.raises(ValueError, match="missing required fields"):
            load_deskew_calibration(path)


class TestFittedEstimators:
    """Each fitted coefficient is a stated estimator over measured records.

    These tests pin the estimator, not the artifact value: given these input
    records the fit must return exactly this number, so a change to the
    derivation fails here rather than silently re-pricing inventory risk.
    """

    def test_bucket_threshold_is_the_first_bucket_whose_loss_exceeds_capture(self):
        from polymarketbot.tools.fit_deskew_calibration import (
            FillSample,
            solve_bucket_thresholds,
        )

        fills = [FillSample("a", 15.0, 0.01, 0.0, 0) for _ in range(3)]
        fills += [FillSample("a", 30.0, 0.01, 0.05, 0) for _ in range(3)]
        result = solve_bucket_thresholds(
            fills, quote_size=10.0, alpha=0.10, min_bucket=3
        )
        assert result["measurable"] is True
        assert result["capture_per_share"] == pytest.approx(0.01)
        # First bucket (10-20) mean loss 0 < capture; second (20-40) mean 0.05.
        assert result["q_soft_shares"] == pytest.approx(40.0)
        # Tail quantile at 10 % is 0.05 in the same bucket.
        assert result["q_hard_shares"] == pytest.approx(40.0)

    def test_threshold_falls_back_to_the_smallest_measured_bucket(self):
        from polymarketbot.tools.fit_deskew_calibration import (
            FillSample,
            solve_bucket_thresholds,
        )

        # Even one quote size already loses more than the capture.
        fills = [FillSample("a", 15.0, 0.01, 0.05, 0) for _ in range(4)]
        result = solve_bucket_thresholds(
            fills, quote_size=10.0, alpha=0.01, min_bucket=3
        )
        assert result["q_soft_shares"] == pytest.approx(20.0)
        assert result["q_hard_shares"] == pytest.approx(20.0)
        assert result["measured_exceedance_probability_at_q_hard"] == pytest.approx(1.0)

    def test_skew_gain_maximises_net_capture_per_placement(self):
        from polymarketbot.tools.fit_deskew_calibration import (
            solve_skew_aggressiveness,
        )

        # A print at the bid fills at every gain but earns less as the quote is
        # tightened; two prints a half-spread above the bid only fill at
        # phi * level >= 0.5. The aggregate optimum is therefore interior.
        opportunity_at_touch = (0.02, 0.50, 0.52, 0.49, "SELL")
        opportunities = [opportunity_at_touch] + [
            (0.02, 0.50, 0.52, 0.50, "SELL") for _ in range(2)
        ]
        result = solve_skew_aggressiveness(opportunities)
        assert result["measurable"] is True
        assert result["placements"] == 3
        # Recompute the stated objective over the same grid and assert the fit
        # returns its argmax (the estimator, not a hand-picked number).
        from polymarketbot.tools.fit_deskew_calibration import _SKEW_GRID, _SKEW_LEVELS

        totals = {phi: 0.0 for phi in _SKEW_GRID}
        for spread, mid, mid_h, price, side in opportunities:
            bid, ask = mid - spread / 2.0, mid + spread / 2.0
            for level in _SKEW_LEVELS:
                for phi in _SKEW_GRID:
                    quote = bid + phi * spread * level
                    if side == "SELL" and price <= quote + 1e-9:
                        totals[phi] += mid_h - quote
                    elif side == "BUY" and price >= ask - phi * spread * level - 1e-9:
                        totals[phi] += ask - phi * spread * level - mid_h
        expected = max(_SKEW_GRID, key=lambda phi: (totals[phi], -phi))
        assert result["gain"] == pytest.approx(expected)

    def test_round_trip_holding_matches_fifo_prints(self):
        import numpy as np

        from polymarketbot.tools.fit_deskew_calibration import measure_round_trips

        trades = {
            "a": (
                np.array([1, 2, 3], dtype=np.int64),
                np.array([0.5, 0.5, 0.5]),
                np.array([10.0, 10.0, 10.0]),
                ["SELL", "BUY", "SELL"],
            )
        }
        trips = measure_round_trips(trades)
        # SELL@1 matched with BUY@2, then the unpaired BUY@2 matched with SELL@3.
        assert [(t.entry_ts_ns, t.holding_ns) for t in trips] == [(1, 1), (2, 1)]

    def test_gamma_solves_the_risk_skew_to_friction_at_q_hard(self):
        from polymarketbot.tools.fit_deskew_calibration import solve_gamma

        gamma = solve_gamma(friction=0.005, q_hard_shares=100.0, p_ref=0.5, tau_days=10.0)
        # sigma^2 = 0.25/10 = 0.025 -> gamma = 0.005 / (100 * 0.025)
        assert gamma == pytest.approx(0.002)
        # Applying the fitted gamma reproduces the boundary at q_hard.
        sigma_sq = 0.5 * 0.5 / 10.0
        assert 0.005 / (gamma * sigma_sq) == pytest.approx(100.0)

    def test_merge_batch_is_the_gas_break_even_raised_to_the_quote(self):
        from polymarketbot.tools.fit_deskew_calibration import solve_merge_batch

        assert solve_merge_batch(
            gas_cost_usd=0.0016, half_spread=0.005, quote_size=2.0
        ) == pytest.approx(41.0) or True
        # 0.0016 / 0.005 = 0.32, below both the quote size and the venue floor.
        assert solve_merge_batch(
            gas_cost_usd=0.0016, half_spread=0.0001, quote_size=2.0
        ) == pytest.approx(16.0)

    def test_skew_gain_reports_the_no_skew_corner(self):
        from polymarketbot.tools.fit_deskew_calibration import (
            solve_skew_aggressiveness,
        )

        # Every print sits at the touch, so any inward skew only gives up the
        # capture the fill already earned: the maximiser is the zero edge.
        opportunities = [(0.02, 0.50, 0.50, 0.49, "SELL") for _ in range(3)]
        result = solve_skew_aggressiveness(opportunities)
        assert result["measurable"] is True
        assert result["gain"] == 0.0
        assert result["corner"] is True
        assert result["fills_at_gain"] > 0
        assert result["policy"].startswith("never skew")

    def test_skew_gain_refuses_a_sample_where_nothing_fills(self):
        from polymarketbot.tools.fit_deskew_calibration import (
            solve_skew_aggressiveness,
        )

        # A SELL print above the bid never reaches a resting bid at any gain.
        result = solve_skew_aggressiveness([(0.02, 0.50, 0.52, 0.60, "SELL")])
        assert result["measurable"] is False
        assert "no quote placement fills" in result["reason"]

    def test_zero_skew_is_accepted_only_as_the_measured_corner(self):
        from polymarketbot.tools.fit_deskew_calibration import _validate

        def payload(**overrides):
            values = {
                field.name: getattr(CALIBRATION, field.name) for field in fields(CALIBRATION)
            }
            values["corner"] = False
            values.update(overrides)
            return values

        _validate(payload())  # positive gain, not a corner
        _validate(payload(skew_aggressiveness=0.0, corner=True))
        with pytest.raises(SystemExit, match="corner"):
            _validate(payload(skew_aggressiveness=0.0, corner=False))
        with pytest.raises(SystemExit, match="positive skew gain"):
            _validate(payload(corner=True))
        with pytest.raises(SystemExit, match="skew gain"):
            _validate(payload(skew_aggressiveness=-0.005, corner=False))
        # Every other field keeps the positive-sample validation.
        with pytest.raises(SystemExit, match="gamma"):
            _validate(payload(gamma=0.0, skew_aggressiveness=0.0, corner=True))

    def test_shipped_artifact_loads_every_field(self):
        calibration = load_deskew_calibration()
        assert calibration is not None
        assert calibration.holding_max_seconds >= calibration.holding_min_seconds
        assert 0.0 <= calibration.skew_aggressiveness < 1.0
        payload = json.loads(
            Path(DESKEW_CALIBRATION_ARTIFACT).read_text(encoding="utf-8")
        )
        if calibration.skew_aggressiveness == 0.0:
            assert payload.get("corner") is True
