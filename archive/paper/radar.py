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
import polymarketbot.strategy.venue.reads as venue
from polymarketbot.paper.pillars.settlement_runner import MARKET_CLOSED, NO_MARKET
from polymarketbot.strategy.settlement.fair_value import normalize_question

log = get_logger(__name__)

# How often the radar thread re-fetches a verdict condition's Gamma row. This
# is the same one-per-minute staleness the tick used before the fetch moved off
# the critical path, so the radar's coverage is unchanged - only its latency
# stopped being the tick's.
_RADAR_REFRESH_SEC = 60.0

# -- near-close staging ------------------------------------------------------
# The verdict index is built from oracle proposals, and a proposal cannot name
# a condition id: ``ProposePrice`` is an Optimistic Oracle event keyed by the
# request, so the benchmark binds it by the market's Gamma *question*.
# ``tape.bind_condition_ids`` can only bind a question it was handed, and the
# universe walker drops a market the moment it stops accepting orders
# (``venue.parse_market`` refuses those rows). Measured 2026-11-10 10:08Z: of
# 34 proposals in the live overlay, 27 carried no condition id, and the single
# one still inside its liveness window was one of them - so the pillar priced
# zero verdicts while the venue held 10,028 markets closing inside 24 h.
#
# Staging therefore walks the venue's own close-date filter and hands the
# benchmark those questions *before* any proposal exists. It never derives a
# verdict: a market with no proposal still has no price.
_CLOSE_SPAN_ARTIFACT = "data/derived/uma/gamma_closed_window.parquet"
# Coverage percentile on the measured span between a market's ``endDate`` and
# ``closedTime``. 95 % of the 134,664 resolved markets in that artifact had
# their book gone within it; the remaining tail is long-dated markets closed a
# week after their endDate, which would stretch the window over the whole
# universe for no tradable gain. Stated, logged, and measured - not a guess.
_CLOSE_SPAN_QUANTILE = 0.95


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

    It also owns the near-close sweep (:func:`stage_near_close`): the venue's
    own cohort of markets whose ``endDate`` is near, which is the only way a
    market can be bound and priced *before* its oracle proposal exists.
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
        # Verdict rows first: they are the markets that can be funded this
        # tick, so the sweep must never delay them.
        fetch_verdict_rows(engine, wanted, now)
        stage_near_close(engine, now)
        # Every cycle, not every sweep: a tape refresh rebuilds the verdict
        # index from the parquet each minute, so the join has to be reapplied.
        bind_near_close(engine)
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


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def close_span_sec(engine) -> float:
    """How long a market can still carry a book after its ``endDate``.

    Measured once from the resolved-market artifact: the p95 of
    ``closedTime - endDate`` over every market Gamma has closed in it. A book
    stops existing when the market stops accepting orders, which is at or
    before ``closedTime``, so this span is deliberately *wider* than book death
    - staging too early costs a page of rows, staging too late loses the
    verdict.

    Without the artifact the measurement is unavailable and the fallback is the
    tape's own measured liveness (the window in which a proposal can still be a
    live verdict). The degradation is logged, because a smaller horizon is a
    smaller reach.
    """
    cached = getattr(engine, "_near_close_span_sec", None)
    if cached:
        return float(cached)
    span = 0.0
    source = ""
    try:
        import duckdb

        con = duckdb.connect()
        try:
            row = con.execute(
                "SELECT quantile_cont(delta, ?) FROM ("
                "  SELECT epoch(TRY_CAST(closedTime AS TIMESTAMP))"
                "       - epoch(TRY_CAST(endDate AS TIMESTAMP)) AS delta"
                "  FROM read_parquet(?)"
                "  WHERE TRY_CAST(closedTime AS TIMESTAMP) IS NOT NULL"
                "    AND TRY_CAST(endDate AS TIMESTAMP) IS NOT NULL"
                "    AND TRY_CAST(closedTime AS TIMESTAMP) > TRY_CAST(endDate AS TIMESTAMP)"
                ")",
                [_CLOSE_SPAN_QUANTILE, _CLOSE_SPAN_ARTIFACT],
            ).fetchone()
        finally:
            con.close()
        span = float((row or [0.0])[0] or 0.0)
        source = f"{_CLOSE_SPAN_ARTIFACT} p{_CLOSE_SPAN_QUANTILE:.0%}"
    except Exception as exc:  # noqa: BLE001 - a missing artifact is a degraded span
        log.warning(f"close span not measurable ({exc}); "
                    "falling back to the tape's measured liveness")
    if span <= 0.0:
        tape = getattr(getattr(engine, "settlement_model", None), "tape", None)
        span = max(1.0, float(getattr(tape, "measured_liveness_sec", 0.0) or 1.0))
        source = "tape liveness (no close-span artifact)"
    engine._near_close_span_sec = span
    log.info(
        f"near-close staging span {span / 3600.0:.2f}h from {source}: books are "
        "gone by then, so binding a market later cannot produce a tradable verdict"
    )
    return span


