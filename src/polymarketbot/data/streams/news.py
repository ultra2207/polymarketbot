"""News tape: publisher RSS/Atom feeds, NewsAPI and Polymarket Gamma comments.

Independent pollers run concurrently, each with its own cadence and its own
error metric, so one dead or throttled source cannot stall the others:

* RSS 2.0 / Atom feeds -- one task per feed, each with its own session, cadence
  and backoff.
* Gamma comments for the highest-reward markets. Gamma rejects
  ``parent_entity_type=Market`` with HTTP 422 (only ``Event``, ``Series`` and
  ``PerpsAsset`` are valid), so a market's condition id is resolved to its
  event id through ``GET /markets?condition_ids=...`` and comments are fetched
  per event. The fallback is logged once.
* NewsAPI.org (optional) -- a documented public wire adapter enabled only when
  ``NEWSAPI_KEY`` is set in the environment. It is skipped silently otherwise.
  AP Wire and Tree News have no public API and are therefore not implemented.

All parsing is pure and unit-tested; only ``_fetch_*`` helpers touch the
network.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import orjson
import requests

from polymarketbot.data.context import CollectorContext
from polymarketbot.data.schema import GAMMA_COMMENTS, NEWS_ITEMS

NAME = "news"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
SUMMARY_LIMIT = 2000
DEDUPE_LIMIT = 50_000

# Shared rate-limit policy: Retry-After wins when present; the exponential
# ladder is capped so a query never sleeps forever, and the circuit breaker
# parks a persistently throttled query instead of hammering it.
BACKOFF_MAX_S = 1800.0
BACKOFF_BREAKER_THRESHOLD = 3
BACKOFF_BREAKER_S = 900.0
BACKOFF_JITTER = 0.25

# NewsAPI.org: documented public REST API. https://newsapi.org/docs/endpoints/everything
NEWSAPI_ENV = "NEWSAPI_KEY"
NEWSAPI_URL = "https://newsapi.org/v2/everything"
NEWSAPI_FREE_TIER_REQUESTS_PER_DAY = 100


# --------------------------------------------------------------------- helpers
def source_host(feed_url: str) -> str:
    host = (urlparse(feed_url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_rfc822_ms(text: str | None) -> int | None:
    """Parse an RSS ``pubDate`` (RFC 822/2822)."""
    if not text:
        return None
    try:
        return _ms(parsedate_to_datetime(text.strip()))
    except (TypeError, ValueError, OverflowError):
        return None


def parse_iso_ms(text: str | None) -> int | None:
    """Parse an ISO-8601 timestamp such as ``2026-11-03T21:14:45Z``."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.endswith(("Z", "z")):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        return _ms(datetime.fromisoformat(cleaned))
    except ValueError:
        return None


