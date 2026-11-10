"""Cash, collateral, inventory and equity accounting hooks."""

from __future__ import annotations

import time
from typing import Any, Optional, Tuple

from polymarketbot.live.base import PositionView
from polymarketbot.logs import get_logger

logger = get_logger("real_money_engine")


class AccountingMixin:
    """Cash, inventory and equity hooks."""

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
        if bid is None or ask is None or not (0.0 < bid < 1.0 and 0.0 < ask < 1.0):
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

    def loan_capital(self, amount: float) -> bool:
        """Loan unencumbered capital to Domain 2 via InterDomainLiquidityRouter."""
        amt = float(amount)
        if amt <= 0.0 or amt > self.cash:
            return False
        self.cash -= amt
        if hasattr(self, "risk_manager") and hasattr(self.risk_manager, "portfolio_state"):
            self.risk_manager.portfolio_state.current_cash_usd = self.cash
        logger.info(
            "RealMoneyTradingEngine loaned ${:.2f} to Domain 2; cash remaining: ${:.2f}",
            amt, self.cash,
        )
        return True

    def repatriate_capital(self, returned_usd: float, profit_usd: float) -> None:
        """Receive repatriated principal and profit from Domain 2 settlement."""
        ret = float(returned_usd)
        prof = float(profit_usd)
        total = ret + prof
        self.cash += total
        self.realized_pnl += prof
        if hasattr(self, "risk_manager") and hasattr(self.risk_manager, "portfolio_state"):
            self.risk_manager.portfolio_state.current_cash_usd = self.cash
        logger.info(
            "RealMoneyTradingEngine repatriated ${:.2f} (${:.2f} principal + ${:.2f} profit); cash now: ${:.2f}",
            total, ret, prof, self.cash,
        )

    def get_unencumbered_cash(self, min_reserve_pct: float = 0.20) -> float:
        """Get unencumbered cash available for inter-domain lending above reserve buffer."""
        free_cash = self.get_cash()
        total_eq = self.get_equity()
        reserve_buffer = total_eq * float(min_reserve_pct)
        return max(0.0, free_cash - reserve_buffer)

    def get_best_bid_ask(
        self, token_side: str
    ) -> Tuple[Optional[float], Optional[float]]:
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
        # No fabricated price: quote callers must skip when either side is missing.
        return self._best_bids.get(tok), self._best_asks.get(tok)

    def _accounting_snapshot(self) -> Tuple[float, float, float]:
        """Free cash, locked collateral and inventory value from one locked pass.

        ``get_cash()`` already recomputes locked collateral, so reading equity
        through it as well would sum the same resting BUY notional a second
        time. Callers that need all three take them here instead.
        """
        cash_val = self.get_cash()
        locked_val = self.locked_cash
        return cash_val, locked_val, self._inventory_value()
