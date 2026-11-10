"""Unit tests for Dynamic Quote Sizing, Trade Frequency Estimation, and Optimal Spread Optimizer."""

from __future__ import annotations

from polymarketbot.strategy.sizing import (
    calculate_dynamic_cycles_budget,
    calibrate_cycles_budget,
    derive_quote_size,
    estimate_market_trade_frequency,
    optimize_spread_offset,
)


class TestEstimateMarketTradeFrequency:
    """Tests for estimate_market_trade_frequency."""

    def test_estimate_frequency_standard(self):
        # $10,000 24h volume, mid 0.50, 50-share avg trade notional = $25.00 -> 400 trades/day
        trades_10k = estimate_market_trade_frequency(volume_24h=10000.0, mid_price=0.50, default_trade_size=50.0)
        assert trades_10k == 400.0

        # $3,000 24h volume, mid 0.50, 50-share trade notional = $25.00 -> 120 trades/day
        trades_3k = estimate_market_trade_frequency(volume_24h=3000.0, mid_price=0.50, default_trade_size=50.0)
        assert trades_3k == 120.0

    def test_estimate_frequency_zero_and_negative_volume(self):
        assert estimate_market_trade_frequency(volume_24h=0.0, mid_price=0.50) == 0.0
        assert estimate_market_trade_frequency(volume_24h=-500.0, mid_price=0.50) == 0.0

    def test_estimate_frequency_price_clamping(self):
        # Deep OTM / ITM mid prices clamped safely between 0.01 and 0.99
        trades_otm = estimate_market_trade_frequency(volume_24h=100.0, mid_price=0.0001, default_trade_size=50.0)
        assert trades_otm > 0.0
        # at clamped price 0.01: notional = 50 * 0.01 = 0.50 -> 100 / 0.5 = 200 trades
        assert trades_otm == 200.0

        trades_itm = estimate_market_trade_frequency(volume_24h=100.0, mid_price=0.9999, default_trade_size=50.0)
        assert trades_itm > 0.0
        # at clamped price 0.99: notional = 50 * 0.99 = 49.50 -> 100 / 49.50 = 2.02 trades
        assert trades_itm == 2.02

    def test_estimate_frequency_custom_trade_size(self):
        # $5,000 volume, mid 0.25, 100-share average trade -> notional = $25.00 -> 200 trades
        trades = estimate_market_trade_frequency(volume_24h=5000.0, mid_price=0.25, default_trade_size=100.0)
        assert trades == 200.0


class TestDynamicCyclesBudgetCalibration:
    """Tests for calculate_dynamic_cycles_budget."""

    def test_cycles_budget_formula_in_bounds(self):
        # alloc_capital = 60.0, quote_size = 20.0, price = 0.50 -> denom = 2 * 20 * 0.50 = 20.0
        # budget = 60.0 / 20.0 = 3.0
        cb = calculate_dynamic_cycles_budget(alloc_capital=60.0, quote_size=20.0, price=0.50)
        assert cb == 3.0

    def test_cycles_budget_clamped_to_ceiling(self):
        # Abundant capital: 500 / 20 = 25.0 -> clamped to 5.0
        cb = calculate_dynamic_cycles_budget(alloc_capital=500.0, quote_size=20.0, price=0.50)
        assert cb == 5.0

    def test_cycles_budget_clamped_to_floor(self):
        # Constrained capital: 15 / 20 = 0.75 -> clamped to 1.5
        cb = calculate_dynamic_cycles_budget(alloc_capital=15.0, quote_size=20.0, price=0.50)
        assert cb == 1.5

    def test_cycles_budget_zero_and_negative_capital(self):
        assert calculate_dynamic_cycles_budget(alloc_capital=0.0, quote_size=20.0, price=0.50) == 1.5
        assert calculate_dynamic_cycles_budget(alloc_capital=-100.0, quote_size=20.0, price=0.50) == 1.5

    def test_cycles_budget_alias(self):
        assert calibrate_cycles_budget == calculate_dynamic_cycles_budget


