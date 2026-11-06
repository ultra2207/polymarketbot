"""A stale complement is a free option for the taker.

YES and NO are complements: their mids sum to one, enforced by merge/split
arbitrage. Each book is updated independently, so after a burst on one token its
mid can move two or three cents while the other token has not ticked. Our quote
on the lagging token is then anchored to a fair value the sibling has already
abandoned, and we are the last maker in the condition to reprice - so the next
taker fills us at a price the market has left.

The rule pulls both sides of the lagging token and leaves the leading one
quoting, so the condition is never one-sided. It is deliberately a *price*
dislocation test rather than a flow test: flow-based stand-downs reduce exposure
without improving the fills that remain.
"""

from __future__ import annotations

from polymarketbot.strategy.base import DecisionContext, SideActions

#: A gap of this many ticks between our mid and the sibling-implied mid is a
#: dislocation. One tick is the smallest thing that is not rounding.
GAP_TICKS = 1.0
#: Mid moves above this many ticks in one update are treated as a repricing.
SIBLING_REPRICE_TICKS = 1.0


class ComplementStaleness:
    name = "micro-complement-staleness"

    def __init__(self) -> None:
        self._tokens: tuple[str, ...] = ()
        self._mid: dict[str, float] = {}
        self._tick: dict[str, float] = {}

    def setup(self, market) -> None:  # noqa: ANN001 - protocol signature
        self._tokens = tuple(str(t) for t in getattr(market, "tokens", ()) or ())

    def on_book(self, ctx) -> None:  # noqa: ANN001 - protocol signature
        self._mid[str(ctx.asset_id)] = float(ctx.mid)
        self._tick[str(ctx.asset_id)] = float(ctx.tick_size)

    def on_trade(self, ctx) -> None:  # noqa: ANN001 - protocol signature
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        asset = str(ctx.book.asset_id)
        self._mid[asset] = float(ctx.book.mid)
        self._tick[asset] = float(ctx.book.tick_size)

        siblings = [t for t in self._tokens if t != asset]
        if not siblings:
            return SideActions(bid="QUOTE", ask="QUOTE", reason="no sibling")
        sibling = siblings[0]
        sibling_mid = self._mid.get(sibling)
        if sibling_mid is None:
            return SideActions(bid="QUOTE", ask="QUOTE", reason="sibling unseen")

        implied = 1.0 - sibling_mid
        tick = max(1e-6, float(ctx.book.tick_size))
        gap_ticks = abs(float(ctx.book.mid) - implied) / tick
        if gap_ticks >= GAP_TICKS:
            return SideActions(
                bid="PULL", ask="PULL",
                reason=f"complement stale by {gap_ticks:.1f} ticks",
            )
        return SideActions(bid="QUOTE", ask="QUOTE", reason=f"gap {gap_ticks:.1f} ticks")


STRATEGY = ComplementStaleness()
