"""Quote full size unless our own pool share is already far above target.

The venue pays each minute's pool in proportion to ``Q_min`` shares, so the
reward per resting dollar falls as ``C / (S + C) ** 2`` while the adverse
selection per resting dollar is constant.  Past a point, extra size buys a
smaller and smaller slice of the pool, and the marginal dollar is better left
unquoted -- but only where the pool is already crowded *by us*.  This is an
optimisation, not a defence: the rule never leaves the book, it only halves a
quote that is already earning more share than the market is worth.

Projected share, from the context only::

    S = bid_score + ask_score
    share = S / max(S + competing_score, 1e-9)

``competing_score`` is the reward-relevant score of the rest of the band
(``BookContext.competing_score``); ``bid_score``/``ask_score`` are the scores
our own resting quotes already earn.  The baseline quotes inside the spread, so
``*_size_at_level`` and ``*_queue_ratio`` are zero for our own quotes and are
deliberately not read here.

Target share, from ``rate_per_day``::

    target = clamp(rate_per_day / 300.0, 0.10, 0.60)

This divisor is a **prior, not a measurement**.  It says a $180/day market
justifies a 60 % share (the cap) and a $15/day market only 5 % (raised to the
10 % floor); the two endpoints are the ones the replay results can move first
without invalidating the shape.  The mapping is intentionally monotone: a
market that pays little per day does not justify size at any share, a market
that pays a lot justifies chasing share.

The rule, in order (per market, both sides together -- the score is a pool
quantity, not a per-side one):

1. First decision on an asset -> ``QUOTE`` both at full scale.  Warm-up: the
   score in the context belongs to quotes the engine has not priced under this
   module yet, so it is not evidence about this rule.
2. ``competing_score == 0`` *and* ``bid_score + ask_score == 0`` -> ``QUOTE``
   both at full scale.  Nothing has scored yet, so a share would be invented;
   ``competing_score`` defaults to ``0.0`` and a missing signal is never read as
   competition.
3. ``share < target`` -> ``QUOTE`` both at full scale.  We are under-represented
   relative to what this market pays; size is the only way to buy share.
4. ``share > target * 1.5`` -> ``SHRINK`` both to ``0.5``.  The half is
   explicit: this is book-wide optimisation of a quote that is over-earning
   share, not a defensive pull, and the engine's ``rewards_min_size`` floor
   keeps both sides reward-eligible.
5. Otherwise -> ``QUOTE`` both at full scale.  Inside the band there is no
   reason to trade reward for size.

Never ``PULL``: a pull zeroes a side and, under the venue's normalisation,
costs two-thirds of that side's score; halving keeps the quote priced while
removing half the exposure.  Reads only ``DecisionContext`` plus a per-asset
"already seen" bit; no clock, no files, no network, no engine internals.
"""

from __future__ import annotations

from polymarketbot.strategy.base import (
    BookContext,
    DecisionContext,
    MarketView,
    SideActions,
    TradeContext,
)

#: Size scale that keeps a side at its normal quote size.
FULL_SCALE = 1.0
#: Size scale for an over-earning quote; an optimisation, never a pull.
SHRINK_SCALE = 0.5
#: Market dollars per day at which the target share reaches 1.0 before the cap.
#: A prior, not a fitted constant.
RATE_PER_DAY_AT_FULL_TARGET = 300.0
#: Floor on the target share: even a small market may be quoted to 10 %.
MIN_TARGET_SHARE = 0.10
#: Cap on the target share: never chase more than 60 % of a pool.
MAX_TARGET_SHARE = 0.60
#: Shrink only once share exceeds the target by this factor.
SHRINK_MULTIPLE = 1.5

_EPS = 1e-9


def target_share(rate_per_day: float) -> float:
    """Target pool share for a market paying ``rate_per_day`` dollars a day."""
    raw = float(rate_per_day) / RATE_PER_DAY_AT_FULL_TARGET
    return max(MIN_TARGET_SHARE, min(MAX_TARGET_SHARE, raw))


class MicroCompetitionDilution:
    """Halve both sides when our share of the reward pool is already rich."""

    name = "micro-competition-dilution"

    def __init__(self) -> None:
        # Assets whose first decision has already been made; warm-up guard.
        self._seen: set[str] = set()

    def setup(self, market: MarketView) -> None:
        self._seen.clear()

    def on_book(self, ctx: BookContext) -> None:
        return None

    def on_trade(self, ctx: TradeContext) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        asset = ctx.book.asset_id
        warm_up = asset not in self._seen
        self._seen.add(asset)

        if warm_up:
            return SideActions(
                bid="QUOTE",
                ask="QUOTE",
                bid_scale=FULL_SCALE,
                ask_scale=FULL_SCALE,
                reason="competition: warm-up, first decision on this asset",
            )

        ours = max(0.0, float(ctx.bid_score)) + max(0.0, float(ctx.ask_score))
        competing = max(0.0, float(ctx.book.competing_score))
        if ours == 0.0 and competing == 0.0:
            return SideActions(
                bid="QUOTE",
                ask="QUOTE",
                bid_scale=FULL_SCALE,
                ask_scale=FULL_SCALE,
                reason="competition: no scores yet, nothing to decide",
            )

        share = ours / max(ours + competing, _EPS)
        target = target_share(ctx.rate_per_day)

        if share < target:
            return SideActions(
                bid="QUOTE",
                ask="QUOTE",
                bid_scale=FULL_SCALE,
                ask_scale=FULL_SCALE,
                reason=f"competition: share {share:.3f} < target {target:.3f}, buy share",
            )

        if share > target * SHRINK_MULTIPLE:
            return SideActions(
                bid="SHRINK",
                ask="SHRINK",
                bid_scale=SHRINK_SCALE,
                ask_scale=SHRINK_SCALE,
                reason=(
                    f"competition: share {share:.3f} > "
                    f"{SHRINK_MULTIPLE} x target {target:.3f}, dilute"
                ),
            )

        return SideActions(
            bid="QUOTE",
            ask="QUOTE",
            bid_scale=FULL_SCALE,
            ask_scale=FULL_SCALE,
            reason=f"competition: share {share:.3f} near target {target:.3f}",
        )


STRATEGY = MicroCompetitionDilution()
