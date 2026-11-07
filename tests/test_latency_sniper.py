"""Unit tests for Strategy A: High-Frequency Oracle Latency Sniping Engine."""

from __future__ import annotations

import pytest

from polymarketbot.trading.latency_sniper import (
    BinanceLatencySniperEngine,
    LatencySniperConfig,
    PolymarketTopBook,
    SnipeDecision,
    TakerOrder,
    VIP_REBATE_RATES,
    calculate_taker_fee,
    get_vip_rebate_rate,
)


class TestLatencySniperConfig:
    def test_default_config(self) -> None:
        cfg = LatencySniperConfig()
        assert cfg.min_jump_bps == 25.0
        assert cfg.max_staleness_ms == 1500.0
        assert cfg.vip_tier == "bronze"
        assert cfg.min_net_edge_bps == 15.0
        assert cfg.max_single_snipe_usd == 250.0
        assert cfg.min_net_edge == 0.0015
        assert cfg.trailing_window_ms == 500.0
        assert cfg.taker_fee_rate == 0.07

    def test_invalid_vip_tier(self) -> None:
        with pytest.raises(ValueError, match="Invalid vip_tier"):
            LatencySniperConfig(vip_tier="ruby_master")

    def test_invalid_numerical_bounds(self) -> None:
        with pytest.raises(ValueError, match="min_jump_bps"):
            LatencySniperConfig(min_jump_bps=0.0)

        with pytest.raises(ValueError, match="max_staleness_ms"):
            LatencySniperConfig(max_staleness_ms=-10.0)

        with pytest.raises(ValueError, match="max_single_snipe_usd"):
            LatencySniperConfig(max_single_snipe_usd=0.0)


class TestFeeAndRebateCalculation:
    def test_vip_tier_rates(self) -> None:
        assert get_vip_rebate_rate("none") == 0.0
        assert get_vip_rebate_rate("bronze") == 0.03
        assert get_vip_rebate_rate("silver") == 0.08
        assert get_vip_rebate_rate("gold") == 0.18
        assert get_vip_rebate_rate("platinum") == 0.32
        assert get_vip_rebate_rate("diamond") == 0.44
        assert get_vip_rebate_rate("obsidian") == 0.50

    def test_fee_curve_at_midpoint(self) -> None:
        # At p=0.50, curve = 0.5 * 0.5 = 0.25
        # rate = 0.07, shares = 100 -> gross = 100 * 0.07 * 0.25 = 1.75
        gross, rebate, net = calculate_taker_fee(
            shares=100.0,
            price=0.50,
            rate=0.07,
            vip_tier="bronze",
        )
        assert gross == 1.75000
        # Bronze is 3%
        assert rebate == pytest.approx(1.75 * 0.03, abs=1e-5)
        assert net == pytest.approx(1.75 * (1.0 - 0.03), abs=1e-5)

    def test_obsidian_captures_edge_where_tier_0_is_unprofitable(self) -> None:
        """Verifies Obsidian (50% rebate) captures edge on smaller jumps where Tier 0 would be unprofitable."""
        # Top ask is 0.50
        # Stale ask = 0.50. Binance smaller jump results in post-jump fair price = 0.5120
        # Gross edge per share = 0.5120 - 0.5000 = 0.0120 (120 bps)
        #
        # Gross fee at 0.50 with rate 0.07 = 0.07 * 0.25 = 0.0175 per share
        # Tier 0 (0% rebate): net fee = 0.0175 -> net edge = 0.0120 - 0.0175 = -0.0055 (< 0, UNPROFITABLE)
        # Obsidian (50% rebate): net fee = 0.0175 * 0.50 = 0.00875 -> net edge = 0.0120 - 0.00875 = +0.00325 (32.5 bps > 15 bps)

        book = PolymarketTopBook(
            token_id="BTC-YES-5M",
            best_bid=0.49,
            best_bid_size=500.0,
            best_ask=0.50,
            best_ask_size=500.0,
            timestamp_ms=1000.0,
        )

        # 1. Tier 0 Engine
        engine_tier0 = BinanceLatencySniperEngine(
            LatencySniperConfig(vip_tier="none", min_net_edge_bps=15.0)
        )
        decision_tier0 = engine_tier0.evaluate_snipe(
            book=book,
            jump_bps=30.0,  # Above min_jump_bps
            fair_price_post_jump=0.5120,
            current_time_ms=1100.0,
        )
        assert decision_tier0.actionable is False
        assert decision_tier0.rejection_reason == "INSUFFICIENT_NET_EDGE"
        assert decision_tier0.net_edge < 0

        # 2. Obsidian Engine (50% Rebate)
        engine_obsidian = BinanceLatencySniperEngine(
            LatencySniperConfig(vip_tier="obsidian", min_net_edge_bps=15.0)
        )
        decision_obsidian = engine_obsidian.evaluate_snipe(
            book=book,
            jump_bps=30.0,  # Above min_jump_bps
            fair_price_post_jump=0.5120,
            current_time_ms=1100.0,
        )
        assert decision_obsidian.actionable is True
        assert decision_obsidian.rejection_reason is None
        assert decision_obsidian.net_edge > 0.0015  # > 15 bps
        assert decision_obsidian.order is not None
        assert decision_obsidian.order.side == "BUY"
        assert decision_obsidian.order.price == 0.50
        assert decision_obsidian.order.vip_rebate > 0.0


