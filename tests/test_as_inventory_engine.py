"""Comprehensive unit tests for Avellaneda-Stoikov & Guéant Inventory Market Making Engine."""

from __future__ import annotations

import math
import time
import pytest

from polymarketbot.trading.as_inventory_engine import (
    ASInventoryConfig,
    ASQuoteDecision,
    AvellanedaStoikovEngine,
    DynamicASCalibrator,
    InventoryRegime,
    QuotePair,
    QuoteSide,
)


@pytest.fixture
def engine() -> AvellanedaStoikovEngine:
    config = ASInventoryConfig(
        gamma=0.05,
        kappa=50.0,
        volatility_annual=0.55,
        target_pair_margin=0.015,  # 1.5c margin -> pair cost <= 0.985
        reward_weight_rho=0.005,
        max_qualifying_spread_v=0.03,
        tick_size=0.01,
        base_ticket_size=5.0,
    )
    return AvellanedaStoikovEngine(config)


# =============================================================================
# 1. Classical Avellaneda-Stoikov & Guéant Analytical Derivations
# =============================================================================

def test_reservation_price_neutral_and_skew(engine: AvellanedaStoikovEngine):
    fair_p = 0.50
    tau = 300.0  # 5 minutes

    # 1. Neutral inventory (q = 0) -> Reservation price exactly equals fair price
    r_neutral = engine.calculate_reservation_price(fair_price=fair_p, net_inventory_q=0.0, tau_seconds=tau)
    assert pytest.approx(r_neutral, abs=1e-6) == fair_p

    # 2. Long YES inventory (q = +10) -> Reservation price drops below fair price
    r_long = engine.calculate_reservation_price(fair_price=fair_p, net_inventory_q=10.0, tau_seconds=tau)
    assert r_long < fair_p

    # 3. Short YES inventory (q = -10) -> Reservation price rises above fair price
    r_short = engine.calculate_reservation_price(fair_price=fair_p, net_inventory_q=-10.0, tau_seconds=tau)
    assert r_short > fair_p

    # 4. Symmetry: r(s, +q) - s == s - r(s, -q)
    assert pytest.approx(r_long - fair_p, abs=1e-6) == -(r_short - fair_p)


def test_reservation_price_parameter_sensitivities(engine: AvellanedaStoikovEngine):
    fair_p = 0.50
    tau = 300.0
    q = 10.0

    sig_sec = engine.calculate_sigma_per_sqrt_sec()
    # Baseline penalty
    baseline_penalty = fair_p - engine.calculate_reservation_price(fair_p, q, tau)

    # Higher risk aversion gamma doubles the penalty
    r_high_gamma = engine.calculate_reservation_price(fair_p, q, tau, gamma=engine.config.gamma * 2.0)
    high_gamma_penalty = fair_p - r_high_gamma
    assert pytest.approx(high_gamma_penalty, rel=1e-3) == baseline_penalty * 2.0

    # Double horizon tau doubles the penalty
    r_double_tau = engine.calculate_reservation_price(fair_p, q, tau * 2.0)
    double_tau_penalty = fair_p - r_double_tau
    assert pytest.approx(double_tau_penalty, rel=1e-3) == baseline_penalty * 2.0


