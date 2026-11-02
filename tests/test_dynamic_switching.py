"""Comprehensive unit and integration tests for Dynamic Opportunity Harvesting & Market Switching.

Tests:
1. Dynamic Opportunity Scanner & Yield Optimization:
   - Marginal yield calculation:
     Y_net = Daily Reward Payout($300, v, s, D_comp) + E[Spread Capture] - E[Adverse Selection] - Gas Fees
   - Strict enforcement of $1.00 daily payout floor (Y_gross < $1.00 => $0.00)
   - Real-time ranking by net yield per dollar.
2. Opportunistic Switching Engine & Hurdle Evaluation:
   - Switching cost calculation (unquote gas + CTF merge gas + crossing spread friction)
   - Hurdle formula: Y_net(M_B) > Y_net(M_A) * (1 + hurdle) + Switching Cost (amortized over 4h)
   - Threshold rejection and approval logic.
3. Atomic Market Switching Execution:
   - Quote cancellation on exit from M_A
   - Complete-set CTF merge on M_A unlocking USDC cash
   - Migration to M_B with orderbook initialization and quote generation.
4. Concentrated Micro-Capital Allocation Model ($300 Bankroll):
   - Relaxation of 20% cap to 70%-85% ($225 on $300 bankroll)
   - 15%-30% ($75 on $300 bankroll) liquid cash reserve retention
   - Delta neutrality preservation via Regime B synthetic cross-hedger and dynamic merging.
"""

from __future__ import annotations

import pytest

from polymarketbot.paper_trading.engine import PaperTradingEngine
from polymarketbot.paper_trading.models import MarketConfig, TokenConfig
from polymarketbot.rewards.scanner import (
    MarketOpportunity,
    OpportunityScanner,
    calculate_expected_yield,
    infer_market_category,
)
from polymarketbot.rewards.switching import (
    OpportunisticSwitchingEngine,
    SwitchEngineState,
    SwitchingConfig,
    calculate_switching_cost,
    evaluate_switching_hurdle,
)
from polymarketbot.risk.manager import RiskManager
from polymarketbot.risk.models import RiskConfig


# =========================================================================
# 1. Dynamic Opportunity Scanner & Yield Optimization Tests
# =========================================================================

def test_calculate_expected_yield_basic():
    """Verify Net Expected Marginal Yield calculation with all components."""
    # Market with $1,000/day pool, max_spread = 2.0c, min_size = 50 shares
    metrics = calculate_expected_yield(
        daily_rate=1000.0,
        max_spread_cents=2.0,
        min_size=50.0,
        bankroll_usd=300.0,
        concentration_pct=0.75,  # $225 committed
        spread_cents=0.5,
        mid_price=0.50,
        competing_depth=2000.0,
        category="politics",
    )

    assert metrics["maker_score"] > 0.0
    assert metrics["gross_reward_payout"] > 0.0
    assert metrics["daily_reward_payout"] == metrics["gross_reward_payout"]
    assert metrics["passed_payout_floor"] is True
    assert metrics["expected_spread_capture"] > 0.0
    assert metrics["expected_adverse_selection"] >= 0.0
    assert metrics["expected_gas_fees"] >= 0.0

    # Net yield = payout + spread_capture - adverse_selection - gas
    expected_net = (
        metrics["daily_reward_payout"]
        + metrics["expected_spread_capture"]
        - metrics["expected_adverse_selection"]
        - metrics["expected_gas_fees"]
    )
    assert pytest.approx(metrics["net_expected_yield"], rel=1e-4) == expected_net
    assert metrics["net_yield_per_dollar"] > 0.0


