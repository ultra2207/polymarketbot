"""High-Speed In-Memory IPC Event Bus & Ring Buffer.

Connects Polymarket and Kalshi worker loops with sub-millisecond dispatch latency (<1ms).
Features:
- Lock-free / low-overhead circular ring buffer for event auditing and replay.
- High-performance non-blocking asyncio.Queue fanout to dedicated venue worker loops.
- Instantaneous synchronous handler execution for microsecond quote-pull signals.
- Precise nanosecond latency metrics tracking.
"""

from __future__ import annotations

import asyncio
import collections
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from polymarketbot.logs import get_logger

logger = get_logger("cross_venue_bus")

# Standard topics
TOPIC_LEAD_LAG_SIGNALS = "signals.lead_lag"
TOPIC_HEDGE_REQUESTS = "hedges.requests"
TOPIC_POLYMARKET_FILLS = "polymarket.fills"
TOPIC_KALSHI_FILLS = "kalshi.fills"
TOPIC_MARKET_DATA = "market_data"
TOPIC_ALL = "*"

DEFAULT_RING_BUFFER_CAPACITY = 65536


class CrossVenueBus:
    """High-speed in-memory IPC message bus with circular ring buffer."""

    def __init__(self, buffer_capacity: int = DEFAULT_RING_BUFFER_CAPACITY) -> None:
        self.buffer_capacity = buffer_capacity
        self._ring_buffer: Deque[Tuple[str, Any, int]] = collections.deque(maxlen=buffer_capacity)
        self._subscribers: Dict[str, Set[asyncio.Queue]] = collections.defaultdict(set)
        self._handlers: Dict[str, List[Callable[[str, Any], None]]] = collections.defaultdict(list)

        # Performance and latency metrics
        self._total_dispatches: int = 0
        self._total_latency_ns: int = 0
        self._max_latency_ns: int = 0
        self._min_latency_ns: int = 1_000_000_000
        self._last_latency_ns: int = 0

    def subscribe(self, topic: str = TOPIC_ALL, maxsize: int = 10000) -> asyncio.Queue:
        """Subscribe an asyncio.Queue to a specific topic or wildcard '*'."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers[topic].add(queue)
        return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        """Unsubscribe a queue from a topic."""
        if topic in self._subscribers and queue in self._subscribers[topic]:
            self._subscribers[topic].remove(queue)

    def register_handler(self, topic: str, handler: Callable[[str, Any], None]) -> None:
        """Register a zero-delay callback handler for latency-critical signals."""
        self._handlers[topic].append(handler)

    def dispatch(self, topic: str, event: Any) -> float:
        """Dispatch an event synchronously to ring buffer, callbacks, and subscriber queues.

        Returns dispatch latency in milliseconds (typically < 0.01 ms).
        """
        start_ns = time.perf_counter_ns()

        # 1. Store in circular ring buffer
        self._ring_buffer.append((topic, event, start_ns))

        # 2. Invoke instantaneous callback handlers
        handlers = self._handlers.get(topic, []) + self._handlers.get(TOPIC_ALL, [])
        for handler in handlers:
            try:
                handler(topic, event)
            except Exception as e:
                logger.error(f"Error in bus handler for topic {topic}: {e}")

        # 3. Non-blocking dispatch to asyncio queues
        queues = self._subscribers.get(topic, set()) | self._subscribers.get(TOPIC_ALL, set())
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()  # Evict oldest event to prevent memory bloat
                    q.put_nowait(event)
                except Exception:
                    pass

        # 4. Latency metrics update
        end_ns = time.perf_counter_ns()
        latency_ns = max(1, end_ns - start_ns)

        self._total_dispatches += 1
        self._total_latency_ns += latency_ns
        self._last_latency_ns = latency_ns
        if latency_ns > self._max_latency_ns:
            self._max_latency_ns = latency_ns
        if latency_ns < self._min_latency_ns:
            self._min_latency_ns = latency_ns

        latency_ms = latency_ns / 1_000_000.0
        return latency_ms

    async def async_dispatch(self, topic: str, event: Any) -> float:
        """Asynchronous dispatch wrapper."""
        return self.dispatch(topic, event)

    def get_recent(self, limit: int = 100, topic: Optional[str] = None) -> List[Tuple[str, Any, int]]:
        """Retrieve recent events from circular ring buffer."""
        items = list(self._ring_buffer)
        if topic:
            items = [item for item in items if item[0] == topic]
        return items[-limit:]

    def get_metrics(self) -> Dict[str, Any]:
        """Return diagnostic metrics including IPC dispatch latency."""
        avg_us = (
            (self._total_latency_ns / self._total_dispatches / 1_000.0)
            if self._total_dispatches > 0
            else 0.0
        )
        return {
            "total_dispatches": self._total_dispatches,
            "buffer_depth": len(self._ring_buffer),
            "buffer_capacity": self.buffer_capacity,
            "subscriber_topics": list(self._subscribers.keys()),
            "last_latency_us": round(self._last_latency_ns / 1_000.0, 3),
            "avg_latency_us": round(avg_us, 3),
            "max_latency_us": round(self._max_latency_ns / 1_000.0, 3),
            "min_latency_us": round(self._min_latency_ns / 1_000.0, 3) if self._total_dispatches > 0 else 0.0,
            "sub_millisecond_compliant": (self._max_latency_ns < 1_000_000),
        }
