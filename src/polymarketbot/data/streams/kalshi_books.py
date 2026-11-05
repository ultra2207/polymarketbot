"""Kalshi book snapshots from ``/markets/orderbooks``.

``/markets/orderbooks`` takes a *repeated* ``tickers=`` parameter
(``?tickers=A&tickers=B``). A comma-joined value is parsed as one ticker and
comes back empty, so the query string is built with :func:`urlencode` over a
list of pairs rather than a dict.

The venue returns **bids only** on both sides. The ask on one side is the
complement of the best bid on the other:

    yes_ask = 1 - no_best_bid          no_ask = 1 - yes_best_bid

The tape stores the derived asks (and the mid/spread built from them) beside
the raw bid ladders, so a consumer never has to repeat that arithmetic. A
crossed book (``yes_best_bid + no_best_bid > 1``) keeps its negative spread:
that is the venue's state, not a parse error.

Prices arrive as dollar strings (``"0.53"``) and sizes as contract-count
strings (``"1000.00"``). Levels are sorted best-first here because the payload
order is not documented.

Emitting policy: a snapshot identical to the previous one for the same ticker
is dropped, except once per ``heartbeat_seconds`` (60 s). A market that has not
traded or re-quoted still leaves a row, so "quiet" and "stream dead" stay
distinguishable in the tape.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlencode

import orjson

from polymarketbot.data.context import CollectorContext
from polymarketbot.data.schema import KALSHI_BOOKS
from polymarketbot.data.streams.kalshi import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_BACKOFF_SECONDS,
    KalshiClient,
    MappingCache,
    plan_ticker_batches,
)

NAME = "kalshi_books"

OrderbookPath = "/markets/orderbooks"

#: Measured: 100 tickers is the endpoint cap (101 -> 400, 200 -> 414).
DEFAULT_BATCH_SIZE = 100
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_HEARTBEAT_SECONDS = 60.0

#: Cumulative resting size within this many cents of the best bid, per side.
DEPTH_BANDS_CENTS = (1, 2, 5, 10)


# ------------------------------------------------------------------ parsing
def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN -> None


def _levels(raw: Any) -> list[tuple[float, float]]:
    """``[[price, size], ...]`` -> ``[(price, size), ...]``, best bid first.

    Levels with an unparseable price/size or a non-positive size are dropped:
    a zero-size level carries no liquidity and would only pollute the bands.
    """
    levels: list[tuple[float, float]] = []
    if not isinstance(raw, (list, tuple)):
        return levels
    for level in raw:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = _num(level[0])
        size = _num(level[1])
        if price is None or size is None or size <= 0:
            continue
        levels.append((price, size))
    levels.sort(key=lambda item: item[0], reverse=True)
    return levels


def _band_size(
    levels: Sequence[tuple[float, float]], best: float | None, cents: float
) -> float | None:
    """Resting size within ``cents`` of the best bid (inclusive of the best)."""
    if best is None:
        return None
    floor = best - float(cents) / 100.0
    return float(sum(size for price, size in levels if price >= floor))


def _levels_json(levels: Sequence[tuple[float, float]]) -> str:
    return orjson.dumps([[price, size] for price, size in levels]).decode()


def parse_orderbook(
    entry: dict[str, Any], recv_ts_ns: int, recv_mono_ns: int
) -> dict[str, Any] | None:
    """One ``orderbooks[]`` entry -> one tape row (or ``None`` without a ticker)."""
    ticker = entry.get("ticker")
    if not ticker:
        return None
    book = entry.get("orderbook_fp")
    if not isinstance(book, dict):
        book = {}
    yes_levels = _levels(book.get("yes_dollars"))
    no_levels = _levels(book.get("no_dollars"))

    yes_best_bid = yes_levels[0][0] if yes_levels else None
    no_best_bid = no_levels[0][0] if no_levels else None
    yes_best_ask = round(1.0 - no_best_bid, 6) if no_best_bid is not None else None
    no_best_ask = round(1.0 - yes_best_bid, 6) if yes_best_bid is not None else None
    if yes_best_bid is not None and yes_best_ask is not None:
        mid = round((yes_best_bid + yes_best_ask) / 2.0, 6)
        spread = round(yes_best_ask - yes_best_bid, 6)
    else:
        mid = None
        spread = None

    row: dict[str, Any] = {
        "recv_ts_ns": int(recv_ts_ns),
        "recv_mono_ns": int(recv_mono_ns),
        "ticker": str(ticker),
        "yes_best_bid": yes_best_bid,
        "yes_best_ask": yes_best_ask,
        "no_best_bid": no_best_bid,
        "no_best_ask": no_best_ask,
        "mid": mid,
        "spread": spread,
        "yes_bid_size": yes_levels[0][1] if yes_levels else None,
        "no_bid_size": no_levels[0][1] if no_levels else None,
        "yes_bids": _levels_json(yes_levels),
        "no_bids": _levels_json(no_levels),
        "raw_json": orjson.dumps(entry).decode(),
    }
    for cents in DEPTH_BANDS_CENTS:
        row[f"yes_bid_size_{cents}c"] = _band_size(yes_levels, yes_best_bid, cents)
        row[f"no_bid_size_{cents}c"] = _band_size(no_levels, no_best_bid, cents)
    return row


def parse_orderbooks_payload(
    payload: dict[str, Any], recv_ts_ns: int, recv_mono_ns: int
) -> tuple[list[dict[str, Any]], int]:
    """Split one response into rows plus the count of empty books.

    An "empty book" is an entry the venue returned with both ladders empty: a
    real market with no resting orders, which is worth counting separately
    from a ticker the venue did not return at all.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("orderbooks"), list):
        raise ValueError(f"unexpected Kalshi orderbooks payload: {type(payload).__name__}")
    rows: list[dict[str, Any]] = []
    empty = 0
    for entry in payload["orderbooks"]:
        if not isinstance(entry, dict):
            continue
        row = parse_orderbook(entry, recv_ts_ns, recv_mono_ns)
        if row is None:
            continue
        if row["yes_bids"] == "[]" and row["no_bids"] == "[]":
            empty += 1
        rows.append(row)
    return rows, empty


