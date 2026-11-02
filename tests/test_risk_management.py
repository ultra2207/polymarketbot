"""Comprehensive unit and integration tests for Polymarket Risk Management System.

Tests:
1. Hard Capital & Position Limits ($300 bankroll, $60/market, 150 shares/token).
2. Max Drawdown Circuit Breakers ($15/5% daily loss, $30/10% drawdown).
3. Price Jump & Volatility Circuit Breaker (>2.0c within 60s -> 5m cooldown).
4. Asymmetric Toxicity / Inventory Skew Limiter (widen/suppress after consecutive fills).
5. Gas & Complete-Set Merge Manager (25-50 shares scaled, gas fee <= 5% of spread).
6. Emergency Unquote / Kill-Switch & Heartbeat Monitor.
7. Integration with PaperTradingEngine and LiquidityRewardsBotEngine.
"""

from __future__ import annotations

import time
import pytest
from pathlib import Path

from polymarketbot.risk.models import (
    CircuitBreakerReason,
    RiskConfig,
    RiskStatus,
)
from polymarketbot.risk.manager import (
    GasMergeManager,
    PriceVolatilityMonitor,
    RiskManager,
    ToxicityTracker,
)
from polymarketbot.paper_trading.engine import PaperTradingEngine
from polymarketbot.paper_trading.models import MarketConfig, TokenConfig
from polymarketbot.rewards.engine import (
    LiquidityRewardsBotEngine,
    MarketRewardParameters,
)


# =========================================================================
# 1. Hard Capital & Position Limits
# =========================================================================

def test_hard_capital_limits_quote_size_capping():
    """Verify max single order size is capped (default 50 shares)."""
    cfg = RiskConfig(total_bankroll_usd=300.0, max_quote_size_shares=50.0)
    rm = RiskManager(cfg)

    # Base quote size requested: 100 shares
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.50,
        size=100.0,
        current_pos_shares=0.0,
        current_pos_cost=0.0,
        current_portfolio_inventory_usd=0.0,
        current_cash_usd=300.0,
    )
    assert allowed is True
    assert size == 50.0  # Capped at 50 shares
    assert price == 0.50


def test_position_shares_cap_per_token():
    """Verify per-token max position shares (150 shares)."""
    cfg = RiskConfig(max_position_shares_per_token=150.0, max_quote_size_shares=50.0)
    rm = RiskManager(cfg)

    # Existing shares = 120. Headroom = 30.
    # Cost = $40 (headroom = $20 -> 40 shares, so shares limit 30 binds first)
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.50,
        size=50.0,
        current_pos_shares=120.0,
        current_pos_cost=40.0,
        current_portfolio_inventory_usd=40.0,
        current_cash_usd=200.0,
    )
    assert allowed is True
    assert size == 30.0  # Trimmed to remaining headroom

    # Existing shares = 150. Headroom = 0.
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.50,
        size=50.0,
        current_pos_shares=150.0,
        current_pos_cost=40.0,
        current_portfolio_inventory_usd=40.0,
        current_cash_usd=200.0,
    )
    assert allowed is False
    assert size == 0.0


def test_market_cost_cap():
    """Verify max notional cost per market ($60)."""
    cfg = RiskConfig(max_position_cost_per_market=60.0, max_quote_size_shares=100.0)
    rm = RiskManager(cfg)

    # Existing cost = $50. Remaining headroom = $10.
    # At price $0.50, max shares = 10 / 0.50 = 20 shares.
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.50,
        size=50.0,
        current_pos_shares=100.0,
        current_pos_cost=50.0,
        current_portfolio_inventory_usd=50.0,
        current_cash_usd=250.0,
    )
    assert allowed is True
    assert size == 20.0

    # Existing cost = $60. No headroom remaining.
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.50,
        size=50.0,
        current_pos_shares=120.0,
        current_pos_cost=60.0,
        current_portfolio_inventory_usd=60.0,
        current_cash_usd=240.0,
    )
    assert allowed is False
    assert size == 0.0