class TestDynamicQuoteSizing:
    """Tests for derive_quote_size."""

    def test_derive_quote_size_default(self):
        # Bankroll $150, mid $0.74, min_size 20
        # Endogenous single-market reserve based dynamic cycles budget sizing
        size = derive_quote_size(initial_cash=150.0, mid_price=0.74, min_size=20.0)
        assert size >= 20.0
        assert abs(size - 29.6) <= 0.2

        # Explicit override reserve_pct=0.25 and cycles_budget=2.5 yields legacy 45.0
        size_legacy = derive_quote_size(
            initial_cash=150.0, mid_price=0.74, min_size=20.0, reserve_pct=0.25, cycles_budget=2.5
        )
        assert size_legacy == 45.0

        # Endogenous single-market reserve with dynamic gas buffer and legacy cycles_budget=2.5
        size_dyn_legacy = derive_quote_size(initial_cash=150.0, mid_price=0.74, min_size=20.0, cycles_budget=2.5)
        assert abs(size_dyn_legacy - 44.3) <= 0.2

        # With explicit static base_gas_buffer_usd=2.0
        size_static_gas = derive_quote_size(
            initial_cash=150.0, mid_price=0.74, min_size=20.0, cycles_budget=2.5, base_gas_buffer_usd=2.0
        )
        assert abs(size_static_gas - 43.5) <= 0.2

    def test_derive_quote_size_bumps_to_pool_min_size(self):
        # Bankroll $150, min_size 50
        size = derive_quote_size(initial_cash=150.0, mid_price=0.74, min_size=50.0)
        assert size >= 50.0

        # With legacy cycles_budget=2.5: Budget is ~43.5, but pool requires 50.0 -> bumps to 50.0
        size_override = derive_quote_size(initial_cash=150.0, mid_price=0.74, min_size=50.0, cycles_budget=2.5)
        assert size_override == 50.0

    def test_derive_quote_size_handles_oversized_pool_min(self):
        # Bankroll $100, min_size 200
        # Cannot afford 200 without leverage; caps to affordable size
        # With dynamic gas buffer:
        size = derive_quote_size(initial_cash=100.0, mid_price=0.75, min_size=200.0)
        assert size < 200.0
        assert abs(size - 97.9) <= 0.6

        # With static base_gas_buffer_usd=2.0:
        size_static = derive_quote_size(initial_cash=100.0, mid_price=0.75, min_size=200.0, base_gas_buffer_usd=2.0)
        assert abs(size_static - 95.2) <= 0.6

        # With explicit 0.25 reserve: 75.0 / 0.75 = 100.0
        size_legacy = derive_quote_size(initial_cash=100.0, mid_price=0.75, min_size=200.0, reserve_pct=0.25)
        assert size_legacy == 100.0

    def test_derive_quote_size_multi_market_diversification(self):
        # 5 diversified markets with $200 each (total $1000)
        multi_alloc = [{"capital": 200.0, "mid": 0.50} for _ in range(5)]
        size = derive_quote_size(initial_cash=1000.0, mid_price=0.50, allocations=multi_alloc)
        assert size > 0.0

    def test_derive_quote_size_zero_cash_fallback(self):
        size = derive_quote_size(initial_cash=0.0, mid_price=0.50)
        assert size == 5.0


class TestOptimalSpreadOptimizer:
    """Tests for optimize_spread_offset."""

    def test_optimize_spread_offset_bounds(self):
        s_opt, meta = optimize_spread_offset(
            max_spread_cents=4.5,
            market_spread_cents=1.0,
            tick_size_cents=1.0,
            daily_reward_pool=25.0,
            quote_size=45.0,
            competing_depth=1000.0,
        )
        # Optimal offset must be strictly <= max_spread * 0.95
        assert 0.1 <= s_opt <= 4.5 * 0.95
        assert "net_utility" in meta
        assert "payout_daily" in meta
        assert "spread_capture" in meta
        assert "adverse_loss" in meta
        assert "daily_trades_estimate" in meta
        assert meta["daily_trades_estimate"] > 0.0

    def test_optimize_spread_offset_respects_floor(self):
        # When competing depth is massive (e.g. 50,000), rewards won't clear $1.00 floor
        # Optimizer should pivot to spread capture rather than over-tightening
        s_opt, meta = optimize_spread_offset(
            max_spread_cents=4.5,
            market_spread_cents=2.0,
            tick_size_cents=1.0,
            daily_reward_pool=10.0,
            quote_size=20.0,
            competing_depth=50000.0,
        )
        assert meta["payout_daily"] == 0.0  # Floor correctly enforced (< $1.00)
        assert meta["spread_capture"] > 0.0

    def test_optimize_spread_offset_category_tuning(self):
        # Crypto has higher adverse selection volatility than politics
        s_crypto, _ = optimize_spread_offset(
            max_spread_cents=4.0,
            market_spread_cents=2.0,
            tick_size_cents=0.5,
            daily_reward_pool=50.0,
            quote_size=50.0,
            category="crypto",
        )
        s_sports, _ = optimize_spread_offset(
            max_spread_cents=4.0,
            market_spread_cents=2.0,
            tick_size_cents=0.5,
            daily_reward_pool=50.0,
            quote_size=50.0,
            category="sports",
        )
        # Crypto should widen or equal sports to guard against informed toxic sweep
        assert s_crypto >= s_sports

    def test_optimize_spread_offset_dynamic_trades_from_volume(self):
        # Higher 24h volume should estimate higher daily trade frequency and scale spread capture
        _, meta_high = optimize_spread_offset(
            max_spread_cents=4.5,
            market_spread_cents=1.0,
            daily_reward_pool=25.0,
            quote_size=50.0,
            volume_24h=50000.0,
            mid_price=0.50,
        )
        _, meta_low = optimize_spread_offset(
            max_spread_cents=4.5,
            market_spread_cents=1.0,
            daily_reward_pool=25.0,
            quote_size=50.0,
            volume_24h=1000.0,
            mid_price=0.50,
        )
        assert meta_high["daily_trades_estimate"] > meta_low["daily_trades_estimate"]
        assert meta_high["spread_capture"] > meta_low["spread_capture"]
        assert meta_high["daily_trades_estimate"] == 2000.0  # 50,000 / (50 * 0.5)

    def test_optimize_spread_offset_explicit_trades_override(self):
        # When explicitly passed, daily_trades_estimate override is respected
        _, meta = optimize_spread_offset(
            max_spread_cents=4.5,
            market_spread_cents=1.0,
            daily_reward_pool=25.0,
            quote_size=45.0,
            daily_trades_estimate=350.0,
        )
        assert meta["daily_trades_estimate"] == 350.0

