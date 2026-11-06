"""Queue erosion velocity: shrink the side whose FIFO position is being eaten.

The level alone cannot tell a cancel (the level is being abandoned) from a take
(the level is being eaten and we are next). The rate of change of the queue
ratio can: a >= 0.2 fall inside ten events with a stationary mid is depth being
consumed in front of us.

Reads only the DecisionContext; keeps a ten-event deque per side.
"""

from __future__ import annotations

from collections import deque
from typing import Deque

from polymarketbot.strategy.base import DecisionContext, SideActions

WINDOW = 10
MIN_HISTORY = 5
EROSION = 0.20


class QueueErosionVelocity:
    name = "micro-queue-erosion-velocity"

    def __init__(self) -> None:
        self._bid: Deque[float] = deque(maxlen=WINDOW)
        self._ask: Deque[float] = deque(maxlen=WINDOW)
        self._mids: Deque[float] = deque(maxlen=WINDOW)

    def setup(self, market) -> None:
        self._bid.clear()
        self._ask.clear()
        self._mids.clear()

    def on_book(self, ctx) -> None:
        return None

    def on_trade(self, ctx) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        self._bid.append(float(ctx.bid_queue_ratio))
        self._ask.append(float(ctx.ask_queue_ratio))
        self._mids.append(float(ctx.book.mid))
        if len(self._bid) < MIN_HISTORY:
            return SideActions(reason="queue erosion: warming up")

        mid_moved = sum(
            1 for prev, cur in zip(list(self._mids)[:-1], list(self._mids)[1:]) if prev != cur
        )

        def eroded(series: Deque[float]) -> float:
            return float(series[-1]) - max(float(v) for v in series)

        bid_drop, ask_drop = eroded(self._bid), eroded(self._ask)
        bid_action = bid_scale = ask_action = ask_scale = None

        if bid_drop <= -EROSION:
            if mid_moved >= 2:
                bid_action, bid_scale = "PULL", 0.0
            else:
                bid_action, bid_scale = "SHRINK", 0.01
        if ask_drop <= -EROSION:
            if mid_moved >= 2:
                ask_action, ask_scale = "PULL", 0.0
            else:
                ask_action, ask_scale = "SHRINK", 0.01
        if bid_action is None and ask_action is None:
            return SideActions(reason="queue erosion: stable")

        return SideActions(
            bid=bid_action or "QUOTE",
            ask=ask_action or "QUOTE",
            bid_scale=1.0 if bid_scale is None else bid_scale,
            ask_scale=1.0 if ask_scale is None else ask_scale,
            reason=f"queue erosion bid={bid_drop:+.2f} ask={ask_drop:+.2f} mid_moves={mid_moved}",
        )


STRATEGY = QueueErosionVelocity()
