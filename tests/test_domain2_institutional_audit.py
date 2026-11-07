"""Institutional Quantitative Adversarial Audit Test Suite for Domain 2.

Verifies the mathematical rigor, dynamic adaptability, and institutional prop firm
methodology (Jane Street, Jump Trading, Citadel, AQR) across all 5 audit targets:

1. Dynamic Hazard Rate & Empirical Binary Volatility (Quadratic Variation, Implied Vol, Shannon Entropy)
2. Precautionary Liquidity Model (Continuous Miller-Orr & Cash-in-Advance VaR Inventory)
3. Microstructure Kyle Lambda & Continuous Order Book Elasticity
4. Cross-Market Multi-Factor Covariance & Dynamic CVaR Risk Budgeting
5. Stochastic UMA Settlement Delay Queue & Live Polygon PoS EIP-1559 Gas Engine
6. Global OR-Tools End-to-End Optimization under all Dynamic Mechanisms
"""

import math
import pytest
from unittest.mock import MagicMock

from polymarketbot.rewards.queue_valuation import PolygonPoSEnvironment
from polymarketbot.trading.domain2.types import (
    BinaryMarketVolatilityMetrics,
    DynamicSettlementDelayMetrics,
    DiscountOpportunity,
)
from polymarketbot.trading.domain2.robust_hazard import (
    BinaryVolatilityEstimator,
    DynamicSettlementDelayModel,
    RobustHazardModel,
    calculate_cycle_velocity,
)
from polymarketbot.trading.domain2.dynamic_sizer import (
    DynamicPositionSizer,
    DynamicSizerConfig,
    StrikeDistanceMetrics,
)
from polymarketbot.trading.domain2.ortools_allocator import (
    Domain2Candidate,
    Domain2OrtoolsAllocator,
    Domain2OptimizationResult,
)
from polymarketbot.trading.domain2.circuit_breaker import Domain2CircuitBreaker


# ==============================================================================
# AUDIT TARGET 1: Dynamic Hazard Rate & Empirical Volatility
# ==============================================================================

def test_binary_volatility_realized_quadratic_variation():
    """Verify that realized quadratic variation accurately reflects historical price volatility."""
    estimator = BinaryVolatilityEstimator()

    # Case A: Low-volatility price series (stable around 0.98, tiny drift)
    low_vol_history = [0.980, 0.981, 0.979, 0.982, 0.980, 0.981]
    # Case B: High-volatility price series (swinging widely between 0.90 and 0.98)
    high_vol_history = [0.980, 0.920, 0.970, 0.910, 0.960, 0.900]

    vol_low = estimator.calculate_quadratic_variation(low_vol_history, dt_hours=1.0)
    vol_high = estimator.calculate_quadratic_variation(high_vol_history, dt_hours=1.0)

    assert vol_low > 0.0
    assert vol_high > vol_low * 15.0  # High volatility is drastically higher

    # Full estimate using realized method
    metrics = estimator.estimate_volatility(
        category="general",
        price=0.980,
        hours_to_resolution=5.0,
        price_history=high_vol_history,
    )
    assert metrics.method == "REALIZED_QUADRATIC_VARIATION"
    assert metrics.realized_qv is not None
    assert metrics.effective_annual_vol > 0.50


