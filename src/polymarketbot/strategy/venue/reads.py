"""Read-only venue access: Gamma universe, CLOB books, the public trade tape.

Every field is parsed from the venue payload. A missing field returns ``None``
and the caller refuses the market; nothing is defaulted to a tradable value.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from polymarketbot.fees import (
    FeeSchedule,
    FeeScheduleUnreadable,
    schedule_for_market,
)
from polymarketbot.gamma import GammaClient
from polymarketbot.logs import get_logger

log = get_logger(__name__)

CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"


@dataclass(frozen=True)
class Market:
    condition_id: str
    question: str
    outcomes: tuple[str, ...]
    token_ids: tuple[str, ...]
    tick: float
    min_order: float
    daily_reward: float
    reward_max_spread_cents: float
    reward_min_size: float
    fee_schedule: FeeSchedule | None
    neg_risk: bool
    closed: bool
    uma_status: str
    outcome_prices: tuple[float, ...] = ()
    category: str = "other"
    #: Gamma event grouping, used by the correlated-event risk caps. Empty
    #: when the payload carries no event; the caller then skips the event cap
    #: instead of guessing a grouping.
    event_id: str = ""

    @property
    def binary(self) -> bool:
        return len(self.token_ids) == 2


def session() -> requests.Session:
    http = requests.Session()
    http.headers.update({"User-Agent": "polymarketbot-paper/2"})
    adapter = HTTPAdapter(pool_connections=32, pool_maxsize=64, max_retries=2)
    http.mount("https://", adapter)
    http.mount("http://", adapter)
    return http


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _event_id(row: dict[str, Any]) -> str:
    """The Gamma event grouping from whichever shape this payload carries.

    Gamma rows expose ``events[0]["id"]``; metadata and CLOB rows may carry a
    flat ``event_id``/``eventId``. Nothing is derived: no field means ``""``.
    """
    for key in ("event_id", "eventId"):
        value = row.get(key)
        if value is not None and str(value) != "":
            return str(value)
    events = _json_list(row.get("events"))
    if events and isinstance(events[0], dict):
        value = events[0].get("id") or events[0].get("event_id")
        if value is not None and str(value) != "":
            return str(value)
    return ""


def parse_market(row: dict[str, Any]) -> Market | None:
    condition_id = str(row.get("conditionId") or row.get("condition_id") or "")
    outcomes = tuple(str(o) for o in _json_list(row.get("outcomes")))
    token_ids = tuple(str(t) for t in _json_list(row.get("clobTokenIds")))
    if not condition_id or len(outcomes) < 2 or len(token_ids) != len(outcomes):
        return None
    try:
        tick = float(row.get("orderPriceMinTickSize") or 0.0)
        min_order = float(row.get("orderMinSize") or 0.0)
    except (TypeError, ValueError):
        return None
    if tick <= 0.0 or min_order <= 0.0:
        return None
    daily_reward = 0.0
    for reward in row.get("clobRewards") or []:
        try:
            daily_reward += float(reward.get("rewardsDailyRate") or 0.0)
        except (TypeError, ValueError):
            continue
    prices = []
    for raw in _json_list(row.get("outcomePrices")):
        try:
            prices.append(float(raw))
        except (TypeError, ValueError):
            prices.append(float("nan"))
    # Gamma's per-market schedule is authoritative (docs/fees.md). A payload
    # that cannot be parsed raises FeeScheduleUnreadable; it is never turned
    # into a free market here.
    schedule = schedule_for_market(row)
    if row.get("acceptingOrders") is False or row.get("archived") is True:
        return None
    tags = row.get("tags") or []
    category = str(tags[0].get("label", "other") if tags and isinstance(tags[0], dict) else "other")
    return Market(
        condition_id=condition_id,
        question=str(row.get("question") or ""),
        outcomes=outcomes,
        token_ids=token_ids,
        tick=tick,
        min_order=min_order,
        daily_reward=daily_reward,
        reward_max_spread_cents=float(row.get("rewardsMaxSpread") or 0.0),
        reward_min_size=float(row.get("rewardsMinSize") or 0.0),
        fee_schedule=schedule,
        neg_risk=bool(row.get("negRisk")),
        closed=bool(row.get("closed")),
        uma_status=str(row.get("umaResolutionStatus") or "").lower(),
        outcome_prices=tuple(prices),
        category=category,
        event_id=_event_id(row),
    )


def _parse_row(row: dict[str, Any]) -> Market | None:
    """``parse_market`` that refuses (logs and skips) an unreadable fee schedule.

    Refusing one market must not abort a universe pass: a single malformed
    payload would otherwise pin the keyset cursor, or stop a full scan, while
    the rest of the universe is tradable.
    """
    try:
        return parse_market(row)
    except FeeScheduleUnreadable as exc:
        log.warning(f"market refused: {exc}")
        return None


def universe(gamma: GammaClient, max_pages: int | None = None) -> Iterator[Market]:
    """Bounded walk over the venue keyset; ``max_pages=None`` runs to the end."""
    cursor: str | None = None
    seen: set[str] = set()
    pages = 0
    while True:
        payload = gamma.markets_keyset(closed="false", after_cursor=cursor,
                                       extra={"active": "true"})
        rows = (payload or {}).get("markets") or []
        for row in rows:
            market = _parse_row(row)
            if market is not None:
                yield market
        pages += 1
        next_cursor = (payload or {}).get("next_cursor")
        if not next_cursor or next_cursor in seen:
            return
        if max_pages is not None and pages >= max_pages:
            return
        seen.add(next_cursor)
        cursor = next_cursor


class UniverseWalker:
    """Incremental full-universe scan.

    The venue lists far more active markets than one tick can page through
    (80k+ on 2026-11-10, dominated by short-lived sports and 5-minute crypto
    markets), and ``/markets`` offset search stops at ~2,100 rows. This walker
    advances a fixed number of keyset pages per tick, so every market is
    eventually scanned without blocking a tick and without a universe cap.
    On a completed pass, markets the pass did not see are dropped.
    """

    def __init__(self, gamma: GammaClient, pages_per_tick: int = 20) -> None:
        self._gamma = gamma
        self._pages_per_tick = max(1, int(pages_per_tick))
        self._cursor: str | None = None
        self._known: dict[str, Market] = {}
        self._pass_seen: set[str] = set()
        self._pages = 0
        self._lock = threading.Lock()
        self._complete = False

    @property
    def complete(self) -> bool:
        with self._lock:
            return self._complete

    def snapshot(self) -> list[Market]:
        """Markets discovered so far; safe to call while the walk runs."""
        with self._lock:
            return list(self._known.values())

    def step(self) -> tuple[list[Market], bool]:
        """Advance the walk; returns (known markets, pass completed)."""
        pages = 0
        complete = False
        while pages < self._pages_per_tick:
            payload = self._gamma.markets_keyset(
                closed="false", after_cursor=self._cursor, extra={"active": "true"}
            )
            rows = (payload or {}).get("markets") or []
            parsed: list[Market] = []
            for row in rows:
                market = _parse_row(row)
                if market is not None:
                    parsed.append(market)
            with self._lock:
                for market in parsed:
                    self._known[market.condition_id] = market
                    self._pass_seen.add(market.condition_id)
            pages += 1
            self._pages += 1
            next_cursor = (payload or {}).get("next_cursor")
            if not next_cursor or next_cursor == self._cursor:
                complete = True
                with self._lock:
                    self._known = {
                        cid: market for cid, market in self._known.items()
                        if cid in self._pass_seen
                    }
                    self._pass_seen = set()
                    self._cursor = None
                    self._complete = True
                break
            with self._lock:
                self._cursor = next_cursor
        return self.snapshot(), complete

    def run_forever(self, stop: threading.Event) -> None:
        """Walk continuously off the tick thread.

        Keyset pagination is sequential by construction, so the walk cannot be
        parallelised; taking it off the decision loop is what keeps the tick at
        its interval while the full universe still gets scanned.
        """
        log.info("universe walk started in the background")
        while not stop.is_set():
            try:
                self.step()
            except Exception:
                log.warning("universe walk page failed; retrying", exc_info=True)
                stop.wait(1.0)


class BookFeed:
    """Batch ``POST /books`` with per-token TTL and a rotating fetch window.

    A full-universe tick cannot re-read 100k+ token books. Each call fetches
    the requested window whose cached copy is older than ``ttl``, and
    ``snapshot`` returns every book still inside ``max_age`` so the pillars see
    the widest book set the budget has covered so far. Coverage rotation is the
    caller's cursor; the cache keeps the rest until it ages out.
    """

    def __init__(self, http: requests.Session, ttl: float = 3.0, chunk: int = 400,
                 workers: int = 16, max_age: float = 120.0) -> None:
        self._http = http
        self._ttl = float(ttl)
        self._chunk = int(chunk)
        self._workers = int(workers)
        self._max_age = float(max_age)
        self._lock = threading.Lock()
        self._book: dict[str, dict[str, Any]] = {}
        self._stamp: dict[str, float] = {}
        #: Wall-clock time of the last successful ``/books`` call (2xx with a
        #: parsed body), or ``None`` until one succeeds. This is the paper
        #: data source's heartbeat: a cached snapshot or a failed fetch does
        #: not advance it, so the risk layer can tell a live path from a stale
        #: cache.
        self.last_ok_ts: float | None = None

    def _fetch(self, token_ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        chunks = [token_ids[i:i + self._chunk] for i in range(0, len(token_ids), self._chunk)]

        def one(chunk: list[str]) -> tuple[bool, dict[str, dict[str, Any]]]:
            try:
                response = self._http.post(
                    f"{CLOB}/books",
                    json=[{"token_id": token} for token in chunk],
                    timeout=10.0,
                )
                if not 200 <= response.status_code < 300:
                    log.warning(f"/books chunk HTTP {response.status_code}")
                    return False, {}
                return True, {
                    str(book.get("asset_id")): book
                    for book in response.json()
                    if book.get("asset_id")
                }
            except Exception as exc:  # noqa: BLE001 - a failed chunk is missing data
                log.warning(f"/books chunk failed: {exc}")
                return False, {}

        ok = False
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            for chunk_ok, partial in pool.map(one, chunks):
                ok = ok or chunk_ok
                out.update(partial)
        if ok:
            with self._lock:
                self.last_ok_ts = time.time()
        return out

    def books(self, token_ids: Iterable[str], force: bool = False) -> dict[str, dict[str, Any]]:
        now = time.time()
        ids = list(dict.fromkeys(t for t in token_ids if t))
        with self._lock:
            stale = [
                token for token in ids
                if force or token not in self._stamp or now - self._stamp[token] >= self._ttl
            ]
        if stale:
            fetched = self._fetch(stale)
            with self._lock:
                self._book.update(fetched)
                stamp = time.time()
                for token in fetched:
                    self._stamp[token] = stamp
        with self._lock:
            return {token: self._book[token] for token in ids if token in self._book}

    def snapshot(self, max_age: float | None = None) -> dict[str, dict[str, Any]]:
        limit = self._max_age if max_age is None else float(max_age)
        cutoff = time.time() - limit
        with self._lock:
            for token in [token for token, stamp in self._stamp.items() if stamp < cutoff]:
                self._book.pop(token, None)
                self._stamp.pop(token, None)
            return dict(self._book)


@dataclass
class Print:
    token_id: str
    condition_id: str
    side: str  # taker side: BUY or SELL
    price: float
    size: float
    ts: float


def _row_ts(row: dict[str, Any]) -> float:
    for key in ("timestamp", "ts", "time"):
        try:
            value = float(row[key])
        except (KeyError, TypeError, ValueError):
            continue
        return value / 1000.0 if value > 1e11 else value
    return 0.0


_TAPE_CACHE: dict[str, tuple[float, list[Print]]] = {}
_TAPE_COOLDOWN: dict[str, float] = {}
_TAPE_LOCK = threading.Lock()
_TAPE_BUDGET = 48
#: Rotating start into the fetch list, so the budget does not permanently
#: starve the tail of a stable, capital-ordered id list.
_TAPE_CURSOR = [0]
_TAPE_TTL = 45.0
_TAPE_STATS = {"fresh": 0, "cached": 0, "limited": 0, "logged": 0.0}
#: Last warning per condition, so a lagging index is reported once per TTL
#: instead of once per quoted market per tick.
_STALE_INDEX_LOGGED: dict[str, float] = {}


def _log_stale_condition_index(condition_id: str, newest: float, cutoff: float) -> None:
    """Say a lagging per-condition index is *missing data*, once per TTL."""
    now = time.time()
    with _TAPE_LOCK:
        if now - _STALE_INDEX_LOGGED.get(condition_id, 0.0) < _TAPE_TTL:
            return
        _STALE_INDEX_LOGGED[condition_id] = now
    lag = max(0.0, cutoff - newest)
    log.warning(
        f"condition tape index behind for {condition_id[:12]}: newest row is "
        f"{lag:.0f}s older than the window cutoff (missing data, not zero "
        f"flow); relying on the global walk"
    )


def _tape_stats(now: float, requested: int) -> None:
    with _TAPE_LOCK:
        if now - _TAPE_STATS["logged"] < 60.0:
            return
        _TAPE_STATS["logged"] = now
        log.info(
            f"condition tape: {_TAPE_STATS['fresh']} fresh, {_TAPE_STATS['cached']} cached, "
            f"{_TAPE_STATS['limited']} rate-limited of {requested} quoted; "
            f"budget={_TAPE_BUDGET}/tick ttl={_TAPE_TTL:.0f}s"
        )
        _TAPE_STATS.update(fresh=0, cached=0, limited=0)


def condition_tape(
    http: requests.Session,
    condition_ids: Iterable[str],
    window_s: float = 180.0,
    page_size: int = 500,
    workers: int = 8,
) -> list[Print]:
    """Prints for the exact conditions we quote, fetched per condition.

    The global ``/v2/trades`` walk is newest-first across every market, so a
    specific quoted token is usually absent from it and its fill model sees no
    flow. ``/v2/trades?condition=`` returns that market's own tape, which is
    what a resting order actually competes against.
    """
    now = time.time()
    cutoff = now - max(1.0, window_s)
    ids = [cid for cid in dict.fromkeys(condition_ids) if cid]
    if not ids:
        return []
    # The Data API rate-limits hard: 2.4k quoted markets is 2.4k requests per
    # tick. Fetch a bounded, rotating slice and serve the rest from a TTL cache,
    # logging the coverage instead of silently truncating the universe.
    with _TAPE_LOCK:
        stale = [
            cid for cid in ids
            if now - _TAPE_CACHE.get(cid, (0.0, []))[0] >= _TAPE_TTL
            and _TAPE_COOLDOWN.get(cid, 0.0) <= now
        ]
        if len(stale) > _TAPE_BUDGET:
            # Round-robin, not a fixed prefix. ``ids`` arrives in the pillar's
            # capital order, which is stable tick to tick, so truncating the
            # list starved every condition past the budget permanently: at 89
            # quoted conditions and a 48 budget the last 41 were never refreshed
            # (measured 2026-11-10 12:53 window, ``1 fresh / 89 quoted`` per
            # minute). Rotating the start means each condition is served within
            # ceil(len/budget) ticks.
            offset = _TAPE_CURSOR[0] % len(stale)
            stale = stale[offset:] + stale[:offset]
            _TAPE_CURSOR[0] = (offset + _TAPE_BUDGET) % len(stale)
        stale = stale[:_TAPE_BUDGET]
        fresh: dict[str, list[Print]] = {
            cid: [p for p in _TAPE_CACHE[cid][1] if p.ts >= cutoff]
            for cid in ids
            if cid in _TAPE_CACHE and _TAPE_CACHE[cid][0] >= now - _TAPE_TTL
        }

    def one(condition_id: str) -> list[Print] | None:
        """Prints for one condition, or ``None`` when the read failed.

        ``None`` and ``[]`` are different answers: an empty list is a measured
        "no prints in the window", while a 429 / non-200 / transport failure is
        *missing data* and must not be cached as a fresh zero.
        """
        try:
            response = http.get(
                f"{DATA_API}/v2/trades",
                params={"condition": condition_id, "limit": int(page_size)},
                timeout=8.0,
            )
            if response.status_code == 429:
                with _TAPE_LOCK:
                    _TAPE_COOLDOWN[condition_id] = time.time() + 30.0
                    _TAPE_STATS["limited"] += 1
                return None
            if response.status_code != 200:
                log.warning(f"condition tape HTTP {response.status_code}")
                return None
            rows = (response.json() or {}).get("data") or []
        except Exception as exc:  # noqa: BLE001 - a failed market is missing data
            log.warning(f"condition tape failed: {exc}")
            return None
        # A page whose newest row is already older than the window is not
        # evidence of "no flow": the per-condition index can lag the websocket
        # tape. Measured 2026-11-10 14:11 for the venue's most active condition
        # (1,152 websocket trades in 26 min): ``?condition=`` returned a page
        # whose newest row was 1,802 s old and was still unchanged 13 minutes
        # later, while the unfiltered feed was 0 s old. Reporting that as zero
        # flow silently discards every fill in the window, so it is logged and
        # the caller is told the measurement is missing rather than empty.
        newest = max((_row_ts(row) for row in rows), default=0.0)
        if rows and newest < cutoff:
            _log_stale_condition_index(condition_id, newest, cutoff)
            return None
        out: list[Print] = []
        for row in rows:
            ts = _row_ts(row)
            if ts <= 0.0 or ts < cutoff:
                continue
            out.append(
                Print(
                    token_id=str(row.get("token_id") or row.get("asset") or ""),
                    condition_id=condition_id,
                    side=str(row.get("side") or "").upper(),
                    price=float(row.get("price") or 0.0),
                    size=float(row.get("size") or 0.0),
                    ts=ts,
                )
            )
        return out

    fetched: dict[str, list[Print]] = {}
    if stale:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(stale)))) as pool:
            for condition_id, rows in zip(stale, pool.map(one, stale)):
                if rows is None:
                    # Leave the cache entry (and its timestamp) untouched so the
                    # next tick retries instead of serving a fabricated zero.
                    continue
                fetched[condition_id] = rows
    with _TAPE_LOCK:
        for condition_id, rows in fetched.items():
            # Cache empty results too: refetching a quiet market every tick is
            # what turns a bounded budget into a rate-limit storm.
            _TAPE_CACHE[condition_id] = (time.time(), rows)
        _TAPE_STATS["fresh"] += sum(1 for rows in fetched.values() if rows)
        _TAPE_STATS["cached"] += len(fresh)
    out = [p for rows in fresh.values() for p in rows if p.ts >= cutoff]
    for rows in fetched.values():
        out.extend(p for p in rows if p.ts >= cutoff)
    _tape_stats(time.time(), len(ids))
    return out


def tape(http: requests.Session, window_s: float = 300.0, max_pages: int = 20,
         page_size: int = 500) -> list[Print]:
    """Newest-first tape walk until the window is covered. Partial tape is measured."""
    cutoff = time.time() - max(1.0, window_s)
    prints: list[Print] = []
    cursor: str | None = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"limit": page_size}
        if cursor:
            params["cursor"] = cursor
        try:
            response = http.get(f"{DATA_API}/v2/trades", params=params, timeout=8.0)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"trade tape failed: {exc}")
            break
        if response.status_code != 200:
            log.warning(f"trade tape HTTP {response.status_code}")
            break
        payload = response.json() or {}
        page = payload.get("data") or []
        oldest = None
        for row in page:
            ts = _row_ts(row)
            if ts <= 0.0:
                continue
            oldest = ts if oldest is None else min(oldest, ts)
            try:
                prints.append(
                    Print(
                        token_id=str(row.get("token_id") or row.get("asset") or ""),
                        condition_id=str(row.get("condition_id") or row.get("conditionId") or ""),
                        side=str(row.get("side") or "").upper(),
                        price=float(row.get("price") or 0.0),
                        size=float(row.get("size") or 0.0),
                        ts=ts,
                    )
                )
            except (TypeError, ValueError):
                continue
        if len(page) < page_size or (oldest is not None and oldest <= cutoff):
            break
        cursor = (payload.get("pagination") or {}).get("next_cursor")
        if not cursor:
            break
    return prints


def ctf_contracts() -> dict[str, str]:
    """Protocol addresses read from config; paper uses them only for labels."""
    from polymarketbot.config import CONTRACTS

    return dict(CONTRACTS)
