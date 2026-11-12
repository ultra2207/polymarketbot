"""Listener dispatch: bounded queue, off-loop delivery, coalescing."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from polymarketbot.logs import get_logger

from .models import MarketTrade, OrderBookSnapshot

logger = get_logger("trading.feed")


class _DispatchMixin:
    """Fan-out of book/trade events to registered listeners."""

    # -------------------------------------------------------------------------
    # Event Dispatching
    # -------------------------------------------------------------------------
    def _dispatch_started(self) -> bool:
        return self._dispatch_task is not None

    def _dispatch_running(self) -> bool:
        return self._dispatch_task is not None and not self._dispatch_task.done()

    def _ensure_dispatch_worker(self) -> None:
        """Start the single listener-dispatch worker if it is not already running."""
        if not self._dispatch_running():
            self._dispatch_task = asyncio.create_task(self._dispatch_worker(), name="CLOB-Dispatch")

    async def _dispatch_worker(self) -> None:
        """Deliver queued events to listeners one at a time, in order, off-loop."""
        while True:
            kind, payload = await self._dispatch_queue.get()
            try:
                if kind == "book":
                    await self._deliver_book(payload)
                else:
                    await self._deliver_trade(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - dispatch worker must survive listener failures
                logger.error(f"Error dispatching {kind} event: {exc}")
            finally:
                self._dispatch_queue.task_done()

    @staticmethod
    async def _call_listener(callback: Callable[[Any], Any], event: Any, kind: str) -> None:
        """Run one listener callback in a worker thread; await async callbacks after."""
        try:
            result = await asyncio.to_thread(callback, event)
            if inspect.isawaitable(result):
                await result
        except Exception as e:  # noqa: BLE001 - listener callbacks are arbitrary user code
            logger.error(f"Error in {kind} listener callback: {e}")

    async def _deliver_book(self, snapshot: OrderBookSnapshot) -> None:
        for cb in list(self._book_listeners):
            await self._call_listener(cb, snapshot, "book")

    async def _deliver_trade(self, trade: MarketTrade) -> None:
        for cb in list(self._trade_listeners):
            await self._call_listener(cb, trade, "trade")

    async def _dispatch_book(self, snapshot: OrderBookSnapshot) -> None:
        """Publish a book snapshot to the event queue and queue listener delivery."""
        self._event_queue.offer("book", snapshot)
        if not self._dispatch_started():
            # Feed not started (direct payload processing in tests/scripts):
            # deliver in order, still off the event loop.
            await self._deliver_book(snapshot)
            return
        if not self._dispatch_running():
            if not self._running:
                await self._deliver_book(snapshot)
                return
            logger.error("Dispatch worker stopped unexpectedly; restarting it.")
            self._ensure_dispatch_worker()
        if not self._dispatch_queue.offer("book", snapshot):
            logger.debug(f"Dispatch queue full; dropped book for {snapshot.asset_id[:12]}")

    async def _dispatch_trade(self, trade: MarketTrade) -> None:
        """Publish a trade to the event queue and queue listener delivery."""
        self._event_queue.offer("trade", trade)
        if not self._dispatch_started():
            await self._deliver_trade(trade)
            return
        if not self._dispatch_running():
            if not self._running:
                await self._deliver_trade(trade)
                return
            logger.error("Dispatch worker stopped unexpectedly; restarting it.")
            self._ensure_dispatch_worker()
        if not self._dispatch_queue.offer("trade", trade):
            logger.debug(f"Dispatch queue full; dropped trade {trade.trade_id}")