def test_strict_one_dollar_payout_floor():
    """Verify that if gross rewards are < $1.00, payout strictly collapses to $0.00."""
    # Tiny pool ($5/day) with large competing depth (50,000 shares) -> gross payout << $1.00
    metrics = calculate_expected_yield(
        daily_rate=5.0,
        max_spread_cents=3.0,
        min_size=50.0,
        bankroll_usd=300.0,
        concentration_pct=0.75,
        spread_cents=1.0,
        mid_price=0.50,
        competing_depth=50000.0,
        category="default",
    )

    assert metrics["gross_reward_payout"] < 1.00
    assert metrics["passed_payout_floor"] is False
    assert metrics["daily_reward_payout"] == 0.0


def test_category_inference():
    """Verify rule-based regime category inference."""
    assert infer_market_category("Will California pass Wealth Tax in 2026?") == "politics"
    assert infer_market_category("Bitcoin price above $100,000 by December?") == "crypto"
    assert infer_market_category("Ceasefire agreement in Israel / Iran conflict?") == "geopolitics"
    assert infer_market_category("Will the Celtics win the NBA championship?") == "sports"
    assert infer_market_category("Will SpaceX launch Starship Flight 10?") == "default"


def test_scanner_ranking_by_net_yield_per_dollar():
    """Verify opportunity scanner scores and ranks candidate markets descending by net yield."""
    scanner = OpportunityScanner()
    raw_pools = [
        {
            "condition_id": "cid_low_yield",
            "question": "Low Rate Market",
            "rewards_config": [{"rate_per_day": "50.0"}],
            "rewards_min_size": "50.0",
            "rewards_max_spread": "2.0",
            "tokens": [{"token_id": "tok_low_yes", "outcome": "Yes", "price": 0.50}],
        },
        {
            "condition_id": "cid_sub_floor",
            "question": "Sub-dollar Floor Market",
            "rewards_config": [{"rate_per_day": "1.0"}],
            "rewards_min_size": "50.0",
            "rewards_max_spread": "3.0",
            "tokens": [{"token_id": "tok_floor_yes", "outcome": "Yes", "price": 0.50}],
        },
        {
            "condition_id": "cid_high_yield",
            "question": "Wealth Tax Head Pool",
            "rewards_config": [{"rate_per_day": "2500.0"}],
            "rewards_min_size": "50.0",
            "rewards_max_spread": "2.5",
            "tokens": [{"token_id": "tok_high_yes", "outcome": "Yes", "price": 0.50}],
        },
    ]

    ranked = scanner.scan_and_rank_markets(
        bankroll_usd=300.0,
        concentration_pct=0.75,
        raw_markets=raw_pools,
    )

    assert len(ranked) == 3
    # High yield must rank #1
    assert ranked[0].condition_id == "cid_high_yield"
    assert ranked[0].passed_payout_floor is True
    assert ranked[0].net_yield_per_dollar > ranked[1].net_yield_per_dollar

    # Sub-dollar pool must fail payout floor and rank last
    assert ranked[-1].condition_id == "cid_sub_floor"
    assert ranked[-1].passed_payout_floor is False
    assert ranked[-1].daily_reward_payout == 0.0


# =========================================================================
# 2. Opportunistic Switching Engine & Hurdle Evaluation Tests
# =========================================================================

def test_switching_cost_calculation():
    """Verify switching cost includes unquote gas, CTF merge gas, and spread friction."""
    # Allocated capital = $225 (75% of $300)
    # unquote_gas = $0.005, merge_gas = $0.015, friction = 0.25% of $225 = $0.5625
    # Total = 0.005 + 0.015 + 0.5625 = $0.5825
    cost = calculate_switching_cost(
        allocated_capital=225.0,
        unquote_gas_usd=0.005,
        merge_gas_usd=0.015,
        crossing_spread_friction_pct=0.0025,
    )
    assert pytest.approx(cost, rel=1e-4) == 0.5825