def _summary(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    return cleaned[:SUMMARY_LIMIT]


def metric_slug(text: str, *, limit: int = 40) -> str:
    """Turn a query or host into a stable metric-name suffix."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:limit] or "default"


def parse_retry_after_seconds(value: str | None) -> float | None:
    """``Retry-After`` is either delta-seconds or an HTTP date; None if absent."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class AdaptiveBackoff:
    """Per-source 429 handling: Retry-After, jittered exponential, breaker.

    ``now`` is injected (``time.monotonic`` seconds) so tests drive the clock
    explicitly instead of sleeping. The breaker opens after
    ``breaker_threshold`` consecutive 429s and parks the source for
    ``breaker_seconds``; a 200 resets the ladder.
    """

    def __init__(
        self,
        *,
        base_seconds: float,
        max_seconds: float = BACKOFF_MAX_S,
        jitter: float = BACKOFF_JITTER,
        breaker_threshold: int = BACKOFF_BREAKER_THRESHOLD,
        breaker_seconds: float = BACKOFF_BREAKER_S,
        rng: random.Random | None = None,
    ) -> None:
        self.base_seconds = max(0.0, float(base_seconds))
        self.max_seconds = max(self.base_seconds, float(max_seconds))
        self.jitter = max(0.0, float(jitter))
        self.breaker_threshold = max(1, int(breaker_threshold))
        self.breaker_seconds = max(0.0, float(breaker_seconds))
        self._rng = rng or random.Random()
        self.consecutive_429 = 0
        self.next_attempt = 0.0
        self.open_until = 0.0
        self.last_delay = 0.0
        self.trips = 0

    # -- circuit state ---------------------------------------------------
    def is_open(self, now: float) -> bool:
        return now < self.open_until

    def allow(self, now: float) -> bool:
        return now >= max(self.next_attempt, self.open_until)

    def wait(self, now: float) -> float:
        return max(0.0, max(self.next_attempt, self.open_until) - now)

    # -- transitions -----------------------------------------------------
    def on_success(self, now: float, next_delay: float) -> None:
        self.consecutive_429 = 0
        self.open_until = 0.0
        self.last_delay = max(0.0, float(next_delay))
        self.next_attempt = now + self.last_delay

    def on_error(self, now: float, delay: float) -> float:
        self.last_delay = max(0.0, float(delay))
        self.next_attempt = now + self.last_delay
        return self.last_delay

    def on_rate_limit(self, now: float, retry_after: float | None = None) -> float:
        self.consecutive_429 += 1
        exponent = min(self.consecutive_429 - 1, 8)
        ladder = min(self.max_seconds, self.base_seconds * (2.0**exponent))
        delay = ladder * (1.0 + self._rng.uniform(0.0, self.jitter))
        if retry_after is not None:
            # Retry-After is authoritative: wait at least that long.
            delay = max(delay, float(retry_after))
        self.last_delay = delay
        self.next_attempt = now + delay
        if self.consecutive_429 >= self.breaker_threshold:
            self.open_until = max(self.open_until, now + self.breaker_seconds)
            self.trips += 1
        return delay


class Deduper:
    """Bounded insertion-ordered set: ``add`` returns True for a new key."""

    def __init__(self, limit: int = DEDUPE_LIMIT) -> None:
        self.limit = max(1, int(limit))
        self._seen: set[str] = set()
        self._order: deque[str] = deque()

    def add(self, key: Any) -> bool:
        if not key:
            return False
        key = str(key)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._order.append(key)
        while len(self._order) > self.limit:
            self._seen.discard(self._order.popleft())
        return True

    def __contains__(self, key: Any) -> bool:
        return bool(key) and str(key) in self._seen

    def __len__(self) -> int:
        return len(self._seen)


# -------------------------------------------------------------------- NewsAPI
def parse_newsapi_articles(
    payload: Any, recv_ts_ns: int, recv_mono_ns: int, source: str = "newsapi"
) -> list[dict[str, Any]]:
    """Convert a NewsAPI.org ``/v2/everything`` response into ``news_items`` rows."""
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return []
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return []
    rows: list[dict[str, Any]] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        url = article.get("url")
        if not isinstance(url, str) or not url:
            continue
        title = article.get("title") if isinstance(article.get("title"), str) else None
        summary = article.get("description") or article.get("content")
        rows.append(
            {
                "recv_ts_ns": recv_ts_ns,
                "recv_mono_ns": recv_mono_ns,
                "source": source,
                "title": title or url,
                "url": url,
                "published_ts_ms": parse_iso_ms(article.get("publishedAt")),
                "summary": _summary(summary),
                "language": None,
                "raw_json": orjson.dumps(article).decode(),
            }
        )
    return rows


# ------------------------------------------------------------------- RSS / Atom
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _children(elem: ET.Element) -> list[ET.Element]:
    return list(elem)


def _child(elem: ET.Element, name: str) -> ET.Element | None:
    wanted = name.lower()
    for child in _children(elem):
        if _local(child.tag) == wanted:
            return child
    return None


def _text(elem: ET.Element | None) -> str | None:
    if elem is None or elem.text is None:
        return None
    cleaned = elem.text.strip()
    return cleaned or None


def _atom_link(elem: ET.Element) -> str | None:
    fallback = None
    for child in _children(elem):
        if _local(child.tag) != "link":
            continue
        href = child.get("href")
        if not href:
            continue
        rel = (child.get("rel") or "alternate").lower()
        if rel == "alternate":
            return href.strip()
        fallback = fallback or href.strip()
    return fallback


def _rss_item(
    item: ET.Element, language: str | None, recv_ts_ns: int, recv_mono_ns: int, source: str
) -> dict[str, Any] | None:
    link = _text(_child(item, "link")) or _atom_link(item)
    guid = _text(_child(item, "guid"))
    url = link or guid
    if not url:
        return None
    title = _text(_child(item, "title"))
    description = _text(_child(item, "description")) or _text(_child(item, "summary"))
    published = _text(_child(item, "pubDate")) or _text(_child(item, "updated"))
    row = {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "source": source,
        "title": title or url,
        "url": url,
        "published_ts_ms": parse_rfc822_ms(published) or parse_iso_ms(published),
        "summary": _summary(description),
        "language": language,
        "raw_json": orjson.dumps(
            {
                "title": title,
                "link": link,
                "guid": guid,
                "description": description,
                "pubDate": published,
            }
        ).decode(),
    }
    return row


def _atom_entry(
    entry: ET.Element, language: str | None, recv_ts_ns: int, recv_mono_ns: int, source: str
) -> dict[str, Any] | None:
    link = _atom_link(entry) or _text(_child(entry, "link"))
    entry_id = _text(_child(entry, "id"))
    url = link or entry_id
    if not url:
        return None
    title = _text(_child(entry, "title"))
    summary = _text(_child(entry, "summary")) or _text(_child(entry, "content"))
    updated = _text(_child(entry, "updated")) or _text(_child(entry, "published"))
    row = {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "source": source,
        "title": title or url,
        "url": url,
        "published_ts_ms": parse_iso_ms(updated) or parse_rfc822_ms(updated),
        "summary": _summary(summary),
        "language": language,
        "raw_json": orjson.dumps(
            {
                "title": title,
                "link": link,
                "id": entry_id,
                "summary": summary,
                "updated": updated,
            }
        ).decode(),
    }
    return row


def parse_rss_feed(
    content: str | bytes, feed_url: str, recv_ts_ns: int, recv_mono_ns: int
) -> list[dict[str, Any]]:
    """Parse an RSS 2.0 or Atom document into ``news_items`` rows.

    Raises ``xml.etree.ElementTree.ParseError`` for non-XML bodies; the caller
    treats that as a feed error rather than inventing rows.
    """
    if isinstance(content, bytes):
        content = content.lstrip(b"\xef\xbb\xbf")
    root = ET.fromstring(content)
    source = source_host(feed_url)
    root_language = root.get("{http://www.w3.org/XML/1998/namespace}lang")
    is_atom = _local(root.tag) == "feed" or _child(root, "entry") is not None
    rows: list[dict[str, Any]] = []

    if is_atom:
        language = root_language or _text(_child(root, "language"))
        for entry in _children(root):
            if _local(entry.tag) != "entry":
                continue
            row = _atom_entry(entry, language, recv_ts_ns, recv_mono_ns, source)
            if row is not None:
                rows.append(row)
        return rows

    channel = _child(root, "channel")
    if channel is None:
        channel = root
    language = root_language or _text(_child(channel, "language"))
    for item in _children(channel):
        if _local(item.tag) != "item":
            continue
        row = _rss_item(item, language, recv_ts_ns, recv_mono_ns, source)
        if row is not None:
            rows.append(row)
    return rows


# --------------------------------------------------------------- Gamma comments
def parse_gamma_comment(
    raw: dict[str, Any],
    recv_ts_ns: int,
    recv_mono_ns: int,
    parent_entity_type: str | None = None,
    parent_entity_id: str | None = None,
) -> dict[str, Any] | None:
    """Convert one Gamma comment object into a ``gamma_comments`` row."""
    if not isinstance(raw, dict):
        return None
    comment_id = raw.get("id")
    if comment_id is None:
        return None
    profile = raw.get("profile") if isinstance(raw.get("profile"), dict) else {}
    author = None
    for key in ("name", "pseudonym", "displayUsername"):
        value = profile.get(key)
        if isinstance(value, str) and value:
            author = value
            break
    if not author:
        author = raw.get("userAddress")
    created = raw.get("createdAt")
    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "comment_id": str(comment_id),
        "parent_entity_type": str(raw.get("parentEntityType") or parent_entity_type or ""),
        "parent_entity_id": str(raw.get("parentEntityID") or raw.get("parentEntityId") or parent_entity_id or ""),
        "body": raw.get("body"),
        "created_at_ms": parse_iso_ms(created),
        "author": str(author) if author is not None else None,
        "raw_json": orjson.dumps(raw).decode(),
    }


