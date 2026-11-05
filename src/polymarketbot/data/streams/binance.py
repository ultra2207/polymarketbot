"""Binance USD-M futures market data.

Two websockets, one process:

* the combined market stream for every configured symbol, carrying
  ``<sym>@bookTicker`` (BBO), ``<sym>@depth<levels>@<interval>ms`` (partial
  book) and ``<sym>@markPrice@1s`` (funding/mark/index);
* ``!forceOrder@arr`` on its own connection, because liquidation bursts would
  otherwise queue behind the book traffic on a single socket.

REST covers what the sockets do not: ``/fapi/v1/premiumIndex`` for the funding
snapshot, ``/fapi/v1/openInterest`` for open interest, and a depth backstop
that only fires when the websocket depth has been silent for
``depth_rest_fallback_seconds``. The backstop exists because the futures feed
is not guaranteed to deliver partial depth on every network path; it is
strictly a gap filler, never a replacement for a working socket.

Host derivation: the config points at ``stream.binance.com`` / ``api.binance.com``
(spot). USD-M futures live on ``fstream.binance.com`` (TLS on 443 only) and
``fapi.binance.com``; both are derived from the configured URLs so one config
edit still moves everything.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import orjson
import requests
from websockets.asyncio.client import connect

from polymarketbot.data.context import CollectorContext
from polymarketbot.data.schema import (
    BINANCE_BBO,
    BINANCE_DEPTH,
    BINANCE_FUNDING,
    BINANCE_LIQUIDATION,
    BINANCE_OI,
)

NAME = "binance"

DEFAULT_WS = "wss://stream.binance.com:9443/stream"
DEFAULT_REST = "https://api.binance.com"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")

# Partial book depth only accepts these update speeds on USD-M futures.
SUPPORTED_DEPTH_INTERVALS_MS = (250, 500)
LIQUIDATION_STREAM = "!forceOrder@arr"

TABLES = (
    ("binance_bbo", BINANCE_BBO),
    ("binance_depth", BINANCE_DEPTH),
    ("binance_funding", BINANCE_FUNDING),
    ("binance_oi", BINANCE_OI),
    ("binance_liquidation", BINANCE_LIQUIDATION),
)

_RECONNECT_MAX_SECONDS = 60.0
_RECONNECT_MIN_SECONDS = 1.0
_REQUEST_TIMEOUT = 10.0
_BBO_MIN_INTERVAL_NS = 100_000_000  # 100 ms per symbol, per the master spec
_USER_AGENT = "polymarketbot/0.1"


# --------------------------------------------------------------------------- #
# Pure helpers: URL derivation, frame routing, payload parsing.
# --------------------------------------------------------------------------- #


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _as_int(value: Any) -> int | None:
    out = _as_float(value)
    return None if out is None else int(out)


def futures_ws_url(ws_url: str) -> str:
    """Derive the USD-M futures websocket base from the configured URL."""
    parts = urlsplit(ws_url or DEFAULT_WS)
    host = parts.hostname or "fstream.binance.com"
    if host.startswith("stream."):
        host = "fstream." + host[len("stream.") :]
    # 9443 is the spot listener; the futures listener only answers on 443.
    netloc = host if not parts.port or parts.port == 443 else f"{host}:443"
    return urlunsplit((parts.scheme or "wss", netloc, parts.path or "/stream", "", ""))


def futures_rest_url(rest_url: str) -> str:
    """Derive the USD-M futures REST base from the configured URL."""
    parts = urlsplit(rest_url or DEFAULT_REST)
    host = parts.hostname or "fapi.binance.com"
    if host.startswith("api."):
        host = "fapi." + host[len("api.") :]
    netloc = host if not parts.port else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme or "https", netloc, (parts.path or "").rstrip("/"), "", ""))


def depth_interval_ms(configured: int) -> int:
    """Clamp a configured depth speed to one the futures feed supports."""
    if configured in SUPPORTED_DEPTH_INTERVALS_MS:
        return configured
    return min(SUPPORTED_DEPTH_INTERVALS_MS, key=lambda v: (abs(v - configured), v))


def stream_names(symbols: list[str], levels: int, interval_ms: int) -> list[str]:
    names: list[str] = []
    for symbol in symbols:
        sym = symbol.lower()
        names.append(f"{sym}@bookTicker")
        names.append(f"{sym}@depth{levels}@{interval_ms}ms")
        names.append(f"{sym}@markPrice@1s")
    return names


def combined_url(base: str, streams: list[str]) -> str:
    return f"{base}?streams={'/'.join(streams)}"


def split_frame(message: str | bytes | dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Return ``(stream_name, payload)`` for combined and raw frames alike."""
    if isinstance(message, dict):
        payload: Any = message
    else:
        try:
            payload = orjson.loads(message)
        except (orjson.JSONDecodeError, TypeError):
            return None, {}
    if not isinstance(payload, dict):
        return None, {}
    data = payload.get("data")
    stream = payload.get("stream")
    if isinstance(data, dict):
        return (str(stream) if stream else None), data
    return (str(stream) if stream else None), payload


