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

import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.allocator import ContinuousOptimizationGovernor
from polymarketbot.strategy.reward.deskew import DeSkewDecision, DynamicDeSkewOptimizer
from polymarketbot.strategy.reward.microstructure import (
    MicrostructureEngine,
    compute_dynamic_cooldown_duration,
    compute_dynamic_volatility_breaker_threshold,
)
from polymarketbot.strategy.reward.queue import (
    CapitalMigrationEngine,
    HurdleDecision,
    SwitchingCostDecomposition,
)
from polymarketbot.strategy.reward.marginal import PROTOCOL_MIN_DAILY_PAYOUT_USD
from polymarketbot.strategy.reward.scoring import (
    PROTOCOL_EPOCH_SAMPLES,
    normalize_sample_score,
)
from polymarketbot.risk.manager import RiskManager
from polymarketbot.risk.models import RiskConfig

from polymarketbot.live.economics import MARKET_ECONOMICS_PATH, MarketEconomics  # noqa: F401
from polymarketbot.live.quoting import QuotingMixin
from polymarketbot.live.store import StoreMixin
from polymarketbot.live.yield_accounting import YieldMixin

logger = get_logger("trading.base")


# Kept for import compatibility. There is deliberately no fabricated price:
# a missing book is represented by ``None`` and every quoting path must skip
# rather than trade against an invented 0.49/0.51 mark.

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


