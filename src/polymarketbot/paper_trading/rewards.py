"""Polymarket Liquidity Rewards Quadratic Scoring Engine.

Implements the official Polymarket CLOB quadratic reward scoring formula:
    S(v, s) = ((v - s) / v)^2 * size
where:
    v = rewards_max_spread (in cents)
    s = distance from midpoint (in cents)
    size = order size in shares

Simulates the 1,440 daily sampling epochs (1 per minute).
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


def compute_quadratic_score(v_cents: float, s_cents: float, size: float) -> float:
    """Compute the quadratic reward score for an individual order.

    Args:
        v_cents: Maximum spread threshold in cents (rewards_max_spread).
        s_cents: Distance of the order price from midpoint in cents.
        size: Order size in shares.

    Returns:
        Quadratic score S(v, s) >= 0.0.
    """
    if v_cents <= 0.0 or size <= 0.0:
        return 0.0
    if s_cents > v_cents or s_cents < 0.0:
        return 0.0

    ratio = (v_cents - s_cents) / v_cents
    return (ratio ** 2) * size


def compute_competing_depth_score(
    bids: list[dict[str, Any]] | list[list[str]],
    asks: list[dict[str, Any]] | list[list[str]],
    mid_price: float,
    max_spread_cents: float,
) -> tuple[float, float, float]:
    """Compute the aggregated quadratic score and dollar depth of competing orders.

    Args:
        bids: Order book bids, list of {"price": "...", "size": "..."} or [price, size].
        asks: Order book asks, list of {"price": "...", "size": "..."} or [price, size].
        mid_price: Current book midpoint.
        max_spread_cents: Maximum spread threshold in cents.

    Returns:
        (total_competing_score, qualifying_bid_usd, qualifying_ask_usd)
    """
    if mid_price <= 0.0 or max_spread_cents <= 0.0:
        return 0.0, 0.0, 0.0

    total_score = 0.0
    qualifying_bid_usd = 0.0
    qualifying_ask_usd = 0.0

    def parse_entry(entry: Any) -> tuple[float, float]:
        if isinstance(entry, dict):
            return float(entry.get("price", 0.0)), float(entry.get("size", 0.0))
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            return float(entry[0]), float(entry[1])
        return 0.0, 0.0

    # Evaluate qualifying bids
    for b in bids:
        p, sz = parse_entry(b)
        if p <= 0.0 or sz <= 0.0:
            continue
        # Distance from mid in cents
        s_cents = (mid_price - p) * 100.0
        if 0.0 <= s_cents <= max_spread_cents:
            total_score += compute_quadratic_score(max_spread_cents, s_cents, sz)
            qualifying_bid_usd += sz * p

    # Evaluate qualifying asks
    for a in asks:
        p, sz = parse_entry(a)
        if p <= 0.0 or sz <= 0.0:
            continue
        # Distance from mid in cents
        s_cents = (p - mid_price) * 100.0
        if 0.0 <= s_cents <= max_spread_cents:
            total_score += compute_quadratic_score(max_spread_cents, s_cents, sz)
            qualifying_ask_usd += sz * p

    return total_score, qualifying_bid_usd, qualifying_ask_usd


def compute_epoch_payout(
    virtual_score: float,
    competing_score: float,
    daily_rate: float,
) -> tuple[float, float, float]:
    """Compute payout share and dollar rewards for a single 1-minute epoch.

    Args:
        virtual_score: Score earned by our virtual resting orders.
        competing_score: Total score from competing orderbook orders.
        daily_rate: Market daily reward rate in USD (rate_per_day).

    Returns:
        (pool_share_pct, epoch_payout_usd, projected_daily_payout_usd)
    """
    total_score = virtual_score + competing_score
    if total_score <= 0.0 or virtual_score <= 0.0:
        return 0.0, 0.0, 0.0

    share = virtual_score / total_score
    epoch_payout = (daily_rate / 1440.0) * share
    projected_daily = daily_rate * share

    return share * 100.0, epoch_payout, projected_daily


# Polymarket normalises a market's score before paying:
#   Q_two  = sum of scores when both sides rest
#   Q_one  = sum of scores when only one side rests
#   Q_min  = min(Q_one, Q_two)
#   Q_norm = Q_two + Q_min / 3
# A fully one-sided quote therefore earns a third of its raw score, not all of
# it. Summing bid+ask (the previous behaviour) overstates every single-sided
# quote by 3x, which is exactly the regime a "pull the threatened side" strategy
# creates, so the reward/risk trade-off cannot be measured without this.
TWO_SIDED_PENALTY = 3.0


def normalise_two_sided(bid_score: float, ask_score: float) -> float:
    """Q_norm for one market bucket from both sides' scores."""
    bid = max(0.0, float(bid_score))
    ask = max(0.0, float(ask_score))
    if bid > 0.0 and ask > 0.0:
        return bid + ask
    one_sided = max(bid, ask)
    return one_sided / TWO_SIDED_PENALTY
