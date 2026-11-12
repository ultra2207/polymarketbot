"""The published R(c) path: what one resting bid is actually paid per day."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from polymarketbot.strategy.books import mid, qualifying_score
from polymarketbot.strategy.reward.competition import CompetingEstimate
from polymarketbot.strategy.reward.marginal import PROTOCOL_MIN_DAILY_PAYOUT_USD
from polymarketbot.strategy.reward.scoring import (
    OrderQuote,
    compute_book_side_scores,
    compute_minimum_score,
    order_spread_cents,
    quadratic_order_score,
    single_sided_score_fraction,
)

REWARD_ELIGIBILITY_SECONDS = 3.5
"""How long an order must rest before the venue counts it at all.

"The minimum amount of time that an order needs to be active to be eligible
for rewards is 3.5 seconds" (``data/raw/docs/llms-full.txt:53855``). Time
before that is not accruable, whatever the order's quoted rate says.
"""

_SECONDS_PER_DAY = 86_400.0


class RewardDayAccrual:
    """Reward accrual for one market, banked per UTC day behind the $1 floor.

    The venue pays liquidity rewards per market per UTC day and refuses a
    payout below $1.00 ("The minimum reward payout is $1; amounts below this
    will not be paid", ``site/programs/liquidity-rewards.md:21``). That floor is
    a property of a *day's total*, never of an instantaneous rate: applying it
    to the rate would book a zero for every reading under $1/day and, worse,
    let a rate just above $1/day be credited as if it were paid continuously.
    This accumulator integrates the rate over real elapsed time instead, and
    the caller settles each UTC day against the floor at rollover.

    ``record`` integrates over ``[previous record, ts]``, so the first call
    only anchors the clock (there is no measured interval yet). Intervals that
    cross UTC midnight are split, because the payout period is the UTC day.
    The caller is responsible for passing only the intervals in which the
    order was actually reward-eligible: time before the 3.5 s minimum, or after
    a cancel, is not a measurable rate at all.
    """

    def __init__(self, condition_id: str = "") -> None:
        self.condition_id = condition_id
        self._last_ts: float | None = None
        self._pending: dict[date, float] = {}
        self._settled: set[date] = set()

    @property
    def last_ts(self) -> float | None:
        """The last timestamp the clock was advanced to (``None`` = never)."""
        return self._last_ts

    def record(self, ts: float, usd_per_day: float) -> None:
        """Accrue ``usd_per_day`` over the interval ending at ``ts``.

        ``ts`` is epoch seconds. A non-positive rate accrues nothing but still
        advances the clock, so an interval with no eligible resting order can
        never be charged to the next rate. A clock that does not advance is
        ignored: the interval is not measured twice.
        """
        ts = float(ts)
        last = self._last_ts
        if last is None:
            self._last_ts = ts
            return
        if ts <= last:
            return
        rate = float(usd_per_day)
        if rate > 0.0:
            start = last
            while start < ts:
                day = datetime.fromtimestamp(start, UTC).date()
                stop = min(ts, _utc_day_start(day + timedelta(days=1)))
                self._pending[day] = (
                    self._pending.get(day, 0.0)
                    + rate * (stop - start) / _SECONDS_PER_DAY
                )
                start = stop
        self._last_ts = ts

    def pending_utc_day(self, day: date) -> float:
        """USD accrued for ``day`` that has not been settled yet."""
        return self._pending.get(day, 0.0)

    def pending_days(self) -> tuple[date, ...]:
        """Every UTC day with unsettled accrual, oldest first."""
        return tuple(sorted(day for day, usd in self._pending.items() if usd != 0.0))

    def settle_utc_day(self, day: date) -> float:
        """Settle ``day`` against the venue's minimum payout.

        Returns the day's total when it clears $1.00 and ``0.0`` when it does
        not; either way the day is marked settled and its pending accrual is
        cleared, so a day can never pay twice. Call it only after the tick that
        closes the day has been recorded, or the pre-midnight tail lands after
        the settle.
        """
        if day in self._settled:
            return 0.0
        self._settled.add(day)
        total = self._pending.pop(day, 0.0)
        return total if total >= PROTOCOL_MIN_DAILY_PAYOUT_USD else 0.0


def _utc_day_start(day: date) -> float:
    """Epoch seconds of 00:00:00 UTC on ``day``."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()


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
    competing_variance: float | None = None
    low_confidence: bool = True
    competing_snapshot: float | None = None

    @property
    def provenance(self) -> str:
        if self.low_confidence:
            return f"LOW-CONFIDENCE n={self.competing_samples} (snapshot, not an epoch average)"
        spread = f", sd={math.sqrt(self.competing_variance):,.1f}" if self.competing_variance else ""
        return f"n={self.competing_samples}{spread}"


def measured_accrual(
    *,
    pool_usd_per_day: float | None,
    price: float,
    shares: float,
    min_size: float | None,
    max_spread_cents: float | None,
    leg_book: dict[str, Any] | None,
    other_book: dict[str, Any] | None,
    competing: CompetingEstimate | None = None,
    books_include_our_order: bool = False,
) -> AccrualShare | None:
    """The payout rate a resting bid earns per day, from its own measured books.

    This is the rate the engine banks per UTC day, so every input is a
    measurement and nothing has a fallback: the pool is the venue's published
    daily rate for the market, the band and the minimum size are the venue's
    published campaign parameters, ``price``/``shares`` are what is actually
    resting, and the mid comes from the current L2 snapshot of *both*
    outcome-token books. Returns ``None`` - and the caller must then credit
    nothing - when any of those is missing or when the measured competing score
    is zero (a singular denominator), because a payout without a measured
    denominator is a guess.

    The returned ``usd_per_day`` is **not** floored: the venue's $1.00 minimum
    applies to a market's daily total at UTC-day rollover, so the caller must
    bank this rate with :class:`RewardDayAccrual` and settle the day, and must
    only bank intervals in which the order was reward-eligible (rested at least
    ``REWARD_ELIGIBILITY_SECONDS``).

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

    ``books_include_our_order`` states whether the supplied L2 snapshots
    contain this order. The paper engine quotes into a simulated book, so the
    venue snapshot it reads never contains the order and the default (``False``)
    adds our score to a clean competitor total. An engine reading the venue's
    own book while its order rests there must pass ``True``: the snapshot then
    already carries our raw score, and adding it again would divide our claim
    by itself twice.
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
    our_raw = ((band - distance) / band) ** 2 * float(shares)
    our_score = our_raw * fraction
    # The venue's denominator is ``sum_makers Q_min``. A book snapshot that
    # includes our own resting order already carries our raw score, so adding
    # ``our_score`` on top of it counts us twice and understates the share
    # (by up to 2x when we are the only maker). Trim our raw contribution from
    # the measured total first, then add back the ``1/c``-penalised score the
    # venue actually counts; when the caller's books do not contain our order
    # (the paper engine quotes into a simulated book, not the venue's), the
    # competitor total is already clean and only the add-back applies.
    competitor_total = denominator
    if books_include_our_order:
        competitor_total = max(0.0, competitor_total - our_raw)
    denom = competitor_total + our_score
    share = our_score / denom if denom > 0.0 else 0.0
    usd_per_day = pool_usd_per_day * share
    # The rate is not floored here. The $1.00 minimum is a property of a
    # market's *daily total* and is applied at UTC-day rollover by
    # :class:`RewardDayAccrual`; flooring the instantaneous rate would discard
    # a real accrual on every sub-$1/day reading and can never reproduce the
    # venue's per-day payout.
    return _share(usd_per_day, share, our_score, True)


@dataclass(frozen=True)
class PairLeg:
    """One resting bid of a matched pair: its outcome, price and size.

    The pair is two legs at one share count - a YES bid and a NO bid - which is
    the neutral quote the live engine places. Prices are USDC per share and
    ``shares`` is that leg's still-resting size, so a partially filled pair
    scores exactly what is on the book.
    """

    outcome: str  # "YES" or "NO"
    price: float
    shares: float


def measured_pair_accrual(
    *,
    pool_usd_per_day: float | None,
    max_spread_cents: float | None,
    min_size: float | None,
    yes_book: dict[str, Any] | None,
    no_book: dict[str, Any] | None,
    resting: tuple[PairLeg, ...] = (),
    competing: CompetingEstimate | None = None,
    books_include_our_order: bool = False,
) -> AccrualShare | None:
    """The payout rate a matched pair earns per day, from its measured books.

    This is :func:`measured_accrual` for the two-sided quote. The venue scores
    the pair once: the YES bid contributes to ``Q_one``, the NO bid to
    ``Q_two``, and ``Q_min`` applies the documented two-sided rule. A full pair
    that rests both legs inside the band is therefore paid its raw score;
    pricing each leg as an independent one-sided quote would instead claim two
    ``Q_one / c`` payouts and divide the pool twice. A half-filled pair falls
    back to the venue's own one-sided rule (``Q_min = Q_one / c`` inside the
    band, zero outside it) because that is what the venue pays for what is
    actually resting.

    Everything else matches :func:`measured_accrual`: every input is a
    measurement with no fallback, a missing book, mid or zero measured
    competing score returns ``None`` (the caller must then credit nothing), and
    the returned rate is **not** floored - the venue's $1.00 minimum applies to
    a market's UTC-day total at rollover.

    ``books_include_our_order`` states whether the supplied snapshots already
    contain the legs; the paper trader quotes into a simulated book, so the
    default (``False``) adds only the pair's ``Q_min`` to a clean competitor
    total. Callers reading the venue's own book pass ``True`` and the legs' raw
    scores are trimmed from the measured total first.
    """
    if pool_usd_per_day is None or pool_usd_per_day <= 0.0:
        return None
    if min_size is None or min_size <= 0.0:
        return None
    if max_spread_cents is None or max_spread_cents <= 0.0:
        return None
    if not isinstance(yes_book, dict) or not isinstance(no_book, dict):
        return None
    mid_yes = mid(yes_book)
    mid_no = mid(no_book)
    if mid_yes is None or mid_no is None:
        return None
    snapshot = qualifying_score(
        yes_book, max_spread_cents=max_spread_cents, mid_price=mid_yes
    ) + qualifying_score(
        no_book, max_spread_cents=max_spread_cents, mid_price=mid_no
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

    # The venue's own pair score, computed once: YES bids are Q_one, NO bids are
    # Q_two, and the two-sided rule combines them. This is exactly the live
    # engine's ``compute_quote_scores``, so paper and live cannot drift.
    q_one = q_two = 0.0
    our_raw = 0.0
    for leg in resting:
        outcome = leg.outcome.upper()
        if outcome not in ("YES", "NO"):
            return None
        leg_mid = mid_yes if outcome == "YES" else mid_no
        price = float(leg.price)
        size = float(leg.shares)
        if not 0.0 < price < 1.0 or size <= 0.0:
            continue
        # ``compute_book_side_scores`` scores a NO bid around ``1 - mid_no``;
        # passing the complement keeps its Q_one/Q_two mapping while using this
        # book's own mid.
        leg_mid_yes = leg_mid if outcome == "YES" else 1.0 - leg_mid
        one, two = compute_book_side_scores(
            [OrderQuote(side="BUY", outcome=outcome, price=price, size=size)],
            mid_price_yes=leg_mid_yes,
            max_spread_cents=float(max_spread_cents),
            min_size=float(min_size),
        )
        q_one += one
        q_two += two
        our_raw += quadratic_order_score(
            max_spread_cents=float(max_spread_cents),
            spread_cents=order_spread_cents(price, leg_mid, "BUY"),
            size=size,
            min_size=float(min_size),
        )
    our_score = compute_minimum_score(q_one=q_one, q_two=q_two, midpoint=mid_yes)

    def _share(usd: float, share: float, qualifies: bool) -> AccrualShare:
        return AccrualShare(
            usd_per_day=usd,
            share=share,
            our_score=our_score,
            competing_score=denominator,
            pool_usd_per_day=pool_usd_per_day,
            qualifies=qualifies,
            competing_samples=samples,
            competing_variance=variance,
            low_confidence=low,
            competing_snapshot=latest,
        )

    if our_score <= 0.0:
        return _share(0.0, 0.0, False)
    competitor_total = denominator
    if books_include_our_order:
        competitor_total = max(0.0, competitor_total - our_raw)
    denom = competitor_total + our_score
    share = our_score / denom if denom > 0.0 else 0.0
    usd_per_day = pool_usd_per_day * share
    # The rate is not floored here; see :func:`measured_accrual`.
    return _share(usd_per_day, share, True)