def event_id_from_market(payload: Any) -> str | None:
    """Extract the event id from a Gamma ``/markets`` response."""
    market = None
    if isinstance(payload, list) and payload:
        market = payload[0]
    elif isinstance(payload, dict):
        market = payload
    if not isinstance(market, dict):
        return None
    events = market.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        event_id = events[0].get("id")
        if event_id is not None and event_id != "":
            return str(event_id)
    for key in ("eventId", "event_id"):
        if market.get(key) not in (None, ""):
            return str(market[key])
    return None


# ------------------------------------------------------------------ HTTP helpers
@dataclass(frozen=True)
class FetchResult:
    """One HTTP GET, decoded enough for the pollers to decide what to do."""

    status: int
    recv_ts_ns: int
    recv_mono_ns: int
    payload: Any = None
    content: bytes = b""
    retry_after_s: float | None = None
    error: str | None = None


def _fetch_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> FetchResult:
    response = session.get(url, params=params, headers=headers, timeout=40)
    result = FetchResult(
        status=response.status_code,
        recv_ts_ns=time.time_ns(),
        recv_mono_ns=time.perf_counter_ns(),
        retry_after_s=parse_retry_after_seconds(response.headers.get("Retry-After")),
    )
    if result.status != 200:
        return result
    try:
        payload = response.json()
    except ValueError as exc:  # malformed/throttled body, not a rate-limit status
        return FetchResult(
            status=result.status,
            recv_ts_ns=result.recv_ts_ns,
            recv_mono_ns=result.recv_mono_ns,
            retry_after_s=result.retry_after_s,
            error=f"invalid JSON: {exc}",
        )
    return FetchResult(
        status=result.status,
        recv_ts_ns=result.recv_ts_ns,
        recv_mono_ns=result.recv_mono_ns,
        payload=payload,
        retry_after_s=result.retry_after_s,
    )


