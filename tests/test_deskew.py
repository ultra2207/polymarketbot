"""Unit tests for the Dynamic Inventory De-Skewing Optimizer."""

import pytest
from polymarketbot.rewards.deskew import DeSkewPathway, DynamicDeSkewOptimizer


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
