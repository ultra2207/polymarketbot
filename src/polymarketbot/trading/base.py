"""Core Shared Market-Making Architecture for Polymarket Prediction Markets.

Implements BaseMarketMakingEngine, providing unified quantitative strategies
for both self-hosted paper trading (NativePaperTradingEngine) and production
real-money trading (RealMoneyTradingEngine).

Unified Features:
1. Two-Tier Dynamic De-Skewing Engine (DynamicDeSkewOptimizer):
   - Tier 1: Passive Avellaneda-Stoikov quote fading & complete-set tightening.
   - Tier 2: Emergency active synthetic complete-set cross-hedging.
2. Cont-Kukanov-Stoikov Order Flow Imbalance (OFI) & Stoikov Micro-Price Drift Sentinels.
3. Master RiskManager Circuit Breakers:
   - High-frequency price jump detection (>2.0c within 60s -> 300s cooldown).
   - Daily loss limit ($15.00 or 5% bankroll).
   - Maximum cumulative drawdown breaker ($30.00 or 10% peak equity).
   - Emergency unquote kill-switch.
4. Continuous Capital Compounding (ContinuousOptimizationGovernor & calculate_endogenous_reserve).
5. Exact Quadratic Liquidity Rewards Scoring across 1,440 daily epochs with $1.00 payout floor.
6. Opportunistic Market Migration & Economic Switching Hurdle (CapitalMigrationEngine).
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from polymarketbot.logs import get_logger
from polymarketbot.rewards.allocator import (
    ContinuousOptimizationGovernor,
    calculate_endogenous_reserve,
)
from polymarketbot.rewards.deskew import (
    DeSkewDecision,
    DeSkewPathway,
    DynamicDeSkewOptimizer,
)
from polymarketbot.rewards.microstructure import (
    MicrostructureEngine,
    compute_dynamic_cooldown_duration,
    compute_dynamic_volatility_breaker_threshold,
)
from polymarketbot.rewards.queue_valuation import (
    CapitalMigrationEngine,
    HurdleDecision,
    SwitchingCostDecomposition,
)
from polymarketbot.rewards.scoring import quadratic_order_score
from polymarketbot.risk.manager import RiskManager
from polymarketbot.risk.models import CircuitBreakerReason, RiskConfig

logger = get_logger("trading.base")


@dataclass
class PositionView:
    """Read-only view of a market condition's inventory and cost basis."""

    condition_id: str
    shares_yes: float = 0.0
    shares_no: float = 0.0
    avg_cost_yes: float = 0.0
    avg_cost_no: float = 0.0
    realized_pnl: float = 0.0

    @property
    def complete_sets(self) -> float:
        return min(self.shares_yes, self.shares_no)

    @property
    def net_exposure(self) -> float:
        return self.shares_yes - self.shares_no


