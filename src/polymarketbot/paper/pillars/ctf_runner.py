"""The CTF pillar's runner: fund parity takers and maker pairs by edge per dollar.

Every binary market competes for the same cash: the pillar's optimizer returns
per-market edges, and this loop funds them in descending edge per dollar until
the budget is exhausted. Quoting every market with the whole bankroll would
over-allocate the same dollars many times over.
"""

from __future__ import annotations

import time
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.paper import venue
from polymarketbot.paper.actions import Resting, pending_for
from polymarketbot.paper.books import best_bid, shares_at
from polymarketbot.paper.pillars import ctf
from polymarketbot.paper.pillars.reward_runner import apply_resting_fills

log = get_logger(__name__)


def markets_with_books(engine, books: dict[str, dict]) -> list[venue.Market]:
    """Markets that have at least one fresh book this tick.

    The universe walk discovers every active market, but a decision needs a
    price. Iterating the book set keeps the full universe eligible — any
    market the feed has priced in the last ``max_age`` seconds competes —
    without re-optimising hundreds of thousands of bookless markets every
    tick.
    """
    out: list[venue.Market] = []
    seen: set[str] = set()
    for token in books:
        market = engine._token_index.get(token)
        if market is None or market.condition_id in seen:
            continue
        seen.add(market.condition_id)
        out.append(market)
    return out


