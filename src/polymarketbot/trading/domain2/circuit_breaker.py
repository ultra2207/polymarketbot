"""Domain 2 Circuit Breakers and Risk Controls.

Enforces emergency cancellations on dispute events and protects capital
from adverse selection, over-allocation, and tail oracle risk.
"""

from __future__ import annotations

from typing import Set, Dict, Optional, Tuple
from polymarketbot.logs import get_logger
from polymarketbot.trading.domain2.types import (
    UmaDisputeEvent,
    DiscountOpportunity,
    Domain2MarketState,
)

logger = get_logger("domain2_circuit_breaker")


class Domain2CircuitBreaker:
    """Risk controller and emergency circuit breaker for Domain 2."""

    def __init__(
        self,
        max_market_allocation_pct: float = 0.10,
        max_portfolio_exposure_usdc: float = 10000.0,
        max_concurrent_markets: int = 15,
        min_price: float = 0.850,
        max_price: float = 0.998,
        min_hours_to_expiry: float = 0.01,
        max_hours_to_expiry: float = 72.0,
    ):
        self.max_market_allocation_pct = max_market_allocation_pct
        self.max_portfolio_exposure_usdc = max_portfolio_exposure_usdc
        self.max_concurrent_markets = max_concurrent_markets
        self.min_price = min_price
        self.max_price = max_price
        self.min_hours_to_expiry = min_hours_to_expiry
        self.max_hours_to_expiry = max_hours_to_expiry

        self._halted_markets: Set[str] = set()
        self._disputed_markets: Set[str] = set()
        self._dispute_records: Dict[str, UmaDisputeEvent] = {}

    def is_market_halted(self, condition_id: str) -> bool:
        """Check if trading is halted for the given market."""
        cid = condition_id.lower()
        return cid in self._halted_markets or cid in self._disputed_markets

    def handle_dispute_event(self, event: UmaDisputeEvent) -> Tuple[bool, str]:
        """Handle incoming UMA DisputePrice event.
        
        Returns:
            Tuple[bool, str]: (should_cancel_orders, action_summary)
        """
        cid = event.condition_id.lower()
        self._disputed_markets.add(cid)
        self._halted_markets.add(cid)
        self._dispute_records[cid] = event

        msg = (
            f"EMERGENCY CIRCUIT BREAKER TRIGGERED: UMA DisputePrice event emitted for "
            f"condition {cid[:14]} by disputer {event.disputer[:10]} at block {event.block_number}! "
            f"Halting quoting and triggering emergency cancellation of all active orders."
        )
        logger.critical(msg)
        return True, msg

    def check_entry_allowed(
        self,
        opportunity: DiscountOpportunity,
        current_equity_usdc: float,
        current_market_cost_usdc: float,
        total_open_exposure_usdc: float,
        active_markets_count: int,
        dynamic_w_max: Optional[float] = None,
    ) -> Tuple[bool, float, Optional[str]]:
        """Validate if a discount trade opportunity satisfies all risk boundaries.

        Returns:
            Tuple[bool, float, Optional[str]]:
                - allowed: Whether entry is permitted.
                - allowed_allocation_usdc: Maximum allowable new capital to allocate.
                - reason: Rejection reason if not allowed.
        """
        cid = opportunity.condition_id.lower()

        # 1. Check dispute / halt state
        if self.is_market_halted(cid):
            return False, 0.0, f"Market {cid[:14]} is halted/disputed"

        # 2. Check time to dispute expiry (must be in valid post-proposal window <= 2.0h)
        if opportunity.hours_to_liveness_expiry > self.max_hours_to_expiry:
            return False, 0.0, (
                f"Hours to expiry ({opportunity.hours_to_liveness_expiry:.2f}h) "
                f"exceeds safe limit ({self.max_hours_to_expiry:.2f}h)"
            )
        if opportunity.hours_to_liveness_expiry < self.min_hours_to_expiry:
            return False, 0.0, (
                f"Hours to expiry ({opportunity.hours_to_liveness_expiry:.2f}h) "
                f"too close to resolution (< {self.min_hours_to_expiry:.2f}h)"
            )

        # 3. Check price boundaries
        target_price = opportunity.recommended_bid_price
        if target_price < self.min_price or target_price > self.max_price:
            return False, 0.0, (
                f"Target price {target_price:.4f} outside safe discount band "
                f"[{self.min_price:.3f}, {self.max_price:.3f}]"
            )

        # 4. Check active market count capacity
        if current_market_cost_usdc <= 0.0 and active_markets_count >= self.max_concurrent_markets:
            return False, 0.0, (
                f"Active markets count ({active_markets_count}) reached cap ({self.max_concurrent_markets})"
            )

        # 5. Check per-market allocation limits (bounded by dynamic sizer if supplied)
        if dynamic_w_max is not None:
            max_market_cap = float(dynamic_w_max)
        else:
            max_market_cap = current_equity_usdc * self.max_market_allocation_pct

        if dynamic_w_max is not None and dynamic_w_max <= 0.0:
            return False, 0.0, f"Market {cid[:14]} rejected by dynamic sizer (w_max = $0.00)"

        remaining_market_cap = max(0.0, max_market_cap - current_market_cost_usdc)
        if remaining_market_cap < 1.0:
            return False, 0.0, f"Market allocation cap (${max_market_cap:.2f}) exhausted"

        # 6. Check aggregate portfolio exposure
        remaining_portfolio_cap = max(0.0, self.max_portfolio_exposure_usdc - total_open_exposure_usdc)
        if remaining_portfolio_cap < 1.0:
            return False, 0.0, f"Portfolio exposure cap (${self.max_portfolio_exposure_usdc:.2f}) reached"

        alloc_cap = min(remaining_market_cap, remaining_portfolio_cap)
        return True, alloc_cap, None

