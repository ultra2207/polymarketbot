"""Dedicated Fast Cancellation Transport for Polymarket CLOB.

Maintains a pool of pre-warmed persistent HTTPS connections strictly dedicated
to latency-critical order cancellations (`DELETE /order` and `DELETE /cancel-all`).

Optimizations:
1. TCP_NODELAY = 1: Disables Nagle's algorithm for sub-millisecond packet transmission.
2. SO_KEEPALIVE = 1: Keeps persistent connections alive at the transport level.
3. Pre-warmed LifoQueue: Reuses the warmest persistent TCP/TLS socket.
4. Minimal serialization: Direct ASCII byte concatenation bypassing full JSON encoder overhead.
5. Automatic reconnection and re-queuing upon keep-alive drop or connection timeout.
"""

from __future__ import annotations

import http.client
import queue
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from polymarketbot.logs import get_logger

logger = get_logger("fast_cancel")

DEFAULT_CLOB_HOST = "clob.polymarket.com"
DEFAULT_CLOB_PORT = 443


@dataclass(frozen=True)
class CancelResult:
    """Outcome of a fast cancellation request."""

    success: bool
    status: int
    body: str
    latency_ms: float
    endpoint: str
    order_id: Optional[str] = None
    error: Optional[str] = None


class DedicatedCancellationPool:
    """High-performance pre-warmed HTTPS connection pool for order cancellations."""

    def __init__(
        self,
        host: str = DEFAULT_CLOB_HOST,
        port: int = DEFAULT_CLOB_PORT,
        pool_size: int = 4,
        timeout: float = 3.0,
        max_retries: int = 2,
        default_headers: Optional[Dict[str, str]] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        auto_warm: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.pool_size = max(1, pool_size)
        self.timeout = timeout
        self.max_retries = max_retries
        self.default_headers = default_headers or {}
        self.ssl_context = ssl_context or ssl.create_default_context()

        self._pool: queue.LifoQueue[http.client.HTTPSConnection] = queue.LifoQueue(
            maxsize=self.pool_size * 2
        )
        self._lock = threading.Lock()
        self._is_closed = False
        self._active_connections: List[http.client.HTTPSConnection] = []

        if auto_warm:
            self.warm_up()

    def _create_connection(self) -> http.client.HTTPSConnection:
        """Create and connect a new persistent HTTPS connection with fast socket options."""
        conn = http.client.HTTPSConnection(
            host=self.host,
            port=self.port,
            timeout=self.timeout,
            context=self.ssl_context,
        )
        conn.connect()
        # Set low-latency and keep-alive socket options
        if conn.sock is not None:
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return conn

    def warm_up(self) -> int:
        """Pre-warm connections in the pool. Returns count of successfully warmed sockets."""
        warmed = 0
        with self._lock:
            while not self._pool.empty():
                try:
                    c = self._pool.get_nowait()
                    c.close()
                except Exception:
                    pass
            self._active_connections.clear()

            for _ in range(self.pool_size):
                try:
                    conn = self._create_connection()
                    self._pool.put_nowait(conn)
                    self._active_connections.append(conn)
                    warmed += 1
                except Exception as exc:
                    logger.warning(f"Failed to pre-warm connection to {self.host}: {exc}")
        return warmed

    def _acquire_connection(self) -> http.client.HTTPSConnection:
        """Acquire a pre-warmed connection from the LifoQueue or create a fresh one."""
        if self._is_closed:
            raise RuntimeError("DedicatedCancellationPool is closed.")

        try:
            return self._pool.get_nowait()
        except queue.Empty:
            # Pool is exhausted; create an on-demand connection
            return self._create_connection()

    def _release_connection(self, conn: http.client.HTTPSConnection) -> None:
        """Return a healthy connection back into the Lifo pool."""
        if self._is_closed:
            try:
                conn.close()
            except Exception:
                pass
            return

        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            try:
                conn.close()
            except Exception:
                pass

    def _close_connection(self, conn: http.client.HTTPSConnection) -> None:
        """Safely close a dropped or broken connection."""
        try:
            conn.close()
        except Exception:
            pass
        with self._lock:
            if conn in self._active_connections:
                self._active_connections.remove(conn)

    def send_delete(
        self,
        endpoint: str,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        order_id: Optional[str] = None,
    ) -> CancelResult:
        """Execute a low-latency DELETE request with automatic reconnection and retry.

        Args:
            endpoint: URL path (e.g. "/order" or "/cancel-all").
            body: Pre-encoded request payload bytes.
            headers: Optional headers (merged with default_headers).
            order_id: Optional order ID for tracking.

        Returns:
            CancelResult indicating status code, latency, and response.
        """
        req_headers = {
            "Host": self.host,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "keep-alive",
        }
        if self.default_headers:
            req_headers.update(self.default_headers)
        if headers:
            req_headers.update(headers)

        last_error: Optional[str] = None

        for attempt in range(self.max_retries + 1):
            conn: Optional[http.client.HTTPSConnection] = None
            try:
                conn = self._acquire_connection()
                t0 = time.perf_counter()
                conn.request("DELETE", endpoint, body=body, headers=req_headers)
                resp = conn.getresponse()
                raw_data = resp.read()
                latency_ms = (time.perf_counter() - t0) * 1000.0

                # Connection is healthy, return to pool
                self._release_connection(conn)

                success = 200 <= resp.status < 300
                return CancelResult(
                    success=success,
                    status=resp.status,
                    body=raw_data.decode("utf-8", errors="replace"),
                    latency_ms=latency_ms,
                    endpoint=endpoint,
                    order_id=order_id,
                )
            except (
                http.client.HTTPException,
                socket.error,
                OSError,
            ) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if conn is not None:
                    self._close_connection(conn)

                logger.debug(
                    f"Connection dropped on attempt {attempt + 1}/{self.max_retries + 1} "
                    f"for {endpoint}: {last_error}. Reconnecting & re-queuing."
                )

                if attempt >= self.max_retries:
                    break

        return CancelResult(
            success=False,
            status=0,
            body="",
            latency_ms=0.0,
            endpoint=endpoint,
            order_id=order_id,
            error=last_error or "Max retries exceeded without response",
        )

    def cancel_order(
        self,
        order_id: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> CancelResult:
        """Cancel a single order by ID via DELETE /order with minimal serialization.

        Constructs payload directly into ASCII bytes: b'{"orderID":"..."}'
        avoiding JSON encoder allocation overhead.
        """
        payload = b'{"orderID":"' + order_id.encode("ascii", errors="replace") + b'"}'
        return self.send_delete(
            endpoint="/order",
            body=payload,
            headers=headers,
            order_id=order_id,
        )

    def cancel_all(
        self,
        market: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> CancelResult:
        """Cancel all orders (optionally scoped to a specific market) via DELETE /cancel-all."""
        if market:
            payload = b'{"market":"' + market.encode("utf-8") + b'"}'
        else:
            payload = b"{}"

        return self.send_delete(
            endpoint="/cancel-all",
            body=payload,
            headers=headers,
            order_id=None,
        )

    def cancel_orders(
        self,
        order_ids: Sequence[str],
        headers: Optional[Dict[str, str]] = None,
    ) -> List[CancelResult]:
        """Cancel a batch of order IDs in sequential rapid-fire succession."""
        results: List[CancelResult] = []
        for oid in order_ids:
            results.append(self.cancel_order(order_id=oid, headers=headers))
        return results

    def close(self) -> None:
        """Close all pooled connections and mark pool closed."""
        self._is_closed = True
        with self._lock:
            while not self._pool.empty():
                try:
                    conn = self._pool.get_nowait()
                    conn.close()
                except Exception:
                    pass
            for conn in self._active_connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._active_connections.clear()

    def __enter__(self) -> DedicatedCancellationPool:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
