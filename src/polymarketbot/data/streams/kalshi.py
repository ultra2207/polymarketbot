"""Kalshi public market tape: BBO snapshots and trade prints.

The Kalshi WebSocket requires an API key (anonymous subscriptions return HTTP
401), so this stream is REST polling only.

Why it polls a *mapped* subset
------------------------------
``GET /markets?status=open`` has > 20,000 open markets and Kalshi caps a page
at 1,000 (limit=2,000 returns HTTP 400). A 1,000-market page is ~5 MB, so the
first version -- three pages every poll, capped at 3,000 markets -- was 48 MB
per poll and, because the page order is stable, only ever captured the
``KXMVECROSSCATEGORY`` parlay block at the front of the list. The real
single-event markets (elections, indices, sports) were never fetched.

The stream therefore polls the tickers listed in the Polymarket -> Kalshi
mapping (``data/derived/kalshi_mapping.parquet``, built by
``python -m polymarketbot.data.kalshi_mapping``) in batches of 100: one ``tickers=``
request returns 100 markets in ~480 KB, i.e. ~0.5 MB per poll instead of 48 MB.
A full-library sweep still exists as a fallback when the mapping is missing.

Why it rotates edges
--------------------
``api.elections.kalshi.com`` answers with four A records. On this host two of
them accept the TCP handshake and then reset the TLS handshake
(``ConnectionResetError WinError 10054``); the Windows resolver returns the
same address order every time, so a pinned bad edge fails 100% of polls until
the DNS TTL expires. The stream probes the candidates, pins a working one,
marks a reset edge as bad for :data:`EDGE_TTL_SECONDS` and retries on the next
candidate with jittered backoff.

Prices have two encodings in the wild: legacy integer cents (``55``) and
current dollar strings (``"0.5500"``). Both normalise to probability with the
same rule -- divide by 100 only when the magnitude exceeds one.
"""

from __future__ import annotations

import asyncio
import random
import socket
import ssl
import time
from collections import deque
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import requests
import urllib3
from urllib3.connection import HTTPSConnection
from urllib3.connection import SocketTimeout
from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError
from urllib3.util import connection as urllib3_connection
from urllib3.util.retry import Retry

from polymarketbot.data.context import CollectorContext
from polymarketbot.data.kalshi_mapping import load_mapping
from polymarketbot.data.schema import KALSHI_MARKETS, KALSHI_TRADES

NAME = "kalshi"

PRICE_FIELDS = ("yes_bid", "yes_ask", "last_price")
CSV_FIELDS = ("volume", "volume_24h", "open_interest", "liquidity")

#: Kalshi rejects ``limit`` above 1,000 with HTTP 400.
MAX_PAGE_SIZE = 1000
#: Tickers per ``tickers=`` request. 100 measured at ~480 KB / 0.3 s.
DEFAULT_BATCH_SIZE = 100
#: How long a resetting edge stays excluded after a 10054.
EDGE_TTL_SECONDS = 300.0
DEFAULT_TIMEOUT_SECONDS = 30.0
#: Bounds on the failure backoff so an outage never turns into a hot loop.
MAX_BACKOFF_SECONDS = 300.0

USER_AGENT = "polymarketbot-tape/0.1 (+data collector)"


# --------------------------------------------------------------------- values
def _first(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _probability(value: Any) -> float | None:
    """Normalise a Kalshi price to 0..1.

    Legacy values are integer cents (``55``), current values are dollars
    (``"0.5500"``). Anything with magnitude above one is treated as cents.
    """
    number = _number(value)
    if number is None:
        return None
    if abs(number) > 1.0:
        return number / 100.0
    return number


def _field(raw: dict[str, Any], name: str, suffix: str) -> Any:
    return _first(raw, name, f"{name}{suffix}")


def parse_market(raw: dict[str, Any], recv_ts_ns: int, recv_mono_ns: int) -> dict[str, Any]:
    """Convert one Kalshi market object into a ``kalshi_markets`` row.

    Accepts both the legacy schema (``yes_bid``/``volume``) and the current one
    (``yes_bid_dollars``/``volume_fp``). Missing values stay ``None``.
    """
    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "ticker": raw.get("ticker"),
        "event_ticker": raw.get("event_ticker"),
        "series_ticker": raw.get("series_ticker"),
        "title": raw.get("title"),
        "yes_bid": _probability(_field(raw, "yes_bid", "_dollars")),
        "yes_ask": _probability(_field(raw, "yes_ask", "_dollars")),
        "last_price": _probability(_field(raw, "last_price", "_dollars")),
        "volume": _number(_field(raw, "volume", "_fp")),
        "volume_24h": _number(_field(raw, "volume_24h", "_fp")),
        "open_interest": _number(_field(raw, "open_interest", "_fp")),
        "liquidity": _number(_field(raw, "liquidity", "_dollars")),
        "close_time": raw.get("close_time"),
        "status": raw.get("status"),
        # Deliberately not stored: a full Kalshi market object is ~16 KB and a
        # 60 s heartbeat over 3,000 markets would write ~48 MB/min. Every field
        # the tape needs is parsed above; raw_json stays available for
        # debugging by re-fetching the ticker.
        "raw_json": None,
    }


