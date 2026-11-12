"""Exact KKT water-filling fallback for the reward allocator.

The allocator's MILP linearises a concave share function; this module inverts
its marginal condition in closed form, which is the exact optimum for the
budget-constrained separable problem. It runs when OR-Tools is unavailable or
does not converge, and it is the reference the MILP is checked against.
"""

from __future__ import annotations

import math
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.marginal import (
    _fill_side_of,
    allocation_for_capital,
    evaluate_market_total_yield,
)
from polymarketbot.strategy.reward.models import (
    MarketAllocation,
    PortfolioAllocationResult,
)

log = get_logger("rewards_allocator")


def solve_water_filling(
    parsed_candidates: list[dict[str, Any]],
    bankroll_usd: float,
    usable_capital: float,
    cash_reserve: float,
) -> PortfolioAllocationResult:
    """Exact KKT water-filling for the separable concave allocation.

    R_i(c) = P_i * alpha_i * c / (D_i + alpha_i * c) is concave and strictly
    increasing, so the budget-constrained optimum equalises the marginal
    yield dR_i/dc = P_i alpha_i D_i / (D_i + alpha_i c_i)^2 = lambda across
    every active market, with c_i >= c_i_min. The closed form inverts to

        c_i(lambda) = (sqrt(P_i alpha_i D_i / lambda) - D_i) / alpha_i,

    and lambda is found by bisection on sum_i c_i(lambda) = usable. Markets
    whose c_i(lambda) falls below their activation lot are funded at the lot
    and removed, then lambda is re-solved. Every candidate here carries a
    measured, positive competing score - an unmeasured one is refused in
    ``LiquidityRewardsAllocator.optimize_allocation_milp`` - so no market is
    ever funded on a zero denominator.

    This replaces the previous fallback, which put the entire bankroll into
    a single market regardless of bankroll size.
    """
    free = [m for m in parsed_candidates if m["c_min"] <= usable_capital]
    # A market whose activation lot does not pay for itself is not "starved"
    # by the water level - it is unviable, and funding it at the lot would
    # book a negative total yield. The fill term makes that reachable: a
    # book whose measured markout exceeds its spread can have a positive
    # accrual and still lose money on every fill.
    viable: list[dict[str, Any]] = []
    for m in free:
        total_at_lot = evaluate_market_total_yield(
            m["c_min"], m["P"], m["D"], m["alpha"], *_fill_side_of(m)
        )
        if total_at_lot is None or total_at_lot <= 0.0:
            continue
        viable.append(m)
    free = viable
    remaining = usable_capital
    funded: list[tuple[dict[str, Any], float]] = []

    def lot_yield_density(market: dict[str, Any]) -> float:
        """Total yield of this market's activation lot, per dollar of lot.

        The lot is an indivisible cash commitment, so the unit of choice is the
        whole lot and the value it buys is ``R(c_min)`` - not the marginal
        ``dR/dc``, which describes a divisible allocation it cannot have.
        """
        lot_yield = evaluate_market_total_yield(
            market["c_min"],
            market["P"],
            market["D"],
            market["alpha"],
            *_fill_side_of(market),
        )
        if lot_yield is None or market["c_min"] <= 0.0:
            return 0.0
        return float(lot_yield) / float(market["c_min"])

    while free and remaining > 1e-9:
        caps = water_level_caps(free, remaining)
        starved = [m for m, cap in zip(free, caps) if cap < m["c_min"]]
        if not starved:
            for m, cap in zip(free, caps):
                funded.append((m, cap))
            remaining = 0.0
            break
        # A market whose water level is below its activation lot cannot take
        # the divisible allocation the KKT solution describes; a market at or
        # above it can. Fund the divisible optimum first, then spend only the
        # cash that optimum left idle on indivisible lots, best yield density
        # first.
        #
        # The previous version funded *every* starved lot unconditionally, so a
        # budget that could not carry all of them was over-allocated by the
        # excess (two $60 lots on a $100 budget spent $120) and the caller's
        # cash invariant broke after the decision to take it.
        divisible = [(m, cap) for m, cap in zip(free, caps) if cap >= m["c_min"]]
        for m, cap in divisible:
            funded.append((m, cap))
            remaining -= cap
        divisible_ids = {id(m) for m, _cap in divisible}
        free = [m for m in free if id(m) not in divisible_ids]
        for m in sorted(free, key=lot_yield_density, reverse=True):
            if m["c_min"] > remaining + 1e-9:
                continue
            funded.append((m, m["c_min"]))
            remaining -= m["c_min"]
        free = []

    if not funded:
        return PortfolioAllocationResult(
            bankroll_usd=bankroll_usd,
            usable_capital=usable_capital,
            cash_reserve=cash_reserve,
            total_allocated=0.0,
            total_daily_payout=0.0,
            total_monthly_payout=0.0,
            portfolio_daily_roi_pct=0.0,
            portfolio_monthly_roi_pct=0.0,
            active_markets_count=0,
            allocations=[],
            solver_name="KKT_WATER_FILLING",
        )

    allocations: list[MarketAllocation] = []
    total_allocated = 0.0
    total_payout = 0.0
    for m, cap in funded:
        cap = max(0.0, min(cap, usable_capital))
        priced = allocation_for_capital(m, cap)
        if priced is None:
            continue
        allocation, payout = priced
        allocations.append(allocation)
        total_allocated += cap
        total_payout += payout

    allocations.sort(key=lambda a: a.expected_total_yield_usd_per_day, reverse=True)
    daily_roi = (total_payout / total_allocated) * 100.0 if total_allocated > 0 else 0.0
    return PortfolioAllocationResult(
        bankroll_usd=round(bankroll_usd, 2),
        usable_capital=round(usable_capital, 2),
        cash_reserve=round(cash_reserve, 2),
        total_allocated=round(total_allocated, 2),
        total_daily_payout=round(total_payout, 2),
        total_monthly_payout=round(total_payout * 30.0, 2),
        portfolio_daily_roi_pct=round(daily_roi, 2),
        portfolio_monthly_roi_pct=round(daily_roi * 30.0, 2),
        active_markets_count=len(allocations),
        allocations=allocations,
        solver_name="KKT_WATER_FILLING",
    )


def water_level_caps(markets: list[dict[str, Any]], budget: float) -> list[float]:
    """c_i(lambda) for every market, with lambda solved by bisection.

    The marginal condition is the **total** yield
    ``dR/dc * stay + fill = lambda``, whose inverse is

        c_i(lambda) = (sqrt(P_i alpha_i D_i stay_i / (lambda - fill_i)) - D_i) / alpha_i

    so a candidate carrying a measured fill side is sized against the yield
    it actually keeps, and one with ``lambda <= fill`` (its accrual cannot
    beat its own fill yield) is funded nothing.
    """

    def demand(lam: float) -> list[float]:
        out = []
        for m in markets:
            stay, fill = _fill_side_of(m)
            excess = lam - fill
            if excess <= 0.0:
                out.append(0.0)
                continue
            base = m["P"] * m["alpha"] * m["D"] * stay / excess
            out.append(max(0.0, (math.sqrt(base) - m["D"]) / m["alpha"]))
        return out

    lo, hi = 1e-18, 1.0
    guard = 0
    while sum(demand(hi)) > budget and hi < 1e18 and guard < 200:
        hi *= 10.0
        guard += 1
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if sum(demand(mid)) > budget:
            lo = mid
        else:
            hi = mid
    return demand(hi)
