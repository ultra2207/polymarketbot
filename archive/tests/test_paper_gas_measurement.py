"""Gas for the paper soak is measured from the shared path, or refused.

The soak used to carry its own oracle lookup, which read ``fast.maxFee`` - a
*certificate* of headroom, not the price - and fell back to
``fast.maxPriorityFee``, which is the tip alone and drops the base fee entirely.
Those two produced 381.34 Gwei and 25 Gwei respectively against a measured
236.58 Gwei, so paper and live priced gas 1.6x apart (and up to 9x apart on the
fallback). Both now read the one measurement.

The contract these tests pin: the price is base + tip from the chain, an
unmeasured input is ``None`` and never ``0.0``, and neither the cap nor the tip
alone is ever substituted for the price.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from polymarketbot.paper.main import GAS_UNITS_PER_ONCHAIN_ACTION, measure_gas_usd


def _chain(base_gwei: float, tip_gwei: float) -> MagicMock:
    chain = MagicMock()
    chain._rpc.side_effect = lambda method, params: (
        {"baseFeePerGas": hex(int(base_gwei * 1e9))}
        if method == "eth_getBlockByNumber"
        else hex(int(tip_gwei * 1e9))
        if method == "eth_maxPriorityFeePerGas"
        else None
    )
    return chain


def test_price_is_base_plus_tip_not_the_oracle_cap(tmp_path, monkeypatch):
    """The price is what the chain charges, not the oracle's fee cap."""
    from polymarketbot.strategy.onchain.ctf import gas as _gas

    monkeypatch.setattr(_gas, "GAS_STATE_PATH", tmp_path / "gas.json")
    monkeypatch.setattr(_gas, "POL_STATE_PATH", tmp_path / "pol.json")
    # The oracle publishes a 381 Gwei cap with headroom; it must not be used
    # while an RPC measurement is available.
    monkeypatch.setattr(
        _gas, "_oracle_samples", lambda: {"oracle:fast.maxFee": 381.0, "oracle:fast.maxPriorityFee": 25.0}
    )
    monkeypatch.setattr(_gas, "get_dynamic_pol_usd_price", lambda **_: 0.09)

    usd = measure_gas_usd(chain_client=_chain(base_gwei=200.0, tip_gwei=25.0))

    assert usd == pytest.approx(round(GAS_UNITS_PER_ONCHAIN_ACTION * 225.0 * 1e-9 * 0.09, 6))


def test_tip_alone_is_never_used_as_the_price(tmp_path, monkeypatch):
    """A missing base fee is not an invitation to price at the tip (25 Gwei)."""
    from polymarketbot.strategy.onchain.ctf import gas as _gas

    monkeypatch.setattr(_gas, "GAS_STATE_PATH", tmp_path / "gas.json")
    monkeypatch.setattr(_gas, "POL_STATE_PATH", tmp_path / "pol.json")
    monkeypatch.setattr(
        _gas, "_oracle_samples", lambda: {"oracle:fast.maxPriorityFee": 25.0}
    )
    monkeypatch.setattr(_gas, "get_dynamic_pol_usd_price", lambda **_: 0.09)

    chain = MagicMock()
    chain._rpc.side_effect = RuntimeError("no base fee, no gas price")

    assert measure_gas_usd(chain_client=chain) is None


def test_unmeasured_pol_is_a_refusal_not_zero(tmp_path, monkeypatch):
    """An unpriced action must not be treated as free."""
    from polymarketbot.strategy.onchain.ctf import gas as _gas

    monkeypatch.setattr(_gas, "GAS_STATE_PATH", tmp_path / "gas.json")
    monkeypatch.setattr(_gas, "POL_STATE_PATH", tmp_path / "pol.json")
    monkeypatch.setattr(_gas, "_oracle_samples", dict)
    monkeypatch.setattr(_gas, "get_dynamic_pol_usd_price", lambda **_: None)

    assert measure_gas_usd(chain_client=_chain(base_gwei=200.0, tip_gwei=25.0)) is None