def test_portfolio_inventory_exposure_cap():
    """Verify total portfolio inventory cap ($200 across all markets)."""
    cfg = RiskConfig(max_portfolio_inventory_usd=200.0, max_quote_size_shares=100.0)
    rm = RiskManager(cfg)

    # Portfolio inventory is already $180. Headroom is $20.
    # At price $0.50, max additional shares = 20 / 0.50 = 40 shares.
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_2",
        condition_id="cid_2",
        side="BUY",
        price=0.50,
        size=60.0,
        current_pos_shares=0.0,
        current_pos_cost=0.0,
        current_portfolio_inventory_usd=180.0,
        current_cash_usd=120.0,
    )
    assert allowed is True
    assert size == 40.0


def test_cash_headroom_cap():
    """Verify buy quote cannot exceed available cash."""
    cfg = RiskConfig(total_bankroll_usd=300.0)
    rm = RiskManager(cfg)

    # Cash remaining is only $10. At price $0.50, max shares = 20.
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.50,
        size=50.0,
        current_pos_shares=0.0,
        current_pos_cost=0.0,
        current_portfolio_inventory_usd=0.0,
        current_cash_usd=10.0,
    )
    assert allowed is True
    assert size == 20.0


# =========================================================================
# 2. Max Drawdown Circuit Breakers
# =========================================================================

def test_daily_loss_circuit_breaker():
    """Verify daily loss > $15 (or 5% of bankroll) halts engine and fires unquotes."""
    cfg = RiskConfig(total_bankroll_usd=300.0, daily_loss_limit_usd=15.0, daily_loss_limit_pct=0.05)
    rm = RiskManager(cfg)

    unquoted_reasons = []
    rm.register_unquote_callback(lambda aid, r: unquoted_reasons.append(r))

    # Equity drops from $300 to $286 (daily loss = $14 < $15 limit)
    st = rm.update_portfolio_pnl(
        current_equity=286.0,
        realized_pnl=-10.0,
        unrealized_pnl=-4.0,
    )
    assert st == RiskStatus.HEALTHY
    assert rm.portfolio_state.is_halted is False
    assert len(unquoted_reasons) == 0

    # Equity drops to $284 (daily loss = $16 >= $15 limit)
    st = rm.update_portfolio_pnl(
        current_equity=284.0,
        realized_pnl=-12.0,
        unrealized_pnl=-4.0,
    )
    assert st == RiskStatus.HALTED
    assert rm.portfolio_state.is_halted is True
    assert len(unquoted_reasons) == 1
    assert "Daily loss" in unquoted_reasons[0]

    # Verify subsequent quotes are rejected
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.50,
        size=10.0,
    )
    assert allowed is False
    assert "halted" in reason.lower()


def test_max_drawdown_circuit_breaker():
    """Verify cumulative drawdown > $30 (10% of $300) halts engine."""
    cfg = RiskConfig(total_bankroll_usd=300.0, max_drawdown_usd=30.0, max_drawdown_pct=0.10)
    rm = RiskManager(cfg)

    # First, equity rises to peak of $350
    rm.update_portfolio_pnl(current_equity=350.0, realized_pnl=50.0, unrealized_pnl=0.0)
    assert rm.portfolio_state.peak_equity_usd == 350.0

    # Equity draws down to $325 (drawdown = $25 < $30)
    st = rm.update_portfolio_pnl(current_equity=325.0, realized_pnl=25.0, unrealized_pnl=0.0)
    assert st == RiskStatus.HEALTHY
    assert rm.portfolio_state.is_halted is False

    # Equity draws down to $315 (drawdown = $35 >= $30)
    st = rm.update_portfolio_pnl(current_equity=315.0, realized_pnl=15.0, unrealized_pnl=0.0)
    assert st == RiskStatus.HALTED
    assert rm.portfolio_state.is_halted is True
    assert rm.portfolio_state.cumulative_drawdown_usd == 35.0


