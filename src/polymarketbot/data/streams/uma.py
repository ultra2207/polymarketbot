"""UMA Optimistic Oracle lifecycle events on Polygon.

Polymarket resolves markets through a UMA Optimistic Oracle adapter. Gamma
says a market resolved; it does not say when a price was requested, who
proposed it, whether it was disputed, or how long settlement took. Those facts
exist only in the oracle's logs, and a dispute is the risk signal the strategy
reads first.

What is polled
--------------
``eth_getLogs`` over the configured addresses in ten-block windows. The
addresses are the ones verified in ``docs/data-collection.md``:

* ``0xee3afe347d5c74317041e2618c49534daf887c24`` - UMA Optimistic Oracle V2.
  The v2 adapter's immutable ``optimisticOracle()`` returns this address
  (``eth_call``, 2026-11-04).
* ``0xCB1822859cEF82Cd2Eb4E6276C7916e692995130`` - ``UmaConditionalTokensBinaryAdapter``
  v1.0.0, the old Polymarket adapter that still settles pre-cutover markets.
* ``0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74`` - ``UmaCtfAdapter`` v2.0.0,
  the current Polymarket adapter.

Decoding is delegated to `polymarketbot.data.uma.decode_log`, which is keyed by
``(contract, topic0)`` from each contract's verified ABI. Unknown addresses or
topics produce no row; an ABI mismatch must not look like a quiet contract.
The Optimistic Oracle is shared with other requesters, so its events are
recorded for every requester, not only Polymarket's adapters.

Resume policy
-------------
The last fully processed block is persisted to ``<root>/uma_state.json`` after
every window, so a kill loses at most one window. A poll starts at
``last_block + 1``; on a cold start it starts ``initial_lookback_blocks`` behind
the head. A gap wider than ``max_windows_per_poll`` windows is closed across
consecutive polls rather than in one unbounded burst. The anchor is a
*contiguous* claim: a window that still fails after retries leaves the anchor
at the previous window.

Rows are de-duplicated by ``(tx_hash, log_index)``. Recent keys persist with
the anchor so a reorg roll-back or a manually reset anchor cannot rewrite a
row that is already in the tape.

Row notes
---------
``ts`` is the block timestamp in epoch milliseconds, fetched once per distinct
event block. ``proposed_price``/``settlement_price``/amounts are decimal
strings: an int256 does not fit int64. ``condition_id`` and ``market_slug`` are
resolved by an exact normalized match between the market question parsed from
the ancillary data and the universe manifest; an unmapped market keeps them
null and keeps the question text the chain supplied. Nothing is guessed.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import orjson
import pyarrow as pa

from polymarketbot.chain import (
    RATE_LIMIT,
    ProviderPool,
    classify_rpc_error,
    jittered_delay,
    rpc_urls,
)
from polymarketbot.data.context import CollectorContext
from polymarketbot.data.streams.polygon import (
    HttpRpc,
    hex_int,
    parse_log,
    plan_windows,
)
from polymarketbot.data.uma import UMA_ADDRESSES, decode_log
from polymarketbot.net import install_doh_fallback

NAME = "uma"

DEFAULT_POLL_SECONDS = 60.0
DEFAULT_LOG_BLOCK_SPAN = 10  # Alchemy free tier eth_getLogs cap
DEFAULT_INITIAL_LOOKBACK_BLOCKS = 600
# One poll catches up at most this many windows; a deeper gap continues on the
# next poll instead of hammering the provider in one burst.
DEFAULT_MAX_WINDOWS_PER_POLL = 60
DEFAULT_WINDOW_PAUSE_S = 0.15
DEFAULT_RETRY_TOTAL = 3
# Bounded de-dupe memory persisted with the anchor.
SEEN_CAPACITY = 5_000
BLOCK_CACHE_CAPACITY = 4_096

MAX_RETRY_SLEEP_S = 30.0
BACKOFF_CAP_S = 300.0
STATE_VERSION = 1

_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
# Polymarket's UMA ancillary data starts ``q: title: <question>, description:``.
# The title itself may contain commas, so the terminator is the literal
# ``description:`` key, not the first comma.
_TITLE_RE = re.compile(r"^\s*q:\s*title:\s*(.*?),\s*description:", re.S)


# --------------------------------------------------------------------------- #
# Table
# --------------------------------------------------------------------------- #
UMA_KEYS = ("tx_hash", "log_index")

UMA_EVENTS = pa.schema(
    [
        ("recv_ts_ns", pa.int64()),
        ("recv_mono_ns", pa.int64()),
        # Block timestamp, epoch milliseconds. Never a receive-time substitute.
        ("ts", pa.int64()),
        ("block_number", pa.int64()),
        ("block_hash", pa.string()),
        ("tx_hash", pa.string()),
        ("tx_index", pa.int32()),
        ("log_index", pa.int32()),
        ("address", pa.string()),
        ("contract", pa.string()),
        ("event", pa.string()),
        ("requester", pa.string()),
        ("proposer", pa.string()),
        ("disputer", pa.string()),
        ("identifier", pa.string()),
        ("identifier_text", pa.string()),
        ("request_timestamp", pa.int64()),
        ("resolution_time", pa.int64()),
        ("expiry", pa.int64()),
        ("ancillary_data", pa.string()),
        ("ancillary_text", pa.string()),
        ("proposed_price", pa.string()),
        ("settlement_price", pa.string()),
        ("disputed", pa.bool_()),
        ("payouts", pa.string()),
        ("currency", pa.string()),
        ("reward", pa.string()),
        ("final_fee", pa.string()),
        ("proposal_bond", pa.string()),
        ("early_resolution", pa.bool_()),
        ("emergency_resolution", pa.bool_()),
        ("condition_id", pa.string()),
        ("market_slug", pa.string()),
        ("question", pa.string()),
        ("removed", pa.bool_()),
    ]
)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _positive(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def normalise_addresses(values: Iterable[Any]) -> tuple[str, ...]:
    """Validated, lower-cased, de-duplicated addresses in configured order."""
    out: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not _ADDRESS_RE.fullmatch(value):
            raise ValueError(f"uma address is not a 20-byte hex address: {value!r}")
        lowered = value.lower()
        if lowered not in out:
            out.append(lowered)
    return tuple(out)


@dataclass(frozen=True)
class UmaSettings:
    poll_seconds: float
    log_block_span: int
    addresses: tuple[str, ...]
    initial_lookback_blocks: int
    max_windows_per_poll: int
    window_pause_s: float
    retry_total: int
    state_path: Path
    dedupe_capacity: int = SEEN_CAPACITY

    @classmethod
    def from_section(cls, section: Mapping[str, Any], root: str | Path) -> "UmaSettings":
        raw = section.get("addresses")
        if isinstance(raw, Mapping):
            values = list(raw.values())
        elif isinstance(raw, str):
            values = [part for part in raw.split(",")]
        elif isinstance(raw, Sequence):
            values = list(raw)
        else:
            values = []
        addresses = normalise_addresses(values) if values else tuple(sorted(UMA_ADDRESSES))
        if not addresses:
            raise ValueError("uma.addresses is empty and no known address is bundled")

        state = str(section.get("state_path") or "").strip()
        state_path = Path(state) if state else Path(root) / "uma_state.json"

        return cls(
            poll_seconds=_positive(section.get("poll_seconds"), DEFAULT_POLL_SECONDS),
            log_block_span=max(1, int(_positive(section.get("log_block_span"), DEFAULT_LOG_BLOCK_SPAN))),
            addresses=addresses,
            initial_lookback_blocks=max(
                1,
                int(
                    _positive(
                        section.get("initial_lookback_blocks"),
                        DEFAULT_INITIAL_LOOKBACK_BLOCKS,
                    )
                ),
            ),
            max_windows_per_poll=max(
                1,
                int(
                    _positive(
                        section.get("max_windows_per_poll"),
                        DEFAULT_MAX_WINDOWS_PER_POLL,
                    )
                ),
            ),
            window_pause_s=max(0.0, float(section.get("window_pause_seconds", DEFAULT_WINDOW_PAUSE_S) or 0.0)),
            retry_total=max(
                1, int(_positive(section.get("retry_total"), DEFAULT_RETRY_TOTAL))
            ),
            state_path=state_path,
            dedupe_capacity=max(
                1, int(_positive(section.get("dedupe_capacity"), SEEN_CAPACITY))
            ),
        )


# --------------------------------------------------------------------------- #
# Persistent anchor
# --------------------------------------------------------------------------- #
@dataclass
class UmaState:
    """Contiguous cursor plus a bounded set of already-written log keys."""

    last_block: int | None = None
    seen: set[str] = field(default_factory=set)
    loaded_from: Path | None = None

    def accept(self, key: str, capacity: int) -> bool:
        if key in self.seen:
            return False
        self.seen.add(key)
        if len(self.seen) > capacity:
            # Sets are unordered; trimming an arbitrary slice is fine because
            # old keys matter far less than bounded state-file growth.
            for stale in list(self.seen)[: len(self.seen) - capacity]:
                self.seen.discard(stale)
        return True


def log_key(tx_hash: Any, log_index: Any) -> str | None:
    if not isinstance(tx_hash, str) or not tx_hash:
        return None
    if log_index is None:
        return None
    return f"{tx_hash.lower()}:{int(log_index)}"


def load_state(path: Path, log: Any) -> UmaState:
    state = UmaState(loaded_from=path)
    try:
        payload = orjson.loads(path.read_bytes())
    except FileNotFoundError:
        return state
    except Exception as exc:  # noqa: BLE001 - a corrupt anchor must not kill the stream
        log.warning(f"uma state {path} unreadable ({type(exc).__name__}: {exc}); starting cold")
        return state
    if not isinstance(payload, dict):
        log.warning(f"uma state {path} is not an object; starting cold")
        return state
    last = payload.get("last_block")
    if isinstance(last, int) and not isinstance(last, bool) and last >= 0:
        state.last_block = last
    seen = payload.get("seen")
    if isinstance(seen, list):
        state.seen = {str(item) for item in seen if isinstance(item, str)}
    return state


def save_state(path: Path, state: UmaState) -> None:
    """Atomic anchor write: a kill can lose a window, never corrupt the state."""
    payload = {
        "version": STATE_VERSION,
        "last_block": state.last_block,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "seen": sorted(state.seen),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(orjson.dumps(payload))
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Decoding and market mapping
# --------------------------------------------------------------------------- #
def ancillary_title(text: Any) -> str | None:
    """The market question from Polymarket's UMA ancillary text, if present."""
    if not isinstance(text, str):
        return None
    match = _TITLE_RE.match(text)
    if not match:
        return None
    title = match.group(1).strip()
    return title or None


