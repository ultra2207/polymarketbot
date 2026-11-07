"""Tests for the Binance High-Frequency Quantitative Pricing Engine."""

from __future__ import annotations

import math
import pytest

from polymarketbot.trading.binance_quant_engine import (
    BinaryOptionGreeks,
    CrossVenueLeadLagKalmanFilter,
    HighFrequencyEWMAVolEstimator,
    HighFrequencyVolEstimator,
    KlineBar,
    MertonJumpDiffusionBinaryEngine,
    MicroPriceEstimator,
    MultiLevelOrderFlowImbalanceTracker,
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


def test_yang_zhang_volatility_estimator():
    # Multi-bar synthetic series with overnight gaps and intra-bar range
    bars = [
        KlineBar(open_time_s=0, open=100.0, high=102.5, low=99.0, close=101.0),
        KlineBar(open_time_s=60, open=102.0, high=103.5, low=101.5, close=103.0),  # Overnight jump 101.0 -> 102.0
        KlineBar(open_time_s=120, open=102.5, high=104.0, low=102.0, close=103.8), # Jump down 103.0 -> 102.5
        KlineBar(open_time_s=180, open=104.0, high=105.0, low=103.5, close=104.5),
    ]

    var_yz = HighFrequencyVolEstimator.yang_zhang_variance(bars)
    assert var_yz > 0.0

    ann_vol_yz = HighFrequencyVolEstimator.annualize_bar_volatility(var_yz, bar_duration_seconds=60.0)
    assert 0.10 <= ann_vol_yz <= 2.50


def test_high_frequency_ewma_vol_estimator():
    estimator = HighFrequencyEWMAVolEstimator(
        half_life_seconds=30.0,
        initial_volatility=0.55,
        noise_correction=True,
    )
    t0 = 1000.0
    # First tick initializes
    vol_init = estimator.update_tick(price=75000.0, timestamp=t0)
    assert pytest.approx(vol_init, rel=1e-2) == 0.55

    # Low volatility drift: 10 ticks with 1 bps random moves
    for i in range(1, 15):
        price = 75000.0 + (i % 2) * 5.0
        estimator.update_tick(price=price, timestamp=t0 + i * 2.0)

    vol_low = estimator.get_current_volatility()
    assert 0.10 <= vol_low <= 1.00

    # Sudden high-frequency volatility burst: massive 200 bps jump in 1 second
    estimator.update_tick(price=76500.0, timestamp=t0 + 31.0)
    vol_spike = estimator.get_current_volatility()
    assert vol_spike > vol_low

    # Clamping bounds [0.10, 2.50]
    assert 0.10 <= vol_spike <= 2.50


def test_online_dynamic_ofi_beta_calibration():
    tracker = OrderFlowImbalanceTracker(history_seconds=60.0, beta_decay=0.90)
    t0 = 1000.0

    # Stream BBO ticks where positive OFI produces systematic upward price moves
    tracker.update(bid_price=75000.0, bid_size=10.0, ask_price=75001.0, ask_size=10.0, timestamp=t0)

    for i in range(1, 15):
        # Bid price steps up and bid size expands -> positive OFI with positive price change
        bid_p = 75000.0 + i * 1.0
        ask_p = 75001.0 + i * 1.0
        inc = tracker.update(bid_price=bid_p, bid_size=15.0 + i, ask_price=ask_p, ask_size=10.0, timestamp=t0 + i * 2.0)
        assert inc > 0.0

    # Check that beta_OFI was dynamically calibrated from online covariance
    assert tracker._calibrated_beta_ofi is not None
    assert tracker._calibrated_beta_ofi > 0.0

    # Drift mu using calibrated beta
    drift_mu = tracker.estimate_drift_mu(lookback_seconds=10.0, use_calibrated_beta=True)
    assert drift_mu > 0.0


def test_multilevel_ofi_tracker():
    multi_tracker = MultiLevelOrderFlowImbalanceTracker(max_levels=3, decay_alpha=0.5)
    t0 = 1000.0

    bids_t0 = [OrderBookLevel(100.0, 10.0), OrderBookLevel(99.0, 20.0), OrderBookLevel(98.0, 30.0)]
    asks_t0 = [OrderBookLevel(101.0, 10.0), OrderBookLevel(102.0, 20.0), OrderBookLevel(103.0, 30.0)]
    inc0 = multi_tracker.update(bids_t0, asks_t0, timestamp=t0)
    assert inc0 == 0.0  # Initial baseline

    # Bids expand across levels 1, 2, and 3
    bids_t1 = [OrderBookLevel(100.0, 25.0), OrderBookLevel(99.0, 40.0), OrderBookLevel(98.0, 50.0)]
    asks_t1 = [OrderBookLevel(101.0, 10.0), OrderBookLevel(102.0, 20.0), OrderBookLevel(103.0, 30.0)]
    inc1 = multi_tracker.update(bids_t1, asks_t1, timestamp=t0 + 2.0)
    assert inc1 > 0.0  # Heavy positive multi-level order flow

    rate = multi_tracker.get_multi_ofi_rate(lookback_seconds=5.0)
    assert rate > 0.0


def test_cross_venue_lead_lag_kalman_filter():
    kf = CrossVenueLeadLagKalmanFilter(
        initial_beta=1.0,
        initial_alpha=0.0,
        process_noise_beta=1e-4,
        measurement_noise=1e-3,
    )
    t0 = 1000.0

    # Stream ticks where Polymarket probability changes by 1.8x the Binance log return
    true_beta = 1.8
    s0 = 75000.0
    p0 = 0.50

    kf.update(binance_price=s0, poly_prob=p0, timestamp=t0)

    for i in range(1, 25):
        s_i = s0 * (1.0 + 0.0002 * i)  # +2 bps each step
        d_ln_s = math.log(s_i / (s0 * (1.0 + 0.0002 * (i - 1))))
        p_i = p0 + true_beta * math.log(s_i / s0)
        state = kf.update(binance_price=s_i, poly_prob=p_i, timestamp=t0 + i * 1.0)

    # Beta should have adapted towards 1.8
    assert state.beta > 1.2
    assert state.beta_variance < 0.1  # Posterior variance narrowed


def test_analytical_transmission_elasticity_and_dynamic_surge():
    spot = 75000.0
    strike = 75000.0
    vol = 0.55

    # 1. At ATM with short horizon (30s) -> Elasticity explodes
    elasticity_short = CrossVenueLeadLagKalmanFilter.calculate_analytical_elasticity(
        spot_s=spot, strike_k=strike, tau_seconds=30.0, annualized_volatility=vol
    )
    # At ATM with long horizon (3600s) -> Elasticity is much lower
    elasticity_long = CrossVenueLeadLagKalmanFilter.calculate_analytical_elasticity(
        spot_s=spot, strike_k=strike, tau_seconds=3600.0, annualized_volatility=vol
    )
    assert elasticity_short > elasticity_long

    # 2. Dynamic surge threshold tightens as expiry approaches
    thresh_short = CrossVenueLeadLagKalmanFilter.calculate_dynamic_surge_threshold_bps(
        spot_s=spot, strike_k=strike, tau_seconds=30.0, annualized_volatility=vol, prob_shock_hurdle=0.015
    )
    thresh_long = CrossVenueLeadLagKalmanFilter.calculate_dynamic_surge_threshold_bps(
        spot_s=spot, strike_k=strike, tau_seconds=3600.0, annualized_volatility=vol, prob_shock_hurdle=0.015
    )
    assert thresh_short < thresh_long

