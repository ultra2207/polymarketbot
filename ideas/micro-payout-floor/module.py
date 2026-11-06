"""The $1 payout floor makes a small-share market a zero-reward game.

The venue pays nothing on a market whose day's payout is under $1. When our
projected *daily* payout at the current share is below that floor, the reward
term is exactly zero and the only remaining term is adverse selection on the
fills we collect. Pulling both sides then costs nothing in reward and removes
the markout.

The projection is computed live from the context rather than from a prepared
table: `rate_per_day * share` is the projected daily payout, and `share` is our
score over ours plus the competitors'. A hysteresis band (resume only above
$1.25) stops the module flapping at the floor.
"""

from __future__ import annotations

from polymarketbot.strategy.base import DecisionContext, MarketView, SideActions

FLOOR_USD = 1.0
RESUME_USD = 1.25


class PayoutFloor:
    name = "micro-payout-floor"

    def __init__(self) -> None:
        self._withdrawn = False
        self._rate = 0.0

    def setup(self, market: MarketView) -> None:
        self._withdrawn = False
        self._rate = float(market.rate_per_day)

    def on_book(self, ctx) -> None:
        return None

    def on_trade(self, ctx) -> None:
        return None

    def _projected_daily(self, ctx: DecisionContext) -> float | None:
        ours = float(ctx.bid_score) + float(ctx.ask_score)
        if ours <= 0.0:
            return None  # nothing resting: the share is undefined, not zero
        competing = float(ctx.book.competing_score)
        share = ours / max(ours + competing, 1e-9)
        rate = float(ctx.rate_per_day or self._rate)
        return rate * share

    def decide(self, ctx: DecisionContext) -> SideActions:
        projected = self._projected_daily(ctx)
        if projected is None:
            return SideActions(reason="payout floor: no resting score yet")
        if self._withdrawn:
            if projected < RESUME_USD:
                return SideActions(
                    bid="PULL", ask="PULL", bid_scale=0.0, ask_scale=0.0,
                    reason=f"payout floor: ${projected:.2f}/day < ${RESUME_USD:.2f}",
                )
            self._withdrawn = False
        elif projected < FLOOR_USD:
            self._withdrawn = True
            return SideActions(
                bid="PULL", ask="PULL", bid_scale=0.0, ask_scale=0.0,
                reason=f"payout floor: ${projected:.2f}/day < ${FLOOR_USD:.2f}",
            )
        return SideActions(reason=f"payout floor: ${projected:.2f}/day")


STRATEGY = PayoutFloor()
