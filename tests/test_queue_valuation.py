"""Tests for FIFO queue priority valuation, CTF gas model, and switching hurdle."""

from __future__ import annotations

import pytest

from polymarketbot.rewards.queue_valuation import (
    CapitalMigrationEngine,
    CTFGasProfile,
    LiquidationRoute,
    PolygonPoSEnvironment,
    QueuePriorityValuationModel,
    solve_inventory_liquidation_friction,
)


def test_polygon_gas_calculations() -> None:
    env = PolygonPoSEnvironment(base_fee_gwei=35.0, priority_fee_gwei=45.0, pol_usd_rate=0.35)
    assert env.total_gas_price_gwei == 80.0

    profile = CTFGasProfile()
    cost_usd = env.gas_units_to_usd(profile.gas_merge_direct_typical)
    # 78,000 * 80e-9 * 0.35 = 0.002184 USD
    assert 0.0020 <= cost_usd <= 0.0025


def test_queue_priority_valuation() -> None:
    model = QueuePriorityValuationModel()

    # Front of queue vs back of queue
    val_front = model.evaluate_queue_value(
        quote_size=100.0,
        queue_ahead=0.0,
        half_spread_cents=1.5,
        token_mid_price=0.50,
    )
    val_back = model.evaluate_queue_value(
        quote_size=100.0,
        queue_ahead=2500.0,
        half_spread_cents=1.5,
        token_mid_price=0.50,
    )

    # Front of queue must have strictly higher fill probability and value than back
    assert val_front.fill_probability > val_back.fill_probability
    assert val_front.net_queue_value_usd > val_back.net_queue_value_usd
    # Front-of-queue value is re-derived from the measured isotonic P(fill) for a
    # zero-ratio queue (data/derived/fill_model.json), not a hand-set threshold.
    p_front = model.calculate_fill_probability(0.0, 100.0)
    assert p_front < 1.0
    # Kyle's lambda is price change per 1000 shares, so a 100-share fill pays
    # lambda * 100 / 1000 dollars per share of adverse selection.
    expected_front = (
        100.0
        * (0.015 - model.kyle_lambda * 100.0 / 1000.0)
        * p_front
    )
    assert val_front.net_queue_value_usd == pytest.approx(expected_front, abs=1e-4)


def test_synthetic_liquidation_dominance() -> None:
    # Scenario 1: Direct bid is depressed (0.45) but opposite ask is tight (0.51)
    # Sum: 0.45 + 0.51 + Gas/q < 1.00 -> Synthetic completion should dominate!
    res_synth = solve_inventory_liquidation_friction(
        q=150.0,
        p_mid=0.50,
        p_bid=0.45,
        p_ask_opp=0.51,
        gas_merge_usd=0.015,
    )
    assert res_synth.route == LiquidationRoute.SYNTHETIC_MERGE
    assert res_synth.synthetic_savings_usd > 0.0
    assert res_synth.effective_exit_price > 0.45  # Better exit than direct bid

    # Scenario 2: Direct bid is tight (0.495) and opposite ask is wide (0.53)
    # Direct crossing should dominate!
    res_direct = solve_inventory_liquidation_friction(
        q=150.0,
        p_mid=0.50,
        p_bid=0.495,
        p_ask_opp=0.53,
        gas_merge_usd=0.015,
    )
    assert res_direct.route == LiquidationRoute.DIRECT_CROSS
    assert res_direct.optimal_friction_usd == pytest.approx(150.0 * (0.50 - 0.495))


def test_capital_migration_hurdle_evaluation() -> None:
    engine = CapitalMigrationEngine()

    cost = engine.compute_switching_cost(
        q_a=100.0,
        p_mid_a=0.50,
        p_bid_a=0.48,
        p_ask_opp_a=0.51,
        resting_bid_size=100.0,
        resting_ask_size=100.0,
        queue_ahead_bid=50.0,
        queue_ahead_ask=50.0,
        half_spread_cents_a=1.5,
        daily_yield_a=15.0,
    )

    assert cost.total_switching_cost_usd > 0.0
    assert cost.liquidation_friction_usd > 0.0

    # Short holding horizon (0.5h): should reject modest yield delta
    dec_short = engine.evaluate_switching_hurdle(
        y_net_a_daily=15.0,
        y_net_b_daily=20.0,
        switching_cost_usd=cost.total_switching_cost_usd,
        holding_horizon_hours=0.5,
    )
    assert not dec_short.should_switch

    # Long holding horizon (24h) with substantial yield delta ($35 vs $15): should accept
    dec_long = engine.evaluate_switching_hurdle(
        y_net_a_daily=15.0,
        y_net_b_daily=35.0,
        switching_cost_usd=cost.total_switching_cost_usd,
        holding_horizon_hours=24.0,
    )
    assert dec_long.should_switch
    assert dec_long.net_alpha_holding_usd > 0.0
