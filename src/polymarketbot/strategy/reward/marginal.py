"""Marginal-yield maths of the reward allocator, as pure functions.

``R(c) = P * alpha * c / (D + alpha * c)`` and its derivative are the whole
optimisation: the MILP linearises this concave function, the KKT water-fill
inverts the derivative in closed form, and the candidate screen ranks on it.
Keeping the functions here means both solvers price the same curve, and the
per-market statement (:func:`allocation_for_capital`) is built one way.
"""

from __future__ import annotations

import math
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.models import MarketAllocation

log = get_logger("rewards_allocator")

# Documented venue rule: a market that would pay less than $1.00 in a daily
# epoch pays nothing at all. It is the only payout hurdle this module applies;
# every other threshold below is measured from the candidate set.
PROTOCOL_MIN_DAILY_PAYOUT_USD = 1.00


def _fill_side_of(candidate: dict[str, Any]) -> tuple[float, float]:
    """``(stay_fraction, fill_yield_per_usd_per_day)`` for a parsed candidate.

    ``(1.0, 0.0)`` means the caller priced no fill side at all - the
    accrual-only objective this module ran before the fill side existed. It is
    never the representation of an unmeasured fill *probability*: a candidate
    that carries ``p_fill`` has both fields, and :func:`fill_side` refuses a
    half-populated one.
    """
    side = candidate.get("fill_side")
    if side is None:
        return 1.0, 0.0
    return float(side[0]), float(side[1])


def calculate_alpha(max_spread_cents: float, spread_offset_cents: float) -> float:
    """Quadratic score multiplier alpha = ((v - s) / v) ** 2.

    ``s`` is the measured distance of *our* quote from the mid in cents, so
    the caller must pass the distance it will actually rest at (for the
    paper pillar that is ``bid + tick`` against the leg mid). A quote at or
    beyond the band scores nothing at all: the venue gives it zero, so the
    multiplier is 0.0 rather than a clamped positive value.
    """
    v = float(max_spread_cents)
    s = float(spread_offset_cents)
    if v <= 0.0 or s < 0.0 or s >= v:
        return 0.0
    return float(((v - s) / v) ** 2)


def calculate_min_floor_capital(
    daily_pool: float,
    competing_score: float,
    alpha: float,
    floor_target: float = PROTOCOL_MIN_DAILY_PAYOUT_USD,
) -> float:
    """Capital required to clear the venue's daily payout floor:

    P * (alpha * c) / (D + alpha * c) >= floor_target
    <=> alpha * c * (P - floor_target) >= floor_target * D
    <=> c >= (floor_target * D) / (alpha * (P - floor_target))

    ``floor_target`` defaults to the documented $1.00 protocol floor. It is
    not a hand-set safety multiplier: anything the venue would not pay is
    capital thrown away, and a larger buffer belongs to the optimizer's
    marginal condition, not to a constant.
    """
    target = float(floor_target)
    if daily_pool <= target or alpha <= 0.0:
        return float("inf")
    excess_pool_ratio = (daily_pool / target) - 1.0
    if excess_pool_ratio <= 1e-6:
        return float("inf")
    return float(competing_score / (alpha * excess_pool_ratio))


def evaluate_market_payout(
    capital: float,
    daily_pool: float | None,
    competing_score: float | None,
    alpha: float,
) -> float | None:
    """Evaluate non-linear daily cash payout function R_i(c_i).

    ``alpha`` here is the venue score earned per USDC of capital (see
    ``score_per_usd`` in :meth:`LiquidityRewardsAllocator.optimize_allocation_milp`),
    so ``alpha * capital`` is in the same share-weighted units as
    ``competing_score`` and the ratio is a genuine share of the pool.

    Returns ``None`` - never a number - when the pool or the competing
    score is missing, non-positive or non-finite. ``competing_score`` is the
    measured in-band depth of the market's competing book (see
    :func:`polymarketbot.strategy.reward.decision.market_competing_score`); a
    zero or absent one used to be read as "no competitors" and paid the
    whole pool on any size, which is a fabricated payout, not a share.
    """
    if daily_pool is None or competing_score is None:
        return None
    pool = float(daily_pool)
    competing = float(competing_score)
    if not math.isfinite(pool) or not math.isfinite(competing):
        return None
    if pool <= 0.0 or competing <= 0.0 or alpha <= 0.0:
        return None
    if capital <= 0.0:
        return 0.0
    user_score = alpha * capital
    denom = competing + user_score
    if denom <= 0.0:
        return None
    raw_payout = pool * (user_score / denom)
    # Documented venue floor: below it the market pays nothing.
    return raw_payout if raw_payout >= PROTOCOL_MIN_DAILY_PAYOUT_USD else 0.0


def marginal_payout(
    capital: float, daily_pool: float, competing_score: float, alpha: float
) -> float:
    """dR/dc = P * alpha * D / (D + alpha * c) ** 2, USD per USD per day.

    This is the KKT price of capital in one market: at the optimum every
    active market shares the same marginal yield. ``alpha`` is the venue
    score per USDC of capital, so this is USD per USDC per day.
    """
    if capital <= 0.0 or alpha <= 0.0 or daily_pool <= 0.0:
        return 0.0
    denom = competing_score + alpha * capital
    if denom <= 0.0:
        return 0.0
    return float(daily_pool * alpha * competing_score / (denom * denom))


