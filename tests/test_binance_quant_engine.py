"""Tests for the Binance High-Frequency Quantitative Pricing Engine."""

from __future__ import annotations

import math
import pytest

from polymarketbot.trading.binance_quant_engine import (
    BinaryOptionGreeks,
    HighFrequencyVolEstimator,
    KlineBar,
    MertonJumpDiffusionBinaryEngine,
    MicroPriceEstimator,
    OrderBookLevel,
    OrderFlowImbalanceTracker,
    PolymarketArbitrageScanner,
    norm_cdf,
    norm_pdf,
)


def test_micro_price_l1_and_multilevel():
    # Symmetric book: micro-price equals mid-price
    p_l1_sym = MicroPriceEstimator.calculate_l1_micro_price(
        bid_price=100.0, bid_size=10.0, ask_price=101.0, ask_size=10.0
    )
    assert p_l1_sym == 100.5

    # Heavy bid queue (100 bid vs 10 ask) -> micro-price leans toward ask
    p_l1_heavy_bid = MicroPriceEstimator.calculate_l1_micro_price(
        bid_price=100.0, bid_size=100.0, ask_price=101.0, ask_size=10.0
    )
    assert pytest.approx(p_l1_heavy_bid, rel=1e-3) == 100.909

    # Multilevel L2 micro-price
    bids = [OrderBookLevel(100.0, 50.0), OrderBookLevel(99.0, 100.0)]
    asks = [OrderBookLevel(101.0, 10.0), OrderBookLevel(102.0, 20.0)]
    p_multi = MicroPriceEstimator.calculate_multilevel_micro_price(bids, asks)
    assert p_multi > 100.5  # Skewed upward due to heavy bids


def test_volatility_estimators():
    # Synthetic bars with upward trend
    bars = [
        KlineBar(open_time_s=100, open=100.0, high=102.0, low=99.5, close=101.5),
        KlineBar(open_time_s=160, open=101.5, high=103.0, low=101.0, close=102.8),
        KlineBar(open_time_s=220, open=102.8, high=104.5, low=102.0, close=104.0),
    ]

    var_rs = HighFrequencyVolEstimator.rogers_satchell_variance(bars)
    var_gk = HighFrequencyVolEstimator.garman_klass_variance(bars)
    var_p = HighFrequencyVolEstimator.parkinson_variance(bars)

    assert var_rs > 0.0
    assert var_gk > 0.0
    assert var_p > 0.0

    ann_vol = HighFrequencyVolEstimator.annualize_bar_volatility(var_rs, bar_duration_seconds=60.0)
    assert 0.10 <= ann_vol <= 2.50

    # Test seasonality
    # Funding settlement at 00:00 UTC (timestamp 0)
    mult_funding = HighFrequencyVolEstimator.get_seasonality_multiplier(utc_timestamp=0.0)
    assert mult_funding >= 1.40


def test_order_flow_imbalance_and_drift():
    tracker = OrderFlowImbalanceTracker(history_seconds=60.0)
    t0 = 1000.0

    # Initial frame
    tracker.update(bid_price=75000.0, bid_size=5.0, ask_price=75001.0, ask_size=5.0, timestamp=t0)

    # Bid price moves up from 75000 to 75000.5 with size 8.0 -> strong positive OFI
    inc = tracker.update(bid_price=75000.5, bid_size=8.0, ask_price=75001.0, ask_size=5.0, timestamp=t0 + 2.0)
    assert inc > 0.0

    rate = tracker.get_ofi_rate(lookback_seconds=5.0)
    assert rate > 0.0

    mu_annual = tracker.estimate_drift_mu(lookback_seconds=5.0, velocity_5s_bps=5.0)
    assert mu_annual > 0.0


def test_merton_jump_diffusion_and_black_scholes():
    engine = MertonJumpDiffusionBinaryEngine(
        annualized_volatility=0.60,
        jump_intensity_per_sec=0.002,
        jump_mean=0.0,
        jump_std=0.003,
    )
    strike = 75000.0
    spot = 75000.0
    tau = 300.0  # 5 minutes

    p_yes_bs, p_no_bs, _ = engine.price_black_scholes_binary(strike, spot, tau)
    p_yes_merton, p_no_merton, _ = engine.price_merton_jump_binary(strike, spot, tau)

    # At ATM with 0 drift and symmetric jumps, probabilities should be near 0.50
    assert pytest.approx(p_yes_bs + p_no_bs, abs=1e-5) == 1.0
    assert pytest.approx(p_yes_merton + p_no_merton, abs=1e-5) == 1.0
    assert 0.48 <= p_yes_bs <= 0.52
    assert 0.48 <= p_yes_merton <= 0.52

    # In-the-money
    p_yes_itm, _, _ = engine.price_merton_jump_binary(strike, spot_s=75150.0, tau_seconds=120.0)
    assert p_yes_itm > 0.70

    # Expired
    p_yes_exp, p_no_exp, _ = engine.price_merton_jump_binary(strike, spot_s=75001.0, tau_seconds=0.0)
    assert p_yes_exp == 1.0
    assert p_no_exp == 0.0


def test_analytical_greeks_against_finite_differences():
    engine = MertonJumpDiffusionBinaryEngine(annualized_volatility=0.55, risk_free_rate=0.0)
    strike = 75000.0
    spot = 75050.0
    tau = 240.0

    greeks = engine.calculate_greeks(strike, spot, tau)

    # Numerical Delta
    h_s = 0.01
    p_up, _, _ = engine.price_black_scholes_binary(strike, spot + h_s, tau)
    p_dn, _, _ = engine.price_black_scholes_binary(strike, spot - h_s, tau)
    num_delta = (p_up - p_dn) / (2.0 * h_s)
    assert pytest.approx(greeks.delta, rel=1e-3) == num_delta

    # Numerical Gamma
    p_mid, _, _ = engine.price_black_scholes_binary(strike, spot, tau)
    num_gamma = (p_up - 2.0 * p_mid + p_dn) / (h_s ** 2)
    assert pytest.approx(greeks.gamma, rel=1e-2) == num_gamma

    # Pinning risk index
    assert greeks.pinning_risk_index > 0.0


def test_polymarket_arbitrage_scanner():
    scanner = PolymarketArbitrageScanner(min_taker_edge=0.015, gas_amortized_usd=0.005)

    # Case 1: CLOB YES is underpriced at 0.40 when Fair is 0.55
    # Taker fee at 0.40: 0.07 * 0.40 * 0.60 = 0.0168
    # Net edge: 0.55 - 0.40 - 0.0168 - 0.005 = +0.1282 (+12.8c)
    snipe_yes = scanner.evaluate_taker_snipe(
        fair_p_yes=0.55,
        best_ask_yes=0.40,
        best_ask_no=0.60,
    )
    assert snipe_yes.is_actionable
    assert snipe_yes.target_side == "YES"
    assert snipe_yes.net_edge > 0.10

    # Case 2: Efficient book (Fair 0.50, Asks at 0.51) -> Not actionable
    no_snipe = scanner.evaluate_taker_snipe(
        fair_p_yes=0.50,
        best_ask_yes=0.51,
        best_ask_no=0.51,
    )
    assert not no_snipe.is_actionable
    assert no_snipe.target_side == "NONE"