class BaseMarketMakingEngine(StoreMixin, QuotingMixin, YieldMixin):
    """Unified Base Class for Polymarket Market Making Strategy Orchestration."""

    def __init__(
        self,
        condition_id: str,
        asset_id_yes: str = "TOKEN_YES",
        asset_id_no: str = "TOKEN_NO",
        quote_size: Optional[float] = None,
        spread_offset_cents: Optional[float] = None,
        daily_reward_rate: Optional[float] = None,
        rewards_max_spread_cents: Optional[float] = None,
        rewards_min_size: Optional[float] = None,
        auto_merge_threshold: Optional[float] = None,
        gas_fee_merge: Optional[float] = None,
        institutional_mode: bool = True,
        gamma: Optional[float] = None,
        competing_score_override: Optional[float] = None,
        db_path: Union[str, Path] = "data/trading.db",
        ledger_path: Union[str, Path] = "data/trading_ledger.json",
        deskew_optimizer: Optional[DynamicDeSkewOptimizer] = None,
        migration_engine: Optional[CapitalMigrationEngine] = None,
        governor: Optional[ContinuousOptimizationGovernor] = None,
        risk_manager: Optional[RiskManager] = None,
        price_jump_threshold_cents: Optional[float] = None,
        cooldown_duration_seconds: Optional[float] = None,
        economics: Optional[MarketEconomics] = None,
    ) -> None:
        # Every economic knob comes from measured economics; explicit kwargs are
        # caller overrides and take precedence over the loaded file.
        economics = economics or MarketEconomics.load()
        self.condition_id = condition_id
        self.asset_id_yes = asset_id_yes
        self.asset_id_no = asset_id_no
        self.quote_size = float(
            economics.quote_size if quote_size is None else quote_size
        )
        self.spread_offset_cents = float(
            economics.spread_offset_cents if spread_offset_cents is None else spread_offset_cents
        )
        self.daily_reward_rate = float(
            economics.daily_reward_rate if daily_reward_rate is None else daily_reward_rate
        )
        self.rewards_max_spread = float(
            economics.rewards_max_spread_cents
            if rewards_max_spread_cents is None
            else rewards_max_spread_cents
        )
        self.rewards_min_size = float(
            economics.rewards_min_size if rewards_min_size is None else rewards_min_size
        )
        self.price_jump_threshold_cents = (
            float(price_jump_threshold_cents) if price_jump_threshold_cents is not None else None
        )
        self.cooldown_duration_seconds = (
            float(cooldown_duration_seconds) if cooldown_duration_seconds is not None else None
        )

        if gas_fee_merge is not None:
            self.gas_fee_merge = float(gas_fee_merge)
        else:
            from polymarketbot.strategy.onchain.ctf import GasUnmeasured, estimate_merge_cost_usd

            measured_gas = estimate_merge_cost_usd()
            if measured_gas is None:
                raise GasUnmeasured(
                    "on-chain merge gas is unmeasured; refusing to size quotes and "
                    "reserves against a guessed fee"
                )
            self.gas_fee_merge = float(measured_gas)

        if auto_merge_threshold is not None:
            self.auto_merge_threshold = float(auto_merge_threshold)
        else:
            from polymarketbot.strategy.onchain.ctf import calculate_optimal_merge_threshold
            self.auto_merge_threshold = float(
                calculate_optimal_merge_threshold(gas_cost_usd=self.gas_fee_merge)
            )

        self.institutional_mode = bool(institutional_mode)
        self.gamma = float(economics.gamma if gamma is None else gamma)
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
            q_soft_threshold=economics.q_soft_threshold,
            q_hard_threshold=economics.q_hard_threshold,
            max_holding_seconds=economics.max_holding_seconds,
        )

        # Microstructure Sentinels for OFI & Micro-Price Drift
        self.microstructure_yes = MicrostructureEngine(
            ofi_threshold=economics.ofi_threshold,
            drift_threshold_cents=economics.drift_threshold_cents,
        )
        self.microstructure_no = MicrostructureEngine(
            ofi_threshold=economics.ofi_threshold,
            drift_threshold_cents=economics.drift_threshold_cents,
        )

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
        # Optional in-process hook for the live session recorder. It must never
        # block or raise: the recorder enqueues and returns immediately.
        self.event_hook: Optional[Callable[[str, dict[str, Any]], None]] = None
        self.risk_manager = risk_manager or RiskManager(
            RiskConfig(
                price_jump_window_seconds=60.0,
                price_jump_threshold_cents=2.0,
                cooldown_duration_seconds=300.0,
                daily_loss_limit_usd=economics.daily_loss_limit_usd,
                max_drawdown_usd=economics.max_drawdown_usd,
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

        # Reprice gate: prevents cancel/replace churn that destroys queue priority.
        # Subclasses with a real market tick override quote_tick_size / round_quote_price.
        self.quote_tick_size: float = 0.001
        self.min_reprice_interval_s: float = 2.0
        self.min_reprice_ticks: int = 1
        # Real time-to-resolution for the A-S variance term. The live engine replaces
        # this from the market's end date; the default matches the research model.
        self.days_to_resolution: float = float(economics.days_to_resolution)

        # Epoch & Telemetry State
        self.epoch_index: int = 0
        self.last_epoch_ts: float = time.time()
        self.total_rewards_credited: float = 0.0
        self._epoch_samples: List[float] = []
        self._last_reward_epoch: Dict[str, Any] = {}
        self.latest_deskew_decision: Optional[DeSkewDecision] = None

        # SQLite Connection
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._init_sqlite()

    # -------------------------------------------------------------------------
    # Database Initialization
    # -------------------------------------------------------------------------

    def _emit_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Best-effort hook for the session recorder; a broken hook is ignored."""
        hook = self.event_hook
        if hook is None:
            return
        try:
            hook(kind, payload)
        except Exception:  # noqa: BLE001 - recording must never affect trading
            pass


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

    def get_locked_cash(self) -> float:
        """Collateral committed to resting BUY orders.

        Replacing those orders releases the collateral, so quote sizing uses
        ``get_cash() + get_locked_cash()``. Simulation engines return 0.
        """
        return 0.0

    def get_position(self, condition_id: Optional[str] = None) -> PositionView:
        """Get current position state for target condition ID."""
        raise NotImplementedError

    def get_equity(self, prices: Optional[dict[str, tuple[float, float]]] = None) -> float:
        """Get total portfolio equity (cash + inventory market value)."""
        raise NotImplementedError

    def get_best_bid_ask(
        self, token_side: str
    ) -> Tuple[Optional[float], Optional[float]]:
        """Best real bid/ask for this outcome, or ``(None, None)`` with no book.

        There is no fabricated fallback price: callers must skip quoting when
        either side is ``None`` instead of marking the market at 0.49/0.51.
        """
        tok = token_side.upper()
        bid = self._best_bids.get(tok)
        ask = self._best_asks.get(tok)
        if bid is not None and ask is not None:
            return bid, ask
        # Cross-infer from complementary token if available
        comp = "NO" if tok == "YES" else "YES"
        b_comp = self._best_bids.get(comp)
        a_comp = self._best_asks.get(comp)
        if (
            b_comp is not None
            and a_comp is not None
            and 0.0 < b_comp < 1.0
            and 0.0 < a_comp < 1.0
        ):
            return round(1.0 - a_comp, 4), round(1.0 - b_comp, 4)
        return None, None

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
            # Register a *per-market* cooldown with the risk manager's volatility monitor.
            # Do NOT trip the global circuit breaker here: `is_halted` has no auto-resume,
            # so a single 2c print would permanently stop the engine.
            self.risk_manager.volatility_monitor.cooldowns[self.asset_id_yes] = (
                self._volatility_cooldown_until
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

    def round_quote_price(self, price: float, token_side: str) -> float:
        """Round ``price`` to the market tick and clamp it to the tradeable band.

        The base class defaults to a 0.001 tick. Subclasses that trade a real CLOB
        override both ``quote_tick_size`` and this method with the market's tick.
        """
        tick = self.quote_tick_size if self.quote_tick_size > 0 else 0.001
        min_price = max(tick, 0.001)
        max_price = min(1.0 - tick, 0.999)
        if max_price < min_price:
            min_price = max_price = 0.5
        rounded = round(round(price / tick) * tick, 6)
        return round(min(max(rounded, min_price), max_price), 6)


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
        # Align the epoch to the wall clock so indices are comparable across restarts
        # and do not drift with uptime.
        self.epoch_index = int(ts // 60) % 1440
        self.last_epoch_ts = ts

        # Official Q_min score (never Q1 + Q2): rewards are paid on the minimum of the
        # two complementary book sides, with the documented single-sided penalty.
        total_user_score, _q_one, _q_two = self.compute_quote_scores()

        if self.competing_score_override is not None and self.competing_score_override > 0.0:
            competing_score = max(self.competing_score_override, competing_score)
        else:
            competing_score = max(0.0, competing_score)

        total_pool_score = total_user_score + competing_score
        # The venue's sample normalisation and epoch budget are the reward
        # pillar's (``strategy/reward/scoring.py``): one implementation, called
        # from here rather than re-derived.
        pool_share = normalize_sample_score(total_user_score, total_pool_score)
        pool_share_pct = pool_share * 100.0

        epoch_budget_usd = self.daily_reward_rate / float(PROTOCOL_EPOCH_SAMPLES)
        raw_epoch_payout = pool_share * epoch_budget_usd

        # Rolling 24h of completed per-minute samples. The projection is the
        # current run rate (one minute × one epoch); the payout floor is applied
        # to the *accrued* day instead, because Polymarket pays a maker only when
        # that maker's earnings in this market reach $1 on the day.
        self._epoch_samples.append(raw_epoch_payout)
        if len(self._epoch_samples) > PROTOCOL_EPOCH_SAMPLES:
            del self._epoch_samples[:-PROTOCOL_EPOCH_SAMPLES]
        projected_daily_usd = raw_epoch_payout * float(PROTOCOL_EPOCH_SAMPLES)
        accrued_daily_usd = sum(self._epoch_samples)

        floor_met = accrued_daily_usd >= PROTOCOL_MIN_DAILY_PAYOUT_USD
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
            realized_pnl=pos.realized_pnl,
            unrealized_pnl=equity_val - cash_val,
            current_cash=cash_val,
            timestamp=ts,
        )

        # Continuous Capital Compounding via Governor. Without a book there is no
        # mid to size against, so the governor is skipped rather than fed a mark.
        b_yes, a_yes = self.get_best_bid_ask("YES")
        gov_actions = []
        if b_yes is not None and a_yes is not None:
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

        result = {
            "epoch_index": self.epoch_index,
            "timestamp": ts,
            "user_score": round(total_user_score, 4),
            "competing_score": round(competing_score, 2),
            "pool_share_pct": round(pool_share_pct, 4),
            "epoch_payout_usd": round(actual_epoch_payout, 6),
            "projected_daily_usd": round(projected_daily_usd, 2),
            "accrued_daily_usd": round(accrued_daily_usd, 4),
            "floor_met": floor_met,
            "total_rewards_usd": round(self.total_rewards_credited, 4),
            "quote_size": self.quote_size,
            "governor_actions": [act.action_type for act in gov_actions],
        }
        self._last_reward_epoch = result
        return result


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
        tracker = getattr(self, "_markout_tracker", None)
        if tracker is not None:
            try:
                tracker.stop()
            except Exception:
                pass
            self._markout_tracker = None
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