def run_ctf(engine, books: dict[str, dict], prints: list[venue.Print]) -> None:
    if engine.gas_usd is None:
        return
    now = time.time()
    apply_resting_fills(engine, engine.maker_quotes, "CtfMinter", books, prints, now)
    remaining = max(0.0, engine.ledger.cash("CtfMinter") - engine._reserved(engine.maker_quotes))
    takers: list[tuple[float, float, venue.Market]] = []
    makers: list[tuple[float, venue.Market, Any, float]] = []
    priced_all = markets_with_books(engine, books)
    # The EV gate costs ~2 ms per market. Evaluate a rotating, logged slice
    # so the stage stays inside the tick interval; every priced market is
    # still evaluated, just not all in the same tick.
    sweep_ticks = max(1, -(-len(priced_all) // engine._ctf_budget))
    log.debug(
        f"ctf sweep: {len(priced_all)} priced markets every {sweep_ticks} ticks, "
        f"{sweep_ticks * max(engine._last_tick_dt, 1.0):.0f}s requote horizon"
    )
    if len(priced_all) > engine._ctf_budget:
        start = engine._ctf_cursor % len(priced_all)
        priced = priced_all[start:start + engine._ctf_budget]
        if len(priced) < engine._ctf_budget:
            priced += priced_all[: engine._ctf_budget - len(priced)]
        engine._ctf_cursor = (start + engine._ctf_budget) % len(priced_all)
        if now - engine._ctf_coverage_ts >= 60.0:
            engine._ctf_coverage_ts = now
            log.info(
                f"ctf scan budget: {engine._ctf_budget} of {len(priced_all)} priced "
                f"markets per tick (full sweep every "
                f"{-(-len(priced_all) // engine._ctf_budget)} ticks)"
            )
    else:
        priced = priced_all
    sweep_ticks = max(1, -(-len(priced_all) // engine._ctf_budget))
    horizon_s = sweep_ticks * max(engine._last_tick_dt, 1.0)
    refused: set[str] = set()
    for market in priced:
        if not market.binary or market.closed:
            continue
        yes, no = market.token_ids
        if yes not in books or no not in books:
            continue
        book_yes, book_no = books.get(yes) or {}, books.get(no) or {}
        tranches = ctf.optimize_taker_parity(market, book_yes, book_no, remaining,
                                             engine.gas_usd)
        if tranches:
            cost = sum(t.shares * (t.price_yes + t.price_no) + t.fee_yes + t.fee_no
                       + t.gas for t in tranches)
            edge = sum(t.edge_usd for t in tranches)
            if cost > 0.0:
                takers.append((edge / cost, cost, market))
            continue
        bid_yes = best_bid(book_yes)
        bid_no = best_bid(book_no)
        if bid_yes is None or bid_no is None:
            continue
        # Size to what the displayed book can actually fill, not to the
        # whole bankroll: the pillar's fill probability is only measured
        # inside that support.
        depth_yes = shares_at(book_yes, bid=True, price=bid_yes)
        depth_no = shares_at(book_no, bid=True, price=bid_no)
        size_budget = min(depth_yes, depth_no, remaining / max(market.tick, bid_yes + bid_no))
        quote = ctf.optimize_maker_parity(
            market, book_yes, book_no, size_budget, requote_horizon_s=horizon_s,
            min_daily_yield=engine._shadow_yield,
        )
        if quote is None:
            refused.add(market.condition_id)
            continue
        pair_cost = quote.bid_yes + quote.bid_no
        if pair_cost <= 0.0:
            continue
        makers.append((quote.edge_usd / pair_cost, market, quote, pair_cost))
    for _ratio, cost, market in sorted(takers, key=lambda item: -item[0]):
        if cost > remaining or pending_for(engine, market.condition_id):
            continue
        if engine.actions.submit("ctf_taker", "CtfMinter",
                                 {"condition_id": market.condition_id}) is not None:
            remaining -= cost
    for _ratio, market, quote, pair_cost in sorted(makers, key=lambda item: -item[0]):
        if pending_for(engine, market.condition_id):
            continue
        yes, no = market.token_ids
        size = min(quote.shares, remaining / pair_cost)
        if size < market.min_order:
            continue
        remaining -= size * pair_cost
        for token_id, price in ((yes, quote.bid_yes), (no, quote.bid_no)):
            current = engine.maker_quotes.get(token_id)
            if current and abs(current.price - price) < 1e-9:
                continue
            if current is not None:
                current.cancel_at = now
                engine.actions.submit("ctf_cancel", "CtfMinter", {"token_id": token_id})
            resting = Resting("CtfMinter", market.condition_id, token_id, price,
                              size, market.min_order, now + engine._rtt(),
                              tick=market.tick,
                              max_age_s=quote.priced_horizon_s)
            engine.actions.submit("ctf_quote", "CtfMinter",
                                  {"token_id": token_id, "resting": resting})
    # Cancel only orders the optimizer no longer wants. Cancelling merely
    # because this tick's cash was already reserved makes the book oscillate
    # between "quote everything" and "cancel everything".
    wanted_tokens: set[str] = set()
    for _ratio, market, _quote, _cost in makers:
        wanted_tokens.update(market.token_ids)
    for token_id, current in list(engine.maker_quotes.items()):
        # A quote outside this tick's rotating slice is held, not cancelled:
        # its requote lifetime is the measured full-sweep horizon. Only a
        # market that was priced and refused this tick loses its order.
        if current.condition_id not in refused or current.cancel_at is not None:
            continue
        if token_id in wanted_tokens:
            continue
        current.cancel_at = now
        engine.actions.submit("ctf_cancel", "CtfMinter", {"token_id": token_id})
    held: dict[str, list[Any]] = {}
    for lot in engine.ledger.positions("CtfMinter"):
        held.setdefault(lot.condition_id, []).append(lot)
    merge_timing: Any = None
    if held:
        # One live head read per tick serves every merge candidate.
        try:
            merge_timing = ctf.resolve_on_chain_timing("merge")
        except ctf.OnChainRefused as exc:
            log.warning(f"{exc}; {len(held)} merge candidate(s) held")
    for condition_id, lots in held.items():
        market = engine.by_condition.get(condition_id)
        if market is None or not market.binary:
            continue
        if merge_timing is None:
            continue
        pairs = ctf.merge_pairs(lots, condition_id)
        # Each merged pair redeems at par ($1). The break-even is the
        # measured gas of one action, not a guessed share count.
        if pairs * 1.0 > float(engine.gas_usd or 0.0):
            engine.actions.submit("ctf_merge", "CtfMinter", {
                "condition_id": condition_id, "token_id": market.token_ids[0],
                "pair_shares": pairs, "gas": engine.gas_usd,
            }, rtt=merge_timing.flight_s)
