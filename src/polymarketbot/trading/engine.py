"""Production Real-Money Polymarket Trading Engine.

Operates symmetrically to NativePaperTradingEngine using BaseMarketMakingEngine,
but sends real signed EIP-712 orders to Polymarket CLOB and executes real CTF
contract merges on Polygon PoS.

Features:
1. Live EIP-712 Order Management via LiveClobClient (Chain ID 137).
2. Live On-Chain CTF Complete-Set Settlement via PolygonCtfClient.
3. Real Wallet & Spender Allowance Auditing (USDC & POL).
4. Full Integration with LivePolymarketFeed for real-time L2 order books & trades.
5. Master RiskManager Circuit Breakers: halts trading if mid-price jumps > 2.0c in 60s,
   daily loss limit ($15/5%), or drawdown limit ($30/10%).
6. Asymmetric Toxicity Quote Fading (Cont-Kukanov-Stoikov OFI & Stoikov micro-price drift).
7. Continuous Capital Compounding via ContinuousOptimizationGovernor & calculate_endogenous_reserve.
8. Opportunistic 300s Market Switching Hurdle via CapitalMigrationEngine.
9. Comprehensive Pre-flight Credential, RPC, and Balance Verification with Safe Dry-Run fallback.
"""

from __future__ import annotations

import heapq
import json
import math
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from polymarketbot.logs import get_logger
from polymarketbot.dataapi import DataAPIClient
from polymarketbot.gamma import GammaClient
from polymarketbot.rewards.allocator import (
    ContinuousOptimizationGovernor,
    calculate_endogenous_reserve,
)
from polymarketbot.rewards.deskew import (
    DeSkewDecision,
    DeSkewPathway,
    DynamicDeSkewOptimizer,
)
from polymarketbot.rewards.microstructure import MicrostructureEngine
from polymarketbot.rewards.queue_valuation import (
    CapitalMigrationEngine,
    HurdleDecision,
    SwitchingCostDecomposition,
)
from polymarketbot.rewards.scoring import quadratic_order_score
from polymarketbot.risk.manager import RiskManager
from polymarketbot.risk.models import RiskConfig
from polymarketbot.trading.base import (
    BaseMarketMakingEngine,
    DEFAULT_FALLBACK_ASK,
    DEFAULT_FALLBACK_BID,
    PositionView,
)
from polymarketbot.execution import PreSignedOrderCache, SelfTradeGuard
from polymarketbot.trading.client import (
    ClobAPIError,
    ClobAuthCredentials,
    LiveClobClient,
    OrderPlacementResult,
)
from polymarketbot.trading.ctf import MergeExecutionResult, PolygonCtfClient
from polymarketbot.trading.settlement import (
    NEGRISK_ADAPTER,
    ROUTER,
    SettlementRoute,
    amount_to_6dp,
    build_negrisk_merge,
    build_router_merge,
    classify_merge_route,
    verify_route,
)
from polymarketbot.trading.pending_settlements import (
    REVERTED as SETTLEMENT_REVERTED,
    SUCCESS as SETTLEMENT_SUCCESS,
    PendingSettlementStore,
)

logger = get_logger("real_money_engine")


@dataclass
class PreflightReport:
    """Diagnostic report assessing trading readiness."""

    ready_for_live: bool
    api_key_configured: bool
    rpc_connected: bool
    wallet_address: str
    usdc_balance: float
    pol_balance: float
    allowances_approved: bool
    dry_run_mode: bool
    recommendations: List[str] = field(default_factory=list)
    free_cash: float = 0.0
    locked_cash: float = 0.0
    order_version: int = 0

    def summary(self) -> str:
        lines = [
            f"Preflight Check: {'READY FOR LIVE' if self.ready_for_live else 'DRY-RUN / ACTION REQUIRED'}",
            f"  - Wallet: {self.wallet_address}",
            f"  - CLOB API Credentials: {'CONFIGURED' if self.api_key_configured else 'UNCONFIGURED (Dry-Run)'}",
            f"  - Polygon RPC: {'CONNECTED' if self.rpc_connected else 'FAILED / DISCONNECTED'}",
            f"  - Free Cash: ${self.free_cash:.2f}",
            f"  - Locked (resting BUY notional): ${self.locked_cash:.2f}",
            f"  - USDC Balance: ${self.usdc_balance:.2f}",
            f"  - POL Gas Balance: {self.pol_balance:.4f} POL",
            f"  - CLOB Order Version: {self.order_version}",
            f"  - CTF Allowances: {'APPROVED' if self.allowances_approved else 'UNAPPROVED / PENDING'}",
        ]
        if self.recommendations:
            lines.append("  Recommendations:")
            for rec in self.recommendations:
                lines.append(f"    * {rec}")
        return "\n".join(lines)


@dataclass
class RealMoneyFillRecord:
    """Record of a trade fill executed on CLOB."""

    fill_id: str
    timestamp: float
    condition_id: str
    token_side: str
    side: str
    fill_price: float
    fill_size: float
    is_maker: bool
    order_id: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Horizon (seconds) -> measured fills column.
MARKOUT_HORIZONS: Tuple[Tuple[float, str], ...] = (
    (30.0, "markout_30s"),
    (300.0, "markout_5m"),
    (1800.0, "markout_30m"),
)


