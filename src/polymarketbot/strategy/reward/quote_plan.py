"""Quote sizing and cash eligibility: the optimizer's capital, in ticks."""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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
    p_fill: float | None = None
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
    """Convert allocated pair capital into tick-valid resting bids.

    One candidate is one matched pair, so ``shares = allocated_capital /
    pair_cost`` floored to whole shares is the size of **each** leg, and every
    candidate emits one quote per leg at that one share count. A candidate
    without ``legs`` is a legacy single-leg plan and emits one quote at
    ``quote_price``. A pair is dropped as a whole when it carries no capital,
    when either leg prices outside (0, 1), or when the pair size floors below
    the market's ``min_size`` shares - never only one of its legs.
    ``books`` is the snapshot the candidates were built from and is accepted so
    callers pass one book set through the pipeline; sizing needs only the
    candidate's prices.

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
        pair_cost = float(
            candidate.get("pair_cost")
            or candidate.get("capital_per_share")
            or candidate["quote_price"]
        )
        if not 0.0 < pair_cost <= 1.0:
            continue
        tick = float(candidate["tick"])
        if tick <= 0.0:
            continue
        raw_shares = allocation.allocated_capital / pair_cost
        # The price tick quantises *prices*, not sizes; shares are whole units
        # because the venue's minimum order is published as a share count.
        shares = float(math.floor(raw_shares + 1e-9))
        min_size = float(candidate["min_size"])
        if min_size <= 0.0 or shares < min_size:
            continue
        legs = candidate.get("legs") or [
            {
                "token_id": candidate["quote_token"],
                "price": candidate["quote_price"],
            }
        ]
        planned: list[RewardQuote] = []
        valid = True
        for leg in legs:
            price = float(leg["price"])
            if not 0.0 < price < 1.0:
                valid = False
                break
            planned.append(
                RewardQuote(
                    condition_id=str(candidate["condition_id"]),
                    question=str(candidate["question"]),
                    token_id=str(leg["token_id"]),
                    price=price,
                    shares=shares,
                    # The optimizer's payout is for the pair; each leg carries
                    # the share of capital it commits so the total is unchanged.
                    daily_payout=float(allocation.daily_reward_payout)
                    * price
                    / pair_cost,
                    competing_score=float(candidate["competing_score"]),
                    max_spread_cents=float(candidate["max_spread"]),
                    min_size=min_size,
                    p_fill=leg.get("p_fill", candidate.get("p_fill")),
                    fill_yield_usd_per_usd_per_day=_as_float(
                        leg.get(
                            "fill_yield_usd_per_usd_per_day",
                            candidate.get("fill_yield_usd_per_usd_per_day"),
                        ),
                        0.0,
                    ),
                )
            )
        if valid:
            quotes.extend(planned)
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

    This function is the reserve ledger. A **matched pair** (both legs of one
    condition) is the unit of commitment: pairs are accepted in descending
    marginal yield (``daily_payout`` per dollar of pair notional) until the cash
    is spent, and a pair whose notional does not fit is **not** returned in
    part. The engine cancels any resting bid it is not asked to keep, so an
    unaffordable stale pair is retired whole instead of leaving one directional
    leg. A legacy single-leg plan is a one-quote group and behaves as before.

    A resting pair is accepted at the sizes it is already resting at, not at
    the freshly optimised size, because the engine keeps the existing orders
    when the price is unchanged; counting the new size while the old one rests
    is exactly the double-spend this function exists to prevent. If either leg
    moved, both legs are cancelled and re-placed at the fresh size. The residual
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
    # never happens. Legs are grouped by condition so a pair is never split.
    pairs: dict[str, list[RewardQuote]] = {}
    for quote in ideal:
        pairs.setdefault(quote.condition_id, []).append(quote)

    def _yield_per_dollar(legs: list[RewardQuote]) -> float:
        total = sum(
            quote.daily_payout * (1.0 - _as_float(quote.p_fill, 0.0))
            + quote.fill_yield_usd_per_usd_per_day * quote.price * quote.shares
            for quote in legs
        )
        notional = sum(quote.price * quote.shares for quote in legs)
        return total / max(notional, 1e-9)

    ordered = sorted(pairs.values(), key=_yield_per_dollar, reverse=True)
    committed: list[RewardQuote] = []
    remaining = cash
    kept_pairs = new_pairs = unaffordable = 0
    for legs in ordered:
        # A resting pair only counts as kept when every leg prices where this
        # tick's plan does; a moved leg is a cancel, and a cancelled order
        # releases its cash, so it must not be charged against this budget.
        kept: list[float] | None = []
        for quote in legs:
            entry = resting.get(quote.token_id)
            if entry is None:
                kept = None
                break
            price, shares = float(entry[0]), float(entry[1])
            if shares < quote.min_size or abs(price - quote.price) > 1e-9:
                kept = None
                break
            kept.append(shares)
        sizes = kept if kept is not None else [quote.shares for quote in legs]
        notional = sum(size * quote.price for size, quote in zip(sizes, legs))
        if notional > remaining:
            unaffordable += 1
            continue
        remaining -= notional
        if kept is not None:
            kept_pairs += 1
        else:
            new_pairs += 1
        for quote, size in zip(legs, sizes):
            committed.append(
                quote if size == quote.shares else dataclasses.replace(quote, shares=size)
            )
    # Logged every tick, not only on a shortfall: ``already resting`` versus
    # ``new`` is the plan's own stability, and the engine cancels every resting
    # bid this plan omits.
    log.info(
        f"reward reserve: kept {len(committed)}/{len(ideal)} quotes over "
        f"{len(ordered)} pair(s), "
        f"${cash - remaining:,.2f} committed of ${cash:,.2f} cash "
        f"({unaffordable} unaffordable; {kept_pairs} already resting, "
        f"{new_pairs} new)"
    )
    return committed

