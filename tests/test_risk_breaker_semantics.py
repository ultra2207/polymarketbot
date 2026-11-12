"""Tests for circuit-breaker semantics: cooldown vs halt, daily rollover, long-only SELL, merge notional.

Covers audit items S3 (risk half), S14 and M2:
- a price jump enters a bounded cooldown instead of permanently halting the engine
- expired cooldowns auto-clear back to HEALTHY; permanent breakers do not
- the daily loss window rolls over at UTC midnight without clearing a halt
- SELL quotes are capped by owned shares (and rejected at zero) when shorting is disabled
- merges below the configured captured-spread notional are rejected
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from polymarketbot.risk.manager import (
    GasMergeManager,
    PriceVolatilityMonitor,
    RiskManager,
)
from polymarketbot.risk.models import CircuitBreakerReason, RiskConfig, RiskStatus

T0 = 1_700_000_000.0  # fixed epoch so tests are deterministic

# The account state validate_quote requires: it has no defaults, because a
# forgotten position or cash balance used to gate the quote against an account
# that does not exist. ``_ACCOUNT`` carries a $300 balance and an existing
# 12-share long so the long-only SELL cases keep their original meaning.
_ACCOUNT = {
    "current_pos_shares": 12.0,
    "current_pos_cost": 5.88,
    "current_portfolio_inventory_usd": 5.88,
    "current_cash_usd": 300.0,
    "mid_price": 0.50,
}
_ACCOUNT_NO_POS = {**_ACCOUNT, "current_pos_shares": 0.0, "current_pos_cost": 0.0}


def _day(day_of_month: int) -> float:
    return datetime(2026, 1, day_of_month, tzinfo=UTC).timestamp()


# =========================================================================
# Price jump -> cooldown, never halt
# =========================================================================

def test_price_jump_sets_cooldown_not_halt():
    rm = RiskManager(RiskConfig())
    unquoted: list[tuple[str | None, str]] = []
    rm.register_unquote_callback(lambda aid, reason: unquoted.append((aid, reason)))

    ok, err = rm.update_market_price("tok", "cid", 0.50, timestamp=T0)
    assert ok is True and err is None

    ok, err = rm.update_market_price("tok", "cid", 0.53, timestamp=T0 + 1.0)
    assert ok is False
    assert "moved 3.00c" in err

    st = rm.portfolio_state
    assert st.is_halted is False
    assert st.status == RiskStatus.COOLDOWN
    assert rm.volatility_monitor.is_in_cooldown("tok", timestamp=T0 + 1.5) is True
    assert unquoted == [("tok", err)]
    assert st.circuit_breaker_events[-1].reason == CircuitBreakerReason.PRICE_JUMP_VOLATILITY
    assert rm.market_states["tok"].status == RiskStatus.COOLDOWN


def test_cooldown_shows_warning_then_auto_clears_to_healthy():
    rm = RiskManager(RiskConfig(cooldown_duration_seconds=300.0))
    rm.update_market_price("tok", "cid", 0.50, timestamp=T0)
    rm.update_market_price("tok", "cid", 0.53, timestamp=T0 + 1.0)

    allowed, reason, size, _ = rm.validate_quote(
        "tok", "cid", "BUY", 0.50, 10.0, **_ACCOUNT, timestamp=T0 + 2.0
    )
    assert allowed is False
    assert "cooldown" in reason.lower()
    assert rm.portfolio_state.status == RiskStatus.WARNING

    # One tick before expiry the cooldown is still active.
    assert rm.volatility_monitor.is_in_cooldown("tok", timestamp=T0 + 299.0) is True

    # After expiry the monitor drops the entry and the manager returns to HEALTHY.
    assert rm.volatility_monitor.is_in_cooldown("tok", timestamp=T0 + 302.0) is False
    rm.auto_clear_cooldowns(T0 + 302.0)
    assert rm.portfolio_state.status == RiskStatus.HEALTHY
    assert "tok" not in rm.volatility_monitor.cooldowns
    assert rm.market_states["tok"].status == RiskStatus.HEALTHY

    allowed, _, size, _ = rm.validate_quote(
        "tok", "cid", "BUY", 0.50, 10.0, **_ACCOUNT, timestamp=T0 + 302.0
    )
    assert allowed is True and size == 10.0


def test_expired_cooldown_for_one_asset_keeps_warning_if_another_is_active():
    rm = RiskManager(RiskConfig())
    for asset in ("a", "b"):
        rm.update_market_price(asset, "cid", 0.50, timestamp=T0)
        rm.update_market_price(asset, "cid", 0.53, timestamp=T0 + 1.0)

    rm.volatility_monitor.cooldowns["a"] = T0 + 2.0
    rm.auto_clear_cooldowns(T0 + 5.0)
    assert rm.portfolio_state.status == RiskStatus.WARNING
    assert rm.volatility_monitor.is_in_cooldown("a", timestamp=T0 + 5.0) is False
    assert rm.volatility_monitor.is_in_cooldown("b", timestamp=T0 + 5.0) is True


def test_duplicate_mids_are_deduped_and_cannot_trip():
    monitor = PriceVolatilityMonitor(RiskConfig())
    assert monitor.record_mid_price("tok", "cid", 0.50, T0) == (False, 0.0)
    assert monitor.record_mid_price("tok", "cid", 0.50, T0 + 30.0) == (False, 0.0)
    assert len(monitor.history["tok"]) == 1

    # record_price is a compatibility alias that routes through the same logic.
    assert monitor.record_price("tok", "cid", 0.50, T0 + 60.0) == (False, 0.0)
    assert len(monitor.history["tok"]) == 1


def test_price_jump_does_not_downgrade_existing_halt():
    rm = RiskManager(RiskConfig())
    rm.trip_circuit_breaker(CircuitBreakerReason.DAILY_LOSS_LIMIT, "daily loss", timestamp=T0)
    assert rm.portfolio_state.status == RiskStatus.HALTED

    rm.update_market_price("tok", "cid", 0.50, timestamp=T0)
    rm.update_market_price("tok", "cid", 0.53, timestamp=T0 + 1.0)

    assert rm.portfolio_state.is_halted is True
    assert rm.portfolio_state.status == RiskStatus.HALTED
    assert len(rm.portfolio_state.circuit_breaker_events) == 2


def test_price_jump_cooldown_is_clamped():
    cfg = RiskConfig(cooldown_duration_seconds=99_999.0, max_price_jump_cooldown_seconds=3600.0)
    rm = RiskManager(cfg)
    rm.update_market_price("tok", "cid", 0.50, timestamp=T0)
    rm.update_market_price("tok", "cid", 0.53, timestamp=T0 + 1.0)
    assert rm.volatility_monitor.get_remaining_cooldown("tok", timestamp=T0 + 1.0) == pytest.approx(3600.0)


# =========================================================================
# UTC daily rollover
# =========================================================================

def test_utc_daily_rollover_resets_day_start_and_gas():
    rm = RiskManager(RiskConfig())
    rm.update_portfolio_pnl(300.0, 0.0, 0.0, timestamp=_day(1))
    rm.record_merge(30.0, gas_fee_usd=0.02)
    assert rm.gas_merge_manager.daily_gas_spent == pytest.approx(0.02)
    assert rm.portfolio_state.daily_gas_spent_usd == pytest.approx(0.02)

    rm.update_portfolio_pnl(310.0, 5.0, 5.0, timestamp=_day(2))

    assert rm.portfolio_state.day_start_equity_usd == pytest.approx(310.0)
    assert rm.portfolio_state.daily_loss_usd == pytest.approx(0.0)
    assert rm.gas_merge_manager.daily_gas_spent == pytest.approx(0.0)
    assert rm.portfolio_state.daily_gas_spent_usd == pytest.approx(0.0)
    assert rm.gas_merge_manager.total_merges_count == 1


def test_daily_loss_halt_persists_across_rollover():
    rm = RiskManager(RiskConfig(total_bankroll_usd=300.0, daily_loss_limit_usd=15.0))
    rm.update_portfolio_pnl(300.0, 0.0, 0.0, timestamp=_day(1))
    rm.update_portfolio_pnl(280.0, -20.0, 0.0, timestamp=_day(1) + 60.0)
    assert rm.portfolio_state.is_halted is True
    assert rm.portfolio_state.status == RiskStatus.HALTED

    rm.update_portfolio_pnl(280.0, -20.0, 0.0, timestamp=_day(2))

    assert rm.portfolio_state.is_halted is True
    assert rm.portfolio_state.status == RiskStatus.HALTED
    assert rm.portfolio_state.day_start_equity_usd == pytest.approx(280.0)
    assert sum(
        1 for e in rm.portfolio_state.circuit_breaker_events
        if e.reason == CircuitBreakerReason.DAILY_LOSS_LIMIT
    ) == 1

    # Only an explicit operator resume clears a permanent breaker.
    rm.resume()
    assert rm.portfolio_state.is_halted is False
    assert rm.portfolio_state.status == RiskStatus.HEALTHY


def test_auto_clear_never_clears_a_permanent_halt():
    rm = RiskManager(RiskConfig())
    rm.trip_circuit_breaker(CircuitBreakerReason.MAX_DRAWDOWN_LIMIT, "drawdown", timestamp=T0)
    rm.auto_clear_cooldowns(T0 + 86_400.0)
    assert rm.portfolio_state.is_halted is True
    assert rm.portfolio_state.status == RiskStatus.HALTED


# =========================================================================
# Long-only SELL
# =========================================================================

def test_sell_is_capped_at_owned_shares_when_shorting_disabled():
    rm = RiskManager(RiskConfig(allow_short=False))
    allowed, _reason, size, price = rm.validate_quote(
        "tok", "cid", "SELL", 0.49, 40.0, **{**_ACCOUNT, "current_pos_shares": 12.0}
    )
    assert allowed is True
    assert size == 12.0
    assert price == 0.49


def test_sell_with_no_owned_shares_is_rejected_when_shorting_disabled():
    rm = RiskManager(RiskConfig(allow_short=False))
    allowed, reason, size, _ = rm.validate_quote(
        "tok", "cid", "SELL", 0.49, 40.0, **_ACCOUNT_NO_POS
    )
    assert allowed is False
    assert size == 0.0
    assert "no owned shares" in reason.lower()


def test_sell_without_inventory_is_allowed_when_shorts_enabled():
    rm = RiskManager(RiskConfig(allow_short=True))
    allowed, _, size, _ = rm.validate_quote(
        "tok", "cid", "SELL", 0.49, 40.0, **_ACCOUNT_NO_POS
    )
    assert allowed is True
    assert size == 40.0


# =========================================================================
# Merge notional gate
# =========================================================================

def test_merge_below_notional_is_rejected():
    cfg = RiskConfig(min_merge_threshold_shares=1.0, min_merge_notional_usd=1.0)
    gm = GasMergeManager(cfg)

    can, reason = gm.can_merge(
        shares=5.0, captured_spread_usd=0.5, gas_fee_usd=0.001
    )
    assert can is False
    assert "below minimum merge notional" in reason

    can, reason = gm.can_merge(
        shares=5.0, captured_spread_usd=1.5, gas_fee_usd=0.001
    )
    assert can is True


def test_merge_gate_requires_measured_spread_and_gas():
    gm = GasMergeManager(RiskConfig())
    # A 30-share merge at a measured $0.30 captured spread with $0.01 gas.
    can, reason = gm.can_merge(
        shares=30.0, captured_spread_usd=0.30, gas_fee_usd=0.01
    )
    assert can is True
    assert "permitted" in reason.lower()

    can, reason = gm.can_merge(
        shares=20.0, captured_spread_usd=0.30, gas_fee_usd=0.01
    )
    assert can is False
    assert "below small capital threshold" in reason

    # Neither the captured spread nor the gas price has a fallback any more:
    # the old defaults were 1c/share and $0.015.
    can, reason = gm.can_merge(shares=30.0, captured_spread_usd=None, gas_fee_usd=0.01)
    assert can is False
    assert "unmeasured" in reason.lower()
    can, reason = gm.can_merge(shares=30.0, captured_spread_usd=0.30, gas_fee_usd=None)
    assert can is False
    assert "unmeasured" in reason.lower()
