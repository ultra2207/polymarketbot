"""Polygon on-chain stream: Polymarket contract logs plus block gas.

Transport
---------
Primary is one WebSocket JSON-RPC session carrying two `eth_subscribe`
subscriptions: `logs` filtered to the CTF / UMA contracts, and `newHeads`.
Polygon PoS produces a block roughly every 2 s, so 60 s of silence means the
socket is dead and the session is torn down and rebuilt.

The socket is dialled against an ordered provider list (Alchemy, then the
keyless fallbacks from `polymarketbot.chain.rpc_urls()`). A provider that
answers a handshake with HTTP 429 is put on a cooldown and the next one is
dialled; on 2026-11-04 the stream retried one throttled Alchemy URL 2x/min for
four hours because nothing rotated.

Fallback is HTTP: `eth_getLogs` over `log_block_span` windows for the same
addresses, and `eth_getBlockByNumber` for gas. The Alchemy free tier caps
`eth_getLogs` at ten blocks, hence the configured span. Gas is a *sample*: one
header for the newest block per `gas_poll_seconds`, never one per missed block,
because a header per 1.5 s block (~40 calls/min) was half of what tripped the
free tier. The fallback runs while the WebSocket is down and once immediately
after a reconnect, so a dropped socket does not silently skip blocks.

Resume policy: the first block processed is
``max(last_block + 1, head - span + 1)``. History older than one span is *not*
backfilled; that is logged rather than papered over, because a gap nobody can
see is a gap that corrupts an analysis later.

Row notes
---------
``newHeads`` carries the block header only, so ``tx_count`` is null on the live
path; the HTTP path reads the header's transaction-hash list and fills it. Logs
with ``removed=true`` are emitted as well: a reorg that is invisible in the tape
is worse than a duplicate row.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import orjson
import requests
from websockets.asyncio.client import connect

from polymarketbot.chain import (
    RATE_LIMIT,
    ProviderPool,
    classify_rpc_error,
    jittered_delay,
    mask_url,
    rpc_url,
    rpc_urls,
    websocket_url,
)
from polymarketbot.data.context import CollectorContext
from polymarketbot.data.schema import POLYGON_GAS, POLYGON_LOGS

NAME = "polygon"

# A healthy chain sends a header every ~2 s. Silence this long is a dead socket,
# not a quiet chain.
STALE_SECONDS = 60.0
OPEN_TIMEOUT_S = 15.0
RPC_TIMEOUT_S = 20.0
MAX_BACKOFF_S = 30.0
# How long one catch-up invocation may spend closing a gap before yielding to
# the live socket, and how often the background loop retries.
CATCHUP_BUDGET_S = 90.0
CATCHUP_POLL_S = 30.0

# How long to poll over HTTP after a 429 before dialling the next provider's
# socket. Long enough to fill the gap, short enough that rotation is immediate.
ROTATE_POLL_S = 5.0

# A contract address is exactly 20 bytes; anything else would filter nothing and
# look exactly like a quiet market in the data.
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")


# --------------------------------------------------------------------------- #
# Pure parsers and planners (unit-tested without a network)
# --------------------------------------------------------------------------- #
def hex_int(value: Any, default: int | None = None) -> int | None:
    """Parse a JSON-RPC quantity such as ``"0x1a"`` into an int.

    Nodes normally return hex strings, but web3-style clients and some proxies
    return ints. Anything unusable yields ``default`` instead of raising: one
    malformed log must not kill the stream.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            if text[:2].lower() == "0x":
                return int(text[2:], 16) if len(text) > 2 else default
            return int(text, 10)
        except ValueError:
            return default
    return default


def _topic(topics: Sequence[Any], index: int) -> str | None:
    if index < len(topics):
        value = topics[index]
        if isinstance(value, str) and value:
            return value
    return None


