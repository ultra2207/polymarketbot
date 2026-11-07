"""Tests for Binance Price Anchoring, Black-Scholes Quoting, and Exit Optimizer."""

from __future__ import annotations

import math

import pytest

from polymarketbot.trading.binance_anchor import (
    BinanceAnchoredQuoter,
    BinanceSpotFeed,
    BlackScholesBinaryEngine,
    ExitPathway,
    HorizonRegime,
    LifecycleExitOptimizer,
    MarketHorizonClassifier,
)


def test_binance_spot_feed_velocity():
    feed = BinanceSpotFeed(symbol="BTCUSDT")
    t0 = 1000.0
    feed.update_price(price=75000.0, timestamp=t0)
    feed.update_price(price=75000.0, timestamp=t0 + 2.0)
    assert feed.get_velocity_bps(lookback_seconds=5.0) == 0.0

    # Simulate +10 bps surge in 3 seconds (75,000 -> 75,075)
    feed.update_price(price=75075.0, timestamp=t0 + 5.0)
    vel = feed.get_velocity_bps(lookback_seconds=5.0)
    assert pytest.approx(vel, rel=1e-2) == 10.0


def test_black_scholes_digital_option_at_the_money():
    engine = BlackScholesBinaryEngine(annualized_volatility=0.55)
    strike = 75000.0
    spot = 75000.0
    tau = 300.0  # 5 minutes

    p_up, p_down, _d2 = engine.price_digital_option(strike, spot, tau)
    # At the money for short horizon, probability of Up and Down is near 50%
    assert 0.49 <= p_up <= 0.51
    assert 0.49 <= p_down <= 0.51
    assert pytest.approx(p_up + p_down, abs=1e-5) == 1.0


def test_black_scholes_digital_option_in_the_money():
    engine = BlackScholesBinaryEngine(annualized_volatility=0.55)
    strike = 75000.0
    spot = 75150.0  # +20 bps above strike
    tau = 120.0     # 2 minutes remaining

    p_up, p_down, _d2 = engine.price_digital_option(strike, spot, tau)
    assert p_up > 0.70
    assert p_down < 0.30
    assert pytest.approx(p_up + p_down, abs=1e-5) == 1.0


def test_black_scholes_digital_option_expired():
    engine = BlackScholesBinaryEngine()
    strike = 75000.0
    # Expired above strike
    p_up, p_down, _ = engine.price_digital_option(strike, 75001.0, time_to_expiry_seconds=0.0)
    assert p_up == 1.0
    assert p_down == 0.0

    # Expired below strike
    p_up_fail, p_down_fail, _ = engine.price_digital_option(strike, 74999.0, time_to_expiry_seconds=0.0)
    assert p_up_fail == 0.0
    assert p_down_fail == 1.0


def test_binance_anchored_quoter_pair_arbitrage():
    quoter = BinanceAnchoredQuoter(
        max_pair_cost=0.985,
        half_spread_cents=1.5,
        default_ticket_size=5.0,
    )
    decision = quoter.evaluate_quotes(
        strike_k=75000.0,
        spot_s=75000.0,
        tau_seconds=300.0,
        velocity_5s_bps=0.0,
        inventory_up=0.0,
        inventory_down=0.0,
    )

    # Verifications
    assert decision.combined_pair_cost <= 0.985
    assert decision.expected_margin_cents >= 1.5
    assert decision.quote_size_up == 5.0
    assert decision.quote_size_down == 5.0
    assert not decision.is_toxic_surge
    assert decision.surge_side_suppressed == "NONE"


def test_binance_anchored_quoter_toxic_surge_defense():
    quoter = BinanceAnchoredQuoter(surge_threshold_bps=1.0)

    # 1. Binance surging upward (+1.5 bps in 5s) -> Down resting bid suppressed!
    surge_up = quoter.evaluate_quotes(
        strike_k=75000.0,
        spot_s=75050.0,
        tau_seconds=180.0,
        velocity_5s_bps=1.5,
    )
    assert surge_up.is_toxic_surge
    assert surge_up.surge_side_suppressed == "DOWN"
    assert surge_up.quote_size_down == 0.0
    assert surge_up.quote_size_up == 5.0

    # 2. Binance dumping downward (-2.0 bps in 5s) -> Up resting bid suppressed!
    dump_down = quoter.evaluate_quotes(
        strike_k=75000.0,
        spot_s=74950.0,
        tau_seconds=180.0,
        velocity_5s_bps=-2.0,
    )
    assert dump_down.is_toxic_surge
    assert dump_down.surge_side_suppressed == "UP"
    assert dump_down.quote_size_up == 0.0
    assert dump_down.quote_size_down == 5.0