def _fetch_bytes(session: requests.Session, url: str) -> FetchResult:
    response = session.get(url, timeout=40)
    return FetchResult(
        status=response.status_code,
        recv_ts_ns=time.time_ns(),
        recv_mono_ns=time.perf_counter_ns(),
        content=response.content,
        retry_after_s=parse_retry_after_seconds(response.headers.get("Retry-After")),
    )


# ------------------------------------------------------------------------ loops
@dataclass
class _QueryState:
    """One NewsAPI query and its independent backoff ladder."""

    query: str
    slug: str
    backoff: AdaptiveBackoff
    last_attempt: float = field(default=0.0)


def _pick_due(states: list[_QueryState], now: float) -> _QueryState | None:
    """Least-recently-attempted query whose backoff has elapsed, else None."""
    due = [state for state in states if state.backoff.allow(now)]
    if not due:
        return None
    return min(due, key=lambda state: state.last_attempt)


def _next_wait(states: list[_QueryState], now: float) -> float:
    return min((state.backoff.wait(now) for state in states), default=0.0)


def _backoff_from_cfg(cfg: dict[str, Any], base_seconds: float, prefix: str) -> AdaptiveBackoff:
    return AdaptiveBackoff(
        base_seconds=base_seconds,
        max_seconds=float(cfg.get(f"{prefix}_backoff_max_seconds", BACKOFF_MAX_S)),
        jitter=float(cfg.get(f"{prefix}_backoff_jitter", BACKOFF_JITTER)),
        breaker_threshold=int(cfg.get(f"{prefix}_breaker_threshold", BACKOFF_BREAKER_THRESHOLD)),
        breaker_seconds=float(cfg.get(f"{prefix}_breaker_seconds", BACKOFF_BREAKER_S)),
    )