# =========================================================================
# 3. Price Jump & Volatility Circuit Breaker
# =========================================================================

def test_price_jump_within_60s_triggers_cooldown():
    """Verify price moving > 2.0c within 60s cancels quotes and triggers 5m cooldown."""
    cfg = RiskConfig(price_jump_threshold_cents=2.0, price_jump_window_seconds=60.0, cooldown_duration_seconds=300.0)
    rm = RiskManager(cfg)

    unquotes = []
    rm.register_unquote_callback(lambda aid, r: unquotes.append((aid, r)))

    t0 = 1000.0
    # Tick 1: mid = 0.50
    ok, err = rm.update_market_price("tok_1", "cid_1", 0.50, timestamp=t0)
    assert ok is True

    # Tick 2: mid = 0.51 (delta = 1.0c < 2.0c threshold)
    ok, err = rm.update_market_price("tok_1", "cid_1", 0.51, timestamp=t0 + 30.0)
    assert ok is True
    assert len(unquotes) == 0

    # Tick 3: mid jumps to 0.525 within 45s (delta = 2.5c > 2.0c threshold)
    ok, err = rm.update_market_price("tok_1", "cid_1", 0.525, timestamp=t0 + 45.0)
    assert ok is False
    assert "moved 2.50c within 60.0s" in err
    assert len(unquotes) == 1
    assert unquotes[0][0] == "tok_1"

    # Verify market is in cooldown for 300s
    assert rm.volatility_monitor.is_in_cooldown("tok_1", timestamp=t0 + 50.0) is True
    rem = rm.volatility_monitor.get_remaining_cooldown("tok_1", timestamp=t0 + 50.0)
    assert rem == pytest.approx(295.0)

    # Quotes should be rejected during cooldown
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.50,
        size=10.0,
        timestamp=t0 + 50.0,
    )
    assert allowed is False
    assert "cooldown" in reason.lower()

    # After 300s (e.g. t0 + 350s), cooldown expires
    assert rm.volatility_monitor.is_in_cooldown("tok_1", timestamp=t0 + 350.0) is False
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.50,
        size=10.0,
        timestamp=t0 + 350.0,
    )
    assert allowed is True


def test_gradual_price_move_does_not_trip_jump_breaker():
    """Verify gradual price move of 3.0c over 120s does not trip 60s jump breaker."""
    cfg = RiskConfig(price_jump_threshold_cents=2.0, price_jump_window_seconds=60.0)
    rm = RiskManager(cfg)

    t0 = 1000.0
    # Step 1: 0.50 at t=0
    rm.update_market_price("tok_1", "cid_1", 0.50, timestamp=t0)
    # Step 2: 0.51 at t=40
    rm.update_market_price("tok_1", "cid_1", 0.51, timestamp=t0 + 40.0)
    # Step 3: 0.52 at t=80 (t=0 evicted from 60s window, window has [40s, 80s] delta = 1.0c)
    ok, err = rm.update_market_price("tok_1", "cid_1", 0.52, timestamp=t0 + 80.0)
    assert ok is True
    # Step 4: 0.53 at t=120 (delta within [80s, 120s] is 1.0c)
    ok, err = rm.update_market_price("tok_1", "cid_1", 0.53, timestamp=t0 + 120.0)
    assert ok is True


# =========================================================================
# 4. Asymmetric Toxicity / Inventory Skew Limiter
# =========================================================================

