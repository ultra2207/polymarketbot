"""Landing: where a submitted action becomes cash, inventory or a resting quote.

An action lands against the book that exists when it lands, never against the
snapshot that triggered it, and the resting-quote record it lands into lives
here too because every quote is created by one of these paths. The venue-state
refresh (CLOB round trip) lives here for the same reason: the landing model
prices every action with it.

Everything here is a free function over the engine: the engine owns the state,
this module owns the transitions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import polymarketbot.strategy.venue.reads as venue
from polymarketbot.fees import taker_fee
from polymarketbot.logs import get_logger
from polymarketbot.risk.queue_position import QueuePositionTracker
from polymarketbot.strategy.books import ladder

log = get_logger(__name__)

#: ``ActionBook`` kind for a Tier-2 deskew cross (``paper.deskew`` submits it).
TAKER_KIND = "deskew_taker"


def new_order_id(strategy: str, token_id: str) -> str:
    """A unique tracker identity for one virtual order.

    Two quotes on the same token at the same price are still different orders:
    reusing ``(token, price, placed)`` as the identity let a replacement read
    the filled state of the order it replaced.
    """
    return f"{strategy}:{token_id}:{uuid.uuid4().hex[:12]}"


def queue_tracker(engine) -> QueuePositionTracker:
    """The engine's FIFO position tracker, created on first use.

    Paper fills are reconstructed from the L2 tape by one tracker per engine
    (``risk/queue_position.py``): the resting quote is not a pro-rata share of
    a level, it is a specific FIFO position behind whatever size was at its
    price when it landed. The tracker holds that position across ticks, so it
    must outlive a single fill pass.
    """
    tracker = getattr(engine, "queues", None)
    if tracker is None:
        tracker = QueuePositionTracker()
        engine.queues = tracker
    return tracker


@dataclass
class Resting:
    strategy: str
    condition_id: str
    token_id: str
    price: float
    shares: float
    min_order: float
    placed: float
    cancel_at: float | None = None
    tick: float = 0.01
    daily_payout: float = 0.0
    """Optimizer payout estimate in USDC/day for the capital behind this quote."""
    seen_ts: float = 0.0
    """Newest print replayed into the tracker; stops re-counting."""
    accumulated: float = 0.0
    """Sub-min-order fill flow carried until it can clear the venue minimum."""
    initial_shares: float = 0.0
    """Size when placed; accrual scales with what is still resting."""
    order_id: str = ""
    """Tracker identity once the quote lands.

    Fills are booked from the FIFO position this id carries, so the id is the
    link between the virtual order and the reconstructed queue. It is empty
    until the quote lands (or is first seen by the fill pass) and is never
    reused by a later quote.
    """
    booked_fill: float = 0.0
    """Cumulative shares the tracker has consumed from this order."""
    max_age_s: float = 0.0
    """Lifetime the quote's EV was measured over; 0.0 = unbounded.

    A maker quote justified by ``p_fill`` at a requote horizon earns a
    probability nobody measured once it outlives that horizon, so it is pulled
    at ``placed + max_age_s`` rather than resting on an unmeasured probability.
    """


def refresh_venue_state(engine) -> None:
    """One measured refresh of the venue's round trip.

    Run on its own thread, not in the tick: the probe is a network round trip
    that costs 0.2-0.9 s, and paying it inside the tick pushed it past the
    snapshot interval (measured ``rtt=0.22s`` on a 10.06 s tick).
    """
    refreshed = engine.rtt.measure()
    if refreshed is None:
        log.error("RTT refresh failed and no persisted probe exists: "
                  "venue actions will be refused, not landed at 0 ms")


def probe_loop(engine) -> None:
    while not engine._stop.is_set():
        try:
            refresh_venue_state(engine)
        except Exception:
            log.exception("venue probe failed")
        engine._stop.wait(engine._rtt_refresh_s)


def land_taker(engine, strategy: str, payload: dict, books: dict[str, dict]) -> None:
    """Land a Tier-2 cross against the book that exists now, not the decision.

    The action reached ``ActionBook.due()`` only after the measured CLOB round
    trip, so ``books`` is the post-flight snapshot. Depth is consumed level by
    level, so a fill is partial exactly when the landing book is thinner than
    the requested size, and the venue's taker fee is charged per executed price.
    A missing book, fee schedule or cash refuses the landing and leaves the
    ledger untouched.
    """
    token_id = str(payload["token_id"])
    side = str(payload["side"]).upper()
    requested = float(payload["shares"])
    pathway = str(payload.get("pathway") or "deskew")
    condition_id = str(payload.get("condition_id") or "")
    if side not in ("BUY", "SELL") or requested <= 0.0:
        return
    schedule = payload.get("fee_schedule")
    if schedule is None:
        log.error(
            f"taker landing refused for {token_id[:12]}: no measured fee schedule, "
            "a taker fill cannot be priced as free"
        )
        return
    if side == "SELL":
        available = sum(
            lot.shares
            for lot in engine.ledger.positions(strategy)
            if lot.token_id == token_id
        )
        requested = min(requested, available)
        if requested <= 0.0:
            log.warning(
                f"taker landing refused for {token_id[:12]}: no inventory left to sell"
            )
            return
    levels = ladder(books.get(token_id) or {}, asks=(side == "BUY"))
    if not levels:
        log.warning(
            f"taker landing refused for {token_id[:12]}: no "
            f"{'ask' if side == 'BUY' else 'bid'} book at landing"
        )
        return

    remaining, filled, cost, fee = requested, 0.0, 0.0, 0.0
    for price, size in levels:
        take = min(size, remaining)
        if take <= 0.0:
            continue
        cost += take * price
        fee += taker_fee(take, price, schedule)
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if filled <= 0.0:
        log.warning(f"taker landing refused for {token_id[:12]}: empty book side")
        return
    price = cost / filled

    if side == "BUY":
        if cost + fee > engine.ledger.cash(strategy) + 1e-9:
            log.warning(
                f"taker landing refused for {token_id[:12]}: BUY "
                f"{filled:.2f} @ ${price:.4f} + fee ${fee:.5f} exceeds cash "
                f"${engine.ledger.cash(strategy):.2f}"
            )
            return
        if not engine.ledger.buy(
            strategy,
            condition_id=condition_id,
            token_id=token_id,
            shares=filled,
            price=price,
            fee=fee,
            tag=f"deskew_{pathway}",
        ):
            log.warning(f"taker landing refused for {token_id[:12]}: ledger refused")
            return
    else:
        engine.ledger.sell(
            strategy,
            condition_id=condition_id,
            token_id=token_id,
            shares=filled,
            price=price,
            fee=fee,
            tag=f"deskew_{pathway}",
        )
    log.warning(
        f"paper de-skew landing ({pathway}): {side} {filled:.2f}/{requested:.2f} "
        f"{token_id[:12]} VWAP ${price:.4f} fee ${fee:.5f} "
        f"[{'partial' if filled + 1e-9 < requested else 'full'} fill]"
    )


def land(engine, books: dict[str, dict], prints: list[venue.Print]) -> None:
    for action in engine.actions.due():
        payload = action.payload
        if action.kind == TAKER_KIND:
            land_taker(engine, action.strategy, payload, books)
        elif action.kind == "reward_cancel":
            resting = engine.reward_quotes.pop(payload["token_id"], None)
            if resting is not None and resting.order_id:
                queue_tracker(engine).drop(resting.token_id, resting.order_id)
        elif action.kind == "reward_quote":
            resting = payload["resting"]
            previous = engine.reward_quotes.get(resting.token_id)
            if previous is not None and previous.order_id:
                queue_tracker(engine).drop(resting.token_id, previous.order_id)
            if not resting.order_id:
                resting.order_id = new_order_id(resting.strategy, resting.token_id)
            # The queue in front of the order is whatever is displayed at its
            # price at the instant it lands. Size that arrives later is behind
            # us (FIFO), so this snapshot is the only one that can define
            # ``ahead``; every later book update can only clamp it downward.
            book = books.get(resting.token_id) or {}
            resting.initial_shares = resting.initial_shares or resting.shares
            queue_tracker(engine).on_entry(
                resting.order_id,
                resting.token_id,
                "BUY",
                resting.price,
                resting.shares,
                resting.placed,
                ladder(book, asks=False),
                ladder(book, asks=True),
            )
            engine.reward_quotes[resting.token_id] = resting