def test_binance_anchored_quoter_inventory_skew():
    quoter = BinanceAnchoredQuoter()
    # Neutral quotes
    neutral = quoter.evaluate_quotes(
        strike_k=75000.0, spot_s=75000.0, tau_seconds=300.0,
        inventory_up=0.0, inventory_down=0.0
    )
    # Excess Up inventory (+20 shares) -> shades Up bid lower and Down bid higher
    skewed = quoter.evaluate_quotes(
        strike_k=75000.0, spot_s=75000.0, tau_seconds=300.0,
        inventory_up=20.0, inventory_down=0.0
    )
    assert skewed.target_bid_up <= neutral.target_bid_up
    assert skewed.target_bid_down >= neutral.target_bid_down


def test_binance_anchored_quoter_evaluate_as_quotes():
    quoter = BinanceAnchoredQuoter(max_pair_cost=0.985)
    as_dec = quoter.evaluate_as_quotes(
        strike_k=75000.0,
        spot_s=75000.0,
        tau_seconds=300.0,
        inventory_up=15.0,
        inventory_down=0.0,
    )
    assert as_dec.is_pair_arbitrage_guaranteed
    assert as_dec.pair_bid_cost <= 0.985
    assert as_dec.yes_quote.bid_price < as_dec.yes_quote.ask_price
    assert as_dec.no_quote.bid_price < as_dec.no_quote.ask_price
    # Up inventory (+15) -> YES bid is low, NO bid is elevated
    assert as_dec.yes_quote.ask_size > as_dec.yes_quote.bid_size


def test_lifecycle_exit_optimizer_short_dwell_recommends_settlement():
    optimizer = LifecycleExitOptimizer()

    # Position: 50 Up and 45 Down (balanced pair) on 5m market with 90 seconds left
    # Bid/Ask: Up 0.48/0.50, Down 0.48/0.50
    result = optimizer.compare_exit_pathways(
        shares_up=50.0,
        shares_down=45.0,
        bid_up=0.48,
        bid_down=0.48,
        ask_up=0.50,
        ask_down=0.50,
        tau_seconds=90.0,
        p_win_up=0.50,
    )

    # Expiration settlement should be recommended because opportunity cost of 90s is < 1c,
    # while selling costs taker fee (1.75%) + spread crossing.
    assert result.recommended_pathway == ExitPathway.EXPIRATION_SETTLEMENT
    assert result.net_payout_expiration > result.net_payout_sell
    assert result.opportunity_cost_waiting < 0.01  # less than 1 cent drag


def test_lifecycle_exit_optimizer_long_dwell_recommends_merge():
    optimizer = LifecycleExitOptimizer()

    # Position: exactly 100 complete sets on a market with 10 days left (864,000s)
    result = optimizer.compare_exit_pathways(
        shares_up=100.0,
        shares_down=100.0,
        bid_up=0.48,
        bid_down=0.48,
        ask_up=0.50,
        ask_down=0.50,
        tau_seconds=864000.0,
        p_win_up=0.50,
    )

    # For 10 days, opportunity cost of waiting is high (~$25), so merging complete sets
    # immediately unlocks $100 cash with $0 taker fee and $0.015 gas.
    assert result.recommended_pathway == ExitPathway.COMPLETE_SET_MERGE
    assert result.net_payout_merge > result.net_payout_sell


def test_market_horizon_classifier_fast_5m():
    market = {
        "market_slug": "btc-updown-5m-1793701200",
        "question": "Bitcoin Up or Down - November 3, 5:20AM-5:25AM ET",
        "condition_id": "0xee661d27a2a3c80e50a3c69f16ace55da4b30acde1f021695008cb5a34b3efbe",
    }
    # Pretend current time is 1793701050 (150s before expiry)
    info = MarketHorizonClassifier.classify_market(market, current_time=1793701050.0)

    assert info.regime == HorizonRegime.FAST_5M
    assert info.is_fast_binary is True
    assert info.base_asset == "BTC"
    assert info.time_to_expiry_seconds == 150.0
    assert info.recommended_exit_default == ExitPathway.EXPIRATION_SETTLEMENT


def test_market_horizon_classifier_fast_15m():
    market = {
        "slug": "eth-updown-15m-1793701800",
        "question": "Ethereum Up or Down - November 3, 5:15AM-5:30AM ET",
        "condition_id": "0x647ab1de3e1813c7636903c64a427932698147c7c6018b82a76a7b77f951089c",
    }
    info = MarketHorizonClassifier.classify_market(market, current_time=1793701200.0)

    assert info.regime == HorizonRegime.FAST_15M
    assert info.is_fast_binary is True
    assert info.base_asset == "ETH"
    assert info.time_to_expiry_seconds == 600.0
    assert info.recommended_exit_default == ExitPathway.EXPIRATION_SETTLEMENT


def test_market_horizon_classifier_strike_and_long_term():
    # Long-term market
    market_long = {
        "slug": "2026-balance-of-power-d-senate-d-house-949",
        "question": "2026 Balance of Power: D Senate, D House",
        "end_date_iso": "2026-11-04T00:00:00Z",
    }
    info_long = MarketHorizonClassifier.classify_market(market_long, current_time=1793600000.0)
    assert info_long.regime == HorizonRegime.LONG_TERM
    assert info_long.is_fast_binary is False
    assert info_long.recommended_exit_default == ExitPathway.COMPLETE_SET_MERGE

    # Strike interval market
    market_strike = {
        "slug": "bitcoin-above-76800-on-november-3-2026",
        "question": "Bitcoin above 76,800 on November 3?",
        "end_date": "2026-11-03T23:59:59Z",
    }
    info_strike = MarketHorizonClassifier.classify_market(market_strike, current_time=1793700000.0)
    assert info_strike.base_asset == "BTC"
    assert info_strike.strike_price == 76800.0


