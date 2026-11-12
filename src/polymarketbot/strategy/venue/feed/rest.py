"""Public CLOB REST reads (book, midpoint, spread, reward pools)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import requests

from polymarketbot.logs import get_logger

from .models import (
    OrderBookSnapshot,
    _parse_float,
    _parse_opt_float,
    build_book_snapshot,
)

logger = get_logger("trading.feed")


class _RestMixin:
    """Synchronous REST calls with their async ``to_thread`` wrappers."""

    # -------------------------------------------------------------------------
    # REST API Queries
    # -------------------------------------------------------------------------
    def _ensure_session(self) -> requests.Session:
        if self._sync_session is None:
            self._sync_session = requests.Session()
            self._sync_session.headers.update({
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            })
        return self._sync_session

    def get_book_sync(self, token_id: str, max_retries: int = 3) -> OrderBookSnapshot | None:
        """Fetch L2 order book snapshot for token_id synchronously via public REST."""
        session = self._ensure_session()
        url = f"{self.rest_url}/book"
        for attempt in range(max_retries):
            try:
                resp = session.get(url, params={"token_id": token_id}, timeout=self.request_timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    snapshot = build_book_snapshot(
                        asset_id=token_id,
                        raw_bids=data.get("bids", []),
                        raw_asks=data.get("asks", []),
                        tick_size=_parse_float(data.get("tick_size"), 0.01),
                        book_hash=data.get("hash"),
                        source="rest",
                    )
                    # REST snapshots are the reconnect base: deltas that follow
                    # must apply to this book, not to a pre-outage one.
                    self._reset_book(token_id, snapshot)
                    return snapshot
                elif resp.status_code == 429:
                    logger.warning(f"Rate limited (429) fetching book for {token_id[:12]}, backing off...")
                    time.sleep(1.0 * (attempt + 1))
                else:
                    logger.warning(f"Error fetching book for {token_id[:12]}: HTTP {resp.status_code}")
                    return None
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                if attempt == max_retries - 1:
                    logger.debug(f"REST get_book failed for {token_id[:12]}: {exc}")
                time.sleep(0.5 * (attempt + 1))
        return None

    async def fetch_book(self, token_id: str) -> OrderBookSnapshot | None:
        """Fetch L2 order book snapshot for token_id asynchronously via public REST."""
        return await asyncio.to_thread(self.get_book_sync, token_id)

    def get_midpoint_sync(self, token_id: str, max_retries: int = 3) -> float | None:
        """Fetch midpoint price for token_id synchronously via public REST."""
        session = self._ensure_session()
        url = f"{self.rest_url}/midpoint"
        for attempt in range(max_retries):
            try:
                resp = session.get(url, params={"token_id": token_id}, timeout=self.request_timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    val = data.get("mid") if isinstance(data, dict) else None
                    if val is None and isinstance(data, dict):
                        val = data.get("midpoint")
                    # A body without a usable mid is "unknown": 0.0 is a valid
                    # extreme price and would be indistinguishable from one.
                    return _parse_opt_float(val)
                elif resp.status_code == 429:
                    time.sleep(1.0 * (attempt + 1))
                else:
                    return None
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                if attempt == max_retries - 1:
                    logger.debug(f"REST get_midpoint failed: {exc}")
                time.sleep(0.5 * (attempt + 1))
        return None

    async def fetch_midpoint(self, token_id: str) -> float | None:
        """Fetch midpoint price for token_id asynchronously via public REST."""
        return await asyncio.to_thread(self.get_midpoint_sync, token_id)

    def get_spread_sync(self, token_id: str, max_retries: int = 3) -> float | None:
        """Fetch spread for token_id synchronously via public REST."""
        session = self._ensure_session()
        url = f"{self.rest_url}/spread"
        for attempt in range(max_retries):
            try:
                resp = session.get(url, params={"token_id": token_id}, timeout=self.request_timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    # Missing and 0.0 stay distinguishable: a zero spread would
                    # read as a locked market.
                    return _parse_opt_float(data.get("spread") if isinstance(data, dict) else None)
                elif resp.status_code == 429:
                    time.sleep(1.0 * (attempt + 1))
                else:
                    return None
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                if attempt == max_retries - 1:
                    logger.debug(f"REST get_spread failed: {exc}")
                time.sleep(0.5 * (attempt + 1))
        return None

    async def fetch_spread(self, token_id: str) -> float | None:
        """Fetch spread for token_id asynchronously via public REST."""
        return await asyncio.to_thread(self.get_spread_sync, token_id)

    def get_rewards_markets_sync(
        self,
        limit: int = 20,
        order_by: str = "rate_per_day",
        position: str = "DESC",
        max_retries: int = 3,
    ) -> list[dict[str, Any]]:
        """Fetch active reward market pools synchronously from CLOB REST endpoint."""
        session = self._ensure_session()
        url = f"{self.rest_url}/rewards/markets/multi"
        params = {
            "order_by": order_by,
            "position": position,
            "page_size": max(limit, 10),
        }
        for attempt in range(max_retries):
            try:
                resp = session.get(url, params=params, timeout=self.request_timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        return (data.get("data", []) or [])[:limit]
                    elif isinstance(data, list):
                        return data[:limit]
                elif resp.status_code == 429:
                    time.sleep(1.0 * (attempt + 1))
                else:
                    return []
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                if attempt == max_retries - 1:
                    logger.warning(f"REST get_rewards_markets failed: {exc}")
                time.sleep(0.5 * (attempt + 1))
        return []

    async def fetch_rewards_markets(
        self,
        limit: int = 20,
        order_by: str = "rate_per_day",
        position: str = "DESC",
    ) -> list[dict[str, Any]]:
        """Fetch active reward market pools asynchronously from CLOB REST endpoint."""
        return await asyncio.to_thread(self.get_rewards_markets_sync, limit, order_by, position)
