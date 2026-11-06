"""Arrival bursts are parent orders being sliced; the calm after them is not.

A cluster of prints inside a few seconds is one order arriving in pieces. The
slices keep coming until the parent is done, and the calm that follows the burst
is the safe window. So the defence is timed from the burst itself: stand down for
four times the burst's own duration, then re-enter. Fixed cooldowns are the wrong
shape because a two-print burst and a twenty-print burst are not the same event.
"""

from __future__ import annotations

from collections import deque

from polymarketbot.strategy.base import DecisionContext, SideActions, TradeContext

#: Prints inside this window define a burst.
BURST_WINDOW_SECONDS = 3.0
#: A burst needs this many prints.
BURST_PRINTS = 4
#: Defence lasts this multiple of the observed burst duration.
COOLDOWN_MULTIPLE = 4.0
#: Never sit out longer than this; a runaway burst must not disable the market.
MAX_COOLDOWN_SECONDS = 120.0


class BurstDecayReentry:
    name = "micro-burst-decay-reentry"

    def __init__(self) -> None:
        self._prints: deque[float] = deque()
        self._until = 0.0

    def setup(self, market) -> None:  # noqa: ANN001 - protocol signature
        return None

    def on_book(self, ctx) -> None:  # noqa: ANN001 - protocol signature
        return None

    def on_trade(self, ctx: TradeContext) -> None:
        now = float(ctx.seconds_elapsed)
        self._prints.append(now)
        while self._prints and now - self._prints[0] > BURST_WINDOW_SECONDS:
            self._prints.popleft()
        if len(self._prints) < BURST_PRINTS:
            return
        span = max(1e-6, now - self._prints[0])
        self._until = max(self._until, now + min(MAX_COOLDOWN_SECONDS, COOLDOWN_MULTIPLE * span))

    def decide(self, ctx: DecisionContext) -> SideActions:
        now = float(ctx.seconds_elapsed)
        if now >= self._until:
            return SideActions(bid="QUOTE", ask="QUOTE", reason="calm")
        return SideActions(
            bid="PULL", ask="PULL",
            reason=f"burst decay, {self._until - now:.1f}s left",
        )


STRATEGY = BurstDecayReentry()
