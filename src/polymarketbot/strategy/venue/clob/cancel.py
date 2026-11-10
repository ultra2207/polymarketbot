"""Resting order cancellation."""

from __future__ import annotations

import json

from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob.common import ClobAPIError

logger = get_logger("clob_client")


class CancelMixin:
    """Single and bulk order cancellation."""


    def cancel_order(self, order_id: str) -> bool:
        """Cancel an individual resting order by order ID.

        Returns False on any failure (empty id, transport error, non-2xx).
        """
        if not order_id:
            return False

        if self.self_trade_guard is not None:
            self.self_trade_guard.unregister_maker_order(order_id)

        if self.dry_run:
            logger.info(f"[DRY-RUN] Simulated CLOB DELETE /order | Cancelled order {order_id}")
            self._mock_orders.pop(order_id, None)
            return True

        if self.hft_pool is not None:
            res = self.hft_pool.cancel_order(order_id)
            return res.success

        path = "/order"
        payload = json.dumps({"orderID": order_id})
        if self._fast_cancel_pool is not None:
            headers = self._get_auth_headers("DELETE", path, body=payload)
            res = self._fast_cancel_pool.cancel_order(order_id, headers=headers)
            return res.success

        try:
            self._request("DELETE", path, body=payload)
            return True
        except ClobAPIError as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def cancel_all_orders(self) -> bool:
        """Cancel all resting orders for the authenticated account.

        Returns False on any failure (transport error or non-2xx).
        """
        if self.self_trade_guard is not None:
            self.self_trade_guard.clear()

        if self.dry_run:
            count = len(self._mock_orders)
            self._mock_orders.clear()
            logger.info(f"[DRY-RUN] Simulated CLOB DELETE /cancel-all | Cleared {count} resting orders.")
            return True

        if self.hft_pool is not None:
            res = self.hft_pool.cancel_all()
            return res.success

        try:
            self._request("DELETE", "/cancel-all")
            return True
        except ClobAPIError as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return False
