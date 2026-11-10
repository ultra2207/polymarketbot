"""Tick-size rounding, price snapping and quantity normalisation."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from polymarketbot.strategy.venue.clob.common import COLLATERAL_DECIMALS, ClobAPIError


"""Per-tick rounding (price dp, size dp, amount dp) from py-clob-client's ROUNDING_CONFIG.

Size is always 2 dp; the collateral/share amount is carried to price_dp + size_dp,
which is why no further product rounding is required.
"""
ROUNDING_CONFIG: dict[float, tuple[int, int, int]] = {
    0.1: (1, 2, 3),
    0.01: (2, 2, 4),
    0.005: (3, 2, 5),
    0.0025: (4, 2, 6),
    0.001: (3, 2, 5),
    0.0001: (4, 2, 6),
}
_TICK_TOLERANCE = 1e-9


def rounding_config(tick_size: float) -> tuple[int, int, int]:
    """Return (price dp, size dp, amount dp) for a market tick.

    Raises:
        ValueError: for a tick that is not in the supported table. The old code
            silently rounded to 3 dp; a wrong tick means a rejected order.
    """
    tick = float(tick_size)
    for known, config in ROUNDING_CONFIG.items():
        if abs(tick - known) <= _TICK_TOLERANCE:
            return config
    raise ValueError(f"unsupported tick size {tick_size!r}")


def _quantize(value: Decimal, places: int, rounding: str) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=rounding)


def round_price_to_tick(price: float, tick_size: float) -> Decimal:
    """Snap ``price`` to the nearest tick, returning an exact Decimal."""
    config = rounding_config(tick_size)
    tick = Decimal(str(tick_size))
    steps = (_quantize(Decimal(str(price)) / tick, 0, ROUND_HALF_UP))
    return (_quantize(steps * tick, config[0], ROUND_HALF_UP))


def compute_order_amounts(
    side: str,
    size: float,
    price: float,
    tick_size: float,
) -> tuple[int, int]:
    """Return (makerAmount, takerAmount) in 6-decimal raw units.

    Mirrors py-clob-client ``OrderBuilder.get_order_amounts``:
    size is floored to 2 dp, price is rounded to the tick's price precision,
    and the product is carried to the tick's amount precision.

    BUY:  makerAmount = shares * price (collateral), takerAmount = shares
    SELL: makerAmount = shares,                    takerAmount = shares * price
    """
    price_dp, size_dp, amount_dp = rounding_config(tick_size)
    side_norm = str(side).upper()
    if side_norm not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")

    raw_price = _quantize(Decimal(str(price)), price_dp, ROUND_HALF_UP)
    raw_size = _quantize(Decimal(str(size)), size_dp, ROUND_DOWN)
    if raw_size <= 0:
        raise ValueError(f"size {size} floors to 0 at {size_dp} dp")
    raw_product = _quantize(raw_size * raw_price, amount_dp, ROUND_DOWN)

    scale = Decimal(10**COLLATERAL_DECIMALS)
    shares_raw = int(raw_size * scale)
    product_raw = int(raw_product * scale)
    if side_norm == "BUY":
        return product_raw, shares_raw
    return shares_raw, product_raw


class TicksMixin:
    """Cached market parameter lookups (tick size, negative risk)."""


    def get_tick_size(self, token_id: str, *, refresh: bool = False) -> float:
        """Fetch (and cache) the market tick size from ``GET /tick-size``.

        Raises:
            ClobAPIError: on a failed call, a missing amount, or a tick this bot
                has no rounding table for.
        """
        key = str(token_id)
        if not refresh:
            cached = self._tick_size_cache.get(key)
            if cached is not None:
                return cached

        path = "/tick-size"
        payload = self._request("GET", path, params={"token_id": key}, auth=False)
        raw = None
        if isinstance(payload, dict):
            raw = payload.get("minimum_tick_size")
            if raw in (None, ""):
                raw = payload.get("tick_size")
        try:
            tick = float(raw)
        except (TypeError, ValueError) as e:
            raise ClobAPIError(
                f"GET {path} returned an unexpected body: {payload!r}",
                status_code=200,
                path=path,
            ) from e
        try:
            rounding_config(tick)
        except ValueError as e:
            raise ClobAPIError(
                f"GET {path} returned an unsupported tick: {e}",
                status_code=200,
                path=path,
            ) from e
        self._tick_size_cache[key] = tick
        return tick

    def get_neg_risk(self, token_id: str, *, refresh: bool = False) -> bool:
        """Fetch (and cache) the negative-risk flag from ``GET /neg-risk``.

        Raises:
            ClobAPIError: on a failed call or a missing/malformed flag.
        """
        key = str(token_id)
        if not refresh and key in self._neg_risk_cache:
            return self._neg_risk_cache[key]

        path = "/neg-risk"
        payload = self._request("GET", path, params={"token_id": key}, auth=False)
        raw = payload.get("neg_risk") if isinstance(payload, dict) else None
        if isinstance(raw, bool):
            flag = raw
        elif isinstance(raw, str):
            flag = raw.strip().lower() in ("true", "1", "yes")
        elif isinstance(raw, (int, float)):
            flag = bool(raw)
        else:
            raise ClobAPIError(
                f"GET {path} returned an unexpected body: {payload!r}",
                status_code=200,
                path=path,
            )
        self._neg_risk_cache[key] = flag
        return flag