def normalise_question(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    cleaned = " ".join(text.split()).casefold()
    return cleaned or None


class MarketIndex:
    """Exact question -> (condition_id, slug, question) from the universe.

    A question shared by more than one manifest market is ambiguous and maps to
    nothing: resolving it to the first row would be a guess.
    """

    def __init__(self, universe: Any) -> None:
        self._by_question: dict[str, tuple[str, str | None, str]] = {}
        self._ambiguous: set[str] = set()
        markets = getattr(universe, "markets", None)
        if not isinstance(markets, Mapping):
            return
        for market in markets.values():
            question = getattr(market, "question", None)
            condition_id = getattr(market, "condition_id", None)
            key = normalise_question(question)
            if not key or not condition_id:
                continue
            if key in self._by_question or key in self._ambiguous:
                self._ambiguous.add(key)
                self._by_question.pop(key, None)
                continue
            self._by_question[key] = (
                str(condition_id),
                getattr(market, "slug", None),
                str(question),
            )

    def lookup(self, ancillary_text: Any) -> tuple[str | None, str | None, str | None]:
        title = ancillary_title(ancillary_text)
        if title is None:
            return None, None, None
        hit = self._by_question.get(normalise_question(title) or "")
        if hit is not None:
            return hit
        # The chain supplied the question text; condition_id/slug stay null.
        return None, None, title


class BlockClock:
    """Block timestamps, fetched once per block and cached.

    Fetching happens through ``asyncio.to_thread`` from `poll_once`, never from
    the decoder, so the event loop is not blocked by a JSON-RPC round trip.
    """

    def __init__(self, rpc: Any) -> None:
        self.rpc = rpc
        self._cache: dict[int, int | None] = {}

    def cached(self, block_number: int | None) -> int | None:
        if block_number is None:
            return None
        return self._cache.get(block_number)

    async def ensure(self, blocks: Iterable[int]) -> None:
        for block_number in blocks:
            if block_number in self._cache:
                continue
            await asyncio.to_thread(self._fetch, block_number)

    def _fetch(self, block_number: int) -> None:
        for attempt in range(2):
            try:
                block = self.rpc.get_block(block_number)
            except Exception:  # noqa: BLE001 - a missing timestamp is null, not fatal
                block = None
            timestamp = hex_int(block.get("timestamp")) if isinstance(block, dict) else None
            if timestamp is not None:
                break
            if attempt == 0:
                time.sleep(0.2)
        if len(self._cache) >= BLOCK_CACHE_CAPACITY:
            self._cache.clear()
        self._cache[block_number] = timestamp * 1000 if timestamp is not None else None


def build_row(
    decoded: Mapping[str, Any],
    *,
    recv_ts_ns: int,
    recv_mono_ns: int,
    ts_ms: int | None,
    mapping: tuple[str | None, str | None, str | None],
) -> dict[str, Any]:
    """Fill the stream schema exactly; unknown fields are null, never zero."""
    row: dict[str, Any] = {name: None for name in UMA_EVENTS.names}
    for name in UMA_EVENTS.names:
        if name in decoded:
            row[name] = decoded[name]
    row["recv_ts_ns"] = recv_ts_ns
    row["recv_mono_ns"] = recv_mono_ns
    row["ts"] = ts_ms
    condition_id, slug, question = mapping
    row["condition_id"] = condition_id
    row["market_slug"] = slug
    row["question"] = question
    return row


def decode_window(
    raw_logs: Sequence[Any],
    *,
    recv_ts_ns: int,
    recv_mono_ns: int,
    clock: BlockClock,
    index: MarketIndex,
) -> tuple[list[dict[str, Any]], int]:
    """Decode one window's logs. Returns ``(rows, undecodable_logs)``."""
    rows: list[dict[str, Any]] = []
    undecodable = 0
    ordered = sorted(
        (log for log in raw_logs if isinstance(log, Mapping)),
        key=lambda log: (
            hex_int(log.get("blockNumber"), 0) or 0,
            hex_int(log.get("logIndex"), 0) or 0,
        ),
    )
    for raw in ordered:
        normalised = parse_log(raw, recv_ts_ns=recv_ts_ns, recv_mono_ns=recv_mono_ns)
        if normalised is None:
            undecodable += 1
            continue
        decoded = decode_log(normalised)
        if decoded is None:
            undecodable += 1
            continue
        rows.append(
            build_row(
                decoded,
                recv_ts_ns=recv_ts_ns,
                recv_mono_ns=recv_mono_ns,
                ts_ms=clock.cached(normalised.get("block_number")),
                mapping=index.lookup(decoded.get("ancillary_text")),
            )
        )
    return rows, undecodable


# --------------------------------------------------------------------------- #
# RPC
# --------------------------------------------------------------------------- #
def open_rpc() -> HttpRpc:
    """The collector's rotating HTTP provider pool, or a clear failure."""
    urls = [url for url in rpc_urls() if url]
    if not urls:
        raise RuntimeError("no Polygon RPC endpoint configured")
    pool = ProviderPool(urls)
    if not pool.health():
        raise RuntimeError("Polygon provider pool is empty")
    return HttpRpc(pool)


async def _fetch_window(
    rpc: Any,
    addresses: Sequence[str],
    low: int,
    high: int,
    *,
    retry_total: int,
    log: Any,
) -> list[dict[str, Any]]:
    """One `eth_getLogs` window with jittered retry.

    A 429 is handled inside `HttpRpc.call`, which cools the provider and
    rotates to the next one in the same pass; this retry covers the case where
    every provider was cooling at once.
    """
    last: Exception | None = None
    for attempt in range(max(1, retry_total)):
        try:
            result = await asyncio.to_thread(rpc.get_logs, addresses, low, high)
            return [row for row in result if isinstance(row, dict)]
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced
            last = exc
            kind = classify_rpc_error(exc)
            if attempt + 1 >= max(1, retry_total):
                break
            # Rate limits back off harder than a transient transport error.
            base = 2.0 if kind == RATE_LIMIT else 1.0
            delay = min(MAX_RETRY_SLEEP_S, jittered_delay(attempt, base=base))
            log.warning(
                f"eth_getLogs {low}..{high} failed ({kind}); retry "
                f"{attempt + 1}/{max(1, retry_total)} in {delay:.2f}s"
            )
            await asyncio.sleep(delay)
    assert last is not None
    raise last


# --------------------------------------------------------------------------- #
# Poll
# --------------------------------------------------------------------------- #
@dataclass
class PollStats:
    head: int | None = None
    start: int | None = None
    windows: int = 0
    logs: int = 0
    events: int = 0
    duplicates: int = 0
    undecodable: int = 0
    malformed: int = 0


async def poll_once(
    ctx: CollectorContext,
    settings: UmaSettings,
    state: UmaState,
    rpc: Any,
    *,
    clock: BlockClock | None = None,
    index: MarketIndex | None = None,
    save_anchor: bool = True,
) -> PollStats:
    """One bounded catch-up pass. Raises when a window cannot be fetched."""
    log = ctx.log(NAME)
    stats = PollStats()
    ctx.health.incr(NAME, "polls")

    head = await asyncio.to_thread(rpc.block_number)
    if head is None:
        raise RuntimeError("eth_blockNumber returned no head")
    stats.head = head

    if state.last_block is None:
        start = max(0, head - settings.initial_lookback_blocks + 1)
    else:
        start = state.last_block + 1
    stats.start = start

    if start > head:
        ctx.health.gauge(
            NAME, "last_block", int(state.last_block if state.last_block is not None else head)
        )
        return stats

    windows = plan_windows(start, head, settings.log_block_span)[
        : settings.max_windows_per_poll
    ]
    clock = clock or BlockClock(rpc)
    index = index or MarketIndex(ctx.universe)

    for low, high in windows:
        if ctx.stop.is_set():
            break
        raw_logs = await _fetch_window(
            rpc,
            settings.addresses,
            low,
            high,
            retry_total=settings.retry_total,
            log=log,
        )
        recv_ts_ns = time.time_ns()
        recv_mono_ns = time.perf_counter_ns()
        blocks = {
            block
            for block in (
                hex_int(entry.get("blockNumber"))
                for entry in raw_logs
                if isinstance(entry, Mapping)
            )
            if block is not None
        }
        if blocks:
            await clock.ensure(blocks)
        rows, undecodable = decode_window(
            raw_logs,
            recv_ts_ns=recv_ts_ns,
            recv_mono_ns=recv_mono_ns,
            clock=clock,
            index=index,
        )
        stats.windows += 1
        stats.logs += len(raw_logs)
        stats.undecodable += undecodable

        for row in rows:
            key = log_key(row.get("tx_hash"), row.get("log_index"))
            if key is None:
                stats.malformed += 1
                continue
            if not state.accept(key, settings.dedupe_capacity):
                stats.duplicates += 1
                continue
            ctx.emit("uma_events", row)
            stats.events += 1
            ctx.health.incr(NAME, "events")

        # The anchor moves only after the whole window is decoded and emitted.
        state.last_block = high
        if save_anchor:
            save_state(settings.state_path, state)

        if settings.window_pause_s > 0 and (low, high) != windows[-1]:
            await asyncio.sleep(settings.window_pause_s)

    if stats.duplicates:
        ctx.health.incr(NAME, "duplicates", stats.duplicates)
    if stats.undecodable:
        ctx.health.incr(NAME, "undecodable_logs", stats.undecodable)
    if stats.malformed:
        ctx.health.incr(NAME, "malformed_logs", stats.malformed)
    if state.last_block is not None:
        ctx.health.gauge(NAME, "last_block", int(state.last_block))
    return stats


# --------------------------------------------------------------------------- #
# Loop
# --------------------------------------------------------------------------- #
async def _wait_until(ctx: CollectorContext, deadline: float, last_poll_done: float) -> None:
    while not ctx.stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        ctx.health.gauge(NAME, "last_poll_age_s", max(0.0, time.monotonic() - last_poll_done))
        try:
            await asyncio.wait_for(ctx.stop.wait(), timeout=min(remaining, 30.0))
        except asyncio.TimeoutError:
            continue


async def run(ctx: CollectorContext) -> None:
    install_doh_fallback()  # idempotent; the orchestrator already did this
    cfg = ctx.section(NAME)
    log = ctx.log(NAME)
    ctx.register_table("uma_events", UMA_EVENTS)

    settings = UmaSettings.from_section(cfg, ctx.config.get("root") or "data/tape")
    state = load_state(settings.state_path, log)
    rpc = open_rpc()
    index = MarketIndex(ctx.universe)
    clock = BlockClock(rpc)

    log.info(
        f"starting: addresses={len(settings.addresses)} "
        f"poll={settings.poll_seconds:.0f}s span={settings.log_block_span} "
        f"state={settings.state_path} last_block={state.last_block}"
    )
    last_poll_done = time.monotonic()
    failures = 0

    try:
        while not ctx.stop.is_set():
            started = time.monotonic()
            try:
                stats = await poll_once(
                    ctx,
                    settings,
                    state,
                    rpc,
                    clock=clock,
                    index=MarketIndex(ctx.universe),
                )
                failures = 0
                log.info(
                    f"poll done: head={stats.head} start={stats.start} "
                    f"windows={stats.windows} logs={stats.logs} "
                    f"events={stats.events} duplicates={stats.duplicates} "
                    f"undecodable={stats.undecodable} "
                    f"elapsed={time.monotonic() - started:.1f}s"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one poll must not kill the stream
                failures += 1
                ctx.health.incr(NAME, "errors")
                log.warning(
                    f"poll failed ({type(exc).__name__}: {exc}); anchor stays at "
                    f"{state.last_block}; retry {failures}"
                )

            last_poll_done = time.monotonic()
            ctx.health.gauge(NAME, "last_poll_age_s", 0.0)
            if failures == 0:
                pause = settings.poll_seconds
            else:
                # Jittered backoff: a provider already refusing us must not be
                # hit on a metronome.
                pause = min(
                    BACKOFF_CAP_S,
                    jittered_delay(
                        failures - 1,
                        base=min(30.0, settings.poll_seconds / 2),
                    ),
                )
            await _wait_until(ctx, last_poll_done + pause, last_poll_done)
    finally:
        rpc.close()
