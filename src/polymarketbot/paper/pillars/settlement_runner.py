"""The settlement pillar's runner: fund the risk-adjusted verdict book.

Selection is the allocator's (see :mod:`polymarketbot.paper.pillars.tail_budget`),
not this module's. This module decides which verdict markets are *priced* this
tick, states why each one was refused, submits the allocator's grant, and
reconciles what is already held.
"""

from __future__ import annotations

import time

from polymarketbot.logs import get_logger
from polymarketbot.paper import venue
from polymarketbot.paper.actions import pending_for
from polymarketbot.paper.books import best_ask
from polymarketbot.paper.pillars import settlement
from polymarketbot.paper.pillars.tail_budget import tail_budget

log = get_logger(__name__)

# Reasons a verdict market produced no settlement candidate. Named so a
# zero-fill window states which one blocked it instead of logging nothing.
NO_MARKET = "no_market_in_universe"
NO_VERDICT = "no_live_verdict"
NO_ASK = "no_ask"
NO_EDGE = "edge_unpriced"
NEGATIVE_EDGE = "edge_negative"
# Gamma returned the row, but it is closed and no longer accepting orders, so
# there is no book to buy. Distinct from ``NO_MARKET`` on purpose: one is a
# coverage gap (Gamma has no such market) and the other is protocol reality.
MARKET_CLOSED = "market_closed"

# Once the settlement tally stops changing, reprint it this often so a silent
# radar is still legible as alive. Five minutes is long enough that a steady
# state costs six lines an hour instead of sixty.
_SETTLEMENT_HEARTBEAT_SEC = 300.0


def run_settlement(engine, books: dict[str, dict]) -> None:
    """Fund the risk-adjusted verdict book, within cash.

    Selection is the allocator's, not this method's. The old rule ranked
    markets by edge per dollar and let the highest win the whole treasury,
    which is how 1,049.1 shares and $1,000.00 landed in a single condition
    at 0.81-0.98 on 2026-11-10 14:25. The allocator prices each candidate's
    payout covariance against the book already held and returns the dollars
    this method may spend per condition; diversification (or a proven
    concentration) falls out of that solve and is logged by it.
    """
    now = time.time()
    engine.settlement_model.refresh(now)
    verdict_rows, radar_markets = engine._radar_snapshot()
    if radar_markets:
        # Rows the radar resolved for markets the walker never lists join
        # the index here, on the tick thread: exactly one writer per tick.
        engine.by_condition.update(radar_markets)
        engine._token_index.update(
            {token: market for market in radar_markets.values() for token in market.token_ids}
        )
    remaining = engine.ledger.cash("SettlementHarvester")
    refused: dict[str, int] = {}
    refused_ask: list[float] = []
    # The walker only lists open, order-accepting markets, and UMA proposes
    # after trading stops - so a verdict market is usually not in it. The
    # verdict index is the radar: fetch exactly those condition ids.
    verdict_ids = engine.settlement_model.tape.live_verdict_condition_ids(now)
    held_ids = [lot.condition_id for lot in engine.ledger.positions("SettlementHarvester")]
    wanted = list(dict.fromkeys([cid for cid in verdict_ids + held_ids if cid]))
    # Hand the radar thread its work list; it owns the HTTP, this loop does
    # not wait for it. Coverage is unchanged: the radar re-fetches each
    # condition on the same 60 s staleness the tick used.
    with engine._radar_lock:
        engine._radar_want = tuple(wanted)
    engine._radar_wake.set()
    if now - engine._held_ts >= 60.0:
        engine._held_ts = now
        reconcile_held(engine, verdict_rows)
    settle_sec = max(
        1.0, float(getattr(engine.settlement_model.tape, "measured_liveness_sec", 0.0) or 1.0)
    )
    priced: list[tuple[float, venue.Market, float, object]] = []
    for cid in wanted:
        market = engine.by_condition.get(cid)
        if market is None or not market.binary:
            reason = engine._market_refusal(cid, verdict_rows)
            refused[reason] = refused.get(reason, 0) + 1
            continue
        verdict = settlement.verdict_for(engine.settlement_model, market)
        if verdict is None:
            refused[NO_VERDICT] = refused.get(NO_VERDICT, 0) + 1
            continue
        token_id = market.token_ids[verdict.winning_index]
        ask = best_ask(books.get(token_id) or {})
        if ask is None or ask <= 0.0:
            # A market with no ask cannot be bought at any price: that is a
            # different fact from a market whose edge is negative, and the
            # difference is the whole question a zero-fill window asks.
            refused[NO_ASK] = refused.get(NO_ASK, 0) + 1
            continue
        edge = settlement.marginal_edge(market, verdict, ask)
        if edge is None:
            refused[NO_EDGE] = refused.get(NO_EDGE, 0) + 1
            continue
        if edge <= 0.0:
            refused[NEGATIVE_EDGE] = refused.get(NEGATIVE_EDGE, 0) + 1
            refused_ask.append(ask)
            continue
        priced.append((edge / ask, market, ask, verdict))
    budget = tail_budget(engine, priced, books, verdict_rows, settle_sec, remaining)
    for _edge, market, ask, _verdict in sorted(priced, key=lambda item: -item[0]):
        if pending_for(engine, market.condition_id):
            continue
        # The allocator's dollars are the whole order this tick: the landing
        # re-prices against the fresh book, so the reservation is what the
        # risk model decided this condition may absorb, not a floor.
        cap = budget.get(market.condition_id, 0.0)
        if cap <= 0.0:
            continue
        if ask * max(market.min_order, 1.0) > cap + 1e-9:
            continue
        if cap > remaining:
            cap = remaining
        if engine.actions.submit("settle_buy", "SettlementHarvester",
                                 {"condition_id": market.condition_id,
                                  "max_cost": cap}) is not None:
            remaining -= cap
    if now - engine._settlement_log_ts >= 60.0:
        engine._settlement_log_ts = now
        tally = " ".join(f"{reason}={count}" for reason, count in sorted(refused.items()))
        best = (f" best_ask={min(refused_ask):.4f} over {len(refused_ask)} refused"
                if refused_ask else "")
        line = (
            f"settlement candidates: {len(wanted)} verdict cid(s), "
            f"{len(priced)} +EV, cash ${remaining:.2f}"
            + (f" | refused: {tally}" if tally else "")
            + best
        )
        # A steady state is a *state*, not an event: reprinting an identical
        # tally every minute buries the change that matters. Measured window
        # 2026-11-10 12:53-13:23: 30 identical lines, not one of them a
        # finding. Log on change, and reprint once past the heartbeat so a
        # silent radar is still visible as alive.
        signature = (len(wanted), len(priced), tuple(sorted(refused.items())))
        last = getattr(engine, "_settlement_signature", None)
        quiet = now - getattr(engine, "_settlement_log_beat", 0.0)
        if signature != last or quiet >= _SETTLEMENT_HEARTBEAT_SEC:
            engine._settlement_signature = signature
            engine._settlement_log_beat = now
            log.info(line)