async def _rss_feed_loop(
    ctx: CollectorContext,
    feed_url: str,
    poll_seconds: float,
    log: Any,
    session: requests.Session,
    *,
    cfg: dict[str, Any] | None = None,
    clock: Any = time.monotonic,
) -> None:
    """One feed, one task: its failures and backoff stay local to this feed."""
    cfg = cfg or {}
    slug = metric_slug(source_host(feed_url) or "feed")
    backoff = _backoff_from_cfg(cfg, max(10.0, poll_seconds), "rss")
    dedupe = Deduper()
    last_success: float | None = None
    while not ctx.stop.is_set():
        if last_success is not None:
            ctx.health.gauge(NAME, f"rss_last_poll_age_s_{slug}", clock() - last_success)
        try:
            result = await asyncio.to_thread(_fetch_bytes, session, feed_url)
            if result.status == 429:
                delay = backoff.on_rate_limit(clock(), result.retry_after_s)
                ctx.health.incr(NAME, "rss_429")
                ctx.health.incr(NAME, f"rss_429_{slug}")
                if backoff.consecutive_429 == 1 or backoff.is_open(clock()):
                    log.warning(
                        f"rss 429 for {feed_url}: backing off {delay:.0f}s"
                    )
                else:
                    log.debug(f"rss 429 #{backoff.consecutive_429} for {feed_url}: {delay:.0f}s")
            elif result.status != 200:
                delay = backoff.on_error(clock(), poll_seconds)
                ctx.health.incr(NAME, "rss_errors")
                ctx.health.incr(NAME, f"rss_errors_{slug}")
                log.warning(f"rss {result.status} for {feed_url}")
            else:
                rows = parse_rss_feed(
                    result.content, feed_url, result.recv_ts_ns, result.recv_mono_ns
                )
                fresh = [row for row in rows if dedupe.add(row["url"])]
                for row in fresh:
                    ctx.emit("news_items", row)
                backoff.on_success(clock(), poll_seconds)
                delay = backoff.last_delay
                last_success = clock()
                ctx.health.incr(NAME, "rss_requests")
                ctx.health.incr(NAME, "rss_items", len(rows))
                ctx.health.incr(NAME, "rss_rows", len(fresh))
                ctx.health.incr(NAME, "rows_emitted", len(fresh))
                log.debug(f"rss {source_host(feed_url)}: {len(rows)} items, {len(fresh)} new")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad feed must not stop the rest
            delay = backoff.on_error(clock(), poll_seconds)
            ctx.health.incr(NAME, "rss_errors")
            ctx.health.incr(NAME, f"rss_errors_{slug}")
            log.warning(f"rss poll failed for {feed_url}: {type(exc).__name__}: {exc}")
        ctx.health.gauge(NAME, f"rss_backoff_seconds_{slug}", backoff.wait(clock()))
        ctx.health.gauge(
            NAME, f"rss_circuit_open_{slug}", 1.0 if backoff.is_open(clock()) else 0.0
        )
        try:
            # wait() covers both the retry delay and an open circuit window.
            await asyncio.wait_for(ctx.stop.wait(), timeout=max(1.0, backoff.wait(clock())))
        except asyncio.TimeoutError:
            pass