def test_binary_volatility_implied_spread_and_shannon_entropy():
    """Verify that CLOB order book spread extracts implied volatility and Shannon entropy compresses at tails."""
    estimator = BinaryVolatilityEstimator()

    # 1. Implied volatility from spread
    # Tight spread (5 bps = 0.0005) vs Wide spread (200 bps = 0.02)
    iv_tight = estimator.calculate_implied_volatility(price=0.98, spread=0.0005, hours_to_resolution=2.0)
    iv_wide = estimator.calculate_implied_volatility(price=0.98, spread=0.0200, hours_to_resolution=2.0)

    assert iv_wide > iv_tight * 10.0

    # 2. Shannon binary entropy scaling: H(p) = -[p ln p + (1-p) ln(1-p)]
    # At p = 0.50 (maximum uncertainty, coin flip): entropy = ln(2) = ~0.6931, eta = 1.0
    vol_50, entropy_50 = estimator.calculate_entropy_volatility(price=0.50, category="politics")
    # At p = 0.98 (near deterministic): entropy is very low
    vol_98, entropy_98 = estimator.calculate_entropy_volatility(price=0.98, category="politics")
    # At p = 0.999 (deep terminal certainty): entropy compresses to near zero
    vol_999, entropy_999 = estimator.calculate_entropy_volatility(price=0.999, category="politics")

    assert entropy_50 > 0.69
    assert entropy_98 < 0.12
    assert entropy_999 < 0.015
    assert vol_50 > vol_98 > vol_999


def test_non_strike_information_distance_to_parity():
    """Verify non-strike binary markets calculate information distance z_info instead of a hardcoded guess."""
    sizer = DynamicPositionSizer()

    # Market A: Near-parity uncertain political race (price = $0.55, 12 hours left)
    metrics_uncertain = sizer.compute_strike_distance_metrics(
        category="politics",
        hours_to_resolution=12.0,
        contract_price=0.55,
    )
    # Market B: High-certainty political election (price = $0.99, 1 hour left)
    metrics_certain = sizer.compute_strike_distance_metrics(
        category="politics",
        hours_to_resolution=1.0,
        contract_price=0.99,
    )

    # Uncertain market must have low z-score and 0 multiplier (rejected)
    assert metrics_uncertain.z_score < 1.50
    assert metrics_uncertain.strike_multiplier == 0.0
    assert metrics_uncertain.downside_hazard > 0.20

    # High-certainty market must have high z-score and full allocation multiplier
    assert metrics_certain.z_score > 3.50
    assert metrics_certain.strike_multiplier == 1.0
    assert metrics_certain.downside_hazard <= 0.01


# ==============================================================================
# AUDIT TARGET 2: Precautionary Liquidity Model (Miller-Orr & VaR Inventory)
# ==============================================================================

def test_stochastic_cash_inventory_miller_orr_dynamics():
    """Verify Miller-Orr optimal cash return point scales with arrival intensity and turnover."""
    sizer = DynamicPositionSizer()
    bankroll = 10000.0

    # Low arrival rate (1 candidate/h, low edge 1%, fast 1h lockup)
    res_low = sizer.compute_precautionary_reserve(
        candidate_count=1,
        avg_candidate_edge=0.01,
        avg_lockup_hours=1.0,
        bankroll_usd=bankroll,
    )

    # Moderate arrival rate (6 candidates/h, moderate edge 2.5%, 2h lockup)
    res_mod = sizer.compute_precautionary_reserve(
        candidate_count=6,
        avg_candidate_edge=0.025,
        avg_lockup_hours=2.0,
        bankroll_usd=bankroll,
    )

    # Extreme arrival surge (25 candidates/h, high edge 5.0%, 4h lockup)
    res_surge = sizer.compute_precautionary_reserve(
        candidate_count=25,
        avg_candidate_edge=0.05,
        avg_lockup_hours=4.0,
        bankroll_usd=bankroll,
    )

    assert res_low < res_mod < res_surge
    assert res_low <= 0.08      # Bounded near minimum floor to maximize deployment
    assert res_surge >= 0.28    # Retains ample dry powder to capture high-edge surge


# ==============================================================================
# AUDIT TARGET 3: Continuous Microstructure Kyle Lambda & Elasticity
# ==============================================================================

