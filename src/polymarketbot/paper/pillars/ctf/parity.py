"""Complete-set parity: tranche/quote records, quote helpers and the audit.

``ParityTranche`` is one taker buy at a single ask pair; ``MakerParityQuote`` is
one two-sided resting bid pair priced as a three-outcome bet.  The helpers here
are the pure book arithmetic both optimizers share (``_maker_bid``,
``_unwind_cost``), the measured fill-probability lookup, and the quote audit
that makes every acceptance and refusal checkable from the log.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Tuple

from polymarketbot.fees import FeeSchedule, taker_fee
from polymarketbot.logs import get_logger
from polymarketbot.paper.books import best_ask, best_bid, shares_at

from .fill_model import QuoteFillHorizon, _warn_unmeasured_horizon

logger = get_logger("paper.ctf")

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


def _unwind_cost(book: dict, quote: float, schedule: Optional[FeeSchedule] = None) -> float:
    """Per-share loss from exiting a filled leg at the executable best bid.

    The ledger marks an open lot at the best bid, so a leg that fills alone is
    already worth ``best_bid - quote`` per share.  With no bid on the book the
    leg has no executable exit and the whole quote is at risk.

    Exiting is a **taker** sell at the bid, so it also pays the full taker fee
    on that price.  Omitting it understated the one-leg branch by 10-20x and
    made a structurally loss-making quote look merely marginal: the maker
    rebate is ``g * fee`` while this exit is ``1.0 * fee``, so a single filled
    leg earns ``fee * (g - 1)`` and is negative for every venue rebate rate
    below 1.0 (measured 2026-11-10: break-even needs g = 1.108-2.131 against a
    maximum published rebate of 0.50 at obsidian).  ``schedule is None`` is a
    fee-free market, the same convention the rebate path below uses, so the fee
    there is a measured zero rather than a missing input.
    """
    bid = best_bid(book)
    if bid is None:
        return float(quote)
    slippage = max(0.0, float(quote) - float(bid))
    if schedule is None:
        return slippage
    return slippage + float(taker_fee(1.0, float(bid), schedule))

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
