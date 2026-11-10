"""Order request validation and the public ``place_order`` entry point."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any, Optional, Union

from polymarketbot.execution.self_trade import STPAction
from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob.common import (
    DEFAULT_TICK_SIZE,
    ZERO_BYTES32_HEX,
    ClobAPIError,
    OrderPlacementResult,
)
from polymarketbot.strategy.venue.clob.signing import _PreparedOrder
from polymarketbot.strategy.venue.clob.ticks import _TICK_TOLERANCE, _quantize

logger = get_logger("clob_client")


class OrderMixin:
    """Order request validation and placement."""


    def _placement_failure(
        self,
        status: str,
        message: str,
        side: str,
        price: float,
        size: float,
        *,
        raw: Optional[dict[str, Any]] = None,
        signature: str = "",
    ) -> OrderPlacementResult:
        return OrderPlacementResult(
            success=False,
            order_id="",
            status=status,
            side=str(side).upper(),
            price=float(price) if isinstance(price, (int, float)) else 0.0,
            size=float(size) if isinstance(size, (int, float)) else 0.0,
            raw_response=raw or {},
            error_message=message,
            signature=signature,
            is_dry_run=self.dry_run,
        )

    def _prepare_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        *,
        tick_size: Optional[float],
        neg_risk: Optional[bool],
    ) -> Union[_PreparedOrder, OrderPlacementResult]:
        """Validate an order request and snap price/size to the market tick.

        Returns ``_PreparedOrder`` on success or a REJECTED
        ``OrderPlacementResult`` for invalid input. Market-parameter lookups
        (``GET /tick-size`` / ``GET /neg-risk``) may raise ``ClobAPIError``;
        the caller reports that as a rejection.
        """
        token_str = str(token_id).strip()
        if not token_str.isdigit():
            return self._placement_failure(
                "REJECTED",
                f"token_id must be a decimal integer string, got {token_id!r}",
                side,
                price,
                size,
            )

        side_norm = str(side).upper()
        if side_norm not in ("BUY", "SELL"):
            return self._placement_failure(
                "REJECTED", f"side must be 'BUY' or 'SELL', got {side!r}", side, price, size
            )

        price_f = float(price)
        size_f = float(size)
        if not 0.0 < price_f < 1.0:
            return self._placement_failure(
                "REJECTED", f"price must satisfy 0 < price < 1, got {price_f}", side, price, size
            )
        if size_f <= 0.0:
            return self._placement_failure(
                "REJECTED", f"size must be positive, got {size_f}", side, price, size
            )

        if tick_size is None:
            # Dry-run is fully offline: never call GET /tick-size for it.
            # Live mode resolves the real market tick (cached per token).
            market_tick = self.get_tick_size(token_str) if not self.dry_run else DEFAULT_TICK_SIZE
        else:
            market_tick = float(tick_size)
        if market_tick <= 0:
            return self._placement_failure(
                "REJECTED", f"tick_size must be positive, got {market_tick}", side, price, size
            )

        if neg_risk is None:
            # Same rule: offline dry-run assumes a non-negative-risk market,
            # live mode asks GET /neg-risk before choosing the exchange.
            neg_risk_flag = self.get_neg_risk(token_str) if not self.dry_run else False
        else:
            neg_risk_flag = bool(neg_risk)

        rounded_price = self._round_to_tick(price_f, market_tick)
        if abs(rounded_price - price_f) > _TICK_TOLERANCE:
            logger.debug(
                f"price {price_f} snapped to tick {market_tick}: {rounded_price}"
            )
        if not 0.0 < rounded_price < 1.0:
            return self._placement_failure(
                "REJECTED",
                f"price {price_f} snaps to {rounded_price}, outside (0, 1) at tick {market_tick}",
                side,
                price,
                size,
            )
        # Size is floored to 2 dp by the rounding table; mirror that in the result.
        rounded_size = float(_quantize(Decimal(str(size_f)), 2, ROUND_DOWN))
        if rounded_size <= 0.0:
            return self._placement_failure(
                "REJECTED",
                f"size {size_f} rounds to {rounded_size} at 2 dp",
                side,
                price,
                size,
            )

        return _PreparedOrder(
            token_id=token_str,
            side=side_norm,
            price=price_f,
            size=size_f,
            tick_size=market_tick,
            neg_risk=neg_risk_flag,
            rounded_price=rounded_price,
            rounded_size=rounded_size,
        )

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        *,
        tick_size: Optional[float] = None,
        fee_rate_bps: int = 0,
        expiration: int = 0,
        order_type: str = "GTC",
        neg_risk: Optional[bool] = None,
        post_only: bool = False,
        metadata: str = ZERO_BYTES32_HEX,
        builder_code: str = ZERO_BYTES32_HEX,
    ) -> OrderPlacementResult:
        """Sign and place a limit order on Polymarket CLOB.

        Validation (audit C12/C13/S17):
            - ``token_id`` must be all digits; the old md5 fallback is gone.
            - ``0 < price < 1`` and ``size > 0``; price is snapped to ``tick_size``
              and size is floored to 2 dp per the per-tick rounding table.
            - ``tick_size``/``neg_risk`` default to ``GET /tick-size`` and
              ``GET /neg-risk`` for the token (cached), so live orders never use a
              guessed market parameter. Dry-run uses 0.01 / False offline.
            - The order version comes from ``GET /version``; an
              ``order_version_mismatch`` rejection refetches the version once and
              retries the order exactly once.

        In dry-run mode, computes the authentic EIP-712 signature and records
        the order locally without sending real funds to CLOB.
        """
        try:
            prepared = self._prepare_order(
                token_id,
                side,
                price,
                size,
                tick_size=tick_size,
                neg_risk=neg_risk,
            )
            if isinstance(prepared, OrderPlacementResult):
                return prepared

            # -------------------------------------------------------------
            # Self-Trade Prevention (Wash Trading Guard) Check
            # -------------------------------------------------------------
            if self.self_trade_guard is not None:
                stp_check = self.self_trade_guard.check_order(
                    token_id=prepared.token_id,
                    side=prepared.side,
                    price=prepared.rounded_price,
                    size=prepared.rounded_size,
                    is_taker=not post_only,
                )
                if not stp_check.allowed:
                    if stp_check.action == STPAction.CANCEL_MAKER:
                        for crossing_ord in stp_check.crossing_orders:
                            self.cancel_order(crossing_ord.order_id)
                    else:
                        logger.warning(
                            f"Order placement blocked by SelfTradeGuard: {stp_check.reason}"
                        )
                        return self._placement_failure(
                            "STP_REJECTED",
                            stp_check.reason,
                            side,
                            price,
                            size,
                        )

            signed = self._sign_order(
                prepared,
                fee_rate_bps=int(fee_rate_bps),
                expiration=int(expiration),
                order_type=order_type,
                metadata=metadata,
                builder_code=builder_code,
                post_only=post_only,
            )
        except ClobAPIError as e:
            logger.error(f"CLOB order setup failed: {e}")
            return self._placement_failure("REJECTED", str(e), side, price, size)
        except ValueError as e:
            logger.error(f"Invalid order parameters: {e}")
            return self._placement_failure("REJECTED", str(e), side, price, size)
        except Exception as e:
            logger.error(f"Failed to generate signed order payload: {e}")
            return self._placement_failure("ERROR", str(e), side, price, size)

        return self._dispatch_order(
            prepared,
            signed,
            fee_rate_bps=int(fee_rate_bps),
            expiration=int(expiration),
            order_type=order_type,
            metadata=metadata,
            builder_code=builder_code,
            post_only=post_only,
        )
