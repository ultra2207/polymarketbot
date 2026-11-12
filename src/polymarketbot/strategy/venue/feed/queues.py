"""Bounded event queues for listener dispatch and the event stream."""

from __future__ import annotations

import asyncio
from typing import Any

# Upper bound on events waiting for listener delivery. A book listener that runs
# slower than the tape must not make the websocket receive loop grow memory.
DISPATCH_QUEUE_MAXSIZE = 1024

# Upper bound on the `get_next_event` stream. The live runner never drains it and
# a multi-day run would otherwise retain every book and trade in RAM.
EVENT_QUEUE_MAXSIZE = 2048


class _BoundedEventQueue(asyncio.Queue):
    """Bounded queue of ``("book" | "trade", payload)`` events.

    Producers never block and never grow memory without bound: when the queue
    is full the oldest queued book is evicted first, and every skipped event is
    counted in ``dropped``.
    """

    def __init__(self, maxsize: int) -> None:
        super().__init__(maxsize=max(1, int(maxsize)))
        self.dropped: int = 0

    def _evict_oldest_book(self) -> bool:
        for index, item in enumerate(self._queue):
            if item[0] == "book":
                del self._queue[index]
                self.dropped += 1
                return True
        return False

    def _evict_oldest(self) -> bool:
        if not self._queue:
            return False
        self._queue.popleft()
        self.dropped += 1
        return True


class _DispatchQueue(_BoundedEventQueue):
    """Bounded listener queue that keeps only the newest queued book per asset.

    Trades are never coalesced. When the queue is full the oldest queued book is
    evicted to make room; if the queue holds no books the incoming event is
    dropped (a slow listener must not be fed stale work). Everything skipped —
    coalesced, evicted, or refused — is counted in ``dropped``.
    """

    def __init__(self, maxsize: int = DISPATCH_QUEUE_MAXSIZE) -> None:
        super().__init__(maxsize)

    def _drop_queued_books(self, asset_id: str) -> None:
        kept = [
            item
            for item in self._queue
            if not (item[0] == "book" and item[1].asset_id == asset_id)
        ]
        if len(kept) != len(self._queue):
            self.dropped += len(self._queue) - len(kept)
            self._queue = type(self._queue)(kept)

    def offer(self, kind: str, payload: Any) -> bool:
        """Enqueue an event, coalescing books and enforcing the bound."""
        if kind == "book":
            self._drop_queued_books(payload.asset_id)
        if self.full() and not self._evict_oldest_book():
            self.dropped += 1
            return False
        self.put_nowait((kind, payload))
        return True


class _EventQueue(_BoundedEventQueue):
    """Bounded ``get_next_event`` stream.

    Book ticks are the overwhelming majority of feed traffic, so they are the
    first to go under pressure; trades are kept for polling consumers. Only when
    the queue holds nothing but trades is the oldest trade evicted, which keeps
    the stream recent instead of stalling the websocket loop.
    """

    def __init__(self, maxsize: int = EVENT_QUEUE_MAXSIZE) -> None:
        super().__init__(maxsize)

    def offer(self, kind: str, payload: Any) -> bool:
        if self.full() and not self._evict_oldest_book():
            self._evict_oldest()
        self.put_nowait((kind, payload))
        return True