def test_asymmetric_toxicity_widening():
    """Verify 3 consecutive BUY fills triggers spread widening on BID."""
    cfg = RiskConfig(
        consecutive_fills_threshold=3,
        toxicity_action="widen",
        toxicity_spread_penalty_cents=1.5,
        toxicity_size_reduction_pct=0.5,
    )
    rm = RiskManager(cfg)

    # 1st BUY fill
    rm.record_fill("tok_1", "cid_1", "BUY", 0.49, 10.0)
    assert rm.toxicity_tracker.toxic_sides.get("tok_1") is None

    # 2nd BUY fill
    rm.record_fill("tok_1", "cid_1", "BUY", 0.49, 10.0)
    assert rm.toxicity_tracker.toxic_sides.get("tok_1") is None

    # 3rd BUY fill -> triggers toxicity!
    rm.record_fill("tok_1", "cid_1", "BUY", 0.49, 10.0)
    assert rm.toxicity_tracker.toxic_sides.get("tok_1") == "BUY"

    # Validate BID quote: should be widened by 1.5c and size cut in half
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.49,
        size=40.0,
        mid_price=0.50,
    )
    assert allowed is True
    # Original spread = 1.0c. Widened spread = 2.5c -> Price = 0.50 - 0.025 = 0.475 -> round tick = 0.47 or 0.48
    assert price < 0.49
    assert size == 20.0  # Cut by 50%

    # ASK quote should be unaffected
    allowed_ask, _, size_ask, price_ask = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="SELL",
        price=0.51,
        size=40.0,
        mid_price=0.50,
    )
    assert allowed_ask is True
    assert size_ask == 40.0
    assert price_ask == 0.51

    # Opposite SELL fill clears toxicity!
    rm.record_fill("tok_1", "cid_1", "SELL", 0.51, 10.0)
    assert rm.toxicity_tracker.toxic_sides.get("tok_1") is None

    # BID quote returns to normal
    _, _, size_norm, price_norm = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.49,
        size=40.0,
        mid_price=0.50,
    )
    assert size_norm == 40.0
    assert price_norm == 0.49


def test_asymmetric_toxicity_suppression():
    """Verify toxicity_action='suppress' halts quoting on accumulating side."""
    cfg = RiskConfig(consecutive_fills_threshold=2, toxicity_action="suppress")
    rm = RiskManager(cfg)

    # 2 consecutive SELL fills
    rm.record_fill("tok_1", "cid_1", "SELL", 0.51, 10.0)
    rm.record_fill("tok_1", "cid_1", "SELL", 0.51, 10.0)
    assert rm.toxicity_tracker.toxic_sides.get("tok_1") == "SELL"

    # ASK quote should be completely suppressed
    allowed, reason, size, price = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="SELL",
        price=0.51,
        size=20.0,
        mid_price=0.50,
    )
    assert allowed is False
    assert "suppressed" in reason.lower()
    assert size == 0.0

    # BID quote still allowed
    allowed_bid, _, size_bid, _ = rm.validate_quote(
        asset_id="tok_1",
        condition_id="cid_1",
        side="BUY",
        price=0.49,
        size=20.0,
        mid_price=0.50,
    )
    assert allowed_bid is True
    assert size_bid == 20.0


# =========================================================================
# 5. Gas & Complete-Set Merge Manager
# =========================================================================

def test_gas_merge_manager_threshold_and_spread_cap():
    """Verify small capital merge threshold and gas fee <= 5% of captured spread."""
    cfg = RiskConfig(
        min_merge_threshold_shares=30.0,
        gas_fee_usd=0.015,
        max_gas_spread_pct=0.05,
        assumed_spread_cents=1.0,
    )
    gm = GasMergeManager(cfg)

    # Case 1: Below share threshold (20 shares < 30) -> reject
    can_m, reason = gm.can_merge(shares=20.0)
    assert can_m is False
    assert "below small capital threshold" in reason

    # Case 2: At 30 shares with 1.0c spread:
    # Captured spread = 30 * $0.01 = $0.30.
    # Gas fee = $0.015. $0.015 / $0.30 = 0.05 (5.0% <= 5.0%) -> permit
    can_m, reason = gm.can_merge(shares=30.0)
    assert can_m is True
    assert "permitted" in reason.lower()

    # Case 3: 30 shares, but actual captured spread was only $0.15 (0.5c spread):
    # Gas fee $0.015 / $0.15 = 10% > 5% cap -> reject
    can_m, reason = gm.can_merge(shares=30.0, captured_spread_usd=0.15)
    assert can_m is False
    assert "10.0% of captured spread" in reason

    # Case 4: 50 shares with $0.50 captured spread -> $0.015 / $0.50 = 3% <= 5% -> permit
    can_m, reason = gm.can_merge(shares=50.0, captured_spread_usd=0.50)
    assert can_m is True


