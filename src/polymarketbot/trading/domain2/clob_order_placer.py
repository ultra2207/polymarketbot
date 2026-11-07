"""Domain 2 CLOB Order Placer.

Places maker limit bids or executes acute taker sweeps via LiveClobClient on winning tokens during
the UMA dispute liveness window to capture settlement basis discounts.
Integrates SettlementMakerPricingEngine for dynamic regime selection:
- Option A: MAKER_IMPROVE_INSIDE_SPREAD
- Option B: MAKER_JOIN_TOP_QUEUE
- Option C: Dynamic EV Maximizer
- Taker Fallback: TAKER_SWEEP_IMMEDIATE
"""

from __future__ import annotations

from typing import Optional, Dict, Any, List
from polymarketbot.logs import get_logger
from polymarketbot.trading.client import LiveClobClient, OrderPlacementResult
from polymarketbot.trading.domain2.types import (
    DiscountOpportunity,
    Domain2Position,
    MakerExecutionRegime,
    MakerPricingDecision,
)
from polymarketbot.trading.domain2.maker_pricing_engine import (
    SettlementMakerPricingEngine,
    MakerEngineConfig,
    parse_l2_book,
    calculate_upward_drift_rate,
)

logger = get_logger("domain2_order_placer")


class Domain2ClobOrderPlacer:
    """Manages CLOB limit bid placement and order lifecycle for Domain 2.
    
    Places passive maker limit bids on winning tokens (0.0% maker fee on Polymarket),
    or executes immediate taker sweeps when expiry is acute and edge exceeds fees.
    Enforces min_order_usdc >= $20.0 to ensure position size >= 20 shares, amortizing
    on-chain CTF.redeemPositions() gas (~$0.005) to < 0.05c/share (< 0.025c/share actual).
    """

    def __init__(
        self,
        clob_client: LiveClobClient,
        tick_size: float = 0.001,
        target_discount_bps: float = 150.0,  # 1.50% discount ($0.985 bid)
        min_order_usdc: float = 20.0,        # Minimum order size amortizing redemption gas to < 0.05c/share
        pricing_engine: Optional[SettlementMakerPricingEngine] = None,
    ):
        self.clob_client = clob_client
        self.tick_size = tick_size
        self.target_discount_bps = target_discount_bps
        self.min_order_usdc = min_order_usdc
        self.pricing_engine = pricing_engine or SettlementMakerPricingEngine(
            config=MakerEngineConfig(default_tick_size=tick_size)
        )

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

    def refresh_opportunity_book(
        self,
        opportunity: DiscountOpportunity,
    ) -> DiscountOpportunity:
        """Refresh live order book depth and queue ahead from CLOB if client supports it."""
        if hasattr(self.clob_client, "get_order_book") and opportunity.winning_asset_id:
            try:
                book = self.clob_client.get_order_book(opportunity.winning_asset_id)
                if book and isinstance(book, dict) and (book.get("bids") or book.get("asks")):
                    best_bid, best_ask, q_ahead, spread = parse_l2_book(
                        book_data=book,
                        tick_size=opportunity.tick_size or self.tick_size,
                        default_price=opportunity.best_bid,
                        proposed_price=opportunity.proposed_price,
                    )
                    opportunity.best_bid = best_bid
                    opportunity.best_ask = best_ask
                    opportunity.standing_depth_ahead = q_ahead
                    opportunity.discount_bps = max(0.0, (opportunity.proposed_price - best_bid) * 10000.0)
                    opportunity.drift_rate_usd_hr = calculate_upward_drift_rate(
                        best_bid=best_bid,
                        hours_to_expiry=opportunity.hours_to_liveness_expiry,
                        proposed_price=opportunity.proposed_price,
                    )
                    logger.debug(
                        "Refreshed live book for {}: bid={:.4f}, ask={:.4f}, Q_ahead={:.1f}, spread={:.4f}",
                        opportunity.winning_asset_id[:10], best_bid, best_ask, q_ahead, spread,
                    )
            except Exception as e:
                logger.debug("Live order book refresh skipped for {}: {}", opportunity.winning_asset_id[:10], e)
        return opportunity

    def evaluate_quote_decision(
        self,
        opportunity: DiscountOpportunity,
        allocated_capital_usdc: float,
    ) -> MakerPricingDecision:
        """Evaluate quote pricing decision via the SettlementMakerPricingEngine."""
        return self.pricing_engine.evaluate_decision(
            best_bid=opportunity.best_bid,
            best_ask=opportunity.best_ask,
            allocated_capital_usdc=allocated_capital_usdc,
            hours_to_expiry=opportunity.hours_to_liveness_expiry,
            standing_depth_ahead=getattr(opportunity, "standing_depth_ahead", None),
            taker_sell_velocity_shares_hr=getattr(opportunity, "taker_sell_velocity_shares_hr", None),
            tick_size=getattr(opportunity, "tick_size", self.tick_size),
            proposed_price=opportunity.proposed_price,
        )

    def place_discount_bid(
        self,
        opportunity: DiscountOpportunity,
        allocated_capital_usdc: float,
    ) -> Optional[OrderPlacementResult]:
        """Place dynamic maker limit bid (or taker sweep) on the winning token."""
        cid = opportunity.condition_id.lower()
        token_id = opportunity.winning_asset_id

        if allocated_capital_usdc < self.min_order_usdc:
            logger.info(
                "Allocated capital (${:.2f}) below minimum order threshold (${:.2f})",
                allocated_capital_usdc, self.min_order_usdc
            )
            return None

        # Dynamically refresh order book parameters right before evaluating quote
        opportunity = self.refresh_opportunity_book(opportunity)

        # Evaluate dynamic regime and pricing
        decision = self.evaluate_quote_decision(opportunity, allocated_capital_usdc)
        order_price = decision.quote_price
        side = decision.side
        shares = round(allocated_capital_usdc / max(0.01, order_price), 2)
        if shares <= 0:
            return None

        logger.info(
            "Executing {} [{}] for condition {}: {} shares of {} at ${:.4f} (${:.2f} USDC) - ARR: {:.1f}%, Drift: ${:.4f}/h, Reason: {}",
            "TAKER SWEEP" if decision.is_taker else "MAKER LIMIT BID",
            decision.regime.value,
            cid[:14],
            shares,
            token_id[:10],
            order_price,
            allocated_capital_usdc,
            decision.expected_arr,
            decision.drift_rate_usd_hr,
            decision.reason,
        )

        try:
            result = self.clob_client.place_order(
                token_id=token_id,
                price=order_price,
                size=shares,
                side=side,
            )
            if result.success and result.order_id:
                self._active_orders.setdefault(cid, []).append(result.order_id)
                self._order_details[result.order_id] = {
                    "condition_id": cid,
                    "token_id": token_id,
                    "price": order_price,
                    "size": shares,
                    "allocated_usdc": allocated_capital_usdc,
                    "decision": decision,
                    "regime": decision.regime.value,
                    "is_taker": decision.is_taker,
                    "p_fill": decision.p_fill,
                    "t_clear_hours": decision.t_clear_hours,
                    "expected_arr": decision.expected_arr,
                    "standing_depth_ahead": decision.standing_depth_ahead,
                    "taker_sell_velocity_shares_hr": decision.taker_sell_velocity_shares_hr,
                    "drift_rate_usd_hr": decision.drift_rate_usd_hr,
                }
                logger.info("Order successfully placed: order_id={} [{}]", result.order_id, decision.regime.value)
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
