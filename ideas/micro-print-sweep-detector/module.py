"""A print that clears half a level is a sweep, not noise.

The engine's size gate is absolute (850 shares), which means nothing across
markets whose minimum qualifying size ranges from 20 to 1,000 shares. Size
*relative to the level it just consumed* is scale-free: a print that ate half
the queue is aggression that usually continues into the next level, a print that
nibbled is noise. Conditioning on the tagger's size against displayed depth is
the standard price-impact decomposition (Cont, Kukanov & Stoikov 2014) and is the
scale-free form of the toxin filter that already exists in the live runner.

The rule is deliberately crude - it buys time, it does not forecast.
"""

from __future__ import annotations

from polymarketbot.strategy.base import (
    BookContext,
    DecisionContext,
    SideActions,
    TradeContext,
)

#: A print consuming this fraction of the resting level counts as a sweep.
CONSUMED_FRACTION = 0.5
#: How many decisions to stay defensive after one. Ten decisions is roughly one
#: book update cycle on the markets this is aimed at.
COOLDOWN = 10
#: Mid movement, in cents, that turns "shrink" into "pull the side that was hit".
REVERSAL_CENTS = 1.0


class PrintSweepDetector:
    name = "micro-print-sweep-detector"

    def __init__(self) -> None:
        self._cooldown = 0
        self._hit_side: str | None = None
        self._mid_at_sweep: float | None = None
        self._last_level = {"BUY": 0.0, "SELL": 0.0}
        self._last_mid = 0.5

    def setup(self, market) -> None:  # noqa: ANN001 - protocol signature
        return None

    def on_book(self, ctx: BookContext) -> None:
        self._last_mid = float(ctx.mid)
        self._last_level["BUY"] = float(ctx.bid_size_at_level) if hasattr(ctx, "bid_size_at_level") else 0.0

    def on_trade(self, ctx: TradeContext) -> None:
        taker = str(ctx.side).upper()
        # A taker SELL consumes the bid; a taker BUY consumes the ask.
        resting_side = "BUY" if taker == "SELL" else "SELL"
        level = self._last_level.get(resting_side, 0.0)
        if level <= 0.0:
            return
        if float(ctx.size) / level < CONSUMED_FRACTION:
            return
        self._cooldown = COOLDOWN
        self._hit_side = resting_side
        self._mid_at_sweep = self._last_mid

    def decide(self, ctx: DecisionContext) -> SideActions:
        self._last_level["BUY"] = float(ctx.bid_size_at_level)
        self._last_level["SELL"] = float(ctx.ask_size_at_level)
        self._last_mid = float(ctx.book.mid)

        if self._cooldown <= 0:
            return SideActions(bid="QUOTE", ask="QUOTE", reason="no sweep")

        self._cooldown -= 1
        moved_cents = (float(ctx.book.mid) - (self._mid_at_sweep or float(ctx.book.mid))) * 100.0
        # The book moved against the direction the sweep was pushing: the next
        # slice is likely to keep going, so stop showing a resting quote there.
        if self._hit_side == "BUY" and moved_cents <= -REVERSAL_CENTS:
            return SideActions(
                bid="PULL", ask="SHRINK", ask_scale=0.01,
                reason=f"sweep on bid, mid -{abs(moved_cents):.1f}c",
            )
        if self._hit_side == "SELL" and moved_cents >= REVERSAL_CENTS:
            return SideActions(
                bid="SHRINK", ask="PULL", bid_scale=0.01,
                reason=f"sweep on ask, mid +{moved_cents:.1f}c",
            )
        return SideActions(
            bid="SHRINK", ask="SHRINK", bid_scale=0.01, ask_scale=0.01,
            reason=f"sweep cooldown {self._cooldown}",
        )


STRATEGY = PrintSweepDetector()
