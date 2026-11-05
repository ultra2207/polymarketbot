"""Whale and competitor activity stream.

The Data API leaderboard names the wallets making money *today*; the activity
feed then says what those wallets actually did. One poll is:

1. ``GET /v2/leaderboard?time_period=day&limit=<leaderboard_limit>``
2. ``GET /v2/activity?user=<wallet>&type=TRADE&limit=<activity_limit>``
   for each wallet on the board.

Rows land in ``whale_activity``. The poll cadence is deliberately slow
(hourly by default): this is a competitor watchlist, not a tape, and the
activity feed's 200 req/10 s limit is shared with other clients.

Parsing is pure and testable: :func:`parse_leaderboard_wallets` and
:func:`parse_activity_rows` take decoded JSON only. Dedupe is by
``(tx_hash, wallet, activity_ts_ms)`` across polls, because the feed is
keyset-paginated from the newest row and the same trade reappears on every
poll until it falls off the limit.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import orjson
import requests

from polymarketbot.data.context import CollectorContext
from polymarketbot.data.schema import WHALE_ACTIVITY

NAME = "whales"
DEFAULT_BASE_URL = "https://data-api.polymarket.com"
DEFAULT_POLL_SECONDS = 3600.0
DEFAULT_LEADERBOARD_LIMIT = 50
DEFAULT_ACTIVITY_LIMIT = 100

# Docs: /v2/activity is 200 req/10 s. 0.06 s spacing is ~16 req/s, far below it.
MIN_REQUEST_INTERVAL_S = 0.06
MAX_RETRY_SLEEP_S = 30.0
DEDUPE_MAX_KEYS = 250_000


# --------------------------------------------------------------------- parsing
def _number(value: Any) -> float | None:
    """JSON number or numeric string -> float; null stays null."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def timestamp_to_ms(value: Any) -> int | None:
    """Normalise the venue's timestamp to epoch milliseconds.

    v2 activity rows carry epoch **seconds**; older/other rows carry
    milliseconds or an ISO-8601 string. Values below 1e11 are seconds
    (1e11 seconds is year 5138; 1e11 ms is 1973, so the split is safe for
    both plausible error directions).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds <= 0:
            return None
        return int(seconds * 1000) if seconds < 1e11 else int(seconds)
    text = str(value).strip()
    if not text:
        return None
    try:
        return timestamp_to_ms(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def parse_leaderboard_wallets(payload: Any) -> list[str]:
    """Extract wallet addresses from a ``/v2/leaderboard`` envelope.

    The ranked-board row names the wallet ``user_id``. ``wallet`` and
    ``proxy_wallet`` are accepted as aliases because the same envelope is
    used by other boards/feeds. Duplicates and blanks are dropped.
    """
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    wallets: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        wallet = _text(
            row.get("user_id")
            or row.get("wallet")
            or row.get("proxy_wallet")
            or row.get("user")
        )
        if not wallet or wallet in seen:
            continue
        seen.add(wallet)
        wallets.append(wallet)
    return wallets


def normalise_activity_row(
    row: dict[str, Any],
    wallet: str,
    *,
    recv_ts_ns: int,
    recv_mono_ns: int,
) -> dict[str, Any]:
    """One activity row -> one ``whale_activity`` row.

    Documented v2 fields are ``proxy_wallet``, ``condition_id``, ``token_id``,
    ``side``, ``size``, ``price``, ``usdc_size``, ``timestamp`` (epoch s),
    ``transaction_hash`` and ``type``. Aliases are used only where the docs
    show two spellings; unknown fields are never invented.
    """
    activity_ts = (
        row.get("timestamp")
        if row.get("timestamp") is not None
        else row.get("block_timestamp")
        if row.get("block_timestamp") is not None
        else row.get("time")
    )
    return {
        "recv_ts_ns": recv_ts_ns,
        "recv_mono_ns": recv_mono_ns,
        "wallet": _text(row.get("proxy_wallet") or row.get("wallet")) or wallet,
        "condition_id": _text(row.get("condition_id") or row.get("conditionId")),
        "asset_id": _text(row.get("token_id") or row.get("asset_id")),
        "side": _text(row.get("side")),
        "size": _number(row.get("size") if row.get("size") is not None else row.get("shares")),
        "price": _number(row.get("price")),
        "usdc_size": _number(
            row.get("usdc_size") if row.get("usdc_size") is not None else row.get("amount")
        ),
        "activity_ts_ms": timestamp_to_ms(activity_ts),
        "tx_hash": _text(row.get("transaction_hash") or row.get("tx_hash")),
        "activity_type": _text(row.get("type") or row.get("activity_type")),
        "raw_json": orjson.dumps(row).decode(),
    }


def parse_activity_rows(
    payload: Any,
    wallet: str,
    *,
    recv_ts_ns: int | None = None,
    recv_mono_ns: int | None = None,
) -> list[dict[str, Any]]:
    """Decode an activity envelope into ``whale_activity`` rows."""
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    ts = time.time_ns() if recv_ts_ns is None else recv_ts_ns
    mono = time.perf_counter_ns() if recv_mono_ns is None else recv_mono_ns
    return [
        normalise_activity_row(row, wallet, recv_ts_ns=ts, recv_mono_ns=mono)
        for row in rows
        if isinstance(row, dict)
    ]


def dedupe_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return (
        row.get("tx_hash") or "",
        row.get("wallet") or "",
        int(row.get("activity_ts_ms") or 0),
    )


class ActivityDeduper:
    """Bounded set of ``(tx_hash, wallet, activity_ts_ms)`` keys.

    Every poll re-reads the newest ``activity_limit`` rows per wallet, so
    without this the same trade would be stored hourly. Eviction is FIFO;
    old keys fall out long after the rows have scrolled off the feed.
    """

    def __init__(self, max_keys: int = DEDUPE_MAX_KEYS) -> None:
        self._keys: dict[tuple[str, str, int], None] = {}
        self._max_keys = max(1, int(max_keys))

    def __len__(self) -> int:
        return len(self._keys)

    def accept(self, row: dict[str, Any]) -> bool:
        key = dedupe_key(row)
        if key in self._keys:
            return False
        self._keys[key] = None
        while len(self._keys) > self._max_keys:
            self._keys.pop(next(iter(self._keys)))
        return True


def parse_retry_after(value: str | None, default: float) -> float:
    """``Retry-After`` is either delta-seconds or an HTTP date."""
    if not value:
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


# ------------------------------------------------------------------------ HTTP
@dataclass
class HttpResult:
    status: int | None
    payload: dict[str, Any] | None
    url: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and isinstance(self.payload, dict)

    @property
    def unusable(self) -> bool:
        """Endpoints reject 4xx deterministically; retrying cannot help."""
        return self.status is not None and 400 <= self.status < 500 and self.status != 429


class DataApiHttp:
    """Small synchronous client; call it through ``asyncio.to_thread``."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_s: float = 30.0,
        max_retries: int = 4,
        min_interval_s: float = MIN_REQUEST_INTERVAL_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max(1, max_retries)
        self.min_interval_s = max(0.0, min_interval_s)
        self.session = requests.Session()
        self.session.headers.setdefault("User-Agent", "polymarketbot-data/0.1")
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def get_json(self, path: str, params: Any = None) -> HttpResult:
        request = self.session.prepare_request(
            requests.Request("GET", f"{self.base_url}{path}", params=params)
        )
        url = request.url or f"{self.base_url}{path}"
        last: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                response = self.session.send(request, timeout=self.timeout_s)
            except Exception as exc:  # noqa: BLE001 - DNS/TLS/connection flakiness
                last = exc
                time.sleep(min(MAX_RETRY_SLEEP_S, 1.0 * (attempt + 1)))
                continue

            if response.status_code == 429:
                delay = parse_retry_after(
                    response.headers.get("Retry-After"), default=2.0 * (attempt + 1)
                )
                time.sleep(min(delay, MAX_RETRY_SLEEP_S))
                last = RuntimeError("429 Too Many Requests")
                continue
            if response.status_code >= 500:
                time.sleep(min(MAX_RETRY_SLEEP_S, 1.5 * (attempt + 1)))
                last = RuntimeError(f"HTTP {response.status_code}")
                continue
            if response.status_code != 200:
                return HttpResult(
                    response.status_code,
                    None,
                    url,
                    f"HTTP {response.status_code}: {response.text[:300]}",
                )
            try:
                payload = response.json()
            except ValueError as exc:
                return HttpResult(200, None, url, f"invalid JSON: {exc}")
            if not isinstance(payload, dict):
                return HttpResult(200, None, url, "response is not a JSON object")
            return HttpResult(200, payload, url)
        return HttpResult(None, None, url, str(last))


