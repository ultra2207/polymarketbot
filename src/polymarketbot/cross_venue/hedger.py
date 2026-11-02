"""Passive Cross-Venue Hedger.

Co-Market Making & Synthetic Complete Set Locking:
1. When Polymarket fills long YES @ P_poly:
   Immediately instructs the Kalshi client to place a resting maker quote for
   NO @ (1.00 - P_poly) on Kalshi (zero maker fees on Kalshi), locking in a
   complete synthetic pair without paying Kalshi's 7% quadratic taker fee.
2. Rejection of Taker Crossing:
   When spread dislocation <= 1.5c, taker crossing is strictly REJECTED to prevent
   fee bleed from Kalshi's 7% quadratic taker fee. Only resting maker quotes (post_only=True)
   are permitted.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from polymarketbot.cross_venue.bus import (
    TOPIC_HEDGE_REQUESTS,
    TOPIC_KALSHI_FILLS,
    CrossVenueBus,
)
from polymarketbot.cross_venue.models import (
    CrossVenueHedgeRequest,
    HedgeExecutionMode,
    HedgeStatus,
    SyntheticPairState,
    Venue,
)
from polymarketbot.kalshi.client import KalshiClient
from polymarketbot.logs import get_logger

logger = get_logger("passive_hedger")

# Rejection hurdle: 1.5 cents ($0.015)
MIN_TAKER_DISLOCATION_HURDLE = 0.015
DEFAULT_ORDER_GROUP_ID = "CIRCUIT_BREAKER_HEDGE"


class TakerCrossingRejectedError(ValueError):
    """Raised when aggressive taker crossing is attempted with spread dislocation <= 1.5c."""


class PassiveCrossVenueHedger:
    """Passive cross-venue co-market maker and inventory hedger."""

    def __init__(
        self,
        bus: CrossVenueBus,
        kalshi_client: KalshiClient,
        min_taker_dislocation: float = MIN_TAKER_DISLOCATION_HURDLE,
        default_order_group_id: str = DEFAULT_ORDER_GROUP_ID,
    ) -> None:
        self.bus = bus
        self.kalshi_client = kalshi_client
        self.min_taker_dislocation = min_taker_dislocation
        self.default_order_group_id = default_order_group_id

        self._synthetic_pairs: Dict[str, SyntheticPairState] = {}
        self._hedge_requests: Dict[str, CrossVenueHedgeRequest] = {}

    def get_or_create_pair_state(
        self,
        poly_token_id: str,
        kalshi_ticker: str,
    ) -> SyntheticPairState:
        """Get or initialize the synthetic pair state between Polymarket and Kalshi."""
        pair_id = f"{poly_token_id}:{kalshi_ticker}"
        if pair_id not in self._synthetic_pairs:
            self._synthetic_pairs[pair_id] = SyntheticPairState(
                pair_id=pair_id,
                poly_token_id=poly_token_id,
                kalshi_ticker=kalshi_ticker,
            )
        return self._synthetic_pairs[pair_id]

    def hedge_polymarket_fill(
        self,
        poly_token_id: str,
        kalshi_ticker: str,
        side: str,  # 'YES' or 'NO'
        fill_price: float,
        fill_size: float,
        originating_order_id: str = "",
        execution_mode: HedgeExecutionMode = HedgeExecutionMode.MAKER_POST_ONLY,
        spread_dislocation: Optional[float] = None,
        order_group_id: Optional[str] = None,
    ) -> CrossVenueHedgeRequest:
        """Hedge an executed Polymarket fill by quoting on Kalshi.

        When Polymarket fills long YES @ P_poly:
        - Target on Kalshi: buy NO @ (1.00 - P_poly) with post_only=True.
        - Zero maker fee on Kalshi.
        - Locks complete set: YES(Poly) + NO(Kalshi) = $1.00 payout.

        When spread dislocation <= 1.5c and taker execution is attempted:
        - Strictly REJECTED to prevent Kalshi 7% quadratic taker fee bleed.
        """
        side_norm = side.upper()
        if side_norm not in ("YES", "NO"):
            raise ValueError(f"Invalid side '{side}', must be 'YES' or 'NO'")

        pair_state = self.get_or_create_pair_state(poly_token_id, kalshi_ticker)

        # 1. Record Polymarket fill in synthetic state
        pair_state.record_fill(
            venue=Venue.POLYMARKET,
            side=side_norm,
            price=fill_price,
            size=fill_size,
        )

        # 2. Determine target Kalshi side and price for synthetic complete set
        if side_norm == "YES":
            target_side = "NO"
            target_price = round(1.00 - fill_price, 4)
        else:
            target_side = "YES"
            target_price = round(1.00 - fill_price, 4)

        target_cents = max(1, min(99, int(round(target_price * 100))))
        dislocation = spread_dislocation if spread_dislocation is not None else 0.0

        # 3. Rule: Rejection of taker crossing when spread dislocation <= 1.5c ($0.015)
        if execution_mode == HedgeExecutionMode.TAKER_CROSS:
            if dislocation <= self.min_taker_dislocation:
                reason = (
                    f"Taker crossing rejected: spread dislocation {dislocation*100:.2f}c <= "
                    f"{self.min_taker_dislocation*100:.2f}c hurdle (Kalshi 7% quadratic taker fee)"
                )
                hedge_req = CrossVenueHedgeRequest(
                    originating_venue=Venue.POLYMARKET,
                    originating_order_id=originating_order_id or f"poly_{time.time_ns()}",
                    originating_token_id=poly_token_id,
                    originating_side=side_norm,
                    fill_price=fill_price,
                    fill_size=fill_size,
                    target_venue=Venue.KALSHI,
                    target_ticker=kalshi_ticker,
                    target_side=target_side,
                    target_price=target_price,
                    target_size=fill_size,
                    execution_mode=HedgeExecutionMode.TAKER_CROSS,
                    post_only=False,
                    dislocation_spread=dislocation,
                    status=HedgeStatus.REJECTED,
                    rejection_reason=reason,
                )
                self._hedge_requests[hedge_req.hedge_id] = hedge_req
                self.bus.dispatch(TOPIC_HEDGE_REQUESTS, hedge_req)
                logger.warning(f"REJECTED TAKER HEDGE: {reason}")
                return hedge_req

        # 4. Passive Maker Quoting on Kalshi (post_only=True, zero maker fees)
        target_group = order_group_id or self.default_order_group_id
        order_resp = self.kalshi_client.create_order(
            ticker=kalshi_ticker,
            action="buy",
            side=target_side.lower(),
            count=int(fill_size),
            yes_price=target_cents if target_side.lower() == "yes" else None,
            no_price=target_cents if target_side.lower() == "no" else None,
            post_only=True,
            order_group_id=target_group,
        )

        kalshi_order_id = order_resp.get("order", {}).get("order_id", f"kord_{time.time_ns()}")
        hedge_req = CrossVenueHedgeRequest(
            originating_venue=Venue.POLYMARKET,
            originating_order_id=originating_order_id or f"poly_{time.time_ns()}",
            originating_token_id=poly_token_id,
            originating_side=side_norm,
            fill_price=fill_price,
            fill_size=fill_size,
            target_venue=Venue.KALSHI,
            target_ticker=kalshi_ticker,
            target_side=target_side,
            target_price=target_price,
            target_size=fill_size,
            execution_mode=HedgeExecutionMode.MAKER_POST_ONLY,
            post_only=True,
            order_group_id=target_group,
            dislocation_spread=dislocation,
            status=HedgeStatus.RESTING,
            target_order_id=kalshi_order_id,
        )

        self._hedge_requests[hedge_req.hedge_id] = hedge_req
        self.bus.dispatch(TOPIC_HEDGE_REQUESTS, hedge_req)
        logger.info(
            f"Resting Maker Quote Placed on Kalshi: buy {target_side} {fill_size} @ {target_cents}c "
            f"(target_order_id={kalshi_order_id}, post_only=True)"
        )
        return hedge_req

    def on_kalshi_hedge_filled(
        self,
        hedge_id: str,
        fill_price: Optional[float] = None,
        fill_size: Optional[float] = None,
    ) -> Optional[SyntheticPairState]:
        """Record execution of the resting maker quote on Kalshi, completing the synthetic pair."""
        hedge_req = self._hedge_requests.get(hedge_id)
        if not hedge_req:
            logger.warning(f"Unknown hedge request ID {hedge_id}")
            return None

        actual_price = fill_price if fill_price is not None else hedge_req.target_price
        actual_size = fill_size if fill_size is not None else hedge_req.target_size

        pair_state = self.get_or_create_pair_state(
            hedge_req.originating_token_id, hedge_req.target_ticker
        )
        pair_state.record_fill(
            venue=Venue.KALSHI,
            side=hedge_req.target_side,
            price=actual_price,
            size=actual_size,
        )

        hedge_req.status = HedgeStatus.FILLED
        self.bus.dispatch(TOPIC_KALSHI_FILLS, hedge_req)
        logger.info(
            f"SYNTHETIC PAIR LOCKED: pair={pair_state.pair_id} locked_sets={pair_state.total_locked_sets} "
            f"locked_profit=${pair_state.locked_profit:.4f} net_delta={pair_state.net_delta}"
        )
        return pair_state

    def get_hedge_request(self, hedge_id: str) -> Optional[CrossVenueHedgeRequest]:
        """Look up a hedge request by ID."""
        return self._hedge_requests.get(hedge_id)

    def get_all_pairs(self) -> Dict[str, SyntheticPairState]:
        """Get all active synthetic pairs."""
        return dict(self._synthetic_pairs)
