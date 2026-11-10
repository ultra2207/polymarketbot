"""Unit tests for the Dynamic Inventory De-Skewing Optimizer."""

import pytest
from polymarketbot.strategy.reward.deskew import DeSkewPathway, DynamicDeSkewOptimizer


def test_neutrality_zone_returns_hold():
    optimizer = DynamicDeSkewOptimizer(q_soft_threshold=15.0)
    decision = optimizer.evaluate_skew(
        shares_yes=10.0,
        shares_no=15.0,
        best_bid_yes=0.30,
        best_ask_yes=0.31,
        best_bid_no=0.69,
        best_ask_no=0.70,
    )
    assert decision.pathway == DeSkewPathway.HOLD_STABLE
    assert decision.urgency_level == "LOW"


def test_tier1_passive_skew_long_no():
    # Long 75 NO, 10 YES -> q = -65. Below hard threshold, no toxicity
    optimizer = DynamicDeSkewOptimizer(q_soft_threshold=15.0, q_hard_threshold=150.0)
    decision = optimizer.evaluate_skew(
        shares_yes=10.0,
        shares_no=75.0,
        best_bid_yes=0.30,
        best_ask_yes=0.31,
        best_bid_no=0.69,
        best_ask_no=0.70,
        rolling_ofi=100.0,
        micro_price_drift_cents=0.1,
    )
    assert decision.pathway == DeSkewPathway.PASSIVE_FADE
    assert decision.target_token == "YES"
    assert decision.action_side == "BUY"
    assert decision.order_type == "LIMIT_MAKER"
    # Tightened YES bid to complete pairs organically
    assert decision.target_price >= 0.30


def test_tier2_emergency_active_skew_when_exceeding_hard_cap():
    optimizer = DynamicDeSkewOptimizer(q_hard_threshold=100.0)
    # Long 200 NO, 10 YES -> q = -190 (exceeds 100)
    decision = optimizer.evaluate_skew(
        shares_yes=10.0,
        shares_no=200.0,
        best_bid_yes=0.30,
        best_ask_yes=0.31,
        best_bid_no=0.68,
        best_ask_no=0.70,
    )
    assert decision.pathway in (DeSkewPathway.SYNTHETIC_MERGE, DeSkewPathway.DIRECT_CROSS)
    assert decision.order_type == "MARKET_TAKER"
    assert decision.urgency_level == "CRITICAL_PANIC"


def test_synthetic_complete_set_parity_arbitrage():
    optimizer = DynamicDeSkewOptimizer(q_hard_threshold=50.0, gas_cost_merge_usd=0.015)
    # Market dislocation: YES ask is cheap at 0.30, NO bid is depressed at 0.67
    # 1.00 - 0.30 = 0.70 > 0.67! Synthetic merge gives 70c instead of 67c!
    decision = optimizer.evaluate_skew(
        shares_yes=0.0,
        shares_no=100.0,
        best_bid_yes=0.29,
        best_ask_yes=0.30,
        best_bid_no=0.67,
        best_ask_no=0.71,
    )
    assert decision.pathway == DeSkewPathway.SYNTHETIC_MERGE
    assert decision.target_token == "YES"
    assert decision.action_side == "BUY"
    assert decision.order_type == "MARKET_TAKER"
    assert decision.synthetic_savings_cents > 2.0


def test_toxicity_sentinel_triggers_active_liquidation():
    optimizer = DynamicDeSkewOptimizer(
        q_soft_threshold=15.0,
        q_hard_threshold=150.0,
        drift_toxicity_cents=1.5,
    )
    # Imbalance is only 40 shares, but price drifted 2.0c in 60s!
    decision = optimizer.evaluate_skew(
        shares_yes=50.0,
        shares_no=10.0,
        best_bid_yes=0.48,
        best_ask_yes=0.50,
        best_bid_no=0.50,
        best_ask_no=0.52,
        micro_price_drift_cents=2.0,  # Toxic drift > 1.5c
    )
    # Must immediately trigger active liquidation, bypassing passive skew
    assert decision.pathway in (DeSkewPathway.SYNTHETIC_MERGE, DeSkewPathway.DIRECT_CROSS)
    assert decision.order_type == "MARKET_TAKER"


