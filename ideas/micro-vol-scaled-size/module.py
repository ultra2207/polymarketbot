"""Scale both sides' quote size by 1/sigma, not by the reward pool.

The claim (``spec.md``): the risk carried by a fixed share scales with the
variance of the mid path while the reward we are paid for quoting does not, so a
maker who quotes the same size in a quiet and in a violent condition is
mis-sizing by the ratio of their volatilities.

Estimator (stated so the number is reproducible):

* state is a ``deque`` of the last :data:`WINDOW` observations, each an
  ``(mid, tick_size)`` pair. Consecutive repeats of the *same* mid are not
  appended: an idle book must not dilute the estimate with the event rate, and
  ingesting the same book twice (``on_book`` then ``decide``, as the engine
  does) must not count as two observations. So an observation is a distinct mid
  value, i.e. the price path, not the event rate.
* ``sigma`` is the **population standard deviation** (``ddof=0``) of the
  tick-to-tick mid changes ``(mid_t - mid_{t-1}) / tick_t``, in **ticks**,
  over those observations.
* a window with fewer than :data:`MIN_OBSERVATIONS` distinct mids is warm-up
  and quotes at full size.

Rule: ``scale = clamp(TARGET_SIGMA / sigma, MIN_SCALE, 1.0)`` with
``TARGET_SIGMA = 1.0`` tick — one tick of typical move is "normal", more than
that shrinks both sides, and the scale never goes below ``MIN_SCALE`` (0.25) so
the idea stays a sizing rule rather than a withdrawal. Both sides always get
the *same* scale and both sides stay ``QUOTE``; this module never returns
``PULL``. The engine floors any positive size at ``rewards_min_size`` and
re-quotes on every book update, so ``SHRINK`` at 0.25 rests at the reward
minimum while still scoring.

The rule needs no level size and no queue ratio, so it fires under the
baseline's inside-the-spread quotes (where both of those are 0); it reads only
``book.mid`` and ``book.tick_size``.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional

from polymarketbot.strategy.base import (
    BookContext,
    DecisionContext,
    MarketView,
    SideActions,
    TradeContext,
)

#: Observations kept for the estimate (distinct mids).
WINDOW = 100
#: Distinct mids needed before sigma is trusted; below this we quote full size.
MIN_OBSERVATIONS = 30
#: One tick of typical move is the neutral point of the ratio.
TARGET_SIGMA = 1.0
#: Lower bound on the scale: never below a quarter of the target size.
MIN_SCALE = 0.25
#: Fallback tick when the book reports nothing usable.
DEFAULT_TICK = 0.01


class VolScaledSize:
    """Quote both sides, size scaled by ``clamp(1 tick / sigma, 0.25, 1)``."""

    name = "micro-vol-scaled-size"

    def __init__(self) -> None:
        self._mids: deque[tuple[float, float]] = deque(maxlen=WINDOW)

    def setup(self, market: MarketView) -> None:
        self._mids.clear()

    def on_book(self, ctx: BookContext) -> None:
        self._observe(ctx.mid, ctx.tick_size)

    def on_trade(self, ctx: TradeContext) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        # The engine calls ``on_book`` and then ``decide`` for the same update;
        # ``_observe`` is idempotent so the second call adds nothing.
        self._observe(ctx.book.mid, ctx.book.tick_size)

        sigma = self._sigma_ticks()
        if sigma is None:
            return SideActions(
                reason=f"vol scaled size: warm-up, {len(self._mids)}/{MIN_OBSERVATIONS} observations",
            )

        if sigma <= 0.0 or not math.isfinite(sigma):
            scale = 1.0
        else:
            scale = min(1.0, max(MIN_SCALE, TARGET_SIGMA / sigma))
        return SideActions(
            bid_scale=scale,
            ask_scale=scale,
            reason=f"vol scaled size: sigma={sigma:.2f}t scale={scale:.2f}",
        )

    def _observe(self, mid: float, tick_size: float) -> None:
        mid = float(mid)
        if not math.isfinite(mid):
            return
        tick = float(tick_size)
        if not math.isfinite(tick) or tick <= 0.0:
            tick = DEFAULT_TICK
        if self._mids and self._mids[-1][0] == mid:
            return  # same price seen again: not a new observation, not a zero change
        self._mids.append((mid, tick))

    def _sigma_ticks(self) -> Optional[float]:
        """Population stdev of tick-to-tick changes; ``None`` during warm-up."""
        if len(self._mids) < MIN_OBSERVATIONS:
            return None
        values = list(self._mids)
        deltas: list[float] = []
        prev_mid, prev_tick = values[0]
        for mid, tick in values[1:]:
            step_tick = tick if tick > 0.0 else prev_tick
            deltas.append((mid - prev_mid) / step_tick)
            prev_mid, prev_tick = mid, step_tick
        if not deltas:
            return None
        mean = sum(deltas) / len(deltas)
        variance = sum((d - mean) ** 2 for d in deltas) / len(deltas)
        return math.sqrt(variance)


STRATEGY = VolScaledSize()