def parse_trade(raw: dict[str, Any], recv_ts_ns: int, recv_mono_ns: int) -> dict[str, Any]:
    """Convert one Kalshi trade object into a ``kalshi_trades`` row."""
    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "trade_id": raw.get("trade_id"),
        "ticker": raw.get("ticker"),
        "created_time": raw.get("created_time"),
        "yes_price": _probability(_field(raw, "yes_price", "_dollars")),
        "no_price": _probability(_field(raw, "no_price", "_dollars")),
        "count": _number(_field(raw, "count", "_fp")),
        "taker_side": raw.get("taker_side"),
        "taker_book_side": raw.get("taker_book_side"),
        "is_block_trade": raw.get("is_block_trade"),
    }


def market_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    """The tuple whose change makes a market worth recording.

    Volume and open interest change on almost every poll and would turn a
    3,000-market snapshot into ~7.5k rows/minute. They are still written on
    every emitted row; only the BBO/last price triggers an emission.
    """
    return (
        row.get("yes_bid"),
        row.get("yes_ask"),
        row.get("last_price"),
    )


class MarketChangeDetector:
    """Emit changed markets plus a periodic full snapshot.

    State is keyed by ticker and replaced on every poll, so markets that leave
    the open set stop consuming memory.
    """

    def __init__(self, heartbeat_seconds: float = 300.0) -> None:
        self.heartbeat_seconds = float(heartbeat_seconds)
        self._signatures: dict[str, tuple[Any, ...]] = {}
        self._last_heartbeat: float | None = None

    def select(self, rows: list[dict[str, Any]], now_mono: float) -> list[dict[str, Any]]:
        signatures = {
            str(row.get("ticker")): market_signature(row)
            for row in rows
            if row.get("ticker")
        }
        heartbeat_due = (
            self._last_heartbeat is None
            or now_mono - self._last_heartbeat >= self.heartbeat_seconds
        )
        if heartbeat_due:
            selected = list(rows)
            self._last_heartbeat = now_mono
        else:
            selected = [
                row
                for row in rows
                if row.get("ticker")
                and self._signatures.get(str(row["ticker"])) != market_signature(row)
            ]
        self._signatures = signatures
        return selected

    @property
    def tracked(self) -> int:
        return len(self._signatures)


