"""Batch order submission via ``POST /orders``."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import ROUND_DOWN, Decimal
from typing import Any

import orjson

from polymarketbot.logs import get_logger
from polymarketbot.risk.fast_signer import FastOrder, OrderV2
from polymarketbot.strategy.venue.clob.common import (
    DEFAULT_TICK_SIZE,
    ClobAPIError,
    OrderPlacementResult,
)
from polymarketbot.strategy.venue.clob.dispatch import _order_response_outcome
from polymarketbot.strategy.venue.clob.ticks import _quantize

logger = get_logger("clob_client")


def _coerce_min_order(value: Any) -> float | None:
    """The market's published minimum notional, or ``None`` when not supplied."""
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0.0 else None


class BatchMixin:
    """Multi-order submission and order-version recovery."""


    def place_orders(
        self,
        orders: Sequence[dict[str, Any]],
    ) -> list[OrderPlacementResult]:
        """Submit a batch of orders to Polymarket CLOB via POST /orders.

        Uses pre-signed cache when available and HFT connection pool for single-RTT execution.
        """
        results: list[OrderPlacementResult] = []
        if not orders:
            return results

        prepared_payloads: list[dict[str, Any]] = []
        prepared_indices: list[int] = []

        for idx, ord_spec in enumerate(orders):
            token_id = str(ord_spec.get("token_id", "")).strip()
            side = str(ord_spec.get("side", "")).upper()
            price = float(ord_spec.get("price", 0.0))
            size = float(ord_spec.get("size", 0.0))
            tick_spec = ord_spec.get("tick_size")
            post_only = bool(ord_spec.get("post_only", False))

            if not token_id.isdigit() or side not in ("BUY", "SELL") or not (0 < price < 1) or size <= 0:
                results.append(self._placement_failure("REJECTED", "Invalid batch order parameters", side, price, size))
                continue

            # Market parameters the signature commits to: never assume the
            # default exchange domain or the default tick for a real order.
            if tick_spec is None:
                try:
                    tick_size = float(self.get_tick_size(token_id)) if not self.dry_run else DEFAULT_TICK_SIZE
                except ClobAPIError as e:
                    results.append(self._placement_failure("REJECTED", str(e), side, price, size))
                    continue
            else:
                tick_size = float(tick_spec)
            neg_spec = ord_spec.get("neg_risk")
            if neg_spec is None:
                try:
                    neg_risk = bool(self.get_neg_risk(token_id)) if not self.dry_run else False
                except ClobAPIError as e:
                    results.append(self._placement_failure("REJECTED", str(e), side, price, size))
                    continue
            else:
                neg_risk = bool(neg_spec)
            fee_spec = ord_spec.get("fee_rate_bps")
            fee_bps = None if fee_spec is None else int(fee_spec)
            min_order_usd = _coerce_min_order(ord_spec.get("min_order_usd"))

            rounded_price = self._round_to_tick(price, tick_size)
            rounded_size = float(_quantize(Decimal(str(size)), 2, ROUND_DOWN))
            if rounded_size <= 0.0:
                results.append(
                    self._placement_failure(
                        "REJECTED",
                        f"size {size} rounds to {rounded_size} at 2 dp",
                        side,
                        price,
                        size,
                    )
                )
                continue
            if min_order_usd is not None and rounded_size * rounded_price + 1e-9 < min_order_usd:
                results.append(
                    self._placement_failure(
                        "REJECTED",
                        f"order notional ${rounded_size * rounded_price:.4f} is below "
                        f"the market's minimum ${min_order_usd:.2f}",
                        side,
                        price,
                        size,
                    )
                )
                continue

            if self.self_trade_guard is not None:
                stp_check = self.self_trade_guard.check_order(token_id, side, rounded_price, rounded_size, is_taker=not post_only)
                if not stp_check.allowed:
                    results.append(self._placement_failure("STP_REJECTED", stp_check.reason, side, price, size))
                    continue

            payload = None
            sig = ""
            order_version = self._resolve_order_version()

            if payload is None:
                try:
                    _, payload = self.build_signed_order(
                        token_id=token_id,
                        side=side,
                        price=rounded_price,
                        size=rounded_size,
                        tick_size=tick_size,
                        neg_risk=neg_risk,
                        fee_rate_bps=fee_bps,
                        post_only=post_only,
                        order_version=order_version,
                    )
                    sig = payload.get("signature") or payload.get("order", {}).get("signature", "")
                except (ValueError, TypeError, KeyError) as e:
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
        try:
            data = self._request("POST", "/orders", body=raw_body.decode("utf-8"))
        except ClobAPIError as e:
            for p_idx in prepared_indices:
                res = results[p_idx]
                results[p_idx] = self._placement_failure("NETWORK_ERROR", str(e), res.side, res.price, res.size, signature=res.signature)
            return results

        if not isinstance(data, list):
            # A dict body here is an error envelope or a schema change; every
            # order in the batch is unconfirmed, never "PENDING success".
            message = (
                f"POST /orders returned a non-list body ({type(data).__name__}): {data!r}"
            )
            logger.error(message)
            for p_idx in prepared_indices:
                res = results[p_idx]
                results[p_idx] = self._placement_failure(
                    "NETWORK_ERROR", message, res.side, res.price, res.size, signature=res.signature
                )
            return results

        if len(data) != len(prepared_indices):
            logger.error(
                f"POST /orders answered {len(data)} result(s) for "
                f"{len(prepared_indices)} order(s); the missing ones are unconfirmed"
            )

        for i, p_idx in enumerate(prepared_indices):
            item = data[i] if i < len(data) else None
            res = results[p_idx]
            if not isinstance(item, dict):
                results[p_idx] = self._placement_failure(
                    "REJECTED",
                    "batch response carried no result for this order",
                    res.side,
                    res.price,
                    res.size,
                    signature=res.signature,
                )
                continue
            outcome, detail = _order_response_outcome(item)
            if outcome == "ok":
                oid = detail
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
                results[p_idx] = self._placement_failure(
                    "REJECTED", detail, res.side, res.price, res.size, raw=item, signature=res.signature
                )

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
    ) -> tuple[int, FastOrder | OrderV2, dict[str, Any]] | OrderPlacementResult:
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
