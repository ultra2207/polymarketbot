"""CTF complete-set parity: buy both legs under par, or quote both bids over par.

A binary market's two outcome tokens form a complete set: one YES share plus one
NO share redeems for exactly ``1.00`` USDC.  The taker optimizer therefore walks
both ask ladders together and buys the largest size whose marginal edge

    1 - (price_yes + price_no) - fee_yes - fee_no - gas/share

is still positive.

The maker optimizer is **not** an arbitrage: resting one bid on each leg is two
independent market-making bets.  The ``1 - bid_yes - bid_no`` sum is a *quote*,
not a fill, and it is below par for essentially every two-sealed market (a
recorded day of L2 tape: 20,217 of 20,221 condition snapshots).  It is realized
only when both legs fill; a single-leg fill leaves naked directional inventory
that the ledger marks at the best bid, i.e. at ``quote - tick``.  The optimizer
therefore prices the pair as the expected value of that three-outcome bet:

    E[P&L per set] = p_y*p_n*(1 - bid_yes - bid_no + rebate_y + rebate_n)
                    + p_y*(1-p_n)*(rebate_y - unwind_cost_y)
                    + p_n*(1-p_y)*(rebate_n - unwind_cost_n)

and emits only when that expectation is strictly positive.  ``p_y``/``p_n`` are
the **horizon-matched** measured fill probabilities from
``data/derived/ctf_quote_fill_horizon.json``: a quote is sampled on the engine's
own tick grid over the recorded L2 tape and counts as filled when a SELL print
crosses it within the requote lifetime.  That is a joint probability (arrival
*and* crossing); the print-conditional ``fill_model.json`` is conditional on a
print having happened and therefore cannot be used here — over a one-tick
lifetime it overstates a per-leg fill by more than an order of magnitude.
``unwind_cost`` is the measured distance from the quote back to the executable
best bid (the same mark the ledger uses); ``rebate`` is the market's measured
maker rebate.  With no horizon measurement loadable the expected value is
unknown and no quote is emitted.

Apart from the cached measurements, every function is a pure function of its
arguments: the books, the market's own fields (``tick``, ``min_order``,
``fee_schedule``) and the caller's cash/gas.  Nothing here invents a rate,
threshold or price.

Units: prices are fractions of ``1.00`` USDC per share, shares are outcome
shares, cash, fees, gas and edges are USDC.  A complete set is one share of
every distinct token of the condition, so its redemption value is ``1.00``.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol, Tuple

from polymarketbot.fees import FeeSchedule, maker_fee_rebate, taker_fee
from polymarketbot.logs import get_logger
from polymarketbot.paper.books import best_ask, best_bid, ladder, shares_at
from polymarketbot.paper.venue import Market

logger = get_logger("paper.ctf")

# ---------------------------------------------------------------------------
# Measured horizon-matched fill probability
# (data/derived/ctf_quote_fill_horizon.json, built by
# ``polymarketbot.tools.ctf_quote_fill_horizon`` from the recorded L2 tape).
#
# The print-conditional model in ``fill_model.json`` answers "given that a print
# hit this level, did it clear the queue ahead of us".  It carries no arrival
# rate, so applying it to a resting quote is a conditional probability used as
# a joint one: the soak quoted 1,262 legs a tick for 30 minutes and filled
# none.  The gate needs P(a quote fills within the lifetime it actually rests),
# which is what this table measures.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_QUOTE_FILL_HORIZON_PATH = (
    _REPO_ROOT / "data" / "derived" / "ctf_quote_fill_horizon.json"
)
# Measured adverse markout of one filled leg, built by
# ``polymarketbot.tools.ctf_quote_fill_horizon --markout`` from the same L2 tape
# as the fill table above.
DEFAULT_MAKER_MARKOUT_PATH = (
    _REPO_ROOT / "data" / "derived" / "ctf_maker_markout.json"
)

# There is no default requote lifetime.  A quote is *held* across ticks until
# its market is priced and refused again, so its lifetime is the caller's
# measured scan gap (``sweep_ticks * last_tick_dt``: measured 415-612 s in the
# 10:10 window, 428-459 s in the 12:53 window).  Substituting an assumed tick
# for a missing measurement would price a lifetime the engine never rests.


@dataclass(frozen=True)
class QuoteFillHorizon:
    """Measured P(a resting buy at ``best_bid + tick`` fills) by horizon.

    ``cells`` holds ``(horizon_s, tick, min_flow_shares, p_fill)`` measured on
    the recorded L2 tape: a placement every engine tick, a fill when a SELL
    print crosses the quote within the horizon with enough size to meet the
    venue minimum order.

    A horizon between two measured cells is linearly interpolated in time. That
    is a measured approximation, not a guess: withholding each measured cell and
    predicting it from its neighbours gives errors of 3.39 %, 2.92 %, 5.31 %,
    0.30 % and 0.33 % (worst 5.31 %, on 8.77 M tape placements), while log
    interpolation is 5-30 % wrong because arrival is not multiplicative over
    these horizons. ``P_FILL_INTERPOLATION_ERROR`` carries that bound.

    Nothing is extrapolated. A horizon below the smallest measured cell, or
    outside the measured tick/flow grid, returns ``None`` (missing data), so the
    maker gate refuses instead of assuming a probability. ``p_fill`` also
    returns ``None`` above the largest measured cell; ``clamped_p_fill`` pins
    that case to the measured maximum and reports the horizon it priced, which
    is a lower bound on the true probability and is what a requote gap wider
    than the table's support gets.
    """

    cells: Tuple[Tuple[float, float, float, float], ...]
    placements: int = 0

    #: Worst held-out error of the linear-in-horizon interpolation, now measured
    #: on the exact-price series the gate prices: 25.71 % over 48 withholds of
    #: the shipped table.  The cells the gate uses most (min order 5 shares,
    #: tick 0.01) stay inside 7.9 %; the worst triple is a 200-share cell at
    #: tick 0.001 whose counts are tiny.  The earlier 5.31 % was measured on the
    #: looser at-or-below series and understated the error for these cells.
    P_FILL_INTERPOLATION_ERROR = 0.2571

    def _cell_at(self, horizon: float, tick_value: float, flow: float) -> Optional[float]:
        rows = [
            c for c in self.cells
            if c[0] == horizon and abs(c[1] - tick_value) < 1e-12 and c[2] >= flow
        ]
        if not rows:
            return None
        return min(rows, key=lambda c: c[2])[3]

    def p_fill(
        self,
        horizon_s: float,
        tick: float,
        min_flow_shares: float,
    ) -> Optional[float]:
        """Measured P(fill) for this horizon, tick and required fill size.

        The horizon and tick must be measured exactly (both are discrete
        operational quantities, not continuous parameters).  The required flow
        is rounded up to the smallest measured bucket that covers it, so a
        minimum order between two measured cells is priced at the harder cell.
        """
        horizon = float(horizon_s)
        tick_value = float(tick)
        flow = max(0.0, float(min_flow_shares))
        exact = self._cell_at(horizon, tick_value, flow)
        if exact is not None:
            return exact
        if flow <= 0.0:
            return None
        measured = sorted({
            c[0] for c in self.cells
            if abs(c[1] - tick_value) < 1e-12 and c[2] >= flow
        })
        if len(measured) < 2 or horizon < measured[0] or horizon > measured[-1]:
            # Outside the measured support: the required flow is above every
            # cell, or the horizon is below the smallest / above the largest
            # measured lifetime. Extrapolating would invent a probability.
            return None
        lower = max(h for h in measured if h <= horizon)
        upper = min(h for h in measured if h >= horizon)
        low_value = self._cell_at(lower, tick_value, flow)
        high_value = self._cell_at(upper, tick_value, flow)
        if low_value is None or high_value is None or upper <= lower:
            return None
        weight = (horizon - lower) / (upper - lower)
        return low_value + (high_value - low_value) * weight

    def supported_horizons(self, tick: float, min_flow_shares: float) -> list[float]:
        """Measured horizons that cover this tick and this required fill size."""
        flow = max(0.0, float(min_flow_shares))
        return sorted({
            c[0] for c in self.cells
            if abs(c[1] - float(tick)) < 1e-12 and c[2] >= flow
        })

    def horizon_bound(self, tick: float, min_flow_shares: float) -> Optional[float]:
        """Longest measurable requote lifetime, or ``None`` without support."""
        measured = self.supported_horizons(tick, min_flow_shares)
        return measured[-1] if measured else None

    def clamped_p_fill(
        self, horizon_s: float, tick: float, min_flow_shares: float
    ) -> Optional[Tuple[float, float]]:
        """``(p_fill, priced_horizon_s)``, pinning an over-long life to support.

        A requote lifetime longer than the longest measured horizon has no
        probability, but refusing to quote is not the only honest answer: the
        probability at the longest measured horizon is a **lower bound** for it
        (arrival is non-negative over the extra time).  So a caller whose sweep
        gap exceeds the table is priced at the measured maximum and told which
        horizon that was, rather than silently dropped.

        The clamp is one-directional.  A horizon *below* the smallest measured
        cell is still refused: pricing it at the shortest cell would overstate
        the probability, and overstating a fill is how a fabricated edge gets
        into the book.
        """
        exact = self.p_fill(horizon_s, tick, min_flow_shares)
        if exact is not None:
            return exact, float(horizon_s)
        bound = self.horizon_bound(tick, min_flow_shares)
        if bound is None or float(horizon_s) <= bound:
            return None
        value = self._cell_at(bound, float(tick), max(0.0, float(min_flow_shares)))
        if value is None:
            return None
        return value, bound


_QUOTE_FILL_HORIZON: Optional[QuoteFillHorizon] = None
_QUOTE_FILL_HORIZON_TRIED = False


def _default_quote_fill_horizon() -> Optional[QuoteFillHorizon]:
    """The measured horizon fill table, or ``None`` when it is unusable."""
    global _QUOTE_FILL_HORIZON, _QUOTE_FILL_HORIZON_TRIED
    if not _QUOTE_FILL_HORIZON_TRIED:
        _QUOTE_FILL_HORIZON_TRIED = True
        try:
            _QUOTE_FILL_HORIZON = load_quote_fill_horizon()
        except Exception as exc:  # noqa: BLE001 - a missing measurement is data, not a crash
            logger.warning(
                f"CTF maker parity disabled: horizon fill measurement unavailable ({exc}). "
                "Resting both legs without a horizon-matched fill probability would "
                "claim an edge that only materialises when both legs fill."
            )
            _QUOTE_FILL_HORIZON = None
    return _QUOTE_FILL_HORIZON


def load_quote_fill_horizon(path: Optional[Path] = None) -> QuoteFillHorizon:
    """Load and validate the measured horizon fill table; raise if unusable."""
    model_path = Path(path) if path is not None else DEFAULT_QUOTE_FILL_HORIZON_PATH
    with open(model_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    cells = []
    for row in payload.get("cells") or []:
        # Price the event the paper execution model realises. It books a fill
        # only when a print lands at the quote's own price
        # (`reward.measured_fill_shares`), which the tape measures as 10.75 % of
        # the at-or-below count: a gate charged the crossing probability would
        # be pricing nine fills for every one it can get.
        p = row.get("p_fill_exact")
        if p is None:
            continue
        cells.append(
            (
                float(row["horizon_s"]),
                float(row["tick"]),
                float(row["min_flow_shares"]),
                float(p),
            )
        )
    if not cells:
        raise ValueError(f"horizon fill table at {model_path} has no measured cells")
    placements = int((payload.get("coverage") or {}).get("placements") or 0)
    logger.info(
        f"Loaded horizon fill measurement {model_path} "
        f"({len(cells)} cells, {placements:,} tape placements)"
    )
    return QuoteFillHorizon(cells=tuple(cells), placements=placements)


@dataclass(frozen=True)
class MakerMarkout:
    """Measured adverse markout of a single filled leg, by ``(horizon, tick)``.

    ``cells`` holds ``(horizon_s, tick, usd_per_share, fills)`` measured on the
    recorded L2 tape by ``polymarketbot.tools.ctf_quote_fill_horizon --markout``:
    for every placement that filled, the best bid at the fill minus the best bid
    one horizon later.  A positive value is the per-share loss the drift
    inflicts on a leg that fills alone -- the outcome the maker gate prices with
    weight ``p * (1 - p)``, i.e. ~3000x the ``p^2`` weight of the lock at the
    measured exact fill probabilities.

    Nothing is interpolated: the drift is not a linear function of time, so a
    horizon without a measured cell has no markout and the gate refuses.  The
    measurement is conditioned on fills that reach the venue minimum order,
    which is the only size every emitted quote must clear.
    """

    cells: Tuple[Tuple[float, float, float, int], ...] = ()

    def markout(self, horizon_s: float, tick: float) -> Optional[float]:
        """Measured adverse drift per share, or ``None`` when unmeasured.

        A horizon between two measured cells is charged the **larger** of the
        two measured drifts.  That is a measured number, not an interpolation:
        the drift is not monotone in time (measured 0.08c at 5 s, 0.25c at 30 s,
        0.12c at 300 s for tick 0.001), so a straight line through it would
        invent values, while taking the worse neighbour can only overstate the
        cost the gate must clear.  Below the smallest measured cell there is no
        cell to lean on and the answer is ``None`` (missing data, refuse); above
        the largest, the caller has already pinned the horizon to support
        (``QuoteFillHorizon.clamped_p_fill``), so a lookup there is out of
        contract rather than a value.
        """
        horizon = float(horizon_s)
        tick_value = float(tick)
        measured = sorted(
            (cell_horizon, value)
            for cell_horizon, cell_tick, value, fills in self.cells
            if abs(cell_tick - tick_value) < 1e-12 and fills > 0
        )
        if not measured:
            return None
        for cell_horizon, value in measured:
            if abs(cell_horizon - horizon) < 1e-9:
                return value
        if horizon < measured[0][0] or horizon > measured[-1][0]:
            return None
        lower = max(h for h, _ in measured if h <= horizon)
        upper = min(h for h, _ in measured if h >= horizon)
        return max(
            value for cell_horizon, value in measured if cell_horizon in (lower, upper)
        )

    def supported_horizons(self, tick: float) -> list[float]:
        """Measured horizons that carry a markout for this tick."""
        return sorted(
            cell_horizon
            for cell_horizon, cell_tick, _value, fills in self.cells
            if abs(cell_tick - float(tick)) < 1e-12 and fills > 0
        )


_MAKER_MARKOUT: Optional[MakerMarkout] = None
_MAKER_MARKOUT_TRIED = False


def load_maker_markout(path: Optional[Path] = None) -> MakerMarkout:
    """Load the measured single-leg markout; raise when it carries no cells."""
    model_path = Path(path) if path is not None else DEFAULT_MAKER_MARKOUT_PATH
    with open(model_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    cells = []
    for row in payload.get("cells") or []:
        value = row.get("mean_markout_usd_per_share")
        fills = row.get("fills")
        if value is None or fills is None:
            continue
        if int(fills) <= 0:
            continue
        cells.append(
            (
                float(row["horizon_s"]),
                float(row["tick"]),
                float(value),
                int(fills),
            )
        )
    if not cells:
        raise ValueError(f"markout table at {model_path} has no measured cells")
    logger.info(
        f"Loaded single-leg markout measurement {model_path} "
        f"({len(cells)} cells, {sum(c[3] for c in cells):,} measured fills)"
    )
    return MakerMarkout(cells=tuple(cells))


def _default_maker_markout() -> Optional[MakerMarkout]:
    """The measured single-leg markout, or ``None`` when it is unusable."""
    global _MAKER_MARKOUT, _MAKER_MARKOUT_TRIED
    if not _MAKER_MARKOUT_TRIED:
        _MAKER_MARKOUT_TRIED = True
        try:
            _MAKER_MARKOUT = load_maker_markout()
        except Exception as exc:  # noqa: BLE001 - a missing measurement is data, not a crash
            logger.warning(
                f"CTF maker parity disabled: single-leg markout measurement "
                f"unavailable ({exc}). Without it the one-leg branch is priced "
                "against a one-tick unwind alone, which is the error the 12:53 "
                "window ran for 30 minutes at 0 fills."
            )
            _MAKER_MARKOUT = None
    return _MAKER_MARKOUT


# Floating-point comparison guard, not an economic threshold.  Fees round to
# 5 dp and prices are decimals, so an exact zero is a comparison artefact; every
# *economic* hurdle lives in a measured quantity (markout, unwind cost, rebate).
_FLOAT_EPS = 1e-12
# Share-count tolerance for ladder consumption.
_SHARE_EPS = 1e-9
# Bisection steps for the cash-bounded size.  Fees round to 5 dp, so 64 halvings
# resolve the affordable size far below the smallest tradable lot.
_SOLVE_STEPS = 64


@dataclass(frozen=True)
class ParityTranche:
    """One buy of ``shares`` complete sets at a single pair of ask prices.

    ``price_yes``/``price_no`` are the resting ask prices consumed (fractions of
    ``1.00``), ``fee_yes``/``fee_no`` are the exact taker fees for this tranche
    (USDC), ``gas`` is the fixed merge cost charged to this tranche (USDC), and
    ``edge_usd`` is

        shares * (1 - price_yes - price_no) - fee_yes - fee_no - gas

    i.e. the locked profit in USDC if the two legs are merged at par.  The
    invariant is ``edge_usd > 0``: a tranche is never emitted at a loss.
    """

    condition_id: str
    shares: float
    price_yes: float
    price_no: float
    fee_yes: float
    fee_no: float
    gas: float
    edge_usd: float


@dataclass(frozen=True)
class MakerParityQuote:
    """A two-sided resting bid pair priced as a three-outcome bet.

    ``bid_yes``/``bid_no`` are the prices to rest (fractions of ``1.00``),
    ``shares`` is the size quoted on each leg (outcome shares) and ``edge_usd``
    is the **expected** P&L per complete set in USDC::

        p_y*p_n*(1 - bid_yes - bid_no + rebate_y + rebate_n)
      + p_y*(1-p_n)*(rebate_y - unwind_cost_y - markout)
      + p_n*(1-p_y)*(rebate_n - unwind_cost_no - markout)

    ``locked_spread`` is the unweighted ``1 - bid_yes - bid_no`` (the number the
    old gate mistook for a locked profit), ``fill_prob_yes``/``fill_prob_no``
    are the measured per-leg fill probabilities, ``edge_if_both_fill`` is the
    per-set profit conditional on both legs filling, and ``markout_usd`` /
    ``unwind_cost_yes`` / ``unwind_cost_no`` are the measured per-share costs a
    leg that fills alone pays.  The invariants are ``edge_usd > 0`` and both
    single-leg branches strictly positive after those costs: a pair whose
    expectation is not positive, or that can only be positive by relying on a
    losing naked leg, is never emitted.

    ``priced_horizon_s`` is the horizon the probabilities were measured over,
    and ``horizon_clamped_from_s`` is the requote gap that was asked for when it
    was wider than the table's support (``None`` otherwise).  A caller that can
    bound a quote's life should cancel it at ``priced_horizon_s`` after landing:
    the EV above is priced over exactly that window, so a quote that outlives it
    is earning a probability nobody measured.
    """

    condition_id: str
    bid_yes: float
    bid_no: float
    shares: float
    edge_usd: float
    locked_spread: float = 0.0
    fill_prob_yes: float = 0.0
    fill_prob_no: float = 0.0
    edge_if_both_fill: float = 0.0
    priced_horizon_s: float = 0.0
    horizon_clamped_from_s: Optional[float] = None
    markout_usd: float = 0.0
    unwind_cost_yes: float = 0.0
    unwind_cost_no: float = 0.0
    rebate_yes: float = 0.0
    rebate_no: float = 0.0


class _LotLike(Protocol):
    """The ledger lot fields ``merge_pairs`` reads; no other structure assumed."""

    condition_id: str
    token_id: str
    shares: float


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


def _maker_bid(book: dict, tick: float) -> Optional[float]:
    """The price that makes a resting buy the best bid, or ``None``.

    ``tick`` is the market's price increment (fraction of ``1.00``).  With a
    resting bid the quote is one tick above it.  With no bid the quote is one
    tick below the best ask, which is the highest price that still rests rather
    than crosses.  The invariant: the returned price is strictly inside
    ``(0, 1)``, on the tick grid, and below the best ask when one exists, so it
    can only fill as a maker order.
    """
    ask = best_ask(book)
    bid = best_bid(book)
    if bid is not None:
        quote = bid + tick
    elif ask is not None:
        quote = ask - tick
    else:
        return None
    if quote <= 0.0 or quote >= 1.0:
        return None
    if ask is not None and quote >= ask:
        # No room to improve without crossing; a taker fill would break the
        # maker premise (and its fee).
        return None
    return quote


def _quote_fill_probability(
    model: QuoteFillHorizon,
    book: dict,
    price: float,
    horizon_s: float,
    tick: float,
    min_order: float,
) -> Optional[Tuple[float, float]]:
    """Measured (P(a resting bid at ``price`` fills), the horizon it priced).

    The probability is the tape-measured joint quantity - a print arriving *and*
    crossing the quote - for this requote lifetime, tick size and required fill
    size, so it needs no queue-ratio correction: the measurement already
    sampled quotes placed one tick above the book.  ``shares_at`` is read only
    to reject a quote that would sit behind displayed size at the same price
    (the tape measurement is for a quote that is alone at its level).
    Returns ``None`` when the measurement has no cell for these arguments.

    A requote gap longer than the table's support is priced at the longest
    measured horizon (``QuoteFillHorizon.clamped_p_fill``) and the horizon that
    was actually priced is returned alongside the probability, so the caller
    can log the gap and, if it can, end the quote's life there.
    """
    if shares_at(book, bid=True, price=price) > 0.0:
        # Displayed size at our exact quote price means we are not alone at the
        # level, and the measured cells are for quotes that were.
        return None
    priced = model.clamped_p_fill(horizon_s, tick, min_order)
    if priced is None:
        _warn_unmeasured_horizon(model, horizon_s, tick, min_order)
    return priced


_UNMEASURED_HORIZON_LOGGED: set[tuple[float, float, float]] = set()
_HORIZON_CLAMP_LOGGED: set[tuple[float, float, float]] = set()


def _warn_horizon_clamped(
    model: QuoteFillHorizon,
    requested_s: float,
    priced_s: float,
    tick: float,
    min_order: float,
) -> None:
    """Log once per (gap, priced, tick, order) that a long gap was pinned.

    The sweep gap is derived from the tick cadence, so a tick that overruns its
    interval silently widens every quote's requote lifetime past the measured
    table.  Pinning the price to the measured maximum keeps the quote alive
    instead of turning the pillar off, but it must never be silent: the priced
    probability is a lower bound over the *requested* lifetime, and the bound is
    the model's own held-out interpolation error.
    """
    key = (float(requested_s), float(priced_s), float(tick), float(min_order))
    if key in _HORIZON_CLAMP_LOGGED:
        return
    _HORIZON_CLAMP_LOGGED.add(key)
    logger.warning(
        f"CTF maker quote priced at the measured maximum {float(priced_s):.0f}s "
        f"horizon, not the {float(requested_s):.0f}s requote gap: the table covers "
        f"{model.supported_horizons(tick, min_order)} s and extrapolating past it "
        "would invent a fill probability. The priced p_fill is a lower bound for the "
        "quote's true lifetime (arrival over the extra "
        f"{float(requested_s) - float(priced_s):.0f}s is unmeasured); the model's "
        f"worst held-out interpolation error measured on the at-or-below series is "
        f"{QuoteFillHorizon.P_FILL_INTERPOLATION_ERROR:.2%} (the exact-price series' "
        "own held-out error is not yet measured)."
    )


def _warn_unmeasured_horizon(
    model: QuoteFillHorizon, horizon_s: float, tick: float, min_order: float
) -> None:
    """Log once per (horizon, tick, order) that the table cannot price.

    The engine derives ``requote_horizon_s`` from the scan cadence, so a scan
    gap longer than the longest measured horizon makes the pillar refuse every
    quote.  That refusal is missing data, not a zero probability, and it must be
    visible: otherwise the sweep cadence silently turns the pillar off.
    """
    key = (float(horizon_s), float(tick), float(min_order))
    if key in _UNMEASURED_HORIZON_LOGGED:
        return
    _UNMEASURED_HORIZON_LOGGED.add(key)
    measured = sorted({cell[0] for cell in model.cells})
    logger.warning(
        f"CTF maker parity refused at a {float(horizon_s):.1f}s requote horizon with "
        f"tick {float(tick):g} and min order {float(min_order):g} shares: the measured "
        f"table covers horizons {measured} s. A scan gap wider than the measured "
        "horizon support is missing data, not a fill probability of zero."
    )


def _unwind_cost(book: dict, quote: float) -> float:
    """Per-share loss from exiting a filled leg at the executable best bid.

    The ledger marks an open lot at the best bid, so a leg that fills alone is
    already worth ``best_bid - quote`` per share.  With no bid on the book the
    leg has no executable exit and the whole quote is at risk.
    """
    bid = best_bid(book)
    if bid is None:
        return float(quote)
    return max(0.0, float(quote) - float(bid))


_UNMEASURED_MARKOUT_LOGGED: set[tuple[float, float]] = set()


def _warn_unmeasured_markout(
    model: Optional[MakerMarkout], horizon_s: float, tick: float
) -> None:
    """Log once per (horizon, tick) that no markout was measured for this quote."""
    key = (float(horizon_s), float(tick))
    if key in _UNMEASURED_MARKOUT_LOGGED:
        return
    _UNMEASURED_MARKOUT_LOGGED.add(key)
    measured = sorted(model.supported_horizons(tick)) if model is not None else []
    logger.warning(
        f"CTF maker parity refused a {float(horizon_s):.0f}s quote at tick "
        f"{float(tick):g}: no single-leg markout is measured there (the markout "
        f"table covers horizons {measured} s at this tick). A re-quote that "
        "outlives its measured lifetime lands unwound, and pricing that as free "
        "is how a rebate-funded quote books a loss. Build the table with "
        "`python -m polymarketbot.tools.ctf_quote_fill_horizon --markout`."
    )


# ---------------------------------------------------------------------------
# Quote audit
#
# Every acceptance logs its six EV components, so the claim "this quote is
# positive because p_yes, p_no, locked, rebate, cost, markout say so" is
# checkable from the log.  Every refusal is counted by reason and the best
# refused candidate -- the one with the highest expected value -- keeps its
# components, drained once a cadence.  Without that, a window with zero quotes
# is indistinguishable from a gate that never ran, which is what the 12:53
# window looked like: 68-150 resting orders, no fills, and no line saying what
# any of them was priced at.
# ---------------------------------------------------------------------------
#: The engine already prints its other periodic lines on this cadence
#: (``settlement candidates``, ``reward reserve``); matching it keeps one
#: timeline in the log.  It is a log cadence, not an economic quantity.
QUOTE_AUDIT_INTERVAL_S = 60.0

_QUOTE_AUDIT: dict = {
    "logged_at": 0.0,
    "evaluated": 0,
    "accepted": 0,
    "refused": {},
    "best_refusal": None,
}

#: Distinct quotes whose components have already been published.  Bounded so a
#: 30-minute run cannot grow it without limit; clearing it re-publishes, which
#: is a log duplicate, never a missing component.
_ACCEPTED_QUOTES_LOGGED_MAX = 10_000
_ACCEPTED_QUOTES_LOGGED: set[tuple] = set()


def _format_components(components: dict) -> str:
    def show(key: str, fmt: str = "{:.5f}") -> str:
        value = components.get(key)
        return "unmeasured" if value is None else fmt.format(value)

    return (
        f"p_yes={show('p_yes', '{:.3e}')} p_no={show('p_no', '{:.3e}')} "
        f"locked={show('locked', '{:.4f}')} "
        f"rebate={show('rebate_yes')}/{show('rebate_no')} "
        f"unwind={show('cost_yes')}/{show('cost_no')} "
        f"markout={show('markout')} "
        f"surplus={show('surplus_yes', '{:+.6f}')}/{show('surplus_no', '{:+.6f}')} "
        f"expected={show('expected', '{:+.3e}')} "
        f"shares={components.get('shares', 0.0):.1f} "
        f"horizon={components.get('horizon_s', 0.0):.0f}s"
    )


def _audit_refusal(reason: str, components: Optional[dict] = None) -> None:
    """Count a refusal and keep the highest-expectation candidate's components."""
    _QUOTE_AUDIT["refused"][reason] = _QUOTE_AUDIT["refused"].get(reason, 0) + 1
    if components is None or components.get("expected") is None:
        return
    best = _QUOTE_AUDIT["best_refusal"]
    if best is None or components["expected"] > best["expected"]:
        _QUOTE_AUDIT["best_refusal"] = {"reason": reason, **components}


