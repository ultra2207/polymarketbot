"""Quote sizing and cash eligibility: the optimizer's capital, in ticks."""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.allocator import PortfolioAllocationResult

log = get_logger("polymarketbot.strategy.reward.decision")


def _as_float(value: Any, missing: float) -> float:
    """``float(value)``, or ``missing`` when the field is absent.

    Used only for fields whose absence is a documented mode (an accrual-only
    study has no fill side), never to turn an unmeasured *measurement* into a
    number: every such field stays ``None`` all the way to its consumer.
    """
    if value is None:
        return float(missing)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(missing)


@dataclass(frozen=True)
class RewardQuote:
    """One resting bid the reward pillar wants to place.

    ``shares`` is tick-valid and never below the market's reward minimum size;
    ``daily_payout`` is the optimizer's estimate for the capital behind it.
    """

    condition_id: str
    question: str
    token_id: str
    price: float
    shares: float
    daily_payout: float
    competing_score: float
    max_spread_cents: float
    min_size: float
    strategy: str = "RewardHarvester"
    p_fill: Optional[float] = None
    """Measured P(this bid is filled within one quote horizon). ``None`` = the
    fill side was not priced for this quote (accrual-only study)."""
    fill_yield_usd_per_usd_per_day: float = 0.0
    """Expected fill-side yield of the capital behind this quote, USD per USD
    per day, net of the measured adverse markout."""




def quotes_from_allocation(
    result: PortfolioAllocationResult,
    candidates: list[dict[str, Any]],
    books: dict[str, dict[str, Any]],
) -> list[RewardQuote]:
    """Convert allocated capital into tick-valid resting bids.

    ``shares = allocated_capital / quote_price`` floored to whole shares, the
    venue's minimum-order unit; a quote is dropped when it carries no capital,
    prices outside (0, 1), or floors below the market's ``min_size`` shares.
    ``books`` is the snapshot the candidates were built from and is accepted so
    callers pass one book set through the pipeline; sizing needs only the
    candidate's quote price.

    Units: capital is USDC, ``price`` is USDC per share, ``shares`` is shares,
    ``daily_payout`` is USDC per day, ``max_spread_cents`` is cents,
    ``min_size`` is shares.
    """
    by_condition = {candidate["condition_id"]: candidate for candidate in candidates}
    quotes: list[RewardQuote] = []
    for allocation in result.allocations:
        candidate = by_condition.get(allocation.condition_id)
        if candidate is None:
            continue
        if allocation.allocated_capital <= 0.0:
            continue
        price = float(candidate["quote_price"])
        if not 0.0 < price < 1.0:
            continue
        tick = float(candidate["tick"])
        if tick <= 0.0:
            continue
        raw_shares = allocation.allocated_capital / price
        # The price tick quantises *prices*, not sizes; shares are whole units
        # because the venue's minimum order is published as a share count.
        shares = float(math.floor(raw_shares + 1e-9))
        min_size = float(candidate["min_size"])
        if min_size <= 0.0 or shares < min_size:
            continue
        quotes.append(
            RewardQuote(
                condition_id=str(candidate["condition_id"]),
                question=str(candidate["question"]),
                token_id=str(candidate["quote_token"]),
                price=price,
                shares=shares,
                daily_payout=float(allocation.daily_reward_payout),
                competing_score=float(candidate["competing_score"]),
                max_spread_cents=float(candidate["max_spread"]),
                min_size=min_size,
                p_fill=candidate.get("p_fill"),
                fill_yield_usd_per_usd_per_day=_as_float(
                    candidate.get("fill_yield_usd_per_usd_per_day"), 0.0
                ),
            )
        )
    return quotes




