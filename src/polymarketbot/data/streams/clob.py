"""Polymarket CLOB raw event tape.

The simulator's ``LivePolymarketFeed`` maintains trading state: it coalesces
books, drops events under load and clears state on reconnect. A tape needs the
opposite: every frame the venue sent, in the order it arrived, with a visible
hole when the socket was down and nothing invented to fill it.

Design
------
* One websocket per shard. Assets are assigned round-robin and re-assigned only
  by the universe diff (subscribe/unsubscribe frames on the live socket).
* A frame is parsed once and written once. ``book`` frames are kept verbatim as
  level lists; ``price_change`` frames keep the venue's list of changes.
* ``last_trade_price``/``trade`` go to ``clob_trades``; everything else,
  including event types this client does not understand, goes to
  ``clob_events`` so a schema change on the venue shows up in the data rather
  than disappearing.
* A disconnect produces one ``gap`` row spanning last-frame to resubscribe.
  The next ``book`` frame is the resync anchor; no book state is fabricated.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import orjson
import websockets

from polymarketbot.data.context import CollectorContext
from polymarketbot.data.schema import CLOB_EVENTS, CLOB_TRADES

NAME = "clob"

DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DEFAULT_SHARDS = 8
DEFAULT_HEARTBEAT_SECONDS = 10.0
DEFAULT_RECONNECT_MAX_SECONDS = 30.0

# While a shard has been silent this long, write a liveness row so the tape
# shows the socket was alive rather than merely idle.
QUIET_HEARTBEAT_SECONDS = 60.0

# Frames whose payload is a trade execution rather than an order-book event.
TRADE_TYPES = frozenset({"last_trade_price", "trade"})

# Text control frames the venue exchanges to keep the socket alive.
CONTROL_FRAMES = frozenset({"PING", "PONG"})

# Keys that mark a dict as an event rather than a transport envelope. A
# ``payload`` wrapper is unwrapped only when none of these are present.
_EVENT_KEYS = ("event_type", "type", "asset_id", "bids", "asks", "price_changes", "market")


# --------------------------------------------------------------------- helpers
def _num(value: Any) -> float | None:
    """Parse a float, preserving the difference between missing and zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def parse_levels(raw: Any) -> list[dict[str, float]]:
    """Parse ``[{"price": "0.48", "size": "120"}, ...]`` keeping venue order."""
    if not isinstance(raw, list):
        return []
    levels: list[dict[str, float]] = []
    for item in raw:
        if isinstance(item, dict):
            price = _num(item.get("price"))
            size = _num(item.get("size"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price = _num(item[0])
            size = _num(item[1])
        else:
            continue
        if price is None:
            continue
        levels.append({"price": price, "size": size if size is not None else 0.0})
    return levels


def parse_price_changes(raw: Any) -> list[dict[str, Any]]:
    """Parse a ``price_change`` frame's change list.

    ``size`` is the new aggregate size at that price; zero means the level was
    removed. The tape records the number as sent, it does not interpret it.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "asset_id": _str(item.get("asset_id")),
                "price": _num(item.get("price")),
                "size": _num(item.get("size")),
                "side": _str(item.get("side")),
                "best_bid": _num(item.get("best_bid")),
                "best_ask": _num(item.get("best_ask")),
            }
        )
    return out


def is_control_message(message: Any) -> bool:
    """True for the text ``PING``/``PONG`` frames used as liveness probes."""
    return isinstance(message, str) and message.strip().upper() in CONTROL_FRAMES


def _is_envelope(obj: dict[str, Any]) -> bool:
    if "payload" not in obj:
        return False
    if not isinstance(obj["payload"], (dict, list)):
        return False
    return not any(key in obj for key in _EVENT_KEYS)


def _flatten(obj: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 4:
        return
    if isinstance(obj, list):
        for item in obj:
            _flatten(item, out, depth + 1)
        return
    if not isinstance(obj, dict):
        return
    if _is_envelope(obj):
        _flatten(obj["payload"], out, depth + 1)
        return
    out.append(obj)


def decode_frames(message: str | bytes | bytearray | memoryview | dict | list) -> list[dict[str, Any]]:
    """Decode one websocket message into the event dicts it carries.

    A message may be a dict, a list of dicts, or a dict wrapping either under
    ``payload``. Raises on malformed JSON; callers count that as a parse error.
    """
    obj: Any = message
    if isinstance(obj, (bytes, bytearray, memoryview)):
        obj = orjson.loads(bytes(obj))
    elif isinstance(obj, str):
        obj = orjson.loads(obj)
    frames: list[dict[str, Any]] = []
    _flatten(obj, frames)
    return frames


def _first_asset(frame: Mapping[str, Any]) -> str | None:
    for key in ("asset_id", "assets_ids", "asset_ids"):
        raw = frame.get(key)
        if isinstance(raw, list):
            for item in raw:
                text = _str(item)
                if text:
                    return text
            continue
        text = _str(raw)
        if text:
            return text
    return None


def _condition_id(frame: Mapping[str, Any], asset_id: str | None, asset_to_market: Mapping[str, str]) -> str | None:
    return _str(frame.get("market")) or _str(frame.get("condition_id")) or asset_to_market.get(asset_id or "")


def parse_frame(
    frame: Mapping[str, Any],
    *,
    shard: int,
    asset_to_market: Mapping[str, str],
    recv_ts_ns: int,
    recv_mono_ns: int,
) -> list[tuple[str, dict[str, Any]]]:
    """Turn one venue frame into ``(table, row)`` pairs.

    Pure: no clock, no I/O. ``recv_ts_ns``/``recv_mono_ns`` are supplied by the
    caller so a whole message shares one receive timestamp.
    """
    if not isinstance(frame, Mapping):
        return []

    event_type = _str(frame.get("event_type")) or _str(frame.get("type"))
    asset_id = _first_asset(frame)
    base: dict[str, Any] = {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "venue_ts_ms": _int(frame.get("timestamp")),
        "shard": int(shard),
        "condition_id": _condition_id(frame, asset_id, asset_to_market),
        "asset_id": asset_id,
        "event_hash": _str(frame.get("hash")),
    }

    if event_type in TRADE_TYPES:
        trade = dict(base)
        trade.pop("event_hash", None)
        trade.update(
            {
                "price": _num(frame.get("price")),
                "size": _num(frame.get("size")),
                "side": _str(frame.get("side")),
                "fee_rate_bps": _num(frame.get("fee_rate_bps")),
                "transaction_hash": _str(frame.get("transaction_hash")),
                "raw_json": orjson.dumps(frame).decode(),
            }
        )
        return [("clob_trades", trade)]

    row = dict(base)
    row["event_type"] = event_type or "unknown"

    if event_type == "book":
        row["bids"] = parse_levels(frame.get("bids"))
        row["asks"] = parse_levels(frame.get("asks"))
    elif event_type == "price_change":
        changes = parse_price_changes(frame.get("price_changes"))
        row["price_changes"] = changes
        # A frame may mix changes for both outcome tokens. Keep the row-level
        # asset only when every change agrees; otherwise the nested
        # `price_changes[*].asset_id` is the authoritative identity and a
        # downstream replay must group by it.
        change_assets = {c["asset_id"] for c in changes if c.get("asset_id")}
        if len(change_assets) == 1 and row["asset_id"] is None:
            row["asset_id"] = next(iter(change_assets))
        elif len(change_assets) > 1:
            row["asset_id"] = None
            row["condition_id"] = None
        if row["condition_id"] is None and row["asset_id"]:
            row["condition_id"] = asset_to_market.get(row["asset_id"])
    elif event_type == "tick_size_change":
        row["old_tick_size"] = _num(frame.get("old_tick_size"))
        row["new_tick_size"] = _num(frame.get("new_tick_size"))
    elif event_type == "best_bid_ask":
        best_bid = _num(frame.get("best_bid"))
        best_ask = _num(frame.get("best_ask"))
        row["best_bid"] = best_bid
        row["best_ask"] = best_ask
        spread = _num(frame.get("spread"))
        if spread is None and best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid
        row["spread"] = spread
    elif event_type in ("new_market", "market_resolved"):
        row["raw_json"] = orjson.dumps(frame).decode()
    else:
        # Unknown or future event types are taped verbatim rather than dropped.
        row["raw_json"] = orjson.dumps(frame).decode()

    return [("clob_events", row)]


def gap_row(
    *,
    shard: int,
    gap_start_ns: int,
    gap_end_ns: int,
    reason: str,
    recv_ts_ns: int,
    recv_mono_ns: int,
) -> tuple[str, dict[str, Any]]:
    """A ``gap`` event: the shard was not observed between the two stamps."""
    payload = {
        "gap_start_ns": int(gap_start_ns),
        "gap_end_ns": int(gap_end_ns),
        "reason": reason,
    }
    return (
        "clob_events",
        {
            "recv_ts_ns": recv_ts_ns,
            "recv_mono_ns": recv_mono_ns,
            "venue_ts_ms": None,
            "shard": int(shard),
            "condition_id": None,
            "asset_id": None,
            "event_type": "gap",
            "event_hash": None,
            "raw_json": orjson.dumps(payload).decode(),
        },
    )


def assign_shards(assets: Iterable[str], shards: int) -> dict[str, int]:
    """Round-robin assignment preserving input order. Duplicates collapse."""
    n = max(1, int(shards))
    out: dict[str, int] = {}
    for index, asset in enumerate(assets):
        asset = str(asset)
        if asset and asset not in out:
            out[asset] = index % n
    return out


def plan_universe_diff(
    added_assets: Iterable[str],
    removed_assets: Iterable[str],
    asset_shard: Mapping[str, int],
    shard_counts: list[int],
    shards: int,
) -> tuple[dict[str, int], list[tuple[int, dict[str, Any]]], list[int]]:
    """Translate a universe diff into per-shard subscribe/unsubscribe frames.

    Pure: ``shard_counts`` is not mutated. Added assets go to the least loaded
    shard (ties to the lowest index). Returns the new assignments, the frames
    to send, and the updated counts.
    """
    counts = list(shard_counts) + [0] * max(0, int(shards) - len(shard_counts))
    counts = counts[: max(1, int(shards))]
    assignments: dict[str, int] = {}
    messages: list[tuple[int, dict[str, Any]]] = []

    for asset in removed_assets:
        asset = str(asset)
        index = asset_shard.get(asset)
        if index is None:
            continue
        counts[index] = max(0, counts[index] - 1)
        messages.append((index, {"assets_ids": [asset], "operation": "unsubscribe"}))

    for asset in added_assets:
        asset = str(asset)
        if not asset or asset in asset_shard or asset in assignments:
            continue
        index = counts.index(min(counts))
        counts[index] += 1
        assignments[asset] = index
        messages.append((index, {"assets_ids": [asset], "operation": "subscribe"}))

    return assignments, messages, counts


def backoff_delay(attempt: int, cap: float, base: float = 1.0, rng=random.random) -> float:
    """Exponential backoff with full jitter, never above ``cap``."""
    cap = max(0.1, float(cap))
    raw = base * (2.0 ** max(0, int(attempt)))
    return min(cap, raw * (0.5 + 0.5 * float(rng())))


def subscribe_frame(assets: Iterable[str], *, initial_dump: bool) -> dict[str, Any]:
    return {
        "assets_ids": sorted(str(a) for a in assets),
        "type": "market",
        "custom_feature_enabled": True,
        "initial_dump": bool(initial_dump),
    }


# ------------------------------------------------------------------- shard I/O
@dataclass
class ShardState:
    index: int
    assets: set[str] = field(default_factory=set)
    outbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    last_msg_mono: float = field(default_factory=time.monotonic)


def _metric(shard: int, name: str) -> str:
    return f"shard{shard}.{name}"


def heartbeat_row(*, shard: int, last_msg_age_s: float, assets: int, recv_ts_ns: int, recv_mono_ns: int) -> tuple[str, dict[str, Any]]:
    """A liveness anchor written while a shard is quiet.

    The row asserts only that the socket was alive and how long it had been
    silent. It carries no book state, so it cannot be mistaken for a snapshot.
    """
    payload = {"last_msg_age_s": float(last_msg_age_s), "assets": int(assets)}
    return (
        "clob_events",
        {
            "recv_ts_ns": recv_ts_ns,
            "recv_mono_ns": recv_mono_ns,
            "venue_ts_ms": None,
            "shard": int(shard),
            "condition_id": None,
            "asset_id": None,
            "event_type": "heartbeat",
            "event_hash": None,
            "raw_json": orjson.dumps(payload).decode(),
        },
    )


async def _keepalive(ctx: CollectorContext, ws, state: ShardState, heartbeat_seconds: float) -> None:
    """Send ``PING`` on schedule and drain universe-diff frames onto the socket."""
    shard = state.index
    next_ping = time.monotonic() + heartbeat_seconds
    last_heartbeat_row = time.monotonic()
    while not ctx.stop.is_set():
        while True:
            try:
                payload = state.outbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            await ws.send(orjson.dumps(payload).decode())
            ctx.health.incr(NAME, _metric(shard, "diffs"))
        now = time.monotonic()
        if now >= next_ping:
            await ws.send("PING")
            next_ping = now + heartbeat_seconds
            ctx.health.incr(NAME, _metric(shard, "pings"))
        ctx.health.gauge(NAME, _metric(shard, "last_msg_age_s"), now - state.last_msg_mono, {"shard": shard})
        ctx.health.gauge(NAME, _metric(shard, "assets"), len(state.assets), {"shard": shard})
        quiet_for = now - state.last_msg_mono
        if quiet_for >= QUIET_HEARTBEAT_SECONDS and now - last_heartbeat_row >= QUIET_HEARTBEAT_SECONDS:
            now_ns = time.time_ns()
            table, row = heartbeat_row(
                shard=shard,
                last_msg_age_s=quiet_for,
                assets=len(state.assets),
                recv_ts_ns=now_ns,
                recv_mono_ns=time.perf_counter_ns(),
            )
            try:
                ctx.emit(table, row)
                ctx.health.incr(NAME, _metric(shard, "heartbeat_rows"))
            except Exception as exc:  # noqa: BLE001
                ctx.health.incr(NAME, _metric(shard, "emit_errors"))
                ctx.log(NAME).error(f"shard {shard} heartbeat emit failed: {exc}")
            last_heartbeat_row = now
        await asyncio.sleep(0.5)


async def _read_loop(ctx: CollectorContext, ws, state: ShardState, universe: Any, log) -> None:
    """Read frames until the socket dies or the run stops. Never swallows a
    disconnect: the exception propagates to the shard loop, which reconnects."""
    shard = state.index
    while not ctx.stop.is_set():
        message = await ws.recv()
        state.last_msg_mono = time.monotonic()
        recv_ts_ns = time.time_ns()
        recv_mono_ns = time.perf_counter_ns()
        ctx.health.incr(NAME, _metric(shard, "msgs"))
        if is_control_message(message):
            ctx.health.incr(NAME, _metric(shard, "heartbeats"))
            continue
        try:
            frames = decode_frames(message)
        except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the shard
            ctx.health.incr(NAME, _metric(shard, "parse_errors"))
            log.debug(f"shard {shard} undecodable message: {exc}")
            continue
        for frame in frames:
            try:
                rows = parse_frame(
                    frame,
                    shard=shard,
                    asset_to_market=universe.asset_to_market,
                    recv_ts_ns=recv_ts_ns,
                    recv_mono_ns=recv_mono_ns,
                )
            except Exception as exc:  # noqa: BLE001
                ctx.health.incr(NAME, _metric(shard, "parse_errors"))
                log.debug(f"shard {shard} unparsable frame: {exc}")
                continue
            for table, row in rows:
                try:
                    ctx.emit(table, row)
                except Exception as exc:  # noqa: BLE001 - writer failure must not kill the shard
                    ctx.health.incr(NAME, _metric(shard, "emit_errors"))
                    log.error(f"shard {shard} emit failed for {table}: {exc}")
                    continue
                ctx.health.incr(NAME, _metric(shard, "trades" if table == "clob_trades" else "events"))
                if row.get("event_type") == "market_resolved" and universe is not None:
                    try:
                        universe.on_market_resolved(row.get("condition_id"))
                    except Exception as exc:  # noqa: BLE001
                        log.warning(f"shard {shard} market_resolved handling failed: {exc}")


async def _pump(ctx: CollectorContext, ws, state: ShardState, universe: Any, heartbeat_seconds: float, log) -> None:
    reader = asyncio.create_task(_read_loop(ctx, ws, state, universe, log), name=f"clob-read-{state.index}")
    keeper = asyncio.create_task(_keepalive(ctx, ws, state, heartbeat_seconds), name=f"clob-keep-{state.index}")
    try:
        done, pending = await asyncio.wait({reader, keeper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (reader, keeper):
            if not task.done():
                task.cancel()
        await asyncio.gather(reader, keeper, return_exceptions=True)
    for task in done:
        exc = task.exception()
        if exc is not None:
            raise exc


async def _sleep_or_stop(ctx: CollectorContext, delay: float) -> None:
    try:
        await asyncio.wait_for(ctx.stop.wait(), timeout=max(0.0, delay))
    except asyncio.TimeoutError:
        pass


async def _shard_loop(
    ctx: CollectorContext,
    state: ShardState,
    cfg: dict[str, Any],
    universe: Any,
    log,
) -> None:
    shard = state.index
    ws_url = str(cfg.get("ws_url") or DEFAULT_WS_URL)
    heartbeat_seconds = float(cfg.get("heartbeat_seconds") or DEFAULT_HEARTBEAT_SECONDS)
    reconnect_max = float(cfg.get("reconnect_max_seconds") or DEFAULT_RECONNECT_MAX_SECONDS)
    initial_dump = bool(cfg.get("resubscribe_initial_dump", True))

    attempt = 0
    pending_gap_start: int | None = None
    last_reason = "disconnect"

    while not ctx.stop.is_set():
        if not state.assets:
            # Nothing to subscribe to; wait for the universe manager.
            await _sleep_or_stop(ctx, 1.0)
            continue
        try:
            async with websockets.connect(
                ws_url,
                max_size=None,
                ping_interval=None,
                open_timeout=20,
                close_timeout=2,
            ) as ws:
                await ws.send(orjson.dumps(subscribe_frame(state.assets, initial_dump=initial_dump)).decode())
                ctx.health.incr(NAME, _metric(shard, "connects"))
                if pending_gap_start is not None:
                    now_ns = time.time_ns()
                    table, row = gap_row(
                        shard=shard,
                        gap_start_ns=pending_gap_start,
                        gap_end_ns=now_ns,
                        reason=last_reason,
                        recv_ts_ns=now_ns,
                        recv_mono_ns=time.perf_counter_ns(),
                    )
                    ctx.emit(table, row)
                    ctx.health.incr(NAME, _metric(shard, "gaps"))
                    log.warning(
                        f"shard {shard} resubscribed {len(state.assets)} assets after "
                        f"{(now_ns - pending_gap_start) / 1e6:.0f}ms gap ({last_reason})"
                    )
                    pending_gap_start = None
                attempt = 0
                log.info(f"shard {shard} subscribed to {len(state.assets)} assets")
                await _pump(ctx, ws, state, universe, heartbeat_seconds, log)
                if ctx.stop.is_set():
                    return
                last_reason = "connection closed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is a reconnect
            last_reason = f"{type(exc).__name__}: {exc}"
        if ctx.stop.is_set():
            return

        if pending_gap_start is None:
            pending_gap_start = time.time_ns()
        ctx.health.incr(NAME, _metric(shard, "reconnects"))
        delay = max(5.0, backoff_delay(attempt, reconnect_max))
        attempt += 1
        log.warning(f"shard {shard} disconnected ({last_reason}); reconnecting in {delay:.1f}s")
        await _sleep_or_stop(ctx, delay)


async def _universe_loop(
    ctx: CollectorContext,
    states: list[ShardState],
    asset_shard: dict[str, int],
    shard_counts: list[int],
    shards: int,
    log,
) -> None:
    """Apply universe add/remove diffs onto the live sockets."""
    while not ctx.stop.is_set():
        try:
            update = await asyncio.wait_for(ctx.universe.updates.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        added = list(getattr(update, "added_assets", None) or [])
        removed = list(getattr(update, "removed_assets", None) or [])
        if not added and not removed:
            for market in getattr(update, "added_markets", None) or []:
                added.extend(getattr(market, "tokens", ()) or ())
            for market in getattr(update, "removed_markets", None) or []:
                removed.extend(getattr(market, "tokens", ()) or ())
        if not added and not removed:
            continue
        try:
            assignments, messages, counts = plan_universe_diff(added, removed, asset_shard, shard_counts, shards)
        except Exception as exc:  # noqa: BLE001
            log.error(f"universe diff planning failed: {exc}")
            continue

        for asset, index in assignments.items():
            asset_shard[asset] = index
        for index, message in messages:
            if message.get("operation") == "subscribe":
                for asset in message["assets_ids"]:
                    states[index].assets.add(asset)
            else:
                for asset in message["assets_ids"]:
                    states[index].assets.discard(asset)
                    asset_shard.pop(asset, None)
            states[index].outbox.put_nowait(message)
        shard_counts[:] = counts
        ctx.health.gauge(NAME, "assets", len(asset_shard))
        log.info(
            f"universe diff: +{len(added)}/-{len(removed)} assets, "
            f"{len(messages)} socket updates, {len(asset_shard)} tracked"
        )


async def run(ctx: CollectorContext) -> None:
    cfg = ctx.section(NAME)
    log = ctx.log(NAME)
    shards = max(1, int(cfg.get("shards") or DEFAULT_SHARDS))

    ctx.register_table("clob_events", CLOB_EVENTS)
    ctx.register_table("clob_trades", CLOB_TRADES)

    assets = list(ctx.universe.assets()) if ctx.universe is not None else []
    if not assets:
        log.error("universe has no assets; clob stream not started")
        return

    asset_shard = assign_shards(assets, shards)
    shard_counts = [0] * shards
    states: list[ShardState] = []
    for index in range(shards):
        state = ShardState(index=index)
        states.append(state)
    for asset, index in asset_shard.items():
        states[index].assets.add(asset)
        shard_counts[index] += 1

    ctx.health.gauge(NAME, "assets", len(asset_shard))
    ctx.health.gauge(NAME, "shards", shards)
    log.info(
        f"clob tape: {len(asset_shard)} assets over {shards} shards, "
        f"ws={cfg.get('ws_url') or DEFAULT_WS_URL}"
    )

    start_delay = float(cfg.get("shard_start_delay_seconds") or 2.0)

    async def _start_shard(state: ShardState) -> None:
        # Polymarket rate-limits new connections; eight simultaneous
        # handshakes after a restart look like a burst and get reset.
        if state.index:
            await asyncio.sleep(state.index * start_delay)
        await _shard_loop(ctx, state, cfg, ctx.universe, log)

    tasks = [
        asyncio.create_task(_start_shard(state), name=f"clob-shard-{state.index}")
        for state in states
    ]
    manager = asyncio.create_task(
        _universe_loop(ctx, states, asset_shard, shard_counts, shards, log),
        name="clob-universe",
    )
    try:
        await ctx.stop.wait()
    finally:
        manager.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(manager, *tasks, return_exceptions=True)
        log.info("clob tape stopped")
