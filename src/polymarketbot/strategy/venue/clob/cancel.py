"""Resting order cancellation."""

from __future__ import annotations

import json
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob.common import ClobAPIError

logger = get_logger("clob_client")


class CancelMixin:
    """Single and bulk order cancellation."""


    @staticmethod
    def _cancel_confirmed(order_id: str, body: Any) -> bool:
        """True only when the venue's cancellation result names this order.

        ``DELETE /order`` answers ``200 OK`` with ``{"canceled": [...],
        "not_canceled": {id: reason}}``; a 2xx alone does not mean the order is
        gone (``order already matched`` is the common case). An absent or
        unparseable result is *not* a confirmation.
        """
        if not isinstance(body, dict):
            logger.warning(f"Cancel of {order_id} returned no cancellation result: {body!r}")
            return False
        canceled = body.get("canceled")
        if isinstance(canceled, (list, tuple)):
            ids = {str(item) for item in canceled}
            if order_id in ids:
                return True
        not_canceled = body.get("not_canceled")
        if isinstance(not_canceled, dict):
            reason = not_canceled.get(order_id)
            if reason is not None:
                logger.warning(f"Cancel of {order_id} was not applied: {reason}")
                return False
        logger.warning(f"Cancel result for {order_id} did not confirm the order: {body!r}")
        return False

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an individual resting order by order ID.

        Returns True only when the venue confirms the cancellation in its
        ``canceled`` list. False on an empty id, a transport error, a non-2xx,
        or a 2xx whose body reports the order as not canceled.
        """
        if not order_id:
            return False

        if self.dry_run:
            logger.info(f"[DRY-RUN] Simulated CLOB DELETE /order | Cancelled order {order_id}")
            if self.self_trade_guard is not None:
                self.self_trade_guard.unregister_maker_order(order_id)
            self._mock_orders.pop(order_id, None)
            return True

        path = "/order"
        payload = json.dumps({"orderID": order_id})
        try:
            body = self._request("DELETE", path, body=payload)
        except ClobAPIError as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False
        confirmed = self._cancel_confirmed(order_id, body)

        if confirmed and self.self_trade_guard is not None:
            self.self_trade_guard.unregister_maker_order(order_id)
        return confirmed

    def cancel_all_orders(self) -> bool:
        """Cancel all resting orders for the authenticated account.

        Returns False on any failure (transport error or non-2xx). The wash-trade
        guard is cleared only after the venue confirms, so a failed cancel-all
        cannot hide resting orders from it.
        """
        if self.dry_run:
            count = len(self._mock_orders)
            self._mock_orders.clear()
            if self.self_trade_guard is not None:
                self.self_trade_guard.clear()
            logger.info(f"[DRY-RUN] Simulated CLOB DELETE /cancel-all | Cleared {count} resting orders.")
            return True

        try:
            self._request("DELETE", "/cancel-all")
        except ClobAPIError as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return False
        confirmed = True

        if confirmed and self.self_trade_guard is not None:
            self.self_trade_guard.clear()
        return confirmed
