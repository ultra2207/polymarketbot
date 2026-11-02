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
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple, Union

import requests
import websockets
from websockets.exceptions import ConnectionClosed

from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback

logger = get_logger("simulator.feed")

DEFAULT_REST_URL = "https://clob.polymarket.com"
DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _parse_float(val: Any, default: float = 0.0) -> float:
    """Safely parse float from arbitrary JSON input."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class OrderBookLevel:
    """A single price level in an L2 order book."""

    price: float
    size: float

    def to_dict(self) -> dict[str, float]:
        return {"price": self.price, "size": self.size}


@dataclass
class OrderBookSnapshot:
    """L2 Order book snapshot with calculated market metrics."""

    asset_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    best_bid: float = 0.0
    best_ask: float = 1.0
    mid: float = 0.50
    spread: float = 0.0
    spread_cents: float = 0.0
    tick_size: float = 0.01
    timestamp: float = field(default_factory=time.time)
    hash: Optional[str] = None
    source: str = "ws"  # "ws" or "rest"

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "bids": [b.to_dict() for b in self.bids],
            "asks": [a.to_dict() for a in self.asks],
            "best_bid": round(self.best_bid, 4),
            "best_ask": round(self.best_ask, 4),
            "mid": round(self.mid, 4),
            "spread": round(self.spread, 4),
            "spread_cents": round(self.spread_cents, 2),
            "tick_size": self.tick_size,
            "timestamp": self.timestamp,
            "hash": self.hash,
            "source": self.source,
        }


@dataclass(frozen=True)
class MarketTrade:
    """Public trade execution event from Polymarket tape."""

    asset_id: str
    price: float
    size: float
    side: str  # "BUY" or "SELL" (taker aggressor side)
    timestamp: float = field(default_factory=time.time)
    trade_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source: str = "ws"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "asset_id": self.asset_id,
            "price": round(self.price, 4),
            "size": round(self.size, 4),
            "side": self.side.upper(),
            "timestamp": self.timestamp,
            "source": self.source,
        }


def parse_levels(raw_levels: Any, reverse_sort: bool = False) -> list[OrderBookLevel]:
    """Parse bids or asks into sorted OrderBookLevel list.

    Bids should be reverse_sort=True (highest price first).
    Asks should be reverse_sort=False (lowest price first).
    """
    levels: list[OrderBookLevel] = []
    if not isinstance(raw_levels, list):
        return levels

    for item in raw_levels:
        p: float = 0.0
        s: float = 0.0
        if isinstance(item, dict):
            p = _parse_float(item.get("price"))
            s = _parse_float(item.get("size"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            p = _parse_float(item[0])
            s = _parse_float(item[1])
        if 0.0 < p < 1.0 and s > 0.0:
            levels.append(OrderBookLevel(price=round(p, 6), size=round(s, 6)))

    levels.sort(key=lambda lvl: lvl.price, reverse=reverse_sort)
    return levels


def build_book_snapshot(
    asset_id: str,
    raw_bids: Any,
    raw_asks: Any,
    tick_size: float = 0.01,
    book_hash: Optional[str] = None,
    timestamp: Optional[float] = None,
    source: str = "ws",
) -> OrderBookSnapshot:
    """Construct a validated, sorted OrderBookSnapshot from raw bid/ask data."""
    bids = parse_levels(raw_bids, reverse_sort=True)
    asks = parse_levels(raw_asks, reverse_sort=False)

    best_bid = bids[0].price if bids else 0.0
    best_ask = asks[0].price if asks else 1.0

    if best_bid > 0.0 and best_ask < 1.0 and best_ask >= best_bid:
        mid = round((best_bid + best_ask) / 2.0, 4)
        spread = round(best_ask - best_bid, 4)
    elif best_bid > 0.0:
        mid = round(best_bid, 4)
        spread = 0.0
    elif best_ask < 1.0:
        mid = round(best_ask, 4)
        spread = 0.0
    else:
        mid = 0.50
        spread = 0.0

    spread_cents = round(spread * 100.0, 2)
    ts = timestamp if timestamp is not None else time.time()

    return OrderBookSnapshot(
        asset_id=asset_id,
        bids=bids,
        asks=asks,
        best_bid=round(best_bid, 4),
        best_ask=round(best_ask, 4),
        mid=mid,
        spread=spread,
        spread_cents=spread_cents,
        tick_size=tick_size,
        timestamp=ts,
        hash=book_hash,
        source=source,
    )


BookCallback = Callable[[OrderBookSnapshot], Union[None, Awaitable[None]]]
TradeCallback = Callable[[MarketTrade], Union[None, Awaitable[None]]]


class LivePolymarketFeed:
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
        self._event_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        # State tracking
        self._running: bool = False
        self._ws_connected: bool = False
        self._last_ws_message_time: float = 0.0
        self._active_ws: Optional[websockets.WebSocketClientProtocol] = None

        # Background tasks
        self._ws_task: Optional[asyncio.Task] = None
        self._fallback_task: Optional[asyncio.Task] = None

        # Network session
        self._sync_session: Optional[requests.Session] = None

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

    # -------------------------------------------------------------------------
    # Subscription Management
    # -------------------------------------------------------------------------
    def subscribe(self, tokens: Union[str, list[str], set[str]]) -> None:
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

    def unsubscribe(self, tokens: Union[str, list[str], set[str]]) -> None:
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

    async def get_next_event(self, timeout: Optional[float] = None) -> Optional[tuple[str, Any]]:
        """Await the next event from the internal queue ('book', snapshot) or ('trade', trade)."""
        try:
            if timeout is not None:
                return await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
            return await self._event_queue.get()
        except asyncio.TimeoutError:
            return None

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

    def get_book_sync(self, token_id: str, max_retries: int = 3) -> Optional[OrderBookSnapshot]:
        """Fetch L2 order book snapshot for token_id synchronously via public REST."""
        session = self._ensure_session()
        url = f"{self.rest_url}/book"
        for attempt in range(max_retries):
            try:
                resp = session.get(url, params={"token_id": token_id}, timeout=self.request_timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    return build_book_snapshot(
                        asset_id=token_id,
                        raw_bids=data.get("bids", []),
                        raw_asks=data.get("asks", []),
                        tick_size=_parse_float(data.get("tick_size"), 0.01),
                        book_hash=data.get("hash"),
                        source="rest",
                    )
                elif resp.status_code == 429:
                    logger.warning(f"Rate limited (429) fetching book for {token_id[:12]}, backing off...")
                    time.sleep(1.0 * (attempt + 1))
                else:
                    logger.warning(f"Error fetching book for {token_id[:12]}: HTTP {resp.status_code}")
                    return None
            except Exception as exc:
                if attempt == max_retries - 1:
                    logger.debug(f"REST get_book failed for {token_id[:12]}: {exc}")
                time.sleep(0.5 * (attempt + 1))
        return None

    async def fetch_book(self, token_id: str) -> Optional[OrderBookSnapshot]:
        """Fetch L2 order book snapshot for token_id asynchronously via public REST."""
        return await asyncio.to_thread(self.get_book_sync, token_id)

    def get_midpoint_sync(self, token_id: str, max_retries: int = 3) -> Optional[float]:
        """Fetch midpoint price for token_id synchronously via public REST."""
        session = self._ensure_session()
        url = f"{self.rest_url}/midpoint"
        for attempt in range(max_retries):
            try:
                resp = session.get(url, params={"token_id": token_id}, timeout=self.request_timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    val = data.get("mid") or data.get("midpoint")
                    return _parse_float(val, default=0.0)
                elif resp.status_code == 429:
                    time.sleep(1.0 * (attempt + 1))
                else:
                    return None
            except Exception as exc:
                if attempt == max_retries - 1:
                    logger.debug(f"REST get_midpoint failed: {exc}")
                time.sleep(0.5 * (attempt + 1))
        return None

    async def fetch_midpoint(self, token_id: str) -> Optional[float]:
        """Fetch midpoint price for token_id asynchronously via public REST."""
        return await asyncio.to_thread(self.get_midpoint_sync, token_id)

    def get_spread_sync(self, token_id: str, max_retries: int = 3) -> Optional[float]:
        """Fetch spread for token_id synchronously via public REST."""
        session = self._ensure_session()
        url = f"{self.rest_url}/spread"
        for attempt in range(max_retries):
            try:
                resp = session.get(url, params={"token_id": token_id}, timeout=self.request_timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    val = data.get("spread")
                    return _parse_float(val, default=0.0)
                elif resp.status_code == 429:
                    time.sleep(1.0 * (attempt + 1))
                else:
                    return None
            except Exception as exc:
                if attempt == max_retries - 1:
                    logger.debug(f"REST get_spread failed: {exc}")
                time.sleep(0.5 * (attempt + 1))
        return None

    async def fetch_spread(self, token_id: str) -> Optional[float]:
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
            except Exception as exc:
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


    # -------------------------------------------------------------------------
    # Event Dispatching
    # -------------------------------------------------------------------------
    async def _dispatch_book(self, snapshot: OrderBookSnapshot) -> None:
        """Send book snapshot to all listeners and event queue."""
        self._event_queue.put_nowait(("book", snapshot))
        for cb in self._book_listeners:
            try:
                res = cb(snapshot)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.error(f"Error in book listener callback: {e}")

    async def _dispatch_trade(self, trade: MarketTrade) -> None:
        """Send trade execution to all listeners and event queue."""
        self._event_queue.put_nowait(("trade", trade))
        for cb in self._trade_listeners:
            try:
                res = cb(trade)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.error(f"Error in trade listener callback: {e}")

    # -------------------------------------------------------------------------
    # WebSocket Message Handling
    # -------------------------------------------------------------------------
    async def _send_subscription(self, token_ids: list[str]) -> None:
        """Send subscription payload over active WebSocket."""
        if not self._active_ws or not token_ids:
            return
        payload = {
            "assets_ids": token_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }
        try:
            await self._active_ws.send(json.dumps(payload))
            logger.info(f"Sent WebSocket subscription for {len(token_ids)} token(s).")
        except Exception as exc:
            logger.warning(f"Failed to send WS subscription payload: {exc}")

    async def _process_ws_payload(self, raw_data: str) -> None:
        """Parse raw WebSocket text and dispatch domain events."""
        self._last_ws_message_time = time.time()
        # Handle ping/pong text frames
        if raw_data.strip().upper() in ("PONG", "PING"):
            return

        try:
            data = json.loads(raw_data)
        except Exception:
            return

        messages = data if isinstance(data, list) else [data]

        for item in messages:
            if not isinstance(item, dict):
                continue

            ev_type = item.get("event_type")
            asset_id = item.get("asset_id")

            # 1. Full order book snapshot
            if ev_type == "book" and asset_id:
                bids = item.get("bids", [])
                asks = item.get("asks", [])
                tick = _parse_float(item.get("tick_size"), 0.01)
                book_hash = item.get("hash")
                ts_raw = item.get("timestamp")
                ts = (_parse_float(ts_raw) / 1000.0) if ts_raw else time.time()

                snapshot = build_book_snapshot(
                    asset_id=asset_id,
                    raw_bids=bids,
                    raw_asks=asks,
                    tick_size=tick,
                    book_hash=book_hash,
                    timestamp=ts,
                    source="ws",
                )
                await self._dispatch_book(snapshot)

            # 2. Best bid/ask top of book update
            elif ev_type == "best_bid_ask" and asset_id:
                bb = _parse_float(item.get("best_bid"), 0.0)
                ba = _parse_float(item.get("best_ask"), 1.0)
                raw_bids = [{"price": bb, "size": 100.0}] if bb > 0 else []
                raw_asks = [{"price": ba, "size": 100.0}] if ba < 1.0 else []
                snapshot = build_book_snapshot(
                    asset_id=asset_id,
                    raw_bids=raw_bids,
                    raw_asks=raw_asks,
                    source="ws",
                )
                await self._dispatch_book(snapshot)

            # 3. Price change / batch delta
            elif ev_type == "price_change":
                changes = item.get("price_changes", [])
                for ch in changes:
                    c_aid = ch.get("asset_id")
                    if c_aid:
                        bb = _parse_float(ch.get("best_bid"), 0.0)
                        ba = _parse_float(ch.get("best_ask"), 1.0)
                        raw_bids = [{"price": bb, "size": 100.0}] if bb > 0 else []
                        raw_asks = [{"price": ba, "size": 100.0}] if ba < 1.0 else []
                        snapshot = build_book_snapshot(
                            asset_id=c_aid,
                            raw_bids=raw_bids,
                            raw_asks=raw_asks,
                            source="ws",
                        )
                        await self._dispatch_book(snapshot)

            # 4. Public trade stream
            elif ev_type in ("last_trade_price", "trade") and asset_id:
                price = _parse_float(item.get("price"))
                size = _parse_float(item.get("size"))
                side = str(item.get("side", "BUY")).upper()
                ts_raw = item.get("timestamp")
                ts = (_parse_float(ts_raw) / 1000.0) if ts_raw else time.time()
                trade_id = str(item.get("transaction_hash") or item.get("id") or uuid.uuid4().hex[:12])

                if price > 0.0 and size > 0.0:
                    trade = MarketTrade(
                        asset_id=asset_id,
                        price=price,
                        size=size,
                        side=side,
                        timestamp=ts,
                        trade_id=trade_id,
                        source="ws",
                    )
                    await self._dispatch_trade(trade)

    # -------------------------------------------------------------------------
    # Resilient WebSocket Worker with Backoff
    # -------------------------------------------------------------------------
    async def _ws_loop(self) -> None:
        """Long-running WebSocket loop featuring automatic reconnection and exponential backoff."""
        reconnect_delay = self.initial_reconnect_delay

        while self._running:
            try:
                logger.info(f"Connecting to Polymarket CLOB WebSocket: {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    self._active_ws = ws
                    self._ws_connected = True
                    self._last_ws_message_time = time.time()
                    reconnect_delay = self.initial_reconnect_delay
                    logger.info("Polymarket CLOB WebSocket connected successfully.")

                    # Subscribe to all current tokens
                    if self._subscribed_tokens:
                        await self._send_subscription(list(self._subscribed_tokens))

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=25.0)
                            await self._process_ws_payload(msg)
                        except asyncio.TimeoutError:
                            # Send explicit ping on quiet connection
                            try:
                                await ws.ping()
                            except Exception:
                                break

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._ws_connected = False
                self._active_ws = None
                jitter = random.uniform(0.1, 0.5)
                wait_time = min(reconnect_delay + jitter, self.max_reconnect_delay)
                logger.warning(
                    f"CLOB WebSocket connection dropped ({exc}). Reconnecting in {wait_time:.1f}s (REST fallback active)..."
                )
                reconnect_delay = min(reconnect_delay * self.backoff_factor, self.max_reconnect_delay)
                try:
                    await asyncio.sleep(wait_time)
                except asyncio.CancelledError:
                    break
            finally:
                self._ws_connected = False
                self._active_ws = None

    # -------------------------------------------------------------------------
    # REST Fallback Polling Worker
    # -------------------------------------------------------------------------
    async def _fallback_poll_loop(self) -> None:
        """Periodic background task that polls REST snapshots when WebSocket drops."""
        while self._running:
            try:
                await asyncio.sleep(self.fallback_poll_interval)
                if not self._running:
                    break

                # If WebSocket is connected and healthy, fallback remains idle
                ws_healthy = self._ws_connected and (time.time() - self._last_ws_message_time < 30.0)
                if ws_healthy or not self._subscribed_tokens:
                    continue

                # WebSocket is down: fetch snapshots via REST
                tokens_to_poll = list(self._subscribed_tokens)
                for tok in tokens_to_poll:
                    if not self._running:
                        break
                    snapshot = await self.fetch_book(tok)
                    if snapshot:
                        await self._dispatch_book(snapshot)
                    await asyncio.sleep(0.1)  # Courteous pacing

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Error in REST fallback polling loop: {exc}")
                await asyncio.sleep(self.fallback_poll_interval)

    # -------------------------------------------------------------------------
    # Lifecycle Management
    # -------------------------------------------------------------------------
    async def start(self) -> None:
        """Start the live feed streaming and background recovery tasks."""
        if self._running:
            return
        self._running = True
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
            except Exception:
                pass
            self._active_ws = None

        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None

        if self._fallback_task is not None:
            self._fallback_task.cancel()
            try:
                await self._fallback_task
            except (asyncio.CancelledError, Exception):
                pass
            self._fallback_task = None

        if self._sync_session is not None:
            self._sync_session.close()
            self._sync_session = None

        logger.info("LivePolymarketFeed stopped.")

    async def __aenter__(self) -> "LivePolymarketFeed":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()
