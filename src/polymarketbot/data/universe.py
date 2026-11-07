"""Reward-market universe, rolling or locked to a pinned manifest.

The 1,028-market set from the 2026-11-01 snapshot is not stable: 709 of those
markets carry an end date before 2026-11-18 and 677 end in the week of the
2026-11-02 midterms. A 14-day tape therefore needs a universe that is re-cut
from Gamma on a schedule and that tells the CLOB stream which assets to add or
drop.

Two modes, selected by ``clob.universe_mode``:

* ``rolling`` (default): the Gamma set above the cutoff is re-cut every
  refresh, and membership is recorded per refresh.
* ``locked``: the set is pinned to ``clob.locked_manifest``. Gamma is still
  crawled for live volume/pool, but only markets in the pinned set are kept;
  a pinned market that Gamma no longer lists is retained from the baseline so
  its tokens stay subscribed.

The cutoff is the same one used in the master plan: ``rewards_daily_rate > 15``.
Active crypto up/down markets (BTC/ETH/SOL) are subscribed regardless of that
cutoff: they are sentinels for Gate 3 and lead-lag measurement, not allocator
targets, so they carry ``sentinel=True`` and never count towards the reward
pool total. The manifest is written atomically and versioned by timestamp, so
the exact universe used at any hour is recoverable.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import requests

from polymarketbot.data.universe_metadata import META_PATH, load_rows
from polymarketbot.logs import IST, get_logger

logger = get_logger("data.universe")

GAMMA_KEYSET = "https://gamma-api.polymarket.com/markets/keyset"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
SAMPLING_MARKETS_URL = "https://clob.polymarket.com/sampling-markets"
CUTOFF_DEFAULT = 15.0
UNIVERSE_MODES = ("rolling", "locked")
LOCKED_MANIFEST_DEFAULT = "data/tape/manifest_locked_1028.json"
# Crypto up/down sentinels: subscribed regardless of the reward cutoff so Gate 3
# (spread/latency vs Binance) and lead-lag stay measurable. They are not
# allocator targets and contribute nothing to the reward pool total.
SENTINEL_PATTERNS_DEFAULT = (
    r"\bbitcoin\b",
    r"\bbtc\b",
    r"\bethereum\b",
    r"\beth\b",
    r"\bsolana\b",
    r"\bsol\b",
)
SENTINEL_CRYPTO_ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb")
SENTINEL_STALE_SECONDS = 3_600.0
SENTINEL_EXPIRY_GRACE_SECONDS = 60.0

# The CLOB reward-campaign endpoint: one row per market with its campaigns and
# token ids. It replaces the 240 k-market Gamma sweep (2026-11-05: 20 min) with
# five pages (~2 k markets, seconds) for the rolling walk.
REWARDS_MARKETS_URL = "https://clob.polymarket.com/rewards/markets/multi"


@dataclass(frozen=True)
class Market:
    condition_id: str
    question: str
    slug: str
    daily_pool: float
    min_size: float
    max_spread: float
    volume: float
    liquidity: float
    neg_risk: bool
    tokens: tuple[str, ...]
    end_date: str | None = None
    sentinel: bool = False
    # Enriched from Gamma (universe_metadata.refresh): event grouping and fees.
    event_id: str | None = None
    event_slug: str | None = None
    event_title: str | None = None
    fee_type: str | None = None
    fees_enabled: bool | None = None
    fee_rate: float | None = None
    fee_exponent: float | None = None
    fee_rebate: float | None = None
    resolution_source: str | None = None
    tick_size: float | None = None
    min_order_size: float | None = None
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "question": self.question,
            "slug": self.slug,
            "daily_pool": self.daily_pool,
            "min_size": self.min_size,
            "max_spread": self.max_spread,
            "volume": self.volume,
            "liquidity": self.liquidity,
            "neg_risk": self.neg_risk,
            "clob_tokens": list(self.tokens),
            "end_date": self.end_date,
            "sentinel": self.sentinel,
            "event_id": self.event_id,
            "event_slug": self.event_slug,
            "event_title": self.event_title,
            "fee_type": self.fee_type,
            "fees_enabled": self.fees_enabled,
            "fee_rate": self.fee_rate,
            "fee_exponent": self.fee_exponent,
            "fee_rebate": self.fee_rebate,
            "resolution_source": self.resolution_source,
            "tick_size": self.tick_size,
            "min_order_size": self.min_order_size,
            "tags": list(self.tags),
        }


def _tag_labels(row: dict[str, Any]) -> tuple[str, ...]:
    """Label strings from Gamma's tag objects, ignoring the metadata noise."""
    labels: list[str] = []
    for tag in row.get("tags") or []:
        if isinstance(tag, dict) and tag.get("label"):
            labels.append(str(tag["label"]))
        elif isinstance(tag, str):
            labels.append(tag)
    return tuple(labels)


