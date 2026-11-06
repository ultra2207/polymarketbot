"""Skew the size, not the price: shrink the side that would grow |inventory|.

The claim (``spec.md``): inventory risk is ``q * sigma * sqrt(horizon)``. The
engine's answer to that is a reservation-price shift (Avellaneda-Stoikov)
capped at ``max_skew_cents = 0.5`` c, which is half a tick at ``tick_size =
0.01`` and moves *both* quotes: it pushes the adding side either nowhere or a
full tick farther from mid, where the reward score falls faster than the fill
probability. Shifting **size** instead leaves both quotes at the same scored
distance and simply stops adding to the position.

Rule (``u = inventory_shares / max_inventory_shares`` clamped to ``[-1, 1]``):

* ``bid_scale = clamp(1 - u, 0.25, 1.0)`` — long inventory must not buy more;
* ``ask_scale = clamp(1 + u, 0.25, 1.0)`` — short inventory must not sell more.

Both expressions are written unconditionally because they already implement
the one-sided rule: for ``u > 0`` the ask factor exceeds 1 and clamps to 1.0,
for ``u < 0`` the bid factor does. Flat inventory gives 1.0/1.0, ``|u| >= 0.75``
gives the 0.25 floor on the adding side, and the reducing side always stays at
full size so the position can decay without a market order.

This module never returns ``PULL`` and never sets a price. The spec sketched a
pull above ``|u| >= 0.9``; it is deliberately not implemented here. Wave 1
(``docs/replay-results.md``) measured two defensive ideas that forfeited reward
that outweighed the markout they avoided, and with the venue's
``Q_norm = Q_two + min(Q_one, Q_two)/3`` normalisation a one-sided quote earns a
third of its raw score. ``0.25 * size`` floored by the engine at
``rewards_min_size`` is the smallest quote that still scores, which is the most
a sizing rule can withdraw.

The rule reads only ``inventory_shares`` and ``max_inventory_shares``, so it
fires under the baseline's inside-the-spread quotes, where ``*_size_at_level``
and ``*_queue_ratio`` are 0.
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

#: Lower bound on the adding side's scale, so a skew stays a sizing rule rather
#: than a withdrawal.
MIN_SCALE = 0.25
#: Upper bound; the reducing side (and every non-skewed side) rests at full size.
MAX_SCALE = 1.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class InventorySkewSize:
    """Quote both sides; shrink the side that increases ``|inventory|``."""

    name = "micro-inventory-skew-size"

    def setup(self, market: MarketView) -> None:
        return None

    def on_book(self, ctx: BookContext) -> None:
        return None

    def on_trade(self, ctx: TradeContext) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        u = self._inventory_ratio(ctx.inventory_shares, ctx.max_inventory_shares)
        bid_scale = _clamp(1.0 - u, MIN_SCALE, MAX_SCALE)
        ask_scale = _clamp(1.0 + u, MIN_SCALE, MAX_SCALE)
        if u > 0.0:
            skew = f"long {u:.2f}: bid {bid_scale:.2f}"
        elif u < 0.0:
            skew = f"short {u:.2f}: ask {ask_scale:.2f}"
        else:
            skew = "flat: full size both sides"
        return SideActions(
            bid="SHRINK" if bid_scale < MAX_SCALE else "QUOTE",
            ask="SHRINK" if ask_scale < MAX_SCALE else "QUOTE",
            bid_scale=bid_scale,
            ask_scale=ask_scale,
            reason=f"inventory skew size: {skew}",
        )

    @staticmethod
    def _inventory_ratio(
        inventory_shares: float,
        max_inventory_shares: float,
    ) -> float:
        """``inventory_shares / max_inventory_shares`` clamped to ``[-1, 1]``.

        A non-positive or non-finite cap carries no normalisation, so it is read
        as flat inventory and the module quotes full size rather than guessing.
        """
        inv = float(inventory_shares)
        cap = float(max_inventory_shares)
        if not math.isfinite(inv) or not math.isfinite(cap) or cap <= 0.0:
            return 0.0
        return _clamp(inv / cap, -1.0, 1.0)


STRATEGY = InventorySkewSize()
