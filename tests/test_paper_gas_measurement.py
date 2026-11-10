"""Gas cost for the paper soak is measured, and its absence is a refusal.

The soak used to require an operator to pass ``--gas-usd``; without it CtfMinter
and on-chain settlement were silently benched. The measurement must price from
live inputs and return ``None`` (never ``0.0``) when an input is missing.
"""

from __future__ import annotations

from polymarketbot.paper.main import (
    GAS_ORACLE,
    GAS_UNITS_PER_ONCHAIN_ACTION,
    POL_PRICE_APIS,
    measure_gas_usd,
)


class _Response:
    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self):
        return self._payload


class _Session:
    """Routes by URL; a missing route behaves like a network failure."""

    def __init__(self, routes: dict[str, _Response]) -> None:
        self.routes = routes

    def get(self, url: str, timeout: float | None = None) -> _Response:
        return self.routes.get(url, _Response(error=RuntimeError(f"no route {url}")))


def test_measured_cost_uses_gas_oracle_and_live_pol_price() -> None:
    http = _Session(
        {
            GAS_ORACLE: _Response({"fast": {"maxFee": 30.0, "maxPriorityFee": 25.0}}),
            POL_PRICE_APIS[0]: _Response({"price": "0.4"}),
        }
    )
    usd = measure_gas_usd(http)
    expected = round(GAS_UNITS_PER_ONCHAIN_ACTION * 30.0 * 1e-9 * 0.4, 6)
    assert usd == expected


def test_priority_fee_is_used_when_max_fee_is_absent() -> None:
    http = _Session(
        {
            GAS_ORACLE: _Response({"fast": {"maxPriorityFee": 25.0}}),
            POL_PRICE_APIS[0]: _Response({"price": 1.0}),
        }
    )
    assert measure_gas_usd(http) == round(GAS_UNITS_PER_ONCHAIN_ACTION * 25.0 * 1e-9, 6)


def test_pol_price_falls_back_to_the_legacy_symbol() -> None:
    http = _Session(
        {
            GAS_ORACLE: _Response({"fast": {"maxFee": 30.0}}),
            POL_PRICE_APIS[1]: _Response({"price": "0.5"}),
        }
    )
    assert measure_gas_usd(http) == round(GAS_UNITS_PER_ONCHAIN_ACTION * 30.0 * 1e-9 * 0.5, 6)


def test_missing_price_is_a_refusal_not_zero() -> None:
    http = _Session({GAS_ORACLE: _Response({"fast": {"maxFee": 30.0}})})
    assert measure_gas_usd(http) is None


def test_missing_gas_price_is_a_refusal_not_zero(monkeypatch) -> None:
    import polymarketbot.chain as chain

    class _NoRpc:
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("no RPC configured")

    monkeypatch.setattr(chain, "ChainClient", _NoRpc)
    http = _Session({POL_PRICE_APIS[0]: _Response({"price": "0.4"})})
    assert measure_gas_usd(http) is None
