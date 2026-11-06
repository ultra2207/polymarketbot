"""Defend the side that was just hit, for the next five decisions.

The protocol has no fill callback: ``PaperTradingEngine`` simulates fills on its
own virtual quotes and never tells the module one happened, and ``decide`` does
not expose a position delta the module could diff.  The only fill evidence in
the context is the print stream (``on_trade``) plus the last top of book
(``on_book``).  Our quotes rest inside the spread, one tick from the touch, so a
print within one tick of the touch on the matching side is the earliest visible
sign that a sweep has reached our quote.  That is the proxy this module arms on:

* a SELL print at or through ``best_bid`` (+1 tick tolerance) marks the **bid**
  as just hit — the buyer side of our quote was taken;
* a BUY print at or through ``best_ask`` (-1 tick tolerance) marks the **ask**
  as just hit.

It is a proxy, deliberately: the module cannot distinguish "the print reached
our price" from "the print stopped one tick short".  It reacts to the venue
event that is closest to a fill, which no price-only signal encodes.

While armed, the hit side is ``SHRINK`` at scale 0.01 (the engine floors a
shrunk quote at ``rewards_min_size``) and the untouched side stays at full
scale, keeping its share of the reward pool.  The window is five ``decide``
calls and closes by itself; a later hit restarts it on the side hit last.

State is one last-touch tuple and one window per asset, so it stays bounded by
the number of tokens the engine quotes (two per condition), not by the tape.

Reads only ``MarketView`` / ``BookContext`` / ``TradeContext`` /
``DecisionContext``; no clock, no files, no network.
"""

from __future__ import annotations

from polymarketbot.strategy.base import (
    BookContext,
    DecisionContext,
    MarketView,
    SideActions,
    TradeContext,
)

#: ``decide`` calls the hit side stays shrunk before both sides quote again.
DEFENCE_CALLS = 5
#: Size scale for the defended side; the engine raises it to the reward floor.
DEFENCE_SCALE = 0.01


class PostFillDefence:
    name = "micro-post-fill-defence"

    def __init__(self) -> None:
        self._touch: dict[str, tuple[float, float, float]] = {}
        self._window: dict[str, tuple[str, int]] = {}

    def setup(self, market: MarketView) -> None:
        self._touch.clear()
        self._window.clear()

    def on_book(self, ctx: BookContext) -> None:
        self._touch[ctx.asset_id] = (
            float(ctx.best_bid),
            float(ctx.best_ask),
            max(float(ctx.tick_size), 1e-9),
        )

    def on_trade(self, ctx: TradeContext) -> None:
        touch = self._touch.get(ctx.asset_id)
        if touch is None:
            return
        best_bid, best_ask, tick = touch
        price = float(ctx.price)
        side = str(ctx.side).strip().upper()
        if side == "SELL":
            # A synthetic 0/1 touch is not a quote; a print against it says
            # nothing about our resting bid.
            if 0.0 < best_bid < 1.0 and price <= best_bid + tick:
                self._window[ctx.asset_id] = ("bid", DEFENCE_CALLS)
        elif side == "BUY":
            if 0.0 < best_ask < 1.0 and price >= best_ask - tick:
                self._window[ctx.asset_id] = ("ask", DEFENCE_CALLS)

    def decide(self, ctx: DecisionContext) -> SideActions:
        asset = ctx.book.asset_id
        window = self._window.get(asset)
        if window is None:
            return SideActions(reason="post-fill: quoting both sides")

        side, left = window
        left -= 1
        if left > 0:
            self._window[asset] = (side, left)
        else:
            del self._window[asset]

        remaining = left + 1
        if side == "bid":
            return SideActions(
                bid="SHRINK",
                bid_scale=DEFENCE_SCALE,
                ask="QUOTE",
                ask_scale=1.0,
                reason=f"post-fill: bid hit, defence {remaining}/{DEFENCE_CALLS}",
            )
        return SideActions(
            bid="QUOTE",
            bid_scale=1.0,
            ask="SHRINK",
            ask_scale=DEFENCE_SCALE,
            reason=f"post-fill: ask hit, defence {remaining}/{DEFENCE_CALLS}",
        )


STRATEGY = PostFillDefence()
