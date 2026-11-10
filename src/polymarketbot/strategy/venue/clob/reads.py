"""Read-only CLOB endpoints: orders, trades, balances, markets and books."""

from __future__ import annotations

from typing import Any, Optional

from polymarketbot.strategy.venue.clob.common import (
    COLLATERAL_DECIMALS,
    DEFAULT_TICK_SIZE,
    ClobAPIError,
)


def _parse_collateral_amount(value: Any) -> float:
    """Convert a CLOB fixed-math amount into decimal collateral units.

    CLOB returns 6-decimal base units as strings (``"1500000"`` == 1.5 USDC).
    Some deployments/proxies return already-decimal strings (``"1.5"``); a value
    containing ``.`` (or an exponent) is treated as decimal, everything else as
    raw base units. The ``> 100_000`` guess from the old implementation is gone.
    """
    if isinstance(value, bool):
        raise ValueError(f"not an amount: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        raise ValueError("empty amount")
    if "." in text or "e" in text.lower():
        return float(text)
    return int(text) / float(10**COLLATERAL_DECIMALS)


def _allowance_from_payload(payload: dict[str, Any]) -> float:
    """Extract the collateral allowance from a /balance-allowance response."""
    if payload.get("allowance") not in (None, ""):
        return _parse_collateral_amount(payload["allowance"])
    allowances = payload.get("allowances")
    if isinstance(allowances, dict):
        amounts = [
            _parse_collateral_amount(v)
            for v in allowances.values()
            if v not in (None, "")
        ]
        # An allowance map holds one entry per spender (exchange / neg-risk exchange).
        # Collateral is tradeable when at least one spender is approved.
        return max(amounts) if amounts else 0.0
    raise ValueError("response contains neither 'allowance' nor 'allowances'")


class ReadMixin:
    """Read-only CLOB queries."""


    def get_open_orders(
        self,
        market: Optional[str] = None,
        *,
        order_id: Optional[str] = None,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        """Query user orders from ``GET /data/orders`` (audit C3/M1).

        Reference: https://docs.polymarket.com/api-reference/trade/get-user-orders
        Response is ``{limit, next_cursor, count, data}``; ``next_cursor`` is
        followed until empty/``LTE=``. ``market`` filters by condition id and
        ``order_id`` returns that single order regardless of status.

        Raises:
            ClobAPIError: on any failed call, unexpected body, or a repeated
                pagination cursor. Never returns ``[]`` for a failed call.
        """
        if self.dry_run:
            orders = list(self._mock_orders.values())
            if order_id:
                return [o for o in orders if str(o.get("id") or o.get("orderID") or "") == order_id]
            if market:
                return [o for o in orders if o.get("market") in (None, market)]
            return orders

        if order_id:
            params: dict[str, Any] = {"id": order_id}
            pages = 1
        else:
            params = {"market": market} if market else {}
            pages = max_pages
        return self._paginate("/data/orders", params, max_pages=pages)

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        """Fetch one order by id (first element of ``/data/orders?id=...``), or None.

        Raises:
            ClobAPIError: on any failed call. None means the exchange answered
                and had no such order, or ``order_id`` was empty.
        """
        if not order_id:
            return None
        if self.dry_run:
            return self._mock_orders.get(order_id)
        orders = self.get_open_orders(order_id=order_id, max_pages=1)
        return orders[0] if orders else None

    def get_trades(
        self,
        market: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query the authenticated user's trades from ``GET /data/trades``.

        Reference: https://docs.polymarket.com/api-reference/trade/get-trades
        Required query parameter ``maker_address`` is sent as ``self.maker_address``;
        response is ``{limit, next_cursor, count, data}`` and ``next_cursor`` is
        followed until ``LTE=``.

        Raises:
            ClobAPIError: on any failed call or unexpected body.
        """
        if self.dry_run:
            return []

        params: dict[str, Any] = {
            "maker_address": self.maker_address,
            "limit": int(limit),
        }
        if market:
            params["market"] = market
        return self._paginate("/data/trades", params, max_pages=20)

    def _get_balance_allowance(self) -> dict[str, Any]:
        """GET /balance-allowance for COLLATERAL at our signature type (audit C4)."""
        path = "/balance-allowance"
        params = {
            "asset_type": "COLLATERAL",
            "signature_type": self.signature_type,
        }
        payload = self._request("GET", path, params=params)
        if not isinstance(payload, dict) or "balance" not in payload:
            raise ClobAPIError(
                f"GET {path} returned an unexpected body (missing 'balance')",
                status_code=200,
                path=path,
            )
        if "allowance" not in payload and "allowances" not in payload:
            raise ClobAPIError(
                f"GET {path} returned an unexpected body (missing 'allowance')",
                status_code=200,
                path=path,
            )
        return payload

    def get_balance(self) -> dict[str, Any]:
        """Query the CLOB collateral balance for the authenticated account.

        Returns ``{"available", "locked", "allowance", "raw"}``. ``available`` is
        the decimal USDC balance; ``locked`` is the collateral reserved by resting
        BUY orders (not reported by this endpoint yet, always 0.0); ``allowance``
        is the approved collateral allowance.

        Raises:
            ClobAPIError: on any failed call or unparseable amount.
        """
        if self.dry_run:
            return {
                "available": self._mock_balance_usd,
                "locked": 0.0,
                "allowance": self._mock_balance_usd,
                "raw": {"mock": True},
            }

        payload = self._get_balance_allowance()
        try:
            available = _parse_collateral_amount(payload["balance"])
            allowance = _allowance_from_payload(payload)
        except (TypeError, ValueError) as e:
            raise ClobAPIError(
                f"GET /balance-allowance returned unparseable amounts: {e}",
                status_code=200,
                path="/balance-allowance",
            ) from e
        return {
            "available": available,
            "locked": 0.0,
            "allowance": allowance,
            "raw": payload,
        }

    def get_allowances(self) -> dict[str, Any]:
        """Query collateral approvals from ``/balance-allowance?asset_type=COLLATERAL``.

        Returns ``{"approved", "collateral", "conditional", "raw"}``; ``approved``
        is ``collateral > 0`` for the signature type in use. ``conditional`` is not
        reported by this call (always 0.0).

        Raises:
            ClobAPIError: if the call fails or the body cannot be parsed. A missing
                key never becomes ``approved=False``.
        """
        if self.dry_run:
            return {
                "approved": True,
                "collateral": 1_000_000.0,
                "conditional": 0.0,
                "raw": {"mock": True},
            }

        payload = self._get_balance_allowance()
        try:
            collateral = _allowance_from_payload(payload)
        except (TypeError, ValueError) as e:
            raise ClobAPIError(
                f"GET /balance-allowance returned an unparseable allowance: {e}",
                status_code=200,
                path="/balance-allowance",
            ) from e
        return {
            "approved": collateral > 0.0,
            "collateral": collateral,
            "conditional": 0.0,
            "raw": payload,
        }

    def get_market(self, condition_id: str) -> dict[str, Any]:
        """Fetch market parameters from ``GET /markets/{condition_id}``.

        Returns ``{"neg_risk", "tick_size", "fee_rate_bps", "closed",
        "accepting_orders", "end_date_iso", "raw"}``.

        Raises:
            ClobAPIError: on any failed call or non-object body.
            ValueError: for an empty condition id.
        """
        if not condition_id:
            raise ValueError("condition_id is required")

        path = f"/markets/{condition_id}"
        payload = self._request("GET", path, auth=False)
        if not isinstance(payload, dict):
            raise ClobAPIError(
                f"GET {path} returned a non-object body",
                status_code=200,
                path=path,
            )

        tick_raw = payload.get("minimum_tick_size")
        if tick_raw in (None, ""):
            tick_raw = payload.get("tick_size")
        try:
            tick_size = float(tick_raw) if tick_raw not in (None, "") else DEFAULT_TICK_SIZE
        except (TypeError, ValueError):
            tick_size = DEFAULT_TICK_SIZE
        if tick_size <= 0.0:
            tick_size = DEFAULT_TICK_SIZE

        fee_raw = payload.get("fee_rate_bps")
        if fee_raw is None:
            fee_raw = payload.get("taker_base_fee")
        try:
            fee_rate_bps = int(float(fee_raw)) if fee_raw not in (None, "") else 0
        except (TypeError, ValueError):
            fee_rate_bps = 0

        end_date_iso = payload.get("end_date_iso")
        return {
            "neg_risk": bool(payload.get("neg_risk", False)),
            "tick_size": tick_size,
            "fee_rate_bps": fee_rate_bps,
            "closed": bool(payload.get("closed", False)),
            "accepting_orders": bool(payload.get("accepting_orders", False)),
            "end_date_iso": str(end_date_iso) if end_date_iso else None,
            "raw": payload,
        }

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        """Fetch live level-2 order book from ``GET /book?token_id={token_id}``.

        Returns:
            dict containing "bids" and "asks" lists with {"price": str, "size": str}.

        Raises:
            ClobAPIError: on any failed call or non-object body.
            ValueError: for an empty token id.
        """
        if not token_id:
            raise ValueError("token_id is required")

        if self.dry_run and hasattr(self, "_mock_books") and str(token_id) in self._mock_books:
            return self._mock_books[str(token_id)]

        path = "/book"
        params = {"token_id": str(token_id)}
        payload = self._request("GET", path, params=params, auth=False)
        if not isinstance(payload, dict):
            raise ClobAPIError(
                f"GET {path} returned an unexpected body: {payload!r}",
                status_code=200,
                path=path,
            )
        return payload

    def set_mock_book(self, token_id: str, book: dict[str, Any]) -> None:
        """Register a mock order book for dry-run or testing."""
        if not hasattr(self, "_mock_books"):
            self._mock_books = {}
        self._mock_books[str(token_id)] = book
