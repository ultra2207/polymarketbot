"""Production-grade Risk Management System for Polymarket Liquidity Rewards.

Enforces:
1. Hard Capital & Position Limits ($300 bankroll, $60/market, 150 shares/token).
2. Max Drawdown & Daily Loss Circuit Breakers ($15/5% daily loss, $30/10% drawdown).
3. Price Jump & High-Frequency Volatility Breaker (>2.0c move within 60s -> 5m cooldown).
4. Asymmetric Toxicity / Inventory Skew Limiter (widen/suppress after 2-3 consecutive fills).
5. Gas & Complete-Set Merge Manager (scaled to 25-50 shares, gas fee <= 5% of captured spread).
6. Emergency Unquote / Kill-Switch & Heartbeat Monitor.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple

from polymarketbot.logs import get_logger
from polymarketbot.risk.models import (
    CircuitBreakerEvent,
    CircuitBreakerReason,
    MarketRiskState,
    PortfolioRiskState,
    RiskConfig,
    RiskStatus,
)

logger = get_logger("risk_management")


class PriceVolatilityMonitor:
    """Monitors high-frequency price movements and detects sudden price jumps."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self.history: dict[str, deque[tuple[float, float]]] = {}  # asset_id -> deque of (ts, mid)
        self.cooldowns: dict[str, float] = {}  # asset_id -> cooldown_expiry_ts

    def record_price(
        self,
        asset_id: str,
        condition_id: str,
        mid_price: float,
        timestamp: Optional[float] = None,
    ) -> tuple[bool, float]:
        """Record mid price tick and check for rolling price jump exceeding threshold.

        Returns:
            (is_jump_detected, max_delta_cents)
        """
        now = timestamp if timestamp is not None else time.time()
        if asset_id not in self.history:
            self.history[asset_id] = deque()

        queue = self.history[asset_id]
        queue.append((now, mid_price))

        # Evict timestamps older than rolling window
        cutoff = now - self.config.price_jump_window_seconds
        while queue and queue[0][0] < cutoff:
            queue.popleft()

        if len(queue) < 2:
            return False, 0.0

        min_p = min(p for _, p in queue)
        max_p = max(p for _, p in queue)
        delta_cents = (max_p - min_p) * 100.0

        if delta_cents > self.config.price_jump_threshold_cents:
            # Enter cooldown
            self.cooldowns[asset_id] = now + self.config.cooldown_duration_seconds
            logger.warning(
                f"[PRICE JUMP BREAKER] Asset {asset_id} jumped {delta_cents:.2f}c within "
                f"{self.config.price_jump_window_seconds}s (limit: {self.config.price_jump_threshold_cents}c). "
                f"Entering {self.config.cooldown_duration_seconds}s cooldown."
            )
            return True, delta_cents

        return False, delta_cents

    def is_in_cooldown(self, asset_id: str, timestamp: Optional[float] = None) -> bool:
        """Check if market is actively in volatility cooldown."""
        now = timestamp if timestamp is not None else time.time()
        expiry = self.cooldowns.get(asset_id, 0.0)
        return now < expiry

    def get_remaining_cooldown(self, asset_id: str, timestamp: Optional[float] = None) -> float:
        """Get seconds remaining in cooldown."""
        now = timestamp if timestamp is not None else time.time()
        return max(0.0, self.cooldowns.get(asset_id, 0.0) - now)

    def clear_cooldown(self, asset_id: str) -> None:
        """Manually clear cooldown for an asset."""
        if asset_id in self.cooldowns:
            del self.cooldowns[asset_id]


