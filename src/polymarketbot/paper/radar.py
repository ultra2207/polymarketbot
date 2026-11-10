"""The settlement radar: every settlement-stage network call, on its own thread.

The decision tick must not pay the venue's latency. A tick stage that can take
15-22 s cannot hold a 5 s interval, and the stage that did it was pure I/O: the
UMA log sweep, and two Gamma row lookups that share its one-per-minute gate.
This module owns all three and publishes what it measured; the settlement
runner reads the cache and never blocks.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from polymarketbot.logs import get_logger
from polymarketbot.paper import venue
from polymarketbot.paper.pillars.settlement_runner import MARKET_CLOSED, NO_MARKET

log = get_logger(__name__)

# How often the radar thread re-fetches a verdict condition's Gamma row. This
# is the same one-per-minute staleness the tick used before the fetch moved off
# the critical path, so the radar's coverage is unchanged - only its latency
# stopped being the tick's.
_RADAR_REFRESH_SEC = 60.0


def settlement_radar_loop(engine) -> None:
    """Every settlement-stage network call, on its own thread.

    The decision tick must not pay the venue's latency. A tick stage that
    can take 15-22 s cannot hold a 5 s interval, and the stage that did it
    was pure I/O: the UMA log sweep, and two Gamma row lookups that share
    its one-per-minute gate. This thread owns all three and publishes what
    it measured; :meth:`_settlement` reads the cache and never blocks.

    Coverage still advances because the thread keeps cycling at the tick
    interval while each individual condition is re-fetched on the same
    60 s staleness the tick used, so the radar's reach is unchanged.
    """
    while not engine._stop.is_set():
        started = time.monotonic()
        now = time.time()
        if engine.poller is not None:
            try:
                engine.poller.poll(now)
            except Exception:  # noqa: BLE001 - the durable tape still prices markets
                log.exception("UMA poll failed; using the durable verdict tape")
        with engine._radar_lock:
            wanted = list(engine._radar_want)
        fetch_verdict_rows(engine, wanted, now)
        engine._radar_cycles += 1
        engine._radar_cost = time.monotonic() - started
        # An `Event.wait` returns immediately while the flag is set, so the
        # flag has to be cleared *after* it is consumed: clearing first
        # would let a request that lands during the cycle be waited out.
        engine._radar_wake.wait(engine.interval)
        engine._radar_wake.clear()


def fetch_verdict_rows(engine, wanted: list[str], now: float) -> None:
    """Refresh Gamma rows for the conditions the settlement stage prices."""
    stale = [
        cid for cid in wanted
        if cid and now - engine._radar_fetched.get(cid, 0.0) >= _RADAR_REFRESH_SEC
    ]
    if not stale:
        return
    try:
        rows = engine.gamma.markets(stale) or []
    except Exception:  # noqa: BLE001 - one failed page is retried next cycle
        log.warning("verdict row fetch failed; keeping the last measured rows",
                    exc_info=True)
        return
    for row in rows:
        fetched = str(row.get("conditionId") or "").lower()
        if not fetched:
            continue
        with engine._radar_lock:
            engine._verdict_rows[fetched] = row
            market = venue.parse_market(row)
            if market is not None:
                engine._radar_markets[market.condition_id] = market
    for cid in stale:
        # A condition Gamma does not return is recorded as refreshed too:
        # the refusal then states "Gamma has no such row" instead of
        # re-asking every cycle for a market that does not exist.
        engine._radar_fetched[cid] = now


def radar_snapshot(engine) -> tuple[dict[str, dict], dict[str, venue.Market]]:
    """The rows and markets the radar has measured so far."""
    with engine._radar_lock:
        return dict(engine._verdict_rows), dict(engine._radar_markets)


def make_uma_poller(engine) -> Any:
    """Wire live UMA proposal polling when an RPC provider is configured.

    The block interval is measured from two consecutive heads; until it is
    measured the poller is not constructed, because an invented interval
    makes every proposal's age (and therefore its liveness) wrong.
    """
    try:
        from polymarketbot.chain import ChainClient
        from polymarketbot.paper import uma_poll
    except Exception:  # noqa: BLE001 - optional chain dependency
        return None
    # The tape's header sequence is the primary measurement: 407
    # consecutive headers beat two live heads, and it is the same number
    # the on-chain flight model uses, so the poller and the CTF legs cannot
    # disagree about the protocol's block time.
    interval_s: Optional[float] = None
    try:
        from polymarketbot.trading.ctf import load_measured_block_interval_s

        interval_s = load_measured_block_interval_s()
    except Exception as exc:  # noqa: BLE001 - no tape is a valid paper mode
        log.warning(f"Block interval not on tape ({exc}); measuring live heads")
    try:
        chain = ChainClient()
        first_block = int(chain.block_number())
    except Exception as exc:  # noqa: BLE001 - no RPC is a valid paper mode
        log.warning(f"UMA polling disabled: no RPC head ({exc})")
        return None
    if interval_s is None:
        first_ts = time.monotonic()
        time.sleep(min(5.0, max(1.0, engine.interval / 2.0)))
        try:
            second_block = int(chain.block_number())
        except Exception:  # noqa: BLE001
            return None
        elapsed = max(1e-6, time.monotonic() - first_ts)
        advanced = second_block - first_block
        if advanced <= 0:
            log.warning("UMA polling disabled: block interval did not advance")
            return None
        # Seconds per block. Dividing blocks by elapsed measures blocks
        # per second, which reported 0.79 s while the header tape measured
        # 1.50 s - a 1.9x error in every proposal age and liveness window.
        interval_s = elapsed / advanced
    if interval_s <= 0.0:
        log.warning("UMA polling disabled: block interval did not advance")
        return None
    poller = uma_poll.UmaProposalPoller(
        tape=engine.settlement_model.tape,
        providers=[chain],
        state_path="data/derived/uma/uma_poll_state.json",
        block_interval_s=interval_s,
        liveness_fallback_sec=uma_poll.UMA_POLL_LIVENESS_FALLBACK_SEC,
        max_blocks=uma_poll.UMA_POLL_MAX_BLOCKS,
        max_windows=uma_poll.UMA_POLL_MAX_WINDOWS,
        confirmations=uma_poll.UMA_POLL_CONFIRMATIONS,
    )
    log.info(f"UMA live polling enabled: block interval {interval_s:.2f}s")
    return poller


def market_refusal(condition_id: str, verdict_rows: dict[str, dict]) -> str:
    """Why a verdict's market produced no candidate, from Gamma's own row.

    ``no_market_in_universe`` used to carry two facts that need opposite
    responses. A row that Gamma does not have is a coverage gap: the verdict
    exists and the radar cannot reach its market. A row that Gamma *does*
    have but that is ``closed`` and no longer accepting orders is protocol
    reality - the verdict market stopped trading before the oracle spoke,
    so there is no book to buy and no trade was missed. Measured 2026-11-10
    12:53-13:23: all three refusals were the second kind (Ferrari F1
    Constructors' Champion, and both WA-05 House seat legs), which is
    invisible when they are counted as "not in universe".
    """
    row = verdict_rows.get(condition_id)
    if row is None:
        return NO_MARKET
    if row.get("closed") is True or row.get("acceptingOrders") is False:
        return MARKET_CLOSED
    return NO_MARKET
