"""Live Polymarket CLOB Feed Client.

Connects directly to Polymarket's public CLOB REST and WebSocket with zero API key
requirement and zero 100-order limits.

Features:
- Asynchronous streaming of live L2 book states and public trades stream via WebSocket
  (wss://ws-subscriptions-clob.polymarket.com/ws/market).
- Direct REST queries for order book (/book), midpoint (/midpoint), spread (/spread),
  and multi-market rewards (/rewards/markets/multi) on https://clob.polymarket.com.
- Auto-reconnection with exponential backoff and jitter for network resilience.
- Graceful REST snapshot fallback whenever WebSocket is disconnected.
- Both async callback listeners and async event queue streaming.
- Synchronous helper methods for non-async scripts.

Package layout (every body was moved verbatim from the former ``feed.py``):

- ``models``     DTOs and raw-frame parsing.
- ``book``       L2 aggregation (snapshot seed + deltas).
- ``queues``     Bounded dispatch/event queues.
- ``decode``     WebSocket frame -> books/trades.
- ``dispatch``   Listener fan-out.
- ``connection`` WebSocket loop, heartbeat, reconnect and REST fallback.
- ``rest``       Public CLOB REST reads.
- ``client``     ``LivePolymarketFeed``: composition and lifecycle.
"""

# ruff: noqa: F401  -- this module is a facade over the submodules.

from __future__ import annotations

from . import (  # noqa: F401
    book,
    client,
    connection,
    decode,
    dispatch,
    models,
    queues,
    rest,
)
from .book import _TokenBook
from .client import DEFAULT_REST_URL, DEFAULT_WS_URL, LivePolymarketFeed
from .models import (
    BookCallback,
    MarketTrade,
    OrderBookLevel,
    OrderBookSnapshot,
    TradeCallback,
    build_book_snapshot,
    parse_levels,
)
from .queues import (
    DISPATCH_QUEUE_MAXSIZE,
    EVENT_QUEUE_MAXSIZE,
    _BoundedEventQueue,
    _DispatchQueue,
    _EventQueue,
)
