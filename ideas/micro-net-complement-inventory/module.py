"""Quote the pair, not the leg: net a condition's two inventories before sizing.

The claim (``spec.md``): the two tokens of one condition are claims on the same
``$1``, so the risk that matters is the *sum* of the two positions, not either
one alone. A maker who is long YES and long NO holds a completed pair and is
riskless, while a maker who is long YES and short NO carries a directional
position on both legs at once. The engine tracks positions per token and caps
each side at ``max_inventory_shares`` independently, so the first case is
penalised twice for risk it does not have and the second is under-penalised.

Rule (``u = inventory_shares / max(max_inventory_shares, 1)``, and ``sib =
signals["sibling_inventory_frac"]``):

* signal absent or not finite -> ``QUOTE``/``QUOTE``. The module is inert: a
  missing signal is never read as zero inventory, because that would shrink a
  side on absent data. The harness must supply ``sibling_inventory_frac`` for
  this module to do anything at all.
* ``sign(u) == sign(sib)`` and both non-zero -> the two legs point the same way,
  so the shared risk is the direction itself: the side that *adds* to that
  direction is ``SHRINK`` at ``0.5`` (bid when both fractions are positive, ask
  when both are negative); the reducing side stays at full size so the position
  can decay.
* opposite signs, or either fraction flat -> the pair is already completed or
  turned around, so both sides ``QUOTE`` at full scale.

The signal is the sibling token's inventory divided by its cap, in ``[-1, 1]``,
prepared per asset by the harness (one module instance sees one token at a
time). Only its sign is used, so a stale-but-persisted value cannot scale the
size; the 0.5 cut is flat by design and is the smallest defensible withdrawal
given that the engine floors any positive size at ``rewards_min_size``.

The rule reads only ``inventory_shares``, ``max_inventory_shares`` and that one
signal. It never reads ``*_size_at_level``/``*_queue_ratio`` (zero for the
baseline's inside-the-spread quotes), never sets a price, and never pulls.
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

#: Harness-prepared signal: the sibling token's inventory / its cap, in [-1, 1].
SIBLING_SIGNAL = "sibling_inventory_frac"
#: Scale applied to the side that grows the shared directional risk.
SHRINK_SCALE = 0.5
#: Full scale for every side that is not growing that risk.
FULL_SCALE = 1.0


def _sign(value: float) -> int:
    """``-1``, ``0`` or ``1``; a non-finite value has no direction."""
    if not math.isfinite(value):
        return 0
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


def _sibling_fraction(signals: dict[str, float]) -> float | None:
    """The sibling inventory fraction, or ``None`` when the harness omitted it."""
    if not signals:
        return None
    raw = signals.get(SIBLING_SIGNAL)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return max(-1.0, min(1.0, value))


class NetComplementInventory:
    """Quote both tokens; shrink only the leg that deepens the shared position."""

    name = "micro-net-complement-inventory"

    def setup(self, market: MarketView) -> None:
        return None

    def on_book(self, ctx: BookContext) -> None:
        return None

    def on_trade(self, ctx: TradeContext) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        sibling = _sibling_fraction(ctx.signals or {})
        if sibling is None:
            return SideActions(
                bid="QUOTE",
                ask="QUOTE",
                reason=(
                    f"net complement inventory: no {SIBLING_SIGNAL} signal; "
                    "sibling exposure unknown, quote both"
                ),
            )

        u = self._inventory_ratio(ctx)
        own = _sign(u)
        if own == 0 or _sign(sibling) != own:
            return SideActions(
                bid="QUOTE",
                ask="QUOTE",
                reason=(
                    f"net complement inventory: own {u:+.2f} / sibling "
                    f"{sibling:+.2f} net to a completed pair; quote both"
                ),
            )

        # Same direction on both tokens: adding to it grows the shared risk.
        # Long pair -> the bid adds; short pair -> the ask adds.
        grow = "bid" if own > 0 else "ask"
        bid_scale = SHRINK_SCALE if grow == "bid" else FULL_SCALE
        ask_scale = SHRINK_SCALE if grow == "ask" else FULL_SCALE
        return SideActions(
            bid="SHRINK" if grow == "bid" else "QUOTE",
            ask="SHRINK" if grow == "ask" else "QUOTE",
            bid_scale=bid_scale,
            ask_scale=ask_scale,
            reason=(
                f"net complement inventory: own {u:+.2f} / sibling "
                f"{sibling:+.2f} same way; {grow} grows shared risk -> "
                f"{SHRINK_SCALE:.2f}"
            ),
        )

    @staticmethod
    def _inventory_ratio(ctx: DecisionContext) -> float:
        """``inventory_shares / max(max_inventory_shares, 1)``.

        The cap is floored at one share, so a zero or negative cap cannot turn
        the ratio into infinity and the sign of the position survives. A
        non-finite position has no direction and is reported as flat.
        """
        inv = float(ctx.inventory_shares)
        if not math.isfinite(inv):
            return 0.0
        cap = float(ctx.max_inventory_shares)
        if not math.isfinite(cap):
            return 0.0
        return inv / max(cap, 1.0)


STRATEGY = NetComplementInventory()
