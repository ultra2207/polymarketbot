"""Depth that refills is absorbed flow; depth that empties is informed flow.

The touch tells you which one you are facing without any forecasting. A level
that shrinks and then rebuilds was somebody feeding a large buyer who was happy
to be filled - the maker earned. A level that shrinks monotonically is a taker
who is still going. Order-flow imbalance counts steps, so it cannot see this; the
distinction lives in whether the size came back.
"""

from __future__ import annotations

from polymarketbot.strategy.base import DecisionContext, SideActions

#: A refill of at least this share of what was eaten counts as absorbed.
REFILL_FRACTION = 0.6
#: Consecutive shrinking snapshots before a level is called informed.
EMPTYING_STREAK = 3


class AbsorbedFlow:
    name = "micro-absorbed-flow"

    def __init__(self) -> None:
        self._prev = {"bid": 0.0, "ask": 0.0}
        self._peak = {"bid": 0.0, "ask": 0.0}
        self._streak = {"bid": 0, "ask": 0}
        self._absorbed = {"bid": True, "ask": True}

    def setup(self, market) -> None:  # noqa: ANN001 - protocol signature
        return None

    def on_book(self, ctx) -> None:  # noqa: ANN001 - protocol signature
        return None

    def on_trade(self, ctx) -> None:  # noqa: ANN001 - protocol signature
        return None

    def _update(self, side: str, level: float) -> None:
        prev = self._prev[side]
        peak = self._peak[side]
        if level > prev:
            eaten = peak - prev
            if eaten > 0 and (level - prev) >= REFILL_FRACTION * eaten:
                self._streak[side] = 0
                self._absorbed[side] = True
            peak = max(peak, level)
        elif level < prev:
            self._streak[side] += 1
            if self._streak[side] >= EMPTYING_STREAK:
                self._absorbed[side] = False
        else:
            self._streak[side] = 0
        self._prev[side] = level
        self._peak[side] = peak if level >= prev else max(level, peak)

    def decide(self, ctx: DecisionContext) -> SideActions:
        self._update("bid", float(ctx.bid_size_at_level))
        self._update("ask", float(ctx.ask_size_at_level))
        bid = "QUOTE" if self._absorbed["bid"] else "PULL"
        ask = "QUOTE" if self._absorbed["ask"] else "PULL"
        return SideActions(
            bid=bid, ask=ask,
            reason=f"bid {self._streak['bid']} ask {self._streak['ask']}",
        )


STRATEGY = AbsorbedFlow()
