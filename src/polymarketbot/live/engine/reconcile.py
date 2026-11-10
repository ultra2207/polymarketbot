"""Startup reconciliation and initial equity baseline."""

from __future__ import annotations

from typing import Any

from polymarketbot.dataapi import DataAPIClient
from polymarketbot.logs import get_logger
from polymarketbot.strategy.onchain.settlement import SettlementRoute

logger = get_logger("real_money_engine")


class ReconcileMixin:
    """Venue/chain reconciliation at startup."""

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