def log_quote_audit(now: Optional[float] = None, force: bool = False) -> Optional[str]:
    """Log the drained quote audit: counts by reason plus the best refusal."""
    stamp = time.monotonic() if now is None else float(now)
    last = _QUOTE_AUDIT["logged_at"]
    elapsed = (stamp - last) if last else 0.0
    if not force and last and elapsed < QUOTE_AUDIT_INTERVAL_S:
        return None
    if _QUOTE_AUDIT["evaluated"] == 0:
        return None
    counts = ", ".join(
        f"{reason}={count}"
        for reason, count in sorted(_QUOTE_AUDIT["refused"].items(), key=lambda kv: -kv[1])
    )
    best = _QUOTE_AUDIT["best_refusal"]
    line = (
        f"CTF maker gate: {_QUOTE_AUDIT['accepted']} accepted / "
        f"{_QUOTE_AUDIT['evaluated']} evaluated over {elapsed:.0f}s"
        + (f" | refused {counts}" if counts else "")
        + (f" | best refusal {best['reason']} {_format_components(best)}" if best else "")
    )
    logger.info(line)
    _QUOTE_AUDIT["logged_at"] = stamp
    _QUOTE_AUDIT["evaluated"] = 0
    _QUOTE_AUDIT["accepted"] = 0
    _QUOTE_AUDIT["refused"] = {}
    _QUOTE_AUDIT["best_refusal"] = None
    return line