# ---------------------------------------------------------- change detection
class BookChangeDetector:
    """Drop re-sent snapshots, but never stay silent for longer than a heartbeat.

    The signature is the two sorted ladders: every derived field (best bid,
    derived ask, mid, spread, band sizes) is a function of them, so equal
    ladders mean the venue has nothing new to say.
    """

    def __init__(self, heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS) -> None:
        self.heartbeat_seconds = max(0.0, float(heartbeat_seconds))
        self._last: dict[str, tuple[tuple[str, str], float]] = {}

    def select(self, rows: Sequence[dict[str, Any]], now_mono: float) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for row in rows:
            ticker = row.get("ticker")
            if not ticker:
                continue
            signature = (str(row.get("yes_bids") or ""), str(row.get("no_bids") or ""))
            previous = self._last.get(ticker)
            if previous is not None:
                changed = signature != previous[0]
                due = (now_mono - previous[1]) >= self.heartbeat_seconds
                if not changed and not due:
                    continue
            self._last[str(ticker)] = (signature, now_mono)
            selected.append(row)
        return selected

    def tracked(self) -> int:
        return len(self._last)

    def forget(self, active: Sequence[str]) -> None:
        """Drop state for tickers that left the mapping."""
        keep = set(active)
        for ticker in [t for t in self._last if t not in keep]:
            del self._last[ticker]


# ---------------------------------------------------------------- transport
def orderbook_path(tickers: Sequence[str]) -> str:
    """``/markets/orderbooks`` with one ``tickers=`` pair per ticker."""
    query = urlencode([("tickers", str(ticker)) for ticker in tickers])
    return f"{OrderbookPath}?{query}"


@dataclass
class BatchOutcome:
    """What one ``tickers=`` batch produced; failures stay local to the batch."""

    tickers: tuple[str, ...]
    rows: list[dict[str, Any]] = field(default_factory=list)
    empty_books: int = 0
    error: str | None = None
    status: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _status_of(exc: BaseException) -> int | None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return int(status) if isinstance(status, int) else None


def fetch_batch(client: Any, batch: Sequence[str]) -> BatchOutcome:
    """One request, isolated: the caller keeps going whatever happens here."""
    tickers = tuple(str(ticker) for ticker in batch)
    try:
        payload = client.get_json(orderbook_path(tickers))
    except Exception as exc:  # noqa: BLE001 - a batch failure must not stop the poll
        return BatchOutcome(tickers, error=f"{type(exc).__name__}: {exc}", status=_status_of(exc))
    recv_ts_ns = time.time_ns()
    recv_mono_ns = time.perf_counter_ns()
    try:
        rows, empty = parse_orderbooks_payload(payload, recv_ts_ns, recv_mono_ns)
    except ValueError as exc:
        return BatchOutcome(tickers, error=str(exc))
    return BatchOutcome(tickers, rows, empty)


def poll_books(client: Any, batches: Sequence[Sequence[str]]) -> list[BatchOutcome]:
    """Every batch, sequentially; a broken batch does not skip its siblings."""
    return [fetch_batch(client, batch) for batch in batches]


