"""Landing: where a submitted action becomes cash, inventory or a resting quote.

An action lands against the book that exists when it lands, never against the
snapshot that triggered it, and the resting-quote record it lands into lives
here too because every quote is created by one of these paths. The venue-state
refresh (CLOB round trip, gas) lives here for the same reason: the landing
model prices every action with it.

Everything here is a free function over the engine: the engine owns the state,
this module owns the transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from polymarketbot.logs import get_logger
import polymarketbot.strategy.venue.reads as venue
from polymarketbot.strategy.books import ladder
import polymarketbot.strategy.settlement.decision as settlement

log = get_logger(__name__)


@dataclass
class Resting:
    strategy: str
    condition_id: str
    token_id: str
    price: float
    shares: float
    min_order: float
    placed: float
    cancel_at: Optional[float] = None
    tick: float = 0.01
    daily_payout: float = 0.0
    """Optimizer payout estimate in USDC/day for the capital behind this quote."""
    seen_ts: float = 0.0
    """Newest print already consumed by the fill model; stops re-counting."""
    accumulated: float = 0.0
    """Sub-min-order fill flow carried until it can clear the venue minimum."""
    initial_shares: float = 0.0
    """Size when placed; accrual scales with what is still resting."""
    max_age_s: float = 0.0
    """Lifetime the quote's EV was measured over; 0.0 = unbounded.

    A maker quote justified by ``p_fill`` at a requote horizon earns a
    probability nobody measured once it outlives that horizon, so it is pulled
    at ``placed + max_age_s`` rather than resting on an unmeasured probability.
    """


def refresh_venue_state(engine) -> None:
    """One measured refresh of venue state: CLOB round trip, then gas.

    Run on its own thread, not in the tick: both are network round trips
    that cost 0.2-0.9 s, and paying them inside the tick pushed it past the
    snapshot interval (measured `rtt=0.22s gas=0.90s` on a 10.06 s tick).
    """
    refreshed = engine.rtt.measure()
    if refreshed is None:
        log.error("RTT refresh failed and no persisted probe exists: "
                  "venue actions will be refused, not landed at 0 ms")
    if engine._gas_probe is None:
        return
    try:
        repriced = engine._gas_probe()
    except Exception:  # noqa: BLE001 - a failed probe is not a failed tick
        log.exception("gas refresh failed; keeping the last measured cost")
        return
    if repriced is None:
        log.warning("gas refresh unmeasured; keeping last measured "
                    f"${engine.gas_usd if engine.gas_usd is not None else float('nan'):.5f}")
        return
    engine.gas_usd = repriced


def probe_loop(engine) -> None:
    while not engine._stop.is_set():
        try:
            refresh_venue_state(engine)
        except Exception:  # noqa: BLE001 - the loop must outlive one bad probe
            log.exception("venue probe failed")
        engine._stop.wait(engine._rtt_refresh_s)


def land(engine, books: dict[str, dict], prints: list[venue.Print]) -> None:
    for action in engine.actions.due():
        payload = action.payload
        if action.kind == "reward_cancel":
            engine.reward_quotes.pop(payload["token_id"], None)
        elif action.kind == "reward_quote":
            engine.reward_quotes[payload["token_id"]] = payload["resting"]
        elif action.kind == "settle_buy":
            market = engine.by_condition.get(payload["condition_id"])
            fresh = (engine.feed.books(market.token_ids, force=True)
                     if market is not None else books)
            land_settlement(engine, action.strategy, payload, fresh)


def land_settlement(engine, strategy: str, payload: dict, books: dict[str, dict]) -> None:
    condition_id = payload["condition_id"]
    market = engine.by_condition.get(condition_id)
    if market is None:
        return
    verdict = settlement.verdict_for(engine.settlement_model, market)
    if verdict is None:
        return
    # The allocator's grant is the whole order. Landing re-prices against
    # the fresh book, so without this clamp it could re-consume the ladder
    # the risk model truncated - the exact path that put $1,000.00 into one
    # condition on 2026-11-10 14:25.
    granted = float(payload.get("max_cost") or 0.0)
    budget = min(engine.ledger.cash(strategy), granted) if granted > 0.0 else 0.0
    if budget <= 0.0:
        return
    token_id = market.token_ids[verdict.winning_index]
    book = books.get(token_id) or {}
    legs = settlement.fundable_legs(
        market, verdict, ladder(book, asks=True), budget
    )
    for leg in legs:
        if engine.ledger.buy(
            strategy,
            condition_id=condition_id,
            token_id=leg.token_id,
            shares=leg.shares,
            price=leg.price,
            fee=leg.fee_usd,
            par_value=settlement.mark_value(market, verdict),
            tag="settlement",
        ):
            engine._fills[strategy] += 1
            log.info(f"settled buy {leg.shares:.1f} @ {leg.price:.4f} "
                     f"p_win={leg.p_win:.3f} edge=${leg.edge_usd:.4f}")


def pending_for(engine, condition_id: str) -> bool:
    return any(action.payload.get("condition_id") == condition_id
               for action in engine.actions.pending())