def test_switching_hurdle_amortization_and_evaluation():
    """Verify hurdle evaluation requires >= 20% premium over current market plus amortized friction."""
    y_current = 20.0  # $20/day
    switching_cost = 0.60  # $0.60 total friction
    # 4-hour horizon: amortized cost per day = $0.60 * (24 / 4) = $3.60/day
    # 20% hurdle on $20/d = $24.00/d.
    # Total hurdle threshold = $24.00 + $3.60 = $27.60/day.

    # Candidate 1: $25.00/day (< $27.60) -> REJECT
    should_switch, thresh, reason = evaluate_switching_hurdle(
        y_net_current=y_current,
        y_net_candidate=25.0,
        switching_cost=switching_cost,
        switch_hurdle_pct=0.20,
        horizon_hours=4.0,
        amortize_costs=True,
    )
    assert should_switch is False
    assert pytest.approx(thresh, rel=1e-3) == 27.60
    assert "fails hurdle" in reason

    # Candidate 2: $30.00/day (> $27.60) -> APPROVE
    should_switch, thresh, reason = evaluate_switching_hurdle(
        y_net_current=y_current,
        y_net_candidate=30.0,
        switching_cost=switching_cost,
        switch_hurdle_pct=0.20,
        horizon_hours=4.0,
        amortize_costs=True,
    )
    assert should_switch is True
    assert pytest.approx(thresh, rel=1e-3) == 27.60
    assert "clears hurdle" in reason


def test_switching_engine_state_machine_decision():
    """Verify OpportunisticSwitchingEngine checks and detects when to switch."""
    scanner = OpportunityScanner()
    engine = OpportunisticSwitchingEngine(
        scanner=scanner,
        config=SwitchingConfig(switch_hurdle_pct=0.20, amortization_horizon_hours=4.0),
        bankroll_usd=300.0,
        concentration_pct=0.75,
    )

    opp_a = MarketOpportunity(
        condition_id="cid_A",
        question="Market A",
        rate_per_day=500.0,
        rewards_min_size=50.0,
        rewards_max_spread=2.0,
        net_expected_yield=15.0,
        net_yield_per_dollar=0.0667,
        passed_payout_floor=True,
    )
    engine.set_active_market(opp_a)
    assert engine.state == SwitchEngineState.QUOTING

    # Scenario 1: Re-scan where market A is still best -> no switch
    should_switch, target, reason = engine.check_opportunity_switch([opp_a])
    assert should_switch is False
    assert target.condition_id == "cid_A"

    # Scenario 2: Candidate B is marginally better ($16 vs $15) -> hurdle not met
    opp_b_low = MarketOpportunity(
        condition_id="cid_B",
        question="Market B Marginal",
        rate_per_day=550.0,
        rewards_min_size=50.0,
        rewards_max_spread=2.0,
        net_expected_yield=16.0,
        net_yield_per_dollar=0.0711,
        passed_payout_floor=True,
    )
    should_switch, target, reason = engine.check_opportunity_switch([opp_b_low, opp_a])
    assert should_switch is False

    # Scenario 3: Candidate C is significantly better ($28 vs $15) -> clears hurdle
    opp_c_high = MarketOpportunity(
        condition_id="cid_C",
        question="Market C High Yield",
        rate_per_day=1500.0,
        rewards_min_size=50.0,
        rewards_max_spread=2.5,
        net_expected_yield=28.0,
        net_yield_per_dollar=0.1244,
        passed_payout_floor=True,
    )
    should_switch, target, reason = engine.check_opportunity_switch([opp_c_high, opp_a])
    assert should_switch is True
    assert target.condition_id == "cid_C"


# =========================================================================
# 3. Market Migration Execution Tests
# =========================================================================