def test_binance_spot_feed_realized_volatility_fallback():
    feed = BinanceSpotFeed(symbol="BTCUSDT")
    # No ticks
    assert feed.get_realized_volatility() == 0.55

    # 3 ticks (< min_ticks=5)
    t0 = 1000.0
    feed.update_price(70000.0, timestamp=t0)
    feed.update_price(70050.0, timestamp=t0 + 1.0)
    feed.update_price(70020.0, timestamp=t0 + 2.0)
    assert feed.get_realized_volatility(min_ticks=5) == 0.55


def test_binance_spot_feed_realized_volatility_bounds():
    feed = BinanceSpotFeed(symbol="BTCUSDT")
    t0 = 1000.0
    # Flat price -> 0 sample variance -> clamped to lower bound 0.10
    for i in range(10):
        feed.update_price(70000.0, timestamp=t0 + i)
    assert feed.get_realized_volatility() == 0.10

    # Wildly oscillating price -> huge variance -> clamped to upper bound 2.50
    feed_wild = BinanceSpotFeed(symbol="BTCUSDT")
    for i in range(10):
        price = 70000.0 if i % 2 == 0 else 85000.0
        feed_wild.update_price(price, timestamp=t0 + i)
    assert feed_wild.get_realized_volatility() == 2.50


def test_binance_spot_feed_realized_volatility_calculation():
    feed = BinanceSpotFeed(symbol="BTCUSDT")
    t0 = 1000.0
    prices = [70000.0, 70010.0, 70005.0, 70020.0, 70015.0, 70025.0, 70018.0]
    for i, p in enumerate(prices):
        feed.update_price(p, timestamp=t0 + i)
    vol = feed.get_realized_volatility(lookback_seconds=30.0, min_ticks=5)
    assert 0.10 <= vol <= 2.50
    assert 0.20 <= vol <= 1.00


def test_black_scholes_engine_update_volatility():
    engine = BlackScholesBinaryEngine(annualized_volatility=0.55)
    initial_sigma_sec = engine.sigma_per_sqrt_sec

    engine.update_volatility(0.85)
    assert engine.sigma == 0.85
    assert engine.sigma_per_sqrt_sec > initial_sigma_sec
    assert pytest.approx(engine.sigma_per_sqrt_sec, rel=1e-5) == 0.85 / math.sqrt(31_557_600.0)


def test_binance_anchored_quoter_dynamic_feed_and_spread():
    feed = BinanceSpotFeed(symbol="BTCUSDT")
    t0 = 1000.0
    # Flat prices -> 0.10 vol
    for i in range(10):
        feed.update_price(70000.0, timestamp=t0 + i)

    quoter = BinanceAnchoredQuoter()
    strike = 70000.0
    spot = 70000.0
    tau = 300.0

    # 1. Evaluate with feed (updates sigma to 0.10)
    decision = quoter.evaluate_quotes(
        strike_k=strike,
        spot_s=spot,
        tau_seconds=tau,
        binance_feed=feed,
    )
    assert quoter.bs_engine.sigma == 0.10

    # 2. Evaluate with market_spread_cents = 2.0c
    # half_spread = max(0.5, min(3.0, 1.0)) / 100 = 0.01
    # max_pair_cost = 1.00 - 2 * 0.01 = 0.98
    dec_2c = quoter.evaluate_quotes(
        strike_k=strike,
        spot_s=spot,
        tau_seconds=tau,
        market_spread_cents=2.0,
    )
    assert dec_2c.combined_pair_cost <= 0.98
    assert dec_2c.expected_margin_cents >= 2.0

    # 3. Evaluate with wide spread market_spread_cents = 8.0c
    # half_spread = min(3.0, 4.0) / 100 = 0.03
    # max_pair_cost = 1.00 - 2 * 0.03 = 0.94
    dec_8c = quoter.evaluate_quotes(
        strike_k=strike,
        spot_s=spot,
        tau_seconds=tau,
        market_spread_cents=8.0,
    )
    assert dec_8c.combined_pair_cost <= 0.94
    assert dec_8c.expected_margin_cents >= 6.0

    # 4. Evaluate with tight spread market_spread_cents = 0.4c
    # half_spread = max(0.5, 0.2) / 100 = 0.005
    # max_pair_cost = 1.00 - 2 * 0.005 = 0.99
    dec_tight = quoter.evaluate_quotes(
        strike_k=strike,
        spot_s=spot,
        tau_seconds=tau,
        market_spread_cents=0.4,
    )
    assert dec_tight.combined_pair_cost <= 0.99


