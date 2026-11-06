"""Quote where the competition is thin, not where the pool is big.

The payout is ``pool * S/(S + C)``: our score over our score plus the
competitors'. It is linear in our score and steep only where ``C`` is small, so
the same shares earn more in a market with a light book than a heavy one. The
reward is therefore a *rank*, not a level - and the rank changes during the day
as other makers arrive and leave.

The rule keeps a rolling window of the competing score this market shows and
stands aside above the 75th percentile, returning below the 25th. Hysteresis
matters: entering and leaving at one threshold would churn the quote, and a
quote that does not rest earns nothing.
"""

from __future__ import annotations

from collections import deque

from polymarketbot.strategy.base import DecisionContext, SideActions

#: Observations kept for the percentile estimate.
WINDOW = 240
#: Below this many observations the window is not informative and we quote.
WARMUP = 30
ENTER_QUANTILE = 0.25
LEAVE_QUANTILE = 0.75


class MakerVacancy:
    name = "micro-maker-vacancy"

    def __init__(self) -> None:
        self._window: deque[float] = deque(maxlen=WINDOW)
        self._in = True

    def setup(self, market) -> None:  # noqa: ANN001 - protocol signature
        return None

    def on_book(self, ctx) -> None:  # noqa: ANN001 - protocol signature
        return None

    def on_trade(self, ctx) -> None:  # noqa: ANN001 - protocol signature
        return None

    def _quantile(self, q: float) -> float | None:
        if len(self._window) < WARMUP:
            return None
        ordered = sorted(self._window)
        idx = min(len(ordered) - 1, int(q * (len(ordered) - 1)))
        return ordered[idx]

    def decide(self, ctx: DecisionContext) -> SideActions:
        comp = float(ctx.book.competing_score)
        self._window.append(comp)

        low = self._quantile(ENTER_QUANTILE)
        high = self._quantile(LEAVE_QUANTILE)
        if low is None or high is None:
            return SideActions(bid="QUOTE", ask="QUOTE", reason="warm-up")

        if self._in and comp > high:
            self._in = False
        elif not self._in and comp < low:
            self._in = True

        if self._in:
            return SideActions(bid="QUOTE", ask="QUOTE", reason=f"vacant ({comp:.0f})")
        return SideActions(bid="PULL", ask="PULL", reason=f"crowded ({comp:.0f})")


STRATEGY = MakerVacancy()