def parse_levels(levels: Any) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for level in levels or []:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = _as_float(level[0])
        size = _as_float(level[1])
        if price is None or size is None:
            continue
        out.append({"price": price, "size": size})
    return out


def parse_bbo(data: dict[str, Any], recv_ts_ns: int, recv_mono_ns: int) -> dict[str, Any] | None:
    bid = _as_float(data.get("b"))
    ask = _as_float(data.get("a"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "venue_ts_ms": _as_int(data.get("E")),
        "symbol": data.get("s"),
        "bid": bid,
        "bid_qty": _as_float(data.get("B")),
        "ask": ask,
        "ask_qty": _as_float(data.get("A")),
        "update_id": _as_int(data.get("u")),
    }


def parse_depth(
    data: dict[str, Any],
    recv_ts_ns: int,
    recv_mono_ns: int,
    *,
    symbol: str | None = None,
    last_update_id: Any = None,
    venue_ts_ms: Any = None,
) -> dict[str, Any] | None:
    bids = parse_levels(data.get("b") if "b" in data else data.get("bids"))
    asks = parse_levels(data.get("a") if "a" in data else data.get("asks"))
    if not bids and not asks:
        return None
    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "venue_ts_ms": _as_int(venue_ts_ms if venue_ts_ms is not None else (data.get("E") or data.get("T"))),
        "symbol": symbol or data.get("s"),
        "last_update_id": _as_int(last_update_id if last_update_id is not None else data.get("u")),
        "bids": bids,
        "asks": asks,
    }


def parse_mark_price(data: dict[str, Any], recv_ts_ns: int, recv_mono_ns: int) -> dict[str, Any] | None:
    symbol = data.get("s")
    funding_rate = _as_float(data.get("r"))
    mark_price = _as_float(data.get("p"))
    if not symbol or (funding_rate is None and mark_price is None):
        return None
    next_funding = _as_int(data.get("T"))
    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "venue_ts_ms": _as_int(data.get("E")),
        "symbol": symbol,
        "funding_rate": funding_rate,
        "funding_time_ms": next_funding,
        "mark_price": mark_price,
        "index_price": _as_float(data.get("i")),
        "next_funding_time_ms": next_funding,
    }


def parse_force_order(data: dict[str, Any], recv_ts_ns: int, recv_mono_ns: int) -> dict[str, Any] | None:
    order = data.get("o") if isinstance(data.get("o"), dict) else data
    symbol = order.get("s") or data.get("s")
    price = _as_float(order.get("p"))
    qty = _as_float(order.get("q"))
    if not symbol or price is None or qty is None:
        return None
    trade_time = _as_int(order.get("T") or data.get("T"))
    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "venue_ts_ms": _as_int(data.get("E")) or trade_time,
        "symbol": symbol,
        "side": order.get("S"),
        "price": price,
        "qty": qty,
        "order_type": order.get("o"),
        "trade_time_ms": trade_time,
    }