def optimize_maker_parity(
    market: Market,
    book_yes: dict,
    book_no: dict,
    size_shares: float,
    *,
    requote_horizon_s: Optional[float] = None,
    fill_probability: Optional[float] = None,
    horizon_model: Optional[QuoteFillHorizon] = None,
    markout_model: Optional[MakerMarkout] = None,
    unwind_cost_yes: Optional[float] = None,
    unwind_cost_no: Optional[float] = None,
    min_daily_yield: Optional[float] = None,
) -> Optional[MakerParityQuote]:
    """Quote both legs one tick better than the book, priced as a two-leg bet.

    Each bid is placed one tick above the current best bid (one tick below the
    best ask when that side has no bid).  Resting both bids is not a locked
    arbitrage: ``1 - bid_yes - bid_no`` is realized only when **both** legs
    fill, and a single-leg fill leaves naked inventory marked at the best bid.

    The quote is emitted only when the expected value of the three-outcome bet
    is positive *and* neither single-leg branch is a loser::

        edge_both   = 1 - bid_yes - bid_no + rebate_yes + rebate_no
        surplus_yes = rebate_yes - unwind_cost_yes - markout
        surplus_no  = rebate_no  - unwind_cost_no  - markout
        expected    = p_y*p_n*edge_both
                    + p_y*(1-p_n)*surplus_yes
                    + p_n*(1-p_y)*surplus_no

    Every number in that expression is measured, and each refusal names the one
    that is missing:

    * ``p_y``/``p_n`` are the **horizon-matched** measured fill probabilities,
      P(a quote at this price fills within ``requote_horizon_s``), read from the
      tape-measured ``data/derived/ctf_quote_fill_horizon.json`` (or an explicit
      ``fill_probability`` when the caller has its own measurement).  The
      print-conditional model in ``fill_model.json`` is *not* used here: it omits
      the print arrival rate and overstates the joint probability by ~2,000x.
    * ``rebate`` is the market's measured maker credit per share
      (``maker_fee_rebate`` from the market's own fee schedule; zero when the
      schedule pays no rebate).
    * ``unwind_cost`` is the per-share loss of exiting the leg at the executable
      best bid, i.e. ``quote - best_bid`` -- the mark the ledger itself uses.
    * ``markout`` is the **measured adverse drift** of that mark over the priced
      horizon (``data/derived/ctf_maker_markout.json``): the best bid at the
      fill minus the best bid one horizon later, averaged over every tape
      placement that filled.  It is conditioned on the venue minimum order, the
      only size a quote must clear.

    Why both single-leg branches must be positive: at the measured exact fill
    probabilities (~3e-4 per leg at a 400 s life) the lock carries weight
    ``p^2`` ~ 1e-7 while a single-leg outcome carries ``p``, i.e. ~3,000x more.
    The bet is economically two naked legs plus a negligible lottery on the
    lock, so a quote whose naked leg loses money is a losing quote no matter
    what the two-leg branch pays.  This is the hurdle the old gate lacked: it
    accepted anything above a 1e-12 float tolerance, and it had no markout term,
    so a 0.25c rebate against a one-tick (0.1c) unwind passed at tick 0.001 --
    the path that left 68-150 orders resting on 99.7 % of the pillar's cash for
    30 minutes with zero fills in the 12:53 window.

    The two-leg branch excludes the merge gas: it is paid only when both legs
    fill and the engine prices it per merge (measured $0.000191 in the 12:53
    window), 4e-5 USDC per share at the 5-share venue minimum, i.e. below the
    reporting precision of every term above and three orders below the naked-leg
    hurdle that binds the decision.

    ``requote_horizon_s`` is required: a quote is held across ticks, so its
    lifetime is the caller's measured scan gap (``sweep_ticks *
    last_tick_dt``).  There is no default -- an assumed lifetime would price a
    probability nobody measured.

    ``size_shares`` caps the quoted size on each leg; a cap below
    ``market.min_order`` is unfillable and returns ``None``.  Units: bids are
    fractions of ``1.00``, shares are outcome shares, edges are USDC per
    complete set.  Invariants: ``shares >= market.min_order``, ``edge_usd > 0``,
    ``edge_usd <= edge_if_both_fill`` and ``surplus_yes, surplus_no > 0`` for
    every returned quote.  Returns ``None`` when a measurement is missing: an
    unknown fill probability or markout is missing data, never a probability of
    one and never a free exit.

    A requote gap wider than the fill table's longest measured horizon is priced
    at that measured horizon instead of being refused, and the gap is logged
    (``_warn_horizon_clamped``) with the bound it costs; the returned quote
    reports ``priced_horizon_s`` and ``horizon_clamped_from_s`` so the caller can
    end the quote's life at the horizon it actually paid for.  The clamp is
    one-directional: a gap shorter than the smallest measured cell is refused,
    because pricing it at the shortest cell would overstate the probability.
    """
    _QUOTE_AUDIT["evaluated"] += 1
    if not market.binary:
        _audit_refusal("not_binary")
        return None
    shares = float(size_shares)
    min_order = float(market.min_order)
    if shares <= 0.0 or shares < min_order:
        _audit_refusal("size_below_min_order")
        return None

    if requote_horizon_s is None:
        _audit_refusal("no_requote_horizon")
        log_quote_audit()
        return None

    tick = float(market.tick)
    bid_yes = _maker_bid(book_yes, tick)
    bid_no = _maker_bid(book_no, tick)
    if bid_yes is None or bid_no is None:
        _audit_refusal("no_bid_to_improve")
        return None

    locked = 1.0 - (bid_yes + bid_no)
    if locked <= 0.0:
        _audit_refusal("pair_not_under_par")
        return None

    # A resting bid larger than the size the book already displays at the same
    # price is not a measured market quantity, and the tape measurement has no
    # support beyond the displayed sizes it observed.  Cap the quote at the
    # measured best-bid depth (the level we are improving on).
    depth_yes = shares_at(book_yes, bid=True, price=best_bid(book_yes) or 0.0)
    depth_no = shares_at(book_no, bid=True, price=best_bid(book_no) or 0.0)
    if depth_yes > 0.0:
        shares = min(shares, depth_yes)
    if depth_no > 0.0:
        shares = min(shares, depth_no)
    if shares < min_order:
        _audit_refusal("depth_below_min_order")
        return None

    horizon = float(requote_horizon_s)
    priced_horizon = horizon
    clamped_from: Optional[float] = None
    if fill_probability is not None:
        p_yes = p_no = float(fill_probability)
        if p_yes <= 0.0 or p_yes > 1.0:
            _audit_refusal("fill_probability_out_of_range")
            return None
    else:
        model = horizon_model if horizon_model is not None else _default_quote_fill_horizon()
        if model is None:
            _audit_refusal("fill_model_missing")
            return None
        priced_yes = _quote_fill_probability(
            model, book_yes, bid_yes, horizon, tick, min_order
        )
        priced_no = _quote_fill_probability(
            model, book_no, bid_no, horizon, tick, min_order
        )
        if priced_yes is None or priced_no is None:
            # Outside the measured support: the probability of a fill over this
            # horizon is missing data, not a probability.
            _audit_refusal("fill_probability_unmeasured")
            log_quote_audit()
            return None
        p_yes, priced_horizon = priced_yes
        p_no, priced_horizon_no = priced_no
        if abs(priced_horizon_no - priced_horizon) > 1e-9:
            # The two legs share a tick and a minimum order, so they must share
            # a priced horizon; a disagreement means one book is not what the
            # table was measured on.
            _audit_refusal("priced_horizon_disagreement")
            return None
        if priced_horizon < horizon:
            clamped_from = horizon
            _warn_horizon_clamped(model, horizon, priced_horizon, tick, min_order)
        if p_yes <= 0.0 or p_no <= 0.0:
            # A quote the measurement says never fills cannot earn an edge.
            _audit_refusal("fill_probability_zero")
            return None

    schedule = market.fee_schedule
    rebate_yes = rebate_no = 0.0
    if schedule is not None:
        rebate_yes = maker_fee_rebate(bid_yes, 1.0, schedule)
        rebate_no = maker_fee_rebate(bid_no, 1.0, schedule)

    cost_yes = (
        float(unwind_cost_yes) if unwind_cost_yes is not None
        else _unwind_cost(book_yes, bid_yes)
    )
    cost_no = (
        float(unwind_cost_no) if unwind_cost_no is not None
        else _unwind_cost(book_no, bid_no)
    )

    components: dict = {
        "p_yes": p_yes,
        "p_no": p_no,
        "locked": locked,
        "rebate_yes": rebate_yes,
        "rebate_no": rebate_no,
        "cost_yes": cost_yes,
        "cost_no": cost_no,
        "markout": None,
        "surplus_yes": None,
        "surplus_no": None,
        "expected": None,
        "shares": shares,
        "horizon_s": priced_horizon,
    }

    markout = markout_model if markout_model is not None else _default_maker_markout()
    if markout is None:
        _audit_refusal("markout_model_missing", components)
        return None
    drift = markout.markout(priced_horizon, tick)
    if drift is None:
        _warn_unmeasured_markout(markout, priced_horizon, tick)
        _audit_refusal("markout_unmeasured", components)
        log_quote_audit()
        return None

    edge_if_both = locked + rebate_yes + rebate_no
    surplus_yes = rebate_yes - cost_yes - drift
    surplus_no = rebate_no - cost_no - drift
    expected = (
        p_yes * p_no * edge_if_both
        + p_yes * (1.0 - p_no) * surplus_yes
        + p_no * (1.0 - p_yes) * surplus_no
    )
    components.update(
        {
            "markout": drift,
            "surplus_yes": surplus_yes,
            "surplus_no": surplus_no,
            "expected": expected,
        }
    )

    if surplus_yes <= 0.0 or surplus_no <= 0.0:
        # A naked leg that does not cover its own measured costs is not
        # subsidised by the lock: the lock's weight is p^2 against p for the
        # naked leg, so this quote loses on its most likely non-zero outcome.
        _audit_refusal("single_leg_below_measured_cost", components)
        log_quote_audit()
        return None
    if expected <= 0.0:
        _audit_refusal("expected_not_positive", components)
        log_quote_audit()
        return None

    # Opportunity cost of the capital the pair locks.  A positive expected
    # value is not a reason to rest a quote: the same dollar can sit on the
    # reward book, whose optimum prices capital at ``min_daily_yield`` (the
    # allocator's KKT shadow price, USD per USD per day).  A measured
    # $6e-09 edge on a 9 s horizon is 7.7e-05 /USD/day - three orders of
    # magnitude below that hurdle - so it must be refused, not ranked.  The
    # yield is annualised over the horizon the quote was *priced* at, which is
    # exactly how long the capital is committed before the next requote.
    pair_capital = bid_yes + bid_no
    if (
        min_daily_yield is None
        or not math.isfinite(min_daily_yield)
        or pair_capital <= 0.0
        or priced_horizon <= 0.0
    ):
        # No measured opportunity cost: refusing is the only honest answer,
        # because "0" would make every positive-EV dust quote admissible.
        _audit_refusal("opportunity_cost_unmeasured", components)
        log_quote_audit()
        return None
    daily_yield = (expected / pair_capital) * (86_400.0 / priced_horizon)
    components.update({"daily_yield": daily_yield, "min_daily_yield": min_daily_yield})
    if daily_yield < min_daily_yield:
        _audit_refusal("below_shadow_yield", components)
        log_quote_audit()
        return None

    _QUOTE_AUDIT["accepted"] += 1
    # Every distinct accepted quote publishes its six components exactly once.
    # The engine re-prices the whole rotating slice each tick but only acts on a
    # price change (it skips a quote already resting at the same price), so the
    # dedupe key is the same event the venue sees: (market, both bids, horizon).
    # The summary line below counts every acceptance, logged or deduped.
    audit_key = (
        market.condition_id,
        round(bid_yes, 6),
        round(bid_no, 6),
        round(priced_horizon, 3),
    )
    if audit_key not in _ACCEPTED_QUOTES_LOGGED:
        if len(_ACCEPTED_QUOTES_LOGGED) >= _ACCEPTED_QUOTES_LOGGED_MAX:
            _ACCEPTED_QUOTES_LOGGED.clear()
        _ACCEPTED_QUOTES_LOGGED.add(audit_key)
        logger.info(
            f"CTF maker quote {market.condition_id[:12]} "
            f"{shares:.1f} sh @ {bid_yes:.4f}+{bid_no:.4f} "
            f"edge=${expected:.3e}/set {_format_components(components)}"
        )
    log_quote_audit()

    return MakerParityQuote(
        condition_id=market.condition_id,
        bid_yes=bid_yes,
        bid_no=bid_no,
        shares=shares,
        edge_usd=expected,
        locked_spread=locked,
        fill_prob_yes=p_yes,
        fill_prob_no=p_no,
        edge_if_both_fill=edge_if_both,
        priced_horizon_s=priced_horizon,
        horizon_clamped_from_s=clamped_from,
        markout_usd=drift,
        unwind_cost_yes=cost_yes,
        unwind_cost_no=cost_no,
        rebate_yes=rebate_yes,
        rebate_no=rebate_no,
    )