def stage_near_close(engine, now: float, force: bool = False) -> None:
    """Put every market closing soon into the radar's reach, without a price.

    The settlement pillar can only buy a market *after* its oracle proposal,
    and a proposal carries no condition id, so the benchmark has to know the
    market's question in advance. The universe walk alone cannot supply it: it
    lists only order-accepting markets and drops a market the moment trading
    stops, which is exactly what happens before the oracle speaks.

    So this sweep asks the venue for its own near-close cohort
    (``end_date_min``/``end_date_max``) and does three things with it: keeps the
    rows where the settlement decision reads Gamma's fields, binds each
    question to its condition id in the verdict benchmark, and publishes the
    cohort for the runner to price. It derives **no** verdict - a market with
    no proposal still prices to ``None`` - so nothing here can invent a value
    for an unresolved market.

    The window is ``[now - tape liveness, now + close span]``. Both bounds are
    measured: a proposal older than the liveness window is no longer a live
    verdict, and a book older than the close span no longer exists.

    The sweep period is derived, not chosen: a market with ``endDate`` E is
    inside a sweep's window whenever the sweep runs in ``[E - span, E +
    liveness]``, and no proposal can arrive before E, so a period no longer
    than the shorter of the two bounds guarantees every market is bound before
    its verdict can exist. Measured cohort 2026-11-10: 3,559 markets in 36
    keyset pages, so sweeping every cycle would spend 18 s of a 60 s gate to
    re-read rows that cannot have changed meaning.
    """
    last = float(getattr(engine, "_near_close_ts", 0.0) or 0.0)
    span = close_span_sec(engine)
    tape = getattr(getattr(engine, "settlement_model", None), "tape", None)
    liveness = max(0.0, float(getattr(tape, "measured_liveness_sec", 0.0) or 0.0))
    period = min(span, liveness) if liveness > 0.0 else min(span, _RADAR_REFRESH_SEC)
    if not force and now - last < period:
        return
    engine._near_close_ts = now
    lo, hi = now - liveness, now + span
    keyset = getattr(engine.gamma, "markets_keyset", None)
    if keyset is None:
        return
    rows: dict[str, dict] = {}
    markets: dict[str, venue.Market] = {}
    started = time.monotonic()
    cursor: Optional[str] = None
    seen: set[str] = set()
    while True:
        try:
            payload = keyset(
                closed="false", after_cursor=cursor, limit=100,
                extra={"active": "true", "end_date_min": _iso(lo), "end_date_max": _iso(hi)},
            ) or {}
        except Exception:  # noqa: BLE001 - one failed page is retried next cycle
            # A failed page must not cost a whole sweep period: restore the old
            # stamp so the next radar cycle retries instead of waiting.
            engine._near_close_ts = last
            log.warning("near-close sweep page failed; keeping the last cohort",
                        exc_info=True)
            return
        for row in payload.get("markets") or []:
            cid = str(row.get("conditionId") or "").lower()
            if not cid:
                continue
            rows[cid] = row
            market = venue.parse_market(row)
            if market is not None:
                markets[market.condition_id] = market
        next_cursor = str(payload.get("next_cursor") or "")
        if not next_cursor or next_cursor in seen:
            break
        seen.add(next_cursor)
        cursor = next_cursor
    accepting = tuple(
        cid for cid, row in rows.items()
        if row.get("acceptingOrders") is not False and row.get("closed") is not True
    )
    with engine._radar_lock:
        previous = set(getattr(engine, "_near_close", {}) or {})
        engine._near_close = rows
        engine._near_close_accepting = accepting
        engine._verdict_rows.update(rows)
        engine._radar_markets.update(markets)
        # Drop the cohort rows and parsed markets that left the window, so a
        # long soak does not accumulate every market the venue has ever closed.
        wanted = set(getattr(engine, "_radar_want", ()) or ())
        stale = previous - set(rows)
        for cid in stale:
            if cid not in wanted:
                engine._verdict_rows.pop(cid, None)
                engine._radar_markets.pop(cid, None)
                engine._radar_fetched.pop(cid, None)
    elapsed = time.monotonic() - started
    signature = (len(rows), len(accepting))
    if signature != getattr(engine, "_near_close_signature", None):
        engine._near_close_signature = signature
        log.info(
            f"near-close staging: {len(rows)} market(s) with endDate in "
            f"[{_iso(lo)}, {_iso(hi)}] ({liveness / 3600.0:.2f}h back, "
            f"{span / 3600.0:.2f}h forward), {len(accepting)} still accepting "
            f"orders, swept in {elapsed:.2f}s"
        )
    # Bind the cohort immediately as well as on the cycle: an explicit
    # ``stage_near_close`` call (a test, a smoke) must leave the benchmark bound.
    bind_near_close(engine)


