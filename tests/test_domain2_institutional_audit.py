"""Institutional Quantitative Adversarial Audit for Binary Volatility.

Verifies the mathematical rigor of the surviving Domain 2 hazard model:

1. Dynamic Hazard Rate & Empirical Binary Volatility (Quadratic Variation,
   Implied Vol, Shannon Entropy)
"""

from polymarketbot.trading.domain2.robust_hazard import BinaryVolatilityEstimator


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
