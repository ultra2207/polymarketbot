"""Tests for wallet archetype classification."""

from __future__ import annotations

import pytest

from polymarketbot.wallets import classify_archetype


@pytest.mark.parametrize("row,expected", [
    # pure directional: no rebates, no LP income
    ({"realized_market_pnl": 1000, "maker_rebate": 0, "realized_lp_pnl": 0}, "directional"),
    # material maker rebate relative to directional PnL
    ({"realized_market_pnl": 1000, "maker_rebate": 200}, "maker"),
    # rebate is real but immaterial
    ({"realized_market_pnl": 100_000, "maker_rebate": 50}, "directional"),
    # LP income dominates
    ({"realized_market_pnl": 100, "realized_lp_pnl": 500}, "liquidity_provider"),
    # combo PnL dominates
    ({"realized_market_pnl": 10, "realized_combo_pnl": 900}, "combo"),
    # nothing at all
    ({}, "unknown"),
    # negative directional PnL still counts as directional activity
    ({"realized_market_pnl": -500}, "directional"),
])
def test_classify_archetype(row, expected):
    assert classify_archetype(row) == expected


def test_none_values_are_treated_as_absent():
    row = {"realized_market_pnl": None, "maker_rebate": None, "realized_lp_pnl": None}
    assert classify_archetype(row) == "unknown"
