"""Live order dispatch over REST or the HFT connection pool."""

from __future__ import annotations

import json
import time
import uuid

from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob.common import ClobAPIError, OrderPlacementResult
from polymarketbot.strategy.venue.clob.signing import _PreparedOrder, _SignedOrder

logger = get_logger("clob_client")


def _is_order_version_mismatch(text: str) -> bool:
    """True when the CLOB rejected an order because its signed version is stale."""
    return "order_version_mismatch" in str(text).lower()


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
        raw_bytes = signed.raw_bytes

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
        if self.hft_pool is not None:
            resp = self.hft_pool.place_order(raw_bytes if raw_bytes is not None else payload)
            data = resp.data
            if resp.success and isinstance(data, dict) and data.get("success", True):
                order_id = str(data.get("orderID") or data.get("id") or "")
                logger.info(
                    f"Live CLOB order placed via HFT pool: ID={order_id} "
                    f"(v{order_version} {side_norm} {rounded_size} @ ${rounded_price} in {resp.latency_ms:.2f}ms)"
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
            err = (data.get("errorMsg") or data.get("error") if isinstance(data, dict) else resp.error) or "HFT order rejected"
            logger.error(f"HFT CLOB order placement failed: {err}")
            return self._placement_failure(
                "REJECTED", str(err), side, price, size, raw=data if isinstance(data, dict) else {}, signature=sig
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
                    order_version, fast_order, payload = refreshed
                    sig = payload.get("signature") or payload.get("order", {}).get("signature", "")
                    continue
                status = "NETWORK_ERROR" if e.status_code == 0 else "REJECTED"
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

            if not data.get("success", True):
                err = data.get("errorMsg") or data.get("error") or "order rejected"
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
                    order_version, fast_order, payload = refreshed
                    sig = payload.get("signature") or payload.get("order", {}).get("signature", "")
                    continue
                logger.error(f"CLOB order placement rejected: {err}")
                return self._placement_failure(
                    "REJECTED", str(err), side, price, size, raw=data, signature=sig
                )

            order_id = str(data.get("orderID") or data.get("id") or "")
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