class MarkoutTracker:
    """Daemon worker that fills post-trade markouts once a horizon matures.

    One instance per engine. Each booked fill queues three horizons; when a
    horizon is due the fresh mid is read under the engine lock and only a NULL
    markout column is written. Nothing is backfilled on restart, so a horizon
    that could not be measured at its due time stays NULL forever.

    The thread starts on the first recorded fill and is joined from
    ``RealMoneyTradingEngine.close()``. Tests inject ``clock`` and disable
    ``autostart`` to drive ``process_due`` deterministically.
    """

    def __init__(
        self,
        conn: Any,
        mid_getter: Callable[[str], Optional[float]],
        lock: Optional[threading.RLock] = None,
        clock: Optional[Callable[[], float]] = None,
        autostart: bool = True,
    ) -> None:
        self._conn = conn
        self._mid_getter = mid_getter
        self._lock = lock or threading.RLock()
        self._clock = clock or time.time
        self._autostart = bool(autostart)
        self._heap: List[tuple[float, int, str, str, float, str, str]] = []
        self._seq = 0
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._written = 0
        self._unmeasured = 0

    # -- public API ---------------------------------------------------------
    def record(
        self,
        fill_key: str,
        token_side: str,
        fill_price: float,
        side: str,
        now: Optional[float] = None,
    ) -> None:
        """Queue the three markout horizons for one booked fill."""
        if not fill_key:
            return
        base_ts = self._clock() if now is None else float(now)
        side_up = str(side).upper()
        if side_up not in ("BUY", "SELL") or not (0.0 < float(fill_price) < 1.0):
            return
        with self._cv:
            for horizon, column in MARKOUT_HORIZONS:
                self._seq += 1
                self._heap.append(
                    (
                        base_ts + horizon,
                        self._seq,
                        str(fill_key),
                        str(token_side).upper(),
                        float(fill_price),
                        side_up,
                        column,
                    )
                )
            heapq.heapify(self._heap)
            self._cv.notify_all()
        if self._autostart:
            self.start()

    def start(self) -> None:
        """Start the daemon thread when it is not already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="markout-tracker", daemon=True
        )
        self._thread.start()

    def pending(self) -> int:
        with self._cv:
            return len(self._heap)

    def process_due(self, now: Optional[float] = None) -> int:
        """Write every markout whose horizon has matured (returns rows attempted)."""
        current = self._clock() if now is None else float(now)
        due: List[tuple[float, int, str, str, float, str, str]] = []
        with self._cv:
            while self._heap and self._heap[0][0] <= current:
                due.append(heapq.heappop(self._heap))
        for item in due:
            self._apply(*item[2:])
        return len(due)

    def stop(self, timeout: float = 2.0) -> int:
        """Stop the worker; returns the number of horizons still pending."""
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                with self._cv:
                    leftover = len(self._heap)
                logger.warning(
                    f"MarkoutTracker did not stop within {timeout:.1f}s; "
                    f"{leftover} pending markout(s) left NULL"
                )
        self._thread = None
        with self._cv:
            return len(self._heap)

    # -- internals ----------------------------------------------------------
    def _apply(
        self,
        fill_key: str,
        token_side: str,
        fill_price: float,
        side: str,
        column: str,
    ) -> None:
        if self._conn is None:
            return
        with self._lock:
            if self._conn is None:
                return
            try:
                mid = self._mid_getter(token_side)
            except Exception as e:  # measurement only, never propagate
                logger.debug(f"Markout mid read failed for {fill_key}: {e}")
                mid = None
            if mid is None:
                self._unmeasured += 1
                return
            markout = (float(mid) - fill_price) if side == "BUY" else (fill_price - float(mid))
            try:
                cur = self._conn.cursor()
                cur.execute(
                    f"UPDATE fills SET {column} = ? "
                    f"WHERE fill_key = ? AND {column} IS NULL",
                    (float(markout), fill_key),
                )
                if cur.rowcount and cur.rowcount > 0:
                    self._written += 1
                self._conn.commit()
            except Exception as e:
                logger.warning(f"Markout write failed for {fill_key} {column}: {e}")

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                if not self._heap:
                    self._cv.wait(1.0)
                    continue
                due_ts = self._heap[0][0]
                now = self._clock()
                if due_ts > now:
                    self._cv.wait(min(max(due_ts - now, 0.01), 5.0))
                    continue
            self.process_due()


class RealMoneyTradingEngine(BaseMarketMakingEngine):
    """Production Real-Money Trading Engine for Polymarket CLOB."""

    def __init__(
        self,
        condition_id: str,
        asset_id_yes: str = "TOKEN_YES",
        asset_id_no: str = "TOKEN_NO",
        clob_client: Optional[LiveClobClient] = None,
        ctf_client: Optional[PolygonCtfClient] = None,
        initial_cash: float = 100.0,
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
        days_to_resolution: float = 7.0,
        book_stale_seconds: float = 30.0,
        merge_cooldown_seconds: float = 60.0,
        event_id: Optional[str] = None,
    ) -> None:
        init_cash = float(initial_cash)
        self.tick_size: float = float(tick_size) if float(tick_size) > 0.0 else 0.01
        self.neg_risk: bool = bool(neg_risk)
        self.event_id: Optional[str] = event_id
        self.fee_rate_bps: int = int(fee_rate_bps)
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

    # -------------------------------------------------------------------------
    # Pre-Flight Readiness Validation
    # -------------------------------------------------------------------------

    def preflight_check(self) -> PreflightReport:
        """Inspect credentials, RPC connection, wallet balances, and contract allowances.

        Never mutates ``dry_run``: a preflight failure must surface as a report,
        not silently downgrade a live engine. Never reads the CTF signing key;
        the maker wallet is the only address that belongs in a report.
        """
        recs: list[str] = []
        ready = True

        try:
            api_ok = bool(self.clob_client.credentials.is_configured)
            format_errors = list(self.clob_client.credentials.validate_formatting())
            has_key = bool(self.clob_client.credentials.has_signing_key)
        except Exception as e:
            api_ok = False
            format_errors = [str(e)]
            has_key = False

        addr = str(getattr(self.clob_client, "maker_address", "") or "")

        rpc_ok = False
        rpc_reason = "Polygon RPC connection failed. Check POLYGON_RPC_URL or network connection."
        try:
            health = getattr(self.ctf_client.chain_client, "health_check", None)
            if callable(health):
                rpc_ok, detail = health()
                rpc_reason = f"Polygon RPC health check failed: {detail}"
            else:
                rpc_ok = self.ctf_client.chain_client.block_number() > 0
        except Exception as exc:
            rpc_ok = False
            rpc_reason = f"Polygon RPC health check failed: {exc}"

        free_cash = float(self.cash)
        locked_cash = float(self._compute_locked_cash())
        allowance_error: Optional[str] = None
        approved = False
        balance_error: Optional[str] = None
        try:
            balance = self.clob_client.get_balance()
            self.cash = float(balance["available"])
            self.locked_cash = self._compute_locked_cash()
            free_cash = self.get_cash()
            locked_cash = self.get_locked_cash()
        except ClobAPIError as e:
            balance_error = str(e)
        try:
            approved = bool(self.clob_client.get_allowances().get("approved", False))
        except ClobAPIError as e:
            allowance_error = str(e)

        try:
            order_version = int(self._order_version_safe())
        except ClobAPIError as e:
            order_version = 0
            recs.append(f"CLOB order version could not be resolved: {e}")
        except Exception as e:
            order_version = 0
            recs.append(f"CLOB order version could not be resolved: {e}")

        if not api_ok or not has_key:
            ready = False
            recs.append(
                "CLOB API credentials (POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE, POLY_PRIVATE_KEY) "
                "missing or incomplete. Provide them in .env to place live orders."
            )
        for fe in format_errors:
            ready = False
            recs.append(f"Credential formatting error: {fe}")
        if balance_error is not None:
            ready = False
            recs.append(f"CLOB collateral balance query failed: {balance_error}")
        if allowance_error is not None:
            ready = False
            recs.append(f"CLOB allowance query failed: {allowance_error}")
        if not rpc_ok:
            ready = False
            recs.append(rpc_reason)
        if free_cash <= 0.0:
            ready = False
            recs.append(
                f"CLOB free collateral is $0.00. Fund the maker wallet ({addr}) on Polymarket to begin quoting."
            )
        pol_error_ok = True
        if addr:
            try:
                self.pol_balance = self.ctf_client.get_pol_balance(addr)
            except Exception as e:
                pol_error_ok = False
                recs.append(f"POL gas balance could not be read from RPC: {e}")
        if self.pol_balance < 0.05:
            recs.append(
                f"POL Gas balance is low ({self.pol_balance:.4f} POL). Deposit at least 1-2 POL for CTF merges."
            )
        if not approved:
            ready = False
            recs.append(
                "CTF Exchange allowances have not been approved. Submit approvals before trading real funds."
            )
        if order_version not in (1, 2):
            ready = False
            recs.append(
                f"CLOB order version {order_version or 'unknown'} is not supported; expected 1 or 2."
            )
        if not bool(getattr(self.ctf_client, "is_settlement_enabled", False)):
            recs.append(
                "CTF on-chain settlement is disabled (dry-run, proxy signature type, or missing key): "
                "complete-set merges will be refused; quoting is unaffected."
            )

        return PreflightReport(
            ready_for_live=ready and not self.dry_run,
            api_key_configured=api_ok and has_key and not format_errors,
            rpc_connected=rpc_ok and pol_error_ok,
            wallet_address=addr,
            usdc_balance=free_cash,
            pol_balance=self.pol_balance,
            allowances_approved=approved,
            dry_run_mode=self.dry_run,
            recommendations=recs,
            free_cash=free_cash,
            locked_cash=locked_cash,
            order_version=order_version,
        )

    # -------------------------------------------------------------------------
    # Balance & State Synchronization
    # -------------------------------------------------------------------------

    def _compute_locked_cash(self) -> float:
        """Sum remaining notional of resting BUY orders from local order state."""
        locked = 0.0
        for qdata in self.active_quotes.values():
            if str(qdata.get("side", "BUY")).upper() != "BUY":
                continue
            size = float(qdata.get("size", 0.0) or 0.0)
            matched = float(qdata.get("size_matched", 0.0) or 0.0)
            price = float(qdata.get("price", 0.0) or 0.0)
            remaining = max(0.0, size - matched)
            if remaining > 0.0 and 0.0 < price < 1.0:
                locked += remaining * price
        return locked

    def _reserve_cash_for_buy(self, size: float, price: float) -> None:
        """Reserve collateral for a resting maker BUY when the endpoint reports free cash."""
        if self.dry_run or self._locked_already_in_balance is True:
            return
        self.cash -= float(size) * float(price)

    def _release_reserved_cash(self, qdata: dict[str, Any]) -> None:
        """Return reserved collateral for a resting maker BUY that is confirmed cancelled."""
        if self.dry_run or self._locked_already_in_balance is True:
            return
        if str(qdata.get("side", "")).upper() != "BUY":
            return
        size = float(qdata.get("size", 0.0) or 0.0)
        matched = float(qdata.get("size_matched", 0.0) or 0.0)
        price = float(qdata.get("price", 0.0) or 0.0)
        remaining = max(0.0, size - matched)
        if remaining > 0.0 and 0.0 < price < 1.0:
            self.cash += remaining * price

    def reconcile_startup(self) -> dict[str, Any]:
        """Reconcile orders, inventory and cash with the venue/chain at startup.

        Cancels every order the venue still lists for this market and proves the
        cancellation, reads the real CTF balances (a failure aborts: inventing
        zero inventory would let the engine sell or merge tokens it does not
        own), then sets cash from the CLOB collateral and the true equity
        baseline. Safe to call again after construction.
        """
        with self._lock:
            if self.dry_run:
                self.locked_cash = 0.0
                self.initial_equity = float(self.cash)
                return {
                    "mode": "DRY_RUN",
                    "cash": self.cash,
                    "locked_cash": 0.0,
                    "shares_yes": self.shares_yes,
                    "shares_no": self.shares_no,
                    "initial_equity": self.initial_equity,
                    "canceled_orders": 0,
                }

            # A settlement that timed out locally may still have been mined.
            # Resolve it by hash before trusting balances, so the inventory read
            # below sees the post-merge chain state.
            try:
                self.reconcile_pending_settlements()
            except Exception as exc:  # noqa: BLE001 - replay must not block startup
                logger.warning(f"Pending settlement replay failed: {exc}")

            open_orders = self.clob_client.get_open_orders(market=self.condition_id)
            canceled = 0
            for ord_info in open_orders:
                order_id = self._order_id_of(ord_info)
                if not order_id:
                    continue
                if not self._order_matches_market(ord_info):
                    continue
                self._canceled_order_ids.add(order_id)
                if self.clob_client.cancel_order(order_id):
                    canceled += 1
                else:
                    logger.warning(
                        f"Startup cancel of order {order_id} was not acknowledged; verifying venue state"
                    )
            self.active_quotes.clear()

            still_open = [
                oid
                for oid in (
                    self._order_id_of(o)
                    for o in self.clob_client.get_open_orders(market=self.condition_id)
                )
                if oid
            ]
            if still_open:
                raise RuntimeError(
                    f"Startup reconciliation failed: {len(still_open)} order(s) still open after cancel: "
                    f"{still_open}"
                )

            addr = str(getattr(self.clob_client, "maker_address", "") or "")
            route, _meta = self._resolve_settlement_route()
            if route is SettlementRoute.UNSUPPORTED:
                logger.critical(
                    "Startup reconciliation: settlement route for "
                    f"{self.condition_id[:14]} is unsupported; keeping the persisted "
                    f"local inventory (YES {self.shares_yes:.4f}, NO {self.shares_no:.4f}) "
                    "instead of assuming zero"
                )
            elif route is SettlementRoute.CTF_MERGE:
                self.shares_yes = float(
                    self.ctf_client.get_position_balance(self.asset_id_yes, addr)
                )
                self.shares_no = float(
                    self.ctf_client.get_position_balance(self.asset_id_no, addr)
                )
            elif route is SettlementRoute.ROUTER_MERGE:
                self._reconcile_dataapi_positions(addr)
            else:
                # Neg-risk positions are held in the adapter's ERC-1155, not
                # CTF; neither read above is correct. Keep the persisted local
                # inventory and say so instead of inventing zero.
                logger.warning(
                    "Startup reconciliation: neg-risk on-chain inventory read is "
                    "not implemented; keeping the persisted local balances "
                    f"(YES {self.shares_yes:.4f}, NO {self.shares_no:.4f})"
                )

            # Seed cost basis only from a trusted fact. The Data API position
            # endpoint is not available in this client, so a fresh mid is the
            # only admissible source; otherwise the basis is explicitly unknown.
            if self.shares_yes > 0.0 and self.avg_cost_yes <= 0.0:
                mid_yes = self._fresh_mid("YES")
                if mid_yes is not None:
                    self.avg_cost_yes = mid_yes
            if self.shares_no > 0.0 and self.avg_cost_no <= 0.0:
                mid_no = self._fresh_mid("NO")
                if mid_no is not None:
                    self.avg_cost_no = mid_no
            self._cost_basis_unknown = (
                (self.shares_yes > 0.0 and self.avg_cost_yes <= 0.0)
                or (self.shares_no > 0.0 and self.avg_cost_no <= 0.0)
            )
            if self._cost_basis_unknown:
                logger.critical(
                    "Pre-existing inventory has an unknown cost basis "
                    f"(YES {self.shares_yes:.4f}, NO {self.shares_no:.4f}). "
                    "Realized PnL is suppressed for it until finalize_initial_baseline() "
                    "establishes a basis from fresh books."
                )

            balance = self.clob_client.get_balance()
            self.cash = float(balance["available"])
            self.locked_cash = 0.0
            self.initial_equity = float(
                self.get_cash() + self.get_locked_cash() + self._inventory_value()
            )
            self.total_wallet_balance = self.initial_equity

            state = self.risk_manager.portfolio_state
            state.current_cash_usd = self.cash
            state.current_equity_usd = self.initial_equity
            state.day_start_equity_usd = self.initial_equity
            state.peak_equity_usd = max(state.peak_equity_usd, self.initial_equity)

            logger.info(
                f"Startup reconciliation: canceled {canceled} stale order(s) | "
                f"free cash ${self.cash:.2f} | YES {self.shares_yes:.4f} | NO {self.shares_no:.4f} | "
                f"initial equity ${self.initial_equity:.2f}"
            )
            return {
                "mode": "LIVE",
                "cash": self.cash,
                "locked_cash": 0.0,
                "shares_yes": self.shares_yes,
                "shares_no": self.shares_no,
                "initial_equity": self.initial_equity,
                "canceled_orders": canceled,
            }

    # -------------------------------------------------------------------------
    # Settlement idempotency
    # -------------------------------------------------------------------------

    def _pending_settlement_rows(self) -> Optional[List[dict]]:
        """Unresolved settlement rows for this condition; None when unreadable."""
        if self._conn is None:
            return None
        try:
            return PendingSettlementStore(self._conn).pending_for(self.condition_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable ledger is a refusal
            logger.error(f"Pending settlement lookup failed: {exc}")
            return None

    def reconcile_pending_settlements(self) -> dict[str, int]:
        """Replay every stored settlement hash against the chain. Read-only.

        A settlement that timed out locally may have been mined anyway. Each
        stored hash is looked up by hash, so the transaction is never re-signed
        and no state is mutated here: the row moves to SUCCESS/REVERTED when a
        receipt exists and stays PENDING otherwise. Returns counts for the
        startup log.
        """
        if self._conn is None:
            return {"checked": 0, "resolved": 0, "pending": 0}
        store = PendingSettlementStore(self._conn)
        rows = store.unresolved()
        resolved = 0
        chain = getattr(self.ctf_client, "chain_client", None)
        receipt_lookup = getattr(chain, "get_transaction_receipt", None)

        for row in rows:
            tx_hash = str(row.get("tx_hash") or "")
            if not tx_hash or receipt_lookup is None:
                continue
            try:
                receipt = receipt_lookup(tx_hash)
            except Exception as exc:  # noqa: BLE001 - unknown stays pending
                logger.warning(f"Settlement receipt lookup for {tx_hash} failed: {exc}")
                continue
            if not isinstance(receipt, dict) or not receipt:
                continue
            status = receipt.get("status")
            try:
                mined = status is not None and int(str(status), 16) == 1
            except (TypeError, ValueError):
                mined = False
            store.resolve(tx_hash, SETTLEMENT_SUCCESS if mined else SETTLEMENT_REVERTED, receipt)
            resolved += 1
            logger.info(
                f"Settlement {tx_hash} ({row.get('kind')} {row.get('amount')}) "
                f"reconciled on chain: {'mined' if mined else 'reverted'}"
            )

        if rows:
            logger.info(
                f"Pending settlements: {len(rows)} checked, {resolved} resolved, "
                f"{len(rows) - resolved} still pending"
            )
        return {"checked": len(rows), "resolved": resolved, "pending": len(rows) - resolved}

    def finalize_initial_baseline(self) -> bool:
        """Re-baseline initial equity once both books are fresh.

        Pre-existing inventory with an unknown basis is marked at the fresh mid
        and adopted as its cost basis; initial equity becomes
        ``free cash + effective locked + inventory at fresh mid``. Returns False
        (and logs critical) when inventory is held but a fresh book is missing,
        because inventing a basis would fabricate both equity and realized PnL.
        """
        with self._lock:
            mid_yes = self._fresh_mid("YES")
            mid_no = self._fresh_mid("NO")
            held = self.shares_yes > 1e-12 or self.shares_no > 1e-12
            if not self.dry_run and held and (mid_yes is None or mid_no is None):
                logger.critical(
                    "Cannot finalize the initial equity baseline: inventory is held but "
                    "both books are not fresh; an operator must reconcile the cost basis"
                )
                return False

            if held:
                if self.shares_yes > 1e-12 and mid_yes is not None:
                    if self._cost_basis_unknown and self.avg_cost_yes <= 0.0:
                        self.avg_cost_yes = float(mid_yes)
                if self.shares_no > 1e-12 and mid_no is not None:
                    if self._cost_basis_unknown and self.avg_cost_no <= 0.0:
                        self.avg_cost_no = float(mid_no)
                if (
                    (self.shares_yes <= 0.0 or self.avg_cost_yes > 0.0)
                    and (self.shares_no <= 0.0 or self.avg_cost_no > 0.0)
                ):
                    self._cost_basis_unknown = False

            self.initial_equity = float(
                self.get_cash() + self.get_locked_cash() + self._inventory_value()
            )
            self.total_wallet_balance = self.initial_equity
            state = self.risk_manager.portfolio_state
            state.current_cash_usd = self.cash
            state.current_equity_usd = self.initial_equity
            state.day_start_equity_usd = self.initial_equity
            state.peak_equity_usd = max(state.peak_equity_usd, self.initial_equity)
            logger.info(
                f"Initial equity baseline finalized: ${self.initial_equity:.2f} "
                f"(cash ${self.cash:.2f}, locked ${self.locked_cash:.2f}, "
                f"YES {self.shares_yes:.4f} @ {self.avg_cost_yes:.4f}, "
                f"NO {self.shares_no:.4f} @ {self.avg_cost_no:.4f})"
            )
            return True

    def sync_live_balances(self) -> dict[str, float]:
        """Synchronize CLOB free collateral, locked notional, POL, and token shares.

        ``cash`` is the CLOB available collateral only. ``budget_pct`` and
        ``max_budget`` are sizing caps and never rewrite cash or equity. A
        failed balance call leaves every figure untouched.
        """
        balances: dict[str, float] = {
            "cash": self.cash,
            "locked": self.locked_cash,
            "pol": self.pol_balance,
        }
        if not self.dry_run:
            try:
                bal_data = self.clob_client.get_balance()
                self.cash = float(bal_data["available"])
            except ClobAPIError as e:
                logger.error(
                    f"Failed to read CLOB collateral balance ({e}); cash unchanged at ${self.cash:.2f}"
                )
                self.sync_rewards()
                return balances
            except (KeyError, TypeError, ValueError) as e:
                logger.error(
                    f"CLOB balance response was not a usable collateral figure ({e}); cash unchanged"
                )
                self.sync_rewards()
                return balances

        self.locked_cash = self._compute_locked_cash()
        if not self.dry_run:
            addr = str(getattr(self.clob_client, "maker_address", "") or "")
            if addr:
                try:
                    self.pol_balance = float(self.ctf_client.get_pol_balance(addr))
                except Exception as e:
                    logger.warning(f"Failed to read POL gas balance for {addr}: {e}")

        self.total_wallet_balance = (
            self.get_cash() + self.get_locked_cash() + self._inventory_value()
        )
        balances["cash"] = self.cash
        balances["locked"] = self.locked_cash
        balances["pol"] = self.pol_balance
        self.sync_rewards()
        return balances

    def sync_balances(self) -> None:
        """Alias for sync_live_balances()."""
        self.sync_live_balances()

    def sync_rewards(self, force: bool = False) -> None:
        """Synchronize ground-truth reward telemetry (throttled to once per 60 s)."""
        now = time.time()
        if not force and (now - self._last_reward_sync) < 60.0:
            return
        self._last_reward_sync = now
        try:
            rew_data = self.clob_client.get_total_rewards()
            if rew_data:
                self.actual_rewards_usd = float(rew_data.get("reward_income_usd", 0.0) or 0.0)
                self.actual_rebates_usd = float(rew_data.get("maker_rebates_usd", 0.0) or 0.0)
                self.reward_source = str(rew_data.get("source", self.reward_source))

            daily_data = self.clob_client.get_daily_rewards()
            if daily_data:
                source = str(daily_data.get("source", "") or "")
                if source and source != "unavailable":
                    self.reward_source = source
                self.actual_daily_rewards_usd = float(
                    daily_data.get("daily_combined_usd", 0.0) or 0.0
                )
                # Only a real venue answer is persisted; an unavailable or
                # simulated response writes nothing (never a fabricated zero).
                if source not in ("", "unavailable", "simulated"):
                    self._record_reward_payouts(daily_data, source)

            # Check scoring status for resting quotes
            for qid, qdata in list(self.active_quotes.items()):
                order_id = qdata.get("order_id")
                if order_id:
                    score_res = self.clob_client.get_order_scoring(order_id)
                    is_scoring = bool(score_res.get("scoring", False))
                    qdata["scoring"] = is_scoring
                    self.verified_scoring_orders[order_id] = is_scoring
        except Exception as e:
            logger.debug(f"Failed to sync ground-truth rewards: {e}")

    def _record_reward_payouts(self, daily_data: dict[str, Any], source: str) -> None:
        """Persist venue-reported per-market daily payouts (idempotent)."""
        breakdown = daily_data.get("market_breakdown")
        if not isinstance(breakdown, list):
            return
        date = str(daily_data.get("date") or time.strftime("%Y-%m-%d", time.gmtime()))
        observed_ts = time.time()
        for row in breakdown:
            if not isinstance(row, dict):
                continue
            condition = row.get("condition_id") or row.get("asset_address")
            usd = row.get("earned_usd")
            if usd is None:
                usd = row.get("earnings_usd")
            if not condition or usd is None:
                continue
            try:
                usd_value = float(usd)
            except (TypeError, ValueError):
                continue
            self._insert_reward_payout(
                date=date,
                condition_id=str(condition),
                usd=usd_value,
                source=source,
                observed_ts=observed_ts,
            )

    # -------------------------------------------------------------------------
    # Subclass Hook Implementations
    # -------------------------------------------------------------------------

    def get_cash(self) -> float:
        """Free collateral: reserved BUY notional removed when the endpoint includes it."""
        self.locked_cash = self._compute_locked_cash()
        if self._locked_already_in_balance:
            return max(0.0, self.cash - self.locked_cash)
        return self.cash

    def get_locked_cash(self) -> float:
        """Collateral committed to resting BUY orders (recomputed from local state)."""
        self.locked_cash = self._compute_locked_cash()
        return self.locked_cash

    def get_position(self, condition_id: Optional[str] = None) -> PositionView:
        cid = condition_id or self.condition_id
        return PositionView(
            condition_id=cid,
            shares_yes=self.shares_yes,
            shares_no=self.shares_no,
            avg_cost_yes=self.avg_cost_yes,
            avg_cost_no=self.avg_cost_no,
            realized_pnl=self.realized_pnl,
        )

    def has_fresh_book(self, token_side: str, max_age: Optional[float] = None) -> bool:
        """True when the engine has a recent L2 snapshot for this outcome token."""
        tok = str(token_side).upper()
        ts = float(self._book_ts.get(tok, 0.0) or 0.0)
        if ts <= 0.0:
            return False
        age = self.book_stale_seconds if max_age is None else float(max_age)
        return (time.time() - ts) <= age

    def _fresh_mid(self, token_side: str) -> Optional[float]:
        """Fresh book mid, or None when the book is missing/stale/invalid."""
        tok = str(token_side).upper()
        if not self.has_fresh_book(tok):
            return None
        if tok not in self._best_bids or tok not in self._best_asks:
            return None
        bid, ask = self.get_best_bid_ask(tok)
        if not (0.0 < bid < 1.0 and 0.0 < ask < 1.0):
            return None
        mid = (bid + ask) / 2.0
        return mid if 0.0 < mid < 1.0 else None

    def _mark_prices(self) -> Tuple[float, float]:
        """Mark YES/NO for equity using fresh mids, else average cost.

        Never falls back to a fabricated 0.50. Crossed or complementary books
        (|mid_yes + mid_no - 1| > 0.02) are normalised so equity cannot be
        inflated by quoting both sides of an inconsistent snapshot.
        """
        mid_yes = self._fresh_mid("YES")
        mid_no = self._fresh_mid("NO")
        if mid_yes is not None and mid_no is not None:
            if abs(mid_yes + mid_no - 1.0) > 0.02:
                mid_no = 1.0 - mid_yes
        elif mid_yes is not None:
            mid_no = 1.0 - mid_yes
        elif mid_no is not None:
            mid_yes = 1.0 - mid_no
        if mid_yes is None:
            mid_yes = self.avg_cost_yes
        if mid_no is None:
            mid_no = self.avg_cost_no
        return mid_yes, mid_no

    def _inventory_value(self) -> float:
        mark_yes, mark_no = self._mark_prices()
        return self.shares_yes * mark_yes + self.shares_no * mark_no

    def get_equity(self, prices: Optional[dict[str, tuple[float, float]]] = None) -> float:
        """Free cash + locked collateral + inventory at fresh mid (else avg cost)."""
        return self.get_cash() + self.get_locked_cash() + self._inventory_value()

    def get_best_bid_ask(self, token_side: str) -> Tuple[float, float]:
        tok = token_side.upper()
        if tok in self._best_bids and tok in self._best_asks:
            return self._best_bids[tok], self._best_asks[tok]
        # Cross-infer from complementary token if available
        comp = "NO" if tok == "YES" else "YES"
        if comp in self._best_bids and comp in self._best_asks:
            b_comp = self._best_bids[comp]
            a_comp = self._best_asks[comp]
            if 0.0 < b_comp < 1.0 and 0.0 < a_comp < 1.0:
                return round(1.0 - a_comp, 4), round(1.0 - b_comp, 4)
        return (
            self._best_bids.get(tok, DEFAULT_FALLBACK_BID),
            self._best_asks.get(tok, DEFAULT_FALLBACK_ASK),
        )

    def credit_reward(self, amount: float, condition_id: str, note: str = "") -> None:
        """Credit modelled rewards to simulated cash only.

        In live mode the modelled epoch payout is telemetry: crediting it would
        mask trading losses in the daily-loss and drawdown breakers.
        """
        amt = float(amount)
        if self.dry_run:
            self.cash += amt
            return
        logger.info(
            f"[REWARD TELEMETRY] Modelled reward ${amt:.6f} for {condition_id[:12]} "
            f"recorded only, not credited to live cash ({note})"
        )

    def _set_quote(
        self,
        token_side: str,
        quote_side: str,
        price: float,
        size: float,
        ts: float,
    ) -> None:
        """Place or update a live signed maker limit order on Polymarket CLOB."""
        tok = str(token_side).upper()
        side = str(quote_side).upper()
        qid = f"{side}_{tok}"

        if not self.trading_enabled or self.market_closed:
            logger.debug(f"Quote {qid} suppressed: trading_enabled={self.trading_enabled} closed={self.market_closed}")
            return
        if not self.has_fresh_book(tok):
            logger.debug(f"Quote {qid} suppressed: no fresh {tok} book")
            return

        requested_price = float(price)
        requested_size = float(size)
        if requested_size <= 0.0 or not (0.0 < requested_price < 1.0):
            logger.warning(f"Quote {qid} rejected before placement: price={requested_price} size={requested_size}")
            return
        price = self.round_quote_price(requested_price, tok)
        token_id = self.asset_id_yes if tok == "YES" else self.asset_id_no

        # Master risk check: position, notional, cash, inventory and toxicity caps.
        token_pos = self.shares_yes if tok == "YES" else self.shares_no
        token_avg = self.avg_cost_yes if tok == "YES" else self.avg_cost_no
        token_mid = self._fresh_mid(tok)
        allowed, reason, approved_size, approved_price = self.risk_manager.validate_quote(
            asset_id=token_id,
            condition_id=self.condition_id,
            side=side,
            price=price,
            size=requested_size,
            current_pos_shares=token_pos,
            current_pos_cost=abs(token_pos * token_avg),
            current_portfolio_inventory_usd=self._inventory_value(),
            current_cash_usd=self.get_cash(),
            mid_price=token_mid if token_mid is not None else price,
            tick_size=self.tick_size,
            timestamp=ts,
            event_id=self.event_id,
            current_event_inventory_usd=self._inventory_value(),
            current_event_cost=abs(token_pos * token_avg),
        )
        if not allowed:
            if ts - self._last_risk_block_ts >= 30.0:
                self._last_risk_block_ts = ts
                logger.warning(f"Risk rejected quote {qid}: {reason}")
            else:
                logger.debug(f"Risk rejected quote {qid}: {reason}")
            self._emit_event(
                "risk_blocked",
                {
                    "asset_id": token_id,
                    "side": side,
                    "price": price,
                    "requested_size": requested_size,
                    "reason": reason,
                },
            )
            self._cancel_quote(qid)
            return
        price = self.round_quote_price(approved_price, tok)
        requested_size = float(approved_size)
        if requested_size + 1e-9 < self.rewards_min_size:
            logger.warning(
                f"Quote {qid} suppressed: risk-approved size {requested_size:.2f} is below "
                f"rewards_min_size={self.rewards_min_size:.2f}"
            )
            self._cancel_quote(qid)
            return

        existing = self.active_quotes.get(qid)
        if existing and (
            abs(float(existing.get("price", 0.0)) - price) < 1e-9
            and abs(float(existing.get("size", 0.0)) - requested_size) < 1e-6
        ):
            # Price and size unchanged: retain active CLOB order without queue penalty
            return

        pre_available: Optional[float] = None
        if (
            not self.dry_run
            and side == "BUY"
            and self._locked_already_in_balance is None
        ):
            try:
                pre_available = float(self.clob_client.get_balance()["available"])
            except (ClobAPIError, KeyError, TypeError, ValueError) as e:
                logger.warning(f"Could not read pre-placement collateral for locked calibration: {e}")

        # Cancel previous resting order if modifying price or size. Never place a
        # replacement while the old order's cancel is unconfirmed.
        if existing and existing.get("order_id"):
            old_oid = str(existing["order_id"])
            self._canceled_order_ids.add(old_oid)
            if not self.clob_client.cancel_order(old_oid):
                existing["status"] = "CANCELLING"
                logger.warning(
                    f"Cancel of {qid} order {old_oid} not confirmed; refusing to stack a replacement"
                )
                return
            self._release_reserved_cash(existing)
            self.active_quotes.pop(qid, None)

        place_started = time.perf_counter()
        res = self.clob_client.place_order(
            token_id=token_id,
            side=side,
            price=price,
            size=requested_size,
            tick_size=self.tick_size,
            neg_risk=self.neg_risk,
            fee_rate_bps=self.fee_rate_bps,
            post_only=True,
        )
        place_rtt_ms = (time.perf_counter() - place_started) * 1000.0

        if not res.success:
            logger.warning(f"Failed to place live CLOB quote {qid}: {res.error_message}")
            self.active_quotes.pop(qid, None)
            return

        self._reserve_cash_for_buy(requested_size, price)

        if pre_available is not None and not self.dry_run:
            try:
                post_available = float(self.clob_client.get_balance()["available"])
            except (ClobAPIError, KeyError, TypeError, ValueError) as e:
                logger.warning(f"Locked-cash calibration read failed after placement: {e}")
            else:
                expected_reserve = requested_size * price
                decreased = (pre_available - post_available) > max(1e-6, expected_reserve * 0.5)
                self._locked_already_in_balance = not decreased
                if self._locked_already_in_balance:
                    # The endpoint includes the reserve, so undo the local
                    # reservation made while the mode was still unknown.
                    self.cash += expected_reserve
                logger.info(
                    f"Locked-cash calibration: available {pre_available:.4f} -> {post_available:.4f} "
                    f"after BUY {requested_size:.2f}@{price:.4f}; balance "
                    f"{'INCLUDES' if self._locked_already_in_balance else 'EXCLUDES'} locked collateral"
                )

        # Score quote for rewards
        b_tok, a_tok = self.get_best_bid_ask(tok)
        mid_tok = (b_tok + a_tok) / 2.0
        spread_cents = abs(price - mid_tok) * 100.0
        score = quadratic_order_score(
            max_spread_cents=self.rewards_max_spread,
            spread_cents=spread_cents,
            size=requested_size,
            min_size=self.rewards_min_size,
        )

        vq_res = self.migration_engine.queue_model.evaluate_queue_value(
            quote_size=requested_size,
            queue_ahead=0.0,
            half_spread_cents=max(0.1, spread_cents),
            token_mid_price=mid_tok,
        )

        self.active_quotes[qid] = {
            "quote_id": qid,
            "order_id": res.order_id,
            "market": self.condition_id,
            "token_side": tok,
            "side": side,
            "price": price,
            "size": requested_size,
            "size_matched": 0.0,
            "queue_ahead": 0.0,
            "score": round(score, 4),
            "v_queue": round(vq_res.net_queue_value_usd, 4),
            "timestamp": ts,
            "status": "LIVE",
            "is_dry_run": res.is_dry_run,
            "is_taker": False,
        }
        self.locked_cash = self._compute_locked_cash()
        self._emit_event(
            "quote_placed",
            {
                "quote_id": qid,
                "order_id": res.order_id,
                "asset_id": token_id,
                "side": side,
                "price": price,
                "size": requested_size,
                "dry_run": bool(res.is_dry_run),
                "place_rtt_ms": round(place_rtt_ms, 3),
            },
        )

        # Persist quote to SQLite
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO active_quotes (
                timestamp, quote_id, condition_id, token_side, side, price, size, queue_ahead, score, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                qid,
                self.condition_id,
                tok,
                side,
                price,
                requested_size,
                0.0,
                score,
                "LIVE",
            ),
        )
        self._conn.commit()

    def _cancel_quote(self, qid: str) -> None:
        """Cancel one resting quote, removing it from local state only on confirmation."""
        quote_data = self.active_quotes.get(qid)
        if not quote_data:
            return
        order_id = str(quote_data.get("order_id", "") or "")
        if not order_id:
            self.active_quotes.pop(qid, None)
            return
        self._canceled_order_ids.add(order_id)
        cancel_started = time.perf_counter()
        cancelled = self.clob_client.cancel_order(order_id)
        cancel_rtt_ms = (time.perf_counter() - cancel_started) * 1000.0
        if cancelled:
            self._release_reserved_cash(quote_data)
            self.active_quotes.pop(qid, None)
            self.locked_cash = self._compute_locked_cash()
            self._emit_event(
                "quote_cancelled",
                {
                    "quote_id": qid,
                    "order_id": order_id,
                    "asset_id": quote_data.get("token_side"),
                    "cancel_rtt_ms": round(cancel_rtt_ms, 3),
                },
            )
        else:
            quote_data["status"] = "CANCELLING"
            logger.warning(f"Cancel of order {order_id} ({qid}) was not confirmed; keeping local tracking")

    def cancel_all_quotes(self) -> bool:
        """Cancel every resting order and verify the venue reports none left.

        Orders are removed from local state only after the venue verification
        confirms they are gone. Failed or unverified cancels stay tracked with
        ``status='CANCELLING'``. Returns True only when the venue reports no
        order left for this market.
        """
        with self._lock:
            for qid, qdata in list(self.active_quotes.items()):
                order_id = str(qdata.get("order_id", "") or "")
                if not order_id:
                    self.active_quotes.pop(qid, None)
                    continue
                self._canceled_order_ids.add(order_id)
                if not self.clob_client.cancel_order(order_id):
                    qdata["status"] = "CANCELLING"

            try:
                remaining = self.clob_client.get_open_orders(market=self.condition_id)
            except ClobAPIError as e:
                logger.error(
                    f"Could not verify cancellations with the venue ({e}); keeping local quote state"
                )
                for qdata in self.active_quotes.values():
                    if qdata.get("order_id"):
                        qdata["status"] = "CANCELLING"
                self.locked_cash = self._compute_locked_cash()
                return False

            remaining_ids = {
                oid
                for oid in (self._order_id_of(o) for o in remaining)
                if oid
            }
            for qid, qdata in list(self.active_quotes.items()):
                order_id = str(qdata.get("order_id", "") or "")
                if order_id and order_id in remaining_ids:
                    qdata["status"] = "CANCELLING"
                else:
                    self._release_reserved_cash(qdata)
                    self.active_quotes.pop(qid, None)
            self.locked_cash = self._compute_locked_cash()
            confirmed = not remaining_ids
            if confirmed:
                logger.info("All resting quotes confirmed cancelled by the venue")
            else:
                logger.warning(
                    f"Venue still lists {len(remaining_ids)} order(s) after cancel-all: {sorted(remaining_ids)}"
                )
            return confirmed

    def _taker_fee(self, size: float, price: float) -> float:
        """Exchange taker fee for a fill, from the market's fee_rate_bps."""
        if self.fee_rate_bps <= 0 or size <= 0.0 or price <= 0.0:
            return 0.0
        return float(size) * float(price) * (self.fee_rate_bps / 10_000.0)

    def _marketable_price(self, token_side: str, side: str) -> Optional[float]:
        bid, ask = self.get_best_bid_ask(token_side)
        price = ask if str(side).upper() == "BUY" else bid
        if not (0.0 < price < 1.0):
            return None
        return price

    def _execute_tier2_taker_deskew(self, decision: DeSkewDecision, ts: float) -> None:
        """Execute Tier 2 emergency cross-spread taker execution on CLOB.

        Placement is not a fill: the order is registered as a taker quote and
        booked only by reconciliation against confirmed exchange state.
        """
        target_token = str(decision.target_token).upper()
        target_side = str(decision.action_side).upper()
        if target_token not in ("YES", "NO") or target_side not in ("BUY", "SELL"):
            return

        if self.dry_run:
            self._execute_tier2_dry_run(decision, ts)
            return

        if not self.trading_enabled or self.market_closed:
            return
        if not self.has_fresh_book(target_token):
            logger.warning(
                f"Tier 2 de-skew skipped for {self.condition_id[:12]}: {target_token} book is stale"
            )
            return

        # Never stack taker orders: resolve the in-flight one first.
        if self._deskew_order is not None:
            age = ts - float(self._deskew_order.get("timestamp", 0.0) or 0.0)
            if age < self.deskew_min_interval_s:
                return
            qid = str(self._deskew_order.get("quote_id", "") or "")
            tracked = self.active_quotes.get(qid)
            if tracked is None or str(tracked.get("order_id", "")) != str(
                self._deskew_order.get("order_id", "")
            ):
                self._deskew_order = None
            else:
                self.sync_live_orders_and_fills(timestamp=ts)
                qid = str(self._deskew_order.get("quote_id", "") if self._deskew_order else "")
                tracked = self.active_quotes.get(qid) if qid else None
                if tracked is None or str(tracked.get("order_id", "")) != str(
                    self._deskew_order.get("order_id", "") if self._deskew_order else ""
                ):
                    self._deskew_order = None
                else:
                    marketable = self._marketable_price(target_token, target_side)
                    moved = (
                        marketable is not None
                        and abs(marketable - float(self._deskew_order.get("price", 0.0) or 0.0))
                        >= self.tick_size - 1e-9
                    )
                    if moved:
                        self._cancel_quote(qid)
                    if self._deskew_order is not None and qid not in self.active_quotes:
                        self._deskew_order = None
                    if self._deskew_order is not None:
                        return

        if ts - self._last_deskew_attempt_ts < self.deskew_min_interval_s:
            return

        marketable = self._marketable_price(target_token, target_side)
        if marketable is None:
            logger.warning(f"Tier 2 de-skew skipped: no valid {target_side} price for {target_token}")
            return
        target_price = self.round_quote_price(marketable, target_token)

        target_size = min(float(decision.target_size), self.quote_size * 2.0)
        max_quote_shares = float(
            getattr(self.risk_manager.config, "max_quote_size_shares", 0.0) or 0.0
        )
        if max_quote_shares > 0.0:
            target_size = min(target_size, max_quote_shares)
        if target_side == "SELL" and not bool(
            getattr(self.risk_manager.config, "allow_short", False)
        ):
            owned = self.shares_yes if target_token == "YES" else self.shares_no
            target_size = min(target_size, max(0.0, owned))
        if target_size <= 0.0:
            logger.warning(
                f"Tier 2 de-skew skipped: size after caps is {target_size:.4f} for {target_token}"
            )
            return

        self._record_deskew_event(decision, ts)
        taker_fee = self._taker_fee(target_size, target_price)
        logger.warning(
            f"REAL TRADER: EXECUTING TIER 2 EMERGENCY DE-SKEW ({decision.pathway.value}): "
            f"{target_side} {target_size:.2f} {target_token} @ ${target_price:.3f} | "
            f"optimizer savings {decision.synthetic_savings_cents:.3f}c | "
            f"market taker fee ${taker_fee:.4f} ({self.fee_rate_bps}bps) | {decision.reason}"
        )

        token_id = self.asset_id_yes if target_token == "YES" else self.asset_id_no
        res = self.clob_client.place_order(
            token_id=token_id,
            side=target_side,
            price=target_price,
            size=target_size,
            tick_size=self.tick_size,
            neg_risk=self.neg_risk,
            fee_rate_bps=self.fee_rate_bps,
            post_only=False,
        )
        self._last_deskew_attempt_ts = ts
        if not res.success:
            logger.warning(f"Tier 2 taker order rejected: {res.error_message}")
            self._deskew_order = None
            return

        self._reserve_cash_for_buy(target_size, target_price)

        qid = f"DESKEW_{target_token}_{int(ts * 1000)}"
        self.active_quotes[qid] = {
            "quote_id": qid,
            "order_id": res.order_id,
            "market": self.condition_id,
            "token_side": target_token,
            "side": target_side,
            "price": target_price,
            "size": target_size,
            "size_matched": 0.0,
            "queue_ahead": 0.0,
            "score": 0.0,
            "v_queue": 0.0,
            "timestamp": ts,
            "status": "LIVE",
            "is_dry_run": res.is_dry_run,
            "is_taker": True,
            "decision_target_price": float(decision.target_price),
        }
        self._deskew_order = {
            "quote_id": qid,
            "order_id": res.order_id,
            "timestamp": ts,
            "price": target_price,
            "size": target_size,
            "token_side": target_token,
            "side": target_side,
        }
        self.locked_cash = self._compute_locked_cash()

    def _execute_tier2_dry_run(self, decision: DeSkewDecision, ts: float) -> None:
        """Simulated tier-2 cross for dry-run only: books the modelled fill."""
        target_token = str(decision.target_token).upper()
        target_side = str(decision.action_side).upper()
        target_size = min(float(decision.target_size), self.quote_size * 2.0)
        target_price = self.round_quote_price(float(decision.target_price), target_token)
        if target_size <= 0.0 or not (0.0 < target_price < 1.0):
            return
        self._record_deskew_event(decision, ts)
        logger.warning(
            f"[DRY-RUN] TIER 2 EMERGENCY DE-SKEW ({decision.pathway.value}): "
            f"{target_side} {target_size:.2f} {target_token} @ ${target_price:.3f} | {decision.reason}"
        )
        fill_rec = RealMoneyFillRecord(
            fill_id=str(uuid.uuid4())[:8],
            timestamp=ts,
            condition_id=self.condition_id,
            token_side=target_token,
            side=target_side,
            fill_price=target_price,
            fill_size=target_size,
            is_maker=False,
            order_id="",
            notes=f"Simulated Tier 2 Taker Fill ({decision.pathway.value}, Dry-Run)",
        )
        self.fills_history.append(fill_rec)
        shares_attr = "shares_yes" if target_token == "YES" else "shares_no"
        shares_before = float(getattr(self, shares_attr, 0.0) or 0.0)
        realized_before = float(self.realized_pnl)
        taker_fee = self._taker_fee(target_size, target_price)
        self._apply_fill(
            target_token,
            target_side,
            target_size,
            target_price,
            fee_usd=taker_fee,
            ts=ts,
            is_taker=True,
        )
        fill_key = f"{fill_rec.fill_id}:{float(ts):.3f}:{target_price:.6f}:{target_size:.6f}"
        self._record_fill_observation(
            fill_key=fill_key,
            ts=ts,
            tok=target_token,
            side=target_side,
            price=target_price,
            size=target_size,
            is_maker=False,
            order_id="",
            fee_usd=taker_fee,
            realized_pnl=float(self.realized_pnl) - realized_before,
            note=fill_rec.notes,
        )
        self._insert_rebalance_row(
            pathway=f"TIER2_{decision.pathway.value}",
            ts=ts,
            crossed_cents=abs(target_price - float(decision.target_price)) * 100.0,
            taker_fee_usd=float(taker_fee),
            gas_usd=0.0,
            inventory_before=shares_before,
            inventory_after=float(getattr(self, shares_attr, 0.0) or 0.0),
            note="simulated tier2 cross (dry-run)",
        )
        if decision.pathway == DeSkewPathway.SYNTHETIC_MERGE:
            self.trigger_ctf_merge()

    def update_dynamic_gas_and_threshold(self) -> Tuple[float, float]:
        """Update dynamic gas fee and optimal merge threshold from live on-chain conditions."""
        gas_cost = self.ctf_client.estimate_merge_cost_usd()
        threshold = self.ctf_client.calculate_optimal_merge_threshold(gas_cost_usd=gas_cost)
        self.gas_fee_merge = gas_cost
        self.auto_merge_threshold = threshold
        return gas_cost, threshold

    def _reconcile_dataapi_positions(self, addr: str) -> None:
        """Set inventory and cost basis from the Data API position ledger.

        This is the v2 PositionManager read: ``CTF.balanceOf`` does not exist on
        that ledger. A failed call raises, because silently zeroing a funded
        position is worse than refusing to start.
        """
        if not addr:
            raise RuntimeError("Data API reconciliation needs a maker address")
        rows = DataAPIClient().positions(addr, max_pages=5)
        condition = self.condition_id.lower()
        shares_yes = shares_no = 0.0
        cost_yes = cost_no = 0.0
        for row in rows:
            condition_id = str(
                row.get("conditionId") or row.get("condition_id") or ""
            ).lower()
            if condition_id != condition:
                continue
            try:
                size = float(row.get("size") or 0.0)
                avg_price = float(row.get("avgPrice") or row.get("avg_price") or 0.0)
            except (TypeError, ValueError):
                continue
            if size <= 0.0:
                continue
            outcome = row.get("outcomeIndex")
            asset = str(row.get("asset") or row.get("tokenId") or "")
            if outcome is not None:
                is_yes = int(outcome) == 0
            else:
                is_yes = asset.lower() == str(self.asset_id_yes).lower()
            if is_yes:
                shares_yes += size
                cost_yes += size * avg_price
            else:
                shares_no += size
                cost_no += size * avg_price

        self.shares_yes = shares_yes
        self.shares_no = shares_no
        if shares_yes > 0.0 and cost_yes > 0.0:
            self.avg_cost_yes = cost_yes / shares_yes
        if shares_no > 0.0 and cost_no > 0.0:
            self.avg_cost_no = cost_no / shares_no
        logger.info(
            f"Data API reconciliation: YES={shares_yes:.4f} NO={shares_no:.4f} "
            f"(avg cost {self.avg_cost_yes:.4f}/{self.avg_cost_no:.4f})"
        )

    def _resolve_settlement_route(self) -> tuple[SettlementRoute, Optional[dict]]:
        """Classify the market's settlement ledger once, from Gamma metadata.

        A lookup failure is *not* cached: a transient Gamma outage must not
        permanently pin a live engine to "unsupported". Only a supported route
        is cached.
        """
        cached = getattr(self, "_settlement_route_cache", None)
        if cached is not None:
            return cached
        condition = str(self.condition_id or "")
        if not (
            len(condition) == 66
            and condition.startswith("0x")
            and all(ch in "0123456789abcdefABCDEF" for ch in condition[2:])
        ):
            # A malformed condition id cannot exist on-chain (and is what the
            # unit tests use). Keep the legacy CTF path without a network call.
            return SettlementRoute.CTF_MERGE, None
        try:
            meta = GammaClient().market_meta(self.condition_id)
        except Exception as exc:  # noqa: BLE001 - the balance preflight is the guard
            logger.warning(
                f"Settlement route lookup failed ({exc}); defaulting to v1 CTF, "
                "which the on-chain balance preflight will refuse if wrong"
            )
            return SettlementRoute.CTF_MERGE, None
        if meta is None:
            # Gamma could not confirm the ledger (unknown condition or a
            # transient outage). Fall back to the legacy CTF path: its on-chain
            # balance preflight refuses when the positions actually live on the
            # Router or NegRisk ledger, so this cannot merge the wrong ledger.
            logger.warning(
                f"Gamma returned no metadata for {self.condition_id[:14]}; "
                "settling as v1 CTF (balance preflight still applies)"
            )
            return SettlementRoute.CTF_MERGE, None
        route = classify_merge_route(meta)
        if route is not SettlementRoute.UNSUPPORTED:
            self._settlement_route_cache = (route, meta)
        return route, meta

    def trigger_ctf_merge(self, amount_sets: Optional[float] = None) -> Optional[MergeExecutionResult]:
        """Merge complete sets only when real on-chain settlement is possible.

        The inventory and cash mutation is driven exclusively by a confirmed
        merge receipt plus its actual gas cost. Every refusal path leaves state
        untouched.
        """
        with self._lock:
            if not self.dry_run and not bool(
                getattr(self.ctf_client, "is_settlement_enabled", False)
            ):
                logger.error(
                    "CTF merge refused: live on-chain settlement is disabled for this client"
                )
                return None

            # One settlement in flight per condition. A PENDING row means a
            # signed transaction exists whose outcome is unknown; merging again
            # would spend the same complete sets twice.
            pending_rows = self._pending_settlement_rows()
            if pending_rows is None:
                logger.error(
                    "CTF merge refused: cannot prove that no settlement is already pending"
                )
                return None
            if pending_rows:
                oldest = pending_rows[0]
                logger.error(
                    f"CTF merge refused: settlement tx {oldest.get('tx_hash')} "
                    f"({oldest.get('kind')} {oldest.get('amount')}) for {self.condition_id[:14]} "
                    f"is still PENDING; resolve it by hash before merging again"
                )
                return None

            pos = self.get_position(self.condition_id)
            available_sets = min(pos.shares_yes, pos.shares_no)
            if available_sets <= 0.0:
                logger.debug(
                    f"CTF merge skipped: no complete sets held "
                    f"(YES: {pos.shares_yes:.2f}, NO: {pos.shares_no:.2f})"
                )
                return None

            if amount_sets is not None:
                sets_to_merge = min(float(amount_sets), available_sets)
                if float(amount_sets) > available_sets:
                    logger.warning(
                        f"Requested CTF merge amount ({amount_sets:.2f}) exceeds available complete sets "
                        f"({available_sets:.2f}). Clamping to {available_sets:.2f}."
                    )
            else:
                sets_to_merge = available_sets

            cfg = self.risk_manager.config
            risk_min_notional = float(getattr(cfg, "min_merge_notional_usd", 0.0) or 0.0)
            min_sets = max(float(self.auto_merge_threshold), risk_min_notional)
            if sets_to_merge < min_sets:
                logger.debug(
                    f"CTF merge skipped: {sets_to_merge:.2f} sets below min threshold {min_sets:.2f}"
                )
                return None

            now = time.time()
            if (now - self._last_merge_ts) < self.merge_cooldown_seconds:
                logger.debug(
                    f"CTF merge skipped: cooldown {self.merge_cooldown_seconds:.0f}s not elapsed"
                )
                return None

            addr = str(getattr(self.clob_client, "maker_address", "") or "")
            route = SettlementRoute.CTF_MERGE
            meta: Optional[dict] = None
            if not self.dry_run:
                if not addr:
                    logger.error(
                        "Settlement aborted: maker address is unknown, cannot verify the ledger"
                    )
                    return None
                route, meta = self._resolve_settlement_route()
                if route is SettlementRoute.UNSUPPORTED:
                    logger.error(
                        "Settlement refused: market has no supported merge route "
                        "(version/neg-risk metadata missing); refusing to broadcast blind"
                    )
                    return None
                if route is SettlementRoute.CTF_MERGE:
                    try:
                        chain_yes = float(self.ctf_client.get_position_balance(self.asset_id_yes, addr))
                        chain_no = float(self.ctf_client.get_position_balance(self.asset_id_no, addr))
                    except Exception as e:
                        logger.error(f"Settlement aborted: on-chain token balances are unverifiable: {e}")
                        return None
                    if chain_yes + 1e-9 < sets_to_merge or chain_no + 1e-9 < sets_to_merge:
                        logger.error(
                            f"Settlement aborted: chain balances YES={chain_yes:.6f} NO={chain_no:.6f} "
                            f"cannot back {sets_to_merge:.6f} sets"
                        )
                        return None

            if route is SettlementRoute.CTF_MERGE:
                res = self.ctf_client.merge_positions(
                    condition_id=self.condition_id,
                    amount_shares=sets_to_merge,
                    token_ids=(self.asset_id_yes, self.asset_id_no),
                )
            else:
                ok, reason = verify_route(
                    route,
                    self.condition_id,
                    addr,
                    self.ctf_client.chain_client,
                    meta=meta,
                )
                if not ok:
                    logger.error(f"Settlement refused ({route.value}): {reason}")
                    return None
                try:
                    amount_6dp = amount_to_6dp(sets_to_merge)
                except ValueError as e:
                    logger.error(f"Settlement aborted: invalid amount {sets_to_merge!r}: {e}")
                    return None
                try:
                    if route is SettlementRoute.ROUTER_MERGE:
                        calldata = build_router_merge(self.condition_id, amount_6dp)
                        target = ROUTER
                    elif route is SettlementRoute.NEGRISK_MERGE:
                        calldata = build_negrisk_merge(self.condition_id, amount_6dp)
                        target = NEGRISK_ADAPTER
                    else:
                        logger.error(f"Settlement aborted: unhandled route {route}")
                        return None
                except ValueError as e:
                    logger.error(
                        f"Settlement aborted: cannot encode {route.value} calldata "
                        f"for {self.condition_id}: {e}"
                    )
                    return None
                logger.info(
                    f"[{route.value}] Broadcasting merge of {sets_to_merge:.6f} sets "
                    f"({amount_6dp} base units) to {target[:10]}..."
                )
                res = self.ctf_client.execute_settlement(
                    calldata,
                    to_address=target,
                    condition_id=self.condition_id,
                    amount_shares=sets_to_merge,
                )
            if not res.success:
                if getattr(res, "status", "failed") == "pending":
                    logger.warning(
                        f"CTF mergePositions pending for {self.condition_id[:14]}: "
                        f"{res.error or 'no receipt yet'}; the signed tx {res.tx_hash} is stored "
                        "and will be resolved by hash. No retry, no state change."
                    )
                    return res
                logger.critical(
                    f"CTF mergePositions failed for {self.condition_id[:14]}: {res.error or 'unknown error'}; "
                    "no inventory or cash change applied"
                )
                return res

            self._last_merge_ts = now
            self.shares_yes = max(0.0, self.shares_yes - sets_to_merge)
            self.shares_no = max(0.0, self.shares_no - sets_to_merge)
            self.cash += float(res.usdc_unlocked) - float(res.gas_fee_usd)
            self.total_gas_fees += float(res.gas_fee_usd)
            self.total_merges += 1
            self.merges_history.append(res)

            if not self.dry_run and route is SettlementRoute.CTF_MERGE:
                try:
                    self.shares_yes = float(
                        self.ctf_client.get_position_balance(self.asset_id_yes, addr)
                    )
                    self.shares_no = float(
                        self.ctf_client.get_position_balance(self.asset_id_no, addr)
                    )
                except Exception as e:
                    logger.warning(
                        f"Post-merge on-chain balance re-read failed ({e}); keeping the local decrement "
                        f"(YES {self.shares_yes:.4f}, NO {self.shares_no:.4f})"
                    )

            # Persist to SQLite
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO ctf_merges (
                    timestamp, condition_id, complete_sets, usdc_gross, gas_fee,
                    net_cash, realized_gain, cash_after
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    self.condition_id,
                    sets_to_merge,
                    res.usdc_unlocked,
                    res.gas_fee_usd,
                    res.usdc_unlocked - res.gas_fee_usd,
                    0.0,
                    self.cash,
                ),
            )
            self._conn.commit()
            self.flush_ledger()

            # Measurement: one row per confirmed merge, keyed by tx hash so a
            # retried call cannot double-count it. Never raises into settlement.
            try:
                tx_hash = str(getattr(res, "tx_hash", "") or "")
                dedupe_key = (
                    f"tx:{tx_hash}" if tx_hash else f"at:{self.condition_id}:{now:.3f}"
                )
                note = (
                    f"merge key={dedupe_key} sets={sets_to_merge:.4f} "
                    f"{'dry-run' if self.dry_run else 'live'}"
                )
                existing = self._conn.execute(
                    "SELECT 1 FROM rebalances WHERE pathway = 'MERGE' AND note = ? LIMIT 1",
                    (note,),
                ).fetchone()
                if existing is None:
                    self._insert_rebalance_row(
                        pathway="MERGE",
                        crossed_cents=0.0,
                        taker_fee_usd=0.0,
                        gas_usd=float(res.gas_fee_usd or 0.0),
                        inventory_before=float(available_sets),
                        inventory_after=max(0.0, float(available_sets) - sets_to_merge),
                        note=note,
                    )
            except Exception as e:  # measurement only; never raises into settlement
                logger.warning(f"Merge cost measurement failed: {e}")

            return res

    def trigger_ctf_split(self, amount_usdc: float) -> dict[str, Any]:
        """Split collateral into YES+NO. Only simulated: no on-chain split exists here."""
        if not self.dry_run:
            message = (
                "CTF split is not implemented for live settlement; refusing to mutate live "
                "inventory or cash"
            )
            logger.error(message)
            return {
                "success": False,
                "error": message,
                "amount_split": 0.0,
                "cash_after": self.cash,
            }

        amt = float(amount_usdc)
        self.cash -= amt
        self.shares_yes += amt
        self.shares_no += amt
        self.total_splits += 1

        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO ctf_splits (timestamp, condition_id, amount_split, cash_after)
            VALUES (?, ?, ?, ?)
            """,
            (time.time(), self.condition_id, amt, self.cash),
        )
        self._conn.commit()
        self.flush_ledger()
        return {"success": True, "amount_split": amt, "cash_after": self.cash}

    # -------------------------------------------------------------------------
    # Feed & Market Event Ingestion
    # -------------------------------------------------------------------------

    def evaluate_quotes(self, timestamp: Optional[float] = None) -> None:
        """Serialize quoting against fill/reconcile threads (RLock, re-entrant)."""
        with self._lock:
            super().evaluate_quotes(timestamp)

    def on_book_update(
        self,
        token_side: str,
        bids: list[Any],
        asks: list[Any],
        timestamp: Optional[float] = None,
    ) -> None:
        """Process incoming L2 order book updates from LivePolymarketFeed."""
        ts = timestamp or time.time()
        tok = str(token_side).upper()

        def _get_price(lvl: Any) -> float:
            if hasattr(lvl, "price"):
                return float(lvl.price)
            if isinstance(lvl, dict):
                return float(lvl.get("price", 0.0) or 0.0)
            return float(lvl[0]) if isinstance(lvl, (list, tuple)) else 0.0

        def _get_size(lvl: Any) -> float:
            if hasattr(lvl, "size"):
                return float(lvl.size)
            if isinstance(lvl, dict):
                return float(lvl.get("size", 100.0) or 100.0)
            return float(lvl[1]) if isinstance(lvl, (list, tuple)) and len(lvl) > 1 else 100.0

        if bids:
            self._best_bids[tok] = max(_get_price(b) for b in bids)
        if asks:
            self._best_asks[tok] = min(_get_price(a) for a in asks)
        self._book_ts[tok] = float(ts)

        # Update microstructure sentinel
        micro = self.microstructure_yes if tok == "YES" else self.microstructure_no
        b_val = self._best_bids.get(tok, 0.49)
        a_val = self._best_asks.get(tok, 0.51)
        bid_sz = sum(_get_size(b) for b in bids) if bids else 100.0
        ask_sz = sum(_get_size(a) for a in asks) if asks else 100.0
        try:
            micro.update(
                best_bid_price=b_val,
                best_bid_size=bid_sz,
                best_ask_price=a_val,
                best_ask_size=ask_sz,
                timestamp=ts,
            )
        except Exception as e:
            logger.debug(f"Microstructure sentinel update: {e}")

        # Price-jump detection uses trusted book mids, never public trade prints.
        # A jump enters a bounded per-asset cooldown; it never halts the engine.
        mid = self._fresh_mid(tok)
        if mid is not None:
            asset_id = self.asset_id_yes if tok == "YES" else self.asset_id_no
            try:
                healthy, message = self.risk_manager.update_market_price(
                    asset_id=asset_id,
                    condition_id=self.condition_id,
                    mid_price=mid,
                    timestamp=ts,
                )
            except Exception as e:
                logger.warning(f"Failed to record mid price for {asset_id}: {e}")
            else:
                if not healthy:
                    logger.warning(
                        f"[PRICE JUMP COOLDOWN] {tok}: {message} "
                        "(quotes cool down, engine is not halted)"
                    )

        # Circuit breaker / risk halt guard
        if self.risk_manager.portfolio_state.is_halted or self.risk_manager.portfolio_state.kill_switch_active:
            if self.active_quotes:
                self.cancel_all_quotes()
            return
        if not self.trading_enabled or self.market_closed:
            return

        # Re-evaluate quotes
        self.evaluate_quotes(timestamp=ts)

    def refresh_book_liveness(self, timestamp: Optional[float] = None) -> None:
        """Mark known books current while the feed connection is alive.

        The CLOB stream sends a book only when it changes, so a quiet market can
        go many minutes without a frame while its snapshot is still the current
        book. Staleness therefore tracks feed liveness, not frame recency; a book
        that has never been received stays stale.
        """
        ts = float(timestamp or time.time())
        for tok in ("YES", "NO"):
            if (
                tok in self._best_bids
                and tok in self._best_asks
                and self._best_bids[tok] > 0.0
                and self._best_asks[tok] > 0.0
            ):
                self._book_ts[tok] = ts

    def halt_trading(self, reason: str, cancel: bool = True) -> None:
        """Disable quoting and record why; by default also cancel resting orders.

        This is an operator/reconnect gate, not a loss breaker: it deliberately
        does not touch ``risk_manager.is_halted``, which stays reserved for
        permanent daily-loss/drawdown/kill-switch state.
        """
        self.trading_enabled = False
        self.halt_reason = str(reason)
        logger.critical(f"Trading halted: {reason}")
        if cancel:
            self.cancel_all_quotes()
        else:
            logger.warning("Resting orders remain live after halt (cancel=False)")

    def resume_trading(self) -> bool:
        """Clear an engine halt; refuse while a risk breaker or kill switch is active."""
        state = self.risk_manager.portfolio_state
        if state.is_halted or state.kill_switch_active:
            logger.error(
                "Resume refused: risk manager is "
                f"{'halted' if state.is_halted else 'kill-switched'}"
            )
            return False
        self.trading_enabled = True
        self.halt_reason = None
        logger.info("Trading resumed")
        return True

    def _apply_fill(
        self,
        tok: str,
        side: str,
        fill_sz: float,
        fill_px: float,
        fee_usd: float = 0.0,
        ts: Optional[float] = None,
        is_taker: bool = False,
    ) -> None:
        """Apply a confirmed fill to cash, inventory, cost basis, and realized PnL.

        A resting maker BUY already reserved its collateral at placement when
        the endpoint reports free cash, so its fill only moves the reserve.
        """
        tok = str(tok).upper()
        side = str(side).upper()
        size = float(fill_sz)
        price = float(fill_px)
        fee = float(fee_usd)
        if size <= 0.0 or not (0.0 < price < 1.0):
            return
        if tok not in ("YES", "NO") or side not in ("BUY", "SELL"):
            return
        if ts is None:
            ts = time.time()

        with self._lock:
            shares_attr = "shares_yes" if tok == "YES" else "shares_no"
            avg_attr = "avg_cost_yes" if tok == "YES" else "avg_cost_no"
            shares = float(getattr(self, shares_attr))
            avg = float(getattr(self, avg_attr))
            realized = 0.0
            allow_short = bool(getattr(self.risk_manager.config, "allow_short", False))

            if side == "BUY":
                if shares >= 0.0:
                    new_shares = shares + size
                    if not (self._cost_basis_unknown and shares > 0.0):
                        avg = (shares * avg + size * price) / new_shares
                else:
                    cover = min(size, -shares)
                    realized = (avg - price) * cover
                    if size + shares >= -1e-12:
                        new_shares = size + shares
                        avg = price if new_shares > 1e-12 else 0.0
                    else:
                        new_shares = shares + size
                if self.dry_run or self._locked_already_in_balance is True:
                    self.cash -= size * price + fee
                else:
                    self.cash -= fee
            else:
                if shares > 1e-12:
                    sold = min(size, shares)
                    realized = 0.0 if self._cost_basis_unknown else (price - avg) * sold
                    remaining = shares - sold
                    if size > sold + 1e-9:
                        extra = size - sold
                        if not allow_short:
                            logger.critical(
                                f"SELL {size:.6f} {tok} exceeds owned {shares:.6f}; "
                                f"clamping to {sold:.6f} (shorting disabled)"
                            )
                            if size > 0.0:
                                fee *= sold / size
                            size = sold
                            extra = 0.0
                        else:
                            remaining = -extra
                            avg = price
                    if remaining <= 1e-12:
                        remaining = 0.0
                        avg = 0.0
                    new_shares = remaining
                elif shares < 0.0:
                    short_sz = -shares
                    total = short_sz + size
                    avg = (short_sz * avg + size * price) / total
                    new_shares = -total
                else:
                    if not allow_short:
                        logger.critical(
                            f"SELL {size:.6f} {tok} with zero owned shares rejected "
                            "(shorting disabled)"
                        )
                        return
                    new_shares = -size
                    avg = price
                if size <= 0.0:
                    return
                self.cash += size * price - fee

            setattr(self, shares_attr, new_shares)
            setattr(self, avg_attr, round(float(avg), 10))
            self.realized_pnl += realized
            self.fees_paid_usd += fee
            self.total_volume_traded_usd += size * price

            asset_id = self.asset_id_yes if tok == "YES" else self.asset_id_no
            self.risk_manager.record_fill(
                asset_id=asset_id,
                condition_id=self.condition_id,
                side=side,
                price=price,
                size=size,
                timestamp=ts,
            )

            if (
                self._cost_basis_unknown
                and self.shares_yes <= 1e-12
                and self.shares_no <= 1e-12
            ):
                self._cost_basis_unknown = False

            if not self.dry_run:
                cfg = self.risk_manager.config
                min_sets = max(
                    float(self.auto_merge_threshold),
                    float(getattr(cfg, "min_merge_notional_usd", 0.0) or 0.0),
                )
                if min(self.shares_yes, self.shares_no) >= min_sets:
                    self.trigger_ctf_merge()

    @staticmethod
    def _order_id_of(ord_info: Any) -> str:
        if not isinstance(ord_info, dict):
            return ""
        return str(
            ord_info.get("id")
            or ord_info.get("orderID")
            or ord_info.get("order_id")
            or ""
        )

    def _order_matches_market(self, ord_info: Any) -> bool:
        if not isinstance(ord_info, dict):
            return False
        market = ord_info.get("market")
        return not market or str(market) == self.condition_id

    @staticmethod
    def _order_matched_size(ord_info: Any) -> float:
        if not isinstance(ord_info, dict):
            return 0.0
        for key in ("size_matched", "sizeMatched", "matched_size"):
            value = ord_info.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    @staticmethod
    def _order_original_size(ord_info: Any) -> float:
        if not isinstance(ord_info, dict):
            return 0.0
        for key in ("original_size", "originalSize", "size", "original_amount"):
            value = ord_info.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    @staticmethod
    def _order_price(ord_info: Any) -> Optional[float]:
        if not isinstance(ord_info, dict):
            return None
        for key in ("price", "avg_price"):
            value = ord_info.get(key)
            if value not in (None, ""):
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    return None
                return parsed if 0.0 < parsed < 1.0 else None
        return None

    @staticmethod
    def _order_status(ord_info: Any) -> str:
        if not isinstance(ord_info, dict):
            return ""
        return str(ord_info.get("status", "") or "").upper()

    def _fill_mode(self) -> str:
        return "DRY_RUN" if self.dry_run else "LIVE"

    def _record_fill_observation(
        self,
        *,
        fill_key: str,
        ts: float,
        tok: str,
        side: str,
        price: float,
        size: float,
        is_maker: bool,
        order_id: str,
        fee_usd: float,
        realized_pnl: float,
        note: str,
    ) -> None:
        """Persist one fill plus its post-fill marks (measurement only)."""
        mid = self._fresh_mid(tok)
        bid_at_fill: Optional[float] = None
        ask_at_fill: Optional[float] = None
        if mid is not None:
            raw_bid, raw_ask = self.get_best_bid_ask(tok)
            bid_at_fill = float(raw_bid) if 0.0 < float(raw_bid) < 1.0 else None
            ask_at_fill = float(raw_ask) if 0.0 < float(raw_ask) < 1.0 else None
            if bid_at_fill is None or ask_at_fill is None:
                mid = None
                bid_at_fill = None
                ask_at_fill = None
        self._insert_yield_fill_row(
            {
                "fill_key": fill_key,
                "ts": float(ts),
                "condition_id": self.condition_id,
                "token_side": tok,
                "side": side,
                "price": float(price),
                "size": float(size),
                "is_maker": 1 if is_maker else 0,
                "order_id": str(order_id or ""),
                "fee_usd": float(fee_usd),
                "mid_at_fill": mid,
                "best_bid_at_fill": bid_at_fill,
                "best_ask_at_fill": ask_at_fill,
                "markout_30s": None,
                "markout_5m": None,
                "markout_30m": None,
                "realized_pnl": float(realized_pnl),
                "mode": self._fill_mode(),
                "note": note,
                # Columns present only on a legacy fills table.
                "fill_id": fill_key,
                "timestamp": float(ts),
                "liquidity_type": "maker" if is_maker else "taker",
                "slippage_cents": 0.0,
                "queue_depleted": 0,
                "post_fill_drift": 0.0,
                "cash_after": float(self.cash),
                "notes": note,
            }
        )
        # Horizons are queued even when the book is stale now: the due-time read
        # decides, and an unmeasurable horizon simply stays NULL.
        self._markout_tracker.record(fill_key, tok, float(price), side, now=float(ts))

    def _book_exchange_fill(
        self,
        qdata: dict[str, Any],
        tok: str,
        side: str,
        size: float,
        price: float,
        is_taker: bool,
        ts: float,
        note: str,
    ) -> RealMoneyFillRecord:
        fee = self._taker_fee(size, price) if is_taker else 0.0
        record = RealMoneyFillRecord(
            fill_id=str(uuid.uuid4())[:8],
            timestamp=ts,
            condition_id=self.condition_id,
            token_side=tok,
            side=side,
            fill_price=price,
            fill_size=size,
            is_maker=not is_taker,
            order_id=str(qdata.get("order_id", "") or ""),
            notes=f"Live CLOB fill reconciled ({note})",
        )
        self.fills_history.append(record)
        shares_attr = "shares_yes" if str(tok).upper() == "YES" else "shares_no"
        shares_before = float(getattr(self, shares_attr, 0.0) or 0.0)
        realized_before = float(self.realized_pnl)
        self._apply_fill(tok, side, size, price, fee_usd=fee, ts=ts, is_taker=is_taker)
        realized_delta = float(self.realized_pnl) - realized_before
        shares_after = float(getattr(self, shares_attr, 0.0) or 0.0)

        order_id = record.order_id
        key_prefix = order_id or record.fill_id
        fill_key = f"{key_prefix}:{float(ts):.3f}:{float(price):.6f}:{float(size):.6f}"
        self._record_fill_observation(
            fill_key=fill_key,
            ts=ts,
            tok=str(tok).upper(),
            side=str(side).upper(),
            price=price,
            size=size,
            is_maker=not is_taker,
            order_id=order_id,
            fee_usd=fee,
            realized_pnl=realized_delta,
            note=record.notes,
        )

        if is_taker:
            reference = float(
                qdata.get("decision_target_price", qdata.get("price", price)) or price
            )
            self._insert_rebalance_row(
                pathway="TIER2_TAKER",
                ts=ts,
                crossed_cents=abs(float(price) - reference) * 100.0,
                taker_fee_usd=float(fee),
                gas_usd=0.0,
                inventory_before=shares_before,
                inventory_after=shares_after,
                note=f"tier2 taker fill ({note})",
            )
        return record

    def sync_live_orders_and_fills(self, timestamp: Optional[float] = None) -> List[RealMoneyFillRecord]:
        """Reconcile resting orders and fills strictly against confirmed exchange facts.

        A failed open-order query changes nothing. An order absent from the open
        list is never assumed filled: its status and final ``size_matched`` are
        read with ``get_order`` and only that delta is booked, at the exchange's
        own price. Thread-safe for the feed thread.
        """
        ts = timestamp or time.time()
        new_fills: List[RealMoneyFillRecord] = []
        terminal_statuses = {"MATCHED", "CANCELED", "CANCELLED", "CANCELED_MARKET_RESOLVED", "INVALID"}

        with self._lock:
            try:
                open_orders_list = self.clob_client.get_open_orders(market=self.condition_id)
            except ClobAPIError as e:
                logger.error(
                    f"Open-order reconciliation aborted with no state change: {e}"
                )
                return new_fills

            open_orders_by_id: dict[str, dict[str, Any]] = {}
            for ord_info in open_orders_list:
                order_id = self._order_id_of(ord_info)
                if order_id:
                    open_orders_by_id[order_id] = ord_info

            for qid, qdata in list(self.active_quotes.items()):
                order_id = str(qdata.get("order_id", "") or "")
                if not order_id:
                    continue

                quote_size = float(qdata.get("size", 0.0) or 0.0)
                quote_price = float(qdata.get("price", 0.0) or 0.0)
                side = str(qdata.get("side", "BUY")).upper()
                tok = str(qdata.get("token_side", "YES")).upper()
                prev_matched = float(qdata.get("size_matched", 0.0) or 0.0)
                is_taker = bool(qdata.get("is_taker", False))

                if order_id in open_orders_by_id:
                    live_ord = open_orders_by_id[order_id]
                    if not self._order_matches_market(live_ord):
                        logger.warning(
                            f"Order {order_id} returned for a different market; skipping it"
                        )
                        continue
                    current_matched = self._order_matched_size(live_ord)
                    status = self._order_status(live_ord)
                    exchange_price = self._order_price(live_ord)
                    if exchange_price is None:
                        exchange_price = quote_price
                    original_size = self._order_original_size(live_ord) or quote_size
                    delta = min(
                        max(0.0, current_matched - prev_matched),
                        max(0.0, original_size - prev_matched),
                    )
                    if delta > 1e-9 and 0.0 < exchange_price < 1.0:
                        new_fills.append(
                            self._book_exchange_fill(
                                qdata, tok, side, delta, exchange_price, is_taker, ts, "order still open"
                            )
                        )
                        qdata["size_matched"] = max(prev_matched, current_matched)
                    if (
                        current_matched >= original_size - 1e-9
                        or status in terminal_statuses
                        or float(qdata.get("size_matched", prev_matched)) >= quote_size - 1e-9
                    ):
                        self._release_reserved_cash(qdata)
                        self.active_quotes.pop(qid, None)
                    continue

                # Absent from the open list: never infer a fill. Read the order.
                try:
                    ord_info = self.clob_client.get_order(order_id)
                except ClobAPIError as e:
                    logger.warning(
                        f"Cannot resolve order {order_id} that left the open list ({e}); "
                        "keeping it tracked with no fill booked"
                    )
                    continue
                if ord_info is None:
                    logger.critical(
                        f"Order {order_id} left the open list and the venue returned no record; "
                        "keeping it tracked, no fill booked"
                    )
                    continue
                if not self._order_matches_market(ord_info):
                    logger.warning(
                        f"Resolved order {order_id} belongs to a different market; skipping it"
                    )
                    continue

                current_matched = self._order_matched_size(ord_info)
                status = self._order_status(ord_info)
                exchange_price = self._order_price(ord_info)
                if exchange_price is None:
                    exchange_price = quote_price
                original_size = self._order_original_size(ord_info) or quote_size
                delta = min(
                    max(0.0, current_matched - prev_matched),
                    max(0.0, original_size - prev_matched),
                )
                if delta > 1e-9 and 0.0 < exchange_price < 1.0:
                    new_fills.append(
                        self._book_exchange_fill(
                            qdata, tok, side, delta, exchange_price, is_taker, ts, "final order state"
                        )
                    )
                qdata["size_matched"] = max(prev_matched, current_matched)

                if (
                    status in terminal_statuses
                    or current_matched >= original_size - 1e-9
                    or float(qdata.get("size_matched", prev_matched)) >= quote_size - 1e-9
                ):
                    self._release_reserved_cash(qdata)
                    self.active_quotes.pop(qid, None)

            self.locked_cash = self._compute_locked_cash()
            if new_fills:
                self.flush_ledger()

        return new_fills

    def on_trade_update(
        self,
        token_side: str,
        trade_price: float,
        trade_size: float,
        trade_side: str,
        timestamp: Optional[float] = None,
    ) -> None:
        """Process a public trade print. Live fills are reconciled from the CLOB, not inferred here."""
        ts = timestamp or time.time()
        tok = str(token_side).upper()
        self.risk_manager.record_heartbeat(ts)

        if not self.dry_run:
            # Public prints between third parties do not fill our resting orders.
            return
        if not self.trading_enabled or self.market_closed:
            return

        # DRY-RUN / SIMULATED MODE ONLY: match public prints against mock quotes.
        taker_side = str(trade_side).upper()
        for qid, qdata in list(self.active_quotes.items()):
            if qdata.get("token_side") != tok:
                continue
            side = str(qdata.get("side", "")).upper()
            quote_price = float(qdata.get("price", 0.0) or 0.0)
            quote_size = float(qdata.get("size", 0.0) or 0.0)
            matched = float(qdata.get("size_matched", 0.0) or 0.0)
            remaining = quote_size - matched
            if remaining <= 1e-9:
                self._discard_dry_run_quote(qid)
                continue

            crossed = (
                side == "BUY"
                and taker_side == "SELL"
                and trade_price <= quote_price
            ) or (
                side == "SELL"
                and taker_side == "BUY"
                and trade_price >= quote_price
            )
            if not crossed:
                continue

            fill_size = min(remaining, float(trade_size))
            if fill_size <= 1e-9:
                continue
            fill_rec = RealMoneyFillRecord(
                fill_id=str(uuid.uuid4())[:8],
                timestamp=ts,
                condition_id=self.condition_id,
                token_side=tok,
                side=side,
                fill_price=quote_price,
                fill_size=fill_size,
                is_maker=True,
                order_id=qdata.get("order_id", ""),
                notes="Simulated maker fill matched against market trade (Dry-Run)",
            )
            self.fills_history.append(fill_rec)
            self._apply_fill(tok, side, fill_size, quote_price, ts=ts)
            qdata["size_matched"] = matched + fill_size
            if quote_size - qdata["size_matched"] <= 1e-9:
                self._discard_dry_run_quote(qid)
            self.evaluate_quotes(timestamp=ts)
            self.flush_ledger()

    def _discard_dry_run_quote(self, qid: str) -> None:
        """Remove a fully simulated quote and its mock CLOB order."""
        qdata = self.active_quotes.pop(qid, None)
        if not qdata:
            return
        self._release_reserved_cash(qdata)
        order_id = str(qdata.get("order_id", "") or "")
        if order_id:
            self.clob_client.cancel_order(order_id)

    # -------------------------------------------------------------------------
    # Ledger Flushing & Telemetry
    # -------------------------------------------------------------------------

    def _order_version_safe(self) -> int:
        """Cached CLOB order version without forcing a network call in dry-run."""
        raw = getattr(self.clob_client, "_order_version", None)
        if raw is None:
            if self.dry_run:
                return 2
            raw = getattr(self.clob_client, "order_version", 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def update_quote_size(self, new_quote_size: float, timestamp: Optional[float] = None) -> bool:
        """Apply governor sizing only when it is a material, in-limit change."""
        new_size = float(new_quote_size)
        current = float(self.quote_size)
        max_shares = float(
            getattr(self.risk_manager.config, "max_quote_size_shares", 0.0) or 0.0
        )
        if max_shares > 0.0 and new_size > max_shares:
            logger.warning(
                f"Governor RESIZE_QUOTE ignored: {new_size:.2f} shares exceeds "
                f"max_quote_size_shares={max_shares:.2f}"
            )
            return False
        if current > 0.0 and abs(new_size - current) / current < 0.25:
            logger.info(
                f"Governor RESIZE_QUOTE ignored: {current:.2f} -> {new_size:.2f} shares "
                "is a <25% change"
            )
            return False
        return super().update_quote_size(new_quote_size, timestamp=timestamp)

    def close(self) -> None:
        """Cancel quotes with venue confirmation, then close persistence."""
        try:
            tracker = getattr(self, "_markout_tracker", None)
            if tracker is not None:
                pending = tracker.stop()
                if pending:
                    logger.info(
                        f"Shutdown: {pending} markout horizon(s) not reached; left NULL"
                    )
        except Exception as e:
            logger.warning(f"Shutdown: markout tracker stop failed: {e}")
        try:
            confirmed = self.cancel_all_quotes()
            if confirmed:
                logger.info("Shutdown: venue confirmed all resting quotes are cancelled")
            else:
                logger.warning(
                    "Shutdown: venue did not confirm that all quotes are cancelled; "
                    "verify open orders before restarting"
                )
        except Exception as e:
            logger.error(f"Shutdown: quote cancellation failed: {e}")
        try:
            self.flush_wal()
        except Exception as e:
            logger.warning(f"Shutdown: WAL checkpoint failed: {e}")
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def flush_ledger(self) -> None:
        """Write atomic state snapshot to SQLite and JSON ledger."""
        equity_val = self.get_equity()
        cash_val = self.get_cash()
        pos = self.get_position(self.condition_id)
        locked_val = self.get_locked_cash()
        inventory_val = equity_val - cash_val - locked_val
        total_pnl_val = equity_val - self.initial_equity

        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO portfolio_snapshots (
                timestamp, cash, market_value, equity, unrealized_pnl, realized_pnl,
                total_pnl, total_rewards, total_gas_fees, total_merges, total_splits, total_volume
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                cash_val,
                inventory_val,
                equity_val,
                inventory_val,
                pos.realized_pnl,
                total_pnl_val,
                self.total_rewards_credited,
                self.total_gas_fees,
                self.total_merges,
                self.total_splits,
                self.total_volume_traded_usd,
            ),
        )

        cur.execute(
            """
            INSERT INTO positions (
                timestamp, condition_id, shares_yes, shares_no, avg_cost_yes,
                avg_cost_no, net_exposure, complete_sets, realized_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                self.condition_id,
                pos.shares_yes,
                pos.shares_no,
                pos.avg_cost_yes,
                pos.avg_cost_no,
                pos.net_exposure,
                pos.complete_sets,
                pos.realized_pnl,
            ),
        )
        self._conn.commit()

        ledger_data = {
            "updated_at": time.time(),
            "condition_id": self.condition_id,
            "mode": "DRY_RUN" if self.dry_run else "REAL_MONEY",
            "portfolio": {
                "cash": cash_val,
                "free_cash": cash_val,
                "locked_cash": locked_val,
                "initial_equity": self.initial_equity,
                "total_wallet_balance": getattr(self, "total_wallet_balance", cash_val),
                "budget_pct": getattr(self, "budget_pct", 1.0),
                "trading_bankroll": getattr(self, "trading_bankroll", self.initial_cash),
                "pol_balance": self.pol_balance,
                "equity": equity_val,
                "unrealized_pnl": inventory_val,
                "realized_pnl": pos.realized_pnl,
                "total_pnl": total_pnl_val,
                "total_rewards_credited": self.total_rewards_credited,
                "modelled_rewards": self.total_rewards_credited,
                "actual_rewards": self.actual_rewards_usd,
                "actual_rewards_usd": self.actual_rewards_usd,
                "actual_rebates_usd": self.actual_rebates_usd,
                "actual_daily_rewards_usd": self.actual_daily_rewards_usd,
                "reward_source": self.reward_source,
                "fees_paid": self.fees_paid_usd,
                "order_version": self._order_version_safe(),
                "tick_size": self.tick_size,
                "total_gas_fees": self.total_gas_fees,
                "total_merges": self.total_merges,
                "total_splits": self.total_splits,
            },
            "position": asdict(pos),
            "active_quotes": list(self.active_quotes.values()),
            "last_deskew": asdict(self.latest_deskew_decision) if self.latest_deskew_decision else None,
            "epoch_index": self.epoch_index,
            "recent_fills": [f.to_dict() for f in self.fills_history[-10:]],
            "recent_merges": [asdict(m) for m in self.merges_history[-10:]],
        }

        tmp_path = Path(f"{self.ledger_path}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(ledger_data, f, indent=2)
            os.replace(tmp_path, self.ledger_path)
        except Exception as e:
            logger.warning(f"Failed to write JSON ledger atomically: {e}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def get_telemetry(self) -> dict[str, Any]:
        """Comprehensive telemetry dictionary for terminal dashboard rendering."""
        cash_val = self.get_cash()
        equity_val = self.get_equity()
        pos = self.get_position(self.condition_id)
        locked_val = self.get_locked_cash()
        inventory_val = equity_val - cash_val - locked_val

        v_queue_total = self.compute_resting_queue_value()
        reserve_ratio_pct = (cash_val / max(1e-4, equity_val)) * 100.0
        daily_yield = self.get_current_daily_yield()
        init_cash = max(1.0, self.initial_cash)
        nominal_apr = (daily_yield / init_cash) * 365.25 * 100.0
        r_daily = max(0.0, daily_yield / init_cash)
        try:
            compounding_apr = (
                (math.pow(1.0 + r_daily, 365.25) - 1.0) * 100.0
                if (0.0 < r_daily <= 5.0)
                else (99999.9 if r_daily > 5.0 else 0.0)
            )
        except (OverflowError, ValueError):
            compounding_apr = 99999.9
        total_wb = cash_val + locked_val + inventory_val
        b_pct = getattr(self, "budget_pct", 1.0) * 100.0
        t_bankroll = getattr(self, "trading_bankroll", self.initial_cash)
        book_stale = not (self.has_fresh_book("YES") and self.has_fresh_book("NO"))

        return {
            "timestamp": time.time(),
            "condition_id": self.condition_id,
            "mode": "DRY_RUN" if self.dry_run else "REAL_MONEY",
            "equity": round(equity_val, 2),
            "cash": round(cash_val, 2),
            "free_cash": round(cash_val, 2),
            "locked_cash": round(locked_val, 2),
            "initial_equity": round(self.initial_equity, 2),
            "total_wallet_balance": round(total_wb, 2),
            "budget_pct": round(b_pct, 1),
            "trading_bankroll": round(t_bankroll, 2),
            "pol_balance": round(self.pol_balance, 4),
            "market_value": round(inventory_val, 2),
            "realized_pnl": round(pos.realized_pnl, 4),
            "unrealized_pnl": round(inventory_val, 4),
            "total_pnl": round(equity_val - self.initial_equity, 4),
            "shares_yes": round(pos.shares_yes, 2),
            "shares_no": round(pos.shares_no, 2),
            "net_exposure": round(pos.net_exposure, 2),
            "complete_sets": round(pos.complete_sets, 2),
            "avg_cost_yes": round(pos.avg_cost_yes, 4),
            "avg_cost_no": round(pos.avg_cost_no, 4),
            "active_quotes": list(self.active_quotes.values()),
            "fills_count": len(self.fills_history),
            "recent_fills": [f.to_dict() for f in self.fills_history[-5:]],
            "total_merges": self.total_merges,
            "total_splits": self.total_splits,
            "total_gas_fees": round(self.total_gas_fees, 4),
            "total_rewards": round(self.total_rewards_credited, 4),
            "theoretical_rewards_usd": round(self.total_rewards_credited, 4),
            "modelled_rewards": round(self.total_rewards_credited, 4),
            "actual_rewards": round(self.actual_rewards_usd, 4),
            "actual_rewards_usd": round(self.actual_rewards_usd, 4),
            "actual_rebates_usd": round(self.actual_rebates_usd, 4),
            "actual_daily_rewards_usd": round(self.actual_daily_rewards_usd, 4),
            "actual_total_payout_usd": round(self.actual_rewards_usd + self.actual_rebates_usd, 4),
            "reward_source": self.reward_source,
            "verified_scoring_orders": dict(self.verified_scoring_orders),
            "total_volume_usd": round(self.total_volume_traded_usd, 2),
            "fees_paid": round(self.fees_paid_usd, 6),
            "epoch_index": self.epoch_index,
            "projected_daily_usd": float(
                self._last_reward_epoch.get("projected_daily_usd", 0.0) or 0.0
            ),
            "accrued_daily_usd": float(
                self._last_reward_epoch.get("accrued_daily_usd", 0.0) or 0.0
            ),
            "latest_deskew": asdict(self.latest_deskew_decision) if self.latest_deskew_decision else None,
            "reserve_cash_usd": round(cash_val, 2),
            "reserve_ratio_pct": round(reserve_ratio_pct, 1),
            "resting_queue_value_usd": round(v_queue_total, 4),
            "daily_yield_usd": round(daily_yield, 2),
            "nominal_apr_pct": round(nominal_apr, 1),
            "compounding_apr_pct": round(compounding_apr, 1),
            "migration_count": len(self.migration_history),
            "trading_enabled": self.trading_enabled,
            "market_closed": self.market_closed,
            "halt_reason": self.halt_reason,
            "book_stale": book_stale,
            "order_version": self._order_version_safe(),
            "tick_size": self.tick_size,
        }




