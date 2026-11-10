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

from dataclasses import dataclass
from typing import Optional

from polymarketbot.logs import get_logger
import polymarketbot.strategy.venue.reads as venue

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