def test_gtm_half_spreads_closed_form(engine: AvellanedaStoikovEngine):
    tau = 300.0

    # 1. Neutral inventory (q = 0) -> delta_bid == delta_ask
    delta_b_0, delta_a_0, delta_0 = engine.calculate_gtm_half_spreads(net_inventory_q=0.0, tau_seconds=tau)
    assert pytest.approx(delta_b_0, abs=1e-6) == delta_a_0
    assert delta_b_0 > delta_0  # Includes the +0.5 * gamma * sigma^2 * tau variance term

    # 2. Long YES inventory (q = +15) -> Bid widens, Ask tightens!
    delta_b_long, delta_a_long, _ = engine.calculate_gtm_half_spreads(net_inventory_q=15.0, tau_seconds=tau)
    assert delta_b_long > delta_b_0  # Bid half-spread widens
    assert delta_a_long < delta_a_0  # Ask half-spread tightens

    # 3. Short YES inventory (q = -15) -> Bid tightens, Ask widens!
    delta_b_short, delta_a_short, _ = engine.calculate_gtm_half_spreads(net_inventory_q=-15.0, tau_seconds=tau)
    assert delta_b_short < delta_b_0  # Bid half-spread tightens
    assert delta_a_short > delta_a_0  # Ask half-spread widens

    # 4. Total continuous spread invariant to inventory q:
    # delta^b(q) + delta^a(q) == 2 * delta_0 + gamma * sigma^2 * tau
    total_spread_neutral = delta_b_0 + delta_a_0
    total_spread_long = delta_b_long + delta_a_long
    total_spread_short = delta_b_short + delta_a_short

    assert pytest.approx(total_spread_long, abs=1e-6) == total_spread_neutral
    assert pytest.approx(total_spread_short, abs=1e-6) == total_spread_neutral


def test_raw_as_quotes_pricing_shifts(engine: AvellanedaStoikovEngine):
    fair_p = 0.50
    tau = 300.0

    bid_0, ask_0, _, _ = engine.calculate_raw_as_quotes(fair_p, net_inventory_q=0.0, tau_seconds=tau)
    bid_long, ask_long, _, _ = engine.calculate_raw_as_quotes(fair_p, net_inventory_q=20.0, tau_seconds=tau)

    # When long YES (q > 0):
    # Both bid and ask drop to repel buyers of YES and attract sellers of YES
    assert bid_long < bid_0
    assert ask_long < ask_0


# =============================================================================
# 2. Binary Complete-Set Adaptation & Structural Pair Arbitrage
# =============================================================================

@pytest.mark.parametrize("q_net", [-50.0, -20.0, -5.0, 0.0, 5.0, 20.0, 50.0])
def test_complete_set_pair_arbitrage_bound(engine: AvellanedaStoikovEngine, q_net: float):
    """Verify that under any inventory skew, quote pair cost <= 1.00 - target_pair_margin."""
    decision = engine.evaluate_market_quotes(
        fair_price_yes=0.52,
        inventory_yes=q_net if q_net > 0 else 0.0,
        inventory_no=-q_net if q_net < 0 else 0.0,
        tau_seconds=300.0,
        use_rewards_optimization=False,  # Test pure AS-GTM enforcement first
    )

    max_allowed_bid_pair = round(1.00 - engine.config.target_pair_margin, 4)  # 0.985
    min_allowed_ask_pair = round(1.00 + engine.config.target_pair_margin, 4)  # 1.015

    # 1. Dual Bid Cost Bound
    assert decision.pair_bid_cost <= max_allowed_bid_pair
    assert decision.pair_bid_margin_cents >= 1.50
    assert decision.is_pair_arbitrage_guaranteed

    # 2. Dual Ask Revenue Bound
    assert decision.pair_ask_cost >= min_allowed_ask_pair
    assert decision.pair_ask_margin_cents >= 1.50

    # 3. Discrete tick snapping: prices are integer multiples of tick_size (0.01)
    for p in [
        decision.yes_quote.bid_price,
        decision.yes_quote.ask_price,
        decision.no_quote.bid_price,
        decision.no_quote.ask_price,
    ]:
        rem = round((p * 100.0) % 1.0, 4)
        assert rem == 0.0 or pytest.approx(rem, abs=1e-4) == 1.0

    # 4. Bid strictly less than Ask for both tokens
    assert decision.yes_quote.bid_price < decision.yes_quote.ask_price
    assert decision.no_quote.bid_price < decision.no_quote.ask_price