# ---------------------------------------------------------------------------
# On-chain leg clock: split / merge / redeem
#
# A merge is not a venue message.  It is a signed transaction that pays the
# measured Polygon RPC round trip and then waits for the next block, so its
# landing time is ``rpc_rtt + block_interval``, not the CLOB round trip the
# ``ActionBook`` uses for order messages.  Both inputs are measurements: the RPC
# round trip persisted by ``polymarketbot.tools.latency_probe`` and the block
# interval measured from consecutive heads.  When either is missing -- or when
# the head itself cannot be read (the RPC layer's rate limiter puts
# ``eth_blockNumber`` on a cooldown measured in hundreds of seconds) -- the leg
# is refused and logged.  There is no fallback interval: an on-chain action
# timed from an assumed block rate is a fabricated fill, and the ledger would
# credit par for a merge that has not landed.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OnChainTiming:
    """Measured flight time of an on-chain CTF leg."""

    rpc_rtt_s: float
    block_interval_s: float

    @property
    def flight_s(self) -> float:
        """Expected seconds from broadcast until the leg lands in a block."""
        return self.rpc_rtt_s + self.block_interval_s


class OnChainRefused(RuntimeError):
    """An on-chain leg was refused because its clock is not measured."""


def require_on_chain_timing(
    rpc_rtt_s: Optional[float],
    block_interval_s: Optional[float],
    *,
    action: str,
    head_unavailable_reason: Optional[str] = None,
) -> OnChainTiming:
    """The measured on-chain flight, or raise :class:`OnChainRefused`.

    ``action`` names the leg (``merge``/``split``/``redeem``) so the refusal is
    diagnosable from the log alone.  Every missing input is named in the
    refusal: an unreadable head, an unmeasured RPC round trip, an unmeasured
    block interval.  Nothing is defaulted; in particular there is no assumed
    block time to fall back to.
    """
    missing: list[str] = []
    if head_unavailable_reason:
        missing.append(f"live Polygon head ({head_unavailable_reason})")
    if rpc_rtt_s is None or not math.isfinite(float(rpc_rtt_s)) or float(rpc_rtt_s) <= 0.0:
        missing.append(
            "measured Polygon RPC round trip "
            "(run polymarketbot.tools.latency_probe: data/derived/local_latency.json "
            "carries no polygon_rpc endpoint)"
        )
    if (
        block_interval_s is None
        or not math.isfinite(float(block_interval_s))
        or float(block_interval_s) <= 0.0
    ):
        missing.append(
            "measured Polygon block interval (no consecutive-head sequence in "
            "data/tape/polygon_gas)"
        )
    if missing:
        reason = "; ".join(missing)
        logger.error(f"on-chain {action} refused: clock unmeasured -- {reason}")
        raise OnChainRefused(f"on-chain {action} refused: {reason}")
    timing = OnChainTiming(float(rpc_rtt_s), float(block_interval_s))
    logger.info(
        f"on-chain {action} admitted: flight {timing.flight_s:.2f}s "
        f"(rpc {timing.rpc_rtt_s:.2f}s + block {timing.block_interval_s:.2f}s)"
    )
    return timing


