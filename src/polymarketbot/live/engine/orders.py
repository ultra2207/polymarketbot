"""Exchange order-field parsing and open-order reconciliation."""

from __future__ import annotations

import time
from typing import Any

from polymarketbot.live.engine.markout import RealMoneyFillRecord
from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob import ClobAPIError

logger = get_logger("real_money_engine")


class OrderSyncMixin:
    """Reconcile resting orders strictly against exchange facts."""

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
    def _order_matched_size(ord_info: Any) -> float | None:
        """Matched size from the order record, or ``None`` when it is unreadable.

        ``None`` is not zero: an unparseable or absent field used to become 0.0,
        which made the fill delta ``max(0, 0 - already)`` — the fill vanished,
        and a terminal status then dropped the order from tracking.
        """
        if not isinstance(ord_info, dict):
            return None
        for key in ("size_matched", "sizeMatched", "matched_size"):
            value = ord_info.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _order_original_size(ord_info: Any) -> float | None:
        """Original order size, or ``None`` when the record does not carry one."""
        if not isinstance(ord_info, dict):
            return None
        for key in ("original_size", "originalSize", "size", "original_amount"):
            value = ord_info.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _order_price(ord_info: Any) -> float | None:
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

    def sync_live_orders_and_fills(self, timestamp: float | None = None) -> list[RealMoneyFillRecord]:
        """Reconcile resting orders and fills strictly against confirmed exchange facts.

        A failed open-order query changes nothing. An order absent from the open
        list is never assumed filled: its status and final ``size_matched`` are
        read with ``get_order`` and only that delta is booked, at the exchange's
        own price. Thread-safe for the feed thread.
        """
        ts = timestamp or time.time()
        new_fills: list[RealMoneyFillRecord] = []
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
                    if current_matched is None:
                        logger.warning(
                            f"Order {order_id} carries an unreadable size_matched "
                            f"({live_ord.get('size_matched')!r}); keeping it tracked and "
                            "booking no fill until the venue reports a usable figure"
                        )
                        continue
                    status = self._order_status(live_ord)
                    exchange_price = self._order_price(live_ord)
                    if exchange_price is None:
                        exchange_price = quote_price
                    original_size = self._order_original_size(live_ord)
                    if original_size is None or original_size <= 0.0:
                        original_size = quote_size
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
                        self.mark_quote_row(
                            qid,
                            "FILLED" if current_matched >= original_size - 1e-9 else "CLOSED",
                        )
                        self.active_quotes.pop(qid, None)
                        side = str(qdata.get("token_side", "")).upper()
                        self.micro_risk.on_cancel(
                            self.asset_id_yes if side == "YES" else self.asset_id_no,
                            order_id,
                        )
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
                if current_matched is None:
                    logger.warning(
                        f"Resolved order {order_id} carries an unreadable size_matched "
                        f"({ord_info.get('size_matched')!r}); keeping it tracked and "
                        "booking no fill until the venue reports a usable figure"
                    )
                    continue
                status = self._order_status(ord_info)
                exchange_price = self._order_price(ord_info)
                if exchange_price is None:
                    exchange_price = quote_price
                original_size = self._order_original_size(ord_info)
                if original_size is None or original_size <= 0.0:
                    original_size = quote_size
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
                    self.mark_quote_row(
                        qid,
                        "FILLED" if current_matched >= original_size - 1e-9 else "CLOSED",
                    )
                    self.active_quotes.pop(qid, None)
                    side = str(qdata.get("token_side", "")).upper()
                    self.micro_risk.on_cancel(
                        self.asset_id_yes if side == "YES" else self.asset_id_no,
                        order_id,
                    )

            self.locked_cash = self._compute_locked_cash()
            if new_fills:
                self.flush_ledger()

        # A fill that completed a set asked for a merge while this method held
        # the lock; run it now, outside the critical section.
        if new_fills:
            try:
                self.run_pending_ctf_merge()
            except Exception as exc:  # noqa: BLE001 - a merge failure must not lose the fill
                logger.error(f"Deferred CTF merge after a fill failed: {exc}")

        return new_fills
