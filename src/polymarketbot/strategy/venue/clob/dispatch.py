"""Live order dispatch over REST or the HFT connection pool."""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob.common import ClobAPIError, OrderPlacementResult
from polymarketbot.strategy.venue.clob.signing import _PreparedOrder, _SignedOrder

logger = get_logger("clob_client")


def _is_order_version_mismatch(text: str) -> bool:
    """True when the CLOB rejected an order because its signed version is stale."""
    return "order_version_mismatch" in str(text).lower()


# Documented order-time HTTP statuses (docs.polymarket.com/resources/error-codes):
# 425 = matching engine restarting, 429 = throttled, 503 = cancel-only/post-only
# mode. None of them is a permanent rejection of the order.
_RESTART_STATUSES = (425, 429, 503)
_RESTART_LABELS = {425: "RESTARTING", 429: "THROTTLED", 503: "RESTRICTED"}


def _retry_after_seconds(err: ClobAPIError) -> float | None:
    """Server-supplied retry delay from an order rejection, or None.

    The venue publishes it as ``retry_after_seconds`` in the body and as the
    ``Retry-After`` header. Only a value the server actually sent is used; no
    delay is invented when the response carries none.
    """
    text = str(getattr(err, "response_text", "") or "")
    if text:
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            for key in ("retry_after_seconds", "retryAfterSeconds"):
                value = payload.get(key)
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and float(value) >= 0.0
                ):
                    return float(value)

    match = re.search(r'retry_after_seconds"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)', text)
    if match:
        return float(match.group(1))
    return None


def _order_response_outcome(data: Any) -> tuple[str, str]:
    """Classify one ``POST /order`` body as ``("ok", order_id)`` or ``("rejected", reason)``.

    A ``200`` body is not a placement. Polymarket answers per-order rejections
    (post-only mode, batch items) with ``success: true`` and an empty
    ``orderID`` plus an ``errorMsg``, so the *order id* is the only proof that
    an order exists. A body without an explicit ``success`` is treated as a
    rejection rather than assumed successful.
    """
    success = data.get("success")
    order_id = str(data.get("orderID") or data.get("id") or "")
    error = data.get("errorMsg") or data.get("error") or ""
    if order_id and error:
        return "rejected", f"{error} (order id {order_id})"
    if order_id and success is not False:
        return "ok", order_id
    return "rejected", str(error or f"venue returned no order id (success={success!r})")



