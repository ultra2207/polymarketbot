"""Production-grade Risk Management System for Polymarket Liquidity Rewards.

Enforces:
1. Hard Capital & Position Limits ($300 bankroll, $60/market, 150 shares/token).
2. Max Drawdown & Daily Loss Circuit Breakers ($15/5% daily loss, $30/10% drawdown).
3. Price Jump & High-Frequency Volatility Breaker (>2.0c move within 60s -> 5m cooldown).
4. Unified microstructure gate: delegates each quote to ``UnifiedRiskEngine``
   (flow, L3 queue position, adverse-fill streak) when one is attached.
5. Gas & Complete-Set Merge Manager (scaled to 25-50 shares, gas fee <= 5% of captured spread).
6. Emergency Unquote / Kill-Switch & Heartbeat Monitor.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.risk.contracts import RiskAction
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
        self.last_mid: dict[str, float] = {}  # asset_id -> last recorded mid (dedupe)

    def record_mid_price(
        self,
        asset_id: str,
        condition_id: str,
        mid: float,
        timestamp: float | None = None,
    ) -> tuple[bool, float]:
        """Record a trusted mid-price observation and detect a rolling jump.

        Only distinct mids are appended (|delta| >= 1e-6) and at least two
        observations are required before a jump may trip, so duplicate prints
        of the same mid cannot fire the breaker.

        Returns:
            (is_jump_detected, max_delta_cents)
        """
        now = timestamp if timestamp is not None else time.time()
        price = float(mid)
        if not math.isfinite(price):
            return False, 0.0

        last = self.last_mid.get(asset_id)
        if last is not None and abs(price - last) < 1e-6:
            return False, 0.0
        self.last_mid[asset_id] = price

        if asset_id not in self.history:
            self.history[asset_id] = deque()
        queue = self.history[asset_id]
        queue.append((now, price))

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
            if now < self.cooldowns.get(asset_id, 0.0):
                # Already cooling down; do not extend the window on every tick.
                return False, delta_cents
            cooldown = max(
                0.0,
                min(
                    float(self.config.cooldown_duration_seconds),
                    float(self.config.max_price_jump_cooldown_seconds),
                ),
            )
            self.cooldowns[asset_id] = now + cooldown
            logger.warning(
                f"[PRICE JUMP BREAKER] Asset {asset_id} jumped {delta_cents:.2f}c within "
                f"{self.config.price_jump_window_seconds}s (limit: {self.config.price_jump_threshold_cents}c). "
                f"Entering {cooldown:.0f}s cooldown."
            )
            return True, delta_cents

        return False, delta_cents

    def record_price(
        self,
        asset_id: str,
        condition_id: str,
        mid_price: float,
        timestamp: float | None = None,
    ) -> tuple[bool, float]:
        """Backwards-compatible alias for :meth:`record_mid_price`."""
        return self.record_mid_price(asset_id, condition_id, mid_price, timestamp)

    def is_in_cooldown(self, asset_id: str, timestamp: float | None = None) -> bool:
        """Check if market is actively in volatility cooldown."""
        now = timestamp if timestamp is not None else time.time()
        return now < self.cooldowns.get(asset_id, 0.0)

    def get_remaining_cooldown(self, asset_id: str, timestamp: float | None = None) -> float:
        """Get seconds remaining in cooldown."""
        now = timestamp if timestamp is not None else time.time()
        return max(0.0, self.cooldowns.get(asset_id, 0.0) - now)

    def clear_cooldown(self, asset_id: str) -> None:
        """Manually clear cooldown for an asset."""
        if asset_id in self.cooldowns:
            del self.cooldowns[asset_id]


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
        captured_spread_usd: float | None,
        gas_fee_usd: float | None,
    ) -> tuple[bool, str]:
        """Check whether complete-set merge meets small-capital thresholds and gas budget.

        Rules:
        1. Must meet the configured merge threshold.
        2. The measured merge gas cannot exceed ``max_gas_spread_pct`` of the
           measured captured spread.
        3. Cumulative daily gas spend cannot exceed the configured daily cap.

        ``captured_spread_usd`` and ``gas_fee_usd`` are both measured at the
        merge site and both are required. The old signature defaulted the
        captured spread to ``shares x 1.0c`` and the gas to a $0.015 constant,
        so a caller that measured neither still got a permitted merge priced on
        two invented numbers.
        """
        if shares < self.config.min_merge_threshold_shares:
            return (
                False,
                f"Merge size {shares:.1f} sh below small capital threshold {self.config.min_merge_threshold_shares:.1f} sh",
            )

        if captured_spread_usd is None:
            return (
                False,
                (
                    "Captured spread is unmeasured; refusing to price the merge "
                    "gas hurdle against an assumed 1c per share"
                ),
            )
        spread_usd = float(captured_spread_usd)

        if spread_usd <= 0.0:
            return False, "Captured spread is non-positive; merge would incur pure gas drag"

        if spread_usd < self.config.min_merge_notional_usd:
            return (
                False,
                (f"Captured spread ${spread_usd:.3f} is below minimum merge notional "
                f"${self.config.min_merge_notional_usd:.2f}"),
            )

        if gas_fee_usd is None:
            return (
                False,
                (
                    "Merge gas is unmeasured; refusing to gate the merge on an "
                    "assumed $0.015 fee"
                ),
            )
        gas_usd = float(gas_fee_usd)

        gas_ratio = gas_usd / spread_usd
        if gas_ratio > self.config.max_gas_spread_pct + 1e-6:
            return (
                False,
                (f"Gas fee ${gas_usd:.4f} is {gas_ratio * 100.0:.1f}% of captured spread "
                f"${spread_usd:.3f}, exceeding {self.config.max_gas_spread_pct * 100.0:.1f}% cap"),
            )

        if self.daily_gas_spent + gas_usd > self.config.daily_gas_cap_usd:
            return (
                False,
                (f"Daily gas spend cap ${self.config.daily_gas_cap_usd:.2f} reached "
                f"(${self.daily_gas_spent:.3f} spent)"),
            )

        return True, "Merge permitted under small capital gas budget"

    def record_merge(self, shares: float, gas_fee_usd: float) -> None:
        """Record merge execution and update gas counters from the measured fee.

        ``gas_fee_usd`` is required: the ledger's daily gas spend is a real cost,
        and the old $0.015 default booked a price nobody measured for every
        merge that did not report one.
        """
        fee = float(gas_fee_usd)
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

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()
        self.portfolio_state = PortfolioRiskState(
            bankroll_usd=self.config.total_bankroll_usd,
            peak_equity_usd=self.config.total_bankroll_usd,
            day_start_equity_usd=self.config.total_bankroll_usd,
            current_equity_usd=self.config.total_bankroll_usd,
            current_cash_usd=self.config.total_bankroll_usd,
        )
        self.market_states: dict[str, MarketRiskState] = {}
        self._day_key: date | None = None

        # Specialized sub-managers
        self.volatility_monitor = PriceVolatilityMonitor(self.config)
        self.gas_merge_manager = GasMergeManager(self.config)

        # The single microstructure authority, wired by the engine that owns it.
        # Standalone constructions (tests, tooling) leave it unset and the
        # microstructure step is skipped rather than guessed.
        self._micro: Any | None = None

        # Unquote callbacks
        self._unquote_callbacks: list[Callable[[str | None, str], None]] = []

    def set_micro_risk(self, engine: Any) -> None:
        """Attach the unified microstructure risk engine used by ``validate_quote``."""
        self._micro = engine

    def register_unquote_callback(self, cb: Callable[[str | None, str], None]) -> None:
        """Register a callback to cancel active quotes when breakers trip or kill switch fires.

        Signature: cb(asset_id: Optional[str], reason: str)
        If asset_id is None, cancel all quotes across all markets.
        """
        self._unquote_callbacks.append(cb)

    def _trigger_unquote(self, asset_id: str | None, reason: str) -> None:
        """Execute all registered unquote callbacks."""
        for cb in self._unquote_callbacks:
            try:
                cb(asset_id, reason)
            except Exception as e:  # noqa: BLE001 - registered unquote callbacks are arbitrary user code
                logger.error(f"Error executing unquote callback: {e}")

    def _market_state(self, asset_id: str, condition_id: str | None = None) -> MarketRiskState:
        """Return (creating if needed) the per-asset risk state."""
        state = self.market_states.get(asset_id)
        if state is None:
            state = MarketRiskState(condition_id=condition_id or "", asset_id=asset_id)
            self.market_states[asset_id] = state
        elif condition_id and not state.condition_id:
            state.condition_id = condition_id
        return state

    def auto_clear_cooldowns(self, timestamp: float | None = None) -> None:
        """Drop expired volatility cooldowns and normalise the portfolio status.

        A volatility cooldown never halts the engine; the status is ``COOLDOWN``
        at trip time and ``WARNING`` while any cooldown is still running. Once
        every cooldown has expired the status returns to ``HEALTHY`` unless a
        permanent breaker (halt/kill switch) is active.
        """
        now = timestamp if timestamp is not None else time.time()
        expired = [
            asset_id
            for asset_id, expiry in self.volatility_monitor.cooldowns.items()
            if expiry <= now
        ]
        for asset_id in expired:
            self.volatility_monitor.cooldowns.pop(asset_id, None)
            state = self.market_states.get(asset_id)
            if state is not None and state.status == RiskStatus.COOLDOWN:
                state.status = RiskStatus.HEALTHY

        st = self.portfolio_state
        if st.kill_switch_active or st.is_halted:
            return
        if self.volatility_monitor.cooldowns:
            if st.status in (RiskStatus.COOLDOWN, RiskStatus.WARNING):
                st.status = RiskStatus.WARNING
        elif st.status in (RiskStatus.COOLDOWN, RiskStatus.WARNING):
            st.status = RiskStatus.HEALTHY

    def is_in_cooldown(self, asset_id: str, timestamp: float | None = None) -> bool:
        """Check volatility cooldown, clearing expired entries first."""
        self.auto_clear_cooldowns(timestamp)
        return self.volatility_monitor.is_in_cooldown(asset_id, timestamp)

    def trip_circuit_breaker(
        self,
        reason: CircuitBreakerReason,
        details: str,
        condition_id: str | None = None,
        asset_id: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        """Trip a circuit breaker.

        ``PRICE_JUMP_VOLATILITY`` enters a bounded per-asset cooldown and
        cancels that asset's quotes without halting the engine. All other
        reasons are permanent halts until :meth:`resume` is called.
        """
        now = timestamp if timestamp is not None else time.time()

        if reason == CircuitBreakerReason.PRICE_JUMP_VOLATILITY:
            cooldown = max(
                0.0,
                min(
                    float(self.config.cooldown_duration_seconds),
                    float(self.config.max_price_jump_cooldown_seconds),
                ),
            )
            if asset_id is not None:
                if now >= self.volatility_monitor.cooldowns.get(asset_id, 0.0):
                    self.volatility_monitor.cooldowns[asset_id] = now + cooldown
                self._market_state(asset_id, condition_id).status = RiskStatus.COOLDOWN
            st = self.portfolio_state
            if not st.kill_switch_active and not st.is_halted:
                st.status = RiskStatus.COOLDOWN if asset_id is not None else RiskStatus.WARNING

            event = CircuitBreakerEvent(
                timestamp=now,
                reason=reason,
                details=details,
                condition_id=condition_id,
                asset_id=asset_id,
                action_taken="COOLDOWN_AND_CANCEL_QUOTES" if asset_id is None else f"CANCEL_QUOTES_{asset_id}",
            )
            self.portfolio_state.circuit_breaker_events.append(event)
            logger.warning(
                f"[VOLATILITY COOLDOWN] {reason.value}: {details} "
                f"(cooldown {cooldown:.0f}s, not halted)"
            )
            self._trigger_unquote(asset_id, details)
            return

        self.portfolio_state.is_halted = True
        self.portfolio_state.status = RiskStatus.HALTED

        event = CircuitBreakerEvent(
            timestamp=now,
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

    def record_heartbeat(self, timestamp: float | None = None) -> None:
        """Record REST or WebSocket heartbeat."""
        now = timestamp if timestamp is not None else time.time()
        self.portfolio_state.last_heartbeat_timestamp = now

    def check_heartbeat(self, timestamp: float | None = None) -> bool:
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
        timestamp: float | None = None,
    ) -> tuple[bool, str | None]:
        """Record mid-price tick and verify price jump circuit breaker.

        Returns:
            (is_healthy, error_or_cooldown_message)
        """
        now = timestamp if timestamp is not None else time.time()
        self.record_heartbeat(now)
        self.auto_clear_cooldowns(now)

        jump_detected, delta_cents = self.volatility_monitor.record_mid_price(
            asset_id, condition_id, mid_price, now
        )

        if jump_detected:
            msg = (
                f"Mid-price moved {delta_cents:.2f}c within "
                f"{self.config.price_jump_window_seconds}s > threshold {self.config.price_jump_threshold_cents}c"
            )
            self.trip_circuit_breaker(
                CircuitBreakerReason.PRICE_JUMP_VOLATILITY,
                msg,
                condition_id=condition_id,
                asset_id=asset_id,
                timestamp=now,
            )
            return False, msg

        return True, None

    def validate_quote(
        self,
        asset_id: str,
        condition_id: str,
        side: str,
        price: float,
        size: float,
        current_pos_shares: float,
        current_pos_cost: float,
        current_portfolio_inventory_usd: float,
        current_cash_usd: float,
        mid_price: float | None,
        tick_size: float = 0.01,
        timestamp: float | None = None,
        event_id: str | None = None,
        current_event_inventory_usd: float | None = None,
        current_event_cost: float | None = None,
    ) -> tuple[bool, str, float, float]:
        """Comprehensive pre-trade risk check on a proposed virtual or live quote.

        Validates:
        - Engine state (halted/killed/breaker)
        - Market volatility cooldown
        - Microstructure: per-quote flow, queue position and adverse-fill streak
        - Hard bankroll and cash limits
        - Per-token share cap (150 shares)
        - Per-market cost cap ($60 notional)
        - Portfolio total inventory cap ($200)
        - Correlated-event inventory/cost cap (all markets sharing ``event_id``)

        The account state (``current_pos_shares``, ``current_pos_cost``,
        ``current_portfolio_inventory_usd``, ``current_cash_usd``) and
        ``mid_price`` have no defaults. They used to default to an empty
        position, zero inventory and a $300 cash balance, so a caller that
        forgot one gated the quote against an account that does not exist -
        and every forgotten position/cash field widened the limit rather than
        narrowing it. ``tick_size`` is accepted for callers that have it and is
        unused here: this gate does not round prices, the placement path does.

        Returns:
            (is_allowed, reason, approved_size, approved_price)
        """
        now = timestamp if timestamp is not None else time.time()
        self.auto_clear_cooldowns(now)

        # 1. Engine & breaker state
        if self.portfolio_state.kill_switch_active:
            return False, "Emergency kill switch is active", 0.0, price
        if self.portfolio_state.is_halted:
            return False, f"Risk engine is halted: {self.portfolio_state.status.value}", 0.0, price

        # 2. Market volatility cooldown
        if self.volatility_monitor.is_in_cooldown(asset_id, now):
            rem = self.volatility_monitor.get_remaining_cooldown(asset_id, now)
            return False, f"Market in volatility cooldown ({rem:.1f}s remaining)", 0.0, price

        # 3. Microstructure: per-quote flow, queue position and adverse fills.
        # No engine means no micro evidence, not a guessed threshold.
        approved_price = price
        if self._micro is not None:
            if mid_price is None:
                # The economics of holding a quote are priced from the distance
                # between our price and the mid. Defaulting the mid to our own
                # price states a zero capture and turns every measured hold cost
                # into a pull; refuse instead.
                return (
                    False,
                    (
                        "mid is unmeasured; refusing to price the quote's hold "
                        "cost against its own limit price"
                    ),
                    0.0,
                    price,
                )
            decision = self._micro.evaluate(asset_id, side, price, float(mid_price), now)
            if decision.action in (
                RiskAction.PULL,
                RiskAction.SUPPRESS,
                RiskAction.BLOCK,
            ):
                return False, decision.reason, 0.0, price
        candidate_size = size

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

            # Correlated-event caps: summed across every market in the event.
            # event_id is optional, so single-market callers keep the old caps.
            if (
                event_id
                and self.config.max_event_inventory_usd is not None
                and current_event_inventory_usd is not None
            ):
                event_inv_headroom = max(
                    0.0,
                    self.config.max_event_inventory_usd - current_event_inventory_usd,
                )
                candidate_size = min(
                    candidate_size, event_inv_headroom / max(1e-4, approved_price)
                )
            if (
                event_id
                and self.config.max_event_position_cost_usd is not None
                and current_event_cost is not None
            ):
                event_cost_headroom = max(
                    0.0,
                    self.config.max_event_position_cost_usd - current_event_cost,
                )
                candidate_size = min(
                    candidate_size, event_cost_headroom / max(1e-4, approved_price)
                )

            # Cash headroom ($300 bankroll)
            cash_headroom = max(0.0, current_cash_usd)
            max_shares_by_cash = cash_headroom / max(1e-4, approved_price)
            candidate_size = min(candidate_size, max_shares_by_cash)

        elif side_norm == "SELL":
            if self.config.allow_short:
                # Shorting allowed: bound the short by the per-token share cap.
                shares_headroom = max(0.0, self.config.max_position_shares_per_token - abs(current_pos_shares))
                candidate_size = min(candidate_size, shares_headroom)
            else:
                # Long-only: never sell more than is actually owned.
                candidate_size = min(candidate_size, max(0.0, current_pos_shares))

        if candidate_size <= 1e-4:
            if side_norm == "SELL" and not self.config.allow_short and max(0.0, current_pos_shares) <= 1e-4:
                return False, "SELL rejected: no owned shares and shorting is disabled", 0.0, approved_price
            return False, "Quote size reduced to 0 by position, cost, capital, or event limits", 0.0, approved_price

        return True, "Quote approved", round(candidate_size, 4), round(approved_price, 4)

    def record_fill(
        self,
        asset_id: str,
        condition_id: str,
        side: str,
        price: float,
        size: float,
        timestamp: float | None = None,
    ) -> None:
        """Record fill and refresh the heartbeat.

        Microstructure streak and queue bookkeeping live in the unified risk
        engine, which the quote path feeds directly via ``on_fill``.
        """
        now = timestamp if timestamp is not None else time.time()
        self.record_heartbeat(now)

    def reset_daily_if_needed(self, timestamp: float | None = None) -> bool:
        """Roll the daily accounting window at the first call after a UTC date change.

        Sets ``day_start_equity_usd`` to the latest marked equity, clears the
        daily PnL/loss counters and the daily gas spend. Permanent breakers
        (daily-loss halt, drawdown halt, kill switch) are NOT cleared here;
        an operator still has to call :meth:`resume`.

        Returns:
            True when a rollover happened.
        """
        now = timestamp if timestamp is not None else time.time()
        day = datetime.fromtimestamp(now, tz=UTC).date()
        if self._day_key is None:
            self._day_key = day
            return False
        if day == self._day_key:
            return False

        st = self.portfolio_state
        st.day_start_equity_usd = st.current_equity_usd
        st.daily_loss_usd = 0.0
        st.daily_realized_pnl = 0.0
        st.daily_unrealized_pnl = 0.0
        st.daily_gas_spent_usd = 0.0
        self.gas_merge_manager.reset_daily()
        self._day_key = day
        logger.info(
            f"[DAILY ROLLOVER] UTC day {day.isoformat()}: day_start_equity reset to "
            f"${st.day_start_equity_usd:.2f}, daily PnL and gas counters cleared."
        )
        return True

    def update_portfolio_pnl(
        self,
        current_equity: float,
        realized_pnl: float,
        unrealized_pnl: float,
        current_cash: float | None = None,
        positions_market_value: float | None = None,
        timestamp: float | None = None,
    ) -> RiskStatus:
        """Update PnL, equity, peak values, and evaluate Drawdown & Daily Loss Breakers.

        Rules:
        - Daily loss limit: $15 or 5% of bankroll.
        - Max drawdown limit: $30 or 10% of peak equity.
        """
        now = timestamp if timestamp is not None else time.time()
        self.record_heartbeat(now)
        self.auto_clear_cooldowns(now)

        st = self.portfolio_state
        st.current_equity_usd = current_equity
        self.reset_daily_if_needed(now)
        st.daily_realized_pnl = realized_pnl
        st.daily_unrealized_pnl = unrealized_pnl
        if current_cash is not None:
            st.current_cash_usd = current_cash
        if positions_market_value is not None:
            st.current_positions_value_usd = positions_market_value

        # Update peak equity
        st.peak_equity_usd = max(st.peak_equity_usd, current_equity)

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
        captured_spread_usd: float | None,
        gas_fee_usd: float | None,
    ) -> tuple[bool, str]:
        """Delegate complete-set merge eligibility to GasMergeManager."""
        return self.gas_merge_manager.can_merge(
            shares, captured_spread_usd, gas_fee_usd
        )

    def record_merge(self, shares: float, gas_fee_usd: float) -> None:
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

    def get_hedging_cash_reserve(self) -> float:
        """Return the reserved liquid cash amount for synthetic hedging under micro-capital model."""
        if self.config.total_bankroll_usd <= 500.0 and self.config.concentrated_micro_capital:
            return round(self.config.total_bankroll_usd * self.config.micro_capital_cash_reserve_pct, 2)
        return 0.0

    def get_effective_capital_allocation(self) -> float:
        """Return maximum allocated capital on the top quoting market under micro-capital model."""
        return self.config.max_position_cost_per_market

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
        for state in self.market_states.values():
            if state.status == RiskStatus.COOLDOWN:
                state.status = RiskStatus.HEALTHY
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