# ----------------------------------------------------------------------- run
async def run(ctx: CollectorContext) -> None:
    cfg = ctx.section(NAME)
    log = ctx.log(NAME)
    ctx.register_table("kalshi_books", KALSHI_BOOKS)

    base_url = str(cfg.get("rest_url", "https://api.elections.kalshi.com/trade-api/v2"))
    poll_seconds = max(1.0, float(cfg.get("poll_seconds", DEFAULT_POLL_SECONDS)))
    batch_size = max(1, int(cfg.get("batch_size", DEFAULT_BATCH_SIZE)))
    heartbeat_seconds = float(cfg.get("heartbeat_seconds", DEFAULT_HEARTBEAT_SECONDS))
    max_backoff = min(MAX_BACKOFF_SECONDS, float(cfg.get("max_backoff_seconds", 60.0)))
    mapping_path = Path(str(cfg.get("mapping_path", "data/derived/kalshi_mapping.parquet")))

    mapping = MappingCache(
        mapping_path, refresh_seconds=float(cfg.get("mapping_refresh_seconds", 900.0))
    )
    detector = BookChangeDetector(heartbeat_seconds=heartbeat_seconds)

    client = KalshiClient(
        base_url,
        timeout=float(cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        retry_total=int(cfg.get("retry_total", 3)),
        backoff_base=float(cfg.get("backoff_base_seconds", 0.5)),
        backoff_cap=max_backoff,
    )

    started = time.monotonic()
    last_success: float | None = None
    failure_streak = 0
    next_poll = 0.0
    idle_warned = False

    log.info(
        f"kalshi books: {base_url} every {poll_seconds:.0f}s, "
        f"{batch_size} tickers/request, heartbeat {heartbeat_seconds:.0f}s"
    )

    try:
        while not ctx.stop.is_set():
            now = time.monotonic()
            if now >= next_poll:
                tickers = mapping.get()
                if not tickers:
                    if not idle_warned:
                        log.warning(
                            f"kalshi books: no mapped tickers at {mapping_path}; idling"
                        )
                        idle_warned = True
                    ctx.health.gauge(NAME, "mapping_tickers", 0.0)
                    next_poll = now + max(poll_seconds, 30.0)
                else:
                    idle_warned = False
                    batches = plan_ticker_batches(tickers, batch_size)
                    detector.forget(tickers)
                    try:
                        outcomes = await asyncio.to_thread(poll_books, client, batches)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - keep the stream alive
                        # poll_books already isolates batches; this is a guard
                        # against anything that escapes that isolation.
                        failure_streak += 1
                        ctx.health.incr(NAME, "errors")
                        log.warning(f"kalshi books poll failed: {type(exc).__name__}: {exc}")
                    else:
                        failed = [outcome for outcome in outcomes if not outcome.ok]
                        if failed:
                            failure_streak += 1
                            ctx.health.incr(NAME, "errors", len(failed))
                            log.warning(
                                f"kalshi books: {len(failed)}/{len(outcomes)} batches failed "
                                f"({failure_streak} in a row); first: {failed[0].error}"
                            )
                        else:
                            failure_streak = 0
                            last_success = time.monotonic()

                        rows: list[dict[str, Any]] = []
                        empty_books = 0
                        for outcome in outcomes:
                            rows.extend(outcome.rows)
                            empty_books += outcome.empty_books
                        selected = detector.select(rows, time.perf_counter())
                        for row in selected:
                            ctx.emit("kalshi_books", row)
                        ctx.health.incr(NAME, "requests", len(outcomes))
                        ctx.health.incr(NAME, "books_returned", len(rows))
                        ctx.health.incr(NAME, "empty_books", empty_books)
                        ctx.health.incr(NAME, "rows_emitted", len(selected))
                        ctx.health.gauge(NAME, "mapping_tickers", float(len(tickers)))
                        ctx.health.gauge(NAME, "books_tracked", float(detector.tracked()))
                        log.debug(
                            f"kalshi books: {len(rows)} books, {empty_books} empty, "
                            f"{len(selected)} rows, edge={client._edge}"
                        )

                    # 429 doubles the cadence for the *next* poll; no batch
                    # splitting, the whole cycle just slows down.
                    interval = poll_seconds * (2 ** min(failure_streak, 5))
                    next_poll = time.monotonic() + min(max_backoff, interval)

            since = last_success if last_success is not None else started
            age = time.monotonic() - since
            ctx.health.gauge(NAME, "last_success_age_s", age)
            ctx.health.gauge(NAME, "retries", float(client.stats.retries))
            ctx.health.gauge(NAME, "edge_resets", float(client.stats.resets))

            wait = next_poll - time.monotonic()
            try:
                await asyncio.wait_for(ctx.stop.wait(), timeout=max(0.05, wait))
            except asyncio.TimeoutError:
                pass
    finally:
        client.close()