async def _rss_loop(ctx: CollectorContext, cfg: dict[str, Any], log: Any) -> None:
    urls = [str(u) for u in cfg.get("rss_urls", []) if str(u).strip()]
    poll_seconds = max(10.0, float(cfg.get("rss_poll_seconds", 60)))
    if not urls:
        log.warning("news/rss: no rss_urls configured; poller idle")
        return

    # One task per feed: a feed that hangs, 429s or raises cannot delay the rest.
    tasks = [
        asyncio.create_task(
            _rss_feed_loop(ctx, feed_url, poll_seconds, log, _make_session(), cfg=cfg),
            name=f"news-rss-{metric_slug(source_host(feed_url) or 'feed')}",
        )
        for feed_url in urls
    ]
    log.info(f"rss poll: {len(urls)} feeds, one task each, {poll_seconds:.0f}s cadence")
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for feed_url, result in zip(urls, results):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            ctx.health.incr(NAME, "rss_poller_errors")
            log.error(f"rss poller for {feed_url} exited: {type(result).__name__}: {result}")


def _top_markets(ctx: CollectorContext, limit: int) -> list[Any]:
    markets = list(getattr(ctx.universe, "markets", {}).values()) if ctx.universe else []
    markets.sort(key=lambda market: getattr(market, "daily_pool", 0.0), reverse=True)
    return markets[: max(0, limit)]