def evaluate_market_total_yield(
    capital: float,
    daily_pool: float | None,
    competing_score: float | None,
    alpha: float,
    stay_fraction: float,
    fill_yield_usd_per_usd_per_day: float,
) -> float | None:
    """Expected **total** yield of ``capital`` in one market, USD per day.

        E[yield] = R(c) * stay_fraction + fill_yield * c

    The pool share is only earned while the bid is in-band and unfilled, so
    ``R(c)`` is multiplied by the measured ``1 - p_fill`` per horizon; the
    filled part of the capital earns the measured fill yield instead, which
    is negative whenever the measured adverse markout exceeds the distance
    from the mid. ``fill_yield_usd_per_usd_per_day`` and ``stay_fraction``
    come from the L2 tape tables, so neither term is a guess.

    Returns ``None`` when the accrual denominator is unmeasured, exactly as
    :func:`evaluate_market_payout` does: a missing denominator is missing
    data, never zero.
    """
    accrual = evaluate_market_payout(capital, daily_pool, competing_score, alpha)
    if accrual is None:
        return None
    return float(accrual) * float(stay_fraction) + float(
        fill_yield_usd_per_usd_per_day
    ) * float(capital)


def marginal_total_yield(
    capital: float,
    daily_pool: float,
    competing_score: float,
    alpha: float,
    stay_fraction: float,
    fill_yield_usd_per_usd_per_day: float,
) -> float:
    """dE[yield]/dc = dR/dc * stay_fraction + fill_yield, USD/USD/day.

    With ``fill_yield <= 0`` this is the accrual's marginal yield discounted
    by the probability the bid is filled, minus the measured drift on the
    part that is. It is the marginal condition every candidate is ranked on,
    so an empty book whose ``D`` is tiny wins only when its accrual still
    beats the yield available on a book that actually trades.
    """
    return (
        marginal_payout(capital, daily_pool, competing_score, alpha)
        * float(stay_fraction)
        + float(fill_yield_usd_per_usd_per_day)
    )


def fill_side(cand: dict[str, Any]) -> tuple[float, float] | None:
    """``(stay_fraction, fill_yield)`` for a candidate, or ``None`` when unpriced.

    ``None`` means the caller did not price the fill side at all (an
    accrual-only study); a candidate that carries ``p_fill`` is priced with
    it. The two modes are never mixed silently - the solver logs which one
    it ran in.
    """
    if cand.get("p_fill") is None and "fill_yield_usd_per_usd_per_day" not in cand:
        return None
    stay = cand.get("stay_fraction")
    fill = cand.get("fill_yield_usd_per_usd_per_day")
    if stay is None or fill is None:
        return None
    return float(stay), float(fill)


def allocation_for_capital(
    m: dict[str, Any], cap: float
) -> tuple[MarketAllocation, float] | None:
    """The per-market statement for ``cap``, and the unrounded payout behind it.

    ``None`` - with the reason logged - when the denominator stopped being
    measurable between the filter and here: fund nothing rather than book a
    guess. The caller sums the unrounded payout, so it is returned rather than
    read back off the rounded statement.
    """
    payout = evaluate_market_payout(cap, m["P"], m["D"], m["alpha"])
    if payout is None:
        log.warning(
            f"allocator: refusing {m['cand'].get('condition_id', '')[:10]} - "
            "payout denominator unmeasured at extraction"
        )
        return None
    roi_pct = (payout / cap) * 100.0 if cap > 0 else 0.0
    stay, fill = _fill_side_of(m)
    fill_usd = fill * cap
    cand_dict = m["cand"]
    allocation = MarketAllocation(
        condition_id=cand_dict.get("condition_id", ""),
        question=cand_dict.get("question", ""),
        allocated_capital=round(cap, 2),
        # Shares per leg. Dividing dollar capital by the *capital one share
        # commits* is the only conversion consistent with ``alpha`` being score
        # per dollar of that capital; dividing by the leg mid overstated the
        # order by 2-4x and the governor resized the engine to it.
        quote_size=round(cap / float(m.get("capital_per_share") or 1.0), 1),
        daily_reward_payout=round(payout, 2),
        daily_roi_pct=round(roi_pct, 2),
        min_floor_capital=round(m["c_floor"], 2),
        clears_floor=payout >= PROTOCOL_MIN_DAILY_PAYOUT_USD,
        daily_pool=round(m["P"], 2),
        competing_depth=round(m["D"], 1),
        mid_price=round(m["mid"], 4),
        tokens=cand_dict.get("raw_market", {}).get("tokens", []),
        yes_token=cand_dict.get("yes_token") or cand_dict.get("token_yes") or "",
        no_token=cand_dict.get("no_token") or cand_dict.get("token_no") or "",
        max_spread=float(cand_dict.get("max_spread", 0.0)),
        min_size=float(cand_dict.get("min_size", 0.0)),
        expected_fill_yield_usd_per_day=round(fill_usd, 4),
        expected_total_yield_usd_per_day=round(payout * stay + fill_usd, 4),
        p_fill=cand_dict.get("p_fill"),
        competing_samples=int(cand_dict.get("competing_samples") or 0),
        competing_low_confidence=bool(cand_dict.get("competing_low_confidence")),
    )
    return allocation, float(payout)