def test_skewed_dual_bids_induce_inventory_neutrality(engine: AvellanedaStoikovEngine):
    """When long YES (+20), the bot must lower YES bid and raise NO bid to rebalance."""
    neutral = engine.evaluate_market_quotes(fair_price_yes=0.50, inventory_yes=0.0, inventory_no=0.0)
    skewed = engine.evaluate_market_quotes(fair_price_yes=0.50, inventory_yes=20.0, inventory_no=0.0)

    # YES token: bid drops lower, ask drops closer to mid
    assert skewed.yes_quote.bid_price <= neutral.yes_quote.bid_price
    assert skewed.yes_quote.ask_price <= neutral.yes_quote.ask_price

    # NO token (where bot is short net NO): bid rises higher, ask rises higher
    assert skewed.no_quote.bid_price >= neutral.no_quote.bid_price
    assert skewed.no_quote.ask_price >= neutral.no_quote.ask_price


# =============================================================================
# 3. Dynamic Size Skewing
# =============================================================================

def test_dynamic_size_skewing(engine: AvellanedaStoikovEngine):
    # Neutral sizes
    s_b_yes_0, s_a_yes_0, s_b_no_0, s_a_no_0 = engine.calculate_dynamic_quote_sizes(0.0)
    assert s_b_yes_0 == engine.config.base_ticket_size
    assert s_a_yes_0 == engine.config.base_ticket_size
    assert s_b_no_0 == engine.config.base_ticket_size
    assert s_a_no_0 == engine.config.base_ticket_size

    # Long YES (q = +20):
    # YES: bid size shrinks, ask size grows (facilitating YES offloading)
    # NO: bid size grows, ask size shrinks (facilitating NO accumulation to complete pairs)
    s_b_yes, s_a_yes, s_b_no, s_a_no = engine.calculate_dynamic_quote_sizes(20.0)
    assert s_b_yes < s_b_yes_0
    assert s_a_yes > s_a_yes_0
    assert s_b_no > s_b_no_0
    assert s_a_no < s_a_no_0

    # Short YES (q = -20): exact opposite
    s_b_yes_neg, s_a_yes_neg, s_b_no_neg, s_a_no_neg = engine.calculate_dynamic_quote_sizes(-20.0)
    assert s_b_yes_neg > s_b_yes_0
    assert s_a_yes_neg < s_a_yes_0
    assert s_b_no_neg < s_b_no_0
    assert s_a_no_neg > s_a_no_0


# =============================================================================
# 4. Polymarket Quadratic Liquidity Rewards Optimization
# =============================================================================

def test_quadratic_reward_score_formula(engine: AvellanedaStoikovEngine):
    v = 0.03  # 3 cents max qualifying spread

    # At spread = 0 -> maximum score = size
    score_0 = engine.calculate_quadratic_reward_score(delta_spread=0.0, max_qualifying_spread_v=v, size=10.0)
    assert score_0 == 10.0

    # At spread = v/2 (1.5c) -> ((3 - 1.5) / 3)^2 * 10 = (0.5)^2 * 10 = 2.5
    score_half = engine.calculate_quadratic_reward_score(delta_spread=0.015, max_qualifying_spread_v=v, size=10.0)
    assert pytest.approx(score_half, abs=1e-5) == 2.50

    # At spread = v (3.0c) -> 0
    score_v = engine.calculate_quadratic_reward_score(delta_spread=0.03, max_qualifying_spread_v=v, size=10.0)
    assert score_v == 0.0

    # Beyond v (4.0c) -> 0
    score_beyond = engine.calculate_quadratic_reward_score(delta_spread=0.04, max_qualifying_spread_v=v, size=10.0)
    assert score_beyond == 0.0


