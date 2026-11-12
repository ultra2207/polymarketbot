"""WebSocket lifecycle: subscriptions, reconnect backoff, REST fallback."""

from __future__ import annotations

import asyncio
import json
import random
import time

import websockets
from websockets.exceptions import WebSocketException

from polymarketbot.logs import get_logger

logger = get_logger("trading.feed")


class _ConnectionMixin:
    """The socket worker, its heartbeat and the REST fallback poller."""

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
        except (OSError, WebSocketException) as exc:
            logger.warning(f"Failed to send WS subscription payload: {exc}")

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
                    # No data has arrived on this connection yet: keep the REST
                    # fallback engaged until the first parsed frame re-seeds it.
                    self._last_ws_data_time = 0.0
                    reconnect_delay = self.initial_reconnect_delay
                    # Deltas received after a gap cannot be applied to depth that
                    # accumulated before it. Drop the books and let the exchange's
                    # first `book` frame re-seed them.
                    self._clear_books()
                    logger.info("Polymarket CLOB WebSocket connected successfully.")

                    # Subscribe to all current tokens
                    if self._subscribed_tokens:
                        await self._send_subscription(list(self._subscribed_tokens))

                    # Start application-level heartbeat task (Polymarket requires text frame "PING" every 10s)
                    async def _heartbeat_worker() -> None:
                        while self._running and self._ws_connected and self._active_ws is ws:
                            await asyncio.sleep(8.0)
                            try:
                                await ws.send("PING")
                            except (OSError, WebSocketException):
                                break

                    hb_task = asyncio.create_task(_heartbeat_worker(), name="CLOB-WS-Heartbeat")

                    try:
                        while self._running:
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=20.0)
                                await self._process_ws_payload(msg)
                            except TimeoutError:
                                # Re-verify connection with explicit ping
                                try:
                                    await ws.send("PING")
                                except (OSError, WebSocketException):
                                    break
                    finally:
                        hb_task.cancel()

            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - reconnect loop: any failure must not stop the feed
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

                # A socket that only answers PONGs is not delivering books, so
                # liveness is measured from the last parsed data frame.
                ws_healthy = self._ws_connected and (
                    time.time() - self._last_ws_data_time < 30.0
                )
                desynced = list(getattr(self, "_desync_assets", ()))
                if ws_healthy and not desynced:
                    continue

                # Re-seed the books a dropped delta left incomplete, and poll
                # everything when the socket is not delivering data at all.
                tokens_to_poll = desynced if ws_healthy else list(self._subscribed_tokens)
                if not tokens_to_poll:
                    continue
                for tok in tokens_to_poll:
                    if not self._running:
                        break
                    snapshot = await self.fetch_book(tok)
                    if snapshot:
                        await self._dispatch_book(snapshot)
                        if ws_healthy:
                            self._desync_assets.discard(tok)
                    await asyncio.sleep(0.1)  # Courteous pacing

            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - fallback poller: any failure must not stop the feed
                logger.error(f"Error in REST fallback polling loop: {exc}")
                await asyncio.sleep(self.fallback_poll_interval)
