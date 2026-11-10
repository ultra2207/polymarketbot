"""The measured competing score a reward share is divided by.

The venue divides a maker's score by the epoch average of the competing book.
Every reading here is the venue's own L2 snapshot scored with its published
rule; :class:`CompetingScoreWindow` is the epoch-average estimator that keeps
one reading per venue sample interval and reports the mean with its sample
count, so no denominator is ever presented without its provenance.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Optional

from polymarketbot.strategy.books import mid
from polymarketbot.strategy.reward.scoring import (
    PROTOCOL_EPOCH_SAMPLES,
    SINGLE_SIDED_PENALTY,
)
from polymarketbot.strategy.venue.reads import Market

_SECONDS_PER_DAY = 86_400.0

# The sampling cadence implied by the documented epoch, in seconds (60 s). The
# competitor window below samples at this cadence rather than at the engine's
# tick rate, so the estimate does not depend on whatever interval the engine
# happens to run at.
PROTOCOL_SAMPLE_INTERVAL_S = _SECONDS_PER_DAY / float(PROTOCOL_EPOCH_SAMPLES)
# A window of one reading cannot state a variance and is not an average: the
# venue's denominator is the mean over the epoch. Below two samples the
# estimate is labelled low-confidence instead of being reported as the epoch
# rate.
MIN_CONFIDENT_SAMPLES = 2


@dataclass(frozen=True)
class CompetingEstimate:
    """Epoch-average competing score estimated from measured samples.

    ``mean`` is the arithmetic mean of the window's readings and is the
    denominator used for ranking and accrual; ``samples`` states how many
    venue-cadence snapshots it came from; ``variance`` is the unbiased sample
    variance of the window (``None`` below two samples, where it is undefined);
    ``snapshot`` is the latest raw reading. ``low_confidence`` is True below
    :data:`MIN_CONFIDENT_SAMPLES`: one snapshot is a point observation of a
    quantity that moves, not the epoch average the venue divides by, and
    :attr:`label` says so.
    """

    mean: float
    samples: int
    variance: Optional[float]
    snapshot: float

    @property
    def low_confidence(self) -> bool:
        return self.samples < MIN_CONFIDENT_SAMPLES

    @property
    def label(self) -> str:
        """Short provenance string for logs: sample count and confidence."""
        if self.samples <= 0:
            return "unmeasured"
        if self.low_confidence:
            return (
                f"LOW-CONFIDENCE n={self.samples} "
                f"(one snapshot, not an epoch average)"
            )
        spread = f", sd={math.sqrt(self.variance):,.1f}" if self.variance else ""
        return f"n={self.samples}{spread}"


class CompetingScoreWindow:
    """Rolling window of measured competing scores, one reading per venue interval.

    The venue divides a maker's score by the epoch average of the competing
    book, over up to 1,440 samples. A single L2 snapshot is not that average:
    measured on the paper engine it moved the implied reward rate 4.5x between
    ticks ($104 -> $472.91/day, 49.37 %/day, window 9 of 2026-11-10) while the
    time-averaged equity delta over the same 30 minutes was $286/day. This
    window keeps at most one reading per :data:`PROTOCOL_SAMPLE_INTERVAL_S`
    over one epoch and reports the mean, the count and the variance, so every
    denominator can state how much evidence stands behind it.

    A reading is only taken when the cadence has elapsed; the window itself is
    pure state, the caller supplies ``now``.
    """

    def __init__(self, horizon_s: float = _SECONDS_PER_DAY) -> None:
        self._horizon_s = float(horizon_s)
        self._readings: dict[str, list[tuple[float, float]]] = {}

    def _prune(self, condition_id: str, now: float) -> list[tuple[float, float]]:
        cutoff = now - self._horizon_s
        series = self._readings.get(condition_id)
        if not series:
            return []
        while series and series[0][0] < cutoff:
            series.pop(0)
        return series

    def observe(self, condition_id: str, snapshot: float, now: float) -> CompetingEstimate:
        """Record one measured reading, at most once per venue sample interval.

        ``snapshot`` must be a measured positive score; a zero or non-finite
        reading is not a measurement of competition and raises rather than
        entering the window as a zero denominator.
        """
        if not math.isfinite(snapshot) or snapshot <= 0.0:
            raise ValueError(
                f"competing score must be a measured positive value, got {snapshot!r}"
            )
        series = self._readings.setdefault(condition_id, [])
        self._prune(condition_id, now)
        if not series or now - series[-1][0] >= PROTOCOL_SAMPLE_INTERVAL_S:
            series.append((float(now), float(snapshot)))
        return self.estimate(condition_id, now, snapshot=snapshot)

    def estimate(
        self,
        condition_id: str,
        now: float,
        snapshot: Optional[float] = None,
    ) -> Optional[CompetingEstimate]:
        """Window mean and its provenance; ``None`` when nothing was measured."""
        series = self._prune(condition_id, now)
        if not series:
            return None
        values = [value for _ts, value in series]
        mean = sum(values) / len(values)
        variance = statistics.variance(values) if len(values) >= MIN_CONFIDENT_SAMPLES else None
        latest = float(snapshot) if snapshot is not None else values[-1]
        return CompetingEstimate(
            mean=mean, samples=len(values), variance=variance, snapshot=latest
        )

    def forget(self, condition_id: str) -> None:
        """Drop a market's window (the market left the reward universe)."""
        self._readings.pop(condition_id, None)

    def clear(self) -> None:
        self._readings.clear()