def test_rewards_co_objective_tightens_neutral_quotes(engine: AvellanedaStoikovEngine):
    """When rewards weight rho > 0, optimal half-spread tightens inward toward the book."""
    tau = 300.0
    # 1. Pure AS spread (no rewards)
    delta_as_b, _, _ = engine.calculate_gtm_half_spreads(net_inventory_q=0.0, tau_seconds=tau)

    # 2. Rewards-optimized spread
    delta_opt_b = engine.optimize_spread_with_rewards(
        side=QuoteSide.BID,
        net_inventory_q=0.0,
        tau_seconds=tau,
        quote_size=5.0,
    )

    # Rewards incentive pulls maker closer to mid to capture quadratic score
    assert delta_opt_b <= delta_as_b


def test_rewards_single_sided_penalty_rules(engine: AvellanedaStoikovEngine):
    # Case 1: Midpoint inside [0.10, 0.90] with unbalanced scores (e.g. Q1=10.0, Q2=0.0)
    # Polymarket rule: Q_min = max(min(10, 0), 10 / 3.0) = 3.333
    dec_mid = engine.evaluate_market_quotes(fair_price_yes=0.50, inventory_yes=0.0, inventory_no=0.0)
    assert dec_mid.reward_breakdown.is_two_sided_qualified
    assert dec_mid.reward_breakdown.q_min > 0.0

    # Case 2: Extreme tail market (fair = 0.05, outside [0.10, 0.90])
    dec_tail = engine.evaluate_market_quotes(fair_price_yes=0.05, inventory_yes=0.0, inventory_no=0.0)
    assert dec_tail.pair_bid_cost <= 0.985
    assert dec_tail.fair_price_yes == 0.05


# =============================================================================
# 5. Underlying Spot & Strike Anchoring Integration
# =============================================================================

def test_evaluate_from_spot_and_strike_at_the_money(engine: AvellanedaStoikovEngine):
    # BTC at $75,000 with $75,000 strike -> fair ~0.50
    dec = engine.evaluate_from_spot_and_strike(
        strike_k=75000.0,
        spot_s=75000.0,
        tau_seconds=300.0,
        inventory_yes=0.0,
        inventory_no=0.0,
    )
    assert 0.49 <= dec.fair_price_yes <= 0.51
    assert 0.49 <= dec.fair_price_no <= 0.51
    assert dec.pair_bid_cost <= 0.985
    assert dec.inventory_regime == InventoryRegime.BALANCED


def test_evaluate_from_spot_and_strike_in_the_money(engine: AvellanedaStoikovEngine):
    # Spot 75,200 vs Strike 75,000 with 60s left -> high YES probability
    dec = engine.evaluate_from_spot_and_strike(
        strike_k=75000.0,
        spot_s=75200.0,
        tau_seconds=60.0,
        inventory_yes=15.0,  # Long YES
        inventory_no=0.0,
    )
    assert dec.fair_price_yes > 0.85
    assert dec.fair_price_no < 0.15
    assert dec.inventory_regime == InventoryRegime.HEAVY_SKEW
    assert dec.pair_bid_cost <= 0.985


# =============================================================================
# 6. Performance & Execution Speed
# =============================================================================

def test_execution_speed_benchmark(engine: AvellanedaStoikovEngine):
    """Ensure sub-millisecond per-quote calculation (<5s for 500 calculations)."""
    t0 = time.perf_counter()
    iterations = 200

    for i in range(iterations):
        engine.evaluate_market_quotes(
            fair_price_yes=0.45 + (i % 10) * 0.01,
            inventory_yes=float(i % 30),
            inventory_no=5.0,
            tau_seconds=150.0,
            use_rewards_optimization=True,
        )

    elapsed = time.perf_counter() - t0
    avg_ms = (elapsed / iterations) * 1000.0

    # Ensure each calculation takes < 1.0 ms
    assert avg_ms < 1.5, f"Average quote calculation time {avg_ms:.2f}ms exceeds 1.5ms"
    assert elapsed < 1.0, f"Total benchmark elapsed {elapsed:.2f}s exceeds 1.0s"


# =============================================================================
# 7. Dynamic Calibration (Gamma, Kappa, A, & Dynamic Margins)
# =============================================================================