# ------------------------------------------------------------------------ poll
async def poll_once(
    ctx: CollectorContext,
    http: DataApiHttp,
    dedupe: ActivityDeduper,
    *,
    leaderboard_limit: int,
    activity_limit: int,
) -> tuple[int, int, int]:
    """One leaderboard + activity sweep. Returns ``(wallets, rows, errors)``."""
    log = ctx.log(NAME)
    errors = 0

    board = await asyncio.to_thread(
        http.get_json,
        "/v2/leaderboard",
        {"time_period": "day", "limit": leaderboard_limit},
    )
    if not board.ok:
        if board.unusable:
            log.error(
                f"leaderboard endpoint unusable: {board.url} -> HTTP {board.status} "
                f"({board.error}); skipping this poll"
            )
        else:
            log.error(f"leaderboard fetch failed: {board.url} ({board.error})")
        ctx.health.incr(NAME, "errors")
        return 0, 0, 1

    wallets = parse_leaderboard_wallets(board.payload)
    ctx.health.gauge(NAME, "wallets", len(wallets))
    log.info(f"leaderboard: {len(wallets)} wallets ({board.url})")

    emitted = 0
    consecutive_transport_failures = 0
    for wallet in wallets:
        if ctx.stop.is_set():
            break
        result = await asyncio.to_thread(
            http.get_json,
            "/v2/activity",
            [("user", wallet), ("type", "TRADE"), ("limit", activity_limit)],
        )
        recv_ts_ns = time.time_ns()
        recv_mono_ns = time.perf_counter_ns()
        if not result.ok:
            errors += 1
            ctx.health.incr(NAME, "errors")
            if result.unusable:
                consecutive_transport_failures = 0
                log.error(
                    f"activity endpoint rejected wallet {wallet}: {result.url} -> "
                    f"HTTP {result.status} ({result.error}); skipping this wallet"
                )
            else:
                consecutive_transport_failures += 1
                log.warning(f"activity fetch failed for {wallet}: {result.url} ({result.error})")
                if consecutive_transport_failures >= 5:
                    log.error(
                        "activity endpoint failed 5 times consecutively; ending this poll "
                        "early and retrying on the next scheduled poll"
                    )
                    break
            continue
        consecutive_transport_failures = 0

        rows = parse_activity_rows(
            result.payload, wallet, recv_ts_ns=recv_ts_ns, recv_mono_ns=recv_mono_ns
        )
        fresh = [row for row in rows if dedupe.accept(row)]
        if len(rows) != len(fresh):
            log.debug(f"{wallet}: {len(rows) - len(fresh)} duplicate activity rows skipped")
        for row in fresh:
            ctx.emit("whale_activity", row)
        emitted += len(fresh)
        ctx.health.incr(NAME, "rows", len(fresh))

    return len(wallets), emitted, errors


