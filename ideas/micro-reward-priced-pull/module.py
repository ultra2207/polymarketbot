"""Price every pull in the forfeited reward dollars, not in risk alone.

The claim (``spec.md``): the queue gate spends reward without a price. It
compares a queue ratio to 0.60 and never asks whether the markout it avoids is
worth the dollars it discards, so it over-pulls exactly when reward income is
large. This module replaces the risk-only test with a unit conversion:

* the venue samples the score **once per minute** at a random offset, so one
  decision that leaves the book forfeits roughly one sample of that side's
  reward, valued at ``(rate_per_day / 1440) * S_side / (S_side + competing)``
  dollars -- our share of the pool times the pool's per-minute rate;
* the markout a filled quote costs is ``PULL_MARKOUT * rewards_min_size``
  dollars, the repo's measured back-of-queue figure (-2.01 c/share, see
  ``docs/predictive-mm-analysis.md``) applied to the smallest size the engine
  will rest.

Pull only when the forfeit is below that markout. When the forfeit is in the
marginal band above it, shrink to the reward-eligible floor (``0.01``, which the
engine floors at ``rewards_min_size``): the quote keeps its score contribution
while the notional that the markout applies to is cut. Above the band, quote at
full size.

Every constant here is a **prior, not a measurement**. ``PULL_MARKOUT`` comes
from the back-of-queue markout on another market slice and is used because the
module may not read the tape; ``SHRINK_CEILING`` is a hysteresis band, not an
estimate of anything; and the per-minute sample value is the venue's stated
sampling rule rather than this market's realised reward. The spec's
events-per-epoch estimator is not implemented: the rate is already quoted in
dollars per minute, so the epoch bookkeeping would only add state that never
changes a comparison.

The rule reads ``bid_score``/``ask_score``/``rate_per_day``/
``book.competing_score`` and ``signals["threat"]``. It does not read
``*_queue_ratio`` or ``*_size_at_level``, which are zero for the baseline's
inside-the-spread quotes. Without a ``threat`` signal it never pulls: there is
nothing to price against.
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

#: The reward pool is quoted per day; the venue samples the score once a minute.
MINUTES_PER_DAY = 1440.0
#: Markout prior in dollars per share. The repo measures -2.01 c/share for a
#: fill at the back of the queue (``docs/predictive-mm-analysis.md``), which is
#: the cost a pull avoids. Rounded to 0.02 because the figure is a prior from
#: another slice, not this market's realised markout.
PULL_MARKOUT = 0.02
#: Upper edge of the marginal band, as a multiple of the markout estimate. A
#: forfeit between 1x and 1.5x the markout is close enough that cutting the
#: notional is preferred to either giving up the reward or taking the risk
#: whole; above 1.5x the reward clearly wins. Two thresholds rather than one
#: keep the decision from chattering on a noisy score.
SHRINK_CEILING = 1.5
#: Fraction of size a marginal side keeps. The engine floors any positive size
#: at ``rewards_min_size``, so this stays reward-eligible while cutting the
#: notional the markout applies to.
SHRINK_SCALE = 0.01
#: ``signals["threat"]`` at or above this means a threat is active.
THREAT_ACTIVE = 1.0
#: Floor for the score-share denominator, so an empty book yields a share of 0
#: rather than a division error.
_SCORE_EPS = 1e-9


def _positive(value: float) -> float:
    """Read a context field as a finite non-negative number; junk becomes 0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number <= 0.0:
        return 0.0
    return number


def _clamp_share(share: float) -> float:
    """Keep the score share inside ``[0, 1]`` despite degenerate inputs."""
    return max(0.0, min(1.0, share))


class RewardPricedPull:
    """Quote, shrink or pull each side by comparing forfeited reward to markout."""

    name = "micro-reward-priced-pull"

    def __init__(self) -> None:
        #: ``rewards_min_size`` from ``setup``; ``None`` until the first market
        #: is loaded. Without it the markout cannot be priced in dollars, so no
        #: pull is ever justified -- that is the warm-up state.
        self._min_size: Optional[float] = None
        #: ``rate_per_day`` from ``setup``, used only when the context omits it.
        self._rate_per_day: float = 0.0

    def setup(self, market: MarketView) -> None:
        min_size = _positive(market.rewards_min_size)
        self._min_size = min_size if min_size > 0.0 else None
        self._rate_per_day = _positive(market.rate_per_day)

    def on_book(self, ctx: BookContext) -> None:
        return None

    def on_trade(self, ctx: TradeContext) -> None:
        return None

    def decide(self, ctx: DecisionContext) -> SideActions:
        threat = self._threat_active(ctx.signals)
        if not threat:
            return SideActions(
                bid="QUOTE",
                ask="QUOTE",
                reason="reward priced pull: no threat signal; keep both sides",
            )
        markout = self._markout_dollars()
        if markout <= 0.0:
            return SideActions(
                bid="QUOTE",
                ask="QUOTE",
                reason="reward priced pull: no reward size to price; keep both sides",
            )

        rate = _positive(ctx.rate_per_day) or self._rate_per_day
        competing = _positive(ctx.book.competing_score)
        bid_value = self._value_per_minute(rate, ctx.bid_score, competing)
        ask_value = self._value_per_minute(rate, ctx.ask_score, competing)

        bid_action, bid_scale = self._price_side(bid_value, markout)
        ask_action, ask_scale = self._price_side(ask_value, markout)
        return SideActions(
            bid=bid_action,
            ask=ask_action,
            bid_scale=bid_scale,
            ask_scale=ask_scale,
            reason=(
                f"reward priced pull: markout {markout:.4f} "
                f"bid {bid_action.lower()} v={bid_value:.4f} "
                f"ask {ask_action.lower()} v={ask_value:.4f}"
            ),
        )

    @staticmethod
    def _threat_active(signals: dict[str, float]) -> bool:
        """A pull is only considered when the context names an active threat."""
        if not signals:
            return False
        try:
            return float(signals.get("threat", 0.0)) >= THREAT_ACTIVE
        except (TypeError, ValueError):
            return False

    def _markout_dollars(self) -> float:
        """Dollars a filled ``rewards_min_size`` quote is expected to cost."""
        if self._min_size is None:
            return 0.0
        return PULL_MARKOUT * self._min_size

    @staticmethod
    def _value_per_minute(rate_per_day: float, score: float, competing: float) -> float:
        """Our share of one minute of this side's reward, in dollars.

        ``S / (S + competing)`` is the venue's score share; multiplying the
        pool's per-minute rate by it gives what leaving the book for one sample
        would forfeit. Both scores are clamped to non-negative, so a missing or
        malformed field reads as "we earn nothing" rather than as a negative
        reward.
        """
        ours = _positive(score)
        share = _clamp_share(ours / max(ours + competing, _SCORE_EPS))
        return (rate_per_day / MINUTES_PER_DAY) * share

    @staticmethod
    def _price_side(value: float, markout: float) -> tuple[str, float]:
        """Map one side's forfeitable value onto an action and a scale."""
        if value < markout:
            return "PULL", 0.0
        if value < markout * SHRINK_CEILING:
            return "SHRINK", SHRINK_SCALE
        return "QUOTE", 1.0


STRATEGY = RewardPricedPull()
