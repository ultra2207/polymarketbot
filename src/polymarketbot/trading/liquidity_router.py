"""Inter-Domain Dynamic Liquidity Router.

Coordinates dynamic capital routing between:
- Domain 1: Continuous High-Frequency Passive Market Maker & Binance Lead-Lag Alpha Engine
- Domain 2: UMA Settlement Discount & Parity Arbitrage Burst Harvester

Domain 1 holds ~85-90% of total bankroll for continuous quoting and yield capture.
When Domain 2 identifies high-conviction UMA settlement proposals with ARR >= hurdle,
the router instantaneously loans unencumbered capital from Domain 1 to Domain 2.
Upon on-chain CTF redemption, principal and profit are immediately swept back
into Domain 1's active quoting pool to compound continuously without cash drag.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from polymarketbot.logs import get_logger
from polymarketbot.trading.domain2.types import DiscountOpportunity

logger = get_logger("liquidity_router")


@dataclass
class LiquidityRouterConfig:
    """Configuration governing inter-domain capital migration and margin limits."""

    min_domain1_reserve_pct: float = 0.20
    """Minimum buffer (20%) reserved for Domain 1 open order margin and quoting buffer."""

    min_settlement_arr_hurdle: float = 150.0
    """Minimum 150% Annualized Return Rate (ARR) required for Domain 2 to request liquidity."""

    max_single_settlement_alloc_pct: float = 0.15
    """Maximum 15% of total portfolio bankroll deployable into a single UMA market."""

    settlement_repatriation_timeout_hours: float = 72.0
    """Maximum expected lockup window before an active settlement loan is flagged as overdue."""

    max_total_settlement_alloc_pct: float = 0.80
    """Maximum aggregate allocation (80%) of portfolio bankroll across all active Domain 2 loans."""


@dataclass
class ActiveSettlementLoan:
    """Record of an active liquidity loan dispatched to Domain 2."""

    condition_id: str
    allocated_usd: float
    timestamp: float
    expected_arr: float
    asset_id: str = ""
    repatriated: bool = False
    returned_usd: float = 0.0
    profit_usd: float = 0.0
    repatriated_timestamp: Optional[float] = None


@runtime_checkable
class CashInventoryManager(Protocol):
    """Interface defining cash and inventory operations for Domain 1."""

    def get_cash(self) -> float:
        """Available free cash balance in USD."""
        ...

    def get_locked_cash(self) -> float:
        """Cash locked in resting orders in USD."""
        ...

    def get_equity(self) -> float:
        """Total mark-to-market equity in USD."""
        ...

    def loan_capital(self, amount: float) -> bool:
        """Debit cash to loan to Domain 2."""
        ...

    def repatriate_capital(self, returned_usd: float, profit_usd: float) -> None:
        """Credit repatriated principal and profit from Domain 2 settlement."""
        ...


class SimpleCashInventoryManager:
    """Concrete reference implementation of CashInventoryManager for Domain 1."""

    def __init__(
        self,
        initial_cash: float = 1000.0,
        locked_cash: float = 0.0,
        inventory_value: float = 0.0,
    ) -> None:
        self.cash: float = float(initial_cash)
        self.locked_cash: float = float(locked_cash)
        self.inventory_value: float = float(inventory_value)
        self.realized_pnl: float = 0.0

    def get_cash(self) -> float:
        return self.cash

    def get_locked_cash(self) -> float:
        return self.locked_cash

    def get_equity(self) -> float:
        return self.cash + self.locked_cash + self.inventory_value

    def loan_capital(self, amount: float) -> bool:
        amt = float(amount)
        if amt <= 0.0 or amt > self.cash:
            return False
        self.cash -= amt
        return True

    def repatriate_capital(self, returned_usd: float, profit_usd: float) -> None:
        self.cash += float(returned_usd) + float(profit_usd)
        self.realized_pnl += float(profit_usd)


class InterDomainLiquidityRouter:
    """Production Liquidity Router coordinating capital between Domain 1 and Domain 2."""

    def __init__(
        self,
        domain1_engine: Optional[Any] = None,
        domain2_harvester: Optional[Any] = None,
        config: Optional[LiquidityRouterConfig] = None,
    ) -> None:
        self.config = config or LiquidityRouterConfig()
        self.domain1_engine = domain1_engine
        self.domain2_harvester = domain2_harvester

        # Active & completed loans
        self._active_loans: Dict[str, ActiveSettlementLoan] = {}
        self._completed_loans: List[ActiveSettlementLoan] = []

        # Lifetime counters
        self.total_rotated_capital: float = 0.0
        self.total_repatriated_capital: float = 0.0
        self.total_profit_earned: float = 0.0
        self.total_loans_count: int = 0
        self.start_timestamp: float = time.time()

        logger.info(
            "InterDomainLiquidityRouter initialized (min_reserve={:.0%}, ARR_hurdle={:.0f}%, max_single_alloc={:.0%})",
            self.config.min_domain1_reserve_pct,
            self.config.min_settlement_arr_hurdle,
            self.config.max_single_settlement_alloc_pct,
        )

    def register_domain1(self, engine: Any) -> None:
        """Register or update Domain 1 trading engine / cash manager."""
        self.domain1_engine = engine
        logger.info("Domain 1 registered with InterDomainLiquidityRouter")

    def register_domain2(self, harvester: Any) -> None:
        """Register or update Domain 2 settlement harvester."""
        self.domain2_harvester = harvester
        logger.info("Domain 2 registered with InterDomainLiquidityRouter")

    def compute_opportunity_arr(self, opportunity: DiscountOpportunity) -> float:
        """Compute the Annualized Return Rate (ARR) in percent for an evaluated discount opportunity."""
        # Check if opportunity already carries an explicit expected_arr attribute
        if hasattr(opportunity, "expected_arr") and getattr(opportunity, "expected_arr") is not None:
            arr_val = float(getattr(opportunity, "expected_arr"))
            # Normalize decimal ratio to percent if <= 10.0
            if 0.0 < arr_val <= 10.0:
                arr_val *= 100.0
            return arr_val

        hours = max(0.01, float(opportunity.hours_to_liveness_expiry))
        price = opportunity.recommended_bid_price if opportunity.recommended_bid_price > 0 else opportunity.best_bid

        # If price is unavailable or 0, fallback to discount_bps
        if price <= 0.0:
            if hasattr(opportunity, "discount_bps") and opportunity.discount_bps > 0:
                return (opportunity.discount_bps / 10000.0) * (8760.0 / hours) * 100.0
            return 0.0

        proposed = opportunity.proposed_price if opportunity.proposed_price > 0 else 1.0
        margin = (proposed - price) / price
        if margin <= 0.0:
            return 0.0

        arr_pct = margin * (8760.0 / hours) * 100.0
        return arr_pct

    def _get_domain1_equity(self) -> float:
        """Calculate total mark-to-market equity in Domain 1."""
        if self.domain1_engine is None:
            return 0.0
        if hasattr(self.domain1_engine, "get_equity"):
            try:
                return float(self.domain1_engine.get_equity())
            except Exception as e:
                logger.debug("Failed calling get_equity() on domain1: {}", e)
        if hasattr(self.domain1_engine, "cash"):
            return float(self.domain1_engine.cash)
        return 0.0

    def _get_domain1_free_cash(self) -> float:
        """Get Domain 1 unencumbered free cash balance (excluding open order margin)."""
        if self.domain1_engine is None:
            return 0.0
        if hasattr(self.domain1_engine, "get_cash"):
            try:
                return float(self.domain1_engine.get_cash())
            except Exception as e:
                logger.debug("Failed calling get_cash() on domain1: {}", e)
        if hasattr(self.domain1_engine, "cash"):
            return float(self.domain1_engine.cash)
        return 0.0

    def _get_domain2_equity(self) -> float:
        """Calculate total mark-to-market equity and inventory in Domain 2."""
        if self.domain2_harvester is not None:
            if hasattr(self.domain2_harvester, "get_domain2_local_equity"):
                try:
                    return float(self.domain2_harvester.get_domain2_local_equity())
                except Exception as e:
                    logger.debug("Failed calling get_domain2_local_equity() on domain2: {}", e)
            if hasattr(self.domain2_harvester, "available_cash"):
                cash = float(self.domain2_harvester.available_cash)
                pos_val = 0.0
                if hasattr(self.domain2_harvester, "_positions"):
                    pos_val = sum(
                        p.shares * p.entry_price
                        for p in self.domain2_harvester._positions.values()
                        if not p.is_redeemed
                    )
                return cash + pos_val
        # Standalone or test mode: active loans count as Domain 2 asset
        return sum(l.allocated_usd for l in self._active_loans.values() if not l.repatriated)

    def get_total_portfolio_equity(self) -> float:
        """Sum of Domain 1 cash + open positions + Domain 2 cash + locked settlement inventory."""
        d1_eq = self._get_domain1_equity()
        d2_eq = self._get_domain2_equity()

        # If domain2 is attached, d2_eq includes both available cash and active positions.
        # If domain2 is not attached, d2_eq counts the active loan principal as receivables.
        total_equity = d1_eq + d2_eq
        return round(total_equity, 4)

    def request_settlement_capital(
        self,
        opportunity: DiscountOpportunity,
        requested_usd: float,
    ) -> float:
        """Evaluate and grant an instantaneous liquidity loan to Domain 2 for a settlement discount.

        Checks:
        1. ARR qualification: must satisfy min_settlement_arr_hurdle (e.g. >= 150% ARR).
        2. Single-bet cap: single market allocation <= max_single_settlement_alloc_pct of portfolio bankroll.
        3. Domain 1 reserve: Domain 1 unencumbered cash must remain above min_domain1_reserve_pct buffer.
        4. Aggregate cap: total active settlement loans <= max_total_settlement_alloc_pct.

        Returns:
            Approved allocation in USD (0.0 if rejected).
        """
        cid = str(opportunity.condition_id).lower()
        req = float(requested_usd)
        if req <= 0.0:
            return 0.0

        # 1. ARR Hurdle Check
        arr = self.compute_opportunity_arr(opportunity)
        if arr < self.config.min_settlement_arr_hurdle:
            logger.info(
                "Capital request rejected for {}: ARR {:.1f}% < hurdle {:.1f}%",
                cid[:14], arr, self.config.min_settlement_arr_hurdle,
            )
            return 0.0

        # 2. Portfolio Equity & Sizing Caps
        total_equity = self.get_total_portfolio_equity()
        if total_equity <= 0.0:
            logger.warning("Capital request rejected: total portfolio equity is <= 0")
            return 0.0

        # Max single settlement market cap
        max_single_alloc = total_equity * self.config.max_single_settlement_alloc_pct
        existing_loan = self._active_loans[cid].allocated_usd if cid in self._active_loans else 0.0
        remaining_market_room = max(0.0, max_single_alloc - existing_loan)
        if remaining_market_room <= 0.0:
            logger.info(
                "Capital request rejected for {}: single market cap ${:.2f} reached (existing: ${:.2f})",
                cid[:14], max_single_alloc, existing_loan,
            )
            return 0.0

        # 3. Domain 1 Unencumbered Cash & Reserve Buffer
        d1_free_cash = self._get_domain1_free_cash()
        d1_reserve_buffer = total_equity * self.config.min_domain1_reserve_pct
        unencumbered_cash = max(0.0, d1_free_cash - d1_reserve_buffer)
        if unencumbered_cash <= 0.0:
            logger.info(
                "Capital request rejected for {}: Domain 1 unencumbered cash is $0.00 (free cash ${:.2f} <= reserve buffer ${:.2f})",
                cid[:14], d1_free_cash, d1_reserve_buffer,
            )
            return 0.0

        # 4. Aggregate Settlement Cap Check
        total_active_loans = sum(l.allocated_usd for l in self._active_loans.values())
        max_aggregate_alloc = total_equity * self.config.max_total_settlement_alloc_pct
        remaining_aggregate_room = max(0.0, max_aggregate_alloc - total_active_loans)
        if remaining_aggregate_room <= 0.0:
            logger.info(
                "Capital request rejected for {}: aggregate settlement cap ${:.2f} reached",
                cid[:14], max_aggregate_alloc,
            )
            return 0.0

        # 5. Determine Approved Allocation Size
        approved = min(req, remaining_market_room, unencumbered_cash, remaining_aggregate_room)
        approved = round(approved, 4)
        if approved <= 0.0:
            return 0.0

        # 6. Execute Transfer: Debit Domain 1
        transfer_success = False
        if self.domain1_engine is not None:
            if hasattr(self.domain1_engine, "loan_capital"):
                transfer_success = bool(self.domain1_engine.loan_capital(approved))
            elif hasattr(self.domain1_engine, "cash") and self.domain1_engine.cash >= approved:
                self.domain1_engine.cash -= approved
                if hasattr(self.domain1_engine, "risk_manager") and hasattr(self.domain1_engine.risk_manager, "portfolio_state"):
                    self.domain1_engine.risk_manager.portfolio_state.current_cash_usd = self.domain1_engine.cash
                transfer_success = True
        else:
            # Standalone router operation without attached engine
            transfer_success = True

        if not transfer_success:
            logger.warning("Domain 1 failed to execute loan transfer of ${:.2f}", approved)
            return 0.0

        # 7. Credit Domain 2
        if self.domain2_harvester is not None:
            if hasattr(self.domain2_harvester, "receive_routed_capital"):
                self.domain2_harvester.receive_routed_capital(approved)
            elif hasattr(self.domain2_harvester, "available_cash"):
                self.domain2_harvester.available_cash += approved

        # 8. Record Active Loan
        if cid in self._active_loans:
            self._active_loans[cid].allocated_usd += approved
        else:
            self._active_loans[cid] = ActiveSettlementLoan(
                condition_id=cid,
                allocated_usd=approved,
                timestamp=time.time(),
                expected_arr=arr,
                asset_id=getattr(opportunity, "winning_asset_id", ""),
            )

        self.total_rotated_capital += approved
        self.total_loans_count += 1

        logger.info(
            "Approved loan for {}: ${:.2f} USDC (ARR: {:.1f}%, Single Cap: ${:.2f}, Reserve Buffer: ${:.2f})",
            cid[:14], approved, arr, max_single_alloc, d1_reserve_buffer,
        )
        return approved

    def release_settlement_capital(
        self,
        condition_id: str,
        returned_usd: float,
        profit_usd: float,
    ) -> None:
        """Called when Domain 2 redeems via CTF; immediately sweeps principal + profit back to Domain 1.

        Ensures capital immediately compounds inside Domain 1's active quoting pool without cash drag.
        """
        cid = str(condition_id).lower()
        now = time.time()
        ret = float(returned_usd)
        prof = float(profit_usd)

        loan = self._active_loans.pop(cid, None)
        if loan is not None:
            # Handle both conventions and partial fills:
            # Convention A: returned_usd is total payout proceeds (principal + profit)
            if abs(ret - (loan.allocated_usd + prof)) < 1e-4:
                total_credit = ret
                principal = ret - prof
                unspent = 0.0
            elif abs(ret - loan.allocated_usd) < 1e-4:
                # Full loan principal returned
                total_credit = ret + prof
                principal = ret
                unspent = 0.0
            else:
                # Partial fill redemption: returned_usd is the redeemed position cost basis
                # Any unspent portion of the allocated loan held in Domain 2 is swept back
                unspent = max(0.0, loan.allocated_usd - ret)
                if self.domain2_harvester is not None and hasattr(self.domain2_harvester, "available_cash"):
                    unspent = min(float(self.domain2_harvester.available_cash), unspent)
                total_credit = ret + prof + unspent
                principal = ret + unspent

            loan.repatriated = True
            loan.returned_usd = principal
            loan.profit_usd = prof
            loan.repatriated_timestamp = now
            self._completed_loans.append(loan)
        else:
            total_credit = ret + prof
            principal = ret
            unspent = 0.0

        # Credit Domain 1
        if self.domain1_engine is not None:
            if hasattr(self.domain1_engine, "repatriate_capital"):
                self.domain1_engine.repatriate_capital(principal, prof)
            elif hasattr(self.domain1_engine, "cash"):
                self.domain1_engine.cash += total_credit
                if hasattr(self.domain1_engine, "risk_manager") and hasattr(self.domain1_engine.risk_manager, "portfolio_state"):
                    self.domain1_engine.risk_manager.portfolio_state.current_cash_usd = self.domain1_engine.cash
                if hasattr(self.domain1_engine, "realized_pnl"):
                    self.domain1_engine.realized_pnl += prof

        # Debit Domain 2 (if available_cash holds the redeemed funds)
        if self.domain2_harvester is not None:
            if hasattr(self.domain2_harvester, "repatriate_routed_capital"):
                self.domain2_harvester.repatriate_routed_capital(total_credit)
            elif hasattr(self.domain2_harvester, "available_cash"):
                self.domain2_harvester.available_cash = max(
                    0.0, self.domain2_harvester.available_cash - total_credit
                )

        # Update telemetry counters
        self.total_repatriated_capital += principal
        self.total_profit_earned += prof

        logger.info(
            "Repatriated settlement capital for {}: principal ${:.2f} + profit ${:.2f} -> Domain 1 quoting pool (${:.2f} total)",
            cid[:14], principal, prof, total_credit,
        )

    def get_timed_out_loans(self) -> List[ActiveSettlementLoan]:
        """Return list of active loans exceeding settlement_repatriation_timeout_hours."""
        now = time.time()
        timeout_seconds = self.config.settlement_repatriation_timeout_hours * 3600.0
        return [
            loan
            for loan in self._active_loans.values()
            if (now - loan.timestamp) > timeout_seconds
        ]

    def get_telemetry(self) -> Dict[str, Any]:
        """Report allocation ratio, active loans, lifetime capital rotated, and incremental ARR boost."""
        total_equity = self.get_total_portfolio_equity()
        d1_equity = self._get_domain1_equity()
        d2_equity = self._get_domain2_equity()

        d1_pct = round(d1_equity / max(1e-6, total_equity), 4)
        d2_pct = round(d2_equity / max(1e-6, total_equity), 4)

        now = time.time()
        active_loans_usd = sum(l.allocated_usd for l in self._active_loans.values())
        active_loans_list = [
            {
                "condition_id": l.condition_id,
                "allocated_usd": round(l.allocated_usd, 2),
                "expected_arr": round(l.expected_arr, 2),
                "age_hours": round((now - l.timestamp) / 3600.0, 2),
                "is_timed_out": ((now - l.timestamp) / 3600.0) > self.config.settlement_repatriation_timeout_hours,
            }
            for l in self._active_loans.values()
        ]

        # Calculate incremental ARR boost from dynamic routing
        if self._completed_loans:
            total_loan_volume = sum(l.allocated_usd for l in self._completed_loans)
            weighted_arr = (
                sum(l.expected_arr * l.allocated_usd for l in self._completed_loans)
                / max(1.0, total_loan_volume)
            )
            # Duration-weighted profit return
            durations = [
                max(0.01, (l.repatriated_timestamp - l.timestamp) / 3600.0)
                for l in self._completed_loans
                if l.repatriated_timestamp is not None
            ]
            avg_duration_hours = sum(durations) / max(1, len(durations)) if durations else 24.0
            incremental_arr_boost = (
                (self.total_profit_earned / max(1.0, total_equity))
                * (8760.0 / avg_duration_hours)
                * 100.0
            )
        elif self._active_loans:
            total_loan_volume = sum(l.allocated_usd for l in self._active_loans.values())
            weighted_arr = (
                sum(l.expected_arr * l.allocated_usd for l in self._active_loans.values())
                / max(1.0, total_loan_volume)
            )
            incremental_arr_boost = weighted_arr * (total_loan_volume / max(1.0, total_equity))
        else:
            weighted_arr = 0.0
            incremental_arr_boost = 0.0

        return {
            "total_portfolio_equity": round(total_equity, 2),
            "domain1_equity": round(d1_equity, 2),
            "domain2_equity": round(d2_equity, 2),
            "allocation_ratio": {
                "domain1_pct": d1_pct,
                "domain2_pct": d2_pct,
            },
            "domain1_unencumbered_cash": round(
                max(0.0, self._get_domain1_free_cash() - (total_equity * self.config.min_domain1_reserve_pct)), 2
            ),
            "domain1_reserve_buffer_usd": round(total_equity * self.config.min_domain1_reserve_pct, 2),
            "active_loans_count": len(self._active_loans),
            "active_loans_usd": round(active_loans_usd, 2),
            "active_loans": active_loans_list,
            "timed_out_loans_count": len(self.get_timed_out_loans()),
            "lifetime_capital_rotated_usd": round(self.total_rotated_capital, 2),
            "lifetime_capital_repatriated_usd": round(self.total_repatriated_capital, 2),
            "lifetime_profit_usd": round(self.total_profit_earned, 2),
            "lifetime_loans_count": self.total_loans_count,
            "weighted_settlement_arr_pct": round(weighted_arr, 2),
            "incremental_arr_boost_pct": round(incremental_arr_boost, 2),
        }