def test_gas_spend_daily_cap():
    """Verify cumulative gas spend cannot exceed daily gas cap ($1.50)."""
    cfg = RiskConfig(
        min_merge_threshold_shares=30.0,
        gas_fee_usd=0.015,
        daily_gas_cap_usd=0.04,  # Low cap for test: allows 2 merges (2 * 0.015 = 0.03)
    )
    gm = GasMergeManager(cfg)

    assert gm.can_merge(30.0)[0] is True
    gm.record_merge(30.0)  # Spent 0.015
    assert gm.can_merge(30.0)[0] is True
    gm.record_merge(30.0)  # Spent 0.030

    # 3rd merge would spend 0.045 > 0.040 cap -> rejected
    can_m, reason = gm.can_merge(30.0)
    assert can_m is False
    assert "Daily gas spend cap" in reason


# =========================================================================
# 6. Emergency Kill-Switch & Heartbeat Monitor
# =========================================================================

def test_emergency_kill_switch():
    """Verify manual emergency unquote trips kill switch and cancels all quotes."""
    rm = RiskManager()
    unquoted = []
    rm.register_unquote_callback(lambda aid, r: unquoted.append((aid, r)))

    rm.emergency_unquote("Operator emergency stop")
    assert rm.portfolio_state.kill_switch_active is True
    assert rm.portfolio_state.status == RiskStatus.KILLED
    assert len(unquoted) == 1
    assert unquoted[0][0] is None  # Cancel all assets

    # Any quote attempt must fail
    allowed, reason, size, price = rm.validate_quote("tok_1", "cid_1", "BUY", 0.50, 10.0)
    assert allowed is False
    assert "kill switch" in reason.lower()

    # Test safe resumption
    rm.resume()
    assert rm.portfolio_state.kill_switch_active is False
    assert rm.portfolio_state.status == RiskStatus.HEALTHY
    allowed, _, _, _ = rm.validate_quote("tok_1", "cid_1", "BUY", 0.50, 10.0)
    assert allowed is True


def test_heartbeat_disconnect_detection():
    """Verify heartbeat loss > 15s halts engine and trips disconnect breaker."""
    cfg = RiskConfig(heartbeat_timeout_seconds=15.0)
    rm = RiskManager(cfg)

    t0 = 1000.0
    rm.record_heartbeat(timestamp=t0)
    assert rm.check_heartbeat(timestamp=t0 + 10.0) is True

    # 20 seconds later: heartbeat lost
    assert rm.check_heartbeat(timestamp=t0 + 20.0) is False
    assert rm.portfolio_state.is_halted is True
    assert rm.portfolio_state.status == RiskStatus.HALTED
    assert rm.portfolio_state.circuit_breaker_events[-1].reason == CircuitBreakerReason.DISCONNECT_TIMEOUT


# =========================================================================
# 7. PaperTradingEngine Integration
# =========================================================================

