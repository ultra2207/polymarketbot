"""Read-only CLOB endpoints: orders, trades, balances, markets and books."""

from __future__ import annotations

from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.strategy.venue.clob.common import (
    COLLATERAL_DECIMALS,
    DEFAULT_TICK_SIZE,
    ClobAPIError,
    ClobMarketParamsUnreadable,
)

logger = get_logger("clob_client")


def _parse_collateral_amount(value: Any) -> float:
    """Convert a CLOB fixed-math amount into decimal collateral units.

    CLOB returns 6-decimal base units as strings (``"1500000"`` == 1.5 USDC).
    Some deployments/proxies return already-decimal strings (``"1.5"``); a value
    containing ``.`` (or an exponent) is treated as decimal, everything else as
    raw base units. The ``> 100_000`` guess from the old implementation is gone.
    """
    if isinstance(value, bool):
        raise ValueError(f"not an amount: {value!r}")  # noqa: TRY004 - callers contract on ValueError for invalid input
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
        market: str | None = None,
        *,
        order_id: str | None = None,
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

    def get_taker_delay_s(self, condition_id: str) -> tuple[bool, float | None]:
        """The venue's published taker delay for one market.

        Returns ``(applies, delay_seconds)``:

        * ``applies`` is True when the market is flagged for the hold
          (``itode`` on ``GET /clob-markets/{condition_id}``, docs
          ``site/concepts/order-lifecycle.md``).
        * ``delay_seconds`` is the published ``trading.takerDelayMs`` (the field
          the unified SDK exposes; ``secondsDelay``/``seconds_delay`` are the
          deprecated spellings). ``None`` means the market applies a hold but
          does not publish its duration, so a caller must refuse to model it
          away rather than treat it as zero.

        A failed read is reported as ``(False, None)`` and logged: quoting a
        maker does not depend on it, and pretending a cross had a hold would be
        an invented constant.
        """
        if self.dry_run or not condition_id:
            return False, None
        path = f"/clob-markets/{condition_id}"
        try:
            payload = self._request("GET", path, auth=False)
        except ClobAPIError as exc:
            logger.debug(f"GET {path} failed ({exc}); taker delay unknown")
            return False, None
        if not isinstance(payload, dict):
            return False, None
        applies = bool(payload.get("itode"))
        trading = payload.get("trading")
        if not isinstance(trading, dict):
            trading = payload
        delay_s: float | None = None
        for key in ("takerDelayMs", "taker_delay_ms"):
            value = trading.get(key)
            if value not in (None, ""):
                try:
                    delay_s = float(value) / 1000.0
                except (TypeError, ValueError):
                    delay_s = None
                break
        if delay_s is None:
            for key in ("secondsDelay", "seconds_delay"):
                value = trading.get(key)
                if value not in (None, ""):
                    try:
                        delay_s = float(value)
                    except (TypeError, ValueError):
                        delay_s = None
                    break
        if delay_s is not None and delay_s <= 0.0:
            delay_s = None
        return applies or delay_s is not None, delay_s

    def get_order(self, order_id: str) -> dict[str, Any] | None:
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
        market: str | None = None,
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

        ``tick_size`` and ``fee_rate_bps`` are the values the venue published.
        A body that omits or mangles either one raises instead of substituting
        a default: a 0.01 tick mis-rounds every price on a finer market and a
        0 bps fee prices the taker side free.

        Raises:
            ClobAPIError: on any failed call or non-object body.
            ClobMarketParamsUnreadable: when the payload omits an unparseable
                tick size or fee rate.
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
            tick_size = float(tick_raw) if tick_raw not in (None, "") else None
        except (TypeError, ValueError):
            tick_size = None
        if tick_size is None or tick_size <= 0.0:
            raise ClobMarketParamsUnreadable(
                f"GET {path} published no usable minimum_tick_size "
                f"({tick_raw!r}); refusing to assume {DEFAULT_TICK_SIZE}"
            )

        fee_raw = payload.get("fee_rate_bps")
        if fee_raw is None:
            fee_raw = payload.get("taker_base_fee")
        try:
            fee_rate_bps: int | None = (
                int(float(fee_raw)) if fee_raw not in (None, "") else None
            )
        except (TypeError, ValueError):
            fee_rate_bps = None
        if fee_rate_bps is None:
            raise ClobMarketParamsUnreadable(
                f"GET {path} published no usable fee_rate_bps ({fee_raw!r}); "
                "refusing to sign a zero-fee order"
            )

        # The market's own minimum order notional (USDC). ``None`` when the
        # payload does not publish it: callers keep the refusal rather than
        # sizing against an assumed minimum.
        min_order_raw = payload.get("minimum_order_size")
        if min_order_raw in (None, ""):
            min_order_raw = payload.get("order_min_size")
        try:
            min_order_size = (
                float(min_order_raw) if min_order_raw not in (None, "") else None
            )
        except (TypeError, ValueError):
            min_order_size = None
        if min_order_size is not None and min_order_size <= 0.0:
            min_order_size = None

        end_date_iso = payload.get("end_date_iso")
        return {
            "neg_risk": bool(payload.get("neg_risk", False)),
            "tick_size": tick_size,
            "fee_rate_bps": fee_rate_bps,
            "order_min_size": min_order_size,
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