async def _wait_until(ctx: CollectorContext, deadline: float, last_poll_done: float) -> None:
    """Sleep until ``deadline``, publishing poll age while idle."""
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
    from polymarketbot.net import install_doh_fallback

    install_doh_fallback()  # idempotent; the orchestrator already did this
    cfg = ctx.section(NAME)
    log = ctx.log(NAME)
    ctx.register_table("whale_activity", WHALE_ACTIVITY)

    http = DataApiHttp(str(cfg.get("base_url") or DEFAULT_BASE_URL))
    dedupe = ActivityDeduper()
    poll_seconds = max(60.0, float(cfg.get("poll_seconds", DEFAULT_POLL_SECONDS)))
    leaderboard_limit = max(1, int(cfg.get("leaderboard_limit", DEFAULT_LEADERBOARD_LIMIT)))
    activity_limit = max(1, int(cfg.get("activity_limit", DEFAULT_ACTIVITY_LIMIT)))

    log.info(
        f"starting: base={http.base_url} poll={poll_seconds:.0f}s "
        f"leaderboard_limit={leaderboard_limit} activity_limit={activity_limit}"
    )

    last_poll_done = time.monotonic()
    while not ctx.stop.is_set():
        started = time.monotonic()
        try:
            wallets, rows, errors = await poll_once(
                ctx,
                http,
                dedupe,
                leaderboard_limit=leaderboard_limit,
                activity_limit=activity_limit,
            )
            log.info(
                f"poll done: wallets={wallets} new_rows={rows} errors={errors} "
                f"elapsed={time.monotonic() - started:.1f}s"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one poll must not kill the stream
            ctx.health.incr(NAME, "errors")
            log.exception(f"poll failed: {exc}")
        last_poll_done = time.monotonic()
        ctx.health.gauge(NAME, "last_poll_age_s", 0.0)
        await _wait_until(ctx, started + poll_seconds, last_poll_done)