def test_neutral_inventory_with_toxic_buying_ofi_triggers_passive_fade():
    """Neutral inventory with toxic OFI (+900) triggers passive fade rather than hold stable."""
    optimizer = DynamicDeSkewOptimizer(
        q_soft_threshold=15.0,
        ofi_toxicity_threshold=850.0,
    )
    # Neutral inventory: net_q = 0 shares (< q_soft = 15.0)
    # But toxic buying OFI = +900 > 850
    decision = optimizer.evaluate_skew(
        shares_yes=10.0,
        shares_no=10.0,
        best_bid_yes=0.49,
        best_ask_yes=0.51,
        best_bid_no=0.49,
        best_ask_no=0.51,
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
    optimizer = DynamicDeSkewOptimizer(
        q_soft_threshold=15.0,
        ofi_toxicity_threshold=850.0,
    )
    # Neutral inventory: net_q = 0 shares (< q_soft = 15.0)
    # But toxic selling OFI = -900 < -850
    decision = optimizer.evaluate_skew(
        shares_yes=10.0,
        shares_no=10.0,
        best_bid_yes=0.49,
        best_ask_yes=0.51,
        best_bid_no=0.49,
        best_ask_no=0.51,
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
    optimizer = DynamicDeSkewOptimizer(
        q_soft_threshold=15.0,
        drift_toxicity_cents=1.5,
    )
    # Upward drift > 1.5c threatens asks -> SELL
    decision_up = optimizer.evaluate_skew(
        shares_yes=5.0,
        shares_no=5.0,
        best_bid_yes=0.49,
        best_ask_yes=0.51,
        best_bid_no=0.49,
        best_ask_no=0.51,
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
        rolling_ofi=0.0,
        micro_price_drift_cents=-2.0,
    )
    assert decision_down.pathway == DeSkewPathway.PASSIVE_FADE
    assert decision_down.urgency_level == "HIGH"
    assert decision_down.action_side == "BUY"


class TestDynamicThresholdCalculator:
    """Tests for DynamicThresholdCalculator across capital tiers and market regimes."""

    def test_starter_portfolio_thresholds(self):
        from polymarketbot.strategy.reward.deskew import DynamicThresholdCalculator

        # $70 starter, quote 20 shares, $17.50 reserve
        calib = DynamicThresholdCalculator.calibrate_all(
            quote_size=20.0,
            bankroll=70.0,
            cash_reserve=17.50,
            mid_price=0.50,
            time_to_resolution_days=7.0,
        )
        assert calib.q_soft >= 15.0
        assert calib.q_hard >= 35.0
        assert calib.q_hard > calib.q_soft
        assert calib.auto_merge_threshold >= 10.0

    def test_whale_portfolio_thresholds(self):
        from polymarketbot.strategy.reward.deskew import DynamicThresholdCalculator

        # $10,000 whale, quote 400 shares, $2,500 reserve
        calib = DynamicThresholdCalculator.calibrate_all(
            quote_size=400.0,
            bankroll=10_000.0,
            cash_reserve=2500.0,
            mid_price=0.50,
            time_to_resolution_days=7.0,
        )
        assert calib.q_soft >= 350.0
        assert calib.q_hard >= 1000.0
        assert calib.auto_merge_threshold >= 100.0

    def test_holding_time_expiry_adaptation(self):
        from polymarketbot.strategy.reward.deskew import DynamicThresholdCalculator

        # 5-minute market (300s remaining): holding time must be tightly bounded
        t_5m = DynamicThresholdCalculator.compute_holding_time(time_to_expiry_seconds=300.0)
        assert t_5m <= 50.0
        assert t_5m >= 15.0

        # 90-day election (7.7m seconds): holding time allows healthy patient fill
        t_90d = DynamicThresholdCalculator.compute_holding_time(time_to_expiry_seconds=7_776_000.0)
        assert t_90d >= 300.0


