"""Shared scanner state: telemetry, TTL caches and the per-thread HTTP session.

Split out of the former single-file ``rewards/scanner`` module so the pricing,
venue-read and screening modules share one cache and one telemetry object
without importing each other. ``reset_scanner_telemetry`` rebinds
``GLOBAL_SCANNER_TELEMETRY``, so consumers read it as ``state.<name>`` rather
than copying it at import time.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import requests

from polymarketbot.logs import get_logger

logger = get_logger("rewards_scanner")

CLOB_REST_URL = "https://clob.polymarket.com"


@dataclass
class ScannerTelemetry:
    """Telemetry, hit rates, and error tracking for the opportunity scanner."""

    total_scans: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    http_429_errors: int = 0
    network_errors: int = 0
    total_book_queries: int = 0
    last_scan_timestamp: float = 0.0
    last_error_time: float = 0.0
    last_error_message: str = ""
    cached_candidates_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        hit_ratio = (self.cache_hits / (self.total_scans or 1)) * 100.0
        return {
            "total_scans": self.total_scans,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_ratio_pct": round(hit_ratio, 1),
            "http_429_errors": self.http_429_errors,
            "network_errors": self.network_errors,
            "total_book_queries": self.total_book_queries,
            "last_scan_timestamp": self.last_scan_timestamp,
            "last_error_time": self.last_error_time,
            "last_error_message": self.last_error_message,
            "cached_candidates_count": self.cached_candidates_count,
        }


GLOBAL_SCANNER_TELEMETRY = ScannerTelemetry()
_SCANNER_CACHE: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()
_BOOK_CACHE: OrderedDict[
    str, tuple[float, tuple[float | None, float | None, float | None]]
] = OrderedDict()
SCANNER_CACHE_TTL_SECONDS: float = 5.0
# Rotation through parameter sets is slow, so the opportunity cache only needs
# enough entries to cover the distinct scan shapes a process actually uses.
_SCANNER_CACHE_MAX_ENTRIES: int = 64
# One entry per audited token id, trimmed LRU so long uptimes cannot grow leak-like.
_BOOK_CACHE_MAX_ENTRIES: int = 2048
# One entry per token: the venue publishes the tick per token, and it never
# changes for a live market.
_TICK_CACHE: OrderedDict[str, tuple[float, float | None]] = OrderedDict()
# One entry per token: the live bid-ask spread in cents, on the same clock as
# the L2 book so one scan reads one snapshot per token.
_SPREAD_CACHE: OrderedDict[str, tuple[float, float | None]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_TELEMETRY_LOCK = threading.Lock()
_SCANNER_SESSION_LOCAL = threading.local()
_SCAN_EXECUTOR = ThreadPoolExecutor(max_workers=10, thread_name_prefix="rewards-scan")


def _cache_get_fresh(
    cache: OrderedDict[str, tuple[float, Any]],
    key: str,
    ttl_seconds: float,
) -> Any:
    """Return a cached value only while it is younger than ``ttl_seconds``."""
    with _CACHE_LOCK:
        entry = cache.get(key)
        if entry is None:
            return None
        timestamp, value = entry
        if (time.time() - timestamp) >= ttl_seconds:
            return None
        cache.move_to_end(key)
        return value


def _cache_get_stale(cache: OrderedDict[str, tuple[float, Any]], key: str) -> Any:
    """Return the last cached value regardless of age (error fallback)."""
    with _CACHE_LOCK:
        entry = cache.get(key)
        if entry is None:
            return None
        cache.move_to_end(key)
        return entry[1]


def _cache_put(
    cache: OrderedDict[str, tuple[float, Any]],
    key: str,
    value: Any,
    max_entries: int,
) -> None:
    """Store a value and evict the least-recently-used entries beyond the bound."""
    with _CACHE_LOCK:
        cache[key] = (time.time(), value)
        cache.move_to_end(key)
        while len(cache) > max_entries:
            cache.popitem(last=False)


def _get_scanner_session() -> requests.Session:
    """Return this worker thread's reusable HTTP session."""
    session = getattr(_SCANNER_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "polymarketbot/optimal-selector"})
        _SCANNER_SESSION_LOCAL.session = session
    return session


def get_scanner_telemetry() -> ScannerTelemetry:
    """Return the global scanner telemetry instance."""
    return GLOBAL_SCANNER_TELEMETRY


def reset_scanner_telemetry() -> None:
    """Reset scanner telemetry and caches (useful for test isolation)."""
    global GLOBAL_SCANNER_TELEMETRY
    with _TELEMETRY_LOCK:
        GLOBAL_SCANNER_TELEMETRY = ScannerTelemetry()
    with _CACHE_LOCK:
        _SCANNER_CACHE.clear()
        _BOOK_CACHE.clear()
        _TICK_CACHE.clear()
