from __future__ import annotations

import math
from datetime import datetime, timezone

import pyarrow as pa
import pytest

from polymarketbot.data.schema import DERIBIT_SURFACE
from polymarketbot.data.streams import deribit as d

NOW_MS = int(datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
SPOT = 100_000.0
STRIKES = (70_000, 80_000, 85_000, 90_000, 95_000, 100_000, 105_000, 110_000, 115_000, 120_000, 130_000)
# Expiry tokens chosen so the buckets land on 7 / 14 / 30 days after NOW_MS.
EXPIRY_TOKENS = ("11NOV26", "18NOV26", "4DEC26")


def _iv(strike: float) -> float:
    """A skewed surface: IV falls as the strike rises above spot."""
    return 0.5 - 0.10 * math.log(strike / SPOT)


def _chain(tokens=EXPIRY_TOKENS, strikes=STRIKES) -> list[dict]:
    rows = []
    for token in tokens:
        for strike in strikes:
            iv = _iv(strike)
            for kind in ("C", "P"):
                rows.append(
                    {
                        "instrument_name": f"BTC-{token}-{strike}-{kind}",
                        "mark_iv": iv * 100.0,
                        "underlying_price": SPOT,
                        "mid_price": 0.01,
                        "open_interest": 10.0,
                    }
                )
    return rows


def test_parse_instrument():
    info = d.parse_instrument("BTC-13NOV26-83000-P")
    assert info["strike"] == 83000.0
    assert info["is_call"] is False
    assert info["expiry_ms"] == int(
        datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert d.parse_instrument("BTC-4NOV26-100000-C")["is_call"] is True
    assert d.parse_instrument("BTC-PERPETUAL") is None
    assert d.parse_instrument("ETH-13NOV26-abc-C") is None
    assert d.parse_instrument("BTC-13XXX26-1000-C") is None


def test_expiry_is_0800_utc():
    expiry = d.parse_expiry_ms("13NOV26")
    moment = datetime.fromtimestamp(expiry / 1000, timezone.utc)
    assert (moment.year, moment.month, moment.day, moment.hour) == (2026, 11, 13, 8)


def test_bs_delta_monotonic_in_strike():
    t_years = 30.0 / 365.0
    vol = 0.5
    call_deltas = [d.bs_delta(SPOT, k, t_years, vol, True) for k in STRIKES]
    put_deltas = [d.bs_delta(SPOT, k, t_years, vol, False) for k in STRIKES]
    assert all(a > b for a, b in zip(call_deltas, call_deltas[1:])), call_deltas
    assert all(a > b for a, b in zip(put_deltas, put_deltas[1:])), put_deltas
    assert call_deltas[0] > 0.99 and call_deltas[-1] < 0.1
    assert put_deltas[0] > -0.1 and put_deltas[-1] < -0.9
    # put delta == call delta - 1 at every strike (r = 0)
    for call, put in zip(call_deltas, put_deltas):
        assert put == pytest.approx(call - 1.0)


def test_bs_delta_degenerate_inputs():
    assert d.bs_delta(0.0, 100.0, 0.1, 0.5, True) is None
    assert d.bs_delta(100.0, 0.0, 0.1, 0.5, True) is None
    assert d.bs_delta(100.0, 100.0, 0.0, 0.5, True) is None
    assert d.bs_delta(100.0, 100.0, 0.1, 0.0, True) is None


def test_build_surface_terms_and_skew():
    row = d.build_surface("btc_usd", SPOT, _chain(), 0.55, NOW_MS, 11, 22)

    assert row["index_name"] == "btc_usd"
    assert row["venue_ts_ms"] == NOW_MS
    assert row["recv_ts_ns"] == 11 and row["recv_mono_ns"] == 22
    assert row["dvol"] == 0.55

    # ATM strike is exactly spot and the IV there is 0.50 on every expiry.
    assert row["atm_iv"] == pytest.approx(0.50)
    assert row["term_7d"] == pytest.approx(0.50)
    assert row["term_14d"] == pytest.approx(0.50)
    assert row["term_30d"] == pytest.approx(0.50)

    # Downward-sloping call wing / upward-sloping put wing.
    assert row["skew_25d_call"] < 0 < row["skew_25d_put"]
    assert row["skew_25d_call"] == pytest.approx(-0.009531, abs=1e-6)
    assert row["skew_25d_put"] == pytest.approx(0.010536, abs=1e-6)

    summary = pa.Table.from_pylist([row], schema=DERIBIT_SURFACE)
    assert summary.num_rows == 1
    import orjson

    raw = orjson.loads(row["raw_json"])
    assert raw["anchor_days"] == 30
    assert [b["target"] for b in raw["buckets"]] == [7, 14, 30]
    assert all(b["n"] == 22 for b in raw["buckets"])


def test_missing_bucket_leaves_fields_null():
    row = d.build_surface("eth_usd", SPOT, _chain(tokens=("11NOV26",)), 0.4, NOW_MS, 1, 2)

    assert row["term_7d"] == pytest.approx(0.50)
    assert row["term_14d"] is None
    assert row["term_30d"] is None
    # No 30d bucket: the anchor falls back to the nearest valid bucket (7d).
    assert row["atm_iv"] == pytest.approx(0.50)
    assert row["skew_25d_call"] == pytest.approx(-0.004879, abs=1e-6)


def test_empty_chain_leaves_everything_null():
    row = d.build_surface("btc_usd", SPOT, [], None, NOW_MS, 1, 2)
    assert row["dvol"] is None
    assert row["atm_iv"] is None
    assert row["skew_25d_call"] is None
    assert row["skew_25d_put"] is None
    assert row["term_7d"] is None
    assert row["term_14d"] is None
    assert row["term_30d"] is None
    assert row["index_name"] == "btc_usd"


def test_unusable_options_are_skipped():
    rows = _chain(tokens=("11NOV26",))
    rows.append({"instrument_name": "BTC-11NOV26-100000-C", "mark_iv": 0.0})  # zero IV
    rows.append({"instrument_name": "BTC-PERPETUAL", "mark_iv": 55.0})  # not an option
    rows.append({"instrument_name": "BTC-3NOV26-100000-C", "mark_iv": 55.0})  # already expired
    options = d.parse_chain(rows, NOW_MS)
    assert len(options) == 22
    assert all(option["iv"] > 0 for option in options)


def test_parse_dvol_uses_last_close():
    payload = {
        "result": {
            "data": [
                [1793747700000, 39.77, 39.77, 39.75, 39.75],
                [1793747760000, 39.75, 39.75, 39.74, 39.80],
            ],
            "continuation": None,
        }
    }
    assert d.parse_dvol(payload) == pytest.approx(0.398)
    assert d.parse_dvol({"result": {"data": []}}) is None
    assert d.parse_dvol({"result": {}}) is None
    assert d.parse_dvol({}) is None


def test_parse_index_price():
    payload = {
        "jsonrpc": "2.0",
        "result": {"estimated_delivery_price": 77238.09, "index_price": 77238.09},
    }
    assert d.parse_index_price(payload) == pytest.approx(77238.09)
    assert d.parse_index_price({"result": {"index_price": None}}) is None
    assert d.parse_index_price({"error": {"code": 1}}) is None


def test_parse_buckets():
    assert d.parse_buckets(["7d", "14d", "30d"]) == (7, 14, 30)
    assert d.parse_buckets([7, 14, 30]) == (7, 14, 30)
    assert d.parse_buckets(None) == (7, 14, 30)
    assert d.parse_buckets(["90d", "7d"]) == (7, 90)
    assert d.parse_buckets(["junk"]) == (7, 14, 30)
