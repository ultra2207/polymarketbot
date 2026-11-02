"""Polymarket Liquidity Rewards official scoring methodology.

Implements the official quadratic scoring function and epoch aggregation:
1. Order position scoring: S(v, s) = ((v - s) / v)^2 * size * multiplier
2. Complementary book side scores (Q_one and Q_two across YES and NO)
3. Minimum score with single-sided penalty (c = 3.0 in [0.10, 0.90], strict outside)
4. Sample score normalization across market makers
5. 1,440-minute daily epoch accumulation
6. Reward payout calculation with $1.00 minimum daily floor per market
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
    duration_seconds: float = 60.0  # seconds resting on book (must be >= 3.5s)


def order_spread_cents(order_price: float, mid_price: float, side: str) -> float:
    """Calculate distance in cents from midpoint for an order.

    For a bid (BUY), order sits below mid: spread = (mid - price) * 100.
    For an ask (SELL), order sits above mid: spread = (price - mid) * 100.
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
    duration_seconds: float = 60.0,
    min_duration_seconds: float = 3.5,
) -> float:
    """Calculate quadratic reward score for a single order: S(v, s).

    S(v, s) = ((v - s) / v)^2 * size * multiplier

    Args:
        max_spread_cents: Qualifying spread threshold (v) in cents.
        spread_cents: Distance (s) in cents from size-cutoff adjusted midpoint.
        size: Share quantity of the order.
        multiplier: Optional in-game or promotional multiplier (b).
        min_size: Minimum qualifying size for the market.
        duration_seconds: Duration the order has rested on the book.
        min_duration_seconds: Polymarket rule requiring resting >= 3.5s.

    Returns:
        Quadratic score for the order, or 0.0 if not qualifying.
    """
    if max_spread_cents <= 0 or size <= 0:
        return 0.0
    if size < min_size:
        return 0.0
    if duration_seconds < min_duration_seconds:
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
        if q.size < min_size or q.duration_seconds < 3.5:
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
                duration_seconds=q.duration_seconds,
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
                duration_seconds=q.duration_seconds,
            )
            # NO ask complements YES bid -> contributes to Q_one
            if side == "SELL":
                q_one += score
            # NO bid complements YES ask -> contributes to Q_two
            elif side == "BUY":
                q_two += score

    return q_one, q_two


def compute_minimum_score(
    q_one: float,
    q_two: float,
    midpoint: float,
    c_penalty: float = 3.0,
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
    if 0.10 <= midpoint <= 0.90:
        return max(min(q_one, q_two), max(q_one, q_two) / c_penalty)
    else:
        return min(q_one, q_two)


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
