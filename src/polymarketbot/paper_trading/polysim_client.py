"""PolySimulator Client Adapter for Cloud-Hosted Polymarket Paper Trading.

Connects to https://api.polysimulator.com/v1 for simulated execution against live
Polymarket CLOB order book data.

Features:
- Authenticates with X-API-Key header (ps_live_...).
- Queries cloud account balance ($100 API wallet), positions, and portfolio.
- Retrieves live Polymarket market data, order books, midpoints, and tick sizes.
- Submits simulated maker limit orders (POST /v1/clob/order) with price & size quantization.
- Cancels resting orders (DELETE /v1/orders/{id}) and mass cancels (POST /v1/cancel-all).
- Built-in rate limiting, retries, and error handling conforming to AGENTS.md.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from polymarketbot.logs import get_logger

log = get_logger("polysim_client")

DEFAULT_BASE_URL = "https://api.polysimulator.com/v1"


def _read_env_key(key_name: str = "POLYSIMULATOR_API_KEY") -> str:
    """Read API key from environment variable or .env file without external dependencies."""
    if key_name in os.environ and os.environ[key_name].strip():
        return os.environ[key_name].strip()
    path = Path(".env")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key_name:
                return v.strip().strip('"').strip("'")
    return ""


@dataclass
class PolySimBalance:
    """PolySimulator Account Balance."""
    balance: float
    currency: str
    unrealized_pnl: float
    total_value: float
    starting_balance: float


@dataclass
class PolySimOrderResult:
    """Result of placing or cancelling an order on PolySimulator."""
    success: bool
    order_id: Optional[str]
    status: str
    side: str
    price: float
    size: float
    error_msg: Optional[str] = None
    raw_response: Optional[dict[str, Any]] = None


class PolySimulatorClient:
    """REST Client for the PolySimulator paper-trading API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key or _read_env_key("POLYSIMULATOR_API_KEY")
        if not self.api_key:
            log.warning("No PolySimulator API key provided or found in .env.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": "polymarketbot/polysim-client",
        })

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[Any] = None,
        max_retries: int = 3,
    ) -> requests.Response:
        """Execute HTTP request with retry logic for 429 rate limits and 5xx errors."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        for attempt in range(max_retries):
            try:
                resp = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    timeout=self.timeout,
                )
                if resp.status_code == 429:
                    wait_sec = float(resp.headers.get("Retry-After", 1.5 * (2**attempt)))
                    code = resp.headers.get("X-Polysim-Code", "")
                    if wait_sec > 10.0 or code == "FREE_TIER_DAILY_LIMIT":
                        log.error(f"PolySimulator daily order limit reached ({wait_sec:.0f}s reset). Returning response immediately.")
                        return resp
                    log.warning(f"PolySimulator rate limit hit (429). Retrying in {wait_sec:.1f}s...")
                    time.sleep(wait_sec)
                    continue
                if resp.status_code >= 500 and attempt < max_retries - 1:
                    wait_sec = 1.0 * (2**attempt)
                    log.warning(f"PolySimulator server error ({resp.status_code}). Retrying in {wait_sec:.1f}s...")
                    time.sleep(wait_sec)
                    continue
                return resp
            except requests.RequestException as exc:
                if attempt < max_retries - 1:
                    wait_sec = 1.0 * (2**attempt)
                    log.warning(f"Network error communicating with PolySimulator: {exc}. Retrying in {wait_sec:.1f}s...")
                    time.sleep(wait_sec)
                else:
                    raise
        raise RuntimeError(f"PolySimulator request to {endpoint} failed after {max_retries} attempts.")

    # ------------------------------------------------------------------
    # Account & Portfolio
    # ------------------------------------------------------------------

    def get_balance(self) -> PolySimBalance:
        """Fetch simulated account balance, unrealized PnL, and total equity."""
        resp = self._request("GET", "/account/balance")
        resp.raise_for_status()
        data = resp.json()
        return PolySimBalance(
            balance=float(data.get("balance", 0.0)),
            currency=str(data.get("currency", "USD")),
            unrealized_pnl=float(data.get("unrealized_pnl", 0.0)),
            total_value=float(data.get("total_value", 0.0)),
            starting_balance=float(data.get("starting_balance", 0.0)),
        )

    def get_positions(self, status: str = "open") -> list[dict[str, Any]]:
        """Fetch open or closed simulated positions."""
        resp = self._request("GET", "/account/positions", params={"status": status})
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("positions", [])

    def get_portfolio(self) -> dict[str, Any]:
        """Fetch aggregate portfolio summary."""
        resp = self._request("GET", "/account/portfolio")
        resp.raise_for_status()
        return resp.json()

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch history of filled simulated orders."""
        resp = self._request("GET", "/account/history", params={"limit": limit})
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("history", [])

    get_trade_history = get_history

    # ------------------------------------------------------------------
    # Markets & Order Book Data
    # ------------------------------------------------------------------

    def get_markets(
        self,
        limit: int = 20,
        offset: int = 0,
        hot_only: bool = False,
        search: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Fetch active Polymarket prediction markets available on PolySimulator."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if hot_only:
            params["hot_only"] = "true"
        if search:
            params["search"] = search
        resp = self._request("GET", "/markets", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_market(self, condition_id: str) -> dict[str, Any]:
        """Fetch metadata for a single market by condition ID."""
        resp = self._request("GET", f"/markets/{condition_id}")
        resp.raise_for_status()
        return resp.json()

    def get_book(self, token_id: str, depth: int = 20) -> dict[str, Any]:
        """Fetch L2 order book for a given outcome token ID.

        Note: PolySimulator /book wire format sorts bids ascending (best bid at [-1])
        and asks descending (best ask at [-1]), identical to Polymarket CLOB.
        """
        resp = self._request("GET", "/book", params={"token_id": token_id, "depth": depth})
        resp.raise_for_status()
        return resp.json()

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Fetch current midpoint price for a token."""
        resp = self._request("GET", "/midpoint", params={"token_id": token_id})
        if resp.status_code == 200:
            val = resp.json().get("mid")
            return float(val) if val is not None else None
        return None

    def get_spread(self, token_id: str) -> Optional[dict[str, Any]]:
        """Fetch spread and top-of-book for a token."""
        resp = self._request("GET", "/spread", params={"token_id": token_id})
        if resp.status_code == 200:
            return resp.json()
        return None

    def get_tick_size(self, token_id: str) -> float:
        """Fetch canonical minimum tick size for a token (e.g. 0.01, 0.001)."""
        resp = self._request("GET", f"/tick-size/{token_id}")
        if resp.status_code == 200:
            data = resp.json()
            return float(data.get("minimum_tick_size", 0.01))
        return 0.01

    # ------------------------------------------------------------------
    # Order Execution & Management
    # ------------------------------------------------------------------

    def place_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        order_type: str = "GTC",
        client_order_id: Optional[str] = None,
        tick_size: Optional[float] = None,
    ) -> PolySimOrderResult:
        """Place a simulated limit order into PolySimulator's matching engine.

        Args:
            token_id: Polymarket outcome token ID.
            side: "BUY" or "SELL".
            size: Quantity in outcome tokens/shares.
            price: Limit price in USD (0.01 to 0.99).
            order_type: "GTC" (default), "FOK", or "GTD".
            client_order_id: Optional idempotency key.
            tick_size: Optional tick size to quantize limit price.

        Returns:
            PolySimOrderResult with order status and ID.
        """
        side_norm = side.strip().upper()
        if side_norm not in ("BUY", "SELL"):
            raise ValueError(f"Invalid side '{side}', must be 'BUY' or 'SELL'.")

        # Quantize price to tick size if provided
        if tick_size and tick_size > 0:
            steps = round(price / tick_size)
            price = round(steps * tick_size, 4)

        # Build payload for /v1/clob/order (legacy flat shape)
        cid = client_order_id or f"ps_{int(time.time() * 1000)}"
        payload = {
            "token_id": token_id,
            "side": side_norm,
            "size": f"{size:.4f}",
            "price": f"{price:.4f}",
            "order_type": order_type,
            "client_order_id": cid,
        }

        resp = self._request("POST", "/clob/order", json_data=payload)
        data = resp.json() if resp.text else {}

        if resp.status_code in (200, 201) and data.get("success", False):
            oid = str(data.get("orderID") or data.get("order_id", ""))
            status = str(data.get("status", "live"))
            return PolySimOrderResult(
                success=True,
                order_id=oid,
                status=status,
                side=side_norm,
                price=float(data.get("price", price)),
                size=float(data.get("size", size)),
                raw_response=data,
            )

        err_msg = data.get("errorMsg") or data.get("message") or resp.text
        log.warning(f"PolySimulator order rejected ({resp.status_code}): {err_msg}")
        return PolySimOrderResult(
            success=False,
            order_id=None,
            status="rejected",
            side=side_norm,
            price=price,
            size=size,
            error_msg=str(err_msg),
            raw_response=data,
        )

    def get_open_orders(self) -> list[dict[str, Any]]:
        """Fetch all currently resting/pending orders."""
        resp = self._request("GET", "/orders", params={"status": "PENDING"})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("orders", [])

    def cancel_order(self, order_id: str | int) -> bool:
        """Cancel a single resting order by order ID."""
        resp = self._request("DELETE", f"/orders/{order_id}")
        if resp.status_code == 200:
            return True
        log.warning(f"Failed to cancel order {order_id} ({resp.status_code}): {resp.text}")
        return False

    def cancel_all(self) -> dict[str, Any]:
        """Cancel all resting orders for the authenticated key (dead-man / kill-switch)."""
        headers = {"X-Confirm-Cancel-All": "true"}
        old_headers = dict(self.session.headers)
        self.session.headers.update(headers)
        try:
            resp = self._request("POST", "/cancel-all", params={"confirm": "true"})
            if resp.status_code == 200:
                log.info("Successfully cancelled all resting orders on PolySimulator.")
                return resp.json() if resp.text else {"status": "ok"}
            log.warning(f"POST /cancel-all failed ({resp.status_code}): {resp.text}")
            return {"status": "error", "code": resp.status_code, "message": resp.text}
        finally:
            self.session.headers.clear()
            self.session.headers.update(old_headers)