def _live_head_error() -> Optional[str]:
    """``None`` when the live head reads, else the refusal reason."""
    try:
        from polymarketbot.chain import ChainClient

        head = int(ChainClient().block_number())
    except Exception as exc:  # noqa: BLE001 - an unreadable head is the signal
        return str(exc) or "eth_blockNumber failed"
    if head <= 0:
        return f"eth_blockNumber returned {head}"
    return None


def _measured_rpc_rtt() -> Optional[float]:
    try:
        from polymarketbot.trading.ctf import load_persisted_rpc_rtt_s

        return load_persisted_rpc_rtt_s()
    except Exception as exc:  # noqa: BLE001 - a missing measurement is data, not a crash
        logger.warning(f"RPC round-trip measurement unavailable: {exc}")
        return None


def _measured_block_interval() -> Optional[float]:
    try:
        from polymarketbot.trading.ctf import load_measured_block_interval_s

        return load_measured_block_interval_s()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Block-interval measurement unavailable: {exc}")
        return None


def resolve_on_chain_timing(
    action: str,
    *,
    head_error: Optional[str] = None,
    probe_head: bool = True,
    rpc_rtt_loader: Optional[Callable[[], Optional[float]]] = None,
    block_interval_loader: Optional[Callable[[], Optional[float]]] = None,
) -> OnChainTiming:
    """Resolve every measured input for an on-chain leg, then admit or refuse.

    ``probe_head`` reads the live Polygon head; ``head_error`` lets a caller
    that already saw the failure pass its reason through instead of paying a
    second round trip.  The loaders are injectable so the gate is testable
    without network or tape.
    """
    reason = head_error
    if probe_head and not reason:
        reason = _live_head_error()
    rpc_rtt = (rpc_rtt_loader or _measured_rpc_rtt)()
    interval = (block_interval_loader or _measured_block_interval)()
    return require_on_chain_timing(
        rpc_rtt, interval, action=action, head_unavailable_reason=reason
    )


def merge_pairs(lots: Iterable[_LotLike], condition_id: str) -> float:
    """Complete sets available across a condition's token legs.

    Sums each token's positive shares over the lots whose ``condition_id``
    matches, then returns the minimum across the distinct tokens: one complete
    set holds one share of every token, so the minimum is the largest number of
    sets that can be merged.  The result is in shares; lots of other
    conditions, non-positive sizes and unknown tokens contribute nothing, and
    fewer than two distinct tokens yields ``0.0``.  Pure arithmetic, no I/O.
    """
    totals: dict[str, float] = {}
    for lot in lots:
        if lot.condition_id != condition_id:
            continue
        shares = float(lot.shares)
        if shares <= 0.0:
            continue
        totals[lot.token_id] = totals.get(lot.token_id, 0.0) + shares
    if len(totals) < 2:
        return 0.0
    return min(totals.values())
