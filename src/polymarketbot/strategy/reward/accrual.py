"""The published R(c) path: what one resting bid is actually paid per day."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from polymarketbot.strategy.books import mid, qualifying_score
from polymarketbot.strategy.reward.competition import CompetingEstimate
from polymarketbot.strategy.reward.marginal import PROTOCOL_MIN_DAILY_PAYOUT_USD
from polymarketbot.strategy.reward.scoring import single_sided_score_fraction


@dataclass(frozen=True)
class AccrualShare:
    """What one resting bid is actually paid, from measured books.

    ``usd_per_day`` is the quote's share of the market's published pool:
    ``pool * our_score / (measured_competing_score + our_score)``, with
    ``our_score = alpha(shares, |price - leg_mid|) * shares * 1/c`` (``c`` = the
    venue's one-sided penalty) and ``competing_score`` the pair-wide in-band
    score, as the epoch-average estimate from
    :class:`CompetingScoreWindow` when the caller supplies one (otherwise a
    single snapshot). ``qualifies`` is False when the quote earns the venue
    nothing at all (size below the market's reward minimum, price outside the
    band, or a leg mid outside the two-sided band); the payout is then a
    measured zero rather than an omission.

    ``competing_samples``, ``competing_variance`` and ``low_confidence`` are the
    provenance of the denominator: a single-sample estimate is flagged rather
    than presented as the epoch rate, and ``competing_snapshot`` keeps the raw
    latest reading so the averaging is auditable. Units: USD/day, shares, score
    units, USD/day.
    """

    usd_per_day: float
    share: float
    our_score: float
    competing_score: float
    pool_usd_per_day: float
    qualifies: bool
    competing_samples: int = 1
    competing_variance: Optional[float] = None
    low_confidence: bool = True
    competing_snapshot: Optional[float] = None

    @property
    def provenance(self) -> str:
        if self.low_confidence:
            return f"LOW-CONFIDENCE n={self.competing_samples} (snapshot, not an epoch average)"
        spread = f", sd={math.sqrt(self.competing_variance):,.1f}" if self.competing_variance else ""
        return f"n={self.competing_samples}{spread}"


def measured_accrual(
    *,
    pool_usd_per_day: Optional[float],
    price: float,
    shares: float,
    min_size: Optional[float],
    max_spread_cents: Optional[float],
    leg_book: Optional[dict[str, Any]],
    other_book: Optional[dict[str, Any]],
    competing: Optional[CompetingEstimate] = None,
) -> Optional[AccrualShare]:
    """The payout a resting bid earns per day, from its own measured books.

    This is the accrual the engine credits as cash, so every input is a
    measurement and nothing has a fallback: the pool is the venue's published
    daily rate for the market, the band and the minimum size are the venue's
    published campaign parameters, ``price``/``shares`` are what is actually
    resting, and the mid comes from the current L2 snapshot of *both*
    outcome-token books. Returns ``None`` - and the caller must then credit
    nothing - when any of those is missing or when the measured competing score
    is zero (a singular denominator), because a payout without a measured
    denominator is a guess.

    ``competing`` is the epoch-average estimate for this market's pair. The
    venue divides by the average of up to 1,440 samples, so pricing an epoch
    rate from one snapshot is the estimator error this parameter removes: when
    it is supplied the mean is used and the sample count travels with the
    result; when it is absent the single measured snapshot is used and the
    result is marked low-confidence.

    Deliberately different from :func:`quotes_from_allocation`: that one prices
    a *plan* from the optimizer, whose payout was computed once for the planned
    capital; this one prices what is on the book **now**, so a partially filled,
    stale or over-sized resting bid accrues what it actually earns.
    """
    if pool_usd_per_day is None or pool_usd_per_day <= 0.0:
        return None
    if min_size is None or min_size <= 0.0:
        return None
    if max_spread_cents is None or max_spread_cents <= 0.0:
        return None
    if not isinstance(leg_book, dict) or not isinstance(other_book, dict):
        return None
    leg_mid = mid(leg_book)
    other_mid = mid(other_book)
    if leg_mid is None or other_mid is None:
        return None
    snapshot = qualifying_score(
        leg_book, max_spread_cents=max_spread_cents, mid_price=leg_mid
    ) + qualifying_score(
        other_book, max_spread_cents=max_spread_cents, mid_price=other_mid
    )
    if competing is not None:
        denominator = float(competing.mean)
        samples = competing.samples
        variance = competing.variance
        low = competing.low_confidence
        latest = competing.snapshot
    else:
        denominator = snapshot
        samples = 1
        variance = None
        low = True
        latest = snapshot
    if denominator <= 0.0:
        return None

    def _share(usd: float, share: float, our: float, qualifies: bool) -> AccrualShare:
        return AccrualShare(
            usd_per_day=usd,
            share=share,
            our_score=our,
            competing_score=denominator,
            pool_usd_per_day=pool_usd_per_day,
            qualifies=qualifies,
            competing_samples=samples,
            competing_variance=variance,
            low_confidence=low,
            competing_snapshot=latest,
        )

    if shares <= 0.0 or shares < min_size or not 0.0 < price < 1.0:
        return _share(0.0, 0.0, 0.0, False)
    band = float(max_spread_cents)
    distance = abs(price - leg_mid) * 100.0
    if distance >= band:
        # Outside the band the venue scores nothing: a measured zero payout.
        return _share(0.0, 0.0, 0.0, False)
    fraction = single_sided_score_fraction(leg_mid)
    if fraction <= 0.0:
        return _share(0.0, 0.0, 0.0, False)
    our_score = ((band - distance) / band) ** 2 * float(shares) * fraction
    denom = denominator + our_score
    share = our_score / denom if denom > 0.0 else 0.0
    usd_per_day = pool_usd_per_day * share
    # Documented venue floor: a market whose daily payout is below $1.00 pays
    # nothing, so the quote's share of it is a measured zero, not a small cash
    # amount the engine would otherwise bank.
    if usd_per_day < PROTOCOL_MIN_DAILY_PAYOUT_USD:
        return _share(0.0, share, our_score, False)
    return _share(usd_per_day, share, our_score, True)


