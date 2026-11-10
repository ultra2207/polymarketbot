"""Composition root of the production real-money trading engine."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, List, Optional, Union

from polymarketbot.execution import SelfTradeGuard
from polymarketbot.fees import FeeSchedule
from polymarketbot.live.base import BaseMarketMakingEngine
from polymarketbot.live.engine.accounting import AccountingMixin
from polymarketbot.live.engine.deskew import DeskewMixin
from polymarketbot.live.engine.feed import FeedMixin
from polymarketbot.live.engine.fills import FillMixin
from polymarketbot.live.engine.ledger import LedgerMixin
from polymarketbot.live.engine.lifecycle import LifecycleMixin
from polymarketbot.live.engine.markout import MarkoutTracker, RealMoneyFillRecord
from polymarketbot.live.engine.merge import MergeMixin
from polymarketbot.live.engine.onchain import OnchainMixin
from polymarketbot.live.engine.orders import OrderSyncMixin
from polymarketbot.live.engine.preflight import PreflightMixin
from polymarketbot.live.engine.quotes import QuoteMixin
from polymarketbot.live.engine.reconcile import ReconcileMixin
from polymarketbot.live.engine.rewards import RewardMixin
from polymarketbot.logs import get_logger
from polymarketbot.risk.manager import RiskManager
from polymarketbot.risk.models import RiskConfig
from polymarketbot.strategy.onchain.ctf import MergeExecutionResult, PolygonCtfClient
from polymarketbot.strategy.onchain.pending import PendingSettlementStore
from polymarketbot.strategy.reward.allocator import ContinuousOptimizationGovernor
from polymarketbot.strategy.reward.deskew import DynamicDeSkewOptimizer
from polymarketbot.strategy.reward.queue import CapitalMigrationEngine
from polymarketbot.strategy.venue.clob import LiveClobClient

logger = get_logger("real_money_engine")


class RealMoneyTradingEngine(
    PreflightMixin,
    ReconcileMixin,
    AccountingMixin,
    QuoteMixin,
    LifecycleMixin,
    FeedMixin,
    FillMixin,
    OrderSyncMixin,
    DeskewMixin,
    MergeMixin,
    OnchainMixin,
    LedgerMixin,
    RewardMixin,
    BaseMarketMakingEngine,
):
    """Production Real-Money Trading Engine for Polymarket CLOB."""

    def __init__(
        self,
        condition_id: str,
        asset_id_yes: str = "TOKEN_YES",
        asset_id_no: str = "TOKEN_NO",
        clob_client: Optional[LiveClobClient] = None,
        ctf_client: Optional[PolygonCtfClient] = None,
        initial_cash: float = 100.0,
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
        db_path: Union[str, Path] = "data/real_money_trading.db",
        ledger_path: Union[str, Path] = "data/real_money_trading_ledger.json",
        deskew_optimizer: Optional[DynamicDeSkewOptimizer] = None,
        migration_engine: Optional[CapitalMigrationEngine] = None,
        governor: Optional[ContinuousOptimizationGovernor] = None,
        risk_manager: Optional[RiskManager] = None,
        dry_run: bool = False,
        total_wallet_balance: Optional[float] = None,
        budget_pct: float = 1.0,
        max_budget: Optional[float] = None,
        tick_size: float = 0.01,
        neg_risk: bool = False,
        fee_rate_bps: int = 0,
        fee_schedule: Optional[FeeSchedule] = None,
        maker_rebate_share_pct: float = 0.20,
        days_to_resolution: float = 7.0,
        book_stale_seconds: float = 30.0,
        merge_cooldown_seconds: float = 60.0,
        event_id: Optional[str] = None,
        router: Optional[Any] = None,
    ) -> None:
        init_cash = float(initial_cash)
        self.tick_size: float = float(tick_size) if float(tick_size) > 0.0 else 0.01
        self.neg_risk: bool = bool(neg_risk)
        self.event_id: Optional[str] = event_id
        self.fee_rate_bps: int = int(fee_rate_bps)
        self.fee_schedule: Optional[FeeSchedule] = fee_schedule
        self.maker_rebate_share_pct: float = float(maker_rebate_share_pct)
        self.days_to_resolution: float = float(days_to_resolution)
        self.book_stale_seconds: float = float(book_stale_seconds)
        self.merge_cooldown_seconds: float = float(merge_cooldown_seconds)

        # Sizing caps are built from the seed bankroll only. In live mode the
        # true equity baseline comes from reconcile_startup(), never from this.
        derived_position_cap = RiskConfig(total_bankroll_usd=init_cash).max_position_cost_per_market
        rm = risk_manager or RiskManager(
            RiskConfig(
                total_bankroll_usd=init_cash,
                price_jump_window_seconds=60.0,
                price_jump_threshold_cents=2.0,
                cooldown_duration_seconds=300.0,
                daily_loss_limit_usd=round(init_cash * 0.05, 2) if init_cash > 0 else 15.0,
                daily_loss_limit_pct=0.05,
                max_drawdown_usd=round(init_cash * 0.10, 2) if init_cash > 0 else 30.0,
                max_drawdown_pct=0.10,
                allow_short=False,
                min_merge_notional_usd=1.0,
                max_quote_size_shares=max(5.0, init_cash),
                max_position_cost_per_market=min(
                    float(derived_position_cap or init_cash), init_cash
                ),
            )
        )
        rm.portfolio_state.bankroll_usd = init_cash
        rm.portfolio_state.day_start_equity_usd = init_cash
        rm.portfolio_state.peak_equity_usd = max(init_cash, rm.portfolio_state.peak_equity_usd)
        rm.portfolio_state.current_equity_usd = init_cash
        rm.portfolio_state.current_cash_usd = init_cash

        super().__init__(
            condition_id=condition_id,
            asset_id_yes=asset_id_yes,
            asset_id_no=asset_id_no,
            quote_size=quote_size,
            spread_offset_cents=spread_offset_cents,
            daily_reward_rate=daily_reward_rate,
            rewards_max_spread_cents=rewards_max_spread_cents,
            rewards_min_size=rewards_min_size,
            auto_merge_threshold=auto_merge_threshold,
            gas_fee_merge=gas_fee_merge,
            institutional_mode=institutional_mode,
            gamma=gamma,
            competing_score_override=competing_score_override,
            db_path=db_path,
            ledger_path=ledger_path,
            deskew_optimizer=deskew_optimizer,
            migration_engine=migration_engine,
            governor=governor,
            risk_manager=rm,
        )
        self.quote_tick_size = self.tick_size

        b_pct = float(budget_pct)
        if b_pct > 1.0:
            b_pct = b_pct / 100.0
        self.budget_pct: float = max(0.01, min(1.0, b_pct))
        self.max_budget: Optional[float] = float(max_budget) if max_budget is not None else None

        self.initial_cash = init_cash
        self.total_wallet_balance: float = (
            float(total_wallet_balance) if total_wallet_balance is not None else init_cash
        )
        self.trading_bankroll: float = init_cash
        self.isolated_cash: float = max(0.0, self.total_wallet_balance - init_cash)
        self.dry_run = bool(dry_run)

        # Clients
        self.clob_client = clob_client or LiveClobClient(dry_run=self.dry_run)
        self.ctf_client = ctf_client or PolygonCtfClient(dry_run=self.dry_run)

        # Wire Self-Trade Prevention (Wash Trading Guard)
        guard = getattr(self.clob_client, "self_trade_guard", None)
        if guard is None:
            try:
                guard = SelfTradeGuard()
                self.clob_client.self_trade_guard = guard
            except (AttributeError, TypeError):
                guard = None
        if guard is not None and hasattr(guard, "link_complementary_tokens"):
            guard.link_complementary_tokens(
                self.asset_id_yes, self.asset_id_no
            )

        # Settlement durability: the signed payload is stored in this engine's
        # database before it is broadcast, so a timed-out settlement survives a
        # restart and is resolved by hash instead of being merged again.
        if hasattr(self.ctf_client, "settlement_store"):
            self.ctf_client.settlement_store = PendingSettlementStore(self._conn)

        # Re-sync dynamic gas fee and merge threshold if not explicitly set
        if gas_fee_merge is None:
            self.gas_fee_merge = self.ctf_client.estimate_merge_cost_usd()
        if auto_merge_threshold is None:
            self.auto_merge_threshold = self.ctf_client.calculate_optimal_merge_threshold(
                gas_cost_usd=self.gas_fee_merge
            )

        # Re-sync dry_run mode from clients
        if self.clob_client.dry_run or self.ctf_client.dry_run:
            self.dry_run = True

        if self.dry_run and hasattr(self.clob_client, "_mock_balance_usd"):
            self.clob_client._mock_balance_usd = self.total_wallet_balance

        # Inter-Domain Dynamic Liquidity Router
        self.router = router
        if self.router is not None and hasattr(self.router, "register_domain1"):
            self.router.register_domain1(self)

        # Wallet & Inventory State
        self.cash: float = self.initial_cash
        self.locked_cash: float = 0.0
        self.initial_equity: float = self.initial_cash
        self.pol_balance: float = 0.50
        self.shares_yes: float = 0.0
        self.shares_no: float = 0.0
        self.avg_cost_yes: float = 0.0
        self.avg_cost_no: float = 0.0
        self._cost_basis_unknown: bool = False
        self.realized_pnl: float = 0.0
        self.total_volume_traded_usd: float = 0.0
        self.fees_paid_usd: float = 0.0
        self.total_gas_fees: float = 0.0
        self.total_merges: int = 0
        self.total_splits: int = 0

        # Cash-vs-locked calibration: None = not yet measured. Dry-run resting
        # orders never reserve simulated cash, so the balance already includes it.
        self._locked_already_in_balance: Optional[bool] = True if self.dry_run else None
        self._book_ts: dict[str, float] = {"YES": 0.0, "NO": 0.0}
        self._last_reward_sync: float = 0.0
        self._last_merge_ts: float = 0.0
        # Conditions with a settlement between claim and apply. The SQLite
        # PENDING row is the durable guard; this set keeps the in-process
        # claim -> receipt window single-flight without holding `self._lock`
        # across network I/O.
        self._settlement_inflight: set[str] = set()
        self._deskew_order: Optional[dict[str, Any]] = None
        self._last_deskew_attempt_ts: float = 0.0
        self._last_risk_block_ts: float = 0.0
        self.deskew_min_interval_s: float = 30.0
        self.trading_enabled: bool = True
        self.market_closed: bool = False
        self.halt_reason: Optional[str] = None
        self._lock = threading.RLock()

        # Ground-truth Polymarket Reward State
        self.actual_rewards_usd: float = 0.0
        self.actual_rebates_usd: float = 0.0
        self.actual_daily_rewards_usd: float = 0.0
        self.reward_source: str = "simulated" if self.dry_run else "uninitialized"
        self.verified_scoring_orders: dict[str, bool] = {}

        # Histories
        self.fills_history: List[RealMoneyFillRecord] = []
        self.merges_history: List[MergeExecutionResult] = []
        self._canceled_order_ids: set[str] = set()

        # Measurement-only helpers (append-only writes; never feed decisions).
        self._markout_tracker = MarkoutTracker(
            conn=self._conn,
            mid_getter=self._fresh_mid,
            lock=self._lock,
        )

        # Auto sync initial balances for live mode or configured credentials
        if not self.dry_run or (self.clob_client.credentials and self.clob_client.credentials.is_configured):
            self.sync_balances()
        if not self.dry_run:
            self.reconcile_startup()
        else:
            self.initial_equity = float(self.cash)

        logger.info(
            f"RealMoneyTradingEngine initialized for condition {self.condition_id[:14]}... | "
            f"Mode: {'DRY-RUN (Simulated)' if self.dry_run else 'LIVE REAL-MONEY'} | "
            f"Free cash: ${self.cash:.2f} | Locked: ${self.locked_cash:.2f} | "
            f"Equity: ${self.get_equity():.2f} | POL: {self.pol_balance:.4f} | "
            f"order_version={self._order_version_safe()} tick={self.tick_size}"
        )