class ToxicityTracker:
    """Detects informed toxic flow via consecutive same-direction fills and skews quotes."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self.consecutive_buys: dict[str, int] = {}  # asset_id -> count
        self.consecutive_sells: dict[str, int] = {}  # asset_id -> count
        self.toxic_sides: dict[str, Optional[str]] = {}  # asset_id -> "BUY" or "SELL"
        self.last_fill_times: dict[str, float] = {}  # asset_id -> ts

    def record_fill(
        self,
        asset_id: str,
        side: str,
        timestamp: Optional[float] = None,
    ) -> tuple[bool, Optional[str]]:
        """Record trade fill and update directional asymmetry counters.

        Returns:
            (is_toxic, toxic_side)
        """
        now = timestamp if timestamp is not None else time.time()
        side_norm = side.upper()
        self.last_fill_times[asset_id] = now

        buys = self.consecutive_buys.get(asset_id, 0)
        sells = self.consecutive_sells.get(asset_id, 0)

        if side_norm == "BUY":
            buys += 1
            sells = 0
            self.consecutive_buys[asset_id] = buys
            self.consecutive_sells[asset_id] = 0

            if buys >= self.config.consecutive_fills_threshold:
                self.toxic_sides[asset_id] = "BUY"
                logger.warning(
                    f"[TOXICITY DETECTED] {buys} consecutive BUY fills in {asset_id}. "
                    f"Applying action '{self.config.toxicity_action}' to BID quote."
                )
                return True, "BUY"
            else:
                self.toxic_sides[asset_id] = None
                return False, None

        elif side_norm == "SELL":
            sells += 1
            buys = 0
            self.consecutive_sells[asset_id] = sells
            self.consecutive_buys[asset_id] = 0

            if sells >= self.config.consecutive_fills_threshold:
                self.toxic_sides[asset_id] = "SELL"
                logger.warning(
                    f"[TOXICITY DETECTED] {sells} consecutive SELL fills in {asset_id}. "
                    f"Applying action '{self.config.toxicity_action}' to ASK quote."
                )
                return True, "SELL"
            else:
                self.toxic_sides[asset_id] = None
                return False, None

        return False, None

    def evaluate_quote_adjustment(
        self,
        asset_id: str,
        side: str,
        base_price: float,
        base_spread_cents: float,
        base_size: float,
        mid_price: float,
        tick_size: float = 0.01,
    ) -> tuple[float, float, float, bool]:
        """Adjust or suppress quote if accumulating side suffers asymmetric toxicity.

        Returns:
            (adjusted_price, adjusted_spread_cents, adjusted_size, is_suppressed)
        """
        side_norm = side.upper()
        toxic = self.toxic_sides.get(asset_id)

        if toxic is None or toxic != side_norm:
            return base_price, base_spread_cents, base_size, False

        # Toxicity action on accumulating side
        if self.config.toxicity_action == "suppress":
            return base_price, base_spread_cents, 0.0, True

        # Widen accumulating side
        widened_spread = base_spread_cents + self.config.toxicity_spread_penalty_cents
        reduced_size = max(0.0, base_size * (1.0 - self.config.toxicity_size_reduction_pct))

        if side_norm == "BUY":
            raw_px = mid_price - (widened_spread / 100.0)
            adj_px = round(raw_px / tick_size) * tick_size
        else:
            raw_px = mid_price + (widened_spread / 100.0)
            adj_px = round(raw_px / tick_size) * tick_size

        adj_px = max(0.001, min(0.999, round(adj_px, 4)))
        return adj_px, widened_spread, reduced_size, False

    def reset(self, asset_id: Optional[str] = None) -> None:
        """Reset toxicity counters."""
        if asset_id:
            self.consecutive_buys[asset_id] = 0
            self.consecutive_sells[asset_id] = 0
            self.toxic_sides[asset_id] = None
        else:
            self.consecutive_buys.clear()
            self.consecutive_sells.clear()
            self.toxic_sides.clear()


class GasMergeManager:
    """Manages gas spend and complete-set CTF merging calibrated for small capital ($300)."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self.daily_gas_spent: float = 0.0
        self.total_gas_spent: float = 0.0
        self.total_merges_count: int = 0

    def can_merge(
        self,
        shares: float,
        captured_spread_usd: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Check whether complete-set merge meets small-capital thresholds and gas budget.

        Rules:
        1. Must meet dynamic merge threshold (e.g. 25-50 shares, default 30).
        2. Gas fee ($0.015) cannot exceed 5% of captured spread.
        3. Cumulative daily gas spend cannot exceed daily gas cap ($1.50).
        """
        if shares < self.config.min_merge_threshold_shares:
            return (
                False,
                f"Merge size {shares:.1f} sh below small capital threshold {self.config.min_merge_threshold_shares:.1f} sh",
            )

        # Calculate captured spread if not explicitly given
        spread_usd = (
            captured_spread_usd
            if captured_spread_usd is not None
            else shares * (self.config.assumed_spread_cents / 100.0)
        )

        if spread_usd <= 0.0:
            return False, "Captured spread is non-positive; merge would incur pure gas drag"

        gas_ratio = self.config.gas_fee_usd / spread_usd
        if gas_ratio > self.config.max_gas_spread_pct + 1e-6:
            return (
                False,
                f"Gas fee ${self.config.gas_fee_usd:.4f} is {gas_ratio * 100.0:.1f}% of captured spread "
                f"${spread_usd:.3f}, exceeding {self.config.max_gas_spread_pct * 100.0:.1f}% cap",
            )

        if self.daily_gas_spent + self.config.gas_fee_usd > self.config.daily_gas_cap_usd:
            return (
                False,
                f"Daily gas spend cap ${self.config.daily_gas_cap_usd:.2f} reached "
                f"(${self.daily_gas_spent:.3f} spent)",
            )

        return True, "Merge permitted under small capital gas budget"

    def record_merge(self, shares: float, gas_fee_usd: Optional[float] = None) -> None:
        """Record merge execution and update gas counters."""
        fee = gas_fee_usd if gas_fee_usd is not None else self.config.gas_fee_usd
        self.daily_gas_spent += fee
        self.total_gas_spent += fee
        self.total_merges_count += 1
        logger.info(
            f"[MERGE RECORDED] Merged {shares:.1f} complete sets | Gas: -${fee:.4f} | "
            f"Daily Gas: ${self.daily_gas_spent:.4f} / ${self.config.daily_gas_cap_usd:.2f}"
        )

    def reset_daily(self) -> None:
        """Reset daily gas counter at day boundary."""
        self.daily_gas_spent = 0.0


class RiskManager:
    """Master risk management controller for Polymarket small capital quoting."""

    def __init__(self, config: Optional[RiskConfig] = None) -> None:
        self.config = config or RiskConfig()
        self.portfolio_state = PortfolioRiskState(
            bankroll_usd=self.config.total_bankroll_usd,
            peak_equity_usd=self.config.total_bankroll_usd,
            day_start_equity_usd=self.config.total_bankroll_usd,
            current_equity_usd=self.config.total_bankroll_usd,
            current_cash_usd=self.config.total_bankroll_usd,
        )
        self.market_states: dict[str, MarketRiskState] = {}

        # Specialized sub-managers
        self.volatility_monitor = PriceVolatilityMonitor(self.config)
        self.toxicity_tracker = ToxicityTracker(self.config)
        self.gas_merge_manager = GasMergeManager(self.config)

        # Unquote callbacks
        self._unquote_callbacks: list[Callable[[Optional[str], str], None]] = []

    def register_unquote_callback(self, cb: Callable[[Optional[str], str], None]) -> None:
        """Register a callback to cancel active quotes when breakers trip or kill switch fires.

        Signature: cb(asset_id: Optional[str], reason: str)
        If asset_id is None, cancel all quotes across all markets.
        """
        self._unquote_callbacks.append(cb)

    def _trigger_unquote(self, asset_id: Optional[str], reason: str) -> None:
        """Execute all registered unquote callbacks."""
        for cb in self._unquote_callbacks:
            try:
                cb(asset_id, reason)
            except Exception as e:
                logger.error(f"Error executing unquote callback: {e}")

    def trip_circuit_breaker(
        self,
        reason: CircuitBreakerReason,
        details: str,
        condition_id: Optional[str] = None,
        asset_id: Optional[str] = None,
    ) -> None:
        """Trip a circuit breaker, halt execution, log event, and fast-cancel quotes."""
        self.portfolio_state.is_halted = True
        self.portfolio_state.status = RiskStatus.HALTED

        event = CircuitBreakerEvent(
            timestamp=time.time(),
            reason=reason,
            details=details,
            condition_id=condition_id,
            asset_id=asset_id,
            action_taken="CANCEL_ALL_QUOTES" if asset_id is None else f"CANCEL_QUOTES_{asset_id}",
        )
        self.portfolio_state.circuit_breaker_events.append(event)
        logger.critical(f"[CIRCUIT BREAKER TRIPPED] {reason.value}: {details}")

        # Emergency unquote
        self._trigger_unquote(asset_id, details)

    def emergency_unquote(self, reason: str = "MANUAL_KILL") -> None:
        """Trigger emergency unquote and activate kill switch."""
        self.portfolio_state.kill_switch_active = True
        self.portfolio_state.is_halted = True
        self.portfolio_state.status = RiskStatus.KILLED

        event = CircuitBreakerEvent(
            timestamp=time.time(),
            reason=CircuitBreakerReason.MANUAL_KILL,
            details=f"Emergency kill switch triggered: {reason}",
            action_taken="CANCEL_ALL_QUOTES_KILL_SWITCH",
        )
        self.portfolio_state.circuit_breaker_events.append(event)
        logger.critical(f"[EMERGENCY KILL SWITCH ACTIVATED] Reason: {reason}")
        self._trigger_unquote(None, reason)

    def record_heartbeat(self, timestamp: Optional[float] = None) -> None:
        """Record REST or WebSocket heartbeat."""
        now = timestamp if timestamp is not None else time.time()
        self.portfolio_state.last_heartbeat_timestamp = now

    def check_heartbeat(self, timestamp: Optional[float] = None) -> bool:
        """Check for connection heartbeat loss exceeding timeout.

        Returns:
            True if healthy, False if disconnected.
        """
        now = timestamp if timestamp is not None else time.time()
        elapsed = now - self.portfolio_state.last_heartbeat_timestamp
        if elapsed > self.config.heartbeat_timeout_seconds:
            msg = f"Heartbeat lost: {elapsed:.1f}s since last heartbeat (timeout: {self.config.heartbeat_timeout_seconds}s)"
            self.trip_circuit_breaker(CircuitBreakerReason.DISCONNECT_TIMEOUT, msg)
            return False
        return True

    def update_market_price(
        self,
        asset_id: str,
        condition_id: str,
        mid_price: float,
        timestamp: Optional[float] = None,
    ) -> tuple[bool, Optional[str]]:
        """Record mid-price tick and verify price jump circuit breaker.

        Returns:
            (is_healthy, error_or_cooldown_message)
        """
        now = timestamp if timestamp is not None else time.time()
        self.record_heartbeat(now)

        jump_detected, delta_cents = self.volatility_monitor.record_price(
            asset_id, condition_id, mid_price, now
        )

        if jump_detected:
            msg = (
                f"Mid-price moved {delta_cents:.2f}c within "
                f"{self.config.price_jump_window_seconds}s > threshold {self.config.price_jump_threshold_cents}c"
            )
            event = CircuitBreakerEvent(
                timestamp=now,
                reason=CircuitBreakerReason.PRICE_JUMP_VOLATILITY,
                details=msg,
                condition_id=condition_id,
                asset_id=asset_id,
                action_taken="COOLDOWN_AND_CANCEL",
            )
            self.portfolio_state.circuit_breaker_events.append(event)
            # Cancel quotes for this volatile asset
            self._trigger_unquote(asset_id, msg)
            return False, msg

        return True, None

    def validate_quote(
        self,
        asset_id: str,
        condition_id: str,
        side: str,
        price: float,
        size: float,
        current_pos_shares: float = 0.0,
        current_pos_cost: float = 0.0,
        current_portfolio_inventory_usd: float = 0.0,
        current_cash_usd: float = 300.0,
        mid_price: Optional[float] = None,
        tick_size: float = 0.01,
        timestamp: Optional[float] = None,
    ) -> tuple[bool, str, float, float]:
        """Comprehensive pre-trade risk check on a proposed virtual or live quote.

        Validates:
        - Engine state (halted/killed/breaker)
        - Market volatility cooldown
        - Asymmetric toxicity skew / suppression
        - Hard bankroll and cash limits
        - Per-token share cap (150 shares)
        - Per-market cost cap ($60 notional)
        - Portfolio total inventory cap ($200)

        Returns:
            (is_allowed, reason, approved_size, approved_price)
        """
        now = timestamp if timestamp is not None else time.time()

        # 1. Engine & breaker state
        if self.portfolio_state.kill_switch_active:
            return False, "Emergency kill switch is active", 0.0, price
        if self.portfolio_state.is_halted:
            return False, f"Risk engine is halted: {self.portfolio_state.status.value}", 0.0, price

        # 2. Market volatility cooldown
        if self.volatility_monitor.is_in_cooldown(asset_id, now):
            rem = self.volatility_monitor.get_remaining_cooldown(asset_id, now)
            return False, f"Market in volatility cooldown ({rem:.1f}s remaining)", 0.0, price

        # 3. Asymmetric toxicity skew
        approved_price = price
        spread_cents = abs(price - (mid_price if mid_price is not None else price)) * 100.0
        eff_mid = mid_price if mid_price is not None else price

        adj_px, adj_spread, adj_sz, is_suppressed = self.toxicity_tracker.evaluate_quote_adjustment(
            asset_id=asset_id,
            side=side,
            base_price=price,
            base_spread_cents=spread_cents,
            base_size=size,
            mid_price=eff_mid,
            tick_size=tick_size,
        )

        if is_suppressed:
            return False, "Quote suppressed due to asymmetric toxicity", 0.0, price

        approved_price = adj_px
        candidate_size = adj_sz

        # 4. Cap single order quote size
        candidate_size = min(candidate_size, self.config.max_quote_size_shares)

        # 5. Position Limits
        side_norm = side.upper()
        if side_norm == "BUY":
            # Per-token max shares
            shares_headroom = max(0.0, self.config.max_position_shares_per_token - current_pos_shares)
            candidate_size = min(candidate_size, shares_headroom)

            # Per-market max cost ($60)
            cost_headroom = max(0.0, self.config.max_position_cost_per_market - current_pos_cost)
            max_shares_by_cost = cost_headroom / max(1e-4, approved_price)
            candidate_size = min(candidate_size, max_shares_by_cost)

            # Portfolio total inventory exposure ($200)
            inv_headroom = max(0.0, self.config.max_portfolio_inventory_usd - current_portfolio_inventory_usd)
            max_shares_by_inv = inv_headroom / max(1e-4, approved_price)
            candidate_size = min(candidate_size, max_shares_by_inv)

            # Cash headroom ($300 bankroll)
            cash_headroom = max(0.0, current_cash_usd)
            max_shares_by_cash = cash_headroom / max(1e-4, approved_price)
            candidate_size = min(candidate_size, max_shares_by_cash)

        elif side_norm == "SELL":
            # For selling: ensure we don't exceed short inventory limits
            shares_headroom = max(0.0, self.config.max_position_shares_per_token - abs(current_pos_shares))
            candidate_size = min(candidate_size, shares_headroom)

        if candidate_size <= 1e-4:
            return False, "Quote size reduced to 0 by position, cost, or capital limits", 0.0, approved_price

        return True, "Quote approved", round(candidate_size, 4), round(approved_price, 4)

    def record_fill(
        self,
        asset_id: str,
        condition_id: str,
        side: str,
        price: float,
        size: float,
        timestamp: Optional[float] = None,
    ) -> None:
        """Record fill and update toxicity tracking."""
        now = timestamp if timestamp is not None else time.time()
        self.record_heartbeat(now)
        self.toxicity_tracker.record_fill(asset_id, side, now)

    def update_portfolio_pnl(
        self,
        current_equity: float,
        realized_pnl: float,
        unrealized_pnl: float,
        current_cash: Optional[float] = None,
        positions_market_value: Optional[float] = None,
        timestamp: Optional[float] = None,
    ) -> RiskStatus:
        """Update PnL, equity, peak values, and evaluate Drawdown & Daily Loss Breakers.

        Rules:
        - Daily loss limit: $15 or 5% of bankroll.
        - Max drawdown limit: $30 or 10% of peak equity.
        """
        now = timestamp if timestamp is not None else time.time()
        self.record_heartbeat(now)

        st = self.portfolio_state
        st.current_equity_usd = current_equity
        st.daily_realized_pnl = realized_pnl
        st.daily_unrealized_pnl = unrealized_pnl
        if current_cash is not None:
            st.current_cash_usd = current_cash
        if positions_market_value is not None:
            st.current_positions_value_usd = positions_market_value

        # Update peak equity
        if current_equity > st.peak_equity_usd:
            st.peak_equity_usd = current_equity

        # 1. Daily Loss Check
        # Total daily loss = day_start_equity - current_equity
        daily_loss = max(0.0, st.day_start_equity_usd - current_equity)
        st.daily_loss_usd = daily_loss
        daily_loss_pct = daily_loss / st.day_start_equity_usd if st.day_start_equity_usd > 0 else 0.0

        if (
            daily_loss >= self.config.daily_loss_limit_usd
            or daily_loss_pct >= self.config.daily_loss_limit_pct
        ):
            msg = (
                f"Daily loss ${daily_loss:.2f} ({daily_loss_pct * 100.0:.1f}%) reached limit "
                f"${self.config.daily_loss_limit_usd:.2f} ({self.config.daily_loss_limit_pct * 100.0:.1f}%)"
            )
            self.trip_circuit_breaker(CircuitBreakerReason.DAILY_LOSS_LIMIT, msg)
            return st.status

        # 2. Cumulative Drawdown Check
        drawdown = max(0.0, st.peak_equity_usd - current_equity)
        drawdown_pct = drawdown / st.peak_equity_usd if st.peak_equity_usd > 0 else 0.0
        st.cumulative_drawdown_usd = drawdown
        st.cumulative_drawdown_pct = drawdown_pct

        if (
            drawdown >= self.config.max_drawdown_usd
            or drawdown_pct >= self.config.max_drawdown_pct
        ):
            msg = (
                f"Cumulative drawdown ${drawdown:.2f} ({drawdown_pct * 100.0:.1f}%) reached limit "
                f"${self.config.max_drawdown_usd:.2f} ({self.config.max_drawdown_pct * 100.0:.1f}%)"
            )
            self.trip_circuit_breaker(CircuitBreakerReason.MAX_DRAWDOWN_LIMIT, msg)
            return st.status

        return st.status

    def can_merge(
        self,
        shares: float,
        captured_spread_usd: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Delegate complete-set merge eligibility to GasMergeManager."""
        return self.gas_merge_manager.can_merge(shares, captured_spread_usd)

    def record_merge(self, shares: float, gas_fee_usd: Optional[float] = None) -> None:
        """Delegate merge execution to GasMergeManager and update portfolio state."""
        self.gas_merge_manager.record_merge(shares, gas_fee_usd)
        self.portfolio_state.daily_gas_spent_usd = self.gas_merge_manager.daily_gas_spent
        self.portfolio_state.total_gas_spent_usd = self.gas_merge_manager.total_gas_spent
        self.portfolio_state.total_merges_count = self.gas_merge_manager.total_merges_count

    def evaluate_synthetic_hedge(
        self,
        current_inventory_q: float,
        fill_price: float,
        complementary_best_ask: float,
        recent_markout_loss_cents: float = 0.0,
    ) -> tuple[bool, str, float]:
        """Evaluate whether to trigger an active synthetic taker hedge on complementary asset (Regime B).

        Returns:
            (should_cross, reason, target_hedge_size)
        """
        if not self.config.enable_synthetic_hedging:
            return False, "synthetic_hedging_disabled", 0.0

        abs_q = abs(current_inventory_q)
        is_urgent_inventory = abs_q >= self.config.q_urgency_threshold_shares
        is_toxic_markout = recent_markout_loss_cents >= self.config.toxic_markout_threshold_cents

        if not (is_urgent_inventory or is_toxic_markout):
            return False, "inventory_within_passive_bounds", 0.0

        # Check combined basis: fill_price + complementary_ask <= 1.00 + tolerance
        combined_basis = fill_price + complementary_best_ask
        max_acceptable_basis = 1.00 + (self.config.max_synthetic_cross_spread_cents / 100.0)

        if combined_basis <= max_acceptable_basis:
            reason = f"urgent_hedge: basis {combined_basis:.4f} <= max {max_acceptable_basis:.4f}"
            if is_toxic_markout:
                reason += f" (toxic markout: {recent_markout_loss_cents:.2f}c)"
            hedge_size = min(abs_q, self.config.max_quote_size_shares)
            return True, reason, hedge_size

        return False, f"basis {combined_basis:.4f} exceeds max {max_acceptable_basis:.4f}", 0.0

    def resume(self, reset_peak: bool = False, reset_day_start: bool = False) -> None:
        """Safely resume after operator inspection."""
        self.portfolio_state.kill_switch_active = False
        self.portfolio_state.is_halted = False
        self.portfolio_state.status = RiskStatus.HEALTHY
        if reset_peak:
            self.portfolio_state.peak_equity_usd = self.portfolio_state.current_equity_usd
        if reset_day_start:
            self.portfolio_state.day_start_equity_usd = self.portfolio_state.current_equity_usd
        self.volatility_monitor.cooldowns.clear()
        self.toxicity_tracker.reset()
        logger.info("[RISK RESUME] Risk manager resumed quoting status: HEALTHY.")

    def get_risk_summary(self) -> dict[str, Any]:
        """Return full risk dashboard metrics."""
        summary = self.portfolio_state.to_dict()
        summary["config"] = {
            "total_bankroll_usd": self.config.total_bankroll_usd,
            "max_position_cost_per_market": self.config.max_position_cost_per_market,
            "max_position_shares_per_token": self.config.max_position_shares_per_token,
            "max_portfolio_inventory_usd": self.config.max_portfolio_inventory_usd,
            "daily_loss_limit_usd": self.config.daily_loss_limit_usd,
            "max_drawdown_usd": self.config.max_drawdown_usd,
            "price_jump_threshold_cents": self.config.price_jump_threshold_cents,
            "min_merge_threshold_shares": self.config.min_merge_threshold_shares,
            "max_gas_spread_pct": self.config.max_gas_spread_pct,
        }
        return summary
