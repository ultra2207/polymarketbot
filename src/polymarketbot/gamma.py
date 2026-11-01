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
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

import requests

from polymarketbot.fees import FeeSchedule

BASE = "https://gamma-api.polymarket.com"
MAX_BATCH = 100


@dataclass
class GammaClient:
    min_interval_s: float = 0.10   # 10 req/s, well inside 300/10s
    timeout_s: float = 30.0
    max_retries: int = 5
    session: requests.Session = field(default_factory=requests.Session)
    _last_call: float = 0.0

    def __post_init__(self) -> None:
        self.session.headers.setdefault("User-Agent", "polymarketbot-research/0.1")

    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def get(self, path: str, params: dict | list[tuple] | None = None) -> Any:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                r = self.session.get(f"{BASE}{path}", params=params, timeout=self.timeout_s)
                if r.status_code == 429:
                    time.sleep(float(r.headers.get("Retry-After", 2 * (attempt + 1))))
                    continue
                if r.status_code >= 500:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"GET {path} failed after {self.max_retries} attempts: {last}")

    # ------------------------------------------------------------------ markets
    def market(self, condition_id: str) -> dict | None:
        rows = self.markets([condition_id])
        return rows[0] if rows else None

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
            for m in payload.get("markets") or []:
                yield m
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
