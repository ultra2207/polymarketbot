"""Ramp the inventory cap instead of letting the engine drop a whole side.

The spec (``spec.md``): the engine's cap is a binary stop — at
``|inventory| >= max_inventory_shares`` it zeroes the adding side, so a
two-sided book silently becomes one-sided. Under the venue's
``Q_norm = max(min(Q1, Q2), max(Q1, Q2)/3)`` that forfeits two-thirds of the
score, and outside ``[0.10, 0.90]`` it forfeits all of it: a discrete reward
cliff paid for a marginal risk reduction that a size ramp delivers smoothly.

Rule (``u = |inventory_shares| / max_inventory_shares``, clamped to ``[0, 1]``):

* ``u < 0.6`` — quote both sides at full size;
* ``u >= 0.6`` — the side that would *increase* ``|inventory|`` is scaled by
  ``clamp((1 - u) / 0.4, 0.25, 1.0)``, the reducing side stays at 1.0.

The floor is 0.25, and the module never returns ``PULL`` and never sets a
price: the engine floors any positive quote at ``rewards_min_size``, so 0.25 is
the smallest size that still scores while the position can always decay
through the other side. The spec's ``PULL`` at ``|u| >= 0.85`` is deliberately
not implemented — it trades the one-side cliff for the eligibility screen, and
Wave 2 (``docs/replay-results.md``) measured that shrinking total size loses
reward linearly.

Trigger: fires on any replay that carries inventory past 60 % of the cap,
which the engine reaches whenever one side fills repeatedly; no signal is
needed, so the rule cannot be silently inert. A missing (non-positive or
non-finite) cap is read as flat inventory and both sides quote full size.
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

#: Fraction of the cap at which the ramp starts. Below it the engine's own
#: risk is small enough that cutting size only loses reward score.
RAMP_START = 0.6
#: Lower bound on the adding side's scale, so the ramp never becomes a pull.
MIN_SCALE = 0.25
#: Full size: the reducing side and every non-ramped side rest here.
MAX_SCALE = 1.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class CapBreadthPreserve:
    """Ramp the adding side above 60 % of the cap; keep both sides resting."""

    name = "micro-cap-breadth-preserve"

    def setup(self, market: MarketView) -> None:
        return None

    def on_book(self, ctx: BookContext) -> None:
        return None

    def on_trade(self, ctx: TradeContext) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        u = self._inventory_ratio(ctx.inventory_shares, ctx.max_inventory_shares)
        add_scale = (
            MAX_SCALE
            if u < RAMP_START
            else _clamp((1.0 - u) / (1.0 - RAMP_START), MIN_SCALE, MAX_SCALE)
        )
        if add_scale >= MAX_SCALE:
            return SideActions(
                reason=f"cap breadth preserve: u={u:.2f} at full size both sides",
            )

        if ctx.inventory_shares > 0.0:
            bid_scale, ask_scale = add_scale, MAX_SCALE
            side = "bid"
        else:
            bid_scale, ask_scale = MAX_SCALE, add_scale
            side = "ask"
        return SideActions(
            bid="SHRINK" if bid_scale < MAX_SCALE else "QUOTE",
            ask="SHRINK" if ask_scale < MAX_SCALE else "QUOTE",
            bid_scale=bid_scale,
            ask_scale=ask_scale,
            reason=f"cap breadth preserve: u={u:.2f}, shrink {side} to {add_scale:.2f}",
        )

    @staticmethod
    def _inventory_ratio(
        inventory_shares: float,
        max_inventory_shares: float,
    ) -> float:
        """``|inventory_shares| / max_inventory_shares`` clamped to ``[0, 1]``.

        A non-positive or non-finite cap carries no normalisation, so it is read
        as flat inventory and both sides quote full size rather than guessing.
        A position beyond the cap keeps the floor rather than extrapolating
        through it.
        """
        inv = float(inventory_shares)
        cap = float(max_inventory_shares)
        if not math.isfinite(inv) or not math.isfinite(cap) or cap <= 0.0:
            return 0.0
        return min(abs(inv) / cap, 1.0)


STRATEGY = CapBreadthPreserve()