def parse_log(
    raw: Any,
    *,
    recv_ts_ns: int | None = None,
    recv_mono_ns: int | None = None,
) -> dict[str, Any] | None:
    """One `eth_getLogs` / `logs` subscription object -> a `polygon_logs` row.

    Returns None when the object carries no block number, because a log that
    cannot be placed on the chain cannot be reasoned about.
    """
    if not isinstance(raw, dict):
        return None
    block_number = hex_int(raw.get("blockNumber"))
    if block_number is None:
        return None
    topics = raw.get("topics")
    if not isinstance(topics, list):
        topics = []
    data = raw.get("data")
    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "block_number": block_number,
        "block_hash": raw.get("blockHash") if isinstance(raw.get("blockHash"), str) else None,
        "tx_hash": (
            raw.get("transactionHash")
            if isinstance(raw.get("transactionHash"), str)
            else None
        ),
        "tx_index": hex_int(raw.get("transactionIndex")),
        "log_index": hex_int(raw.get("logIndex")),
        "address": raw.get("address") if isinstance(raw.get("address"), str) else None,
        "topic0": _topic(topics, 0),
        "topic1": _topic(topics, 1),
        "topic2": _topic(topics, 2),
        "topic3": _topic(topics, 3),
        "data": data if isinstance(data, str) else None,
        "removed": bool(raw.get("removed", False)),
    }


def parse_block_header(
    raw: Any,
    *,
    recv_ts_ns: int | None = None,
    recv_mono_ns: int | None = None,
    tx_count: int | None = None,
) -> dict[str, Any] | None:
    """A block header (or full block) -> a `polygon_gas` row.

    `newHeads` omits the transaction list, so `tx_count` is taken from the
    caller when it already knows it; an absent list stays null rather than 0 --
    "no transactions observed" and "no transaction list in this payload" are
    different facts.
    """
    if not isinstance(raw, dict):
        return None
    block_number = hex_int(raw.get("number"))
    if block_number is None:
        return None
    base_fee = hex_int(raw.get("baseFeePerGas"))
    timestamp_s = hex_int(raw.get("timestamp"))
    transactions = raw.get("transactions")
    if tx_count is None and isinstance(transactions, list):
        tx_count = len(transactions)
    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "block_number": block_number,
        "block_hash": raw.get("hash") if isinstance(raw.get("hash"), str) else None,
        "base_fee_gwei": (base_fee / 1e9) if base_fee is not None else None,
        "gas_used": hex_int(raw.get("gasUsed")),
        "gas_limit": hex_int(raw.get("gasLimit")),
        "timestamp_ms": timestamp_s * 1000 if timestamp_s is not None else None,
        "tx_count": tx_count,
    }


def plan_windows(start_block: int, end_block: int, span: int) -> list[tuple[int, int]]:
    """Inclusive block windows of at most `span` blocks covering [start, end].

    The provider rejects ranges wider than its cap, so the window size is a
    property of the endpoint and is applied here once instead of at every call.
    """
    span = max(1, int(span))
    start_block = int(start_block)
    end_block = int(end_block)
    if end_block < start_block:
        return []
    windows: list[tuple[int, int]] = []
    low = start_block
    while low <= end_block:
        high = min(low + span - 1, end_block)
        windows.append((low, high))
        low = high + 1
    return windows


def wss_from_http(url: str) -> str:
    """Derive the WebSocket endpoint from an HTTP JSON-RPC URL."""
    return websocket_url(url)


