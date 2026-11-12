"""Reward pillar: quote the cheaper leg inside the venue's reward band.

The venue pays a daily pool to resting size scored by
``size * ((v - |price - mid|) / v) ** 2`` inside a maximum-spread band ``v``.
This module turns a measured universe into allocator candidates, hands them to
the package optimizer, and converts the optimizer's capital into tick-valid
resting bids on the cheaper outcome token.  Nothing here invents a price: the
quote is one venue tick inside the touch, capped at the highest price that
still rests below the ask, and a market whose quote falls outside the band is
not a candidate.

Every pricing function is a pure function of its arguments; the caller owns all
I/O. The one piece of state is :class:`CompetingScoreWindow`, which the caller
creates and feeds explicitly (it is the epoch-average estimator, and it is
passed in, never global).

The pillar is split by seam, and this module is the public surface for all of
it: ``competition`` measures the denominator, ``quote_candidates`` builds and
scores allocator candidates, ``accrual`` prices the published ``R(c)`` path,
``fill_measurement``/``fill_tape`` price and audit the fill side, and
``quote_plan`` turns allocated capital into ticks and commits it within cash.
The tick decision itself (:func:`allocate`, :func:`plan_quotes`) lives here.

Units: prices, mids and ticks are fractions of 1.00 USDC per share; spreads
are cents; sizes are shares; ``daily_pool`` and capital are USDC per day and
USDC.
"""

from __future__ import annotations

from typing import Any

from polymarketbot.strategy.reward.accrual import AccrualShare, measured_accrual
from polymarketbot.strategy.reward.allocator import (
    PROTOCOL_MIN_DAILY_PAYOUT_USD,
    LiquidityRewardsAllocator,
    PortfolioAllocationResult,
)
from polymarketbot.strategy.reward.competition import (
    MIN_CONFIDENT_SAMPLES,
    PROTOCOL_SAMPLE_INTERVAL_S,
    CompetingDetail,
    CompetingEstimate,
    CompetingScoreWindow,
    market_competing_detail,
    market_competing_score,
)
from polymarketbot.strategy.reward.fill_measurement import (
    MarketFillYield,
    expected_fill_yield,
    markout_cost_usd,
)
from polymarketbot.strategy.reward.fill_tape import (
    FillDecision,
    FillTape,
    fill_audit,
    fill_tape,
    measured_fill_shares,
    reset_fill_tape,
)
from polymarketbot.strategy.reward.quote_candidates import reward_candidates
from polymarketbot.strategy.reward.quote_plan import (
    RewardQuote,
    commit_quotes,
    quotes_from_allocation,
)
from polymarketbot.strategy.reward.scoring import (
    PROTOCOL_EPOCH_SAMPLES,
    SINGLE_SIDED_PENALTY,
    single_sided_score_fraction,
)

__all__ = [
    "MIN_CONFIDENT_SAMPLES",
    "PROTOCOL_EPOCH_SAMPLES",
    "PROTOCOL_MIN_DAILY_PAYOUT_USD",
    "PROTOCOL_SAMPLE_INTERVAL_S",
    "SINGLE_SIDED_PENALTY",
    "AccrualShare",
    "CompetingDetail",
    "CompetingEstimate",
    "CompetingScoreWindow",
    "FillDecision",
    "FillTape",
    "MarketFillYield",
    "PortfolioAllocationResult",
    "RewardQuote",
    "allocate",
    "commit_quotes",
    "expected_fill_yield",
    "fill_audit",
    "fill_tape",
    "market_competing_detail",
    "market_competing_score",
    "markout_cost_usd",
    "measured_accrual",
    "measured_fill_shares",
    "plan_quotes",
    "quotes_from_allocation",
    "reset_fill_tape",
    "reward_candidates",
    "single_sided_score_fraction",
]


def allocate(
    candidates: list[dict[str, Any]], bankroll_usd: float
) -> PortfolioAllocationResult:
    """Run the package optimizer over ``candidates`` with ``bankroll_usd``.

    Returns the optimizer's statement unchanged: the reserve policy, the MILP
    and its fallbacks are the allocator's, not this pillar's.  Units:
    ``bankroll_usd`` and every returned capital/payout figure are USDC;
    ``candidates`` carry the units documented on :func:`reward_candidates`.
    """
    return LiquidityRewardsAllocator().optimize_allocation_milp(
        candidates, bankroll_usd=bankroll_usd
    )


def plan_quotes(
    candidates: list[dict[str, Any]],
    cash_usd: float,
    resting: dict[str, tuple[float, float]] | None = None,
    state: dict[str, Any] | None = None,
) -> list[RewardQuote]:
    """The pillar's tick decision: optimise, then commit within cash.

    ``allocate`` answers "where would this cash go if the book were empty";
    :func:`commit_quotes` answers "and what may rest given the cash already
    committed". Splitting the two keeps the optimizer a pure function of its
    inputs and makes the cash invariant enforce-able on the real allocator
    output rather than on a hand-scaled budget. See :func:`commit_quotes` for
    the measurement behind the reserve ledger.

    ``state``, when given, receives the optimum's KKT price of capital
    (``"shadow_yield"``, USD per USD per day). The engine publishes it as the
    opportunity-cost hurdle every other pillar must beat to use the same
    dollar; ``None`` means no allocation was solved, which the other pillars
    treat as missing data rather than as a zero hurdle.
    """
    cash = max(0.0, float(cash_usd))
    if cash <= 0.0 or not candidates:
        if state is not None:
            state["shadow_yield"] = None
        return []
    allocation = allocate(candidates, cash)
    if state is not None:
        state["shadow_yield"] = getattr(
            allocation, "shadow_yield_floor_usd_per_usd_per_day", None
        )
    return commit_quotes(
        quotes_from_allocation(allocation, candidates, {}),
        cash,
        resting or {},
    )
