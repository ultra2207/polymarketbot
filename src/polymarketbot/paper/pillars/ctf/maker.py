"""Maker complete-set parity: rest one bid on each leg, priced as a bet.

The maker optimizer is *not* an arbitrage.  ``1 - bid_yes - bid_no`` is a quote,
not a fill, and it is realized only when both legs fill; a single-leg fill
leaves naked directional inventory the ledger marks at the best bid.  The quote
is therefore priced as the expected value of that three-outcome bet over the
measured horizon-matched fill probability and the measured single-leg markout,
and emitted only when neither naked-leg branch loses money.
"""

from __future__ import annotations

import math
from typing import Optional

from polymarketbot.fees import maker_fee_rebate
from polymarketbot.logs import get_logger
from polymarketbot.paper.books import best_bid, shares_at
from polymarketbot.paper.venue import Market

from . import fill_model, parity
from . import markout as markout_models
from .fill_model import QuoteFillHorizon
from .markout import MakerMarkout
from .parity import MakerParityQuote

logger = get_logger("paper.ctf")

# There is no default requote lifetime.  A quote is *held* across ticks until
# its market is priced and refused again, so its lifetime is the caller's
# measured scan gap (``sweep_ticks * last_tick_dt``: measured 415-612 s in the
# 10:10 window, 428-459 s in the 12:53 window).  Substituting an assumed tick
# for a missing measurement would price a lifetime the engine never rests.


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
    parity._QUOTE_AUDIT["evaluated"] += 1
    if not market.binary:
        parity._audit_refusal("not_binary")
        return None
    shares = float(size_shares)
    min_order = float(market.min_order)
    if shares <= 0.0 or shares < min_order:
        parity._audit_refusal("size_below_min_order")
        return None

    if requote_horizon_s is None:
        parity._audit_refusal("no_requote_horizon")
        parity.log_quote_audit()
        return None

    tick = float(market.tick)
    bid_yes = parity._maker_bid(book_yes, tick)
    bid_no = parity._maker_bid(book_no, tick)
    if bid_yes is None or bid_no is None:
        parity._audit_refusal("no_bid_to_improve")
        return None

    locked = 1.0 - (bid_yes + bid_no)
    if locked <= 0.0:
        parity._audit_refusal("pair_not_under_par")
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
        parity._audit_refusal("depth_below_min_order")
        return None

    horizon = float(requote_horizon_s)
    priced_horizon = horizon
    clamped_from: Optional[float] = None
    if fill_probability is not None:
        p_yes = p_no = float(fill_probability)
        if p_yes <= 0.0 or p_yes > 1.0:
            parity._audit_refusal("fill_probability_out_of_range")
            return None
    else:
        model = horizon_model if horizon_model is not None else fill_model._default_quote_fill_horizon()
        if model is None:
            parity._audit_refusal("fill_model_missing")
            return None
        priced_yes = parity._quote_fill_probability(
            model, book_yes, bid_yes, horizon, tick, min_order
        )
        priced_no = parity._quote_fill_probability(
            model, book_no, bid_no, horizon, tick, min_order
        )
        if priced_yes is None or priced_no is None:
            # Outside the measured support: the probability of a fill over this
            # horizon is missing data, not a probability.
            parity._audit_refusal("fill_probability_unmeasured")
            parity.log_quote_audit()
            return None
        p_yes, priced_horizon = priced_yes
        p_no, priced_horizon_no = priced_no
        if abs(priced_horizon_no - priced_horizon) > 1e-9:
            # The two legs share a tick and a minimum order, so they must share
            # a priced horizon; a disagreement means one book is not what the
            # table was measured on.
            parity._audit_refusal("priced_horizon_disagreement")
            return None
        if priced_horizon < horizon:
            clamped_from = horizon
            fill_model._warn_horizon_clamped(model, horizon, priced_horizon, tick, min_order)
        if p_yes <= 0.0 or p_no <= 0.0:
            # A quote the measurement says never fills cannot earn an edge.
            parity._audit_refusal("fill_probability_zero")
            return None

    schedule = market.fee_schedule
    rebate_yes = rebate_no = 0.0
    if schedule is not None:
        rebate_yes = maker_fee_rebate(bid_yes, 1.0, schedule)
        rebate_no = maker_fee_rebate(bid_no, 1.0, schedule)

    cost_yes = (
        float(unwind_cost_yes) if unwind_cost_yes is not None
        else parity._unwind_cost(book_yes, bid_yes, schedule)
    )
    cost_no = (
        float(unwind_cost_no) if unwind_cost_no is not None
        else parity._unwind_cost(book_no, bid_no, schedule)
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

    markout = markout_model if markout_model is not None else markout_models._default_maker_markout()
    if markout is None:
        parity._audit_refusal("markout_model_missing", components)
        return None
    drift = markout.markout(priced_horizon, tick)
    if drift is None:
        markout_models._warn_unmeasured_markout(markout, priced_horizon, tick)
        parity._audit_refusal("markout_unmeasured", components)
        parity.log_quote_audit()
        return None

    edge_if_both = locked + rebate_yes + rebate_no
    # A leg that fills alone is not necessarily dumped at the bid.  The two
    # exits are (a) cross the spread as a taker, paying slippage + the full
    # taker fee (``cost_yes``), or (b) hold the leg to resolution, which costs
    # the measured adverse drift (``drift``) and pays no fee.  Only the cheaper
    # one is incurred, so charging both overstated the one-leg branch by the
    # fee and refused every quote unconditionally - the opposite error to the
    # one it replaced, which charged neither fee nor drift.
    exit_yes = min(cost_yes, drift)
    exit_no = min(cost_no, drift)
    surplus_yes = rebate_yes - exit_yes
    surplus_no = rebate_no - exit_no
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
        parity._audit_refusal("single_leg_below_measured_cost", components)
        parity.log_quote_audit()
        return None
    if expected <= 0.0:
        parity._audit_refusal("expected_not_positive", components)
        parity.log_quote_audit()
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
        parity._audit_refusal("opportunity_cost_unmeasured", components)
        parity.log_quote_audit()
        return None
    daily_yield = (expected / pair_capital) * (86_400.0 / priced_horizon)
    components.update({"daily_yield": daily_yield, "min_daily_yield": min_daily_yield})
    if daily_yield < min_daily_yield:
        parity._audit_refusal("below_shadow_yield", components)
        parity.log_quote_audit()
        return None

    parity._QUOTE_AUDIT["accepted"] += 1
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
    if audit_key not in parity._ACCEPTED_QUOTES_LOGGED:
        if len(parity._ACCEPTED_QUOTES_LOGGED) >= parity._ACCEPTED_QUOTES_LOGGED_MAX:
            parity._ACCEPTED_QUOTES_LOGGED.clear()
        parity._ACCEPTED_QUOTES_LOGGED.add(audit_key)
        logger.info(
            f"CTF maker quote {market.condition_id[:12]} "
            f"{shares:.1f} sh @ {bid_yes:.4f}+{bid_no:.4f} "
            f"edge=${expected:.3e}/set {parity._format_components(components)}"
        )
    parity.log_quote_audit()

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
