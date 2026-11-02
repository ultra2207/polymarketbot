"""Lightweight Kalshi REST API v2 Client.

Supports:
- Kalshi REST v2 production (`https://api.elections.kalshi.com/trade-api/v2`)
- Kalshi REST v2 demo (`https://external-api.demo.kalshi.co/trade-api/v2`)
- Post-Only limit orders (maker-only execution with zero maker fees)
- Order Groups circuit breaker specification (max fills / automatic cancellation)
- Single and batch cancellation endpoints
- Sync and async execution interfaces
- Offline mock mode for deterministic low-latency testing
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests

from polymarketbot.logs import get_logger

logger = get_logger("kalshi_client")

PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"


class KalshiOrderError(RuntimeError):
    """Raised when Kalshi order placement, validation or cancellation fails."""


@dataclass
class KalshiOrder:
    """Represents an order on Kalshi."""

    order_id: str
    ticker: str
    action: str  # 'buy' or 'sell'
    side: str  # 'yes' or 'no'
    type: str  # 'limit' or 'market'
    count: int
    yes_price: Optional[int] = None
    no_price: Optional[int] = None
    post_only: bool = True
    order_group_id: Optional[str] = None
    client_order_id: Optional[str] = None
    status: str = "resting"  # 'resting', 'filled', 'canceled', 'rejected'
    placed_at: float = field(default_factory=time.time)
    filled_count: int = 0
    maker_fee: float = 0.0  # 0% maker fee on Kalshi


@dataclass
class KalshiOrderGroup:
    """Circuit breaker order group."""

    order_group_id: str
    contracts_limit: Optional[int] = None
    max_cost_cents: Optional[int] = None
    status: str = "active"  # 'active', 'triggered', 'closed'
    filled_contracts: int = 0
    active_orders: List[str] = field(default_factory=list)


class KalshiClient:
    """Client for Kalshi REST v2 API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        private_key: Optional[str] = None,
        token: Optional[str] = None,
        demo: bool = False,
        base_url: Optional[str] = None,
        timeout: float = 5.0,
        mock_mode: bool = False,
    ) -> None:
        self.demo = demo
        if base_url:
            self.base_url = base_url.rstrip("/")
        else:
            self.base_url = DEMO_BASE_URL if demo else PROD_BASE_URL

        self.api_key = api_key
        self.private_key = private_key
        self.token = token
        self.timeout = timeout
        self.mock_mode = mock_mode

        self.session = requests.Session()
        self._mock_orders: Dict[str, KalshiOrder] = {}
        self._mock_order_groups: Dict[str, KalshiOrderGroup] = {}
        self.recorded_requests: List[Dict[str, Any]] = []

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.api_key:
            headers["KALSHI-ACCESS-KEY"] = self.api_key
        return headers

    def create_order(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        type: str = "limit",
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        post_only: bool = True,
        order_group_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        buy_max_cost: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create an order on Kalshi REST v2.

        Enforces post_only limit rules (zero maker fee) and circuit breaker order group tags.
        """
        action = action.lower()
        side = side.lower()
        type = type.lower()

        if action not in ("buy", "sell"):
            raise KalshiOrderError(f"Invalid action '{action}', must be 'buy' or 'sell'")
        if side not in ("yes", "no"):
            raise KalshiOrderError(f"Invalid side '{side}', must be 'yes' or 'no'")
        if count <= 0:
            raise KalshiOrderError(f"Order count must be positive, got {count}")
        if type == "limit":
            if yes_price is None and no_price is None:
                raise KalshiOrderError("Limit order requires yes_price or no_price in cents (1-99)")
            price_val = yes_price if yes_price is not None else no_price
            if price_val is not None and not (1 <= price_val <= 99):
                raise KalshiOrderError(f"Price in cents must be in range [1, 99], got {price_val}")
        elif post_only:
            raise KalshiOrderError("Market orders cannot be post_only (must be limit)")

        payload: Dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "type": type,
            "side": side,
            "count": count,
            "post_only": post_only,
        }
        if yes_price is not None:
            payload["yes_price"] = yes_price
        if no_price is not None:
            payload["no_price"] = no_price
        if order_group_id:
            payload["order_group_id"] = order_group_id
        if client_order_id:
            payload["client_order_id"] = client_order_id
        if buy_max_cost is not None:
            payload["buy_max_cost"] = buy_max_cost

        if self.mock_mode:
            # Check circuit breaker order group if specified
            if order_group_id and order_group_id in self._mock_order_groups:
                group = self._mock_order_groups[order_group_id]
                if group.status == "triggered":
                    raise KalshiOrderError(f"Order group '{order_group_id}' circuit breaker is triggered")
                if group.contracts_limit and (group.filled_contracts + count > group.contracts_limit):
                    group.status = "triggered"
                    raise KalshiOrderError(
                        f"Order rejected: order group '{order_group_id}' limit {group.contracts_limit} reached"
                    )

            order_id = client_order_id or f"ord_{uuid.uuid4().hex[:12]}"
            order = KalshiOrder(
                order_id=order_id,
                ticker=ticker,
                action=action,
                side=side,
                type=type,
                count=count,
                yes_price=yes_price,
                no_price=no_price,
                post_only=post_only,
                order_group_id=order_group_id,
                client_order_id=client_order_id,
                status="resting",
            )
            self._mock_orders[order_id] = order
            if order_group_id and order_group_id in self._mock_order_groups:
                self._mock_order_groups[order_group_id].active_orders.append(order_id)

            record = {"endpoint": "create_order", "payload": payload, "order_id": order_id}
            self.recorded_requests.append(record)
            logger.info(f"Mock Kalshi Order Placed: {order_id} ticker={ticker} side={side} post_only={post_only}")
            return {
                "order": {
                    "order_id": order_id,
                    "ticker": ticker,
                    "action": action,
                    "side": side,
                    "type": type,
                    "status": "resting",
                    "post_only": post_only,
                    "order_group_id": order_group_id,
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "count": count,
                }
            }

        url = f"{self.base_url}/portfolio/orders"
        resp = self.session.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            raise KalshiOrderError(f"Kalshi order placement failed: {resp.status_code} - {resp.text}")
        return resp.json()

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an order by order_id."""
        if not order_id:
            raise KalshiOrderError("Order ID cannot be empty")

        if self.mock_mode:
            self.recorded_requests.append({"endpoint": "cancel_order", "order_id": order_id})
            if order_id in self._mock_orders:
                self._mock_orders[order_id].status = "canceled"
                return {"order": {"order_id": order_id, "status": "canceled"}}
            return {"order": {"order_id": order_id, "status": "canceled", "mock_not_found": True}}

        url = f"{self.base_url}/portfolio/orders/{order_id}"
        resp = self.session.delete(url, headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            raise KalshiOrderError(f"Kalshi order cancellation failed: {resp.status_code} - {resp.text}")
        return resp.json()

    def cancel_all_orders(
        self, ticker: Optional[str] = None, order_group_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Batch cancel orders, optionally filtered by ticker or order_group_id."""
        if self.mock_mode:
            cancelled_ids = []
            for oid, ord_obj in list(self._mock_orders.items()):
                if ord_obj.status == "resting":
                    if ticker and ord_obj.ticker != ticker:
                        continue
                    if order_group_id and ord_obj.order_group_id != order_group_id:
                        continue
                    ord_obj.status = "canceled"
                    cancelled_ids.append(oid)
            self.recorded_requests.append(
                {"endpoint": "cancel_all_orders", "cancelled_ids": cancelled_ids, "ticker": ticker}
            )
            return {"cancelled_orders": cancelled_ids}

        url = f"{self.base_url}/portfolio/orders"
        params: Dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if order_group_id:
            params["order_group_id"] = order_group_id

        resp = self.session.delete(url, params=params, headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            raise KalshiOrderError(f"Kalshi batch cancel failed: {resp.status_code} - {resp.text}")
        return resp.json()

    def create_order_group(
        self,
        contracts_limit: Optional[int] = None,
        max_cost_cents: Optional[int] = None,
        order_group_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create an Order Group for circuit breaker protections."""
        group_id = order_group_id or f"grp_{uuid.uuid4().hex[:8]}"
        payload = {
            "order_group_id": group_id,
            "contracts_limit": contracts_limit,
            "max_cost_cents": max_cost_cents,
        }

        if self.mock_mode:
            group = KalshiOrderGroup(
                order_group_id=group_id,
                contracts_limit=contracts_limit,
                max_cost_cents=max_cost_cents,
                status="active",
            )
            self._mock_order_groups[group_id] = group
            self.recorded_requests.append({"endpoint": "create_order_group", "payload": payload})
            return {"order_group": {"order_group_id": group_id, "status": "active"}}

        url = f"{self.base_url}/portfolio/order_groups"
        resp = self.session.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            raise KalshiOrderError(f"Create order group failed: {resp.status_code} - {resp.text}")
        return resp.json()

    def cancel_order_group(self, order_group_id: str) -> Dict[str, Any]:
        """Trip circuit breaker and cancel all orders in an order group."""
        if self.mock_mode:
            if order_group_id in self._mock_order_groups:
                self._mock_order_groups[order_group_id].status = "triggered"
            # Cancel all orders associated with this group
            return self.cancel_all_orders(order_group_id=order_group_id)

        url = f"{self.base_url}/portfolio/order_groups/{order_group_id}"
        resp = self.session.delete(url, headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            raise KalshiOrderError(f"Cancel order group failed: {resp.status_code} - {resp.text}")
        return resp.json()

    def get_markets(
        self,
        status: str = "open",
        limit: int = 100,
        cursor: Optional[str] = None,
        series_ticker: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Query Kalshi markets (GET /markets)."""
        params: Dict[str, Any] = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker

        if self.mock_mode:
            record = {"endpoint": "get_markets", "params": params}
            self.recorded_requests.append(record)
            return {"markets": [], "cursor": None}

        url = f"{self.base_url}/markets"
        resp = self.session.get(url, params=params, headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            raise KalshiOrderError(f"Query markets failed: {resp.status_code} - {resp.text}")
        return resp.json()

    # Async helpers
    async def async_get_markets(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Asynchronous wrapper for get_markets."""
        return await asyncio.to_thread(self.get_markets, *args, **kwargs)

    async def async_create_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Asynchronous wrapper for create_order."""
        return await asyncio.to_thread(self.create_order, *args, **kwargs)

    async def async_cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Asynchronous wrapper for cancel_order."""
        return await asyncio.to_thread(self.cancel_order, order_id)

    async def async_cancel_all_orders(
        self, ticker: Optional[str] = None, order_group_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Asynchronous wrapper for cancel_all_orders."""
        return await asyncio.to_thread(self.cancel_all_orders, ticker=ticker, order_group_id=order_group_id)

    async def async_create_order_group(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Asynchronous wrapper for create_order_group."""
        return await asyncio.to_thread(self.create_order_group, *args, **kwargs)