class TestSniperExecutionAndJumps:
    def test_upward_jump_detection_fires_taker_buy(self) -> None:
        """Test upward jump detection: Binance jumps +0.50%, Polymarket ask is stale at 0.50, engine fires taker BUY."""
        engine = BinanceLatencySniperEngine(
            LatencySniperConfig(
                min_jump_bps=25.0,
                vip_tier="bronze",
                max_single_snipe_usd=250.0,
            )
        )

        # Record Binance price jump: 100,000 -> 100,500 (+0.50%, +50 bps) in 200ms
        t0 = 1_000_000.0
        engine.record_binance_price(100_000.0, timestamp_ms=t0)
        engine.record_binance_price(100_500.0, timestamp_ms=t0 + 200.0)

        # Polymarket book has stale ask at 0.50
        book = PolymarketTopBook(
            token_id="BTC-UP-5M",
            best_bid=0.49,
            best_bid_size=1000.0,
            best_ask=0.50,
            best_ask_size=1000.0,
            timestamp_ms=t0 + 250.0,
        )

        now_ms = t0 + 300.0
        decision = engine.evaluate_snipe(book=book, current_time_ms=now_ms)

        assert decision.actionable is True
        assert decision.order is not None
        order = decision.order

        assert order.side == "BUY"
        assert order.price == 0.50
        assert order.order_type == "FAK"
        # Budget is $250.0 / $0.50 = 500 shares (available is 1000)
        assert order.shares == 500.0
        assert order.notional_usd == 250.0
        assert order.fair_price > 0.50
        assert order.net_edge > 0.0015
        assert order.vip_rebate > 0.0
        assert order.expected_profit_usd > 0.0

        # Execute order and verify telemetry
        executed = engine.execute_snipe(decision)
        assert executed == order
        assert engine.total_snipes == 1
        assert engine.gross_profit > 0.0
        assert engine.fees_paid > 0.0
        assert engine.rebates_earned > 0.0
        assert engine.net_latency_pnl > 0.0
        assert engine.telemetry.total_volume_usd == 250.0
        assert engine.telemetry.total_shares == 500.0

    def test_downward_dump_detection_fires_taker_sell(self) -> None:
        """Test downward dump detection: Binance dumps -0.50%, Polymarket bid is stale at 0.50, engine fires taker SELL."""
        engine = BinanceLatencySniperEngine(
            LatencySniperConfig(
                min_jump_bps=25.0,
                vip_tier="bronze",
                max_single_snipe_usd=200.0,
            )
        )

        # Record Binance dump: 100,000 -> 99,500 (-0.50%, -50 bps)
        t0 = 2_000_000.0
        engine.record_binance_price(100_000.0, timestamp_ms=t0)
        engine.record_binance_price(99_500.0, timestamp_ms=t0 + 200.0)

        # Stale top bid at 0.50
        book = PolymarketTopBook(
            token_id="BTC-UP-5M",
            best_bid=0.50,
            best_bid_size=800.0,
            best_ask=0.52,
            best_ask_size=800.0,
            timestamp_ms=t0 + 250.0,
        )

        now_ms = t0 + 300.0
        decision = engine.evaluate_snipe(book=book, current_time_ms=now_ms)

        assert decision.actionable is True
        assert decision.order is not None
        order = decision.order

        assert order.side == "SELL"
        assert order.price == 0.50
        assert order.shares == 400.0  # $200 / 0.50
        assert order.fair_price < 0.50
        assert order.gross_edge > 0.0
        assert order.net_edge > 0.0015
        assert order.expected_profit_usd > 0.0

        # Execute
        engine.execute_snipe(decision)
        assert engine.total_snipes == 1
        assert engine.net_latency_pnl > 0.0


