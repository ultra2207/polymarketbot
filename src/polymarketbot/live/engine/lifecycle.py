"""Engine lifecycle: cancel-all, halt/resume, quote sizing and shutdown."""

from __future__ import annotations

import sqlite3
import time

from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob import ClobAPIError

logger = get_logger("real_money_engine")


class LifecycleMixin:
    """Trading and process lifecycle."""

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
                    # The queue record must die with the order, or a later
                    # re-place at the same price inherits a cancelled order's
                    # queue position.
                    side = str(qdata.get("token_side", "")).upper()
                    self.micro_risk.on_cancel(
                        self.asset_id_yes if side == "YES" else self.asset_id_no,
                        order_id,
                    )
            self.locked_cash = self._compute_locked_cash()
            confirmed = not remaining_ids
            if confirmed:
                logger.info("All resting quotes confirmed cancelled by the venue")
            else:
                logger.warning(
                    f"Venue still lists {len(remaining_ids)} order(s) after cancel-all: {sorted(remaining_ids)}"
                )
            return confirmed

    def evaluate_quotes(self, timestamp: float | None = None) -> None:
        """Serialize quoting against fill/reconcile threads (RLock, re-entrant).

        Two refusals happen before any quote is priced:

        * the book snapshot this tick would quote from is older than the
          engine's own reprice interval (the mark has already moved);
        * the quote distance is unmeasured, so there is no defensible price.
        """
        with self._lock:
            ts = timestamp if timestamp is not None else time.time()
            if self.spread_offset_cents is None:
                logger.warning(
                    "Quoting skipped: spread_offset_cents is unmeasured "
                    f"(condition {self.condition_id[:12]})"
                )
                return
            age = self.quote_book_age_seconds(ts)
            if age is None or age > self.min_reprice_interval_s:
                logger.warning(
                    f"Quoting skipped for {self.condition_id[:12]}: book snapshot "
                    f"{'missing' if age is None else f'{age:.2f}s old'} exceeds the "
                    f"measured reprice interval ({self.min_reprice_interval_s:.2f}s)"
                )
                return
            super().evaluate_quotes(ts)

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

    def update_quote_size(self, new_quote_size: float, timestamp: float | None = None) -> bool:
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
        except Exception as e:  # noqa: BLE001 - shutdown must reach quote cancellation
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
        except Exception as e:  # noqa: BLE001 - shutdown must still close persistence
            logger.error(f"Shutdown: quote cancellation failed: {e}")
        try:
            self.flush_wal()
        except sqlite3.Error as e:
            logger.warning(f"Shutdown: WAL checkpoint failed: {e}")
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error as e:
                logger.debug(f"Shutdown: SQLite close failed: {e}")
            self._conn = None
