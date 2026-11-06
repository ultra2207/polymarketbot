"""Keep both book sides populated: coupling reward, not size, is the lever.

The reward program does not sum the two sides of our book; it normalises them.
With ``Q_two = bid + ask`` when both sides rest and ``Q_one = max(bid, ask)``
when only one does, the paired score is ``Q_two + Q_min / 3`` with
``Q_min = min(Q_one, Q_two)``.  A one-sided quote therefore earns a **third**
of its raw score (and in the venue's strict-minimum regime, nothing at all).
Pulling one side does not cost half the market's reward, it costs at least
two-thirds of it, while the adverse selection avoided is only that one side's
markout — the asymmetry every defensive idea in ``docs/replay-results.md``
(Wave 2) ran into.  So the only defensible shape for this idea is: never
``PULL`` a side while the other rests, and actively restore a side that has
dropped out.

Presence is read from the context the engine already builds for us:
``bid_score > 0`` means the bid side is resting and reward-eligible,
``ask_score > 0`` the same for the ask.  The module records one presence bit
per side per asset in ``decide`` and keeps the last ``WINDOW`` decisions.  The
rule, in order:

* first decision for an asset (both windows empty) -> ``QUOTE`` both at full
  scale; one observation is not evidence that a side is gone (warm-up);
* both sides resting -> ``QUOTE`` both at full scale;
* neither side resting -> ``QUOTE`` both at full scale, to restore the pair;
* exactly one side resting and the missing side scored at least once in the
  window -> ``QUOTE`` both at full scale; the pair is worth more than any
  one-sided risk reduction;
* exactly one side resting and the missing side has been unscored for the
  whole window -> ``QUOTE`` the missing side at full scale (retry the restore)
  and ``SHRINK`` the resting side to 0.01.  The engine floors any positive size
  at ``rewards_min_size``, so the surviving side stays reward-eligible while
  its exposure to an unpaired, unhedged quote is minimised.  This is the
  closest we come to a pull without paying the two-thirds penalty.

The baseline's quotes sit inside the spread, so ``*_size_at_level`` and
``*_queue_ratio`` are zero for our own book; this rule deliberately reads only
the per-side scores and the asset id.  No clock, no files, no network, no
engine internals.
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

#: Decisions of presence history kept per side, per asset.
WINDOW = 20
#: Size scale for the unpaired side; the engine raises it to the reward floor.
SHRINK_SCALE = 0.01
#: Size scale that keeps a side at its normal quote size.
FULL_SCALE = 1.0


class MicroTwoSidedCoupling:
    """Restore a missing side; never pull one side while the other rests."""

    name = "micro-two-sided-coupling"

    def __init__(self) -> None:
        # Per asset: one presence bit per side, per decision.
        self._presence: dict[str, dict[str, deque[bool]]] = {}

    def setup(self, market: MarketView) -> None:
        self._presence.clear()

    def on_book(self, ctx: BookContext) -> None:
        return None

    def on_trade(self, ctx: TradeContext) -> None:
        return None

    def _windows(self, asset: str) -> dict[str, deque[bool]]:
        sides = self._presence.get(asset)
        if sides is None:
            sides = {
                "bid": deque(maxlen=WINDOW),
                "ask": deque(maxlen=WINDOW),
            }
            self._presence[asset] = sides
        return sides

    def decide(self, ctx: DecisionContext) -> SideActions:
        asset = ctx.book.asset_id
        sides = self._windows(asset)
        bid_resting = float(ctx.bid_score) > 0.0
        ask_resting = float(ctx.ask_score) > 0.0

        # History as of the *previous* decision: the current decision cannot be
        # evidence that the side it reports missing has ever been restorable.
        bid_history = tuple(sides["bid"])
        ask_history = tuple(sides["ask"])
        sides["bid"].append(bid_resting)
        sides["ask"].append(ask_resting)

        if not bid_history and not ask_history:
            return SideActions(
                bid="QUOTE",
                ask="QUOTE",
                bid_scale=FULL_SCALE,
                ask_scale=FULL_SCALE,
                reason="coupling: warm-up, no presence history",
            )

        if bid_resting and ask_resting:
            return SideActions(
                bid="QUOTE",
                ask="QUOTE",
                bid_scale=FULL_SCALE,
                ask_scale=FULL_SCALE,
                reason="coupling: both sides resting, keep the pair",
            )

        if not bid_resting and not ask_resting:
            return SideActions(
                bid="QUOTE",
                ask="QUOTE",
                bid_scale=FULL_SCALE,
                ask_scale=FULL_SCALE,
                reason="coupling: neither side resting, restore the pair",
            )

        if bid_resting:
            # Ask is missing. If it scored inside the window it demonstrably
            # rests in the band, so re-quote it rather than shrink its partner.
            if any(ask_history):
                return SideActions(
                    bid="QUOTE",
                    ask="QUOTE",
                    bid_scale=FULL_SCALE,
                    ask_scale=FULL_SCALE,
                    reason="coupling: ask restorable within window, restore the pair",
                )
            return SideActions(
                bid="SHRINK",
                ask="QUOTE",
                bid_scale=SHRINK_SCALE,
                ask_scale=FULL_SCALE,
                reason="coupling: ask unscored for the window, shrink unpaired bid",
            )

        # Bid is missing.
        if any(bid_history):
            return SideActions(
                bid="QUOTE",
                ask="QUOTE",
                bid_scale=FULL_SCALE,
                ask_scale=FULL_SCALE,
                reason="coupling: bid restorable within window, restore the pair",
            )
        return SideActions(
            bid="QUOTE",
            ask="SHRINK",
            bid_scale=FULL_SCALE,
            ask_scale=SHRINK_SCALE,
            reason="coupling: bid unscored for the window, shrink unpaired ask",
        )


STRATEGY = MicroTwoSidedCoupling()
