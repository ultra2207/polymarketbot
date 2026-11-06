"""Shrink the side a lopsided resting book says will be swept first.

The claim (``spec.md``): the *stock* of resting size at the touch says which
side of the book is thin right now, and the thin side is the side a sweep
reaches first (Cont, Kukanov & Stoikov 2014). A taker arriving into a thin ask
runs through our ask quote, so a defensible rule is:

* ``depth_imbalance >= +0.5`` -- the ask is thin, buy-side pressure is likely:
  keep the bid scoring at full size and cut the ask;
* ``depth_imbalance <= -0.5`` -- mirror image: cut the bid;
* otherwise quote both sides at full size.

Reward is preserved rather than traded away. The cut is ``SHRINK`` at
``SHRINK_SCALE = 0.01``, not ``PULL``: the engine floors any positive size at
``rewards_min_size``, so the quote stays reward-eligible while the notional the
adverse markout applies to collapses. Under the venue's
``Q_norm = Q_two + min(Q_one, Q_two)/3`` normalisation a pull costs two-thirds
of that side's score; Wave 2 (``docs/replay-results.md``) measured four more
size-reducing rules losing for exactly that reason, so a rule that fires only on
``|di| >= 0.5`` must not also surrender the side's reward. ``PULL`` is never
returned and no price override is set; the baseline already stands inside the
spread and this rule changes only size.

Data source, and why the module is inert until prepared
--------------------------------------------------------
``spec.md`` sketched the imbalance from ``bid_size_at_level`` and
``ask_size_at_level``. Those are the sizes at *our* quote's level, and the
baseline quotes sit inside the spread, so both are ``0`` on every decision in a
replay -- a ratio over them never fires (``0 / max(1e-9, 0) = 0``). The depth
proxy this rule actually needs is the resting depth at the touch, and the
harness exposes that as the named signal ``ctx.signals["depth_imbalance"]``,
materialised as-of by ``prepare.sql`` into
``data/derived/ideas/micro-depth-imbalance/signals.parquet``.

No ``prepare.sql`` ships with this module in this change, so in a bare replay
``signals`` is empty on every step and the module returns ``QUOTE``/``QUOTE``
for both sides. That inertness is deliberate: without a measured depth ratio
the rule has no evidence, and inventing an imbalance from the spread or the
price would test a different idea. A malformed or non-finite signal value is
read the same way as a missing one -- quote both sides rather than classify on
junk.
"""

from __future__ import annotations

import math
from typing import Optional

from polymarketbot.strategy.base import (
    BookContext,
    DecisionContext,
    MarketView,
    SideActions,
    TradeContext,
)

#: Name of the prepared signal this rule reads from ``ctx.signals``.
SIGNAL_NAME = "depth_imbalance"
#: ``|di|`` at or above this identifies a thin side. From ``spec.md``.
IMBALANCE_THRESHOLD = 0.5
#: Fraction of size the threatened side keeps. The engine floors any positive
#: size at ``rewards_min_size``, so this stays reward-eligible while cutting the
#: notional exposed to the taker.
SHRINK_SCALE = 0.01


def _read_depth_imbalance(signals: dict[str, float]) -> Optional[float]:
    """The prepared depth imbalance, or ``None`` when it cannot be trusted.

    ``None`` covers all three no-evidence states: no signal table, no
    ``depth_imbalance`` column in it, and a value that is not a finite number.
    Each maps to "quote both sides", because a rule that cannot name the thin
    side must not take risk out of the book.
    """
    if not signals or SIGNAL_NAME not in signals:
        return None
    try:
        value = float(signals[SIGNAL_NAME])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


class MicroDepthImbalance:
    """Quote both sides; shrink the side a lopsided touch says is threatened."""

    name = "micro-depth-imbalance"

    def setup(self, market: MarketView) -> None:
        return None

    def on_book(self, ctx: BookContext) -> None:
        return None

    def on_trade(self, ctx: TradeContext) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        depth_imbalance = _read_depth_imbalance(ctx.signals)
        if depth_imbalance is None:
            return SideActions(
                bid="QUOTE",
                ask="QUOTE",
                reason=(
                    "depth imbalance: no usable "
                    f"{SIGNAL_NAME} signal; inert, both sides quoted"
                ),
            )
        if depth_imbalance >= IMBALANCE_THRESHOLD:
            # Thin ask: buying pressure would run over the resting ask first.
            return SideActions(
                bid="QUOTE",
                ask="SHRINK",
                bid_scale=1.0,
                ask_scale=SHRINK_SCALE,
                reason=(
                    f"depth imbalance: ask thin di={depth_imbalance:+.3f}; "
                    "shrink ask, keep bid scoring"
                ),
            )
        if depth_imbalance <= -IMBALANCE_THRESHOLD:
            # Thin bid: selling pressure would run over the resting bid first.
            return SideActions(
                bid="SHRINK",
                ask="QUOTE",
                bid_scale=SHRINK_SCALE,
                ask_scale=1.0,
                reason=(
                    f"depth imbalance: bid thin di={depth_imbalance:+.3f}; "
                    "shrink bid, keep ask scoring"
                ),
            )
        return SideActions(
            bid="QUOTE",
            ask="QUOTE",
            reason=(
                f"depth imbalance: balanced di={depth_imbalance:+.3f}; "
                "both sides at full size"
            ),
        )


STRATEGY = MicroDepthImbalance()