class TradeDeduper:
    """Remember the last N trade ids so overlapping windows are not re-written.

    ``GET /markets/trades`` is newest-first with no since-cursor, so consecutive
    polls overlap. Ids are retained in insertion order and evicted oldest-first.
    """

    def __init__(self, capacity: int = 20_000) -> None:
        self.capacity = int(capacity)
        self._order: deque[str] = deque()
        self._seen: set[str] = set()

    def __len__(self) -> int:
        return len(self._seen)

    def accept(self, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        fresh: list[dict[str, Any]] = []
        for row in rows:
            trade_id = row.get("trade_id")
            key = str(trade_id) if trade_id else None
            if key is not None and key in self._seen:
                continue
            if key is not None:
                self._seen.add(key)
                self._order.append(key)
                while len(self._order) > self.capacity:
                    self._seen.discard(self._order.popleft())
            fresh.append(row)
        return fresh


# ------------------------------------------------------------------ transport
def backoff_delay(
    attempt: int,
    *,
    base: float = 0.5,
    cap: float = MAX_BACKOFF_SECONDS,
    rng: Callable[[], float] = random.random,
) -> float:
    """Full-jitter exponential backoff.

    ``attempt`` is 1-based: 0.5 s +- jitter, 1 s +- jitter, ... capped. Jitter
    is what stops two streams from retrying in lockstep after a venue reset.
    """
    ceiling = min(float(cap), float(base) * (2 ** max(0, int(attempt) - 1)))
    return max(0.0, ceiling * (0.5 + 0.5 * float(rng())))


def plan_ticker_batches(
    tickers: Sequence[str], batch_size: int = DEFAULT_BATCH_SIZE
) -> list[list[str]]:
    """Deduplicate and chunk tickers, preserving order."""
    size = max(1, int(batch_size))
    ordered: list[str] = []
    seen: set[str] = set()
    for ticker in tickers:
        if not ticker:
            continue
        text = str(ticker)
        if text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return [ordered[i : i + size] for i in range(0, len(ordered), size)]


def _resolve_ipv4(host: str) -> list[str]:
    """Every A record for ``host`` in resolver order."""
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return []
    ordered: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ordered:
            ordered.append(ip)
    return ordered


def probe_tls(host: str, ip: str, timeout: float = 4.0) -> bool:
    """True when a TLS handshake to ``ip`` with ``host``'s SNI completes.

    A TCP probe is not enough on this host: the bad edges accept the SYN and
    then reset during the handshake (WinError 10054), which a TCP-only check
    happily reports as healthy.
    """
    context = ssl.create_default_context()
    try:
        with socket.create_connection((ip, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                return tls.version() is not None
    except OSError:
        return False


class EdgePool:
    """Resolver candidates for one host plus which ones recently reset us."""

    def __init__(
        self,
        host: str,
        *,
        ttl: float = EDGE_TTL_SECONDS,
        resolver: Callable[[str], list[str]] = _resolve_ipv4,
        probe: Callable[[str, str], bool] = probe_tls,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host = host
        self.ttl = float(ttl)
        self._resolver = resolver
        self._probe = probe
        self._clock = clock
        self._resolved: tuple[float, list[str]] | None = None
        self.bad: dict[str, float] = {}
        self.good: dict[str, float] = {}
        self.order: list[str] = []

    def candidates(self, *, force: bool = False) -> list[str]:
        now = self._clock()
        if force or self._resolved is None or now >= self._resolved[0]:
            resolved = list(self._resolver(self.host))
            self._resolved = (now + self.ttl, resolved)
            self.order = resolved
        return list(self._resolved[1])

    def healthy(self) -> list[str]:
        """Candidates ordered: recently good first, recently-reset last."""
        now = self._clock()
        known = self.candidates()
        if not known:
            return []
        excluded = {
            ip for ip, until in self.bad.items() if until > now and ip in known
        }
        usable = [ip for ip in known if ip not in excluded]
        if not usable:
            # Every edge reset us: forget the exclusions rather than going dark
            # for the whole TTL. A fresh handshake is the only way to tell.
            self.bad.clear()
            usable = list(known)
        usable.sort(key=lambda ip: (-self.good.get(ip, 0.0), known.index(ip)))
        return usable

    def mark_bad(self, ip: str | None) -> None:
        if ip:
            self.bad[str(ip)] = self._clock() + self.ttl
            self.good.pop(str(ip), None)

    def mark_good(self, ip: str | None) -> None:
        if ip:
            self.good[str(ip)] = self._clock()
            self.bad.pop(str(ip), None)

    def first_reachable(self) -> str | None:
        """Probe candidates until one completes a TLS handshake."""
        for ip in self.healthy():
            if self._probe(self.host, ip):
                self.mark_good(ip)
                return ip
            self.mark_bad(ip)
        return None


def _pinned_connection_class(ip: str) -> type[HTTPSConnection]:
    """An ``HTTPSConnection`` that dials ``ip`` but keeps the real SNI/Host."""

    class _Pinned(HTTPSConnection):
        pinned_ip = ip

        def _new_conn(self) -> socket.socket:
            try:
                return urllib3_connection.create_connection(
                    (self.pinned_ip, self.port),
                    self.timeout,
                    source_address=self.source_address,
                    socket_options=self.socket_options,
                )
            except socket.gaierror as exc:
                raise NameResolutionError(self.host, self, exc) from exc
            except SocketTimeout as exc:
                raise ConnectTimeoutError(
                    self, f"Connection to {self.host} timed out."
                ) from exc
            except OSError as exc:
                raise NewConnectionError(
                    self, f"Failed to establish a new connection: {exc}"
                ) from exc

    return _Pinned


class _PinnedPoolManager(urllib3.PoolManager):
    def __init__(self, connection_cls: type[HTTPSConnection], **kwargs: Any) -> None:
        self._connection_cls = connection_cls
        super().__init__(**kwargs)

    def _new_pool(self, scheme, host, port, request_context=None):  # noqa: ANN001
        pool = super()._new_pool(scheme, host, port, request_context)
        pool.ConnectionCls = self._connection_cls
        return pool


class _PinnedAdapter(requests.adapters.HTTPAdapter):
    """Route every connection for this session to one specific edge IP."""

    def __init__(self, ip: str, **kwargs: Any) -> None:
        self._pinned_ip = ip
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=None, **pool_kwargs):  # noqa: ANN001
        if block is None:
            block = requests.adapters.DEFAULT_POOLBLOCK
        self.poolmanager = _PinnedPoolManager(
            _pinned_connection_class(self._pinned_ip),
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )


def _new_session(ip: str, *, retry_total: int = 2, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> requests.Session:
    """A session pinned to ``ip`` with transport-level retries and jitter."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    retry = Retry(
        total=retry_total,
        connect=retry_total,
        read=retry_total,
        backoff_factor=0.4,
        backoff_jitter=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = _PinnedAdapter(ip, max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session._kalshi_edge_ip = ip  # type: ignore[attr-defined]
    return session


@dataclass
class ClientStats:
    requests: int = 0
    retries: int = 0
    resets: int = 0


class KalshiClient:
    """Pinned HTTP client with edge rotation and jittered retries.

    A connection error means the pinned edge is unusable: the edge is excluded
    for :data:`EDGE_TTL_SECONDS`, the session is rebuilt against the next
    candidate and the request is retried. HTTP-level throttling (429/5xx) backs
    off without blaming the edge.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        retry_total: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = MAX_BACKOFF_SECONDS,
        edges: EdgePool | None = None,
        session_factory: Callable[[str], requests.Session] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        from urllib.parse import urlparse

        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.retry_total = max(1, int(retry_total))
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self.edges = edges or EdgePool(urlparse(self.base_url).hostname or "")
        self.stats = ClientStats()
        self._session_factory = session_factory or (
            lambda ip: _new_session(ip, timeout=self.timeout)
        )
        self._sleep = sleep
        self._rng = rng
        self._session: requests.Session | None = None
        self._edge: str | None = None

    # -- session -----------------------------------------------------------
    def _close_session(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
        self._session = None
        self._edge = None

    def _open_session(self) -> requests.Session | None:
        if self._session is not None:
            return self._session
        ip = self.edges.first_reachable()
        if ip is None:
            return None
        self._session = self._session_factory(ip)
        self._edge = ip
        return self._session

    def close(self) -> None:
        self._close_session()

    # -- requests ----------------------------------------------------------
    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last: Exception | None = None
        for attempt in range(1, self.retry_total + 2):
            session = self._open_session()
            if session is None:
                last = ConnectionError(
                    f"no reachable Kalshi edge for {self.edges.host}"
                )
            else:
                try:
                    response = session.get(url, params=params, timeout=self.timeout)
                    if response.status_code == 429 or response.status_code >= 500:
                        raise requests.HTTPError(
                            f"HTTP {response.status_code}", response=response
                        )
                    response.raise_for_status()
                    self.stats.requests += 1
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError(
                            f"unexpected Kalshi payload: {type(payload).__name__}"
                        )
                    self.edges.mark_good(self._edge)
                    return payload
                except (
                    requests.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                    urllib3.exceptions.ProtocolError,
                ) as exc:
                    # Transport failure: the pinned edge reset us (10054) or the
                    # handshake never completed. Blame the edge and rotate.
                    self.stats.resets += 1
                    self.edges.mark_bad(self._edge)
                    self._close_session()
                    last = exc
                except (requests.RequestException, ValueError) as exc:
                    # Reachable but unhappy (429/5xx after transport retries,
                    # bad JSON): back off, keep the session and the edge.
                    self.stats.retries += 1
                    last = exc

            if attempt > self.retry_total:
                break
            self.stats.retries += 1
            self._sleep(
                backoff_delay(
                    attempt,
                    base=self.backoff_base,
                    cap=self.backoff_cap,
                    rng=self._rng,
                )
            )
        assert last is not None
        raise last


# -------------------------------------------------------------------- polling
def parse_markets_payload(
    payload: dict[str, Any], recv_ts_ns: int, recv_mono_ns: int
) -> tuple[list[dict[str, Any]], str | None]:
    """Split a ``/markets`` page into items plus the next cursor."""
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        raise ValueError(f"unexpected Kalshi markets payload: {type(payload).__name__}")
    items = [
        {"raw": raw, "recv_ts_ns": recv_ts_ns, "recv_mono_ns": recv_mono_ns}
        for raw in payload["markets"]
        if isinstance(raw, dict)
    ]
    cursor = payload.get("cursor")
    return items, (str(cursor) if cursor else None)


def parse_trades_payload(
    payload: dict[str, Any], recv_ts_ns: int, recv_mono_ns: int
) -> tuple[list[dict[str, Any]], str | None]:
    """Split a ``/markets/trades`` page into rows plus the next cursor."""
    if not isinstance(payload, dict) or not isinstance(payload.get("trades"), list):
        raise ValueError(f"unexpected Kalshi trades payload: {type(payload).__name__}")
    rows = [
        parse_trade(raw, recv_ts_ns, recv_mono_ns)
        for raw in payload["trades"]
        if isinstance(raw, dict)
    ]
    cursor = payload.get("cursor")
    return rows, (str(cursor) if cursor else None)


def poll_tickers(
    client: KalshiClient,
    tickers: Sequence[str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[dict[str, Any]]:
    """Fetch the mapped tickers, one ``tickers=`` request per batch."""
    collected: list[dict[str, Any]] = []
    for batch in plan_ticker_batches(tickers, batch_size):
        payload = client.get_json(
            "/markets", {"tickers": ",".join(batch), "limit": MAX_PAGE_SIZE}
        )
        items, _ = parse_markets_payload(
            payload, time.time_ns(), time.perf_counter_ns()
        )
        collected.extend(items)
    return collected


def poll_status(
    client: KalshiClient,
    status: str,
    *,
    max_markets: int,
    page_size: int = MAX_PAGE_SIZE,
    max_pages: int = 50,
    min_close_days: float = 14.0,
) -> list[dict[str, Any]]:
    """Page through ``/markets?status=...`` (fallback when no mapping exists).

    ``min_close_ts`` is what makes this fallback useful: the first 3,000 open
    markets in page order are all ``KXMVECROSSCATEGORY`` parlays, which close
    within days. Anything closing more than ``min_close_days`` out is a real
    single-event market.
    """
    collected: list[dict[str, Any]] = []
    cursor: str | None = None
    min_close_ts = int(time.time() + max(0.0, float(min_close_days)) * 86_400)
    for _ in range(max(1, int(max_pages))):
        size = min(MAX_PAGE_SIZE, max(1, int(page_size)), max(1, max_markets - len(collected)))
        params: dict[str, Any] = {
            "limit": size,
            "status": status,
            "min_close_ts": min_close_ts,
        }
        if cursor:
            params["cursor"] = cursor
        payload = client.get_json("/markets", params)
        items, next_cursor = parse_markets_payload(
            payload, time.time_ns(), time.perf_counter_ns()
        )
        collected.extend(items)
        if len(collected) >= max_markets:
            return collected[:max_markets]
        if not next_cursor or not items or next_cursor == cursor:
            break
        cursor = next_cursor
    return collected


def filter_trades(
    rows: Sequence[dict[str, Any]], tickers: Sequence[str] | set[str]
) -> list[dict[str, Any]]:
    """Keep only prints for tickers the collector follows.

    ``/markets/trades`` filters by a single ``ticker=``; ``tickers=`` is
    ignored, so the stream reads the global window and discards the rest. One
    page of 1,000 prints is only 1.0-5.1 s of tape (measured 226 prints/s);
    ``poll_trades`` walks the cursor from the last ``min_ts`` so a slow cycle
    does not drop prints.
    """
    wanted = set(tickers)
    if not wanted:
        return list(rows)
    return [row for row in rows if row.get("ticker") in wanted]


def _iso_to_epoch(value: Any) -> float | None:
    """Parse a Kalshi ``created_time`` (ISO-8601, UTC) to epoch seconds."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def poll_trades(
    client: KalshiClient,
    *,
    limit: int = MAX_PAGE_SIZE,
    min_ts: int | None = None,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """Trade prints across every open market, newest first, with catch-up.

    One page of 1,000 prints is only 1.0-5.1 s of the global tape (measured
    226 prints/s), so a fixed-cadence poll drops prints. This walks the
    ``cursor`` until a page starts before ``min_ts`` (the newest print of the
    previous poll), which keeps the feed contiguous across a slow cycle.
    """
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _page in range(max(1, int(max_pages))):
        params: dict[str, Any] = {"limit": max(1, int(limit))}
        if min_ts is not None:
            params["min_ts"] = int(min_ts)
        if cursor:
            params["cursor"] = cursor
        payload = client.get_json("/markets/trades", params)
        page, cursor = parse_trades_payload(
            payload, time.time_ns(), time.perf_counter_ns()
        )
        rows.extend(page)
        if not page or not cursor:
            break
        oldest = min(
            (ts for ts in (_iso_to_epoch(row.get("created_time")) for row in page) if ts is not None),
            default=None,
        )
        if min_ts is not None and oldest is not None and oldest <= float(min_ts):
            break
    return rows


class MappingCache:
    """Re-read the mapping artifact when it changes, without polling disk hot."""

    def __init__(
        self,
        path: Path | str,
        *,
        refresh_seconds: float = 900.0,
        loader: Callable[[Path | str], tuple[str, ...]] = load_mapping,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = Path(path)
        self.refresh_seconds = float(refresh_seconds)
        self._loader = loader
        self._clock = clock
        self._tickers: tuple[str, ...] = ()
        # st_mtime alone collides on filesystems whose reported precision is
        # coarser than two consecutive writes (observed on NTFS: the file cache
        # kept serving the previous tuple after an in-place rewrite). The size
        # disambiguates same-timestamp rewrites.
        self._signature: tuple[int, int] | None = None
        self._next_check = 0.0

    def get(self) -> tuple[str, ...]:
        now = self._clock()
        if now < self._next_check:
            return self._tickers
        self._next_check = now + self.refresh_seconds
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._tickers = ()
            self._signature = None
            return self._tickers
        if self._signature != signature:
            self._tickers = tuple(self._loader(self.path))
            self._signature = signature
        return self._tickers


# ----------------------------------------------------------------------- run
async def run(ctx: CollectorContext) -> None:
    cfg = ctx.section(NAME)
    log = ctx.log(NAME)
    ctx.register_table("kalshi_markets", KALSHI_MARKETS)
    ctx.register_table("kalshi_trades", KALSHI_TRADES)

    base_url = str(cfg.get("rest_url", "https://api.elections.kalshi.com/trade-api/v2"))
    poll_seconds = max(1.0, float(cfg.get("poll_seconds", 15)))
    # The global trade window is 1,000 prints ≈ 9.6 s at the measured rate, so
    # the poll must be faster than that or prints are lost between windows.
    trades_poll_seconds = max(2.0, float(cfg.get("trades_poll_seconds", 8)))
    trades_scope = str(cfg.get("trades_scope", "mapped"))
    batch_size = max(1, int(cfg.get("batch_size", DEFAULT_BATCH_SIZE)))
    max_markets = max(1, int(cfg.get("max_markets", 3000)))
    status = str(cfg.get("status", "open"))
    heartbeat_seconds = float(cfg.get("heartbeat_seconds", 300.0))
    discovery_seconds = float(cfg.get("discovery_seconds", 600.0))
    max_backoff = min(MAX_BACKOFF_SECONDS, float(cfg.get("max_backoff_seconds", 60.0)))
    mapping_path = Path(str(cfg.get("mapping_path", "data/derived/kalshi_mapping.parquet")))

    mapping = MappingCache(
        mapping_path, refresh_seconds=float(cfg.get("mapping_refresh_seconds", 900.0))
    )
    detector = MarketChangeDetector(heartbeat_seconds=heartbeat_seconds)
    deduper = TradeDeduper()
    last_trade_ts = int(time.time())

    client = KalshiClient(
        base_url,
        timeout=float(cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        retry_total=int(cfg.get("retry_total", 3)),
        backoff_base=float(cfg.get("backoff_base_seconds", 0.5)),
        backoff_cap=max_backoff,
    )

    started = time.monotonic()
    last_ok: float | None = None
    last_discovery: float | None = None
    failure_streak = 0
    tickers = mapping.get()

    log.info(
        f"kalshi poll: {base_url} every {poll_seconds:.0f}s "
        f"(trades {trades_poll_seconds:.0f}s), "
        f"{len(tickers)} mapped tickers in batches of {batch_size}"
        if tickers
        else f"kalshi poll: {base_url} every {poll_seconds:.0f}s, "
        f"no mapping at {mapping_path} -> full {status} sweep "
        f"(max {max_markets}) every {max(poll_seconds, discovery_seconds):.0f}s"
    )

    next_bbo = 0.0
    next_trades = 0.0
    try:
        while not ctx.stop.is_set():
            now = time.monotonic()

            if now >= next_bbo:
                tickers = mapping.get()
                interval = poll_seconds * (2 ** min(failure_streak, 4))
                next_bbo = now + min(max_backoff, interval)
                try:
                    polled = True
                    if tickers:
                        fetched = await asyncio.to_thread(
                            poll_tickers, client, tickers, batch_size=batch_size
                        )
                    elif last_discovery is None or now - last_discovery >= discovery_seconds:
                        fetched = await asyncio.to_thread(
                            poll_status,
                            client,
                            status,
                            max_markets=max_markets,
                            max_pages=int(cfg.get("max_pages", 50)),
                            min_close_days=float(cfg.get("min_close_days", 14.0)),
                        )
                        last_discovery = time.monotonic()
                    else:
                        # Between discovery sweeps: nothing to fetch this tick.
                        polled = False
                        fetched = []

                    if polled:
                        last_ok = time.monotonic()
                        failure_streak = 0
                    rows = [
                        parse_market(item["raw"], item["recv_ts_ns"], item["recv_mono_ns"])
                        for item in fetched
                    ]
                    selected = detector.select(rows, time.perf_counter())
                    for row in selected:
                        ctx.emit("kalshi_markets", row)
                    ctx.health.incr(NAME, "markets_polled", len(rows))
                    ctx.health.incr(NAME, "rows_emitted", len(selected))
                    ctx.health.gauge(NAME, "mapping_tickers", float(len(tickers)))
                    log.debug(
                        f"kalshi poll ok: {len(rows)} markets, {len(selected)} rows, "
                        f"{detector.tracked} tracked, edge={client._edge}"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep the stream alive
                    failure_streak += 1
                    ctx.health.incr(NAME, "errors")
                    log.warning(
                        f"kalshi poll failed ({failure_streak} in a row): "
                        f"{type(exc).__name__}: {exc}"
                    )

            now = time.monotonic()
            if now >= next_trades:
                next_trades = now + trades_poll_seconds * (2 ** min(failure_streak, 4))
                try:
                    trades = await asyncio.to_thread(
                        poll_trades,
                        client,
                        min_ts=last_trade_ts,
                        max_pages=int(cfg.get("trades_max_pages", 10)),
                    )
                    newest = max(
                        (ts for ts in (_iso_to_epoch(r.get("created_time")) for r in trades) if ts is not None),
                        default=None,
                    )
                    if newest is not None:
                        last_trade_ts = int(newest)
                    fresh = deduper.accept(trades)
                    if trades_scope == "mapped" and tickers:
                        fresh = filter_trades(fresh, tickers)
                    for row in fresh:
                        ctx.emit("kalshi_trades", row)
                    ctx.health.incr(NAME, "trades_polled", len(trades))
                    ctx.health.incr(NAME, "trades_emitted", len(fresh))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - trades must not kill BBO
                    ctx.health.incr(NAME, "trade_errors")
                    log.warning(f"kalshi trades poll failed: {type(exc).__name__}: {exc}")

            age = (time.monotonic() - last_ok) if last_ok is not None else (time.monotonic() - started)
            ctx.health.gauge(NAME, "last_poll_age_s", age)
            ctx.health.gauge(NAME, "edge_resets", float(client.stats.resets))
            ctx.health.gauge(NAME, "edge_candidates", float(len(client.edges.order)))
            ctx.health.gauge(NAME, "retries", float(client.stats.retries))

            wake = min(next_bbo, next_trades) - time.monotonic()
            try:
                await asyncio.wait_for(ctx.stop.wait(), timeout=max(0.05, wake))
            except asyncio.TimeoutError:
                pass
    finally:
        client.close()