@dataclass
class UniverseUpdate:
    added_markets: list[Market] = field(default_factory=list)
    removed_markets: list[Market] = field(default_factory=list)
    added_assets: list[str] = field(default_factory=list)
    removed_assets: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.added_markets or self.removed_markets)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value: Any) -> float | None:
    """Epoch seconds from an ISO date/string, or None."""
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def _parse_tokens(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ()
    if not isinstance(raw, list):
        return ()
    return tuple(str(t) for t in raw if t)


class UniverseState:
    """Current market set plus an asyncio queue of add/remove updates."""

    def __init__(
        self,
        manifest_path: str | Path = "data/tape/manifest_current.json",
        *,
        cutoff: float = CUTOFF_DEFAULT,
        refresh_seconds: float = 300.0,
        max_pages: int = 800,
        poll_seconds: float = 30.0,
        incremental: bool = True,
        mode: str = "rolling",
        locked_manifest: str | Path = LOCKED_MANIFEST_DEFAULT,
        sentinel_enabled: bool = True,
        sentinel_question_patterns: Sequence[str] | None = None,
        sentinel_max_markets: int = 200,
        metadata_path: str | Path | None = META_PATH,
    ) -> None:
        mode = str(mode or "rolling").strip().lower()
        if mode not in UNIVERSE_MODES:
            raise ValueError(
                f"unknown clob.universe_mode {mode!r}; "
                f"expected one of {', '.join(UNIVERSE_MODES)}"
            )
        self.mode = mode
        self.locked_manifest = Path(locked_manifest)
        self.manifest_path = Path(manifest_path)
        self._metadata_path = Path(metadata_path) if metadata_path else None
        self._metadata: dict[str, dict[str, Any]] = (
            load_rows(self._metadata_path) if self._metadata_path else {}
        )
        self.state_path = self.manifest_path.parent / "universe_state.json"
        self.cutoff = float(cutoff)
        self.sentinel_enabled = bool(sentinel_enabled)
        self.sentinel_max_markets = max(0, int(sentinel_max_markets))
        patterns = (
            SENTINEL_PATTERNS_DEFAULT
            if sentinel_question_patterns is None
            else tuple(str(p) for p in sentinel_question_patterns if str(p))
        )
        try:
            self.sentinel_patterns = tuple(
                re.compile(p, re.IGNORECASE) for p in patterns
            )
        except re.error as exc:
            raise ValueError(
                f"invalid clob.sentinel_question_patterns entry: {exc}"
            ) from exc
        self.refresh_seconds = max(60.0, float(refresh_seconds))
        self.max_pages = max_pages
        self.poll_seconds = max(0.0, float(poll_seconds))
        self.incremental = bool(incremental) and self.poll_seconds > 0
        # Loaded eagerly so a missing or malformed pinned set fails at startup
        # with a clear error rather than after the first Gamma crawl.
        self.locked: dict[str, Market] = (
            self._load_locked() if self.mode == "locked" else {}
        )
        # Locked markets dropped because the venue resolved them must not come
        # back from the baseline on the next crawl.
        self._retired: set[str] = set()
        self.markets: dict[str, Market] = {}
        self.asset_to_market: dict[str, str] = {}
        self.updates: asyncio.Queue[UniverseUpdate] = asyncio.Queue()
        self.generated_at: str | None = None
        self.last_full_walk_ts: float = 0.0
        self.last_poll_ts: float = 0.0
        self.last_market_id: int = 0
        self._stale = False
        self._task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "polymarketbot-tape/0.1"

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.mode == "locked":
            logger.info(
                f"universe mode: locked; pinned to {self.locked_manifest} "
                f"({len(self.locked)} markets)"
            )
            # The baseline is complete on its own: subscribe immediately and let
            # the refresh loop's first crawl confirm Gamma coverage in the
            # background, rather than stalling the stream on a 40k-market walk.
            self._install_locked()
        else:
            logger.info("universe mode: rolling; membership recorded per refresh")
            if self._load_manifest():
                logger.info(
                    f"universe loaded: {len(self.markets)} markets, "
                    f"{len(self.asset_to_market)} assets, generated {self.generated_at}"
                    + (" (stale; refreshing in background)" if self._stale else "")
                )
            else:
                logger.info("no cached universe manifest; refreshing from Gamma")
                # First-ever start: the CLOB stream has nothing to subscribe to
                # until this completes. The orchestrator bounds it.
                await self.refresh(notify=False)
        logger.info(self._sentinel_log_line())
        self._task = asyncio.create_task(self._refresh_loop(), name="universe-refresh")
        if self.incremental:
            self._poll_task = asyncio.create_task(self._poll_loop(), name="universe-poll")

    def _sentinel_log_line(self) -> str:
        if not self.sentinel_enabled:
            return "sentinel: 0 markets (sentinel_enabled=false)"
        return f"sentinel: {self.sentinel_count} markets"

    async def stop(self) -> None:
        tasks = [t for t in (self._task, self._poll_task) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def next_full_walk_delay(self, now: float | None = None) -> float:
        """Seconds until the cached keyset walk is one refresh interval old."""
        now = time.time() if now is None else now
        if self.last_full_walk_ts <= 0:
            return 0.0
        return max(0.0, self.last_full_walk_ts + self.refresh_seconds - now)

    def next_poll_delay(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        if self.last_poll_ts <= 0:
            return 0.0
        return max(0.0, self.last_poll_ts + self.poll_seconds - now)

    async def _refresh_loop(self) -> None:
        while True:
            delay = self.next_full_walk_delay()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"universe refresh failed; keeping current set: {exc}")
                await asyncio.sleep(30.0)

    async def _poll_loop(self) -> None:
        while True:
            delay = self.next_poll_delay()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await self.poll_incremental()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"incremental universe poll failed: {exc}")
                await asyncio.sleep(15.0)

    # ------------------------------------------------------------------ poll
    async def poll_incremental(self) -> UniverseUpdate | None:
        """Cheap poll for markets created or closed since the last check.

        The hourly keyset walk remains the authoritative reconciliation; this
        only closes the gap so a new market is subscribable within
        ``poll_seconds`` and a resolved one is dropped without waiting an hour.
        """
        if not self.incremental:
            return None
        since_id = self.last_market_id
        since_ts = self.last_poll_ts or self.last_full_walk_ts
        new_rows, closed_rows = await asyncio.to_thread(
            self._fetch_incremental, since_id, since_ts
        )
        new_markets = self._build(new_rows, include_locked_baseline=False)
        closed_ids = {self._row_cid(row) for row in closed_rows if self._row_cid(row)}

        # Active expiration check: auto-prune markets whose trading window has expired
        now = time.time()
        for cid, market in list(self.markets.items()):
            end_ts = _parse_ts(market.end_date)
            if end_ts is not None:
                # 5-minute sentinels cease trading at window close; prune after grace period
                grace = SENTINEL_EXPIRY_GRACE_SECONDS if market.sentinel else 3600.0
                if end_ts < now - grace:
                    closed_ids.add(cid)

        if self.mode == "locked":
            # A closed pinned market must not be resurrected by the next crawl.
            self._retired.update(closed_ids)
        old = dict(self.markets)
        for market in new_markets.values():
            self.markets[market.condition_id] = market
        for cid in closed_ids:
            self.markets.pop(cid, None)
        self._cap_sentinels()
        added = [m for cid, m in self.markets.items() if cid not in old]
        removed = [m for cid, m in old.items() if cid not in self.markets]
        if added or removed:
            self.asset_to_market = {
                t: m.condition_id for m in self.markets.values() for t in m.tokens
            }
        for row in new_rows:
            self.last_market_id = max(self.last_market_id, self._row_id(row))
        self.last_poll_ts = time.time()
        if added or removed:
            self._write_manifest(version=False)
            update = UniverseUpdate(
                added_markets=added,
                removed_markets=removed,
                added_assets=[t for m in added for t in m.tokens],
                removed_assets=[t for m in removed for t in m.tokens],
            )
            await self.updates.put(update)
            logger.info(
                f"universe poll: +{len(added)}/-{len(removed)} markets, "
                f"+{len(update.added_assets)}/-{len(update.removed_assets)} assets"
            )
        self._write_state()
        return UniverseUpdate(
            added_markets=added,
            removed_markets=removed,
            added_assets=[t for m in added for t in m.tokens],
            removed_assets=[t for m in removed for t in m.tokens],
        )

    def _cap_sentinels(self) -> list[Market]:
        """Drop the lowest-volume sentinels when the live set exceeds the cap.

        The full crawl caps its own output, but the incremental poll adds new
        five-minute binaries every minute; without a total cap the sentinel set
        would grow until the next crawl and consume subscription slots long
        after the markets ended.
        """
        cap = self.sentinel_max_markets
        if not self.sentinel_enabled or cap <= 0:
            return []
        sentinels = [m for m in self.markets.values() if m.sentinel]
        if len(sentinels) <= cap:
            return []
        dropped = sorted(
            sentinels, key=lambda m: (m.volume, m.liquidity)
        )[: len(sentinels) - cap]
        for market in dropped:
            self.markets.pop(market.condition_id, None)
        self.asset_to_market = {
            t: m.condition_id for m in self.markets.values() for t in m.tokens
        }
        return dropped

    def on_market_resolved(self, condition_id: str | None) -> None:
        """Drop a market the venue just resolved, without waiting for a poll."""
        cid = str(condition_id or "")
        market = self.markets.pop(cid, None)
        if market is None:
            return
        if self.mode == "locked":
            self._retired.add(cid)
        for token in market.tokens:
            self.asset_to_market.pop(token, None)
        self.updates.put_nowait(
            UniverseUpdate(removed_markets=[market], removed_assets=list(market.tokens))
        )
        self._write_manifest(version=False)
        self._write_state()
        logger.info(
            f"market resolved: {cid[:16]}...; dropped {len(market.tokens)} assets"
        )

    def _fetch_short_duration_markets(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fast query for active markets ending soonest (e.g. 5m, 15m, 1h).

        Gamma returns active, non-closed markets sorted by endDate ascending in
        ~0.3s, directly surfacing ultra-short duration binaries as they trade.
        """
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        params = [
            ("active", "true"),
            ("closed", "false"),
            ("end_date_min", now_iso),
            ("order", "endDate"),
            ("ascending", "true"),
            ("limit", str(min(limit, 100))),
        ]
        try:
            payload = self._get_json(GAMMA_MARKETS, params)
            return (
                payload
                if isinstance(payload, list)
                else (payload.get("markets") or payload.get("data") or [])
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"failed to fetch short-duration markets: {exc}")
            return []

    def _fetch_deterministic_sentinels(self) -> list[dict[str, Any]]:
        """Pre-fetch current and upcoming 5m & 15m crypto up/down markets by deterministic slug.

        Polymarket crypto binaries use the deterministic slug format:
            {asset}-updown-{timeframe}-{boundary_timestamp}
        We compute the current and next 5m/15m intervals for active crypto assets,
        querying Gamma /events in a single batched request (repeated slug params)
        taking ~0.2s. This pre-registers the next window BEFORE it starts trading.
        """
        if not self.sentinel_enabled:
            return []
        now_sec = int(time.time())
        w300 = now_sec - (now_sec % 300)
        w900 = now_sec - (now_sec % 900)

        slugs: list[str] = []
        for a in SENTINEL_CRYPTO_ASSETS:
            # Current 5m window and next (+5m) window
            slugs.append(f"{a}-updown-5m-{w300}")
            slugs.append(f"{a}-updown-5m-{w300 + 300}")
            # Current 15m window
            slugs.append(f"{a}-updown-15m-{w900}")

        params = [("slug", s) for s in slugs]
        try:
            payload = self._get_json(GAMMA_EVENTS, params)
            events = (
                payload
                if isinstance(payload, list)
                else (payload.get("events") or payload.get("data") or [])
            )
            markets: list[dict[str, Any]] = []
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                ev_slug = ev.get("slug")
                ev_title = ev.get("title")
                ev_id = str(ev.get("id")) if ev.get("id") is not None else None
                for m in ev.get("markets") or []:
                    if isinstance(m, dict):
                        m_copy = dict(m)
                        m_copy["event_slug"] = ev_slug
                        m_copy["event_title"] = ev_title
                        m_copy["event_id"] = ev_id
                        markets.append(m_copy)
            return markets
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"failed to fetch deterministic sentinels: {exc}")
            return []

    def _fetch_incremental(
        self, since_id: int, since_ts: float
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        new_rows: list[dict[str, Any]] = []
        closed_rows: list[dict[str, Any]] = []

        # 1. Multi-tier fast discovery for active & short-duration markets
        try:
            short_rows = self._fetch_short_duration_markets(limit=100)
            new_rows.extend(short_rows)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"short-duration market fetch failed: {exc}")

        # 2. Deterministic 5m & 15m crypto up/down sentinels
        if self.sentinel_enabled:
            try:
                sentinel_rows = self._fetch_deterministic_sentinels()
                new_rows.extend(sentinel_rows)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"deterministic sentinel fetch failed: {exc}")

        # 3. Real-time newly created markets (by createdAt)
        try:
            created_rows = self._fetch_market_pages(
                {"closed": "false", "limit": 50, "order": "createdAt", "ascending": "false"},
                stop=lambda rows: False,
                max_pages=1,
            )
            new_rows.extend(created_rows)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"newly created market fetch failed: {exc}")

        # 4. Keyset ID poll (backwards-compatible incremental poll)
        if since_id > 0:
            try:
                id_rows = self._fetch_market_pages(
                    {"closed": "false", "limit": 100, "order": "id", "ascending": "false"},
                    stop=lambda rows: any(self._row_id(r) <= since_id for r in rows),
                    max_pages=2,
                )
                new_rows.extend(id_rows)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"id-based market fetch failed: {exc}")

        # 5. Recently closed markets (order by closedTime descending, fallback to endDate)
        try:
            closed_rows = self._fetch_market_pages(
                {"closed": "true", "limit": 50, "order": "closedTime", "ascending": "false"},
                stop=lambda rows: any(
                    (_parse_ts(r.get("closedTime") or r.get("endDate")) or 0) < since_ts
                    for r in rows
                ),
                max_pages=2,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"closedTime poll failed, trying endDate fallback: {exc}")
            try:
                closed_rows = self._fetch_market_pages(
                    {"closed": "true", "limit": 50, "order": "endDate", "ascending": "false"},
                    stop=lambda rows: any(
                        (_parse_ts(r.get("endDate")) or 0) < since_ts for r in rows
                    ),
                    max_pages=1,
                )
            except Exception as exc2:  # noqa: BLE001
                logger.warning(f"closed-market poll failed: {exc2}")
                closed_rows = []

        # Deduplicate new_rows by conditionId / id
        seen: set[str] = set()
        deduped_new: list[dict[str, Any]] = []
        for r in new_rows:
            cid = self._row_cid(r)
            if cid and cid not in seen:
                seen.add(cid)
                deduped_new.append(r)

        return deduped_new, closed_rows

    def _fetch_market_pages(
        self,
        params: dict[str, Any],
        *,
        stop: Callable[[list[dict[str, Any]]], bool],
        max_pages: int = 5,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        limit = int(params.get("limit") or 100)
        offset = 0
        for _ in range(max(1, max_pages)):
            page_params = dict(params)
            page_params["offset"] = offset
            payload = self._get_json(GAMMA_MARKETS, page_params)
            markets = (
                payload
                if isinstance(payload, list)
                else (payload.get("markets") or payload.get("data") or [])
            )
            if not markets:
                break
            rows.extend(markets)
            if stop(rows):
                break
            if len(markets) < limit:
                break
            offset += len(markets)
            time.sleep(0.1)
        return rows

    def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        last: Exception | None = None
        for attempt in range(5):
            try:
                r = self._session.get(url, params=params, timeout=45)
                if r.status_code == 429:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:  # noqa: BLE001
                last = exc
                logger.warning(f"Gamma request failed ({url}): {exc}")
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Gamma request failed after retries: {last}")

    @staticmethod
    def _row_id(row: dict[str, Any]) -> int:
        try:
            return int(str(row.get("id") or "0"))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _row_cid(row: dict[str, Any]) -> str:
        return str(row.get("conditionId") or row.get("id") or "")

    @staticmethod
    def _iso(ts: float) -> str | None:
        return datetime.fromtimestamp(ts, IST).isoformat() if ts else None

    # ------------------------------------------------------------------ state
    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"unreadable universe state {self.state_path}: {exc}")
            return
        full = _parse_ts(payload.get("last_full_walk_at"))
        poll = _parse_ts(payload.get("last_poll_at"))
        if full:
            self.last_full_walk_ts = full
        if poll:
            self.last_poll_ts = poll
        try:
            self.last_market_id = int(payload.get("last_market_id") or 0)
        except (TypeError, ValueError):
            self.last_market_id = 0

    def _write_state(self) -> None:
        payload = {
            "last_full_walk_at": self._iso(self.last_full_walk_ts),
            "last_poll_at": self._iso(self.last_poll_ts),
            "last_market_id": self.last_market_id,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------------ refresh
    async def refresh(self, *, notify: bool = True) -> UniverseUpdate:
        rows: list[dict[str, Any]] | None = None
        last_id = 0
        if self.mode == "locked":
            rows = await asyncio.to_thread(self._fetch_markets)
            new_markets = self._build(rows)
            last_id = max((self._row_id(row) for row in rows), default=0)
        else:
            try:
                reward_rows = await asyncio.to_thread(self._fetch_reward_markets)
                if not reward_rows:
                    raise RuntimeError("CLOB rewards endpoint returned no rows")
                new_markets = self._build_reward_markets(reward_rows)
                if not new_markets:
                    raise RuntimeError("CLOB rewards endpoint produced no qualifying markets")
                last_id = max(
                    (int(row.get("market_id") or 0) for row in reward_rows), default=0
                )
                logger.info(
                    f"reward walk: {len(reward_rows)} campaign rows -> "
                    f"{len(new_markets)} markets"
                )
            except Exception as exc:  # noqa: BLE001 - the crawl is the fallback
                logger.warning(
                    f"reward walk unavailable ({exc}); falling back to the Gamma crawl"
                )
                rows = await asyncio.to_thread(self._fetch_markets)
                new_markets = self._build(rows)
                last_id = max((self._row_id(row) for row in rows), default=0)
        if not new_markets:
            raise RuntimeError("refresh returned zero qualifying markets")
        old_markets = self.markets
        if self.mode == "locked":
            present = sum(
                1 for cid in {self._row_cid(row) for row in rows} if cid in self.locked
            )
            logger.info(
                f"locked universe: {present}/{len(self.locked)} markets present at Gamma"
            )
        elif old_markets and len(new_markets) < len(old_markets) * 0.5:
            # A complete crawl that halves the universe is a real event (e.g.
            # the 2026-11-04 midterm markets resolving), not a partial page.
            # `_fetch_markets` raises on any failed or truncated page, so this
            # is applied with a loud warning rather than rejected.
            logger.warning(
                f"universe shrank {len(old_markets)} -> {len(new_markets)} markets "
                "(complete walk; applying)"
            )
        added = [m for cid, m in new_markets.items() if cid not in old_markets]
        removed = [m for cid, m in old_markets.items() if cid not in new_markets]
        added_assets = [t for m in added for t in m.tokens]
        removed_assets = [t for m in removed for t in m.tokens]

        self.markets = new_markets
        self.asset_to_market = {t: m.condition_id for m in new_markets.values() for t in m.tokens}
        self.generated_at = datetime.now(IST).isoformat()
        self.last_full_walk_ts = time.time()
        self.last_poll_ts = self.last_full_walk_ts
        self.last_market_id = max(last_id, self.last_market_id)
        self._stale = False
        self._write_manifest(version=True)
        self._write_state()
        update = UniverseUpdate(added, removed, added_assets, removed_assets)
        if notify and not update.empty():
            await self.updates.put(update)
        logger.info(
            f"universe refreshed: {len(new_markets)} markets, "
            f"+{len(added)}/-{len(removed)} markets, "
            f"+{len(added_assets)}/-{len(removed_assets)} assets, "
            f"pool=${self.reward_pool_total:,.0f}/day, "
            f"sentinel={self.sentinel_count}"
        )
        return update

    def _fetch_reward_markets(self) -> list[dict[str, Any]]:
        """Every live reward campaign from the CLOB endpoint, not the Gamma crawl.

        Rows are ordered by ``rate_per_day`` descending, so the walk stops as
        soon as a page's best campaign is at or below the cutoff: the tail is
        zero-rate and ended campaigns (the endpoint lists ~25 k rows, of which
        ~2.3 k are above $15/day).
        """
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        for page in range(100):
            params: dict[str, Any] = {
                "order_by": "rate_per_day",
                "position": "DESC",
                "page_size": 500,
            }
            if cursor:
                params["next_cursor"] = cursor
            payload = self._get_json(REWARDS_MARKETS_URL, params)
            if not isinstance(payload, dict):
                raise RuntimeError(f"unexpected rewards payload: {type(payload).__name__}")
            batch = payload.get("data") or []
            rows.extend(batch)
            cursor = payload.get("next_cursor")
            if not batch or not cursor:
                break
            best = 0.0
            for row in batch:
                campaigns = row.get("rewards_config") or []
                if isinstance(campaigns, dict):
                    campaigns = [campaigns]
                for campaign in campaigns:
                    best = max(best, _f(campaign.get("rate_per_day")))
            if best < self.cutoff:
                break
            time.sleep(0.1)
        logger.info(f"rewards endpoint: {len(rows)} campaign rows, {page + 1} page(s)")
        return rows

    def _with_metadata(self, market: Market) -> Market:
        """Fill event/fee fields from the last metadata refresh, if any."""
        row = self._metadata.get(market.condition_id.lower())
        if not row:
            return market
        updates: dict[str, Any] = {}
        for field in (
            "event_id", "event_slug", "event_title", "fee_type",
            "fee_rate", "fee_exponent", "fee_rebate", "resolution_source",
            "tick_size", "min_order_size",
        ):
            if getattr(market, field) is None and row.get(field) is not None:
                updates[field] = row[field]
        if market.fees_enabled is None and row.get("fees_enabled") is not None:
            updates["fees_enabled"] = bool(row["fees_enabled"])
        if not market.tags and row.get("tags"):
            updates["tags"] = tuple(str(t) for t in row["tags"])
        return replace(market, **updates) if updates else market

    def _market_from_reward_row(self, row: dict[str, Any]) -> Market | None:
        """Map one CLOB reward row to a Market, or None below the cutoff."""
        cid = str(row.get("condition_id") or "")
        tokens = tuple(
            str(token.get("token_id"))
            for token in (row.get("tokens") or [])
            if token.get("token_id")
        )
        if not cid or len(tokens) < 2:
            return None
        campaigns = row.get("rewards_config") or []
        if isinstance(campaigns, dict):
            campaigns = [campaigns]
        now = time.time()
        daily = 0.0
        for campaign in campaigns:
            start = _parse_ts(campaign.get("start_date"))
            end = _parse_ts(campaign.get("end_date"))
            if start is not None and start > now:
                continue
            if end is not None and end < now:
                continue
            daily += _f(campaign.get("rate_per_day"))
        if daily < self.cutoff:
            return None
        market = Market(
            condition_id=cid,
            question=str(row.get("question") or ""),
            slug=str(row.get("market_slug") or ""),
            daily_pool=daily,
            min_size=_f(row.get("rewards_min_size")),
            max_spread=_f(row.get("rewards_max_spread")),
            volume=_f(row.get("volume_24hr")),
            liquidity=0.0,
            neg_risk=bool(row.get("neg_risk")),
            tokens=tokens,
            end_date=row.get("end_date"),
            sentinel=False,
            event_id=str(row.get("event_id")) if row.get("event_id") is not None else None,
            event_slug=str(row.get("event_slug")) if row.get("event_slug") else None,
        )
        return self._with_metadata(market)

    def _build_reward_markets(
        self,
        rows: list[dict[str, Any]],
        *,
        sentinel_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Market]:
        """Reward markets from the endpoint, keeping fresh sentinels within the cap."""
        out: dict[str, Market] = {}
        for row in rows:
            market = self._market_from_reward_row(row)
            if market is not None:
                out[market.condition_id] = market
        if not self.sentinel_enabled:
            return out

        # Build any newly discovered sentinels
        if sentinel_rows:
            sentinel_markets = self._build(sentinel_rows, include_locked_baseline=False)
            for cid, market in sentinel_markets.items():
                if market.sentinel:
                    out[cid] = market
        now = time.time()
        alive: list[Market] = []
        for market in self.markets.values():
            if not market.sentinel:
                continue
            end_ts = _parse_ts(market.end_date)
            if end_ts is not None and end_ts < now - SENTINEL_STALE_SECONDS:
                continue
            alive.append(market)
        alive.sort(key=lambda m: (m.volume, m.liquidity), reverse=True)
        for market in alive[: self.sentinel_max_markets]:
            out.setdefault(market.condition_id, market)
        return out

    def _fetch_markets(self) -> list[dict[str, Any]]:
        """Fast market sweep across CLOB sampling and Gamma.

        Replaces the old 20-30 minute keyset crawler with a bounded multi-tier sweep:
        1. CLOB /sampling-markets: 1,000 markets per page, cursor-paginated.
           Fetches active, order-book enabled markets in ~2-4s total.
        2. Gamma short-duration & deterministic sentinels for 5m/15m markets.
        3. If CLOB is unavailable, performs a bounded Gamma keyset crawl capped at 50 pages.
        """
        rows: list[dict[str, Any]] = []
        # Tier 1: Try CLOB sampling markets (fast, 1000/page)
        try:
            cursor: str | None = None
            max_sampling_pages = min(int(self.max_pages), 10)
            for page in range(max_sampling_pages):
                params: dict[str, Any] = {}
                if cursor:
                    params["next_cursor"] = cursor
                payload = self._get_json(SAMPLING_MARKETS_URL, params)
                batch = payload.get("data") or [] if isinstance(payload, dict) else []
                if not batch:
                    break
                for row in batch:
                    tokens = [
                        t.get("token_id")
                        for t in (row.get("tokens") or [])
                        if isinstance(t, dict) and t.get("token_id")
                    ]
                    rates = (row.get("rewards") or {}).get("rates") or []
                    rewards = [
                        {
                            "rewardsDailyRate": r.get("rewards_daily_rate")
                            or r.get("rate_per_day")
                            or 0.0,
                            "startDate": r.get("start_date"),
                            "endDate": r.get("end_date"),
                        }
                        for r in rates
                        if isinstance(r, dict)
                    ]
                    rows.append({
                        "conditionId": row.get("condition_id"),
                        "question": row.get("question"),
                        "slug": row.get("market_slug"),
                        "endDate": row.get("end_date_iso") or row.get("end_date"),
                        "closed": bool(row.get("closed")),
                        "archived": bool(row.get("archived")),
                        "negRisk": bool(row.get("neg_risk")),
                        "clobTokenIds": tokens,
                        "clobRewards": rewards,
                        "rewardsMinSize": (row.get("rewards") or {}).get("min_size", 0.0),
                        "rewardsMaxSpread": (row.get("rewards") or {}).get("max_spread", 0.0),
                        "volume": _f(row.get("volume_24hr")),
                        "liquidity": 0.0,
                    })
                cursor = payload.get("next_cursor")
                if not cursor:
                    break
                time.sleep(0.05)
            if rows:
                logger.info(f"CLOB sampling sweep: {len(rows)} markets harvested in {page + 1} pages")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"CLOB sampling sweep unavailable ({exc}); falling back to Gamma")

        # Tier 2: Add short-duration & sentinel markets from Gamma
        try:
            short_rows = self._fetch_short_duration_markets(limit=100)
            sentinel_rows = self._fetch_deterministic_sentinels()
            rows.extend(short_rows)
            rows.extend(sentinel_rows)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"short-duration / sentinel sweep enrichment failed: {exc}")

        if rows:
            # Deduplicate by conditionId
            seen: set[str] = set()
            deduped: list[dict[str, Any]] = []
            for r in rows:
                cid = self._row_cid(r)
                if cid and cid not in seen:
                    seen.add(cid)
                    deduped.append(r)
            return deduped

        # Tier 3: Keyset crawl fallback (strictly bounded to at most 50 pages / ~5,000 markets)
        logger.info("falling back to bounded Gamma keyset crawl")
        cursor = None
        bounded_pages = min(int(self.max_pages), 50)
        for page in range(bounded_pages):
            params = {
                "limit": 100,
                "include_tag": "true",
                "closed": "false",
            }
            if cursor:
                params["after_cursor"] = cursor
            payload = self._get_json(GAMMA_KEYSET, params)
            markets = payload.get("markets") or []
            if not markets:
                break
            rows.extend(markets)
            cursor = payload.get("next_cursor")
            if page % 10 == 0:
                logger.info(f"Gamma keyset page {page}: {len(rows):,} markets")
            if not cursor:
                break
            time.sleep(0.05)
        return rows

    def _build(
        self, rows: list[dict[str, Any]], *, include_locked_baseline: bool = True
    ) -> dict[str, Market]:
        """Qualifying markets from Gamma rows.

        In locked mode only conditionIds in the pinned set survive, and (for a
        full crawl) pinned markets Gamma no longer lists are retained from the
        baseline so their tokens stay subscribed. The incremental poll passes
        ``include_locked_baseline=False``: its few rows say nothing about
        absence, and the baseline must not resurrect a resolved market.

        Below-cutoff rows matching ``sentinel_question_patterns`` are kept as
        sentinels with ``sentinel=True`` and ``daily_pool=0``: they are
        subscribed so Gate 3 and lead-lag stay measurable, but they are not
        allocator targets and, in locked mode, sit outside the pinned set.
        """
        out: dict[str, Market] = {}
        now = time.time()
        locked = self.locked if self.mode == "locked" else None
        present: set[str] = set()
        for row in rows:
            cid = self._row_cid(row)
            in_locked = locked is not None and cid in locked
            if in_locked:
                present.add(cid)
            if row.get("closed") or row.get("archived"):
                continue
            rewards = row.get("clobRewards") or []
            daily = 0.0
            for campaign in rewards:
                start = _parse_ts(campaign.get("startDate"))
                end = _parse_ts(campaign.get("endDate"))
                if start is not None and start > now:
                    continue
                if end is not None and end < now:
                    continue
                daily += _f(campaign.get("rewardsDailyRate"))
            tokens = _parse_tokens(row.get("clobTokenIds"))
            if not cid or not tokens:
                continue
            sentinel = False
            if daily > self.cutoff:
                if locked is not None and not in_locked:
                    continue
            else:
                # Below the reward cutoff: keep it only as a sentinel, so the
                # crypto up/down markets stay subscribed without joining the
                # allocator set or the reward pool total.
                if not self._matches_sentinel(row):
                    continue
                end_ts = _parse_ts(row.get("endDate"))
                if end_ts is not None and end_ts < now - SENTINEL_STALE_SECONDS:
                    # Five-minute binaries stay closed=false past their end
                    # while resolution settles; subscribing to stale ones only
                    # churns the sockets.
                    continue
                sentinel = True
                daily = 0.0
            events = row.get("events") or []
            event = events[0] if events and isinstance(events[0], dict) else {}
            market = Market(
                condition_id=str(cid),
                question=str(row.get("question") or ""),
                slug=str(row.get("slug") or ""),
                daily_pool=daily,
                min_size=_f(row.get("rewardsMinSize")),
                max_spread=_f(row.get("rewardsMaxSpread")),
                volume=_f(row.get("volume")),
                liquidity=_f(row.get("liquidity")),
                neg_risk=bool(row.get("negRisk")),
                tokens=tokens,
                end_date=row.get("endDate"),
                sentinel=sentinel,
                event_id=str(event.get("id")) if event.get("id") is not None else None,
                event_slug=str(event.get("slug")) if event.get("slug") else None,
                event_title=str(event.get("title")) if event.get("title") else None,
                fee_type=str(row.get("feeType")) if row.get("feeType") else None,
                fees_enabled=row.get("feesEnabled"),
                tags=_tag_labels(row),
            )
            out[str(cid)] = self._with_metadata(market)
        if locked is not None and include_locked_baseline:
            for cid, market in locked.items():
                if cid not in present and cid not in self._retired:
                    out[cid] = market
        cap = self.sentinel_max_markets
        if cap > 0:
            sentinels = [m for m in out.values() if m.sentinel]
            if len(sentinels) > cap:
                keep = {
                    m.condition_id
                    for m in sorted(
                        sentinels,
                        key=lambda m: (m.volume, m.liquidity),
                        reverse=True,
                    )[:cap]
                }
                out = {
                    cid: m
                    for cid, m in out.items()
                    if not m.sentinel or cid in keep
                }
        return out

    def _matches_sentinel(self, row: dict[str, Any]) -> bool:
        """True for an open crypto up/down row matched by a sentinel pattern."""
        if not self.sentinel_enabled:
            return False
        text = f"{row.get('question') or ''} {row.get('slug') or ''}"
        if self.sentinel_patterns and any(p.search(text) for p in self.sentinel_patterns):
            return True
        slug = str(row.get("slug") or "").lower()
        if "-updown-" in slug and any(slug.startswith(f"{a}-") for a in SENTINEL_CRYPTO_ASSETS):
            return True
        return False

    # ------------------------------------------------------------------ locked
    def _load_locked(self) -> dict[str, Market]:
        """The pinned market set from ``clob.locked_manifest``.

        The file is the same JSON list the rolling manifest uses, so an old
        ``manifest_locked_1028.json`` snapshot loads unchanged.
        """
        path = self.locked_manifest
        if not path.exists():
            raise FileNotFoundError(
                f"clob.universe_mode is 'locked' but clob.locked_manifest "
                f"does not exist: {path}"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"unreadable clob.locked_manifest {path}: {exc}"
            ) from exc
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("markets") or []
        else:
            items = []
        markets = self._markets_from_items(items)
        if not markets:
            raise ValueError(
                f"clob.locked_manifest {path} contains no usable markets"
            )
        return markets

    def _install_locked(self) -> None:
        """Seed the live set from the pinned baseline, before any crawl."""
        self.markets = {
            cid: m for cid, m in self.locked.items() if cid not in self._retired
        }
        self.asset_to_market = {
            t: m.condition_id for m in self.markets.values() for t in m.tokens
        }
        self.generated_at = None
        self.last_full_walk_ts = 0.0
        self.last_poll_ts = 0.0
        self.last_market_id = 0
        self._stale = False
        self._load_state()
        # Force the refresh loop's first crawl: it is what logs the coverage
        # count, and the baseline already carries every pinned token.
        self.last_full_walk_ts = 0.0
        self._write_manifest(version=False)

    # ------------------------------------------------------------------ manifest
    def _load_manifest(self) -> bool:
        """Load the newest usable manifest, stale or not.

        A stale manifest is still the right thing to subscribe to at startup:
        blocking a 14-day run on a 40k-market Gamma crawl means minutes of
        zero coverage. The refresh loop applies the diff when it completes.
        """
        candidates = [self.manifest_path]
        legacy = self.manifest_path.parent / "manifest_locked_1028.json"
        if legacy.exists() and legacy != self.manifest_path:
            candidates.append(legacy)

        for path in candidates:
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"unreadable universe manifest {path}: {exc}")
                continue
            if isinstance(payload, list):
                raw_markets = payload
                generated = None
            elif isinstance(payload, dict):
                raw_markets = payload.get("markets") or []
                generated = payload.get("generated_at")
            else:
                continue
            markets = self._markets_from_items(raw_markets)
            if not markets:
                continue
            self.markets = markets
            self.asset_to_market = {
                t: m.condition_id for m in markets.values() for t in m.tokens
            }
            self.generated_at = generated
            self.last_full_walk_ts = _parse_ts(generated) or 0.0
            self.last_poll_ts = self.last_full_walk_ts
            self.last_market_id = 0
            self._load_state()
            if self.last_full_walk_ts:
                age = time.time() - self.last_full_walk_ts
                self._stale = age > self.refresh_seconds
                if self._stale:
                    logger.info(f"universe manifest is {age / 3600:.1f}h old; refreshing")
            else:
                # Legacy list format predates the midterms; always refresh.
                self._stale = True
            return True
        return False

    @staticmethod
    def _markets_from_items(raw_markets: list[dict[str, Any]]) -> dict[str, Market]:
        markets: dict[str, Market] = {}
        for item in raw_markets:
            try:
                m = Market(
                    condition_id=str(item["condition_id"]),
                    question=str(item.get("question") or ""),
                    slug=str(item.get("slug") or ""),
                    daily_pool=_f(item.get("daily_pool")),
                    min_size=_f(item.get("min_size")),
                    max_spread=_f(item.get("max_spread")),
                    volume=_f(item.get("volume")),
                    liquidity=_f(item.get("liquidity")),
                    neg_risk=bool(item.get("neg_risk")),
                    tokens=tuple(str(t) for t in item.get("clob_tokens") or []),
                    end_date=item.get("end_date"),
                    sentinel=bool(item.get("sentinel")),
                    event_id=item.get("event_id"),
                    event_slug=item.get("event_slug"),
                    event_title=item.get("event_title"),
                    fee_type=item.get("fee_type"),
                    fees_enabled=item.get("fees_enabled"),
                    fee_rate=item.get("fee_rate"),
                    fee_exponent=item.get("fee_exponent"),
                    fee_rebate=item.get("fee_rebate"),
                    resolution_source=item.get("resolution_source"),
                    tick_size=item.get("tick_size"),
                    min_order_size=item.get("min_order_size"),
                    tags=tuple(str(t) for t in item.get("tags") or []),
                )
            except (KeyError, TypeError):
                continue
            if m.tokens:
                markets[m.condition_id] = m
        return markets

    def _write_manifest(self, *, version: bool = True) -> None:
        # The metadata refresh job rewrites the parquet independently of the
        # universe walk; reload before writing so its fields are not lost.
        if self._metadata_path is not None:
            self._metadata = load_rows(self._metadata_path)
        payload = {
            "generated_at": self.generated_at,
            "last_full_walk_at": self._iso(self.last_full_walk_ts),
            "last_poll_at": self._iso(self.last_poll_ts),
            "last_market_id": self.last_market_id,
            "cutoff_daily_reward": self.cutoff,
            "universe_mode": self.mode,
            "count": len(self.markets),
            "sentinel_enabled": self.sentinel_enabled,
            "sentinel_count": self.sentinel_count,
            # Sentinels are not reward targets; only allocator pools count.
            "total_daily_pool": self.reward_pool_total,
            "markets": [self._with_metadata(m).to_dict() for m in self.markets.values()],
        }
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(tmp, self.manifest_path)
        if version:
            stamp = datetime.now(IST).strftime("%Y%m%dT%H%M%S")
            version_path = self.manifest_path.parent / "universe" / f"{stamp}.json"
            version_path.parent.mkdir(parents=True, exist_ok=True)
            version_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------ access
    def assets(self) -> list[str]:
        return list(self.asset_to_market)

    @property
    def sentinel_count(self) -> int:
        return sum(1 for m in self.markets.values() if m.sentinel)

    @property
    def reward_pool_total(self) -> float:
        """Daily reward pool over allocator markets; sentinels contribute 0."""
        return sum(m.daily_pool for m in self.markets.values() if not m.sentinel)

    def market_for(self, asset_id: str) -> Market | None:
        cid = self.asset_to_market.get(str(asset_id))
        return self.markets.get(cid) if cid else None
