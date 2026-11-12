"""``LivePolymarketFeed``: composition, subscriptions and lifecycle."""

from __future__ import annotations

import asyncio
import threading
import time
from types import TracebackType
from typing import Any, Self

import requests
import websockets

from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback

from .book import _BookAggregationMixin, _TokenBook
from .connection import _ConnectionMixin
from .decode import _DecodeMixin
from .dispatch import _DispatchMixin
from .models import BookCallback, TradeCallback
from .queues import _DispatchQueue, _EventQueue
from .rest import _RestMixin

logger = get_logger("trading.feed")

DEFAULT_REST_URL = "https://clob.polymarket.com"
DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class LivePolymarketFeed(
    _ConnectionMixin,
    _RestMixin,
    _BookAggregationMixin,
    _DispatchMixin,
    _DecodeMixin,
):
    """Asynchronous Live Polymarket CLOB Feed with resilient WebSocket and REST fallback.

    Zero API key required; directly streams public orderbook and public trades.
    """

    def __init__(
        self,
        rest_url: str = DEFAULT_REST_URL,
        ws_url: str = DEFAULT_WS_URL,
        fallback_poll_interval: float = 2.0,
        initial_reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
        backoff_factor: float = 1.5,
        request_timeout: float = 10.0,
        user_agent: str = "polymarketbot/rewards-scanner",
    ) -> None:
        self.rest_url = rest_url.rstrip("/")
        self.ws_url = ws_url
        self.fallback_poll_interval = max(0.01, float(fallback_poll_interval))
        self.initial_reconnect_delay = max(0.1, float(initial_reconnect_delay))
        self.max_reconnect_delay = max(self.initial_reconnect_delay, float(max_reconnect_delay))
        self.backoff_factor = max(1.1, float(backoff_factor))
        self.request_timeout = float(request_timeout)
        self.user_agent = user_agent

        # Subscriptions
        self._subscribed_tokens: set[str] = set()

        # Listeners
        self._book_listeners: list[BookCallback] = []
        self._trade_listeners: list[TradeCallback] = []
        self._event_queue: _EventQueue = _EventQueue()
        self._dispatch_queue: _DispatchQueue = _DispatchQueue()
        self._dispatch_task: asyncio.Task | None = None

        # Maintained L2 books, rebuilt from `book` snapshots and `price_change`
        # deltas. Guarded by a lock because REST snapshots are refreshed from a
        # worker thread (asyncio.to_thread) while the event loop applies deltas.
        self._books: dict[str, _TokenBook] = {}
        self._book_lock = threading.Lock()

        # State tracking
        self._running: bool = False
        self._ws_connected: bool = False
        self._last_ws_message_time: float = 0.0
        # Last *data* frame (a parsed book/trade message). Heartbeat PONGs keep
        # the socket alive but carry no depth, so liveness for the REST fallback
        # is measured from here, not from ``_last_ws_message_time``.
        self._last_ws_data_time: float = 0.0
        # Assets whose book was left incomplete by a delta we could not apply.
        # They are re-seeded from a REST snapshot by the fallback poller.
        self._desync_assets: set[str] = set()
        self._active_ws: websockets.WebSocketClientProtocol | None = None

        # Background tasks
        self._ws_task: asyncio.Task | None = None
        self._fallback_task: asyncio.Task | None = None

        # Network session
        self._sync_session: requests.Session | None = None

        # Ensure DoH fallback is installed to prevent DNS drops
        install_doh_fallback()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_ws_connected(self) -> bool:
        return self._ws_connected

    @property
    def subscribed_tokens(self) -> set[str]:
        return set(self._subscribed_tokens)

    @property
    def last_message_age_seconds(self) -> float | None:
        """Seconds since the last WebSocket frame (pings included).

        ``None`` means no frame has ever been received on this connection.
        """
        if self._last_ws_message_time <= 0.0:
            return None
        return max(0.0, time.time() - self._last_ws_message_time)

    def is_stale(self, max_age: float = 30.0) -> bool:
        """True when no frame arrived within ``max_age`` seconds (or none ever did)."""
        age = self.last_message_age_seconds
        return age is None or age > max_age

    @property
    def dispatch_dropped_count(self) -> int:
        """Book updates coalesced/evicted plus events refused by the queue bound."""
        return self._dispatch_queue.dropped

    @property
    def event_dropped_count(self) -> int:
        """Events evicted from the bounded ``get_next_event`` stream."""
        return self._event_queue.dropped

    # -------------------------------------------------------------------------
    # Subscription Management
    # -------------------------------------------------------------------------
    def subscribe(self, tokens: str | list[str] | set[str]) -> None:
        """Subscribe to order book and trade updates for one or more token IDs."""
        if isinstance(tokens, str):
            token_list = [tokens]
        else:
            token_list = list(tokens)

        new_tokens = [t for t in token_list if t and t not in self._subscribed_tokens]
        if not new_tokens:
            return

        self._subscribed_tokens.update(new_tokens)
        logger.info(f"Subscribed to {len(new_tokens)} token(s). Total: {len(self._subscribed_tokens)}")

        # If WebSocket is actively connected, push dynamic subscription
        if self._ws_connected and self._active_ws is not None:
            asyncio.create_task(self._send_subscription(new_tokens))

    def unsubscribe(self, tokens: str | list[str] | set[str]) -> None:
        """Unsubscribe from token IDs."""
        if isinstance(tokens, str):
            token_list = [tokens]
        else:
            token_list = list(tokens)

        for t in token_list:
            self._subscribed_tokens.discard(t)
        logger.info(f"Unsubscribed from {len(token_list)} token(s). Remaining: {len(self._subscribed_tokens)}")

    def add_book_listener(self, callback: BookCallback) -> None:
        """Register a callback for order book updates."""
        if callback not in self._book_listeners:
            self._book_listeners.append(callback)

    def remove_book_listener(self, callback: BookCallback) -> None:
        """Unregister a callback for order book updates."""
        if callback in self._book_listeners:
            self._book_listeners.remove(callback)

    def add_trade_listener(self, callback: TradeCallback) -> None:
        """Register a callback for market trade executions."""
        if callback not in self._trade_listeners:
            self._trade_listeners.append(callback)

    def remove_trade_listener(self, callback: TradeCallback) -> None:
        """Unregister a callback for market trade executions."""
        if callback in self._trade_listeners:
            self._trade_listeners.remove(callback)

    async def get_next_event(self, timeout: float | None = None) -> tuple[str, Any] | None:
        """Await the next event from the internal queue ('book', snapshot) or ('trade', trade)."""
        try:
            if timeout is not None:
                return await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
            return await self._event_queue.get()
        except TimeoutError:
            return None


    # -------------------------------------------------------------------------
    # Lifecycle Management
    # -------------------------------------------------------------------------
    async def start(self) -> None:
        """Start the live feed streaming and background recovery tasks."""
        if self._running:
            return
        self._running = True
        self._ensure_dispatch_worker()
        self._ws_task = asyncio.create_task(self._ws_loop(), name="CLOB-WS-Feed")
        self._fallback_task = asyncio.create_task(self._fallback_poll_loop(), name="CLOB-REST-Fallback")
        logger.info("LivePolymarketFeed started.")

    async def stop(self) -> None:
        """Gracefully stop all streaming and background connections."""
        self._running = False
        self._ws_connected = False

        if self._active_ws is not None:
            try:
                await self._active_ws.close()
            except Exception:  # noqa: BLE001, S110 - teardown must not raise
                pass
            self._active_ws = None

        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 - task teardown
                pass
            self._ws_task = None

        if self._fallback_task is not None:
            self._fallback_task.cancel()
            try:
                await self._fallback_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 - task teardown
                pass
            self._fallback_task = None

        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 - task teardown
                pass
            self._dispatch_task = None

        # A stopped feed owes no further listener events.
        while not self._dispatch_queue.empty():
            try:
                self._dispatch_queue.get_nowait()
                self._dispatch_queue.task_done()
            except asyncio.QueueEmpty:
                break

        if self._sync_session is not None:
            self._sync_session.close()
            self._sync_session = None

        logger.info("LivePolymarketFeed stopped.")

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.stop()
