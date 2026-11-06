"""Quote through bid-ask bounce, shrink into a one-way repricing.

The top of book moves for two different reasons, and the maker's edge has the
opposite sign in each.  When the mid alternates between two prices with no
direction, takers are bouncing off both touches and a resting quote collects
the spread; that is the classic bid-ask bounce, and the mid's *sign pattern*
identifies it even when its signed mean is zero.  When the mid steps the same
way update after update, real information is being repriced one tick at a
time, and a quote on the side the path runs into is stale; that side's fill
markout is the one that pays for everything.

The statistic is the flip fraction over the last ``WINDOW`` mid changes per
asset: the share of consecutive **non-zero** changes whose signs differ.
``on_book`` records only the sign of each change (+1, -1, or 0 when the mid is
unchanged), so the buffer is 30 small ints per asset regardless of tape
length.  In ``decide``:

* fewer than ``MIN_NON_ZERO`` non-zero changes buffered -> ``QUOTE`` both
  (warm-up, or a book that is not moving at all);
* ``flip_frac >= 0.7`` -> ``QUOTE`` both at full scale; the bounce regime is
  where spread capture is the edge, so there is no reason to give up size;
* ``flip_frac <= 0.3`` -> ``SHRINK`` the side the run threatens at scale 0.01
  and keep the other at full scale: a down-trend threatens the **bid** (the
  path falls through a resting bid), an up-trend threatens the **ask**.  The
  direction is the last non-zero change in the buffer;
* anything between -> ``QUOTE`` both.

``SHRINK`` at 0.01, not ``PULL``: the replay results in
``docs/replay-results.md`` show a pull forfeits more reward than the markout
it avoids on this slice, while the engine floors a shrunk quote at
``rewards_min_size`` and leaves the other side's reward share intact.

This rule reads only ``book.mid``; it does not use ``queue_ratio`` or
``size_at_level``, so it fires with the baseline's inside-the-spread quotes
(where both of those are zero).  No clock, no files, no network, no engine
internals.
"""

from __future__ import annotations

from collections import deque

from polymarketbot.strategy.base import (
    BookContext,
    DecisionContext,
    MarketView,
    SideActions,
    TradeContext,
)

#: Number of most recent mid changes the flip statistic looks at.
WINDOW = 30
#: Non-zero changes required before the statistic is trusted at all.
MIN_NON_ZERO = 10
#: At or above this flip fraction the book is bouncing, not repricing.
OSCILLATION_FLIPS = 0.7
#: At or below this flip fraction the book is repricing in one direction.
TREND_FLIPS = 0.3
#: Size scale for the threatened side; the engine raises it to the reward floor.
SHRINK_SCALE = 0.01

_UP = 1
_DOWN = -1
_FLAT = 0


class MicroOscillationVsTrend:
    """Size quotes by whether recent mid changes alternate or run one way."""

    name = "micro-oscillation-vs-trend"

    def __init__(self) -> None:
        # Per asset: last observed mid, and the signs of the changes since.
        self._last_mid: dict[str, float] = {}
        self._signs: dict[str, deque[int]] = {}

    def setup(self, market: MarketView) -> None:
        self._last_mid.clear()
        self._signs.clear()

    def on_book(self, ctx: BookContext) -> None:
        asset = ctx.asset_id
        mid = float(ctx.mid)
        last = self._last_mid.get(asset)
        self._last_mid[asset] = mid
        if last is None:
            return
        if mid > last:
            sign = _UP
        elif mid < last:
            sign = _DOWN
        else:
            sign = _FLAT
        self._signs.setdefault(asset, deque(maxlen=WINDOW)).append(sign)

    def on_trade(self, ctx: TradeContext) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        signs = self._signs.get(ctx.book.asset_id)
        non_zero = [sign for sign in (signs or ()) if sign != _FLAT]
        if len(non_zero) < MIN_NON_ZERO:
            return SideActions(
                reason=f"oscillation: warm-up {len(non_zero)}/{MIN_NON_ZERO} changes",
            )

        pairs = len(non_zero) - 1
        flips = sum(1 for a, b in zip(non_zero, non_zero[1:]) if a != b) / pairs

        if flips >= OSCILLATION_FLIPS:
            return SideActions(
                reason=f"oscillation: flips {flips:.2f} >= {OSCILLATION_FLIPS}, bounce",
            )
        if flips <= TREND_FLIPS:
            if non_zero[-1] < 0:
                return SideActions(
                    bid="SHRINK",
                    bid_scale=SHRINK_SCALE,
                    ask="QUOTE",
                    ask_scale=1.0,
                    reason=f"oscillation: flips {flips:.2f} <= {TREND_FLIPS}, down-trend threatens bid",
                )
            return SideActions(
                bid="QUOTE",
                bid_scale=1.0,
                ask="SHRINK",
                ask_scale=SHRINK_SCALE,
                reason=f"oscillation: flips {flips:.2f} <= {TREND_FLIPS}, up-trend threatens ask",
            )
        return SideActions(
            reason=f"oscillation: flips {flips:.2f} between {TREND_FLIPS} and {OSCILLATION_FLIPS}",
        )


STRATEGY = MicroOscillationVsTrend()
