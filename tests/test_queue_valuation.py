"""Economic-property tests for FIFO queue priority, CTF gas and switching frictions.

The queue model has no default parameters, so every test supplies the fitted
inputs explicitly. Assertions are economic properties (monotonicity, refusal),
not a re-derivation of the model's formula.
"""

from __future__ import annotations

import pytest

from polymarketbot.strategy.reward.queue import (
    CapitalMigrationEngine,
    CTFGasProfile,
    LiquidationRoute,
    PolygonPoSEnvironment,
    QueuePriorityValuationModel,
    solve_inventory_liquidation_friction,
)


def _fitted_model() -> QueuePriorityValuationModel:
    """A model built from an explicit fit (no constructor defaults exist)."""
    return QueuePriorityValuationModel(
        trade_arrival_rate_shares_sec=25.0,
        cancellation_rate_intensity=0.05,
        kyle_lambda=0.00015,
        volatility_annualized=0.45,
        annual_capital_hurdle_rate=0.12,
    )


def _measured_gas_env() -> PolygonPoSEnvironment:
    """A priced environment supplied by the test instead of a network read."""
    return PolygonPoSEnvironment(
        base_fee_gwei=100.0,
        priority_fee_gwei=0.0,
        pol_usd_rate=0.09,
    )


def _engine() -> CapitalMigrationEngine:
    return CapitalMigrationEngine(
        gas_env=_measured_gas_env(),
        queue_model=_fitted_model(),
    )


def test_polygon_gas_calculations() -> None:
    env = PolygonPoSEnvironment(base_fee_gwei=35.0, priority_fee_gwei=45.0, pol_usd_rate=0.35)
    assert env.total_gas_price_gwei == 80.0

    profile = CTFGasProfile()
    cost_usd = env.gas_units_to_usd(profile.gas_merge_direct_typical)
    # 78,000 * 80e-9 * 0.35 = 0.002184 USD
    assert 0.0020 <= cost_usd <= 0.0025


def test_queue_model_refuses_unmeasured_parameters() -> None:
    with pytest.raises(TypeError):
        QueuePriorityValuationModel()  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        QueuePriorityValuationModel(
            trade_arrival_rate_shares_sec=0.0,
            cancellation_rate_intensity=0.05,
            kyle_lambda=0.00015,
            volatility_annualized=0.45,
            annual_capital_hurdle_rate=0.12,
        )
    with pytest.raises(ValueError):
        QueuePriorityValuationModel(
            trade_arrival_rate_shares_sec=25.0,
            cancellation_rate_intensity=-1.0,
            kyle_lambda=0.00015,
            volatility_annualized=0.45,
            annual_capital_hurdle_rate=0.12,
        )


def test_deeper_queue_strictly_lowers_projection() -> None:
    model = _fitted_model()
    level_size = 10_000.0
    ahead = (0.0, 1_000.0, 5_000.0)

    valuations = [
        model.evaluate_queue_value(
            quote_size=100.0,
            queue_ahead=queue_ahead,
            half_spread_cents=1.5,
            token_mid_price=0.50,
            level_size_shares=level_size,
        )
        for queue_ahead in ahead
    ]

    # FIFO depth strictly lowers fill probability and the projected net value,
    # and strictly lengthens the wait for the shares ahead to clear.
    probabilities = [v.fill_probability for v in valuations]
    values = [v.net_queue_value_usd for v in valuations]
    waits = [v.expected_wait_time_seconds for v in valuations]
    assert probabilities[0] > probabilities[1] > probabilities[2]
    assert values[0] > values[1] > values[2]
    assert waits[0] < waits[1] < waits[2]


def test_synthetic_liquidation_dominance() -> None:
    # Scenario 1: Direct bid is depressed (0.45) but opposite ask is tight (0.51)
    # Sum: 0.45 + 0.51 + Gas/q < 1.00 -> Synthetic completion should dominate!
    res_synth = solve_inventory_liquidation_friction(
        q=150.0,
        p_mid=0.50,
        p_bid=0.45,
        p_ask_opp=0.51,
        gas_merge_usd=0.015,
        taker_fee_rate=0.0,
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
        taker_fee_rate=0.0,
    )
    assert res_direct.route == LiquidationRoute.DIRECT_CROSS
    assert res_direct.optimal_friction_usd == pytest.approx(150.0 * (0.50 - 0.495))


def test_missing_taker_fee_is_refused() -> None:
    with pytest.raises(TypeError):
        solve_inventory_liquidation_friction(  # type: ignore[call-arg]
            q=150.0,
            p_mid=0.50,
            p_bid=0.45,
            p_ask_opp=0.51,
            gas_merge_usd=0.015,
        )
    with pytest.raises(ValueError):
        solve_inventory_liquidation_friction(
            q=150.0,
            p_mid=0.50,
            p_bid=0.45,
            p_ask_opp=0.51,
            gas_merge_usd=0.015,
            taker_fee_rate=None,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        solve_inventory_liquidation_friction(
            q=150.0,
            p_mid=0.50,
            p_bid=0.45,
            p_ask_opp=0.51,
            gas_merge_usd=0.015,
            taker_fee_rate=-0.01,
        )


def test_higher_taker_fee_strictly_raises_switching_cost() -> None:
    engine = _engine()
    kwargs = {
        "q_a": 100.0,
        "p_mid_a": 0.50,
        "p_bid_a": 0.48,
        "p_ask_opp_a": 0.51,
        "resting_bid_size": 100.0,
        "resting_ask_size": 100.0,
        "queue_ahead_bid": 50.0,
        "queue_ahead_ask": 50.0,
        "half_spread_cents_a": 1.5,
        "daily_yield_a": 15.0,
        # One measured CLOB round trip; the former 4.5 s placeholder is gone.
        "transit_latency_seconds": 0.57,
    }
    free = engine.compute_switching_cost(taker_fee_rate=0.0, **kwargs)
    charged = engine.compute_switching_cost(taker_fee_rate=0.05, **kwargs)

    # A measured taker fee can only add friction: it is never free.
    assert charged.liquidation_friction_usd > free.liquidation_friction_usd
    assert charged.total_switching_cost_usd > free.total_switching_cost_usd


def test_switching_cost_refuses_a_missing_fee() -> None:
    engine = _engine()
    with pytest.raises(TypeError):
        engine.compute_switching_cost(  # type: ignore[call-arg]
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
            transit_latency_seconds=0.57,
        )
    with pytest.raises(ValueError):
        engine.compute_switching_cost(
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
            transit_latency_seconds=0.57,
            taker_fee_rate=None,  # type: ignore[arg-type]
        )


def test_capital_migration_engine_requires_a_fitted_queue_model() -> None:
    with pytest.raises(TypeError):
        CapitalMigrationEngine()  # type: ignore[call-arg]


def test_capital_migration_hurdle_evaluation() -> None:
    engine = _engine()

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
        transit_latency_seconds=0.57,
        taker_fee_rate=0.0,
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