class TestSniperRejections:
    def test_rejection_spread_too_wide(self) -> None:
        """Test rejection when spread is too wide."""
        engine = BinanceLatencySniperEngine(LatencySniperConfig(max_spread=0.10))

        # Spread is 0.65 - 0.35 = 0.30 > 0.10
        book = PolymarketTopBook(
            token_id="BTC-WIDE",
            best_bid=0.35,
            best_bid_size=100.0,
            best_ask=0.65,
            best_ask_size=100.0,
            timestamp_ms=1000.0,
        )

        decision = engine.evaluate_snipe(
            book=book,
            jump_bps=50.0,
            current_time_ms=1100.0,
        )
        assert decision.actionable is False
        assert decision.rejection_reason == "SPREAD_TOO_WIDE"
        assert engine.telemetry.rejected_opportunities == 1
        assert engine.telemetry.rejection_counts["SPREAD_TOO_WIDE"] == 1

    def test_rejection_book_already_updated(self) -> None:
        """Test rejection when book has already updated to reflect fair value."""
        engine = BinanceLatencySniperEngine()

        # Binance jumped +50 bps, fair value is 0.55
        # But Polymarket ask already jumped to 0.56!
        book = PolymarketTopBook(
            token_id="BTC-FRESH",
            best_bid=0.53,
            best_bid_size=100.0,
            best_ask=0.56,
            best_ask_size=100.0,
            timestamp_ms=1000.0,
        )

        decision = engine.evaluate_snipe(
            book=book,
            jump_bps=50.0,
            fair_price_post_jump=0.55,
            current_time_ms=1100.0,
        )
        assert decision.actionable is False
        assert decision.rejection_reason == "BOOK_ALREADY_UPDATED"

    def test_rejection_book_too_stale(self) -> None:
        """Test rejection when book snapshot age exceeds max_staleness_ms (1500ms)."""
        engine = BinanceLatencySniperEngine(LatencySniperConfig(max_staleness_ms=1500.0))

        # Snapshot from 2000ms ago
        book = PolymarketTopBook(
            token_id="BTC-OLD",
            best_bid=0.49,
            best_bid_size=100.0,
            best_ask=0.50,
            best_ask_size=100.0,
            timestamp_ms=1000.0,
        )

        decision = engine.evaluate_snipe(
            book=book,
            jump_bps=50.0,
            current_time_ms=3000.0,  # 2000ms age
        )
        assert decision.actionable is False
        assert decision.rejection_reason == "BOOK_TOO_STALE"

    def test_rejection_jump_below_threshold(self) -> None:
        """Test rejection when Binance velocity is below min_jump_bps."""
        engine = BinanceLatencySniperEngine(LatencySniperConfig(min_jump_bps=25.0))

        book = PolymarketTopBook(
            token_id="BTC-CALM",
            best_bid=0.49,
            best_bid_size=100.0,
            best_ask=0.50,
            best_ask_size=100.0,
            timestamp_ms=1000.0,
        )

        # Move is only 10 bps
        decision = engine.evaluate_snipe(
            book=book,
            jump_bps=10.0,
            current_time_ms=1100.0,
        )
        assert decision.actionable is False
        assert decision.rejection_reason == "JUMP_BELOW_THRESHOLD"

    def test_rejection_insufficient_liquidity(self) -> None:
        """Test rejection when top-of-book depth is less than min_shares."""
        engine = BinanceLatencySniperEngine(LatencySniperConfig(min_shares=5.0))

        book = PolymarketTopBook(
            token_id="BTC-THIN",
            best_bid=0.49,
            best_bid_size=0.5,
            best_ask=0.50,
            best_ask_size=0.5,  # only 0.5 shares available
            timestamp_ms=1000.0,
        )

        decision = engine.evaluate_snipe(
            book=book,
            jump_bps=50.0,
            fair_price_post_jump=0.55,
            current_time_ms=1100.0,
        )
        assert decision.actionable is False
        assert decision.rejection_reason == "INSUFFICIENT_LIQUIDITY"


class TestTelemetryAndMultipleSnipes:
    def test_cumulative_telemetry(self) -> None:
        engine = BinanceLatencySniperEngine(
            LatencySniperConfig(vip_tier="obsidian", max_single_snipe_usd=100.0)
        )

        book1 = PolymarketTopBook("T1", 0.49, 500, 0.50, 500, 1000.0)
        dec1 = engine.evaluate_snipe(book1, jump_bps=50.0, fair_price_post_jump=0.55, current_time_ms=1100.0)
        assert dec1.actionable is True
        engine.execute_snipe(dec1)

        book2 = PolymarketTopBook("T2", 0.50, 500, 0.51, 500, 2000.0)
        dec2 = engine.evaluate_snipe(book2, jump_bps=-50.0, fair_price_post_jump=0.45, current_time_ms=2100.0)
        assert dec2.actionable is True
        engine.execute_snipe(dec2)

        assert engine.total_snipes == 2
        assert engine.gross_profit > 0.0
        assert engine.fees_paid > 0.0
        assert engine.rebates_earned > 0.0
        # In telemetry, net_latency_pnl should match expected PnL
        assert engine.net_latency_pnl == pytest.approx(
            engine.gross_profit - engine.fees_paid + engine.rebates_earned, abs=1e-3
        )