def bind_near_close(engine) -> int:
    """Attach the staged cohort's condition ids to the verdict benchmark.

    ``ProposePrice`` carries no condition id, so ``live_verdict_condition_ids``
    - and with it the whole funding census - is empty for an unbound proposal.
    Binding is the join that makes one visible, and it has to run on *every*
    radar cycle rather than once per sweep: a tape refresh rebuilds the index
    from the parquet every ``tape_refresh_sec`` (60 s) and only the overlay
    keeps its ids, so a binding applied to a parquet-sourced proposal is gone
    60 s later. The cohort is cached, so this pass costs no I/O.
    """
    tape = getattr(getattr(engine, "settlement_model", None), "tape", None)
    if tape is None:
        return 0
    with engine._radar_lock:
        rows = dict(getattr(engine, "_near_close", {}) or {})
    by_question = {
        normalize_question(str(row.get("question") or "")): cid
        for cid, row in rows.items()
        if str(row.get("question") or "").strip()
    }
    if not by_question:
        return 0
    try:
        bound = int(tape.bind_condition_ids(by_question) or 0)
    except Exception:  # noqa: BLE001 - binding is retried next cycle
        log.warning("near-close binding failed", exc_info=True)
        return 0
    if bound and bound != getattr(engine, "_near_close_bound", None):
        engine._near_close_bound = bound
        log.info(f"near-close binding: {bound} unbound proposal(s) attached to a "
                 f"condition id from {len(by_question)} staged question(s)")
    return bound


def near_close_snapshot(engine) -> tuple[dict[str, dict], tuple[str, ...]]:
    """The staged cohort's rows and the ids of those still accepting orders."""
    with engine._radar_lock:
        return dict(getattr(engine, "_near_close", {}) or {}), tuple(
            getattr(engine, "_near_close_accepting", ()) or ()
        )


def make_uma_poller(engine) -> Any:
    """Wire live UMA proposal polling when an RPC provider is configured.

    The block interval is measured from two consecutive heads; until it is
    measured the poller is not constructed, because an invented interval
    makes every proposal's age (and therefore its liveness) wrong.
    """
    try:
        from polymarketbot.strategy.onchain.core import ChainClient
        from polymarketbot.paper import uma_poll
    except Exception:  # noqa: BLE001 - optional chain dependency
        return None
    # The tape's header sequence is the primary measurement: 407
    # consecutive headers beat two live heads, and it is the same number
    # the on-chain flight model uses, so the poller and the CTF legs cannot
    # disagree about the protocol's block time.
    interval_s: Optional[float] = None
    try:
        from polymarketbot.strategy.onchain.ctf import load_measured_block_interval_s

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