def test_concave_kyle_lambda_and_continuous_depth_gamma():
    """Verify continuous dynamic depth gamma and concave market impact elasticity."""
    sizer = DynamicPositionSizer()

    # 1. Continuous dynamic depth gamma: gamma(Spread, Depth)
    # Tight liquid book: spread = 10 bps (0.0010), depth = $20,000
    _, lambda_tight = sizer.compute_kyle_market_impact_bound(
        price=0.98,
        depth_usd=20000.0,
        p_win=0.995,
        spread_bps=10.0,
    )
    # Illiquid wide book: spread = 300 bps (0.0300), depth = $500
    _, lambda_wide = sizer.compute_kyle_market_impact_bound(
        price=0.98,
        depth_usd=500.0,
        p_win=0.995,
        spread_bps=300.0,
    )

    assert lambda_wide > lambda_tight * 100.0

    # 2. Concave elasticity exponent (square-root impact alpha = 0.50)
    w_impact_concave, _ = sizer.compute_kyle_market_impact_bound(
        price=0.97,
        depth_usd=5000.0,
        p_win=0.995,
        fee_rate=0.0,
        spread_bps=100.0,
        concave_alpha=0.50,
    )
    assert w_impact_concave > 0.0
    assert w_impact_concave <= 5000.0 * 0.80  # Absorbs up to dynamic gamma


# ==============================================================================
# AUDIT TARGET 4: Cross-Market Covariance & Dynamic CVaR Risk Budgeting
# ==============================================================================

def test_multi_factor_covariance_and_cvar_risk_budgeting():
    """Verify continuous multi-factor covariance estimation and endogenous CVaR bounds."""
    sizer = DynamicPositionSizer()
    bankroll = 10000.0

    # 1. Factor loadings across multiple assets
    crypto_load = sizer.get_factor_loadings("crypto", "Will Bitcoin break $100k?")
    politics_load = sizer.get_factor_loadings("politics", "Who will win the presidential election?")
    sports_load = sizer.get_factor_loadings("sports", "Manchester City vs Arsenal FC")

    assert crypto_load["crypto"] == 1.0
    assert politics_load["politics"] == 1.0
    assert sports_load["sports"] == 0.35  # Orthogonal idiosyncratic sports match

    # 2. Dynamic CVaR bounds:
    # Asset with zero existing portfolio correlation vs asset facing massive existing correlated exposure ($3k)
    w_cvar_clean = sizer.compute_cvar_risk_budget_bound(
        p_win=0.995,
        bankroll_usd=bankroll,
        existing_correlated_exposure_usd=0.0,
        category="crypto",
    )
    w_cvar_crowded = sizer.compute_cvar_risk_budget_bound(
        p_win=0.995,
        bankroll_usd=bankroll,
        existing_correlated_exposure_usd=3000.0,  # $3k already invested in crypto consumes 90% of mdd budget
        category="crypto",
    )

    assert w_cvar_crowded < w_cvar_clean
    assert w_cvar_clean > 1000.0


# ==============================================================================
# AUDIT TARGET 5: UMA Settlement Delay Queue & Polygon PoS EIP-1559 Gas Friction
# ==============================================================================

