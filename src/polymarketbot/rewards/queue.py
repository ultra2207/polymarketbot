"""Order book depth, queue dynamics, and realistic execution simulation.

Models:
1. Price-Time Priority (FIFO) queue ahead of our resting limit orders.
2. Competing order book depth at and around qualifying ticks.
3. Queue depletion via market trade volume and competing cancellations.
4. Fill execution when trades cross limit quotes.
5. In-flight adverse selection markouts (60s / 300s post-fill price drift).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class RestingOrder:
    """A resting order tracked in the queue."""

    order_id: str
    side: str  # "BUY" or "SELL"
    outcome: str  # "YES" or "NO"
    price: float
    size: float
    queue_ahead: float  # Quantity of competing volume in front of this order
    placed_at_epoch: int
    duration_seconds: float = 0.0
    filled_size: float = 0.0

    @property
    def remaining_size(self) -> float:
        return max(0.0, self.size - self.filled_size)

    @property
    def is_filled(self) -> bool:
        return self.remaining_size <= 1e-6


@dataclass
class FillExecution:
    """Record of a trade fill against our quote."""

    epoch: int
    order_id: str
    side: str
    outcome: str
    fill_price: float
    fill_size: float
    mid_at_fill: float
    is_cross: bool  # True if filled because market trade crossed our price
    spread_captured: float  # (price - mid) for sell, (mid - price) for buy
    adverse_selection_loss: float = 0.0  # Markout loss if price moves against fill


class OrderBookQueueSimulator:
    """Simulates realistic queue dynamics and fills against competing book depth."""

    def __init__(
        self,
        cancellation_rate_per_min: float = 0.12,  # 12% queue churn per minute
        touch_fill_rate: float = 0.20,  # Baseline passive fill probability at touch
    ) -> None:
        self.cancellation_rate = cancellation_rate_per_min
        self.touch_fill_rate = touch_fill_rate
        self.active_orders: Dict[str, RestingOrder] = {}
        self.fills_history: List[FillExecution] = []

    def sync_quote(
        self,
        order_id: str,
        side: str,
        outcome: str,
        price: float,
        size: float,
        competing_depth_at_price: float,
        current_epoch: int,
    ) -> RestingOrder:
        """Update or insert a resting order, managing queue priority.

        If price matches existing order: retains queue position and increases duration.
        If price changed: cancels old order (queue priority lost) and joins back of queue.
        """
        existing = self.active_orders.get(order_id)
        if existing and abs(existing.price - price) < 1e-4 and existing.side == side:
            # Requote at same price: keep queue position, advance time
            existing.duration_seconds += 60.0
            existing.size = size
            # Natural cancellation decay of queue in front of us
            existing.queue_ahead = max(0.0, existing.queue_ahead * (1.0 - self.cancellation_rate))
            return existing

        # New order or price change: joins back of queue behind competing depth
        new_order = RestingOrder(
            order_id=order_id,
            side=side,
            outcome=outcome,
            price=price,
            size=size,
            queue_ahead=max(0.0, competing_depth_at_price),
            placed_at_epoch=current_epoch,
            duration_seconds=60.0,
        )
        self.active_orders[order_id] = new_order
        return new_order

    def cancel_order(self, order_id: str) -> None:
        self.active_orders.pop(order_id, None)

    def process_trade(
        self,
        trade_side: str,  # Taker direction: "BUY" or "SELL"
        trade_price: float,
        trade_volume: float,
        current_mid: float,
        next_mid_60s: float,
        current_epoch: int,
    ) -> List[FillExecution]:
        """Simulate trade execution against resting quotes in the book.

        When taker BUYs:
          Hits resting asks (SELL orders).
          If trade_price >= our ask_price:
            - If trade_price > our ask: crosses through our quote -> instant fill.
            - If trade_price == our ask: consumes queue_ahead first, then fills us.

        When taker SELLs:
          Hits resting bids (BUY orders).
          If trade_price <= our bid_price:
            - If trade_price < our bid: crosses through our quote -> instant fill.
            - If trade_price == our bid: consumes queue_ahead first, then fills us.
        """
        fills = []

        for order_id, order in list(self.active_orders.items()):
            if order.is_filled:
                continue

            filled_qty = 0.0
            is_cross = False

            if order.side == "BUY" and trade_side == "SELL":
                # Taker sells into our bid
                if trade_price < order.price - 1e-4:
                    # Trade crossed inside our bid! Instant aggressive fill
                    is_cross = True
                    filled_qty = min(order.remaining_size, trade_volume)
                    order.queue_ahead = 0.0
                elif abs(trade_price - order.price) <= 0.0051:
                    # Trade at our price level
                    if trade_volume > order.queue_ahead:
                    	surplus = trade_volume - order.queue_ahead
                    	filled_qty = min(order.remaining_size, surplus)
                    	order.queue_ahead = 0.0
                    else:
                    	order.queue_ahead -= trade_volume

            elif order.side == "SELL" and trade_side == "BUY":
                # Taker buys from our ask
                if trade_price > order.price + 1e-4:
                    # Trade crossed above our ask! Instant aggressive fill
                    is_cross = True
                    filled_qty = min(order.remaining_size, trade_volume)
                    order.queue_ahead = 0.0
                elif abs(trade_price - order.price) <= 0.0051:
                    # Trade at our price level
                    if trade_volume > order.queue_ahead:
                    	surplus = trade_volume - order.queue_ahead
                    	filled_qty = min(order.remaining_size, surplus)
                    	order.queue_ahead = 0.0
                    else:
                    	order.queue_ahead -= trade_volume

            if filled_qty > 0.0:
                order.filled_size += filled_qty

                # Calculate spread captured and adverse selection markout
                if order.side == "BUY":
                    spread_captured = (current_mid - order.price) * filled_qty
                    # If mid drops below fill price, we suffer adverse selection
                    markout_drop = max(0.0, order.price - next_mid_60s)
                    adverse_loss = markout_drop * filled_qty
                else:
                    spread_captured = (order.price - current_mid) * filled_qty
                    # If mid rises above fill price, we suffer adverse selection
                    markout_rise = max(0.0, next_mid_60s - order.price)
                    adverse_loss = markout_rise * filled_qty

                fill_event = FillExecution(
                    epoch=current_epoch,
                    order_id=order_id,
                    side=order.side,
                    outcome=order.outcome,
                    fill_price=order.price,
                    fill_size=round(filled_qty, 2),
                    mid_at_fill=current_mid,
                    is_cross=is_cross,
                    spread_captured=round(spread_captured, 4),
                    adverse_selection_loss=round(adverse_loss, 4),
                )
                fills.append(fill_event)
                self.fills_history.append(fill_event)

                if order.is_filled:
                    del self.active_orders[order_id]

        return fills
