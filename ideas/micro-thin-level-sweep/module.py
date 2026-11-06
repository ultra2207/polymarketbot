"""Thin levels are sweep magnets: shrink exposure where the level is thin.

A print reaches our quote only after the level ahead is consumed. When that
level is thin, one aggressive print clears several ticks and the fill is a
sweep; a deep level absorbs the same print and the fill is noise. The gap
between them is a property of depth, not of our FIFO position.

Reads only the DecisionContext; keeps a 200-event deque per side.
"""

from __future__ import annotations

from collections import deque
from typing import Deque

from polymarketbot.strategy.base import DecisionContext, SideActions

WINDOW = 200
MIN_HISTORY = 30
THIN = 0.25
QUEUE_MIN = 0.5


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


class ThinLevelSweep:
    name = "micro-thin-level-sweep"

    def __init__(self) -> None:
        self._bid: Deque[float] = deque(maxlen=WINDOW)
        self._ask: Deque[float] = deque(maxlen=WINDOW)

    def setup(self, market) -> None:
        self._bid.clear()
        self._ask.clear()

    def on_book(self, ctx) -> None:
        return None

    def on_trade(self, ctx) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        self._bid.append(float(ctx.bid_size_at_level))
        self._ask.append(float(ctx.ask_size_at_level))
        if len(self._bid) < MIN_HISTORY:
            return SideActions(reason="thin level: no distribution yet")

        bid_thin = (
            float(ctx.bid_size_at_level) < _quantile(list(self._bid), THIN)
            and float(ctx.bid_queue_ratio) >= QUEUE_MIN
        )
        ask_thin = (
            float(ctx.ask_size_at_level) < _quantile(list(self._ask), THIN)
            and float(ctx.ask_queue_ratio) >= QUEUE_MIN
        )
        if not bid_thin and not ask_thin:
            return SideActions(reason="thin level: both sides acceptable")
        return SideActions(
            bid="SHRINK" if bid_thin else "QUOTE",
            ask="SHRINK" if ask_thin else "QUOTE",
            bid_scale=0.01 if bid_thin else 1.0,
            ask_scale=0.01 if ask_thin else 1.0,
            reason=f"thin level: bid_thin={bid_thin} ask_thin={ask_thin}",
        )


STRATEGY = ThinLevelSweep()
