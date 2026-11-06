"""Size both sides by ``1 - |2*mid - 1|``: the price-shaped part of reward and risk.

The claim (``spec.md``): the reward score counts *shares* and the fill cost is
*cents per share*, so a share earns the same score and carries the same
$0.01-per-share adverse move at any price, while the capital committed is
``price`` dollars per share. Reward per committed dollar and cent-risk per
committed dollar are therefore both ``1/price``-shaped, and a share at mid 0.05
risks 20 % of its own price on a one-cent move. Sizing proportional to
``1 - |2*mid - 1|`` spends the same dollars where a share is cheapest in risk
per cent and shrinks in the tails, where the tape's favourite-longshot markout
is worst and strict two-sidedness is required.

Rule: ``scale = clamp(1 - abs(2 * mid - 1), 0.25, 1.0)`` returned on **both**
sides, always as ``QUOTE`` — this module never returns ``PULL`` or ``SHRINK``,
because the claim is about dollars per share, not about withdrawing. The scale
is 1.0 at mid 0.50, 0.25 at mid 0.875 and below (a quarter of the baseline
size, which the engine floors at ``rewards_min_size`` whenever the baseline is
larger), and symmetric about 0.50: mid 0.10 and mid 0.90 both get the floor.

The condition is pure price geometry: it fires on the first ``decide`` call of
every market and needs no level size, no queue ratio, no reward score, no
signal and no state at all, so there is no warm-up window and nothing that can
leave it silently inert. The engine re-quotes on every book update, so a moving
mid simply re-prices the scale on the next step.
"""

from __future__ import annotations

import math

from polymarketbot.strategy.base import (
    BookContext,
    DecisionContext,
    MarketView,
    SideActions,
    TradeContext,
)

#: Lower bound on the scale; below this the rule would be a withdrawal, not a size.
MIN_SCALE = 0.25


class TailPriceSize:
    """Quote both sides, size scaled by ``clamp(1 - |2*mid - 1|, 0.25, 1)``."""

    name = "micro-tail-price-bps"

    def setup(self, market: MarketView) -> None:
        return None

    def on_book(self, ctx: BookContext) -> None:
        return None

    def on_trade(self, ctx: TradeContext) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        mid = float(ctx.book.mid)
        if not math.isfinite(mid):
            # A broken book must not produce a NaN scale (SideActions would reject it).
            return SideActions(reason="tail price size: non-finite mid, full size")

        tail = abs(2.0 * mid - 1.0)
        scale = min(1.0, max(MIN_SCALE, 1.0 - tail))
        return SideActions(
            bid_scale=scale,
            ask_scale=scale,
            reason=f"tail price size: mid={mid:.4f} tail={tail:.2f} scale={scale:.2f}",
        )


STRATEGY = TailPriceSize()
