"""Batch order submission via ``POST /orders``."""

from __future__ import annotations

import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Any, List, Sequence, Union

import orjson

from polymarketbot.logs import get_logger
from polymarketbot.risk.fast_signer import FastOrder, OrderV2
from polymarketbot.strategy.venue.clob.common import (
    DEFAULT_TICK_SIZE,
    ClobAPIError,
    OrderPlacementResult,
)
from polymarketbot.strategy.venue.clob.ticks import _quantize

logger = get_logger("clob_client")


class BatchMixin:
    """Multi-order submission and order-version recovery."""


    def place_orders(
        self,
        orders: Sequence[dict[str, Any]],
    ) -> List[OrderPlacementResult]:
        """Submit a batch of orders to Polymarket CLOB via POST /orders.

        Uses pre-signed cache when available and HFT connection pool for single-RTT execution.
        """
        results: List[OrderPlacementResult] = []
        if not orders:
            return results

        prepared_payloads: List[dict[str, Any]] = []
        prepared_indices: List[int] = []

        for idx, ord_spec in enumerate(orders):
            token_id = str(ord_spec.get("token_id", "")).strip()
            side = str(ord_spec.get("side", "")).upper()
            price = float(ord_spec.get("price", 0.0))
            size = float(ord_spec.get("size", 0.0))
            tick_size = float(ord_spec.get("tick_size") or DEFAULT_TICK_SIZE)
            post_only = bool(ord_spec.get("post_only", False))

            if not token_id.isdigit() or side not in ("BUY", "SELL") or not (0 < price < 1) or size <= 0:
                results.append(self._placement_failure("REJECTED", "Invalid batch order parameters", side, price, size))
                continue

            rounded_price = self._round_to_tick(price, tick_size)
            rounded_size = float(_quantize(Decimal(str(size)), 2, ROUND_DOWN))

            if self.self_trade_guard is not None:
                stp_check = self.self_trade_guard.check_order(token_id, side, rounded_price, rounded_size, is_taker=not post_only)
                if not stp_check.allowed:
                    results.append(self._placement_failure("STP_REJECTED", stp_check.reason, side, price, size))
                    continue

            payload = None
            sig = ""
            order_version = self._resolve_order_version()

            if self.presigned_cache is not None:
                cached = self.presigned_cache.pop_order(token_id, side, rounded_price, rounded_size, tick_size)
                if cached is not None:
                    payload = cached.payload
                    sig = cached.signature
                    order_version = cached.version

            if payload is None:
                try:
                    _, payload = self.build_signed_order(
                        token_id=token_id,
                        side=side,
                        price=rounded_price,
                        size=rounded_size,
                        tick_size=tick_size,
                        post_only=post_only,
                        order_version=order_version,
                    )
                    sig = payload.get("signature") or payload.get("order", {}).get("signature", "")
                except Exception as e:
                    results.append(self._placement_failure("ERROR", str(e), side, price, size))
                    continue

            prepared_payloads.append(payload)
            prepared_indices.append(idx)
            results.append(OrderPlacementResult(
                success=True,
                order_id="",
                status="PENDING",
                side=side,
                price=rounded_price,
                size=rounded_size,
                signature=sig,
                is_dry_run=self.dry_run,
            ))

        if not prepared_payloads:
            return results

        if self.dry_run:
            for p_idx in prepared_indices:
                mock_id = f"mock_ord_{uuid.uuid4().hex[:12]}"
                res = results[p_idx]
                results[p_idx] = OrderPlacementResult(
                    success=True,
                    order_id=mock_id,
                    status="LIVE",
                    side=res.side,
                    price=res.price,
                    size=res.size,
                    signature=res.signature,
                    is_dry_run=True,
                    raw_response={"orderID": mock_id, "status": "LIVE", "mock": True},
                )
                if self.self_trade_guard is not None:
                    self.self_trade_guard.register_maker_order(
                        order_id=mock_id,
                        token_id=orders[p_idx]["token_id"],
                        side=res.side,
                        price=res.price,
                        size=res.size,
                    )
            return results

        # Live dispatch
        raw_body = orjson.dumps(prepared_payloads)
        if self.hft_pool is not None:
            resp = self.hft_pool.place_orders(raw_body)
            data = resp.data
        else:
            try:
                data = self._request("POST", "/orders", body=raw_body.decode("utf-8"))
            except Exception as e:
                for p_idx in prepared_indices:
                    res = results[p_idx]
                    results[p_idx] = self._placement_failure("NETWORK_ERROR", str(e), res.side, res.price, res.size, signature=res.signature)
                return results

        if isinstance(data, list):
            for i, p_idx in enumerate(prepared_indices):
                item = data[i] if i < len(data) else {}
                res = results[p_idx]
                if isinstance(item, dict) and item.get("success", True):
                    oid = str(item.get("orderID") or item.get("id") or "")
                    results[p_idx] = OrderPlacementResult(
                        success=True,
                        order_id=oid,
                        status="LIVE",
                        side=res.side,
                        price=res.price,
                        size=res.size,
                        signature=res.signature,
                        raw_response=item,
                        is_dry_run=False,
                    )
                    if self.self_trade_guard is not None:
                        self.self_trade_guard.register_maker_order(
                            order_id=oid,
                            token_id=orders[p_idx]["token_id"],
                            side=res.side,
                            price=res.price,
                            size=res.size,
                        )
                else:
                    err = item.get("errorMsg") if isinstance(item, dict) else "Batch item rejected"
                    results[p_idx] = self._placement_failure("REJECTED", str(err), res.side, res.price, res.size, raw=item if isinstance(item, dict) else {}, signature=res.signature)

        return results

    def _retry_with_refreshed_version(
        self,
        *,
        token_id: str,
        side: str,
        price: float,
        size: float,
        tick_size: float,
        neg_risk: bool,
        fee_rate_bps: int,
        expiration: int,
        order_type: str,
        metadata: str,
        builder_code: str,
        post_only: bool,
    ) -> Union[tuple[int, Union[FastOrder, OrderV2], dict[str, Any]], OrderPlacementResult]:
        """Refetch ``GET /version`` once and rebuild the order for it."""
        try:
            version = self.refresh_order_version()
            order, payload = self.build_signed_order(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                expiration=expiration,
                fee_rate_bps=fee_rate_bps,
                neg_risk=neg_risk,
                order_type=order_type,
                tick_size=tick_size,
                metadata=metadata,
                builder_code=builder_code,
                post_only=post_only,
                order_version=version,
            )
        except ClobAPIError as e:
            logger.error(f"Order version refresh failed: {e}")
            return self._placement_failure("REJECTED", str(e), side, price, size)
        logger.warning(f"order_version_mismatch: retrying the order with CLOB version {version}")
        return version, order, payload
