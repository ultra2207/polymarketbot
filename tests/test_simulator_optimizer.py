"""Unit tests for Dynamic Quote Sizing and Optimal Spread Optimizer."""

from __future__ import annotations

import pytest

from polymarketbot.simulator.optimizer import (
    derive_quote_size,
    optimize_spread_offset,
)


class TestDynamicQuoteSizing:
    """Tests for derive_quote_size."""

    def test_derive_quote_size_default(self):
        # Bankroll $150, mid $0.74, min_size 20
        # Avail = 150 * 0.75 = 112.5. Budget = 112.5 / 2.5 = 45.0
        size = derive_quote_size(initial_cash=150.0, mid_price=0.74, min_size=20.0)
        assert size == 45.0

    def test_derive_quote_size_bumps_to_pool_min_size(self):
        # Bankroll $150, min_size 50. Budget is 45.0, but pool requires 50.0.
        # Cost of 50 @ 0.74 = $37.00 <= $112.5 avail -> bumps to 50.0 to qualify for rewards
        size = derive_quote_size(initial_cash=150.0, mid_price=0.74, min_size=50.0)
        assert size == 50.0

    def test_derive_quote_size_handles_oversized_pool_min(self):
        # Bankroll $100, min_size 200 (200 * 0.75 = $150 > $75 avail)
        # Cannot afford 200 without leverage; caps to affordable size
        size = derive_quote_size(initial_cash=100.0, mid_price=0.75, min_size=200.0)
        assert size < 200.0
        assert size == 100.0  # 75.0 / 0.75 = 100.0

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