def mask_endpoint(url: str) -> str:
    """`mask_url` for ws/wss endpoints, keeping their scheme visible.

    `mask_url` only matches http(s); the key is in the path, so swapping the
    scheme for the call is enough and the log still says `wss://`.
    """
    scheme, separator, rest = (url or "").partition("://")
    if not separator:
        return mask_url(url)
    masked = mask_url(f"https://{rest}")
    return f"{scheme}{separator}{masked.split('://', 1)[1]}"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PolygonSettings:
    rpc_url: str
    ws_url: str
    poll_seconds: float
    log_block_span: int
    gas_poll_seconds: float
    addresses: tuple[str, ...]
    fallback_urls: tuple[str, ...] = ()

    @property
    def http_urls(self) -> list[str]:
        out = [self.rpc_url]
        for url in self.fallback_urls:
            if url and url not in out:
                out.append(url)
        return out

    @classmethod
    def from_section(
        cls, section: dict[str, Any], *, fallbacks: Sequence[str] | None = None
    ) -> "PolygonSettings":
        """Build settings from `ctx.section("polygon")`.

        `fallbacks` defaults to the read-only endpoints from
        `polymarketbot.chain.rpc_urls()`; tests pass a list to stay offline.
        """
        configured = str(section.get("rpc_url") or "").strip()
        if configured:
            primary = configured.rstrip("/")
        else:
            primary, _keyed = rpc_url()
        configured_ws = str(section.get("ws_url") or "").strip()
        ws_url = configured_ws.rstrip("/") if configured_ws else wss_from_http(primary)

        addresses = section.get("addresses") or {}
        if not isinstance(addresses, dict):
            raise ValueError("polygon.addresses must be a mapping of name -> address")
        cleaned: list[str] = []
        # The old binary adapter (uma_v1_adapter) still settles markets opened
        # before the v2 cutover, so its logs are part of the resolution record.
        for key in ("ctf", "uma_oo", "uma_v1_adapter", "uma_adapter"):
            value = str(addresses.get(key) or "").strip()
            if not value:
                continue
            if not _ADDRESS_RE.fullmatch(value):
                raise ValueError(f"polygon.addresses.{key} is not a 20-byte hex address")
            if value not in cleaned:
                cleaned.append(value)
        if not cleaned:
            raise ValueError("polygon.addresses has no usable contract addresses")

        extra = rpc_urls() if fallbacks is None else list(fallbacks)
        normalised: list[str] = []
        for url in extra:
            endpoint = str(url or "").strip().rstrip("/")
            if endpoint and endpoint != primary and endpoint not in normalised:
                normalised.append(endpoint)
        return cls(
            rpc_url=primary,
            ws_url=ws_url,
            poll_seconds=max(1.0, float(section.get("poll_seconds", 10) or 10)),
            log_block_span=max(1, int(section.get("log_block_span", 10) or 10)),
            gas_poll_seconds=max(1.0, float(section.get("gas_poll_seconds", 10) or 10)),
            addresses=tuple(cleaned),
            fallback_urls=tuple(normalised),
        )


def provider_pool(settings: PolygonSettings) -> ProviderPool:
    """Ordered providers for this stream, primary first.

    `settings.ws_url` is an explicit endpoint, so it is pinned to the primary
    provider instead of being re-derived: a config that names a socket must be
    honoured even when its host is not the HTTP host.
    """
    pool = ProviderPool(settings.http_urls)
    pool.set_wss(settings.rpc_url, settings.ws_url)
    return pool