class DispatchMixin:
    """Single-order dispatch with one order-version retry."""


    def _dispatch_order(
        self,
        prepared: _PreparedOrder,
        signed: _SignedOrder,
        *,
        fee_rate_bps: int,
        expiration: int,
        order_type: str,
        metadata: str,
        builder_code: str,
        post_only: bool,
    ) -> OrderPlacementResult:
        """Send the signed order: dry-run mock, HFT pool, or REST with one retry."""
        token_str = prepared.token_id
        side = prepared.side
        side_norm = prepared.side
        price = prepared.price
        size = prepared.size
        market_tick = prepared.tick_size
        neg_risk_flag = prepared.neg_risk
        rounded_price = prepared.rounded_price
        rounded_size = prepared.rounded_size
        order_version = signed.order_version
        sig = signed.signature
        payload = signed.payload

        if self.dry_run:
            mock_id = f"mock_ord_{uuid.uuid4().hex[:12]}"
            logger.info(
                f"[DRY-RUN] Simulated CLOB POST /order | ID: {mock_id} | {side_norm} {rounded_size:.2f} @ ${rounded_price:.3f} | "
                f"v{order_version} | Sig: {sig[:14]}..."
            )
            self._mock_orders[mock_id] = {
                "orderID": mock_id,
                "id": mock_id,
                "token_id": token_str,
                "side": side_norm,
                "price": rounded_price,
                "size": rounded_size,
                "timestamp": time.time(),
                "status": "LIVE",
                "order_version": order_version,
            }
            if self.self_trade_guard is not None:
                self.self_trade_guard.register_maker_order(
                    order_id=mock_id,
                    token_id=token_str,
                    side=side_norm,
                    price=rounded_price,
                    size=rounded_size,
                )
            return OrderPlacementResult(
                success=True,
                order_id=mock_id,
                status="LIVE",
                side=side_norm,
                price=rounded_price,
                size=rounded_size,
                raw_response={"orderID": mock_id, "status": "LIVE", "mock": True, "order_version": order_version},
                signature=sig,
                is_dry_run=True,
            )

        # -------------------------------------------------------------
        # Live Placement via HFT Connection Pool
        # -------------------------------------------------------------
        restricted_until = float(getattr(self, "_order_restricted_until", 0.0) or 0.0)
        if restricted_until > time.time():
            remaining = restricted_until - time.time()
            logger.warning(
                f"CLOB order suppressed: the venue is restarting/throttling "
                f"({remaining:.0f}s left of its own retry delay)"
            )
            return self._placement_failure(
                "RESTRICTED",
                f"venue restriction in force for another {remaining:.0f}s",
                side,
                price,
                size,
                signature=sig,
            )

        path = "/order"
        retried_version = False
        while True:
            body_json = json.dumps(payload)
            try:
                data = self._request("POST", path, body=body_json)
            except ClobAPIError as e:
                if not retried_version and _is_order_version_mismatch(str(e)):
                    retried_version = True
                    refreshed = self._retry_with_refreshed_version(
                        token_id=token_str,
                        side=side_norm,
                        price=rounded_price,
                        size=rounded_size,
                        tick_size=market_tick,
                        neg_risk=neg_risk_flag,
                        fee_rate_bps=int(fee_rate_bps),
                        expiration=int(expiration),
                        order_type=order_type,
                        metadata=metadata,
                        builder_code=builder_code,
                        post_only=post_only,
                    )
                    if isinstance(refreshed, OrderPlacementResult):
                        return refreshed
                    order_version, _fast_order, payload = refreshed
                    sig = payload.get("signature") or payload.get("order", {}).get("signature", "")
                    continue
                status = "NETWORK_ERROR" if e.status_code == 0 else "REJECTED"
                if e.status_code in _RESTART_STATUSES:
                    # Restart / throttle / restricted mode, not a rejection of the
                    # order. Honour the server's own retry delay so the next ticks
                    # do not hammer a restarting engine; never sleep the tick.
                    delay = _retry_after_seconds(e)
                    status = _RESTART_LABELS.get(e.status_code, "RESTRICTED")
                    if delay is not None:
                        self._order_restricted_until = max(
                            float(getattr(self, "_order_restricted_until", 0.0) or 0.0),
                            time.time() + delay,
                        )
                    logger.warning(
                        f"CLOB order not accepted ({status}): {e}"
                        + (f"; venue asked for {delay:.1f}s" if delay is not None else "")
                    )
                else:
                    logger.error(f"CLOB order placement failed: {e}")
                return self._placement_failure(status, str(e), side, price, size, signature=sig)

            if not isinstance(data, dict):
                return self._placement_failure(
                    "REJECTED",
                    f"POST /order returned an unexpected body: {data!r}",
                    side,
                    price,
                    size,
                    signature=sig,
                )

            outcome, detail = _order_response_outcome(data)
            if outcome != "ok":
                err = detail or "order not placed"
                if not retried_version and _is_order_version_mismatch(f"{err} {data}"):
                    retried_version = True
                    refreshed = self._retry_with_refreshed_version(
                        token_id=token_str,
                        side=side_norm,
                        price=rounded_price,
                        size=rounded_size,
                        tick_size=market_tick,
                        neg_risk=neg_risk_flag,
                        fee_rate_bps=int(fee_rate_bps),
                        expiration=int(expiration),
                        order_type=order_type,
                        metadata=metadata,
                        builder_code=builder_code,
                        post_only=post_only,
                    )
                    if isinstance(refreshed, OrderPlacementResult):
                        return refreshed
                    order_version, _fast_order, payload = refreshed
                    sig = payload.get("signature") or payload.get("order", {}).get("signature", "")
                    continue
                logger.error(f"CLOB order placement rejected: {err}")
                return self._placement_failure(
                    "REJECTED", str(err), side, price, size, raw=data, signature=sig
                )

            order_id = detail
            logger.info(
                f"Live CLOB order placed successfully: ID={order_id} "
                f"(v{order_version} {side_norm} {rounded_size} @ ${rounded_price})"
            )
            if self.self_trade_guard is not None:
                self.self_trade_guard.register_maker_order(
                    order_id=order_id,
                    token_id=token_str,
                    side=side_norm,
                    price=rounded_price,
                    size=rounded_size,
                )
            return OrderPlacementResult(
                success=True,
                order_id=order_id,
                status="LIVE",
                side=side_norm,
                price=rounded_price,
                size=rounded_size,
                raw_response=data,
                signature=sig,
                is_dry_run=False,
            )
