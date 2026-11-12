"""Client for the Polymarket Gamma API (market/event metadata).

Gamma is the only free source for several fields the tape does not carry:

* `version` - "v1" (CTF) or "v2" (PositionManager). **Position system, not
  exchange generation.** A market can be `v1` while trading on CTF Exchange V2.
* `clobTokenIds` (v1) and `positionIds` (v2) - the outcome identifiers. This
  matters: a position ID must never be sent to Conditional Tokens, and vice
  versa.
* `feeSchedule` {rate, exponent, takerOnly, rebateRate} and `feeType`
  (`sports_fees_v2` 0.03/0.25 vs `sports_fees_v3` 0.05/0.15, `crypto_fees_v2`).
* `orderPriceMinTickSize`, `orderMinSize` (minimum USDC notional).
* `negRisk`, `umaResolutionStatuses`, tags.

Pagination
----------
`/markets/keyset` and `/events/keyset` use `after_cursor` (not `cursor`, not
`offset`). **`closed` is not optional for historical work**: omitting it on
`/markets/keyset` returns *open markets only*, which silently makes every
historical lookup return zero rows.

Rate limits: 300 req/10s on `/markets`, 500 on `/events`. This client defaults
to 10 req/s.

Errors
------
Three classes, because the caller acts differently on each:

* :class:`GammaTransportError` - the request never completed (connection,
  timeout). Retried with backoff.
* :class:`GammaHTTPError` - the venue answered 429 or 5xx (retried) or any
  other 4xx (raised immediately: retrying a malformed request cannot fix it,
  and it used to burn five backoffs and 20+ seconds per call).
* :class:`GammaUnparseable` - a 2xx body that is not JSON. Retried, because a
  truncated body can be a proxy artefact, and raised as itself if it survives.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import requests

from polymarketbot.fees import FeeSchedule
from polymarketbot.logs import get_logger

logger = get_logger("gamma")

BASE = "https://gamma-api.polymarket.com"
MAX_BATCH = 100
# A venue that answers 429 with a multi-hour Retry-After must fail the request,
# not freeze the caller for the duration; the walk retries on its next tick.
MAX_RETRY_SLEEP_S = 60.0


class GammaError(RuntimeError):
    """A Gamma request that could not be completed."""


class GammaTransportError(GammaError):
    """The request never reached an answer (DNS, connect, TLS, timeout)."""


class GammaHTTPError(GammaError):
    """Gamma answered with a non-retryable status, or retries ran out on a 4xx/5xx."""

    def __init__(self, status: int, path: str, detail: str = "") -> None:
        self.status = status
        self.path = path
        message = f"GET {path} -> HTTP {status}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class GammaUnparseable(GammaError):
    """A 2xx response whose body is not the JSON the caller asked for."""


@dataclass
class GammaClient:
    min_interval_s: float = 0.10   # 10 req/s, well inside 300/10s
    timeout_s: float = 30.0
    max_retries: int = 5
    session: requests.Session = field(default_factory=requests.Session)
    _last_call: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.session.headers.setdefault("User-Agent", "polymarketbot-research/0.1")

    def _throttle(self) -> None:
        # One client is shared by the universe walker and the metadata refresh;
        # an unlocked read/modify of `_last_call` let two threads both see an
        # expired wait and fire together, which is the burst the venue resets.
        with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    @staticmethod
    def _retry_after_s(response: requests.Response, attempt: int) -> float:
        """Seconds to rest after a 429: the header when it is a number, else linear backoff."""
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return max(0.0, min(float(raw), MAX_RETRY_SLEEP_S))
            except ValueError:
                pass  # HTTP-date form; fall back to linear backoff
        return min(2.0 * (attempt + 1), MAX_RETRY_SLEEP_S)

    def get(self, path: str, params: dict | list[tuple] | None = None) -> Any:
        last: GammaError | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                r = self.session.get(
                    f"{BASE}{path}", params=params, timeout=self.timeout_s
                )
            except requests.RequestException as exc:
                last = GammaTransportError(f"GET {path}: {exc}")
                time.sleep(min(1.5 * (attempt + 1), MAX_RETRY_SLEEP_S))
                continue
            if r.status_code == 429:
                last = GammaHTTPError(429, path, "rate limited")
                time.sleep(self._retry_after_s(r, attempt))
                continue
            if r.status_code >= 500:
                last = GammaHTTPError(r.status_code, path)
                time.sleep(min(1.5 * (attempt + 1), MAX_RETRY_SLEEP_S))
                continue
            if r.status_code >= 400:
                # 4xx other than 429 is the caller's request. It used to be
                # caught by the blanket handler and retried five times.
                raise GammaHTTPError(r.status_code, path, r.text[:200])
            try:
                return r.json()
            except ValueError as exc:
                last = GammaUnparseable(f"GET {path}: body is not JSON ({exc})")
                time.sleep(min(1.5 * (attempt + 1), MAX_RETRY_SLEEP_S))
        if isinstance(last, GammaUnparseable):
            raise last
        raise GammaError(
            f"GET {path} failed after {self.max_retries} attempts: {last}"
        ) from last

    # ------------------------------------------------------------------ markets
    def market(self, condition_id: str) -> dict | None:
        rows = self.markets([condition_id])
        return rows[0] if rows else None

    def market_meta(self, condition_id: str) -> dict | None:
        """Settlement-relevant fields for one market, keyed by snake_case.

        The Gamma field names are camelCase: ``version``, ``negRisk``,
        ``negRiskMarketID`` (the neg-risk grouping id), ``positionIds``,
        ``clobTokenIds`` and ``orderMinSize``. Returns ``None`` when the
        market itself cannot be found. An absent ``negRiskMarketID`` is
        logged and returned as ``None``; that is normal for a non-neg-risk
        market but leaves a neg-risk market unroutable.
        """
        row = self.market(condition_id)
        if not row:
            logger.warning(f"Gamma returned no market for condition {condition_id}")
            return None

        neg_risk = bool(row.get("negRisk"))
        market_id = row.get("negRiskMarketID") or None
        if neg_risk and not market_id:
            logger.warning(
                f"Gamma market {condition_id} is negRisk but has no "
                "negRiskMarketID; neg-risk settlement cannot be routed"
            )
        return {
            "condition_id": row.get("conditionId") or condition_id,
            "version": row.get("version"),
            "neg_risk": neg_risk,
            "position_ids": self.parse_ids(row.get("positionIds")),
            "clob_token_ids": self.parse_ids(row.get("clobTokenIds")),
            "neg_risk_market_id": market_id,
            "order_min_size": self.min_order_size(row),
        }

    def markets(self, condition_ids: Iterable[str]) -> list[dict]:
        """Batch lookup by condition id. `closed=any` is required for history."""
        ids = list(dict.fromkeys(condition_ids))
        out: list[dict] = []
        for i in range(0, len(ids), MAX_BATCH):
            chunk = ids[i:i + MAX_BATCH]
            params = ([("condition_ids", c) for c in chunk]
                      + [("closed", "any"), ("limit", str(MAX_BATCH))])
            payload = self.get("/markets/keyset", params)
            out.extend(payload.get("markets") or [])
        return out

    def markets_keyset(self, *, closed: str | None = None, version: str | None = None,
                       after_cursor: str | None = None, limit: int = 100,
                       extra: dict | None = None) -> dict:
        params: list[tuple] = [("limit", str(min(limit, 100)))]
        if closed is not None:
            params.append(("closed", closed))
        if version:
            params.append(("version", version))
        if after_cursor:
            params.append(("after_cursor", after_cursor))
        for k, v in (extra or {}).items():
            params.append((k, str(v)))
        return self.get("/markets/keyset", params)

    def events_keyset(self, *, closed: str | None = None, version: str | None = None,
                      after_cursor: str | None = None, limit: int = 100,
                      extra: dict | None = None) -> dict:
        params: list[tuple] = [("limit", str(min(limit, 100)))]
        if closed is not None:
            params.append(("closed", closed))
        if version:
            params.append(("version", version))
        if after_cursor:
            params.append(("after_cursor", after_cursor))
        for k, v in (extra or {}).items():
            params.append((k, str(v)))
        return self.get("/events/keyset", params)

    def iter_markets(self, *, closed: str | None = None, version: str | None = None,
                     max_pages: int | None = None,
                     extra: dict | None = None) -> Iterator[dict]:
        cursor = None
        pages = 0
        while True:
            payload = self.markets_keyset(closed=closed, version=version,
                                          after_cursor=cursor, extra=extra)
            yield from payload.get("markets") or []
            pages += 1
            cursor = payload.get("next_cursor")
            if not cursor or (max_pages and pages >= max_pages):
                return

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def parse_ids(value: Any) -> list[str] | None:
        """`clobTokenIds` arrives as a JSON *string*; `positionIds` as a list."""
        if value is None:
            return None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return None
        return [str(x) for x in value] if isinstance(value, list) else None

    @classmethod
    def fee_schedule(cls, market: dict) -> FeeSchedule | None:
        sched = market.get("feeSchedule")
        if not sched:
            return None
        return FeeSchedule.from_gamma(sched)

    @staticmethod
    def tick_size(market: dict) -> float | None:
        for key in ("orderPriceMinTickSize", "orderMinTickSize", "tickSize"):
            v = market.get(key)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def min_order_size(market: dict) -> float | None:
        for key in ("orderMinSize", "minimumOrderSize"):
            v = market.get(key)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def tag_labels(market: dict) -> list[str]:
        return [t.get("label") for t in (market.get("tags") or []) if t.get("label")]