def test_market_migration_execution(tmp_path):
    """Verify execution of market switch: quote cancellation, merge, and capital migration."""
    ledger_file = tmp_path / "test_switch_ledger.json"
    paper_engine = PaperTradingEngine(
        initial_cash_usd=300.0,
        default_quote_size=50.0,
        auto_merge_threshold=10.0,
        ledger_path=ledger_file,
    )

    # 1. Setup Market A with tokens and quotes
    mkt_a = MarketConfig(
        condition_id="cid_A",
        question="Market A Initial",
        rate_per_day=500.0,
        rewards_min_size=50.0,
        rewards_max_spread=2.0,
        tokens=[
            TokenConfig(token_id="tok_A_yes", outcome="Yes", price=0.50),
            TokenConfig(token_id="tok_A_no", outcome="No", price=0.50),
        ],
    )
    paper_engine.register_market(mkt_a)
    paper_engine.update_orderbook("tok_A_yes", [{"price": "0.49", "size": "100"}], [{"price": "0.51", "size": "100"}])
    paper_engine.update_orderbook("tok_A_no", [{"price": "0.49", "size": "100"}], [{"price": "0.51", "size": "100"}])

    # Ensure resting quotes are active on Market A
    assert paper_engine.virtual_quotes["tok_A_yes"]["bid"] is not None
    assert paper_engine.virtual_quotes["tok_A_yes"]["ask"] is not None

    # Simulate matched inventory (15 YES + 15 NO shares)
    paper_engine.positions["tok_A_yes"].shares = 15.0
    paper_engine.positions["tok_A_yes"].avg_cost = 0.50
    paper_engine.positions["tok_A_no"].shares = 15.0
    paper_engine.positions["tok_A_no"].avg_cost = 0.50
    paper_engine.cash_usd = 285.0

    # 2. Setup Switching Engine with active Market A
    scanner = OpportunityScanner()
    switch_engine = OpportunisticSwitchingEngine(
        scanner=scanner,
        config=SwitchingConfig(switch_hurdle_pct=0.20),
        bankroll_usd=300.0,
        concentration_pct=0.75,
    )
    opp_a = MarketOpportunity(
        condition_id="cid_A",
        question="Market A Initial",
        rate_per_day=500.0,
        rewards_min_size=50.0,
        rewards_max_spread=2.0,
        net_expected_yield=12.0,
        tokens=mkt_a.tokens,
    )
    switch_engine.set_active_market(opp_a)

    # 3. Candidate Market B
    opp_b = MarketOpportunity(
        condition_id="cid_B",
        question="Market B Target",
        rate_per_day=2000.0,
        rewards_min_size=50.0,
        rewards_max_spread=3.0,
        net_expected_yield=35.0,
        tokens=[
            TokenConfig(token_id="tok_B_yes", outcome="Yes", price=0.50),
            TokenConfig(token_id="tok_B_no", outcome="No", price=0.50),
        ],
    )

    # Execute switch
    switch_event = switch_engine.execute_market_switch(
        new_market=opp_b,
        engine=paper_engine,
        client=None,
        reason="Hurdle cleared",
    )

    # Verify:
    # A. Market A quotes cleanly cancelled
    assert paper_engine.virtual_quotes["tok_A_yes"]["bid"] is None
    assert paper_engine.virtual_quotes["tok_A_yes"]["ask"] is None

    # B. Complete sets merged on Market A to unlock cash
    assert paper_engine.positions["tok_A_yes"].shares == 0.0
    assert paper_engine.positions["tok_A_no"].shares == 0.0
    assert switch_event.unlocked_cash_usd == 15.0
    # Cash restored from merge ($285 + $15 - $0.015 gas fee = $299.985)
    assert paper_engine.cash_usd == pytest.approx(299.985, rel=1e-4)

    # C. Market B registered and actively quoting
    assert "cid_B" in paper_engine.markets
    assert switch_engine.active_opportunity.condition_id == "cid_B"
    assert len(switch_engine.history) == 1


# =========================================================================
# 4. Concentrated Micro-Capital Allocation Model ($300 Bankroll)
# =========================================================================

