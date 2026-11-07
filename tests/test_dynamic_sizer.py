"""Unit tests for Institutional Dynamic Position Sizer & Risk Budgeting."""

import pytest
import math
from polymarketbot.trading.domain2.dynamic_sizer import (
    DynamicPositionSizer,
    DynamicSizerConfig,
    StrikeDistanceMetrics,
)


def test_strike_distance_metrics_near_strike():
    """Verify that near-strike coin-flip markets (z < 1.5 sigma) are rejected."""
    sizer = DynamicPositionSizer()

    # BTC Spot = 78,200, Strike = 78,000, 1.0 hour left, 65% annualized vol
    # z = ln(78200/78000) / (0.65 * sqrt(1/8760)) = 0.00256 / (0.65 * 0.01068) = 0.00256 / 0.00694 = ~0.37 sigma
    metrics = sizer.compute_strike_distance_metrics(
        category="crypto",
        hours_to_resolution=1.0,
        spot_price=78200.0,
        strike_price=78000.0,
        is_uma_proposed=False,
    )

    assert metrics.is_strike_applicable is True
    assert metrics.z_score < 1.0
    assert metrics.downside_hazard > 0.20  # Over 20% downside hazard!
    assert metrics.strike_multiplier == 0.0  # Multiplier drops to zero!


def test_strike_distance_metrics_deep_itm():
    """Verify that deep-ITM 4+ sigma markets unlock full allocation capacity."""
    sizer = DynamicPositionSizer()

    # BTC Spot = 85,000, Strike = 78,000, 1.0 hour left
    # z = ln(85000/78000) / (0.65 * sqrt(1/8760)) = 0.0859 / 0.00694 = ~12.3 sigma
    metrics = sizer.compute_strike_distance_metrics(
        category="crypto",
        hours_to_resolution=1.0,
        spot_price=85000.0,
        strike_price=78000.0,
        is_uma_proposed=False,
    )

    assert metrics.is_strike_applicable is True
    assert metrics.z_score > 4.0
    assert metrics.downside_hazard < 0.001
    assert metrics.strike_multiplier == 1.0  # Full capacity unlocked!


def test_continuous_kelly_downside_semi_variance():
    """Verify that downside semi-variance penalizes risky bets and rewards safe yield."""
    sizer = DynamicPositionSizer()
    bankroll = 10000.0

    # Market A: 96c price, 97c win prob (1% edge, 3% downside hazard)
    # Market B: 96c price, 99.9% win prob (3.9% edge, 0.1% downside hazard)
    w_a, f_a, semi_a = sizer.compute_continuous_kelly_bound(
        p_win=0.97, price=0.96, bankroll_usd=bankroll, strike_multiplier=1.0
    )
    w_b, f_b, semi_b = sizer.compute_continuous_kelly_bound(
        p_win=0.999, price=0.96, bankroll_usd=bankroll, strike_multiplier=1.0
    )

    assert semi_a > semi_b * 10  # Market A has vastly higher downside semi-variance
    assert w_b > w_a             # Market B receives substantially larger Kelly allocation


def test_kyle_market_impact_bound():
    """Verify Kyle's lambda bounds trade size based on depth and spread."""
    sizer = DynamicPositionSizer()

    # Deep book ($10,000 depth) vs Shallow book ($200 depth)
    w_deep, lambda_deep = sizer.compute_kyle_market_impact_bound(
        price=0.96, depth_usd=10000.0, p_win=0.99
    )
    w_shallow, lambda_shallow = sizer.compute_kyle_market_impact_bound(
        price=0.96, depth_usd=200.0, p_win=0.99
    )

    assert lambda_shallow > lambda_deep * 20
    assert w_deep > w_shallow


def test_dynamic_single_bet_bound_btc_reversal_vs_clean():
    """Verify dynamic sizing on Bitcoin reversal market vs clean settled market."""
    sizer = DynamicPositionSizer()
    bankroll = 10000.0

    # 1. Dangerous Bitcoin Reversal Market (BTC $78k strike, spot $78,250, 1h left, z ~ 0.4 sigma)
    res_btc = sizer.compute_dynamic_single_bet_bound(
        category="crypto",
        price=0.965,
        hours_to_resolution=1.0,
        depth_usd=5000.0,
        p_win=0.65,  # Real hazard is 35%!
        bankroll_usd=bankroll,
        spot_price=78250.0,
        strike_price=78000.0,
        is_uma_proposed=False,
    )

    # 2. Clean settled market (sports blowout or certified election, 4+ sigma)
    res_clean = sizer.compute_dynamic_single_bet_bound(
        category="sports",
        price=0.965,
        hours_to_resolution=0.5,
        depth_usd=8000.0,
        p_win=0.9995,
        bankroll_usd=bankroll,
        is_uma_proposed=True,
    )

    # BTC reversal market MUST be rejected (0 allocation)
    assert res_btc["w_max_usd"] == 0.0
    assert res_btc["limiting_factor"] == "STRIKE_DIFFUSION_REJECTION"

    # Clean settled market MUST be allocated substantially above the static 10% cap (up to 25%)
    assert res_clean["w_max_usd"] > 1000.0  # > 10% of $10,000!
    assert res_clean["w_max_usd"] <= 2500.0 # <= 25% physical ceiling


def test_precautionary_liquidity_sizing():
    """Verify dynamic cash reserve adjusts to Poisson opportunity arrival rate."""
    sizer = DynamicPositionSizer()

    # Case 1: Quiet market (few incoming opportunities) -> Low reserve (preserves capital velocity)
    res_quiet = sizer.compute_precautionary_reserve(
        candidate_count=1,
        avg_candidate_edge=0.01,
        avg_lockup_hours=2.0,
    )

    # Case 2: Opportunity surge (many high-edge opportunities) -> High reserve (precautionary dry powder)
    res_surge = sizer.compute_precautionary_reserve(
        candidate_count=20,
        avg_candidate_edge=0.04,
        avg_lockup_hours=3.0,
    )

    assert res_quiet < 0.12
    assert res_surge > 0.25
    assert res_quiet >= sizer.config.min_reserve_pct
    assert res_surge <= sizer.config.max_reserve_pct


def test_factor_risk_budgets_and_loadings():
    """Verify Barra-style factor risk budgeting across categories."""
    sizer = DynamicPositionSizer()
    bankroll = 10000.0

    loadings_btc = sizer.get_factor_loadings("crypto", "Will Bitcoin reach $80,000?")
    loadings_election = sizer.get_factor_loadings("politics", "Will Pennsylvania vote Republican?")
    loadings_sports = sizer.get_factor_loadings("sports", "Edmonton Oilers vs Boston Bruins")

    assert loadings_btc["crypto"] == 1.0
    assert loadings_election["politics"] == 1.0
    assert loadings_sports["sports"] == 0.35  # Low cross-correlation for sports

    budgets = sizer.compute_factor_risk_budgets(bankroll)
    assert budgets["crypto"] == 2000.0  # 20%
    assert budgets["politics"] == 3000.0 # 30%
    assert budgets["sports"] == 4000.0   # 40%
