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

import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

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
from polymarketbot.rewards.microstructure import MicrostructureEngine
from polymarketbot.rewards.queue_valuation import (
    CapitalMigrationEngine,
    HurdleDecision,
    SwitchingCostDecomposition,
)
from polymarketbot.rewards.scoring import quadratic_order_score
from polymarketbot.risk.manager import RiskManager
from polymarketbot.risk.models import CircuitBreakerReason, RiskConfig
from polymarketbot.trading.base import BaseMarketMakingEngine, PositionView
from polymarketbot.trading.client import (
    ClobAuthCredentials,
    LiveClobClient,
    OrderPlacementResult,
)
from polymarketbot.trading.ctf import MergeExecutionResult, PolygonCtfClient

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

    def summary(self) -> str:
        lines = [
            f"Preflight Check: {'READY FOR LIVE' if self.ready_for_live else 'DRY-RUN / ACTION REQUIRED'}",
            f"  - Wallet: {self.wallet_address}",
            f"  - CLOB API Credentials: {'CONFIGURED' if self.api_key_configured else 'UNCONFIGURED (Dry-Run)'}",
            f"  - Polygon RPC: {'CONNECTED' if self.rpc_connected else 'FAILED / DISCONNECTED'}",
            f"  - USDC Balance: ${self.usdc_balance:.2f}",
            f"  - POL Gas Balance: {self.pol_balance:.4f} POL",
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
    ) -> None:
        init_cash = float(initial_cash)
        rm = risk_manager or RiskManager(
            RiskConfig(
                total_bankroll_usd=init_cash,
                price_jump_window_seconds=60.0,
                price_jump_threshold_cents=2.0,
                cooldown_duration_seconds=300.0,
                daily_loss_limit_usd=min(15.0, init_cash * 0.05 if init_cash > 0 else 15.0),
                daily_loss_limit_pct=0.05,
                max_drawdown_usd=min(30.0, init_cash * 0.10 if init_cash > 0 else 30.0),
                max_drawdown_pct=0.10,
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

        # Wallet & Inventory State
        self.cash: float = self.initial_cash
        self.pol_balance: float = 0.50
        self.shares_yes: float = 0.0
        self.shares_no: float = 0.0
        self.avg_cost_yes: float = 0.0
        self.avg_cost_no: float = 0.0
        self.realized_pnl: float = 0.0
        self.total_volume_traded_usd: float = 0.0
        self.total_gas_fees: float = 0.0
        self.total_merges: int = 0
        self.total_splits: int = 0

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

        # Auto sync initial balances for live mode or configured credentials
        if not self.dry_run or (self.clob_client.credentials and self.clob_client.credentials.is_configured):
            self.sync_balances()

        logger.info(
            f"RealMoneyTradingEngine initialized for condition {self.condition_id[:14]}... | "
            f"Mode: {'DRY-RUN (Simulated)' if self.dry_run else 'LIVE REAL-MONEY'} | "
            f"Cash: ${self.cash:.2f} | POL: {self.pol_balance:.4f}"
        )

    # -------------------------------------------------------------------------
    # Pre-Flight Readiness Validation
    # -------------------------------------------------------------------------

    def preflight_check(self) -> PreflightReport:
        """Inspect credentials, RPC connection, wallet balances, and contract allowances."""
        api_ok = self.clob_client.credentials.is_configured
        format_errors = self.clob_client.credentials.validate_formatting()
        has_key = self.clob_client.credentials.has_signing_key
        addr = self.clob_client.address or self.ctf_client.private_key
        rpc_ok = False
        try:
            bn = self.ctf_client.chain_client.block_number()
            rpc_ok = bn > 0
        except Exception:
            rpc_ok = False

        self.sync_balances()
        allowances = self.clob_client.get_allowances()
        approved = bool(allowances.get("approved", False))

        recs: list[str] = []
        ready = True

        if not api_ok or not has_key:
            ready = False
            recs.append(
                "CLOB API credentials (POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE, POLY_PRIVATE_KEY) "
                "missing or incomplete. Provide them in .env to place live orders."
            )
        if format_errors:
            ready = False
            for fe in format_errors:
                recs.append(f"Credential formatting error: {fe}")
        if not rpc_ok:
            ready = False
            recs.append("Polygon RPC connection failed. Check POLYGON_RPC_URL or network connection.")
        if self.cash <= 0.0:
            ready = False
            recs.append(
                f"USDC Balance is $0.00! Fund your wallet ({addr}) with USDC on Polygon to begin quoting."
            )
        if self.pol_balance < 0.05:
            recs.append(
                f"POL Gas balance is low ({self.pol_balance:.4f} POL). Deposit at least 1-2 POL for CTF merges."
            )
        if not approved:
            ready = False
            recs.append(
                "CTF Exchange allowances have not been approved. Submit approvals before trading real funds."
            )

        if not ready and not self.dry_run:
            self.dry_run = True
            self.clob_client.dry_run = True
            self.ctf_client.dry_run = True
            recs.append("Safety override: Dry-run mode has been automatically activated.")

        return PreflightReport(
            ready_for_live=ready and not self.dry_run,
            api_key_configured=api_ok and has_key and not format_errors,
            rpc_connected=rpc_ok,
            wallet_address=addr,
            usdc_balance=self.cash,
            pol_balance=self.pol_balance,
            allowances_approved=approved,
            dry_run_mode=self.dry_run,
            recommendations=recs,
        )

    # -------------------------------------------------------------------------
    # Balance & State Synchronization
    # -------------------------------------------------------------------------

    def sync_live_balances(self) -> dict[str, float]:
        """Synchronize on-chain and CLOB balances for USDC, POL, and token shares."""
        balances: dict[str, float] = {"cash": self.cash, "pol": self.pol_balance}
        try:
            bal_data = self.clob_client.get_balance()
            clob_cash = None
            if bal_data and bal_data.get("available") is not None:
                clob_cash = float(bal_data["available"])

            addr = self.clob_client.address
            if not addr and hasattr(self.ctf_client, "private_key") and self.ctf_client.private_key:
                addr = getattr(self.clob_client.credentials, "address", "")

            onchain_usdc = 0.0
            if addr:
                self.pol_balance = self.ctf_client.get_pol_balance(addr)
                onchain_usdc = self.ctf_client.get_usdc_balance(addr)

            raw_wallet = max(clob_cash or 0.0, onchain_usdc or 0.0)
            if raw_wallet > 0.0:
                self.total_wallet_balance = raw_wallet
                budgeted = raw_wallet * getattr(self, "budget_pct", 1.0)
                if getattr(self, "max_budget", None) is not None and self.max_budget > 0:
                    budgeted = min(budgeted, self.max_budget)
                self.cash = round(budgeted, 2)
            elif not self.dry_run and not (self.clob_client.credentials and self.clob_client.credentials.is_configured):
                self.cash = 0.0

            balances["cash"] = self.cash
            balances["pol"] = self.pol_balance
        except Exception as e:
            logger.debug(f"Failed to sync live balances: {e}")

        self.sync_rewards()
        return balances

    def sync_balances(self) -> None:
        """Alias for sync_live_balances()."""
        self.sync_live_balances()

    def sync_rewards(self) -> None:
        """Synchronize ground-truth reward and rebate balances from Polymarket APIs."""
        try:
            rew_data = self.clob_client.get_total_rewards()
            if rew_data:
                self.actual_rewards_usd = float(rew_data.get("reward_income_usd", 0.0) or 0.0)
                self.actual_rebates_usd = float(rew_data.get("maker_rebates_usd", 0.0) or 0.0)
                self.reward_source = str(rew_data.get("source", "simulated"))

            daily_data = self.clob_client.get_daily_rewards()
            if daily_data:
                self.actual_daily_rewards_usd = float(daily_data.get("daily_combined_usd", 0.0) or 0.0)

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

    # -------------------------------------------------------------------------
    # Subclass Hook Implementations
    # -------------------------------------------------------------------------

    def get_cash(self) -> float:
        return self.cash

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

    def get_equity(self, prices: Optional[dict[str, tuple[float, float]]] = None) -> float:
        b_yes, a_yes = self.get_best_bid_ask("YES")
        b_no, a_no = self.get_best_bid_ask("NO")
        mid_yes = (b_yes + a_yes) / 2.0
        mid_no = (b_no + a_no) / 2.0
        inv_val = (self.shares_yes * mid_yes) + (self.shares_no * mid_no)
        return self.cash + inv_val

    def get_best_bid_ask(self, token_side: str) -> Tuple[float, float]:
        tok = token_side.upper()
        bid = self._best_bids.get(tok, 0.49 if tok == "YES" else 0.49)
        ask = self._best_asks.get(tok, 0.51 if tok == "YES" else 0.51)
        return bid, ask

    def credit_reward(self, amount: float, condition_id: str, note: str = "") -> None:
        self.cash += amount

    def _set_quote(
        self,
        token_side: str,
        quote_side: str,
        price: float,
        size: float,
        ts: float,
    ) -> None:
        """Place or update a live signed limit order on Polymarket CLOB."""
        qid = f"{quote_side}_{token_side}".upper()
        token_id = self.asset_id_yes if token_side.upper() == "YES" else self.asset_id_no

        existing = self.active_quotes.get(qid)
        if (
            existing
            and abs(existing.get("price", 0.0) - price) < 1e-4
            and abs(existing.get("size", 0.0) - size) < 1e-4
        ):
            # Price and size unchanged: retain active CLOB order without queue penalty
            return

        # Cancel previous resting order if modifying price or size
        if existing and existing.get("order_id"):
            old_oid = existing["order_id"]
            self._canceled_order_ids.add(old_oid)
            self.clob_client.cancel_order(old_oid)

        # Place new signed order
        res = self.clob_client.place_order(
            token_id=token_id,
            side=quote_side,
            price=price,
            size=size,
        )

        if not res.success:
            logger.warning(f"Failed to place live CLOB quote {qid}: {res.error_message}")
            self.active_quotes.pop(qid, None)
            return

        # Score quote for rewards
        b_tok, a_tok = self.get_best_bid_ask(token_side)
        mid_tok = (b_tok + a_tok) / 2.0
        spread_cents = abs(price - mid_tok) * 100.0
        score = quadratic_order_score(
            max_spread_cents=self.rewards_max_spread,
            spread_cents=spread_cents,
            size=size,
            min_size=self.rewards_min_size,
        )

        vq_res = self.migration_engine.queue_model.evaluate_queue_value(
            quote_size=size,
            queue_ahead=0.0,
            half_spread_cents=max(0.1, spread_cents),
            token_mid_price=mid_tok,
        )

        quote_data = {
            "quote_id": qid,
            "order_id": res.order_id,
            "token_side": token_side.upper(),
            "side": quote_side.upper(),
            "price": price,
            "size": size,
            "size_matched": 0.0,
            "queue_ahead": 0.0,
            "score": round(score, 4),
            "v_queue": round(vq_res.net_queue_value_usd, 4),
            "timestamp": ts,
            "status": "LIVE",
            "is_dry_run": res.is_dry_run,
        }
        self.active_quotes[qid] = quote_data

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
                token_side.upper(),
                quote_side.upper(),
                price,
                size,
                0.0,
                score,
                "LIVE",
            ),
        )
        self._conn.commit()

    def _cancel_quote(self, qid: str) -> None:
        """Cancel an individual resting quote on CLOB."""
        if qid in self.active_quotes:
            quote_data = self.active_quotes.pop(qid)
            order_id = quote_data.get("order_id", "")
            if order_id:
                self._canceled_order_ids.add(order_id)
                self.clob_client.cancel_order(order_id)

    def cancel_all_quotes(self) -> None:
        """Cancel all resting orders on Polymarket CLOB."""
        for qdata in self.active_quotes.values():
            order_id = qdata.get("order_id", "")
            if order_id:
                self._canceled_order_ids.add(order_id)
        self.active_quotes.clear()
        self.clob_client.cancel_all_orders()

    def _execute_tier2_taker_deskew(self, decision: DeSkewDecision, ts: float) -> None:
        """Execute Tier 2 emergency cross-spread taker execution on CLOB."""
        self._record_deskew_event(decision, ts)
        target_token = decision.target_token
        target_side = decision.action_side
        target_size = min(decision.target_size, self.quote_size * 2.0)
        target_price = decision.target_price
        token_id = self.asset_id_yes if target_token.upper() == "YES" else self.asset_id_no

        logger.warning(
            f"REAL TRADER: EXECUTING TIER 2 EMERGENCY DE-SKEW ({decision.pathway.value}): "
            f"{target_side} {target_size:.2f} {target_token} @ ${target_price:.3f} | {decision.reason}"
        )

        res = self.clob_client.place_order(
            token_id=token_id,
            side=target_side,
            price=target_price,
            size=target_size,
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
            order_id=res.order_id,
            notes=f"Tier 2 Emergency Taker Fill ({decision.pathway.value})",
        )
        self.fills_history.append(fill_rec)

        # Update position tracking
        if target_side == "BUY":
            if target_token == "YES":
                self.shares_yes += target_size
            else:
                self.shares_no += target_size
            self.cash -= target_size * target_price
        else:
            if target_token == "YES":
                self.shares_yes = max(0.0, self.shares_yes - target_size)
            else:
                self.shares_no = max(0.0, self.shares_no - target_size)
            self.cash += target_size * target_price

        # If synthetic merge: trigger CTF merge
        if decision.pathway == DeSkewPathway.SYNTHETIC_MERGE:
            self.trigger_ctf_merge()

    def update_dynamic_gas_and_threshold(self) -> Tuple[float, float]:
        """Update dynamic gas fee and optimal merge threshold from live on-chain conditions."""
        gas_cost = self.ctf_client.estimate_merge_cost_usd()
        threshold = self.ctf_client.calculate_optimal_merge_threshold(gas_cost_usd=gas_cost)
        self.gas_fee_merge = gas_cost
        self.auto_merge_threshold = threshold
        return gas_cost, threshold

    def trigger_ctf_merge(self, amount_sets: Optional[float] = None) -> Optional[MergeExecutionResult]:
        """Execute on-chain CTF complete-set merge via PolygonCtfClient."""
        pos = self.get_position(self.condition_id)
        available_sets = min(pos.shares_yes, pos.shares_no)

        if available_sets <= 0.0:
            logger.debug(
                f"CTF merge skipped: no complete sets held (YES: {pos.shares_yes:.2f}, NO: {pos.shares_no:.2f})"
            )
            return None

        if amount_sets is not None:
            if amount_sets > available_sets:
                logger.warning(
                    f"Requested CTF merge amount ({amount_sets:.2f}) exceeds available complete sets "
                    f"({available_sets:.2f}). Clamping to {available_sets:.2f}."
                )
                sets_to_merge = available_sets
            else:
                sets_to_merge = float(amount_sets)
        else:
            sets_to_merge = available_sets

        if sets_to_merge < self.auto_merge_threshold and amount_sets is None:
            return None

        # Verify that actual complete sets are held before broadcasting on-chain mergePositions
        if sets_to_merge <= 0.0 or sets_to_merge > min(pos.shares_yes, pos.shares_no):
            logger.warning(f"Aborting CTF merge: sets_to_merge={sets_to_merge:.2f} is invalid or exceeds inventory.")
            return None

        res = self.ctf_client.merge_positions(
            condition_id=self.condition_id,
            amount_shares=sets_to_merge,
        )

        if res.success:
            self.shares_yes = max(0.0, self.shares_yes - sets_to_merge)
            self.shares_no = max(0.0, self.shares_no - sets_to_merge)
            self.cash += res.usdc_unlocked - res.gas_fee_usd
            self.total_gas_fees += res.gas_fee_usd
            self.total_merges += 1
            self.merges_history.append(res)

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
                    time.time(),
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

        return res

    def trigger_ctf_split(self, amount_usdc: float) -> dict[str, Any]:
        """Execute simulated or on-chain CTF Split ($1.00 USDC -> 1 YES + 1 NO)."""
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
        return {"amount_split": amt, "cash_after": self.cash}

    # -------------------------------------------------------------------------
    # Feed & Market Event Ingestion
    # -------------------------------------------------------------------------

    def on_book_update(
        self,
        token_side: str,
        bids: list[Any],
        asks: list[Any],
        timestamp: Optional[float] = None,
    ) -> None:
        """Process incoming L2 order book updates from LivePolymarketFeed."""
        ts = timestamp or time.time()
        tok = token_side.upper()

        # Circuit breaker / risk halt guard
        if self.risk_manager.portfolio_state.is_halted or self.risk_manager.portfolio_state.kill_switch_active:
            if self.active_quotes:
                self.cancel_all_quotes()
            return

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
            best_bid = max(_get_price(b) for b in bids)
            self._best_bids[tok] = best_bid
        if asks:
            best_ask = min(_get_price(a) for a in asks)
            self._best_asks[tok] = best_ask

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

        # Re-evaluate quotes
        self.evaluate_quotes(timestamp=ts)

    def _apply_fill(self, tok: str, side: str, fill_sz: float, fill_px: float) -> None:
        """Apply inventory, cash, and volume changes resulting from a confirmed fill."""
        if fill_sz <= 0.0:
            return

        if side == "BUY":
            if tok == "YES":
                self.shares_yes += fill_sz
            else:
                self.shares_no += fill_sz
            self.cash -= fill_sz * fill_px
        else:
            if tok == "YES":
                self.shares_yes = max(0.0, self.shares_yes - fill_sz)
            else:
                self.shares_no = max(0.0, self.shares_no - fill_sz)
            self.cash += fill_sz * fill_px

        self.total_volume_traded_usd += fill_sz * fill_px

        # Check CTF auto-merge
        pos = self.get_position(self.condition_id)
        if pos.complete_sets >= self.auto_merge_threshold:
            self.trigger_ctf_merge()

    def sync_live_orders_and_fills(self, timestamp: Optional[float] = None) -> List[RealMoneyFillRecord]:
        """Reconcile active resting orders and fills against live CLOB open orders.

        Queries the CLOB exchange /orders API for active orders.
        Reconciles size_matched for resting orders, and emits authentic RealMoneyFillRecord
        when matched size increases or an order leaves open orders without being explicitly canceled.
        """
        ts = timestamp or time.time()
        new_fills: List[RealMoneyFillRecord] = []

        try:
            open_orders_list = self.clob_client.get_open_orders()
        except Exception as e:
            logger.error(f"Failed to fetch live open orders from CLOB: {e}")
            return new_fills

        # Map by order ID
        open_orders_by_id: dict[str, dict[str, Any]] = {}
        for ord_info in open_orders_list:
            if isinstance(ord_info, dict):
                oid = str(ord_info.get("orderID") or ord_info.get("id") or ord_info.get("order_id") or "")
                if oid:
                    open_orders_by_id[oid] = ord_info

        # Reconcile each active quote in our engine
        for qid, qdata in list(self.active_quotes.items()):
            order_id = str(qdata.get("order_id", ""))
            if not order_id:
                continue

            quote_sz = float(qdata.get("size", 0.0))
            quote_px = float(qdata.get("price", 0.0))
            side = str(qdata.get("side", "BUY")).upper()
            tok = str(qdata.get("token_side", "YES")).upper()
            prev_matched = float(qdata.get("size_matched", 0.0))

            if order_id in open_orders_by_id:
                # Order is still active on exchange
                live_ord = open_orders_by_id[order_id]
                current_matched = float(
                    live_ord.get("size_matched", live_ord.get("matched_size", live_ord.get("sizeMatched", 0.0))) or 0.0
                )
                status = str(live_ord.get("status", "LIVE")).upper()

                matched_delta = current_matched - prev_matched
                if matched_delta > 0.0:
                    fill_sz = min(matched_delta, max(0.0, quote_sz - prev_matched))
                    fill_rec = RealMoneyFillRecord(
                        fill_id=str(uuid.uuid4())[:8],
                        timestamp=ts,
                        condition_id=self.condition_id,
                        token_side=tok,
                        side=side,
                        fill_price=quote_px,
                        fill_size=fill_sz,
                        is_maker=True,
                        order_id=order_id,
                        notes="Live CLOB maker fill synchronized from exchange open orders",
                    )
                    self.fills_history.append(fill_rec)
                    new_fills.append(fill_rec)
                    self._apply_fill(tok=tok, side=side, fill_sz=fill_sz, fill_px=quote_px)
                    qdata["size_matched"] = current_matched

                if current_matched >= quote_sz or status in ("FILLED", "MATCHED", "CLOSED"):
                    # Order is completely filled
                    self.active_quotes.pop(qid, None)

            else:
                # Order is no longer present in open orders on exchange
                if order_id in self._canceled_order_ids:
                    # Explicitly canceled by our engine
                    self.active_quotes.pop(qid, None)
                else:
                    # Disappeared without cancellation: order was fully filled on CLOB!
                    unfilled_sz = max(0.0, quote_sz - prev_matched)
                    if unfilled_sz > 0.0:
                        fill_rec = RealMoneyFillRecord(
                            fill_id=str(uuid.uuid4())[:8],
                            timestamp=ts,
                            condition_id=self.condition_id,
                            token_side=tok,
                            side=side,
                            fill_price=quote_px,
                            fill_size=unfilled_sz,
                            is_maker=True,
                            order_id=order_id,
                            notes="Live CLOB maker fill inferred from completed open order",
                        )
                        self.fills_history.append(fill_rec)
                        new_fills.append(fill_rec)
                        self._apply_fill(tok=tok, side=side, fill_sz=unfilled_sz, fill_px=quote_px)
                    self.active_quotes.pop(qid, None)

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
        """Process incoming trade event from WebSocket/REST feed."""
        ts = timestamp or time.time()
        tok = token_side.upper()
        self.risk_manager.record_heartbeat(ts)

        # Circuit breaker / risk halt guard
        if self.risk_manager.portfolio_state.is_halted or self.risk_manager.portfolio_state.kill_switch_active:
            if self.active_quotes:
                self.cancel_all_quotes()
            return

        # Price jump circuit breaker check from public trades
        asset_id = self.asset_id_yes if tok == "YES" else self.asset_id_no
        jump, delta = self.risk_manager.volatility_monitor.record_price(
            asset_id=asset_id,
            condition_id=self.condition_id,
            mid_price=trade_price,
            timestamp=ts,
        )
        if jump:
            self.risk_manager.trip_circuit_breaker(
                reason=CircuitBreakerReason.VOLATILITY_SPIKE,
                details=f"Price jump {delta:.2f}c exceeded threshold on {tok}",
                condition_id=self.condition_id,
                asset_id=asset_id,
            )
            self.cancel_all_quotes()
            return

        if not self.dry_run:
            # IN LIVE REAL-MONEY MODE:
            # Public market trades between third parties do NOT infer simulated fills on our resting orders!
            # Resting maker quotes are queued in the CLOB order book; fills occur via exchange matching.
            # Synchronize our authentic open orders and fills from the exchange:
            self.sync_live_orders_and_fills(timestamp=ts)
            return

        # IN DRY-RUN / SIMULATED MODE ONLY:
        # Check if our active quote was crossed/filled for testing harness
        for qid, qdata in list(self.active_quotes.items()):
            if qdata.get("token_side") == tok:
                side = qdata.get("side")
                q_px = qdata.get("price", 0.0)
                is_filled = False
                if side == "BUY" and trade_price <= q_px:
                    is_filled = True
                elif side == "SELL" and trade_price >= q_px:
                    is_filled = True

                if is_filled:
                    fill_sz = min(qdata.get("size", 20.0), trade_size)
                    fill_rec = RealMoneyFillRecord(
                        fill_id=str(uuid.uuid4())[:8],
                        timestamp=ts,
                        condition_id=self.condition_id,
                        token_side=tok,
                        side=side,
                        fill_price=q_px,
                        fill_size=fill_sz,
                        is_maker=True,
                        order_id=qdata.get("order_id", ""),
                        notes="Simulated maker fill matched against market trade (Dry-Run)",
                    )
                    self.fills_history.append(fill_rec)
                    self._apply_fill(tok=tok, side=side, fill_sz=fill_sz, fill_px=q_px)
                    self.evaluate_quotes(timestamp=ts)
                    self.flush_ledger()

    # -------------------------------------------------------------------------
    # Ledger Flushing & Telemetry
    # -------------------------------------------------------------------------

    def flush_ledger(self) -> None:
        """Write atomic state snapshot to SQLite and JSON ledger."""
        equity_val = self.get_equity()
        cash_val = self.get_cash()
        pos = self.get_position(self.condition_id)

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
                equity_val - cash_val,
                equity_val,
                equity_val - cash_val,
                pos.realized_pnl,
                equity_val - self.initial_cash,
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
                "total_wallet_balance": getattr(self, "total_wallet_balance", cash_val),
                "budget_pct": getattr(self, "budget_pct", 1.0),
                "trading_bankroll": getattr(self, "trading_bankroll", self.initial_cash),
                "pol_balance": self.pol_balance,
                "equity": equity_val,
                "unrealized_pnl": equity_val - cash_val,
                "realized_pnl": pos.realized_pnl,
                "total_pnl": equity_val - self.initial_cash,
                "total_rewards_credited": self.total_rewards_credited,
                "actual_rewards_usd": self.actual_rewards_usd,
                "actual_rebates_usd": self.actual_rebates_usd,
                "actual_daily_rewards_usd": self.actual_daily_rewards_usd,
                "reward_source": self.reward_source,
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
        total_wb = getattr(self, "total_wallet_balance", cash_val)
        b_pct = getattr(self, "budget_pct", 1.0) * 100.0
        t_bankroll = getattr(self, "trading_bankroll", self.initial_cash)

        return {
            "timestamp": time.time(),
            "condition_id": self.condition_id,
            "mode": "DRY_RUN" if self.dry_run else "REAL_MONEY",
            "equity": round(equity_val, 2),
            "cash": round(cash_val, 2),
            "total_wallet_balance": round(total_wb, 2),
            "budget_pct": round(b_pct, 1),
            "trading_bankroll": round(t_bankroll, 2),
            "pol_balance": round(self.pol_balance, 4),
            "market_value": round(equity_val - cash_val, 2),
            "realized_pnl": round(pos.realized_pnl, 4),
            "unrealized_pnl": round(equity_val - cash_val, 4),
            "total_pnl": round(equity_val - self.initial_cash, 4),
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
            "actual_rewards_usd": round(self.actual_rewards_usd, 4),
            "actual_rebates_usd": round(self.actual_rebates_usd, 4),
            "actual_daily_rewards_usd": round(self.actual_daily_rewards_usd, 4),
            "actual_total_payout_usd": round(self.actual_rewards_usd + self.actual_rebates_usd, 4),
            "reward_source": self.reward_source,
            "verified_scoring_orders": dict(self.verified_scoring_orders),
            "total_volume_usd": round(self.total_volume_traded_usd, 2),
            "epoch_index": self.epoch_index,
            "latest_deskew": asdict(self.latest_deskew_decision) if self.latest_deskew_decision else None,
            "reserve_cash_usd": round(cash_val, 2),
            "reserve_ratio_pct": round(reserve_ratio_pct, 1),
            "resting_queue_value_usd": round(v_queue_total, 4),
            "daily_yield_usd": round(daily_yield, 2),
            "nominal_apr_pct": round(nominal_apr, 1),
            "compounding_apr_pct": round(compounding_apr, 1),
            "migration_count": len(self.migration_history),
        }