def test_dynamic_polygon_eip1559_gas_and_queue_delay():
    """Verify live EIP-1559 gas fee conversion and base fee congestion queue delays."""
    # Normal network environment: 30 Gwei base, 40 Gwei priority, POL = $0.35
    env_normal = PolygonPoSEnvironment(base_fee_gwei=30.0, priority_fee_gwei=40.0, pol_usd_rate=0.35)
    # Severe congestion environment: 350 Gwei base fee spike!
    env_congested = PolygonPoSEnvironment(base_fee_gwei=350.0, priority_fee_gwei=60.0, pol_usd_rate=0.35)

    delay_model_normal = DynamicSettlementDelayModel(pos_env=env_normal)
    delay_model_congested = DynamicSettlementDelayModel(pos_env=env_congested)

    # 1. Gas cost check
    gas_normal = delay_model_normal.compute_redemption_gas_usd()
    gas_congested = delay_model_congested.compute_redemption_gas_usd()

    assert gas_normal < 0.003
    assert gas_congested > 0.010  # Gas spikes > 3x under network congestion!
    assert gas_congested > gas_normal * 3.0

    # 2. Relayer queue delay check
    delay_metrics_normal = delay_model_normal.calculate_turnover_delay(liveness_remaining_hours=2.0)
    delay_metrics_congested = delay_model_congested.calculate_turnover_delay(liveness_remaining_hours=2.0)

    # Congested network causes relayers to queue/delay settlements to save gas
    assert delay_metrics_congested.relayer_queue_delay_hours > delay_metrics_normal.relayer_queue_delay_hours
    assert delay_metrics_congested.effective_turnover_latency_hours > delay_metrics_normal.effective_turnover_latency_hours

    # 3. Capital velocity impact: higher latency reduces annualized turnover multiplier
    _, vel_normal = calculate_cycle_velocity(
        hours_to_resolution=0.5,
        settlement_latency_hours=delay_metrics_normal.effective_turnover_latency_hours,
    )
    _, vel_congested = calculate_cycle_velocity(
        hours_to_resolution=0.5,
        settlement_latency_hours=delay_metrics_congested.effective_turnover_latency_hours,
    )
    assert vel_normal > vel_congested


# ==============================================================================
# AUDIT TARGET 6: End-to-End OR-Tools Mathematical Optimization
# ==============================================================================

def test_ortools_allocator_with_all_dynamic_mechanisms():
    """Verify OR-Tools SCIP mathematical program optimizes using dynamic gas, volatility, and reserves."""
    # Use live EIP-1559 gas environment
    pos_env = PolygonPoSEnvironment(base_fee_gwei=40.0, priority_fee_gwei=45.0, pol_usd_rate=0.35)
    allocator = Domain2OrtoolsAllocator(
        pos_env=pos_env,
        use_dynamic_sizing=True,
        use_precautionary_reserve=True,
        use_factor_risk_budgeting=True,
    )

    candidates = [
        # Candidate 1: High edge sports match, 0.5h left, deep book, spread 30 bps
        Domain2Candidate(
            condition_id="0xSPORT_OPT",
            token_id="tok_sp",
            question="Lakers vs Celtics",
            category="sports",
            price=0.980,
            hours_to_resolution=0.5,
            depth_usd=8000.0,
            p_hat=0.998,
            spread=0.003,
            pos_env=pos_env,
        ),
        # Candidate 2: Clean politics settlement, 1.0h left, deep book, spread 20 bps
        Domain2Candidate(
            condition_id="0xPOLITICS_OPT",
            token_id="tok_pol",
            question="Election Certification",
            category="politics",
            price=0.975,
            hours_to_resolution=1.0,
            depth_usd=12000.0,
            p_hat=0.999,
            spread=0.002,
            pos_env=pos_env,
        ),
        # Candidate 3: Toxic near-parity candidate with huge hazard -> MUST REJECT
        Domain2Candidate(
            condition_id="0xTOXIC_COINFLIP",
            token_id="tok_tox",
            question="Coin Flip",
            category="crypto",
            price=0.520,
            hours_to_resolution=2.0,
            depth_usd=5000.0,
            p_hat=0.550,
            spread=0.020,
            pos_env=pos_env,
        ),
    ]

    res = allocator.optimize_allocation(candidates, bankroll_usd=10000.0)

    assert res.solver_status == "OPTIMAL"
    assert res.active_markets_count == 2
    allocated_cids = {a.condition_id for a in res.allocations}
    assert "0xSPORT_OPT" in allocated_cids
    assert "0xPOLITICS_OPT" in allocated_cids
    assert "0xTOXIC_COINFLIP" not in allocated_cids

    # Precautionary reserve was calculated via Miller-Orr
    assert res.cash_reserve_usd > 500.0
    # Redemption gas was calculated dynamically from Polygon PoS EIP-1559
    for alloc in res.allocations:
        assert alloc.redemption_gas_usd > 0.0
        assert alloc.redemption_gas_usd < 0.010
