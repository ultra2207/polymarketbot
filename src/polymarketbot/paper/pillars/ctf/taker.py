"""Taker complete-set parity: buy both ask ladders while the marginal edge is
positive.

The optimizer is the sign of the marginal edge itself: there is no fixed
minimum edge and no size cap beyond ladder depth, ``market.min_order`` and the
caller's cash.
"""

from __future__ import annotations

from typing import Optional

from polymarketbot.fees import FeeSchedule, taker_fee
from polymarketbot.paper.books import ladder
from polymarketbot.paper.venue import Market

from .parity import _FLOAT_EPS, _SHARE_EPS, _SOLVE_STEPS, ParityTranche


def _fee_schedule(market: Market) -> Optional[FeeSchedule]:
    """The market's taker schedule, or ``None`` when Gamma supplied none.

    A market without a schedule has an unknown taker fee.  Pricing a parity
    edge off an invented rate would be fabrication, so taker parity refuses it.
    """
    return market.fee_schedule


def _max_affordable_pair(
    price_yes: float,
    price_no: float,
    schedule: FeeSchedule,
    budget_usd: float,
    gas_usd: float,
) -> float:
    """Largest share count whose two legs plus gas fit ``budget_usd``.

    ``price_yes``/``price_no`` are ask prices (fractions of ``1.00``), the
    budget and gas are USDC, and the result is outcome shares.  The invariant is
    ``shares*(price_yes+price_no) + fee_yes + fee_no + gas <= budget_usd``.
    Fees are monotone in shares, so bisection between zero and the fee-free
    bound ``(budget-gas)/(price_yes+price_no)`` is exact up to the solver's
    tolerance and never returns a size the ledger would reject for cash.
    """
    net = float(budget_usd) - float(gas_usd)
    pair_price = float(price_yes) + float(price_no)
    if net <= 0.0 or pair_price <= 0.0:
        return 0.0

    def cost(shares: float) -> float:
        return (
            shares * pair_price
            + taker_fee(shares, price_yes, schedule)
            + taker_fee(shares, price_no, schedule)
        )

    hi = net / pair_price
    if cost(hi) <= net:
        return hi
    lo = 0.0
    for _ in range(_SOLVE_STEPS):
        mid = (lo + hi) * 0.5
        if cost(mid) <= net:
            lo = mid
        else:
            hi = mid
    return lo


def optimize_taker_parity(
    market: Market,
    book_yes: dict,
    book_no: dict,
    cash_usd: float,
    gas_usd: float = 0.0,
) -> list[ParityTranche]:
    """Buy complete sets from both ask ladders while the marginal edge is positive.

    Both ladders are sorted ascending and walked with one pointer each.  The
    current pair is the cheapest YES ask still available plus the cheapest NO
    ask still available; the size at that pair is the smaller remaining level
    size, clipped by what ``cash_usd`` can fund (fees and ``gas_usd`` included)
    and skipped when it is below ``market.min_order``.  A pair is taken while

        1 - (price_yes + price_no)
            - taker_fee(1, price_yes) - taker_fee(1, price_no)
            - gas_usd / shares_taken > 0,

    so the optimizer is the sign of the marginal edge itself: there is no fixed
    minimum edge and no size cap beyond ladder depth, ``market.min_order`` and
    cash.  ``cash_usd`` is the total USDC budget for both legs, matching the
    ledger's cash invariant; each tranche pays ``gas_usd`` once.

    Returns ``[]`` when the market is not binary, has no fee schedule, either
    ladder is empty, or cash cannot fund ``market.min_order`` at the best pair.
    Invariants: every tranche has ``shares >= market.min_order`` and
    ``edge_usd > 0``, and the sum of ``shares*price + fee + gas`` never exceeds
    ``cash_usd``.
    """
    schedule = _fee_schedule(market)
    if schedule is None or not market.binary:
        return []
    cash = float(cash_usd)
    gas = float(gas_usd)
    if cash <= 0.0:
        return []

    levels_yes = ladder(book_yes, asks=True)
    levels_no = ladder(book_no, asks=True)
    if not levels_yes or not levels_no:
        return []

    min_order = float(market.min_order)
    i = j = 0
    rem_yes = levels_yes[0][1]
    rem_no = levels_no[0][1]
    tranches: list[ParityTranche] = []

    while i < len(levels_yes) and j < len(levels_no):
        price_yes = levels_yes[i][0]
        price_no = levels_no[j][0]
        overlap = min(rem_yes, rem_no)

        affordable = _max_affordable_pair(price_yes, price_no, schedule, cash, gas)
        if affordable <= _SHARE_EPS or affordable < min_order:
            # Deeper pairs only cost more per set, so no later pair is fundable.
            break

        take = min(overlap, affordable)
        if take < min_order:
            # A thin level cannot meet the venue minimum; consume it and move on.
            if rem_yes <= rem_no:
                i += 1
                if i < len(levels_yes):
                    rem_yes = levels_yes[i][1]
            else:
                j += 1
                if j < len(levels_no):
                    rem_no = levels_no[j][1]
            continue

        fee_share = taker_fee(1.0, price_yes, schedule) + taker_fee(1.0, price_no, schedule)
        gas_share = gas / take if take > 0.0 else 0.0
        marginal = 1.0 - (price_yes + price_no) - fee_share - gas_share
        if marginal <= _FLOAT_EPS:
            break

        fee_yes = taker_fee(take, price_yes, schedule)
        fee_no = taker_fee(take, price_no, schedule)
        cost = take * (price_yes + price_no) + fee_yes + fee_no + gas
        if cost > cash + 1e-9:
            # Rounding in the fee solver can push the size a hair over the
            # budget; the affordable size is authoritative.
            break

        edge_usd = take * (1.0 - price_yes - price_no) - fee_yes - fee_no - gas
        if edge_usd <= _FLOAT_EPS:
            # Fee rounding can shave a marginal pair below zero; never emit it.
            break
        tranches.append(
            ParityTranche(
                condition_id=market.condition_id,
                shares=take,
                price_yes=price_yes,
                price_no=price_no,
                fee_yes=fee_yes,
                fee_no=fee_no,
                gas=gas,
                edge_usd=edge_usd,
            )
        )
        cash -= cost

        rem_yes -= take
        rem_no -= take
        if rem_yes <= _SHARE_EPS:
            i += 1
            if i < len(levels_yes):
                rem_yes = levels_yes[i][1]
        if rem_no <= _SHARE_EPS:
            j += 1
            if j < len(levels_no):
                rem_no = levels_no[j][1]

    return tranches