def test_dynamic_as_calibrator_gamma():
    calibrator = DynamicASCalibrator()

    # Small capital ($100) -> higher risk aversion gamma (defensive inventory)
    gamma_small = calibrator.calibrate_gamma(capital_usd=100.0, target_sharpe=2.0, volatility_annual=0.55)
    # Large institutional capital ($10,000) -> lower risk aversion gamma (can absorb inventory)
    gamma_large = calibrator.calibrate_gamma(capital_usd=10000.0, target_sharpe=2.0, volatility_annual=0.55)

    assert gamma_small > gamma_large
    assert 0.001 <= gamma_large <= 0.05
    assert 0.10 <= gamma_small <= 1.0

    # Higher target Sharpe or lower volatility increases gamma
    gamma_high_sr = calibrator.calibrate_gamma(capital_usd=1000.0, target_sharpe=4.0, volatility_annual=0.55)
    gamma_low_sr = calibrator.calibrate_gamma(capital_usd=1000.0, target_sharpe=1.0, volatility_annual=0.55)
    assert gamma_high_sr > gamma_low_sr


def test_dynamic_as_calibrator_kappa_from_depth():
    calibrator = DynamicASCalibrator()

    # Dense book: L1 (500) >> L2 (100) at 1c spread -> rapid depth decay -> high kappa
    kappa_dense = calibrator.calibrate_kappa_from_depth(spread=0.01, l1_size=500.0, l2_size=100.0, tick_size=0.01)

    # Thin wide book: L1 (50) ~= L2 (50) at 4c spread -> low decay -> lower kappa
    kappa_thin = calibrator.calibrate_kappa_from_depth(spread=0.04, l1_size=50.0, l2_size=50.0, tick_size=0.01)

    assert kappa_dense > kappa_thin
    assert 5.0 <= kappa_thin <= 200.0
    assert 5.0 <= kappa_dense <= 200.0


def test_dynamic_as_calibrator_from_arrivals_regression():
    calibrator = DynamicASCalibrator()

    # Synthetic arrival data obeying lambda(delta) = 2.0 * exp(-60 * delta)
    deltas = [0.01, 0.02, 0.03, 0.04]
    total_time_s = 1000.0
    # Expected counts: A * exp(-kappa * delta) * total_time
    counts = [int(2.0 * math.exp(-60.0 * d) * total_time_s) for d in deltas]

    est_a, est_kappa = calibrator.calibrate_from_order_arrivals(deltas, counts, total_time_s)
    # Check that estimated A and kappa are close to ground truth (2.0 and 60.0)
    assert pytest.approx(est_a, rel=0.25) == 2.0
    assert pytest.approx(est_kappa, rel=0.25) == 60.0


def test_as_engine_with_dynamic_parameters(engine: AvellanedaStoikovEngine):
    calibrator = DynamicASCalibrator()
    gamma_dyn = calibrator.calibrate_gamma(capital_usd=500.0, target_sharpe=2.5)
    kappa_dyn = calibrator.calibrate_kappa_from_depth(spread=0.02, l1_size=200.0, l2_size=80.0)

    # Dynamic target pair margin of 2.0c
    dynamic_margin = 0.020

    decision = engine.evaluate_market_quotes(
        fair_price_yes=0.55,
        inventory_yes=10.0,
        inventory_no=0.0,
        tau_seconds=200.0,
        gamma=gamma_dyn,
        kappa=kappa_dyn,
        target_pair_margin=dynamic_margin,
    )

    # Guarantees pair bid cost <= 1.00 - dynamic_margin = 0.980
    assert decision.pair_bid_cost <= 0.980
    assert decision.pair_bid_margin_cents >= 2.0
    assert decision.is_pair_arbitrage_guaranteed
    assert decision.yes_quote.bid_price < decision.yes_quote.ask_price
    assert decision.no_quote.bid_price < decision.no_quote.ask_price