def test_paper_trading_engine_risk_integration(tmp_path: Path):
    """Test full integration with PaperTradingEngine under small capital constraints."""
    ledger_file = tmp_path / "ledger.json"
    risk_cfg = RiskConfig(
        total_bankroll_usd=300.0,
        max_position_cost_per_market=60.0,
        max_position_shares_per_token=150.0,
        max_quote_size_shares=50.0,
        price_jump_threshold_cents=2.0,
        min_merge_threshold_shares=30.0,
    )
    rm = RiskManager(risk_cfg)

    engine = PaperTradingEngine(
        initial_cash_usd=300.0,
        base_spread_offset_cents=1.0,
        default_quote_size=50.0,
        ledger_path=ledger_file,
        risk_manager=rm,
    )

    mkt = MarketConfig(
        condition_id="0xcond1",
        question="Fed Rate Decision",
        rate_per_day=500.0,
        rewards_min_size=10.0,
        rewards_max_spread=3.0,
        tokens=[
            TokenConfig(token_id="tok_yes", outcome="YES", price=0.50),
            TokenConfig(token_id="tok_no", outcome="NO", price=0.50),
        ],
    )
    engine.register_market(mkt)

    # 1. Update orderbook and generate quotes
    engine.update_orderbook(
        "tok_yes",
        bids=[{"price": "0.48", "size": "500"}],
        asks=[{"price": "0.52", "size": "500"}],
    )

    v_bid = engine.virtual_quotes["tok_yes"]["bid"]
    v_ask = engine.virtual_quotes["tok_yes"]["ask"]
    assert v_bid is not None
    assert v_ask is not None
    assert v_bid.size <= 50.0  # Capped by risk manager
    assert v_ask.size <= 50.0

    # 2. Simulate price jump > 2.0c in orderbook
    # Mid moves from 0.50 to 0.53 (3.0c move)
    engine.update_orderbook(
        "tok_yes",
        bids=[{"price": "0.52", "size": "500"}],
        asks=[{"price": "0.54", "size": "500"}],
    )
    # Quotes should be wiped and asset in cooldown
    assert engine.virtual_quotes["tok_yes"]["bid"] is None
    assert engine.virtual_quotes["tok_yes"]["ask"] is None
    assert rm.volatility_monitor.is_in_cooldown("tok_yes") is True

    # 3. Test complete-set merge integration with small capital threshold (30 shares)
    engine.positions["tok_yes"].shares = 35.0
    engine.positions["tok_yes"].avg_cost = 0.49
    engine.positions["tok_no"].shares = 35.0
    engine.positions["tok_no"].avg_cost = 0.50

    merge_evt = engine.check_and_execute_merge("0xcond1")
    assert merge_evt is not None
    assert merge_evt.shares_merged == 35.0
    assert merge_evt.usdc_unlocked == 35.0
    assert rm.gas_merge_manager.total_merges_count == 1

    # 4. Portfolio summary includes risk status
    summary = engine.get_portfolio_summary()
    assert "risk_status" in summary
    assert summary["kill_switch_active"] is False


# =========================================================================
# 8. LiquidityRewardsBotEngine Integration
# =========================================================================

def test_liquidity_rewards_bot_engine_risk_integration():
    """Verify LiquidityRewardsBotEngine simulation obeys small capital risk controls."""
    mkt = MarketRewardParameters(
        condition_id="0xcond_rewards",
        question="Test Polymarket Rewards Market",
        rate_per_day=300.0,
        rewards_min_size=20.0,
        rewards_max_spread=3.0,
        initial_mid=0.50,
        category="politics",
        competing_qualifying_depth=3000.0,
        competing_touch_depth=500.0,
    )

    risk_cfg = RiskConfig(
        total_bankroll_usd=300.0,
        max_position_cost_per_market=60.0,
        max_position_shares_per_token=150.0,
        max_quote_size_shares=50.0,
        min_merge_threshold_shares=30.0,
    )
    rm = RiskManager(risk_cfg)

    bot_engine = LiquidityRewardsBotEngine(
        market=mkt,
        quote_size=50.0,
        risk_gamma=0.01,
        max_inventory=200.0,
        seed=42,
        risk_manager=rm,
    )

    # Auto merge threshold on position should be set to 30.0 (from risk manager)
    assert bot_engine.position.auto_merge_threshold == 30.0

    # Simulate 120 minutes
    report = bot_engine.simulate_epoch(epoch_idx=1, n_minutes=120)

    assert report.epoch_index == 1
    assert report.allocated_capital > 0.0
    # Net inventory must not blow past small capital limits
    assert abs(report.final_net_inventory) <= 200.0