class BaseMarketMakingEngine:
    """Unified Base Class for Polymarket Market Making Strategy Orchestration."""

    def __init__(
        self,
        condition_id: str,
        asset_id_yes: str = "TOKEN_YES",
        asset_id_no: str = "TOKEN_NO",
        quote_size: float = 20.0,
        spread_offset_cents: float = 0.50,
        daily_reward_rate: float = 20.0,
        rewards_max_spread_cents: float = 1.0,
        rewards_min_size: float = 20.0,
        auto_merge_threshold: Optional[float] = None,
        gas_fee_merge: Optional[float] = None,
        institutional_mode: bool = True,
        gamma: float = 0.02,
        competing_score_override: Optional[float] = None,
        db_path: Union[str, Path] = "data/trading.db",
        ledger_path: Union[str, Path] = "data/trading_ledger.json",
        deskew_optimizer: Optional[DynamicDeSkewOptimizer] = None,
        migration_engine: Optional[CapitalMigrationEngine] = None,
        governor: Optional[ContinuousOptimizationGovernor] = None,
        risk_manager: Optional[RiskManager] = None,
        price_jump_threshold_cents: Optional[float] = None,
        cooldown_duration_seconds: Optional[float] = None,
    ) -> None:
        self.condition_id = condition_id
        self.asset_id_yes = asset_id_yes
        self.asset_id_no = asset_id_no
        self.quote_size = float(quote_size)
        self.spread_offset_cents = float(spread_offset_cents)
        self.daily_reward_rate = float(daily_reward_rate)
        self.rewards_max_spread = float(rewards_max_spread_cents)
        self.rewards_min_size = float(rewards_min_size)
        self.price_jump_threshold_cents = (
            float(price_jump_threshold_cents) if price_jump_threshold_cents is not None else None
        )
        self.cooldown_duration_seconds = (
            float(cooldown_duration_seconds) if cooldown_duration_seconds is not None else None
        )

        if gas_fee_merge is not None:
            self.gas_fee_merge = float(gas_fee_merge)
        else:
            from polymarketbot.trading.ctf import estimate_merge_cost_usd
            self.gas_fee_merge = float(estimate_merge_cost_usd())

        if auto_merge_threshold is not None:
            self.auto_merge_threshold = float(auto_merge_threshold)
        else:
            from polymarketbot.trading.ctf import calculate_optimal_merge_threshold
            self.auto_merge_threshold = float(
                calculate_optimal_merge_threshold(gas_cost_usd=self.gas_fee_merge)
            )

        self.institutional_mode = bool(institutional_mode)
        self.gamma = float(gamma)
        self.competing_score_override = (
            float(competing_score_override) if competing_score_override is not None else None
        )

        # Persistence paths
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

        # Quantitative De-Skew Optimizer
        self.deskew_optimizer = deskew_optimizer or DynamicDeSkewOptimizer(
            gamma=self.gamma,
            gas_cost_merge_usd=self.gas_fee_merge,
            q_soft_threshold=15.0,
            q_hard_threshold=100.0,
            max_holding_seconds=300.0,
        )

        # Microstructure Sentinels for OFI & Micro-Price Drift
        self.microstructure_yes = MicrostructureEngine(ofi_threshold=850.0, drift_threshold_cents=1.5)
        self.microstructure_no = MicrostructureEngine(ofi_threshold=850.0, drift_threshold_cents=1.5)

        # Capital Migration & Switching Hurdle Engine
        self.migration_engine = migration_engine or CapitalMigrationEngine()
        self.migration_history: List[dict[str, Any]] = []
        self.latest_hurdle_decision: Optional[HurdleDecision] = None
        self.latest_switching_cost: Optional[SwitchingCostDecomposition] = None

        # Continuous Optimization Governor
        self.governor = governor or ContinuousOptimizationGovernor()
        if self.condition_id not in self.governor.market_entry_times:
            self.governor.market_entry_times[self.condition_id] = time.time()

        # Risk Management & Circuit Breakers
        self.risk_manager = risk_manager or RiskManager(
            RiskConfig(
                price_jump_window_seconds=60.0,
                price_jump_threshold_cents=2.0,
                cooldown_duration_seconds=300.0,
                daily_loss_limit_usd=15.0,
                max_drawdown_usd=30.0,
            )
        )
        self.risk_manager.register_unquote_callback(self._on_risk_unquote)

        # Price jump rolling buffer
        self._rolling_price_ticks: deque[tuple[float, float]] = deque()
        self._volatility_cooldown_until: float = 0.0
        self._trigger_instant_scan: bool = False
        self._last_cooldown_log_time: float = 0.0

        # Best bid/ask cache: token_side -> (bid, ask)
        self._best_bids: dict[str, float] = {}
        self._best_asks: dict[str, float] = {}

        # Quote Registry: quote_id -> dict
        self.active_quotes: Dict[str, dict[str, Any]] = {}

        # Epoch & Telemetry State
        self.epoch_index: int = 0
        self.last_epoch_ts: float = time.time()
        self.total_rewards_credited: float = 0.0
        self.latest_deskew_decision: Optional[DeSkewDecision] = None

        # SQLite Connection
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._init_sqlite()

    # -------------------------------------------------------------------------
    # Database Initialization
    # -------------------------------------------------------------------------

    def _init_sqlite(self) -> None:
        """Create high-performance SQLite schema with WAL journal mode."""
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode = WAL;")
        cur.execute("PRAGMA synchronous = NORMAL;")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                cash REAL NOT NULL,
                market_value REAL NOT NULL,
                equity REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                total_pnl REAL NOT NULL,
                total_rewards REAL NOT NULL,
                total_gas_fees REAL NOT NULL,
                total_merges INTEGER NOT NULL,
                total_splits INTEGER NOT NULL,
                total_volume REAL NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                shares_yes REAL NOT NULL,
                shares_no REAL NOT NULL,
                avg_cost_yes REAL NOT NULL,
                avg_cost_no REAL NOT NULL,
                net_exposure REAL NOT NULL,
                complete_sets REAL NOT NULL,
                realized_pnl REAL NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS active_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                quote_id TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                token_side TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                queue_ahead REAL NOT NULL,
                score REAL NOT NULL,
                status TEXT NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fill_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                token_side TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                liquidity_type TEXT NOT NULL,
                slippage_cents REAL NOT NULL,
                queue_depleted REAL NOT NULL,
                post_fill_drift REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                cash_after REAL NOT NULL,
                notes TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS deskew_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                pathway TEXT NOT NULL,
                urgency TEXT NOT NULL,
                target_token TEXT NOT NULL,
                action_side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                savings_cents REAL NOT NULL,
                reason TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS reward_epochs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                epoch_index INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                user_score REAL NOT NULL,
                competing_score REAL NOT NULL,
                pool_share_pct REAL NOT NULL,
                epoch_payout_usd REAL NOT NULL,
                projected_daily_usd REAL NOT NULL,
                floor_met INTEGER NOT NULL,
                cumulative_rewards_usd REAL NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ctf_merges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                complete_sets REAL NOT NULL,
                usdc_gross REAL NOT NULL,
                gas_fee REAL NOT NULL,
                net_cash REAL NOT NULL,
                realized_gain REAL NOT NULL,
                cash_after REAL NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ctf_splits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                condition_id TEXT NOT NULL,
                amount_split REAL NOT NULL,
                cash_after REAL NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                from_condition_id TEXT NOT NULL,
                to_condition_id TEXT NOT NULL,
                delta_yield_daily REAL NOT NULL,
                switching_cost REAL NOT NULL,
                net_alpha_holding REAL NOT NULL,
                unlocked_cash REAL NOT NULL,
                reason TEXT NOT NULL
            );
        """)

        # Performance indexes for high-frequency queries and monitoring
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reward_epochs_ts ON reward_epochs(timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_cond ON positions(condition_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_migrations_ts ON market_migrations(timestamp);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_deskew_ts ON deskew_events(timestamp);")
        self._conn.commit()

    # -------------------------------------------------------------------------
    # Subclass Abstract / Execution Hooks
    # -------------------------------------------------------------------------

    def _set_quote(
        self,
        token_side: str,
        quote_side: str,
        price: float,
        size: float,
        ts: float,
    ) -> None:
        """Place or update a quote (virtual maker quote or live CLOB order)."""
        raise NotImplementedError

    def _cancel_quote(self, qid: str) -> None:
        """Cancel an individual resting quote."""
        raise NotImplementedError

    def cancel_all_quotes(self) -> None:
        """Cancel all resting quotes across all tokens and sides."""
        raise NotImplementedError

    def _execute_tier2_taker_deskew(self, decision: DeSkewDecision, ts: float) -> None:
        """Execute Tier 2 emergency taker or synthetic merge."""
        raise NotImplementedError

    def trigger_ctf_merge(self, amount_sets: Optional[float] = None) -> Any:
        """Execute complete-set CTF merge."""
        raise NotImplementedError

    def trigger_ctf_split(self, amount_usdc: float) -> Any:
        """Execute CTF split."""
        raise NotImplementedError

    def get_cash(self) -> float:
        """Get available cash balance in USD."""
        raise NotImplementedError

    def get_position(self, condition_id: Optional[str] = None) -> PositionView:
        """Get current position state for target condition ID."""
        raise NotImplementedError

    def get_equity(self, prices: Optional[dict[str, tuple[float, float]]] = None) -> float:
        """Get total portfolio equity (cash + inventory market value)."""
        raise NotImplementedError

    def get_best_bid_ask(self, token_side: str) -> Tuple[float, float]:
        """Get best real bid and ask prices for target outcome token."""
        tok = token_side.upper()
        bid = self._best_bids.get(tok, 0.49 if tok == "YES" else 0.49)
        ask = self._best_asks.get(tok, 0.51 if tok == "YES" else 0.51)
        return bid, ask

    def credit_reward(self, amount: float, condition_id: str, note: str = "") -> None:
        """Hook for crediting settled liquidity rewards to portfolio cash."""
        pass

    # -------------------------------------------------------------------------
    # Circuit Breakers & Volatility Protection
    # -------------------------------------------------------------------------

    @property
    def is_volatility_cooldown_active(self) -> bool:
        """Check if volatility cooldown is currently active."""
        if self._rolling_price_ticks:
            latest_tick_ts = self._rolling_price_ticks[-1][0]
            if latest_tick_ts < 1e8:  # Simulated relative timestamp
                return latest_tick_ts < self._volatility_cooldown_until
        return time.time() < self._volatility_cooldown_until

    @property
    def volatility_cooldown_until(self) -> float:
        """Timestamp until which volatility cooldown remains active."""
        return self._volatility_cooldown_until

    def _on_risk_unquote(self, asset_id: Optional[str], reason: str) -> None:
        """Callback from master RiskManager: emergency cancel resting quotes."""
        logger.warning(f"[RISK MANAGER UNQUOTE] Cancelling active quotes due to: {reason}")
        self.cancel_all_quotes()

    def pop_instant_scan_trigger(self) -> bool:
        """Check and reset the instant opportunity scan trigger flag."""
        if self._trigger_instant_scan:
            self._trigger_instant_scan = False
            return True
        return False

    def trigger_instant_scan(self) -> None:
        """Manually trigger an instant opportunity scan (e.g. on hold or cooldown)."""
        self._trigger_instant_scan = True

    def _check_volatility_breaker(self, mid_yes: float, ts: Optional[float] = None) -> bool:
        """Track rolling price ticks. If mid jumps > 2.0c within 60s, cancel quotes and cool down for 300s."""
        now = ts if ts is not None else time.time()

        if now < self._volatility_cooldown_until:
            if self.active_quotes:
                self.cancel_all_quotes()
            # Throttle repetitive logging to once every 5.0 seconds
            if now - self._last_cooldown_log_time >= 5.0:
                self._last_cooldown_log_time = now
                logger.warning(
                    f"[ASYMMETRIC TOXICITY DEFENSE] Volatility cooldown active for condition {self.condition_id} "
                    f"({self._volatility_cooldown_until - now:.1f}s remaining). Quotes cancelled/suppressed."
                )
            return True

        # Evict ticks older than 60s
        cutoff = now - 60.0
        while self._rolling_price_ticks and self._rolling_price_ticks[0][0] < cutoff:
            self._rolling_price_ticks.popleft()

        if not (self._rolling_price_ticks and self._rolling_price_ticks[-1] == (now, float(mid_yes))):
            self._rolling_price_ticks.append((now, float(mid_yes)))

        prices = [p for _, p in self._rolling_price_ticks]
        max_px = max(prices)
        min_px = min(prices)
        jump_cents = (max_px - min_px) * 100.0

        if self.price_jump_threshold_cents is not None:
            dyn_thresh = self.price_jump_threshold_cents
        else:
            dyn_thresh = compute_dynamic_volatility_breaker_threshold(mid_price=float(mid_yes), spread_cents=1.0)

        if jump_cents > dyn_thresh:
            if self.cooldown_duration_seconds is not None:
                cooldown_dur = self.cooldown_duration_seconds
            else:
                cooldown_dur = compute_dynamic_cooldown_duration(
                    jump_cents_observed=jump_cents,
                    breaker_threshold_cents=dyn_thresh,
                )
            self._volatility_cooldown_until = now + cooldown_dur
            self._trigger_instant_scan = True
            self._last_cooldown_log_time = now
            self.cancel_all_quotes()
            msg = f"Mid-price jump {jump_cents:.2f}c > {dyn_thresh:.2f}c within 60s."
            self.risk_manager.trip_circuit_breaker(
                CircuitBreakerReason.PRICE_JUMP_VOLATILITY,
                msg,
                condition_id=self.condition_id,
                asset_id=self.asset_id_yes,
            )
            logger.warning(
                f"[ASYMMETRIC TOXICITY DEFENSE] Dynamic price jump volatility breaker triggered: {msg} "
                f"Active quotes cancelled; entering {cooldown_dur:.0f}s cooldown until ts={self._volatility_cooldown_until:.1f}. "
                f"Triggering instant opportunity scanner for market rotation."
            )
            return True

        return False

    # -------------------------------------------------------------------------
    # Quoting Logic & 2-Tier De-Skewing Strategy
    # -------------------------------------------------------------------------

    def evaluate_quotes(self, timestamp: Optional[float] = None) -> None:
        """Run 2-Tier Dynamic De-Skewing Optimizer and generate maker quotes."""
        ts = timestamp or time.time()
        pos = self.get_position(self.condition_id)

        # Check master RiskManager halt or kill-switch status
        if self.risk_manager.portfolio_state.is_halted or self.risk_manager.portfolio_state.kill_switch_active:
            if self.active_quotes:
                self.cancel_all_quotes()
            return

        # Immediate Risk Breaker check on current mark-to-market equity
        cash_val = self.get_cash()
        equity_val = self.get_equity()
        self.risk_manager.update_portfolio_pnl(
            current_equity=equity_val,
            realized_pnl=pos.realized_pnl + self.total_rewards_credited,
            unrealized_pnl=equity_val - cash_val,
            current_cash=cash_val,
            timestamp=ts,
        )
        if self.risk_manager.portfolio_state.is_halted or self.risk_manager.portfolio_state.kill_switch_active:
            if self.active_quotes:
                self.cancel_all_quotes()
            return

        b_yes, a_yes = self.get_best_bid_ask("YES")
        b_no, a_no = self.get_best_bid_ask("NO")

        mid_yes = (b_yes + a_yes) / 2.0

        # Check Price Jump Volatility Circuit Breaker
        if self._check_volatility_breaker(mid_yes, ts):
            return

        # Microstructure signals
        state_yes = self.microstructure_yes.latest_state
        rolling_ofi = state_yes.ofi_10_events if state_yes else 0.0
        drift_cents = state_yes.drift_cents if state_yes else 0.0

        # Evaluate skew
        decision = self.deskew_optimizer.evaluate_skew(
            shares_yes=pos.shares_yes,
            shares_no=pos.shares_no,
            best_bid_yes=b_yes,
            best_ask_yes=a_yes,
            best_bid_no=b_no,
            best_ask_no=a_no,
            rolling_ofi=rolling_ofi,
            micro_price_drift_cents=drift_cents,
            time_to_resolution_days=7.0,
        )
        self.latest_deskew_decision = decision

        # Tier 2: Emergency Taker
        if decision.pathway in (DeSkewPathway.SYNTHETIC_MERGE, DeSkewPathway.DIRECT_CROSS):
            self._execute_tier2_taker_deskew(decision, ts)
            return

        # Tier 1: Passive Avellaneda-Stoikov Quote Fading
        if decision.pathway == DeSkewPathway.PASSIVE_FADE:
            net_q = pos.shares_yes - pos.shares_no
            if abs(net_q) >= self.deskew_optimizer.q_soft:
                self._execute_tier1_passive_fade(decision, mid_yes, b_yes, a_yes, ts)
                return
            else:
                self._record_deskew_event(decision, ts)
                logger.warning(
                    f"[ASYMMETRIC TOXICITY DEFENSE] {decision.reason} "
                    f"(fading {decision.action_side} {decision.target_token})"
                )

        # Neutral Two-Sided Maker Quoting around Mid
        self._execute_neutral_maker_quoting(mid_yes, b_yes, a_yes, ts)

    def _execute_tier1_passive_fade(
        self,
        decision: DeSkewDecision,
        mid_yes: float,
        best_bid_yes: float,
        best_ask_yes: float,
        ts: float,
    ) -> None:
        """Tier 1: Avellaneda-Stoikov inventory quote fading."""
        self._record_deskew_event(decision, ts)

        if self.institutional_mode:
            pos = self.get_position(self.condition_id)
            net_q = pos.shares_yes - pos.shares_no
            p = max(0.01, min(0.99, mid_yes))
            variance = (p * (1.0 - p)) / 7.0
            r_yes = max(0.01, min(0.99, mid_yes - (net_q * self.gamma * variance)))
            offset = self.spread_offset_cents / 100.0

            if net_q > 0:
                # Long YES -> tighten NO bid to complete sets; fade/widen YES bid
                target_bid_no = round(min(0.98, max(0.01, 1.00 - max(best_bid_yes + 0.001, decision.target_price))), 3)
                faded_bid_yes = round(max(0.01, min(best_ask_yes - 0.001, r_yes - offset - 0.01)), 3)
                deskew_size = max(self.quote_size, net_q)
                self._set_quote("NO", "BUY", target_bid_no, deskew_size, ts)
                self._set_quote("YES", "BUY", faded_bid_yes, self.quote_size, ts)
            elif net_q < 0:
                # Long NO -> tighten YES bid to complete sets; fade/widen NO bid
                target_bid_yes = round(max(0.01, min(best_ask_yes - 0.001, decision.target_price)), 3)
                faded_bid_no = round(max(0.01, min(0.98, 1.00 - (r_yes + offset + 0.01))), 3)
                deskew_size = max(self.quote_size, abs(net_q))
                self._set_quote("YES", "BUY", target_bid_yes, deskew_size, ts)
                self._set_quote("NO", "BUY", faded_bid_no, self.quote_size, ts)
            else:
                self._execute_neutral_maker_quoting(mid_yes, best_bid_yes, best_ask_yes, ts)
                return

            self._cancel_quote("SELL_YES")
            self._cancel_quote("SELL_NO")
            return

        # Legacy Single-Token Mode
        if decision.target_token == "YES" and decision.action_side == "SELL":
            faded_ask = min(best_ask_yes, max(best_bid_yes + 0.001, decision.target_price))
            self._set_quote("YES", "SELL", faded_ask, self.quote_size, ts)
            self._cancel_quote("BID_YES")
            self._cancel_quote("BUY_YES")
        elif decision.target_token == "YES" and decision.action_side == "BUY":
            faded_bid = max(best_bid_yes, min(best_ask_yes - 0.001, decision.target_price))
            self._set_quote("YES", "BUY", faded_bid, self.quote_size, ts)
            self._cancel_quote("ASK_YES")
            self._cancel_quote("SELL_YES")

    def _execute_neutral_maker_quoting(
        self,
        mid_yes: float,
        best_bid_yes: float,
        best_ask_yes: float,
        ts: float,
    ) -> None:
        """Standard two-sided maker quotes with asymmetric toxicity quote fading."""
        if self._check_volatility_breaker(mid_yes, ts):
            return

        offset = self.spread_offset_cents / 100.0

        # Query microstructure actions
        action_buy = self.microstructure_yes.evaluate_quote_action("BUY")
        action_sell = self.microstructure_yes.evaluate_quote_action("SELL")

        bid_offset = offset
        ask_offset = offset
        suppress_bid = False
        suppress_ask = False

        if action_sell.should_pull:
            suppress_ask = True
            logger.warning(
                f"[ASYMMETRIC TOXICITY DEFENSE] Suppressing ask quote (toxic buying flow): {action_sell.reason}"
            )
        elif action_sell.should_widen:
            widen_val = action_sell.spread_offset_cents / 100.0
            ask_offset += widen_val
            logger.warning(
                f"[ASYMMETRIC TOXICITY DEFENSE] Widening ask offset by +{action_sell.spread_offset_cents:.2f}c: {action_sell.reason}"
            )

        if action_buy.should_pull:
            suppress_bid = True
            logger.warning(
                f"[ASYMMETRIC TOXICITY DEFENSE] Suppressing bid quote (toxic selling flow): {action_buy.reason}"
            )
        elif action_buy.should_widen:
            widen_val = action_buy.spread_offset_cents / 100.0
            bid_offset += widen_val
            logger.warning(
                f"[ASYMMETRIC TOXICITY DEFENSE] Widening bid offset by +{action_buy.spread_offset_cents:.2f}c: {action_buy.reason}"
            )

        cash_balance = self.get_cash()

        if self.institutional_mode:
            # Jane Street Institutional Dual-Cash Quoting (BUY YES + BUY NO)
            pos = self.get_position(self.condition_id)
            net_q = pos.shares_yes - pos.shares_no
            p = max(0.01, min(0.99, mid_yes))
            variance = (p * (1.0 - p)) / 7.0
            r_yes = max(0.01, min(0.99, mid_yes - (net_q * self.gamma * variance)))

            skew_adj = net_q * 0.0005
            target_bid_yes = r_yes - bid_offset - max(0.0, skew_adj)
            target_ask_yes = r_yes + ask_offset - min(0.0, skew_adj)

            target_bid_yes = min(best_ask_yes - 0.001, target_bid_yes)
            target_ask_yes = max(best_bid_yes + 0.001, target_ask_yes)

            target_bid_yes = round(max(0.01, min(0.98, target_bid_yes)), 3)
            target_ask_yes = round(max(target_bid_yes + 0.002, min(0.99, target_ask_yes)), 3)
            target_bid_no = round(1.00 - target_ask_yes, 3)

            # Enforce cash sizing bounds
            active_price_sum = 0.0
            if not suppress_bid:
                active_price_sum += target_bid_yes
            if not suppress_ask:
                active_price_sum += target_bid_no

            if active_price_sum > 0:
                req_cash = active_price_sum * self.quote_size
                eff_size = self.quote_size if cash_balance >= req_cash else max(5.0, math.floor(cash_balance / active_price_sum))
            else:
                eff_size = self.quote_size

            # Place quotes or cancel if suppressed
            if not suppress_bid:
                self._set_quote("YES", "BUY", target_bid_yes, eff_size, ts)
            else:
                self._cancel_quote("BUY_YES")

            if not suppress_ask:
                self._set_quote("NO", "BUY", target_bid_no, eff_size, ts)
            else:
                self._cancel_quote("BUY_NO")

            self._cancel_quote("SELL_YES")
            self._cancel_quote("SELL_NO")
            return

        # Legacy Single-Token Mode (BUY YES + SELL YES)
        target_bid = round(max(0.01, mid_yes - bid_offset), 3)
        target_ask = round(min(0.99, mid_yes + ask_offset), 3)

        if target_bid >= best_ask_yes:
            target_bid = round(best_ask_yes - 0.001, 3)
        if target_ask <= best_bid_yes:
            target_ask = round(best_bid_yes + 0.001, 3)

        if not suppress_bid:
            self._set_quote("YES", "BUY", target_bid, self.quote_size, ts)
        else:
            self._cancel_quote("BUY_YES")

        if not suppress_ask:
            self._set_quote("YES", "SELL", target_ask, self.quote_size, ts)
        else:
            self._cancel_quote("SELL_YES")

    def update_quote_size(self, new_quote_size: float, timestamp: Optional[float] = None) -> bool:
        """Safely update quote size and re-evaluate active quotes if changed by >= 5.0 shares."""
        new_size = round(max(5.0, float(new_quote_size)), 1)
        size_diff = abs(new_size - self.quote_size)
        old_size = self.quote_size
        self.quote_size = new_size

        if size_diff >= 5.0:
            ts = timestamp or time.time()
            logger.info(
                f"Quote size compounded: {old_size:.1f} -> {new_size:.1f} shares "
                f"(diff={size_diff:.1f} >= 5.0). Re-evaluating active quotes."
            )
            self.evaluate_quotes(timestamp=ts)
            return True

        return False

    def _record_deskew_event(self, decision: DeSkewDecision, ts: float) -> None:
        """Persist dynamic de-skew decision to SQLite."""
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO deskew_events (
                timestamp, condition_id, pathway, urgency, target_token,
                action_side, price, size, savings_cents, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                self.condition_id,
                decision.pathway.value,
                decision.urgency_level,
                decision.target_token,
                decision.action_side,
                decision.target_price,
                decision.target_size,
                decision.synthetic_savings_cents,
                decision.reason,
            ),
        )
        self._conn.commit()

    # -------------------------------------------------------------------------
    # Liquidity Rewards Epoch & Capital Compounding
    # -------------------------------------------------------------------------

    def process_reward_epoch(
        self,
        timestamp: Optional[float] = None,
        competing_score: float = 0.0,
    ) -> dict[str, Any]:
        """Process 1-minute quadratic reward epoch and trigger continuous compounding."""
        ts = timestamp or time.time()
        self.epoch_index += 1
        self.last_epoch_ts = ts

        # Total User Score S(v, s)
        total_user_score = sum(float(q.get("score", 0.0)) for q in self.active_quotes.values())

        if self.competing_score_override is not None and self.competing_score_override > 0.0:
            competing_score = max(self.competing_score_override, competing_score)
        else:
            competing_score = max(0.0, competing_score)

        total_pool_score = total_user_score + competing_score
        pool_share_pct = (total_user_score / total_pool_score) * 100.0 if total_pool_score > 0 else 0.0

        epoch_budget_usd = self.daily_reward_rate / 1440.0
        raw_epoch_payout = (total_user_score / total_pool_score) * epoch_budget_usd if total_pool_score > 0 else 0.0
        projected_daily_usd = raw_epoch_payout * 1440.0

        # $1.00 Daily Payout Floor Rule
        floor_met = projected_daily_usd >= 1.00
        actual_epoch_payout = raw_epoch_payout if floor_met else 0.0

        if actual_epoch_payout > 0:
            self.total_rewards_credited += actual_epoch_payout
            self.credit_reward(
                actual_epoch_payout,
                condition_id=self.condition_id,
                note=f"Epoch {self.epoch_index} Reward ({pool_share_pct:.2f}% share)",
            )

        # Record to SQLite
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO reward_epochs (
                epoch_index, timestamp, condition_id, user_score, competing_score,
                pool_share_pct, epoch_payout_usd, projected_daily_usd, floor_met,
                cumulative_rewards_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.epoch_index,
                ts,
                self.condition_id,
                total_user_score,
                competing_score,
                pool_share_pct,
                actual_epoch_payout,
                projected_daily_usd,
                1 if floor_met else 0,
                self.total_rewards_credited,
            ),
        )
        self._conn.commit()

        # Update Risk Manager PnL & Check Daily Loss / Drawdown Breakers
        cash_val = self.get_cash()
        equity_val = self.get_equity()
        pos = self.get_position(self.condition_id)
        risk_status = self.risk_manager.update_portfolio_pnl(
            current_equity=equity_val,
            realized_pnl=pos.realized_pnl + self.total_rewards_credited,
            unrealized_pnl=equity_val - cash_val,
            current_cash=cash_val,
            timestamp=ts,
        )

        # Continuous Capital Compounding via Governor
        b_yes, a_yes = self.get_best_bid_ask("YES")
        mid_yes = (b_yes + a_yes) / 2.0
        cand = {
            "condition_id": self.condition_id,
            "question": f"Market {self.condition_id[:12]}",
            "daily_pool": self.daily_reward_rate,
            "competing_score": max(1.0, competing_score),
            "max_spread": self.rewards_max_spread,
            "min_size": self.rewards_min_size,
            "mid_yes": mid_yes,
            "real_mid": mid_yes,
        }
        gov_actions = self.governor.evaluate_governor_cycle(
            current_active_markets=[self.condition_id],
            live_bankroll_usd=cash_val,
            scanned_universe=[cand],
            current_time=ts,
        )
        for act in gov_actions:
            if act.action_type == "RESIZE_QUOTE" and act.condition_id == self.condition_id:
                logger.info(
                    f"Governor recommended RESIZE_QUOTE for {self.condition_id[:12]}: "
                    f"New target size={act.target_quote_size:.1f} shares. Compounding into resting quotes."
                )
                self.update_quote_size(act.target_quote_size, timestamp=ts)

        return {
            "epoch_index": self.epoch_index,
            "timestamp": ts,
            "user_score": round(total_user_score, 4),
            "competing_score": round(competing_score, 2),
            "pool_share_pct": round(pool_share_pct, 4),
            "epoch_payout_usd": round(actual_epoch_payout, 6),
            "projected_daily_usd": round(projected_daily_usd, 2),
            "floor_met": floor_met,
            "total_rewards_usd": round(self.total_rewards_credited, 4),
            "quote_size": self.quote_size,
            "governor_actions": [act.action_type for act in gov_actions],
        }

    # -------------------------------------------------------------------------
    # Queue Valuation, Switching Hurdle, and Capital Migration
    # -------------------------------------------------------------------------

    def compute_resting_queue_value(self) -> float:
        """Compute the total economic value ($V_queue) of resting FIFO queue priority across all active quotes."""
        total_v = 0.0
        for q in self.active_quotes.values():
            sz = float(q.get("size", 0.0))
            if sz <= 0.0:
                continue
            q_ahead = float(q.get("queue_ahead", 0.0))
            tok = q.get("token_side", "YES")
            b_tok, a_tok = self.get_best_bid_ask(tok)
            mid = (b_tok + a_tok) / 2.0
            px = float(q.get("price", mid))
            spread_c = abs(px - mid) * 100.0
            val = self.migration_engine.queue_model.evaluate_queue_value(
                quote_size=sz,
                queue_ahead=q_ahead,
                half_spread_cents=max(0.1, spread_c),
                token_mid_price=mid,
            )
            total_v += val.net_queue_value_usd
        return round(total_v, 4)

    def get_current_daily_yield(self) -> float:
        """Calculate current daily reward payout ($/day) based on active quote score or expected yield."""
        user_score = sum(float(q.get("score", 0.0)) for q in self.active_quotes.values())
        if user_score <= 0.0 and self.quote_size > 0.0 and self.rewards_max_spread > 0.0:
            # Expected target quote score for current market
            v = self.rewards_max_spread
            s = self.spread_offset_cents
            if s < v:
                eff_score = ((v - s) / v) ** 2 * self.quote_size
                # Since dual-cash quotes 2 sides (YES and NO)
                user_score = eff_score * 2.0

        comp_score = self.competing_score_override if self.competing_score_override is not None else 0.0
        tot = user_score + comp_score
        share_payout = (user_score / tot) * self.daily_reward_rate if tot > 0 else 0.0
        if self.epoch_index >= 5 and self.total_rewards_credited > 0:
            proj = (self.total_rewards_credited / self.epoch_index) * 1440.0
            return max(share_payout, proj)
        return share_payout

    def compute_switching_cost(
        self,
        daily_yield: Optional[float] = None,
        transit_latency_seconds: float = 4.5,
    ) -> SwitchingCostDecomposition:
        """Compute exact economic switching friction to migrate away from active market."""
        pos = self.get_position(self.condition_id)
        net_q = pos.shares_yes - pos.shares_no

        b_yes, a_yes = self.get_best_bid_ask("YES")
        b_no, a_no = self.get_best_bid_ask("NO")
        mid_yes = (b_yes + a_yes) / 2.0

        bid_quote = self.active_quotes.get("BUY_YES")
        ask_quote = self.active_quotes.get("BUY_NO") or self.active_quotes.get("SELL_YES")

        resting_bid_sz = float(bid_quote.get("size", 0.0)) if bid_quote else 0.0
        q_ahead_bid = float(bid_quote.get("queue_ahead", 0.0)) if bid_quote else 0.0

        resting_ask_sz = float(ask_quote.get("size", 0.0)) if ask_quote else 0.0
        q_ahead_ask = float(ask_quote.get("queue_ahead", 0.0)) if ask_quote else 0.0

        y_a = daily_yield if daily_yield is not None else self.get_current_daily_yield()

        decomp = self.migration_engine.compute_switching_cost(
            q_a=net_q,
            p_mid_a=mid_yes,
            p_bid_a=b_yes,
            p_ask_opp_a=a_no,
            resting_bid_size=resting_bid_sz,
            resting_ask_size=resting_ask_sz,
            queue_ahead_bid=q_ahead_bid,
            queue_ahead_ask=q_ahead_ask,
            half_spread_cents_a=self.spread_offset_cents,
            daily_yield_a=y_a,
            has_matched_complete_sets=(pos.complete_sets > 0),
            transit_latency_seconds=transit_latency_seconds,
        )
        self.latest_switching_cost = decomp
        return decomp

    def evaluate_migration_hurdle(
        self,
        candidate_daily_yield: float,
        holding_horizon_hours: float = 4.0,
        daily_yield_a: Optional[float] = None,
    ) -> Tuple[bool, HurdleDecision, SwitchingCostDecomposition]:
        """Evaluate Net Alpha Hurdle: Delta_Yield * Tau >= Cost_switch."""
        y_a = daily_yield_a if daily_yield_a is not None else self.get_current_daily_yield()
        cost = self.compute_switching_cost(daily_yield=y_a)
        decision = self.migration_engine.evaluate_switching_hurdle(
            y_net_a_daily=y_a,
            y_net_b_daily=candidate_daily_yield,
            switching_cost_usd=cost.total_switching_cost_usd,
            holding_horizon_hours=holding_horizon_hours,
        )
        self.latest_hurdle_decision = decision
        return decision.should_switch, decision, cost

    def flush_wal(self) -> None:
        """Explicitly checkpoint WAL journal to primary SQLite database file."""
        if self._conn:
            try:
                cur = self._conn.cursor()
                cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                self._conn.commit()
            except Exception as e:
                logger.warning(f"Error executing WAL checkpoint: {e}")

    def close(self) -> None:
        """Close SQLite connection and unquote cleanly."""
        try:
            self.cancel_all_quotes()
        except Exception:
            pass
        try:
            self.flush_wal()
        except Exception:
            pass
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
