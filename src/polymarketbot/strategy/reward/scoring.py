"""Polymarket Liquidity Rewards official scoring methodology.

Implements the official quadratic scoring function and epoch aggregation:
1. Order position scoring: S(v, s) = ((v - s) / v)^2 * size * multiplier
2. Complementary book side scores (Q_one and Q_two across YES and NO)
3. Minimum score with single-sided penalty (c = 3.0 in [0.10, 0.90], strict outside)
4. Sample score normalization across market makers
5. 1,440-minute daily epoch accumulation
6. Reward payout calculation with $1.00 minimum daily floor per market

There is **no minimum rest time** in the published rules. The venue samples the
book once per minute at a random offset and scores whatever rests at that
instant (``docs.polymarket.com/programs/liquidity-rewards``, equation 6); an
order that is on the book when the sample lands counts, and one that is not
does not. An earlier revision of this module gated scoring on a 3.5 s
"qualification timer" that appears nowhere in the documentation; it was
removed on 2026-11-10 because it silently zeroed orders that the venue pays.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class OrderQuote:
    """A resting limit order submitted to the CLOB."""

    side: str  # "BUY" (bid) or "SELL" (ask)
    outcome: str  # "YES" or "NO"
    price: float  # order price in (0, 1)
    size: float  # quantity in shares


def order_spread_cents(order_price: float, mid_price: float, side: str) -> float:
    """Calculate distance in cents from midpoint for an order.

    For a bid (BUY), order sits below mid: spread = (mid - price) * 100.
    For an ask (SELL), order sits above mid: spread = (price - mid) * 100.

    ``mid_price`` is the **raw** L2 mid ``(best_bid + best_ask) / 2``. The
    published rule measures ``s`` from the *size-cutoff-adjusted* midpoint
    (``docs.polymarket.com/programs/liquidity-rewards``, variable table), but
    the adjustment is never defined - no published formula maps the minimum
    size or the book to an adjusted mid. This module therefore measures from
    the raw mid and the reward pillar's accrual log states the resulting known
    bias every window; inventing an adjustment would be a guess dressed as a
    measurement.
    """
    s = side.upper() if not (side == "BUY" or side == "SELL") else side
    if s == "BUY":
        return max(0.0, (mid_price - order_price) * 100.0)
    elif s == "SELL":
        return max(0.0, (order_price - mid_price) * 100.0)
    else:
        raise ValueError(f"Unknown order side: {side}")


def quadratic_order_score(
    max_spread_cents: float,
    spread_cents: float,
    size: float,
    multiplier: float = 1.0,
    min_size: float = 0.0,
) -> float:
    """Calculate quadratic reward score for a single order: S(v, s).

    S(v, s) = ((v - s) / v)^2 * size * multiplier

    Args:
        max_spread_cents: Qualifying spread threshold (v) in cents.
        spread_cents: Distance (s) in cents from size-cutoff adjusted midpoint.
        size: Share quantity of the order.
        multiplier: Optional in-game or promotional multiplier (b).
        min_size: Minimum qualifying size for the market.

    Returns:
        Quadratic score for the order, or 0.0 if not qualifying.

    There is no resting-time input: the published rule scores whatever is on
    the book at the sample instant, so an order has no minimum age to clear.
    """
    if max_spread_cents <= 0 or size <= 0:
        return 0.0
    if size < min_size:
        return 0.0
    if spread_cents > max_spread_cents or spread_cents < 0.0:
        return 0.0

    ratio = (max_spread_cents - spread_cents) / max_spread_cents
    return (ratio**2) * size * multiplier


def compute_book_side_scores(
    quotes: Sequence[OrderQuote],
    mid_price_yes: float,
    max_spread_cents: float,
    min_size: float = 0.0,
    multiplier: float = 1.0,
) -> tuple[float, float]:
    """Calculate Q_one and Q_two for a collection of maker orders.

    According to Polymarket docs:
    In binary markets with YES (m1) and NO (m'):
    Q_one = Sum S(v, Spread_bid_YES) * Size + Sum S(v, Spread_ask_NO) * Size
    Q_two = Sum S(v, Spread_ask_YES) * Size + Sum S(v, Spread_bid_NO) * Size

    If orders are quoted on YES only:
    mid_price_yes is used, and:
    Q_one accumulates YES bids; Q_two accumulates YES asks.
    """
    mid_price_no = 1.0 - mid_price_yes
    q_one = 0.0
    q_two = 0.0

    for q in quotes:
        if q.size < min_size:
            continue

        outcome = q.outcome.upper() if not (q.outcome == "YES" or q.outcome == "NO") else q.outcome
        side = q.side.upper() if not (q.side == "BUY" or q.side == "SELL") else q.side

        if outcome == "YES":
            spr = order_spread_cents(q.price, mid_price_yes, side)
            score = quadratic_order_score(
                max_spread_cents=max_spread_cents,
                spread_cents=spr,
                size=q.size,
                multiplier=multiplier,
                min_size=min_size,
            )
            if side == "BUY":
                q_one += score
            elif side == "SELL":
                q_two += score
        elif outcome == "NO":
            spr = order_spread_cents(q.price, mid_price_no, side)
            score = quadratic_order_score(
                max_spread_cents=max_spread_cents,
                spread_cents=spr,
                size=q.size,
                multiplier=multiplier,
                min_size=min_size,
            )
            # NO ask complements YES bid -> contributes to Q_one
            if side == "SELL":
                q_one += score
            # NO bid complements YES ask -> contributes to Q_two
            elif side == "BUY":
                q_two += score

    return q_one, q_two


# Documented epoch structure (equation 6): the order book is sampled once per
# minute at a random offset, so one UTC day holds up to 1,440 samples.
PROTOCOL_EPOCH_SAMPLES = 1_440


# Documented protocol constants of the two-sided rule below: inside the
# midpoint band a one-sided maker is paid at 1/c of its raw score, outside the
# band it is paid nothing at all.
TWO_SIDED_MIDPOINT_LOW = 0.10
TWO_SIDED_MIDPOINT_HIGH = 0.90
SINGLE_SIDED_PENALTY = 3.0


def compute_minimum_score(
    q_one: float,
    q_two: float,
    midpoint: float,
    c_penalty: float = SINGLE_SIDED_PENALTY,
) -> float:
    """Calculate Q_min applying two-sided vs single-sided rules.

    Official Polymarket specification:
    1. If midpoint is in [0.10, 0.90]:
       Single-sided liquidity still scores at reduced rate / c:
       Q_min = max(min(Q_one, Q_two), max(Q_one, Q_two) / c)
    2. If midpoint is outside [0.10, 0.90] (i.e. < 0.10 or > 0.90):
       Liquidity must be strictly two-sided:
       Q_min = min(Q_one, Q_two)
    """
    if TWO_SIDED_MIDPOINT_LOW <= midpoint <= TWO_SIDED_MIDPOINT_HIGH:
        return max(min(q_one, q_two), max(q_one, q_two) / c_penalty)
    else:
        return min(q_one, q_two)


def single_sided_score_fraction(
    midpoint: float, c_penalty: float = SINGLE_SIDED_PENALTY
) -> float:
    """Fraction of its raw quadratic score a one-sided maker is actually paid.

    Substituting ``q_two = 0`` into :func:`compute_minimum_score` gives
    ``Q_min = q_one / c`` inside the two-sided midpoint band and ``Q_min = 0``
    outside it. Every caller that prices a quote resting on **one** side of a
    binary market (a lone bid on the cheaper leg, say) must multiply its raw
    score ``alpha * shares`` by this fraction; using the raw score instead
    claims three times the payout, and claims a payout at all in the markets
    where the venue pays nothing.

    ``midpoint`` is the market's YES price, which is the leg mid when the quoted
    leg is YES and ``1 - leg mid`` when it is NO; either way the band reduces to
    ``0.10 <= leg mid`` for the cheaper leg, because that leg prices at or below
    0.50 by construction.
    """
    if not TWO_SIDED_MIDPOINT_LOW <= midpoint <= TWO_SIDED_MIDPOINT_HIGH:
        return 0.0
    return 1.0 / float(c_penalty)


def normalize_sample_score(
    maker_q_min: float,
    all_makers_q_min_sum: float,
) -> float:
    """Compute Q_normal for a single sample minute.

    Q_normal = Q_min / sum(Q_min_all)
    """
    if all_makers_q_min_sum <= 0.0 or maker_q_min <= 0.0:
        return 0.0
    return maker_q_min / all_makers_q_min_sum


def calculate_epoch_payout(
    maker_epoch_score: float,
    total_epoch_score: float,
    rate_per_day: float,
    payout_floor_usd: float = 1.00,
) -> tuple[float, float, float]:
    """Calculate daily reward payout for a maker across 1,440-minute epoch.

    Args:
        maker_epoch_score: Sum of Q_normal for maker across all samples in epoch.
        total_epoch_score: Sum of Q_epoch across all makers in epoch.
        rate_per_day: Daily reward allocation in USD/USDC for this market.
        payout_floor_usd: Polymarket minimum daily payout threshold ($1.00).

    Returns:
        tuple of (reward_share, gross_payout_usd, net_payout_usd)
        where net_payout_usd is 0.0 if gross_payout_usd < payout_floor_usd.
    """
    if total_epoch_score <= 0.0 or maker_epoch_score <= 0.0 or rate_per_day <= 0.0:
        return 0.0, 0.0, 0.0

    share = maker_epoch_score / total_epoch_score
    gross_payout = share * rate_per_day
    net_payout = gross_payout if gross_payout >= payout_floor_usd else 0.0
    return share, gross_payout, net_payout


DEFAULT_COMPETITOR_LADDER = (
    (0.35, 0.5),  # 35% of qualifying depth at s = 0.5c
    (0.30, 1.0),  # 30% of qualifying depth at s = 1.0c
    (0.20, 2.0),  # 20% of qualifying depth at s = 2.0c
    (0.15, 3.5),  # 15% of qualifying depth at s = 3.5c
)


def compute_competitor_ladder_score(
    max_spread_cents: float,
    qualifying_depth: float,
    ladder: Sequence[tuple[float, float]] = DEFAULT_COMPETITOR_LADDER,
) -> float:
    """Compute quadratic score for competing liquidity distributed across a multi-tier ladder.

    Eliminates Jensen's Inequality distortion from lumping all competitor depth at one spread.
    Default distribution:
      - 35% of qualifying depth at s = 0.5c
      - 30% of qualifying depth at s = 1.0c
      - 20% of qualifying depth at s = 2.0c
      - 15% of qualifying depth at s = 3.5c
    """
    total_score = 0.0
    for weight, spread_cents in ladder:
        size = qualifying_depth * weight
        total_score += quadratic_order_score(
            max_spread_cents=max_spread_cents,
            spread_cents=spread_cents,
            size=size,
        )
    return total_score