def parse_premium_index(payload: dict[str, Any], recv_ts_ns: int, recv_mono_ns: int) -> dict[str, Any] | None:
    symbol = payload.get("symbol")
    if not symbol:
        return None
    next_funding = _as_int(payload.get("nextFundingTime"))
    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "venue_ts_ms": _as_int(payload.get("time")),
        "symbol": symbol,
        "funding_rate": _as_float(payload.get("lastFundingRate")),
        "funding_time_ms": next_funding,
        "mark_price": _as_float(payload.get("markPrice")),
        "index_price": _as_float(payload.get("indexPrice")),
        "next_funding_time_ms": next_funding,
    }


def parse_open_interest(payload: dict[str, Any], recv_ts_ns: int, recv_mono_ns: int) -> dict[str, Any] | None:
    symbol = payload.get("symbol")
    open_interest = _as_float(payload.get("openInterest"))
    if not symbol or open_interest is None:
        return None
    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "venue_ts_ms": _as_int(payload.get("time")),
        "symbol": symbol,
        "open_interest": open_interest,
    }


def parse_rest_depth(
    payload: dict[str, Any], symbol: str, recv_ts_ns: int, recv_mono_ns: int
) -> dict[str, Any] | None:
    return parse_depth(
        payload,
        recv_ts_ns,
        recv_mono_ns,
        symbol=symbol,
        last_update_id=payload.get("lastUpdateId"),
        venue_ts_ms=payload.get("E") or payload.get("T"),
    )


_EVENT_TO_KIND = {
    "bookTicker": "bbo",
    "depthUpdate": "depth",
    "markPriceUpdate": "funding",
    "forceOrder": "liquidation",
}

_KIND_TO_TABLE = {
    "bbo": ("binance_bbo", parse_bbo),
    "depth": ("binance_depth", parse_depth),
    "funding": ("binance_funding", parse_mark_price),
    "liquidation": ("binance_liquidation", parse_force_order),
}


def frame_kind(stream: str | None, data: dict[str, Any]) -> str | None:
    if stream:
        name = stream.lower()
        if name.endswith("@bookticker"):
            return "bbo"
        if "@depth" in name:
            return "depth"
        if "@markprice" in name:
            return "funding"
        if name.startswith("!forceorder"):
            return "liquidation"
    return _EVENT_TO_KIND.get(data.get("e"))


def handle_frame(
    message: str | bytes | dict[str, Any], recv_ts_ns: int, recv_mono_ns: int
) -> tuple[str, dict[str, Any]] | None:
    """Route one websocket frame to ``(table, row)``; ``None`` means ignore."""
    stream, data = split_frame(message)
    kind = frame_kind(stream, data)
    if kind is None:
        return None
    table, parser = _KIND_TO_TABLE[kind]
    row = parser(data, recv_ts_ns, recv_mono_ns)
    return None if row is None else (table, row)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def _get_json(url: str, params: dict[str, Any], timeout: float = _REQUEST_TIMEOUT) -> Any:
    response = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": _USER_AGENT})
    response.raise_for_status()
    return response.json()


async def _fetch_json(url: str, params: dict[str, Any], timeout: float = _REQUEST_TIMEOUT) -> Any:
    return await asyncio.to_thread(_get_json, url, params, timeout)


