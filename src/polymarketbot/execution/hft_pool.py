"""High-Performance Dedicated Pre-Warmed HFT Connection Pool for Polymarket CLOB.

Provides sub-millisecond network execution for:
- Order Placement: POST /order (single) and POST /orders (batch)
- Order Cancellation: DELETE /order (single) and DELETE /cancel-all (mass)

Key HFT Optimizations:
1. TCP_NODELAY = 1: Disables Nagle's packet-coalescing algorithm to send frames immediately.
2. SO_KEEPALIVE = 1: Maintains active transport-level TCP keep-alive probes.
3. Pre-Warmed LifoQueue: Reuses the warmest persistent TCP/TLS socket to avoid TLS handshakes.
4. Fast orjson serialization: Direct C-level JSON serialization bypassing Python dict encoding overhead.
5. Pre-decoded Base64 Secret: HMAC-SHA256 signing done directly on raw byte arrays.
6. Automatic Reconnection & LIFO Recycling: Recovers dropped keep-alive sockets transparently.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import queue
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

import orjson

from polymarketbot.execution.fast_cancel import CancelResult
from polymarketbot.logs import get_logger

logger = get_logger("hft_pool")

DEFAULT_CLOB_HOST = "clob.polymarket.com"
DEFAULT_CLOB_PORT = 443


@dataclass(frozen=True)
class HftOrderResponse:
    """Outcome of submitting an order or batch through the HFT connection pool."""

    success: bool
    status: int
    data: Any
    latency_ms: float
    endpoint: str
    error: Optional[str] = None
    raw_body: str = ""


class HftConnectionPool:
    """Dedicated low-latency connection pool for Polymarket CLOB order execution and cancellation."""

    def __init__(
        self,
        host: str = DEFAULT_CLOB_HOST,
        port: int = DEFAULT_CLOB_PORT,
        pool_size: int = 4,
        timeout: float = 3.0,
        max_retries: int = 2,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        signer_address: str = "",
        ssl_context: Optional[ssl.SSLContext] = None,
        auto_warm: bool = True,
    ) -> None:
        self.host = host.removeprefix("https://").removeprefix("http://").split("/")[0]
        self.port = port
        self.pool_size = max(1, pool_size)
        self.timeout = timeout
        self.max_retries = max_retries

        # L2 Credentials & Pre-decoded Secret for fast HMAC signing
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.signer_address = signer_address

        self._secret_bytes: bytes = b""
        if api_secret:
            try:
                padded = api_secret + "=" * (-len(api_secret) % 4)
                self._secret_bytes = base64.urlsafe_b64decode(padded)
            except Exception:
                self._secret_bytes = api_secret.encode("utf-8")

        self.ssl_context = ssl_context or ssl.create_default_context()

        # LIFO queue prioritizes the warmest, most-recently used connection
        self._pool: queue.LifoQueue[http.client.HTTPSConnection] = queue.LifoQueue(
            maxsize=self.pool_size * 2
        )
        self._lock = threading.Lock()
        self._is_closed = False
        self._active_connections: List[http.client.HTTPSConnection] = []

        if auto_warm:
            self.warm_up()

    def _create_connection(self) -> http.client.HTTPSConnection:
        """Instantiate and pre-connect an HTTPS connection configured for sub-millisecond latency."""
        conn = http.client.HTTPSConnection(
            host=self.host,
            port=self.port,
            timeout=self.timeout,
            context=self.ssl_context,
        )
        conn.connect()
        # Set socket options: TCP_NODELAY (no Nagle) + SO_KEEPALIVE
        if conn.sock is not None:
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return conn

    def warm_up(self) -> int:
        """Pre-warm connections in the pool. Returns the number of successfully connected sockets."""
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
                    logger.warning(f"Failed to pre-warm HFT connection to {self.host}: {exc}")
        return warmed

    def _acquire_connection(self) -> http.client.HTTPSConnection:
        """Acquire the warmest persistent connection from the LIFO pool or spawn a new one."""
        if self._is_closed:
            raise RuntimeError("HftConnectionPool is closed.")

        try:
            return self._pool.get_nowait()
        except queue.Empty:
            return self._create_connection()

    def _release_connection(self, conn: http.client.HTTPSConnection) -> None:
        """Return a healthy persistent connection back to the front of the LIFO pool."""
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
        """Safely close and deregister a dropped or broken connection."""
        try:
            conn.close()
        except Exception:
            pass
        with self._lock:
            if conn in self._active_connections:
                self._active_connections.remove(conn)

    def build_fast_auth_headers(
        self,
        method: str,
        path: str,
        body_bytes: Optional[bytes] = None,
    ) -> Dict[str, str]:
        """Compute Level-2 HMAC-SHA256 headers directly on raw bytes with zero string copying."""
        if not self.api_key or not self._secret_bytes:
            return {}

        ts = str(int(time.time()))
        # Fast byte concatenation: ts + method + path + body
        msg = ts.encode("ascii") + method.upper().encode("ascii") + path.encode("ascii")
        if body_bytes:
            msg += body_bytes

        digest = hmac.new(self._secret_bytes, msg, hashlib.sha256).digest()
        sig = base64.urlsafe_b64encode(digest).decode("ascii")

        headers = {
            "Host": self.host,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "keep-alive",
            "POLY_API_KEY": self.api_key,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts,
            "POLY_PASSPHRASE": self.passphrase,
        }
        if self.signer_address:
            headers["POLY_ADDRESS"] = self.signer_address
        return headers

    def send_request(
        self,
        method: str,
        endpoint: str,
        body: Optional[Union[bytes, str, dict, list]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> HftOrderResponse:
        """Execute an ultra-low latency HTTP request over the warmest pre-warmed connection."""
        method_up = method.upper()

        # Fast serialization into bytes via orjson
        if body is None:
            body_bytes: Optional[bytes] = None
        elif isinstance(body, bytes):
            body_bytes = body
        elif isinstance(body, str):
            body_bytes = body.encode("utf-8")
        else:
            body_bytes = orjson.dumps(body)

        req_headers = {
            "Host": self.host,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Connection": "keep-alive",
        }
        # Apply precomputed auth headers
        if self.api_key and self._secret_bytes:
            req_headers.update(
                self.build_fast_auth_headers(method_up, endpoint, body_bytes)
            )
        if headers:
            req_headers.update(headers)

        last_error: Optional[str] = None

        for attempt in range(self.max_retries + 1):
            conn: Optional[http.client.HTTPSConnection] = None
            try:
                conn = self._acquire_connection()
                t0 = time.perf_counter()
                conn.request(method_up, endpoint, body=body_bytes, headers=req_headers)
                resp = conn.getresponse()
                raw_data = resp.read()
                latency_ms = (time.perf_counter() - t0) * 1000.0

                # Return healthy connection back to LIFO queue
                self._release_connection(conn)

                success = 200 <= resp.status < 300
                raw_str = raw_data.decode("utf-8", errors="replace")
                parsed_data: Any = None
                if raw_data:
                    try:
                        parsed_data = orjson.loads(raw_data)
                    except Exception:
                        parsed_data = raw_str

                return HftOrderResponse(
                    success=success,
                    status=resp.status,
                    data=parsed_data,
                    latency_ms=latency_ms,
                    endpoint=endpoint,
                    raw_body=raw_str,
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
                    f"HFT connection dropped on attempt {attempt + 1}/{self.max_retries + 1} "
                    f"for {method_up} {endpoint}: {last_error}. Reconnecting & retrying."
                )

                if attempt >= self.max_retries:
                    break

        return HftOrderResponse(
            success=False,
            status=0,
            data=None,
            latency_ms=0.0,
            endpoint=endpoint,
            error=last_error or "Max retries exceeded without response",
            raw_body="",
        )

    # -------------------------------------------------------------------------
    # High-Performance Order Placement (POST /order & POST /orders)
    # -------------------------------------------------------------------------
    def place_order(
        self,
        order_payload: Union[bytes, dict[str, Any]],
        headers: Optional[Dict[str, str]] = None,
    ) -> HftOrderResponse:
        """Submit a single signed order to POST /order with zero-overhead socket dispatch."""
        return self.send_request("POST", "/order", body=order_payload, headers=headers)

    def place_orders(
        self,
        orders_payload: Union[bytes, Sequence[dict[str, Any]]],
        headers: Optional[Dict[str, str]] = None,
    ) -> HftOrderResponse:
        """Submit a batch of signed orders to POST /orders via a single network round-trip."""
        return self.send_request("POST", "/orders", body=orders_payload, headers=headers)

    # -------------------------------------------------------------------------
    # High-Performance Order Cancellation (DELETE /order & DELETE /cancel-all)
    # -------------------------------------------------------------------------
    def cancel_order(
        self,
        order_id: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> CancelResult:
        """Cancel a single resting order via DELETE /order with minimal serialization."""
        payload = b'{"orderID":"' + order_id.encode("ascii", errors="replace") + b'"}'
        resp = self.send_request("DELETE", "/order", body=payload, headers=headers)
        return CancelResult(
            success=resp.success,
            status=resp.status,
            body=resp.raw_body,
            latency_ms=resp.latency_ms,
            endpoint=resp.endpoint,
            order_id=order_id,
            error=resp.error,
        )

    def cancel_all(
        self,
        market: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> CancelResult:
        """Cancel all resting orders via DELETE /cancel-all."""
        if market:
            payload = b'{"market":"' + market.encode("utf-8") + b'"}'
        else:
            payload = b"{}"

        resp = self.send_request("DELETE", "/cancel-all", body=payload, headers=headers)
        return CancelResult(
            success=resp.success,
            status=resp.status,
            body=resp.raw_body,
            latency_ms=resp.latency_ms,
            endpoint=resp.endpoint,
            order_id=None,
            error=resp.error,
        )

    def cancel_orders(
        self,
        order_ids: Sequence[str],
        headers: Optional[Dict[str, str]] = None,
    ) -> List[CancelResult]:
        """Cancel multiple orders in rapid succession reusing the warmest keep-alive socket."""
        return [self.cancel_order(oid, headers=headers) for oid in order_ids]

    # -------------------------------------------------------------------------
    # Lifecycle Management
    # -------------------------------------------------------------------------
    def close(self) -> None:
        """Gracefully close all active connections and tear down the pool."""
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

    def __enter__(self) -> HftConnectionPool:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