def test_concentrated_micro_capital_limits():
    """Verify 70%-85% capital concentration when bankroll <= $500 while retaining 15%-30% cash reserve."""
    # $300 bankroll with default 75% concentration and 25% reserve
    cfg = RiskConfig(
        total_bankroll_usd=300.0,
        concentrated_micro_capital=True,
        micro_capital_concentration_pct=0.75,
        micro_capital_cash_reserve_pct=0.25,
    )
    rm = RiskManager(cfg)

    # Max position cost per market should be 75% of $300 = $225.0 (relaxing 20% / $60 cap)
    assert cfg.max_position_cost_per_market == 225.0
    assert cfg.max_portfolio_inventory_usd == 225.0
    assert rm.get_hedging_cash_reserve() == 75.0  # 25% of $300 = $75 liquid reserve

    # Allowed quote under relaxed $225 limit:
    # Say we have existing cost of $150 (which would be blocked under the old $60 cap)
    # Remaining headroom is $225 - $150 = $75 -> at $0.50 price, 150 shares
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.50,
        size=50.0,
        current_pos_shares=100.0,
        current_pos_cost=150.0,
        current_portfolio_inventory_usd=150.0,
        current_cash_usd=150.0,
    )
    assert allowed is True
    assert size == 50.0

    # If cost reaches $225, market cap binds
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.50,
        size=50.0,
        current_pos_shares=200.0,
        current_pos_cost=225.0,
        current_portfolio_inventory_usd=225.0,
        current_cash_usd=75.0,
    )
    assert allowed is False
    assert size == 0.0


def test_delta_neutrality_via_synthetic_cross_hedger_and_merge(tmp_path):
    """Verify Regime B synthetic cross-hedger restores delta neutrality when inventory skews."""
    ledger_file = tmp_path / "test_hedge_ledger.json"
    risk_cfg = RiskConfig(
        total_bankroll_usd=300.0,
        enable_synthetic_hedging=True,
        q_urgency_threshold_shares=60.0,
        max_synthetic_cross_spread_cents=0.5,
        min_merge_threshold_shares=30.0,
    )
    rm = RiskManager(risk_cfg)

    paper_engine = PaperTradingEngine(
        initial_cash_usd=300.0,
        default_quote_size=50.0,
        risk_manager=rm,
        ledger_path=ledger_file,
    )

    mkt = MarketConfig(
        condition_id="cid_hedge_test",
        question="Delta Neutrality Test Market",
        rate_per_day=1000.0,
        rewards_min_size=50.0,
        rewards_max_spread=2.0,
        tokens=[
            TokenConfig(token_id="tok_yes", outcome="Yes", price=0.49),
            TokenConfig(token_id="tok_no", outcome="No", price=0.51),
        ],
    )
    paper_engine.register_market(mkt)

    # Provide order book depth on both sides
    paper_engine.update_orderbook("tok_yes", [{"price": "0.49", "size": "200"}], [{"price": "0.50", "size": "200"}])
    paper_engine.update_orderbook("tok_no", [{"price": "0.50", "size": "200"}], [{"price": "0.51", "size": "200"}])

    # Simulate an asymmetric incoming BUY fill on YES that pushes inventory past urgency threshold (70 shares)
    fill = paper_engine._execute_buy_fill(
        asset_id="tok_yes",
        fill_px=0.49,
        fill_sz=70.0,
        trigger_reason="taker_order_fill",
        ts=1000.0,
    )
    assert fill is not None

    # Synthetic cross-hedger must have detected inventory urgency (+70 YES)
    # and immediately crossed the spread on NO @ 0.51 (basis: 0.49 + 0.51 = 1.00 <= 1.005),
    # forming complete sets and triggering complete-set merge back into USDC!
    yes_pos = paper_engine.positions["tok_yes"]
    no_pos = paper_engine.positions["tok_no"]
    net_exposure = yes_pos.shares - no_pos.shares

    # Both sides were merged, returning net exposure back to delta-neutral (<= 20 shares unhedged)
    assert abs(net_exposure) <= 20.0
    # Complete set merges were recorded
    assert paper_engine.total_merges_count >= 1