async def _gamma_loop(
    ctx: CollectorContext, cfg: dict[str, Any], log: Any, session: requests.Session
) -> None:
    base = str(cfg.get("gamma_base_url", "https://gamma-api.polymarket.com")).rstrip("/")
    poll_seconds = max(10.0, float(cfg.get("gamma_comments_poll_seconds", 120)))
    top_n = max(0, int(cfg.get("gamma_comment_events", 50)))
    dedupe = Deduper()
    event_ids: dict[str, str | None] = {}
    market_parent_mode = True
    fallback_logged = False
    log.info(f"gamma comments: top {top_n} markets every {poll_seconds:.0f}s")

    while not ctx.stop.is_set():
        markets = _top_markets(ctx, top_n)
        emitted_this_cycle = 0
        for market in markets:
            if ctx.stop.is_set():
                return
            cid = getattr(market, "condition_id", None)
            if not cid:
                continue
            try:
                if market_parent_mode:
                    result = await asyncio.to_thread(
                        _fetch_json,
                        session,
                        f"{base}/comments",
                        {
                            "limit": 100,
                            "parent_entity_type": "Market",
                            "parent_entity_id": cid,
                            "order": "createdAt",
                            "ascending": "false",
                        },
                    )
                    ctx.health.incr(NAME, "comments_requests")
                    if result.status == 422:
                        market_parent_mode = False
                        if not fallback_logged:
                            log.info(
                                "gamma comments rejects parent_entity_type=Market (422); "
                                "resolving event ids and polling per Event"
                            )
                            fallback_logged = True
                    elif result.status == 200 and isinstance(result.payload, list):
                        for raw in result.payload:
                            row = parse_gamma_comment(
                                raw, result.recv_ts_ns, result.recv_mono_ns, "Market", cid
                            )
                            if row is not None and dedupe.add(row["comment_id"]):
                                ctx.emit("gamma_comments", row)
                                ctx.health.incr(NAME, "rows_emitted")
                                ctx.health.incr(NAME, "gamma_rows")
                                emitted_this_cycle += 1
                        continue
                    else:
                        ctx.health.incr(NAME, "comments_errors")
                        log.warning(f"gamma comments (Market) {result.status} for {cid}")
                        continue

                event_id = event_ids.get(cid)
                if event_id is None:
                    lookup = await asyncio.to_thread(
                        _fetch_json, session, f"{base}/markets", {"condition_ids": cid}
                    )
                    event_id = event_id_from_market(lookup.payload) if lookup.status == 200 else None
                    event_ids[cid] = event_id
                    if not event_id:
                        ctx.health.incr(NAME, "comments_no_event")
                        log.warning(
                            f"gamma: no event id for market {cid} (status {lookup.status}); "
                            "skipping its comments"
                        )
                        continue

                result = await asyncio.to_thread(
                    _fetch_json,
                    session,
                    f"{base}/comments",
                    {
                        "limit": 100,
                        "parent_entity_type": "Event",
                        "parent_entity_id": event_id,
                        "order": "createdAt",
                        "ascending": "false",
                    },
                )
                ctx.health.incr(NAME, "comments_requests")
                if result.status != 200:
                    ctx.health.incr(NAME, "comments_errors")
                    log.warning(f"gamma comments (Event {event_id}) {result.status}")
                    continue
                if not isinstance(result.payload, list):
                    ctx.health.incr(NAME, "comments_errors")
                    log.warning(
                        f"gamma comments (Event {event_id}) returned "
                        f"{type(result.payload).__name__}, expected a list; not emitting"
                    )
                    continue
                for raw in result.payload:
                    row = parse_gamma_comment(
                        raw, result.recv_ts_ns, result.recv_mono_ns, "Event", event_id
                    )
                    if row is not None and dedupe.add(row["comment_id"]):
                        ctx.emit("gamma_comments", row)
                        ctx.health.incr(NAME, "rows_emitted")
                        ctx.health.incr(NAME, "gamma_rows")
                        emitted_this_cycle += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad market must not stop the loop
                ctx.health.incr(NAME, "comments_errors")
                log.warning(f"gamma comments failed for {cid}: {type(exc).__name__}: {exc}")
        ctx.health.gauge(NAME, "comments_last_cycle_rows", float(emitted_this_cycle))
        log.debug(f"gamma comments cycle: {emitted_this_cycle} new comments")

        try:
            await asyncio.wait_for(ctx.stop.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass


def newsapi_key() -> str | None:
    """The optional wire is enabled only by ``NEWSAPI_KEY`` in the environment."""
    key = os.environ.get(NEWSAPI_ENV, "").strip()
    return key or None


class NewsApiPoller:
    """Optional NewsAPI.org adapter; a no-op unless ``NEWSAPI_KEY`` is set.

    Free tier is 100 requests/day, so the default cadence is one query per hour
    (24 × queries/day). Paid plans raise that limit; the env var is unchanged.
    """

    def __init__(
        self,
        ctx: CollectorContext,
        cfg: dict[str, Any],
        log: Any,
        session: requests.Session,
        *,
        clock: Any = time.monotonic,
        api_key: str | None = None,
    ) -> None:
        self.ctx = ctx
        self.log = log
        self.session = session
        self.clock = clock
        self.key = api_key if api_key is not None else newsapi_key()
        self.url = str(cfg.get("newsapi_url", NEWSAPI_URL))
        self.poll_seconds = max(60.0, float(cfg.get("newsapi_poll_seconds", 3600)))
        queries = [str(q).strip() for q in cfg.get("newsapi_queries", []) if str(q).strip()]
        self.dedupe = Deduper()
        self.states = [
            _QueryState(
                query=query,
                slug=metric_slug(query),
                backoff=_backoff_from_cfg(cfg, self.poll_seconds, "newsapi"),
            )
            for query in queries
        ]
        self.disabled_reason: str | None = None

    async def poll_once(self) -> float:
        if self.key is None or self.disabled_reason is not None:
            return self.poll_seconds
        now = self.clock()
        state = _pick_due(self.states, now)
        if state is None:
            return max(_next_wait(self.states, now), 1.0)

        state.last_attempt = now
        params = {
            "q": state.query,
            "sortBy": "publishedAt",
            "pageSize": 50,
            "language": "en",
        }
        headers = {"X-Api-Key": self.key}
        try:
            result = await asyncio.to_thread(
                _fetch_json, self.session, self.url, params, headers
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - network flakiness must not kill the loop
            delay = state.backoff.on_error(self.clock(), self.poll_seconds)
            self.ctx.health.incr(NAME, "newsapi_errors")
            self.log.warning(
                f"newsapi poll failed for {state.query!r}: {type(exc).__name__}: {exc}"
            )
            return delay

        now = self.clock()
        if result.status == 429:
            delay = state.backoff.on_rate_limit(now, result.retry_after_s)
            self.ctx.health.incr(NAME, "newsapi_429")
            self.log.warning(
                f"newsapi 429 for {state.query!r}: backing off {delay:.0f}s "
                f"(free tier is {NEWSAPI_FREE_TIER_REQUESTS_PER_DAY} requests/day)"
            )
            return delay
        if result.status in (401, 403, 426):
            # Bad key or plan limit: retrying cannot help until the env changes.
            self.disabled_reason = f"HTTP {result.status}"
            self.ctx.health.incr(NAME, "newsapi_errors")
            self.ctx.health.incr(NAME, "newsapi_disabled")
            self.log.error(
                f"newsapi disabled (HTTP {result.status}); check {NEWSAPI_ENV}. "
                "RSS and Gamma continue."
            )
            return self.poll_seconds
        if result.status != 200:
            delay = state.backoff.on_error(now, self.poll_seconds)
            self.ctx.health.incr(NAME, "newsapi_errors")
            self.log.warning(f"newsapi {result.status} for {state.query!r}")
            return delay
        if result.error is not None:
            delay = state.backoff.on_error(self.clock(), self.poll_seconds)
            self.ctx.health.incr(NAME, "newsapi_errors")
            self.log.warning(f"newsapi {result.error} for {state.query!r}")
            return delay

        rows = parse_newsapi_articles(result.payload, result.recv_ts_ns, result.recv_mono_ns)
        fresh = [row for row in rows if self.dedupe.add(row["url"])]
        for row in fresh:
            self.ctx.emit("news_items", row)
        state.backoff.on_success(self.clock(), self.poll_seconds)
        self.ctx.health.incr(NAME, "newsapi_requests")
        self.ctx.health.incr(NAME, "newsapi_rows", len(fresh))
        self.ctx.health.incr(NAME, "rows_emitted", len(fresh))
        self.log.debug(f"newsapi {state.query!r}: {len(rows)} articles, {len(fresh)} new")
        return state.backoff.last_delay

    async def run(self) -> None:
        if self.key is None:
            self.log.info(f"newsapi adapter off ({NEWSAPI_ENV} unset); free wires only")
            return
        if not self.states:
            self.log.warning("newsapi: no newsapi_queries configured; idle")
            return
        self.log.info(
            f"newsapi poll: {len(self.states)} queries every {self.poll_seconds:.0f}s "
            f"(free tier {NEWSAPI_FREE_TIER_REQUESTS_PER_DAY} requests/day)"
        )
        while not self.ctx.stop.is_set():
            delay = await self.poll_once()
            if self.disabled_reason is not None:
                return
            try:
                await asyncio.wait_for(self.ctx.stop.wait(), timeout=max(1.0, delay))
            except asyncio.TimeoutError:
                pass


async def _newsapi_loop(
    ctx: CollectorContext, cfg: dict[str, Any], log: Any, session: requests.Session
) -> None:
    await NewsApiPoller(ctx, cfg, log, session).run()


def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, application/rss+xml, application/atom+xml, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


async def run(ctx: CollectorContext) -> None:
    cfg = ctx.section(NAME)
    log = ctx.log(NAME)
    ctx.register_table("news_items", NEWS_ITEMS)
    ctx.register_table("gamma_comments", GAMMA_COMMENTS)

    # Separate sessions: the pollers run concurrently in worker threads. Each
    # poller owns its own error metric, so one dead source does not stop the rest.
    tasks = [
        asyncio.create_task(_rss_loop(ctx, cfg, log), name="news-rss"),
        asyncio.create_task(_gamma_loop(ctx, cfg, log, _make_session()), name="news-gamma"),
        asyncio.create_task(_newsapi_loop(ctx, cfg, log, _make_session()), name="news-newsapi"),
    ]
    names = [task.get_name() for task in tasks]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for name, result in zip(names, results):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            ctx.health.incr(NAME, "poller_errors")
            log.error(f"poller {name} exited: {type(result).__name__}: {result}")