@dataclass(frozen=True)
class CompetingDetail:
    """Measured in-band score of one market's pair, and what it can claim.

    ``q_one_side`` is every bid on YES plus every ask on NO (the quantity that
    sums to the venue's ``Q_one`` across makers), ``q_two_side`` its
    complement, and ``total`` their sum - the denominator this pillar uses.
    The venue's actual denominator is ``sum_makers Q_min``, and per maker
    ``Q_min`` lies between ``(Q_one + Q_two) / (c + 1)`` and
    ``Q_one + Q_two`` (the low end is a maker quoting one side at ``1/c`` of
    the other, the high end a perfectly two-sided maker). Summing over makers,
    the venue's denominator therefore lies in ``[total / (c + 1), total]``:
    ``total`` is an upper bound, so the share this pillar claims is
    *understated* - conservative, never an overclaim - by at most
    :attr:`bias_factor_high`. The bracket cannot be tightened from the books
    because per-maker pairing is not published.
    """

    total: float
    q_one_side: float
    q_two_side: float

    @property
    def venue_denominator_low(self) -> float:
        return self.total / (1.0 + SINGLE_SIDED_PENALTY)

    @property
    def bias_factor_high(self) -> float:
        return 1.0 + SINGLE_SIDED_PENALTY

    @property
    def direction(self) -> str:
        return "overstates denominator -> understates our share (conservative)"


def market_competing_detail(
    market: Market, books: dict[str, dict[str, Any]]
) -> Optional[CompetingDetail]:
    """Side-resolved measured competing score of a binary market's pair.

    Returns ``None`` under the same conditions as
    :func:`market_competing_score`: a non-binary market, no published band, a
    missing book or mid, or a measured total of zero (an unmeasured or empty
    book, which cannot be told apart from a stale snapshot).
    """
    if not market.binary or market.reward_max_spread_cents <= 0.0:
        return None
    # Q_one is bids on YES plus asks on NO; Q_two the complement. Gamma lists
    # outcomes as (Yes, No); when the label is missing the two sides cannot be
    # attributed, but their sum (the denominator) is still measured.
    yes_first = bool(market.outcomes) and str(market.outcomes[0]).upper() in ("YES", "Y")
    q_one = 0.0
    q_two = 0.0
    for index, token_id in enumerate(market.token_ids):
        book = books.get(token_id)
        if not isinstance(book, dict):
            return None
        token_mid = mid(book)
        if token_mid is None:
            return None
        band = float(market.reward_max_spread_cents)
        is_yes = (index == 0) == yes_first
        for side in ("bids", "asks"):
            levels = book.get(side)
            if not isinstance(levels, (list, tuple)):
                continue
            score = 0.0
            for level in levels:
                try:
                    price = float(level["price"])
                    size = float(level["size"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not (0.0 < price < 1.0) or size <= 0.0:
                    continue
                distance = abs(price - token_mid) * 100.0
                if distance > band:
                    continue
                score += size * ((band - distance) / band) ** 2
            # A YES bid and a NO ask are the same book side of the pair; so are
            # a YES ask and a NO bid. This mirrors Q_one/Q_two in the docs.
            if (side == "bids") == is_yes:
                q_one += score
            else:
                q_two += score
    total = q_one + q_two
    if total <= 0.0:
        return None
    return CompetingDetail(total=total, q_one_side=q_one, q_two_side=q_two)


def market_competing_score(
    market: Market, books: dict[str, dict[str, Any]]
) -> Optional[float]:
    """The market's MEASURED in-band competing score, summed over both tokens.

    Source: the venue's own L2 snapshot for **both** outcome-token books, each
    level scored with the venue's published rule ``size * ((v - |p - mid|)/v)**2``
    inside the market's ``rewards_max_spread`` band ``v`` and against that
    token's own book mid. This is the quantity the venue divides a maker's score
    by, so it is the only honest denominator for a share of the daily pool; no
    multiplier, floor or rate-derived estimate is applied anywhere.

    Both tokens matter: a market's campaign pays on the market (condition), so a
    maker and its competitors are scored across the pair. Measuring only the
    quoted leg halves the denominator and doubles our claimed share - measured
    on 40 live reward markets on 2026-11-10, the leg-only reading implied
    $772/day of payout on $838 of capital where the pair-wide reading implied
    $365/day, a 2.1x overclaim.

    Returns ``None`` - never a floor - when either book or either mid is
    missing, and also when the measured total is zero: a zero denominator makes
    the payout singular (the whole pool), and a book with no in-band depth is
    indistinguishable from a stale snapshot, so the caller must refuse the
    market instead of pricing it. A market that is not binary has no pair to
    measure and is likewise refused.

    This sum is ``Q_one + Q_two`` over every resting order, while the venue's
    denominator is ``sum_makers Q_min``. It is therefore an **upper bound**
    (see :class:`CompetingDetail`): our share is understated by a factor
    between ``1`` and ``1 + c``, which is the conservative direction and is
    stated in the accrual log. It is also a *snapshot*; the venue averages
    1,440 of them per epoch, so callers that price a rate must feed
    :class:`CompetingScoreWindow` rather than use this reading directly.
    """
    detail = market_competing_detail(market, books)
    return None if detail is None else detail.total