def commit_quotes(
    ideal: list[RewardQuote],
    cash_usd: float,
    resting: Mapping[str, tuple[float, float]],
) -> list[RewardQuote]:
    """The subset of ``ideal`` the pillar can afford, given what already rests.

    ``allocate`` is handed the whole balance every tick and re-derives sizes,
    so on its own it prices a *fresh* book. The engine, however, keeps a
    resting bid whose price has not moved instead of cancel-and-replacing it,
    which means the cash behind an earlier tick's quote is still committed
    while this tick's plan spends the same dollars again. The 2026-11-10
    10:10-10:40 window measured the result: 48 reward bids resting $1,445.65
    while cash was $1,000.51, i.e. 44 % more notional on the book than the
    pillar could settle. No fill occurred, so nothing was refused; had the
    tape filled that book, ``Ledger.buy`` would have refused the overflow and
    silently dropped fills the pillar had already decided to take.

    This function is the reserve ledger. Quotes are accepted in descending
    marginal yield (``daily_payout`` per dollar of notional) until the cash is
    spent, and a quote whose notional does not fit is **not** returned: the
    engine cancels any resting bid it is not asked to keep, so an unaffordable
    stale bid is retired instead of being allowed to over-commit.

    A resting bid is accepted at the size it is already resting at, not at the
    freshly optimised size, because the engine keeps the existing order when
    the price is unchanged; counting the new size while the old one rests is
    exactly the double-spend this function exists to prevent. The residual
    (which can only be smaller than the fresh plan) is deliberately not
    redistributed: re-sizing a kept order needs a cancel/replace, and holding
    a smaller quote for one more tick costs a fraction of one tick's accrual.

    ``ideal`` is the optimizer's statement for this tick (see
    :func:`quotes_from_allocation`), ``cash_usd`` the pillar's spendable cash,
    ``resting`` maps token id to ``(price, shares)`` of the bids currently on
    the book and not cancelled. Returns quotes whose notional sums to at most
    ``cash_usd``. Units: USDC, shares, USDC per share.
    """
    cash = max(0.0, float(cash_usd))
    if cash <= 0.0 or not ideal:
        return []
    resting = resting or {}
    # Rank by expected *total* yield per dollar, the same objective the
    # optimizer maximised: the accrual alone would rank the emptiest book first,
    # and an empty book is exactly where a fill - the other half of the yield -
    # never happens.
    ordered = sorted(
        ideal,
        key=lambda quote: (
            quote.daily_payout * (1.0 - _as_float(quote.p_fill, 0.0))
            + quote.fill_yield_usd_per_usd_per_day * quote.price * quote.shares
        ) / max(quote.price * quote.shares, 1e-9),
        reverse=True,
    )
    committed: list[RewardQuote] = []
    remaining = cash
    already = 0
    for quote in ordered:
        entry = resting.get(quote.token_id)
        # A resting bid only counts as kept when it prices where this tick's
        # plan does; a moved price is a cancel, and a cancelled order releases
        # its cash, so it must not be charged against this budget.
        kept = None
        if entry is not None:
            price, shares = float(entry[0]), float(entry[1])
            if shares >= quote.min_size and abs(price - quote.price) <= 1e-9:
                kept = shares
        notional = (kept if kept is not None else quote.shares) * quote.price
        if notional > remaining:
            continue
        remaining -= notional
        already += kept is not None
        committed.append(quote if kept is None else dataclasses.replace(quote, shares=kept))
    # Logged every tick, not only on a shortfall: ``already resting`` versus
    # ``new`` is the plan's own stability, and the engine cancels every resting
    # bid this plan omits. The 11:48-12:19 window issued >=156 cancels and >=191
    # placements over 110 ticks on a 32.7-quote book (3.15 quotes of churn per
    # tick); it cost 0.09-0.13 % of the window's $9.09 accrual, so churn was not
    # what kept the book at zero fills.
    log.info(
        f"reward reserve: kept {len(committed)}/{len(ordered)} quotes, "
        f"${cash - remaining:,.2f} committed of ${cash:,.2f} cash "
        f"({len(ordered) - len(committed)} unaffordable; "
        f"{already} already resting, {len(committed) - already} new)"
    )
    return committed

