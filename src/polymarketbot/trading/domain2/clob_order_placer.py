"""Domain 2 CLOB Order Placer.

Places passive maker limit bids via LiveClobClient on winning tokens during
the 2-hour UMA dispute liveness window to capture settlement basis discounts.
"""

from __future__ import annotations

from typing import Optional, Dict, Any, List
from polymarketbot.logs import get_logger
from polymarketbot.trading.client import LiveClobClient, OrderPlacementResult
from polymarketbot.trading.domain2.types import (
    DiscountOpportunity,
    Domain2Position,
)

logger = get_logger("domain2_order_placer")


class Domain2ClobOrderPlacer:
    """Manages CLOB limit bid placement and order lifecycle for Domain 2.
    
    Places passive maker limit bids on winning tokens (0.0% maker fee on Polymarket).
    Enforces min_order_usdc >= $20.0 to ensure position size >= 20 shares, amortizing
    on-chain CTF.redeemPositions() gas (~$0.005) to < 0.05c/share (< 0.025c/share actual).
    """

    def __init__(
        self,
        clob_client: LiveClobClient,
        tick_size: float = 0.001,
        target_discount_bps: float = 150.0,  # 1.50% discount ($0.985 bid)
        min_order_usdc: float = 20.0,        # Minimum order size amortizing redemption gas to < 0.05c/share
    ):
        self.clob_client = clob_client
        self.tick_size = tick_size
        self.target_discount_bps = target_discount_bps
        self.min_order_usdc = min_order_usdc

        # Active orders keyed by condition_id -> list of order IDs
        self._active_orders: Dict[str, List[str]] = {}
        # Order metadata keyed by order_id
        self._order_details: Dict[str, Dict[str, Any]] = {}

    def calculate_passive_bid_price(
        self,
        best_bid: float,
        best_ask: float,
        proposed_price: float = 1.0,
    ) -> float:
        """Calculate optimal passive limit bid price.

        Targets between best_bid and 0.990, respecting tick size.
        """
        # Target price from discount bps
        target_from_discount = proposed_price - (self.target_discount_bps / 10000.0)
        
        # If best_bid is active and below 0.990, place at or slightly above best_bid
        if 0.95 <= best_bid < 0.990:
            bid = min(0.990, max(best_bid, target_from_discount))
        else:
            bid = min(0.990, max(0.975, target_from_discount))

        # Round to nearest tick
        rounded_bid = round(round(bid / self.tick_size) * self.tick_size, 4)
        return min(0.990, max(0.975, rounded_bid))

    def place_discount_bid(
        self,
        opportunity: DiscountOpportunity,
        allocated_capital_usdc: float,
    ) -> Optional[OrderPlacementResult]:
        """Place passive maker limit bid on the winning token."""
        cid = opportunity.condition_id.lower()
        token_id = opportunity.winning_asset_id
        
        bid_price = self.calculate_passive_bid_price(
            best_bid=opportunity.best_bid,
            best_ask=opportunity.best_ask,
            proposed_price=opportunity.proposed_price,
        )

        if allocated_capital_usdc < self.min_order_usdc:
            logger.info("Allocated capital (${:.2f}) below minimum order threshold (${:.2f})", allocated_capital_usdc, self.min_order_usdc)
            return None

        # Compute shares
        shares = round(allocated_capital_usdc / bid_price, 2)
        if shares <= 0:
            return None

        logger.info(
            "Placing passive limit BID for condition {}: {} shares of {} at ${:.4f} (${:.2f} USDC)",
            cid[:14], shares, token_id[:10], bid_price, allocated_capital_usdc,
        )

        try:
            result = self.clob_client.place_order(
                token_id=token_id,
                price=bid_price,
                size=shares,
                side="BUY",
            )
            if result.success and result.order_id:
                self._active_orders.setdefault(cid, []).append(result.order_id)
                self._order_details[result.order_id] = {
                    "condition_id": cid,
                    "token_id": token_id,
                    "price": bid_price,
                    "size": shares,
                    "allocated_usdc": allocated_capital_usdc,
                }
                logger.info("Limit BID successfully placed: order_id={}", result.order_id)
            else:
                logger.warning("Order placement rejected: error={}", result.error_message)
            return result
        except Exception as e:
            logger.error("Exception placing order for condition {}: {}", cid[:14], e)
            return None

    def cancel_market_orders(self, condition_id: str) -> List[str]:
        """Cancel all open orders associated with a specific condition.

        Critical for emergency circuit breaker execution upon UMA disputes.
        """
        cid = condition_id.lower()
        order_ids = self._active_orders.pop(cid, [])
        cancelled = []

        if not order_ids:
            logger.info("No active orders found to cancel for condition {}", cid[:14])
            return []

        logger.warning("Cancelling {} active orders for condition {}", len(order_ids), cid[:14])
        for oid in order_ids:
            try:
                self.clob_client.cancel_order(oid)
                self._order_details.pop(oid, None)
                cancelled.append(oid)
                logger.info("Successfully cancelled order {}", oid)
            except Exception as e:
                logger.error("Failed to cancel order {}: {}", oid, e)

        return cancelled

    def cancel_all(self) -> None:
        """Cancel every open order tracked by the placer."""
        all_cids = list(self._active_orders.keys())
        for cid in all_cids:
            self.cancel_market_orders(cid)