async def _sleep_or_stop(ctx: CollectorContext, seconds: float) -> bool:
    try:
        await asyncio.wait_for(ctx.stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


# --------------------------------------------------------------------------- #
# Websocket consumer
# --------------------------------------------------------------------------- #


async def _ws_consumer(
    ctx: CollectorContext,
    url: str,
    label: str,
    state: dict[str, float] | None,
) -> None:
    log = ctx.log(NAME)
    backoff = _RECONNECT_MIN_SECONDS
    # The venue pushes bookTicker on every change (hundreds/sec during a
    # squeeze). The spec's cadence is 100 ms, so keep at most one BBO row per
    # symbol per interval; the next emitted frame carries the latest state.
    last_bbo_mono_ns: dict[str, int] = {}
    while not ctx.stop.is_set():
        try:
            async with connect(url, open_timeout=15, ping_interval=20, ping_timeout=20, max_size=2**24) as ws:
                ctx.health.incr(NAME, f"ws_connect_{label}")
                ctx.health.gauge(NAME, f"ws_connected_{label}", 1.0)
                log.info(f"{label} websocket connected")
                backoff = _RECONNECT_MIN_SECONDS
                async for raw in ws:
                    recv_ts_ns = time.time_ns()
                    recv_mono_ns = time.perf_counter_ns()
                    try:
                        handled = handle_frame(raw, recv_ts_ns, recv_mono_ns)
                    except Exception as exc:  # a single bad frame must not drop the socket
                        ctx.health.incr(NAME, f"frame_errors_{label}")
                        log.warning(f"{label} bad frame: {exc}")
                        continue
                    if handled is None:
                        continue
                    table, row = handled
                    if table == "binance_bbo":
                        symbol = str(row.get("symbol") or "")
                        previous = last_bbo_mono_ns.get(symbol, 0)
                        if recv_mono_ns - previous < _BBO_MIN_INTERVAL_NS:
                            ctx.health.incr(NAME, "bbo_throttled")
                            continue
                        last_bbo_mono_ns[symbol] = recv_mono_ns
                    ctx.emit(table, row)
                    ctx.health.incr(NAME, f"rows_{table}")
                    if state is not None and table == "binance_depth":
                        state["last_depth_mono_ns"] = recv_mono_ns
        except asyncio.CancelledError:
            ctx.health.gauge(NAME, f"ws_connected_{label}", 0.0)
            raise
        except Exception as exc:
            ctx.health.incr(NAME, f"ws_errors_{label}")
            ctx.health.gauge(NAME, f"ws_connected_{label}", 0.0)
            log.warning(f"{label} websocket error: {type(exc).__name__}: {exc}; retry in {backoff:.0f}s")
        else:
            ctx.health.gauge(NAME, f"ws_connected_{label}", 0.0)
            log.warning(f"{label} websocket closed; retry in {backoff:.0f}s")
        if await _sleep_or_stop(ctx, backoff):
            return
        backoff = min(backoff * 2, _RECONNECT_MAX_SECONDS)


# --------------------------------------------------------------------------- #
# REST pollers
# --------------------------------------------------------------------------- #


async def _rest_poller(
    ctx: CollectorContext,
    *,
    label: str,
    url: str,
    path: str,
    symbols: list[str],
    interval: float,
    table: str,
    parser: Callable[..., dict[str, Any] | None],
) -> None:
    log = ctx.log(NAME)
    endpoint = url + path
    while not ctx.stop.is_set():
        for symbol in symbols:
            if ctx.stop.is_set():
                return
            try:
                payload = await _fetch_json(endpoint, {"symbol": symbol})
                row = parser(payload, time.time_ns(), time.perf_counter_ns())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                ctx.health.incr(NAME, f"{label}_errors")
                log.warning(f"{label} {symbol} failed: {type(exc).__name__}: {exc}")
                continue
            if row is None:
                ctx.health.incr(NAME, f"{label}_empty")
                continue
            ctx.emit(table, row)
            ctx.health.incr(NAME, f"rows_{table}")
        if await _sleep_or_stop(ctx, interval):
            return


async def _depth_backstop(
    ctx: CollectorContext,
    *,
    rest_url: str,
    symbols: list[str],
    levels: int,
    interval: float,
    stale_after: float,
    state: dict[str, float],
) -> None:
    """Poll ``/fapi/v1/depth`` only while the websocket depth is silent."""
    log = ctx.log(NAME)
    endpoint = rest_url + "/fapi/v1/depth"
    while not ctx.stop.is_set():
        if await _sleep_or_stop(ctx, interval):
            return
        age = (time.perf_counter_ns() - state["last_depth_mono_ns"]) / 1e9
        if age < stale_after:
            if state.get("backstop_active"):
                state["backstop_active"] = 0.0
                log.info("websocket depth resumed; REST depth backstop idle")
            continue
        if not state.get("backstop_active"):
            state["backstop_active"] = 1.0
            log.warning(
                f"websocket depth silent for {age:.0f}s; "
                f"polling {endpoint} every {interval:.0f}s until it resumes"
            )
        for symbol in symbols:
            if ctx.stop.is_set():
                return
            try:
                payload = await _fetch_json(endpoint, {"symbol": symbol, "limit": levels})
                row = parse_rest_depth(payload, symbol, time.time_ns(), time.perf_counter_ns())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                ctx.health.incr(NAME, "depth_backstop_errors")
                log.warning(f"depth backstop {symbol} failed: {type(exc).__name__}: {exc}")
                continue
            if row is None:
                continue
            ctx.emit("binance_depth", row)
            ctx.health.incr(NAME, "rows_binance_depth")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def run(ctx: CollectorContext) -> None:
    cfg = ctx.section(NAME)
    log = ctx.log(NAME)

    for table, schema in TABLES:
        ctx.register_table(table, schema)

    symbols = [str(s).upper() for s in (cfg.get("symbols") or DEFAULT_SYMBOLS)]
    levels = max(1, int(cfg.get("depth_levels", 10)))
    configured_interval = int(cfg.get("depth_interval_ms", 1000))
    interval_ms = depth_interval_ms(configured_interval)
    funding_seconds = max(1.0, float(cfg.get("funding_poll_seconds", 60)))
    oi_seconds = max(1.0, float(cfg.get("oi_poll_seconds", 60)))
    backstop_after = float(cfg.get("depth_rest_fallback_seconds", 20))
    backstop_interval = max(1.0, interval_ms / 1000.0)

    ws_base = futures_ws_url(str(cfg.get("ws_url") or DEFAULT_WS))
    rest_base = futures_rest_url(str(cfg.get("rest_url") or DEFAULT_REST))

    if interval_ms != configured_interval:
        log.warning(
            f"depth_interval_ms={configured_interval} is not a futures update speed; using {interval_ms}ms"
        )
    log.info(
        f"binance: {len(symbols)} symbols, depth {levels}@{interval_ms}ms, "
        f"ws={ws_base}, rest={rest_base}"
    )

    state: dict[str, float] = {
        "last_depth_mono_ns": float(time.perf_counter_ns()),
        "backstop_active": 0.0,
    }

    tasks = [
        asyncio.create_task(
            _ws_consumer(
                ctx,
                combined_url(ws_base, stream_names(symbols, levels, interval_ms)),
                "market",
                state,
            )
        ),
        asyncio.create_task(_ws_consumer(ctx, combined_url(ws_base, [LIQUIDATION_STREAM]), "liquidations", None)),
        asyncio.create_task(
            _rest_poller(
                ctx,
                label="funding",
                url=rest_base,
                path="/fapi/v1/premiumIndex",
                symbols=symbols,
                interval=funding_seconds,
                table="binance_funding",
                parser=parse_premium_index,
            )
        ),
        asyncio.create_task(
            _rest_poller(
                ctx,
                label="open_interest",
                url=rest_base,
                path="/fapi/v1/openInterest",
                symbols=symbols,
                interval=oi_seconds,
                table="binance_oi",
                parser=parse_open_interest,
            )
        ),
    ]
    if backstop_after > 0:
        tasks.append(
            asyncio.create_task(
                _depth_backstop(
                    ctx,
                    rest_url=rest_base,
                    symbols=symbols,
                    levels=levels,
                    interval=backstop_interval,
                    stale_after=backstop_after,
                    state=state,
                )
            )
        )

    stop_task = asyncio.create_task(ctx.stop.wait())
    try:
        await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop_task.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, stop_task, return_exceptions=True)
    log.info("binance stream stopped")