# --------------------------------------------------------------------------- #
# HTTP JSON-RPC with endpoint failover
# --------------------------------------------------------------------------- #
class HttpRpc:
    """Blocking JSON-RPC over HTTP, rotating providers and resting them on a 429."""

    def __init__(self, pool: ProviderPool, *, timeout_s: float = RPC_TIMEOUT_S) -> None:
        if not pool.health():
            raise ValueError("HttpRpc needs at least one endpoint")
        self.pool = pool
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "User-Agent": "polymarketbot-research/0.1",
            }
        )

    def close(self) -> None:
        self.session.close()

    def _candidates(self) -> list[str]:
        """Healthy providers first; if all are cooling, the soonest to recover."""
        ready = [h.url for h in self.pool.available()]
        if ready:
            return ready
        candidate = self.pool.next_ready()
        return [candidate.url] if candidate is not None else []

    def call(self, method: str, params: list[Any]) -> Any:
        """One JSON-RPC call, rotating providers. Raises when every one fails.

        A 429 costs that provider a cooldown but not the call: the next provider
        is tried in the same pass, which is what keeps a rate-limited primary
        from stalling the stream.
        """
        errors: list[str] = []
        urls = self._candidates()
        if not urls:
            raise RuntimeError(f"{method}: no RPC endpoint configured")
        for url in urls:
            body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            try:
                response = self.session.post(url, json=body, timeout=self.timeout_s)
                if response.status_code == 429:
                    cooled = self.pool.mark_rate_limited(url)
                    errors.append(
                        f"{mask_url(url)}: HTTP 429 rate limited, cooled {cooled:.0f}s"
                    )
                    continue
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and payload.get("error"):
                    raise RuntimeError(f"provider error: {payload['error']}")
                if not isinstance(payload, dict) or "result" not in payload:
                    raise RuntimeError("malformed JSON-RPC response")
                self.pool.mark_ok(url)
                return payload["result"]
            except Exception as exc:  # noqa: BLE001 - failover over any transport error
                self.pool.mark_error(url, classify_rpc_error(exc))
                detail = str(exc).replace(url, mask_url(url))[:160]
                errors.append(f"{mask_url(url)}: {type(exc).__name__}: {detail}")
        raise RuntimeError(f"{method} failed on {len(urls)} endpoint(s): {errors}")

    def get_logs(self, addresses: Sequence[str], low: int, high: int) -> list[dict]:
        result = self.call(
            "eth_getLogs",
            [{"fromBlock": hex(low), "toBlock": hex(high), "address": list(addresses)}],
        )
        return [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []

    def get_block(self, number: int) -> dict | None:
        result = self.call("eth_getBlockByNumber", [hex(int(number)), False])
        return result if isinstance(result, dict) else None

    def block_number(self) -> int | None:
        return hex_int(self.call("eth_blockNumber", []))


# --------------------------------------------------------------------------- #
# Stream state
# --------------------------------------------------------------------------- #
@dataclass
class StreamState:
    log_block: int | None = None  # highest block emitted (live or backfill)
    backfill_block: int | None = None  # highest block whose logs are contiguously covered
    head_block: int | None = None  # highest block whose gas row was emitted
    head_hash: str | None = None
    head_wall: float | None = None  # monotonic arrival time of the last head
    gas_wall: float | None = None  # monotonic time of the last gas sample
    logs: int = 0
    blocks: int = 0
    reconnects: int = 0
    backfill_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _emit_log(
    ctx: CollectorContext,
    state: StreamState,
    raw: Any,
    ts: int,
    mono: int,
    *,
    force: bool = False,
    seen: set[tuple[int, int]] | None = None,
) -> bool:
    row = parse_log(raw, recv_ts_ns=ts, recv_mono_ns=mono)
    if row is None:
        return False
    block = row["block_number"]
    removed = bool(row["removed"])
    if seen is not None:
        key = (int(block), int(row["log_index"]))
        if key in seen:
            return False
        seen.add(key)
    # A reorg notification is always kept and rolls the watermark back so the
    # replacement logs for that height are not mistaken for duplicates.
    if not removed and not force and state.log_block is not None and block <= state.log_block:
        return False
    if removed:
        if state.log_block is not None:
            state.log_block = min(state.log_block, block - 1)
        if state.backfill_block is not None:
            state.backfill_block = min(state.backfill_block, block - 1)
    ctx.emit("polygon_logs", row)
    ctx.health.incr(NAME, "logs")
    state.logs += 1
    if not removed:
        state.log_block = block if state.log_block is None else max(state.log_block, block)
    return True


def _emit_header(
    ctx: CollectorContext,
    state: StreamState,
    raw: Any,
    ts: int,
    mono: int,
    *,
    warn_gap: bool = True,
) -> bool:
    row = parse_block_header(raw, recv_ts_ns=ts, recv_mono_ns=mono)
    if row is None:
        return False
    block = row["block_number"]
    block_hash = row["block_hash"]
    if state.head_block is not None:
        if block < state.head_block:
            return False
        if block == state.head_block and block_hash == state.head_hash:
            return False
        if block == state.head_block and block_hash != state.head_hash:
            ctx.health.incr(NAME, "reorgs")
            ctx.log(NAME).warning(
                f"reorg at block {block}: {state.head_hash} -> {block_hash}"
            )
        elif state.head_block + 1 < block and warn_gap:
            ctx.log(NAME).warning(
                f"block gap {state.head_block} -> {block} ({block - state.head_block - 1} missed)"
            )
    ctx.emit("polygon_gas", row)
    ctx.health.incr(NAME, "blocks")
    state.blocks += 1
    state.head_block = block
    state.head_hash = block_hash
    state.head_wall = time.monotonic()
    return True


# --------------------------------------------------------------------------- #
# Catch-up / fallback over HTTP
# --------------------------------------------------------------------------- #
def _last_taped_block(root: Path) -> int | None:
    """Highest block already committed to the tape, used as the restart anchor.

    Without it the first catch-up only looks one span back, so every collector
    restart leaves a small permanent hole between the last run and the new head.
    """
    try:
        import duckdb

        path = Path(root) / "polygon_logs" / "**" / "*.parquet"
        row = duckdb.connect().execute(
            "SELECT MAX(block_number) FROM read_parquet(?)", [str(path)]
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:  # noqa: BLE001 - a missing or partial tape must not stop the stream
        return None


async def _catch_up(
    ctx: CollectorContext,
    settings: PolygonSettings,
    state: StreamState,
    rpc: HttpRpc,
    *,
    gas: bool = True,
    budget_s: float = CATCHUP_BUDGET_S,
) -> None:
    """Close the log gap from the contiguous cursor to head, in bounded windows.

    The old implementation floored the start at ``head - span + 1``, so every
    outage longer than one 10-block window left a permanent hole: the live
    socket then advanced the watermark past it and no later run could see it.
    Logs are fetched for every missed window until head, with a 90 s budget per
    invocation; if the budget expires, the cursor stays at the last fully
    covered block and the background loop resumes there. Gas is a *sample*: one
    header for the newest head, at most once per `gas_poll_seconds`.
    """
    log = ctx.log(NAME)
    async with state.backfill_lock:
        head = await asyncio.to_thread(rpc.block_number)
        if head is None:
            raise RuntimeError("eth_blockNumber returned no block")
        span = settings.log_block_span
        start = (
            state.backfill_block + 1
            if state.backfill_block is not None
            else max(0, head - span + 1)
        )
        if start <= head:
            deadline = time.monotonic() + max(0.0, float(budget_s))
            seen: set[tuple[int, int]] = set()
            covered = 0
            for low, high in plan_windows(start, head, span):
                if time.monotonic() > deadline:
                    ctx.health.incr(NAME, "backfill_budget_exceeded")
                    log.warning(
                        f"backfill paused at block {state.backfill_block} of {head} "
                        f"({head - (state.backfill_block or 0)} blocks remaining)"
                    )
                    break
                rows = await asyncio.to_thread(
                    rpc.get_logs, settings.addresses, low, high
                )
                ts, mono = time.time_ns(), time.perf_counter_ns()
                for raw in rows:
                    if _emit_log(ctx, state, raw, ts, mono, force=True, seen=seen):
                        covered += 1
                state.backfill_block = high
                if state.log_block is None or high > state.log_block:
                    state.log_block = high
            if start < head:
                log.info(
                    f"backfill {start}..{state.backfill_block} ({covered} logs), "
                    f"gap {max(0, head - (state.backfill_block or 0))} blocks"
                )
        ctx.health.gauge(
            NAME, "backfill_gap_blocks", max(0, head - (state.backfill_block or 0))
        )

    if gas:
        await _sample_gas(ctx, settings, state, rpc, head)


async def _backfill_loop(
    ctx: CollectorContext,
    settings: PolygonSettings,
    state: StreamState,
    rpc: HttpRpc,
) -> None:
    """Keep closing the log gap while the socket is live.

    A WebSocket outage advances the live head without emitting the missed
    blocks; this task retries the contiguous cursor every 30 s until it reaches
    the head. It never blocks the live socket: `_catch_up` holds the lock only
    for its bounded batch.
    """
    log = ctx.log(NAME)
    while not ctx.stop.is_set():
        try:
            await _catch_up(ctx, settings, state, rpc, gas=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - backfill must not kill the stream
            ctx.health.incr(NAME, "backfill_errors")
            log.warning(f"backfill failed: {type(exc).__name__}: {exc}")
        await _sleep(ctx, CATCHUP_POLL_S)


async def _sample_gas(
    ctx: CollectorContext,
    settings: PolygonSettings,
    state: StreamState,
    rpc: HttpRpc,
    head: int,
) -> None:
    """One header for the newest block, no more often than `gas_poll_seconds`."""
    now = time.monotonic()
    if state.gas_wall is not None and now - state.gas_wall < settings.gas_poll_seconds:
        return
    # `full=false` still returns the transaction hash list, so `tx_count` is
    # exact from a 7 kB header; the block body is never needed here.
    block = await asyncio.to_thread(rpc.get_block, head)
    state.gas_wall = time.monotonic()
    if block is None:
        ctx.log(NAME).warning(f"block {head} returned no body")
        return
    if state.head_block is not None and head < state.head_block:
        # A provider behind the socket would otherwise stall the gas series
        # silently, because `_emit_header` drops anything older than the head.
        ctx.health.incr(NAME, "gas_stale_samples")
        ctx.log(NAME).debug(
            f"gas sample {head} is behind the covered head {state.head_block}"
        )
        return
    if state.head_block is not None and head > state.head_block + 1:
        # Sampling skips blocks by design, so the jump is counted, not warned.
        ctx.health.incr(NAME, "gas_blocks_skipped", head - state.head_block - 1)
    _emit_header(
        ctx, state, block, time.time_ns(), time.perf_counter_ns(), warn_gap=False
    )


async def _sleep(ctx: CollectorContext, seconds: float) -> None:
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(ctx.stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _http_poll(
    ctx: CollectorContext,
    settings: PolygonSettings,
    state: StreamState,
    rpc: HttpRpc,
    seconds: float,
) -> None:
    """Poll over HTTP while the WebSocket is unavailable."""
    log = ctx.log(NAME)
    deadline = time.monotonic() + max(0.0, seconds)
    while not ctx.stop.is_set() and time.monotonic() < deadline:
        pause = settings.poll_seconds
        try:
            await _catch_up(ctx, settings, state, rpc)
        except Exception as exc:  # noqa: BLE001 - keep the process alive
            ctx.health.incr(NAME, "errors")
            log.warning(f"HTTP poll failed: {exc}")
            # Jitter a failing poll: a fixed cadence is a metronome aimed at a
            # provider that is already refusing us.
            pause = jittered_delay(0, base=settings.poll_seconds, cap=MAX_BACKOFF_S)
        remaining = deadline - time.monotonic()
        await _sleep(ctx, min(pause, remaining))


# --------------------------------------------------------------------------- #
# WebSocket path
# --------------------------------------------------------------------------- #
async def _subscribe(
    ws: Any, method: str, params: Any, request_id: int, pending: list[Any]
) -> str:
    """Send one `eth_subscribe` and read until its acknowledgement.

    Notifications that arrive before the acknowledgement (the log subscription
    starts streaming immediately) are appended to `pending` instead of being
    dropped: losing them would be exactly the skipped block this stream
    promises not to have.
    """
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": "eth_subscribe"}
    request["params"] = [method] if params is None else [method, params]
    await ws.send(orjson.dumps(request).decode())
    while True:
        raw = await ws.recv()
        message = orjson.loads(raw)
        if not isinstance(message, dict) or message.get("id") != request_id:
            pending.append(raw)
            continue
        if message.get("error"):
            raise RuntimeError(f"eth_subscribe {method} failed: {message['error']}")
        subscription = message.get("result")
        if not isinstance(subscription, str) or not subscription:
            raise RuntimeError(f"eth_subscribe {method} returned no subscription id")
        return subscription


def _discard(task: "asyncio.Future[Any]") -> None:
    """Cancel a pending recv and consume its exception.

    An abandoned `recv()` that finishes with `ConnectionClosed` would otherwise
    be reported as "Task exception was never retrieved" at interpreter
    shutdown, which hides real errors in the log.
    """
    if task.done():
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown only
            pass
    else:
        task.cancel()


async def _ws_session(
    ctx: CollectorContext,
    settings: PolygonSettings,
    state: StreamState,
    rpc: HttpRpc,
    ws_url: str,
) -> None:
    """One WebSocket session; returns only when the socket closes or stalls."""
    log = ctx.log(NAME)
    async with connect(
        ws_url,
        open_timeout=OPEN_TIMEOUT_S,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
        max_queue=1024,
    ) as ws:
        pending: list[Any] = []
        logs_sub = await _subscribe(
            ws, "logs", {"address": list(settings.addresses)}, 1, pending
        )
        heads_sub = await _subscribe(ws, "newHeads", None, 2, pending)
        log.info(
            f"subscribed logs={logs_sub[:10]} heads={heads_sub[:10]} "
            f"addresses={len(settings.addresses)}"
        )
        ctx.health.gauge(NAME, "connected", 1)
        try:
            # Subscribe first, then close the gap: anything mined during the
            # catch-up is already queued and is de-duplicated by block number.
            try:
                await _catch_up(ctx, settings, state, rpc, budget_s=5.0)
            except Exception as exc:  # noqa: BLE001 - the live socket still works
                ctx.health.incr(NAME, "errors")
                log.warning(f"catch-up failed, continuing live: {exc}")
            # Frames that arrived during the handshake are applied only now, so
            # they cannot raise the watermark past blocks the catch-up still has
            # to cover. The socket queue preserves order, so nothing is reordered.
            for raw in pending:
                _handle_message(
                    ctx, state, raw, logs_sub, heads_sub, time.time_ns(), time.perf_counter_ns()
                )

            stop_wait = asyncio.ensure_future(ctx.stop.wait())
            try:
                while True:
                    recv_task = asyncio.ensure_future(ws.recv())
                    await asyncio.wait(
                        {recv_task, stop_wait},
                        timeout=STALE_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stop_wait.done():
                        _discard(recv_task)
                        return
                    if not recv_task.done():
                        _discard(recv_task)
                        raise TimeoutError(f"no message in {STALE_SECONDS:.0f}s")
                    raw = recv_task.result()
                    ts, mono = time.time_ns(), time.perf_counter_ns()
                    try:
                        _handle_message(ctx, state, raw, logs_sub, heads_sub, ts, mono)
                    except Exception as exc:  # noqa: BLE001 - one bad frame, not the session
                        ctx.health.incr(NAME, "errors")
                        log.warning(f"bad message: {type(exc).__name__}: {exc}")
            finally:
                stop_wait.cancel()
        finally:
            ctx.health.gauge(NAME, "connected", 0)


def _handle_message(
    ctx: CollectorContext,
    state: StreamState,
    raw: Any,
    logs_sub: str,
    heads_sub: str,
    ts: int,
    mono: int,
) -> None:
    message = orjson.loads(raw)
    if not isinstance(message, dict) or message.get("method") != "eth_subscription":
        return
    params = message.get("params") or {}
    subscription = params.get("subscription")
    result = params.get("result")
    if subscription == logs_sub:
        for item in result if isinstance(result, list) else [result]:
            _emit_log(ctx, state, item, ts, mono)
    elif subscription == heads_sub:
        _emit_header(ctx, state, result, ts, mono)


async def _supervise(
    ctx: CollectorContext,
    settings: PolygonSettings,
    state: StreamState,
    rpc: HttpRpc,
    pool: ProviderPool,
) -> None:
    """Keep one WebSocket session up, rotating providers on a 429.

    Rotation is per failure, not per session: a rate-limited provider is put to
    rest and the next one in `pool.ws_candidates()` is dialled, so a single
    throttled key cannot pin the stream to HTTP for hours (2026-11-04: 492
    handshakes, all to one endpoint).
    """
    log = ctx.log(NAME)
    attempt = 0
    while not ctx.stop.is_set():
        candidates = pool.ws_candidates()
        if not candidates:
            wait = min(MAX_BACKOFF_S, max(1.0, pool.next_available_in()))
            log.warning(f"no WSS provider available; next attempt in {wait:.0f}s")
            await _sleep(ctx, wait)
            continue
        provider = candidates[0]
        ws_url = provider.wss or settings.ws_url
        try:
            await _ws_session(ctx, settings, state, rpc, ws_url)
            pool.mark_ok(provider.url)
            attempt = 0
            await _sleep(ctx, 1.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect, never die
            ctx.health.incr(NAME, "errors")
            ctx.health.incr(NAME, "reconnects")
            state.reconnects += 1
            kind = classify_rpc_error(exc)
            if kind == RATE_LIMIT:
                cooled = pool.mark_rate_limited(provider.url)
                ctx.health.incr(NAME, "rate_limited")
                following = pool.ws_candidates()
                target = following[0].name if following else "none"
                log.warning(
                    f"WSS {mask_endpoint(ws_url)} rate limited (HTTP 429): "
                    f"{provider.name} cooling down {cooled:.0f}s, trying {target}"
                )
                # Do not spend the whole cooldown on HTTP: the point of the
                # cooldown is to switch sockets, so poll only long enough to
                # keep the gaps filled while the next provider is dialled.
                delay = min(cooled, ROTATE_POLL_S)
            else:
                pool.mark_error(provider.url, kind)
                attempt += 1
                delay = jittered_delay(attempt, base=1.0, cap=MAX_BACKOFF_S)
                log.warning(
                    f"WSS {mask_endpoint(ws_url)} failed: "
                    f"{type(exc).__name__}: {exc}; HTTP fallback for {delay:.0f}s"
                )
            await _http_poll(ctx, settings, state, rpc, delay)


def _publish_providers(ctx: CollectorContext, pool: ProviderPool) -> None:
    """Per-provider health: counters and how long each must still rest."""
    for metric, value in pool.metrics().items():
        ctx.health.gauge(NAME, metric, value)


async def _report_age(
    ctx: CollectorContext, state: StreamState, pool: ProviderPool
) -> None:
    """Publish how long the chain has been silent, measured on a monotonic clock.

    The block's own timestamp would be the natural measure, but this host's
    clock is a fraction of a second off the chain's, which clamps a real age to
    zero. Wall arrival time cannot go backwards and grows while a socket stalls,
    which is the signal the health table needs.
    """
    while not ctx.stop.is_set():
        if state.head_wall is not None:
            ctx.health.gauge(
                NAME, "last_block_age_s", max(0.0, time.monotonic() - state.head_wall)
            )
        _publish_providers(ctx, pool)
        await _sleep(ctx, 5.0)


async def run(ctx: CollectorContext) -> None:
    log = ctx.log(NAME)
    ctx.register_table("polygon_logs", POLYGON_LOGS)
    ctx.register_table("polygon_gas", POLYGON_GAS)

    settings = PolygonSettings.from_section(ctx.section(NAME))
    pool = provider_pool(settings)
    log.info(
        f"polygon: ws={mask_endpoint(settings.ws_url)} "
        f"http={[mask_endpoint(u) for u in settings.http_urls]} "
        f"span={settings.log_block_span} addresses={len(settings.addresses)} "
        f"providers={[h.name for h in pool.health()]}"
    )
    rpc = HttpRpc(pool)
    state = StreamState()
    state.backfill_block = _last_taped_block(ctx.hub.root)
    state.log_block = state.backfill_block
    age_task = asyncio.create_task(_report_age(ctx, state, pool))
    backfill_task = asyncio.create_task(
        _backfill_loop(ctx, settings, state, rpc), name="polygon-backfill"
    )
    try:
        await _supervise(ctx, settings, state, rpc, pool)
    finally:
        backfill_task.cancel()
        age_task.cancel()
        for task in (backfill_task, age_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown only
                pass
        rpc.close()
        log.info(
            f"polygon stopped: logs={state.logs} blocks={state.blocks} "
            f"reconnects={state.reconnects} last_block={state.head_block} "
            f"providers={ {h.name: h.ok for h in pool.health()} }"
        )
