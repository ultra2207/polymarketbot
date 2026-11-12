"""Client for the Polymarket Data API v2.

Why this exists
---------------
The v2 API exposes things the trade tape cannot give us cheaply:

* `/v2/activity?user=` - a wallet's whole lifecycle: trades (both maker and
  taker), splits, merges, redeems. **Use this for wallet reconstruction.**
* `/v2/trades?user=&full_history=true` - a *taker-perspective* trade feed.
  Verified 2026-11-01: a wallet that only made (0 taker fills) returns 0 rows
  here but 6 TRADE rows on `/v2/activity`. Do not treat `/v2/trades?user=` as a
  complete wallet history.
* `/v2/positions?user=` - open/closed positions with realized PnL and fee basis
* `/v2/prices-history?token_id=&interval=max` - a token's full price series
  (12h buckets back to 2022; 3h from July 2026; finer grains are windowed)
* `/v2/resolutions?condition=` - resolution state, disputes, resolved_at
* `/v2/tokens?condition=` - token ids, module (v1_ctf/v2...), final_price
* `/v2/leaderboard`, `/v2/biggest-winners` - ranked wallets, without scanning
  every wallet in the tape

Rate limits (docs, 2026-11-01): 800 req/10s general on /v2; 300/10s for
`/v2/trades`; 200/10s for positions, activity and prices-history. This client
defaults to 20 req/s, well inside all of them, and honours `Retry-After`.

Notes
-----
* Every paginated response wraps rows in `data` and adds `pagination` with an
  opaque `next_cursor`. There is no offset for v2.
* Bare `size`/`volume` fields are **shares**; `_usdc` fields are USD.
* A null numeric field means *unavailable*, never zero.
* `/v2/holders` returns **current** holders only. A fully redeemed market
  returns none, so it is not a source of final PnL for closed markets. Use
  `/v2/positions?user=` with `status=CLOSED` instead.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import requests

BASE = "https://data-api.polymarket.com"


@dataclass
class DataAPIClient:
    min_interval_s: float = 0.05   # 20 req/s
    timeout_s: float = 30.0
    max_retries: int = 5
    session: requests.Session = field(default_factory=requests.Session)
    _last_call: float = 0.0

    def __post_init__(self) -> None:
        self.session.headers.setdefault("User-Agent", "polymarketbot-research/0.1")

    # ---------------------------------------------------------------- plumbing
    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def get(self, path: str, params: dict | list[tuple] | None = None) -> dict:
        """GET one page. Raises on failure after retries."""
        last: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                r = self.session.get(f"{BASE}{path}", params=params, timeout=self.timeout_s)
                if r.status_code == 429:
                    retry_after = float(r.headers.get("Retry-After", 2 * (attempt + 1)))
                    time.sleep(min(retry_after, 30))
                    continue
                if r.status_code >= 500:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:  # noqa: BLE001 - includes DNS flakiness
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"GET {path} failed after {self.max_retries} attempts: {last}")

    def paginate(
        self,
        path: str,
        params: dict | list[tuple] | None = None,
        *,
        max_pages: int | None = None,
        page_size: int = 500,
    ) -> Iterator[dict]:
        base = list(params.items()) if isinstance(params, dict) else list(params or [])
        cursor: str | None = None
        pages = 0
        while True:
            query = base + [("limit", str(page_size))]
            if cursor:
                query.append(("cursor", cursor))
            payload = self.get(path, query)
            yield from payload.get("data") or []
            pages += 1
            cursor = (payload.get("pagination") or {}).get("next_cursor")
            if not cursor or (max_pages and pages >= max_pages):
                return

    # ------------------------------------------------------------------ reads
    def trades(self, *, condition: str | Iterable[str] | None = None,
               user: str | None = None, full_history: bool = False,
               start: int | None = None, end: int | None = None,
               max_pages: int | None = None) -> list[dict]:
        """Taker-perspective trade feed.

        `user=` filters to trades where this wallet was the taker. A pure maker
        returns zero rows. For a complete wallet history use `activity()`.
        """
        params: list[tuple] = []
        if condition:
            conds = [condition] if isinstance(condition, str) else list(condition)
            params += [("condition", c) for c in conds]
        if user:
            params.append(("user", user))
        if full_history:
            params.append(("full_history", "true"))
        if start is not None:
            params.append(("start", str(start)))
        if end is not None:
            params.append(("end", str(end)))
        return list(self.paginate("/v2/trades", params, max_pages=max_pages))

    def activity(self, *, user: str | None = None,
                 condition: str | Iterable[str] | None = None,
                 types: str | Iterable[str] | None = None,
                 start: int | None = None, end: int | None = None,
                 sort_direction: str | None = None,
                 exclude_deposits_withdrawals: bool = True,
                 max_pages: int | None = None) -> list[dict]:
        """Wallet lifecycle feed: TRADE, SPLIT, MERGE, REDEEM, CONVERSION, ...

        This is the only free source of splits/merges/redeems after 2026-04-28,
        when the moose-code lifecycle tables stop. Omitted `start` floors to
        three years back. Re-send every filter on each page: the cursor carries
        only its seek anchor.
        """
        params: list[tuple] = []
        if user:
            params.append(("user", user))
        if condition:
            conds = [condition] if isinstance(condition, str) else list(condition)
            params += [("condition", c) for c in conds]
        if types:
            vals = [types] if isinstance(types, str) else list(types)
            params += [("type", t) for t in vals]
        if start is not None:
            params.append(("start", str(start)))
        if end is not None:
            params.append(("end", str(end)))
        if sort_direction:
            params.append(("sort_direction", sort_direction))
        if not exclude_deposits_withdrawals:
            params.append(("exclude_deposits_withdrawals", "false"))
        return list(self.paginate("/v2/activity", params, max_pages=max_pages))

    def positions(self, user: str, *, status: str | None = None,
                  sort_by: str | None = None, title: str | None = None,
                  full_history: bool = True, max_pages: int | None = None) -> list[dict]:
        params: list[tuple] = [("user", user)]
        if status:
            params.append(("status", status))
        if sort_by:
            params.append(("sort_by", sort_by))
        if title:
            params.append(("title", title))
        if full_history:
            params.append(("full_history", "true"))
        return list(self.paginate("/v2/positions", params, max_pages=max_pages))

    def holders(self, condition: str, *, include_pnl: bool = False,
                max_pages: int | None = None) -> list[dict]:
        params: list[tuple] = [("condition", condition)]
        if include_pnl:
            params.append(("include_pnl", "true"))
        return list(self.paginate("/v2/holders", params, max_pages=max_pages))

    def open_interest(self, condition: str | None = None) -> list[dict]:
        params = [("condition", condition)] if condition else []
        return self.get("/v2/oi", params).get("data") or []

    def live_volume(self, event_id: str | int) -> dict:
        return self.get("/v2/live-volume", {"event_id": str(event_id)})

    def value(self, user: str) -> dict:
        return self.get("/v2/value", {"user": user}).get("data") or {}

    def prices_history(self, token_id: str, *, interval: str | None = "max",
                       start: int | None = None, end: int | None = None,
                       as_of: int | None = None,
                       bucket_seconds: int = 43_200,
                       max_pages: int | None = None) -> list[dict]:
        params: list[tuple] = [("token_id", token_id),
                               ("bucket_seconds", str(bucket_seconds))]
        if as_of is not None:
            params.append(("as_of", str(as_of)))
        elif start is not None:
            params.append(("start", str(start)))
            if end is not None:
                params.append(("end", str(end)))
        elif interval:
            params.append(("interval", interval))
        return list(self.paginate("/v2/prices-history", params, max_pages=max_pages))

    def resolutions(self, condition: str | Iterable[str]) -> list[dict]:
        conds = condition if isinstance(condition, str) else ",".join(condition)
        return self.get("/v2/resolutions", {"condition": conds}).get("data") or []

    def tokens(self, condition: str | None = None,
               token_id: str | None = None) -> list[dict]:
        if bool(condition) == bool(token_id):
            raise ValueError("pass exactly one of condition or token_id")
        key, value = ("condition", condition) if condition else ("token_id", token_id)
        return self.get("/v2/tokens", {key: value}).get("data") or []

    def token_references(self, token_ids: Iterable[str]) -> list[dict]:
        """Batched `/v2/tokens`: up to 50 token ids per call, not paginated.

        Unknown ids are omitted from the response, so a short result is normal
        and must not be read as "unresolved". Returns rows carrying
        `token_id`, `condition_id`, `outcome_index`, `resolved` and
        `final_price` (the settled payout per share, or None).
        """
        ids = [str(t) for t in token_ids]
        if not 1 <= len(ids) <= 50:
            raise ValueError(f"token_id batch must be 1-50 ids, got {len(ids)}")
        return self.get("/v2/tokens", {"token_id": ",".join(ids)}).get("data") or []

    def leaderboard(self, *, limit: int = 100, max_pages: int | None = None,
                    **extra: Any) -> list[dict]:
        params = list(extra.items())
        return list(self.paginate("/v2/leaderboard", params,
                                  max_pages=max_pages, page_size=limit))

    def biggest_winners(self, *, limit: int = 100,
                        max_pages: int | None = None) -> list[dict]:
        return list(self.paginate("/v2/biggest-winners", [],
                                  max_pages=max_pages, page_size=limit))

    def user_pnl(self, user: str, **extra: Any) -> dict:
        return self.get("/v2/user-pnl", [("user", user), *extra.items()]).get("data") or {}

    def user_pnl_points(self, user: str, **extra: Any) -> list[dict]:
        return (self.user_pnl(user, **extra) or {}).get("points") or []

    def user_stats(self, user: str) -> dict:
        """Profile + all-time PnL decomposition.

        `all_time_pnl` separates realised market PnL from LP/combo PnL and from
        rebates (`maker_rebate`, `taker_rebate`, `fees_refunded`). That split is
        the cheapest available signal for classifying a wallet's archetype.
        """
        return self.get("/v2/user-stats", {"user": user}).get("data") or {}

    def status(self) -> dict:
        """Data freshness: serving and ingestion lag."""
        return self.get("/v2/status")