def reconcile_held(engine, verdict_rows: dict[str, dict]) -> None:
    """Mark held lots at the oracle's par and redeem the resolved ones.

    Two facts cannot both hold through ``venue.parse_market``: it returns
    ``None`` for any row with ``acceptingOrders == False`` (it is the
    *trading* gate), and redemption is only legal once the market is
    ``closed``. Reading the verdict off a parsed ``Market`` therefore made
    the redeem branch unreachable - a closed market never parsed, so a
    winning lot stayed marked at par and never became cash. The payout
    decision reads Gamma's own fields instead; only the *tradable* side of
    the pillar needs a ``Market``.

    ``verdict_rows`` is the radar thread's measured cache. This method does
    no I/O: the row fetch that used to run here was one of the three
    network calls that pushed a 5 s tick to 15-22 s.

    Accounting: par is written only when the oracle has a verdict, and cash
    is credited only when the settlement is final. A pending redemption is
    inventory at par - never cash, never a drawdown.
    """
    held = {}
    for lot in engine.ledger.positions("SettlementHarvester"):
        held.setdefault(lot.condition_id, []).append(lot)
    if not held:
        return
    for condition_id, lots in held.items():
        row = verdict_rows.get(condition_id)
        if row is None:
            continue
        token_ids = tuple(engine.gamma.parse_ids(row.get("clobTokenIds")) or ())
        if len(token_ids) < 2:
            continue
        # The market's own Gamma question is the exact join key the tape
        # already uses; the walker's copy (if still open) is the same text.
        known = engine.by_condition.get(condition_id)
        question = known.question if known is not None else str(row.get("question") or "")
        uma_status = str(row.get("umaResolutionStatus") or "").lower()
        closed = bool(row.get("closed"))
        verdict = settlement.verdict_for_fields(
            engine.settlement_model,
            condition_id=condition_id,
            question=question,
            outcomes=engine.gamma.parse_ids(row.get("outcomes")) or (),
            token_count=len(token_ids),
            prices=engine.gamma.parse_ids(row.get("outcomePrices")) or (),
            closed=closed,
            uma_status=uma_status,
            category=known.category if known is not None else "settlement",
        )
        if verdict is None:
            continue
        winner = token_ids[verdict.winning_index]
        engine.ledger.set_par(
            condition_id, winner, par=settlement.par_from_verdict(verdict)
        )
        if not closed:
            continue
        winners, losers = settlement.split_redemption(
            lots, condition_id=condition_id, token_ids=token_ids, verdict=verdict
        )
        for token_id, shares in winners:
            engine.ledger.redeem("SettlementHarvester", condition_id=condition_id,
                                 token_id=token_id, shares=shares,
                                 gas=engine.gas_usd or 0.0)
        for token_id, shares in losers:
            engine.ledger.write_off("SettlementHarvester", condition_id=condition_id,
                                    token_id=token_id, shares=shares)
