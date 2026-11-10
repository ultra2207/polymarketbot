"""Tests for polymarketbot.tools.market_scorecard.

Three layers:

* the pure scoring helpers (edge, Gate 1, fee per share, reward proxy, gas),
  where an unknown must stay ``None`` rather than becoming a zero;
* the markout sign convention and its ASOF matching window, on a one-condition
  DuckDB fixture;
* the whole ``collect`` path (quotes -> trades -> markout -> score) against the
  same fixture, so the SQL and the Python assembly are tested together.

No tape, no network: the fixture is three tiny parquet files in ``tmp_path``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from polymarketbot.tools import market_scorecard

NS = 1_000_000_000
DATE = "2026-11-05"
HOUR_FROM = 3
HOUR_TO = 3
T0 = int(dt.datetime(2026, 11, 5, 3, tzinfo=dt.timezone.utc).timestamp()) * NS

CID_MAIN = "0x" + "ab" * 32
CID_WIDE = "0x" + "cd" * 32
CID_POOL_ONLY = "0x" + "ef" * 32
ASSET_MAIN = "111"
ASSET_WIDE = "222"


@pytest.fixture(scope="module")
def score():
    return market_scorecard


# ------------------------------------------------------------------- fixture
CHANGE = pa.struct([
    ("asset_id", pa.string()),
    ("price", pa.float64()),
    ("size", pa.float64()),
    ("side", pa.string()),
    ("best_bid", pa.float64()),
    ("best_ask", pa.float64()),
])
EVENTS_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()),
    ("condition_id", pa.string()),
    ("asset_id", pa.string()),
    ("event_type", pa.string()),
    ("price_changes", pa.list_(CHANGE)),
    ("best_bid", pa.float64()),
    ("best_ask", pa.float64()),
    ("date", pa.date32()),
    ("hour", pa.string()),
])
TRADES_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()),
    ("condition_id", pa.string()),
    ("asset_id", pa.string()),
    ("price", pa.float64()),
    ("size", pa.float64()),
    ("side", pa.string()),
    ("date", pa.date32()),
    ("hour", pa.string()),
])
GAS_SCHEMA = pa.schema([
    ("recv_ts_ns", pa.int64()),
    ("base_fee_gwei", pa.float64()),
    ("tx_count", pa.int32()),
    ("date", pa.date32()),
    ("hour", pa.string()),
])
SNAPSHOT_SCHEMA = pa.schema([
    ("conditionId", pa.string()),
    ("feeType", pa.string()),
])
DAY = dt.date(2026, 11, 5)
HOUR = f"{HOUR_FROM:02d}"


def _change(asset: str, bid: float, ask: float) -> dict:
    return {
        "asset_id": asset, "price": bid, "size": 100.0, "side": "BUY",
        "best_bid": bid, "best_ask": ask,
    }


def _quote(offset_s: int, asset: str, bid: float, ask: float,
           condition_id: str | None = None, row_asset: bool = True) -> dict:
    """A price_change frame quoting ``asset``."""
    return {
        "recv_ts_ns": T0 + offset_s * NS,
        "condition_id": condition_id,
        "asset_id": asset if row_asset else None,
        "event_type": "price_change",
        "price_changes": [_change(asset, bid, ask)],
        "best_bid": None,
        "best_ask": None,
        "date": DAY,
        "hour": HOUR,
    }


def _trade(offset_s: int, condition_id: str, asset: str, price: float,
           size: float, side: str) -> dict:
    return {
        "recv_ts_ns": T0 + offset_s * NS,
        "condition_id": condition_id,
        "asset_id": asset,
        "price": price,
        "size": size,
        "side": side,
        "date": DAY,
        "hour": HOUR,
    }


def _write(path: Path, rows: list[dict], schema: pa.Schema) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def build_tape(tmp_path: Path, events: list[dict], trades: list[dict],
               snapshot: list[dict] | None = None) -> Path:
    tape = tmp_path / "tape"
    _write(tape / "clob_events" / "part.parquet", events, EVENTS_SCHEMA)
    _write(tape / "clob_trades" / "part.parquet", trades, TRADES_SCHEMA)
    _write(
        tape / "polygon_gas" / "part.parquet",
        [
            {"recv_ts_ns": T0 + i * 10 * NS, "base_fee_gwei": gwei, "tx_count": 1,
             "date": DAY, "hour": HOUR}
            for i, gwei in enumerate((30.0, 40.0, 50.0))
        ],
        GAS_SCHEMA,
    )
    _write(
        tmp_path / "snapshot.parquet",
        snapshot if snapshot is not None else [
            {"conditionId": CID_MAIN, "feeType": "politics_fees"},
            {"conditionId": CID_WIDE, "feeType": "zero_fees"},
        ],
        SNAPSHOT_SCHEMA,
    )
    return tape


@pytest.fixture
def main_tape(tmp_path: Path) -> Path:
    """One narrow (fee-paying) market and one wide (zero-fee) market.

    Asset 111 trades twice; the quote at t+399 s is deliberately one second too
    early for the 300 s horizon of the t+100 s trade, so a nearest-quote or a
    "last quote" implementation would return 0.60 instead of 0.55.
    """
    events = [
        _quote(10, ASSET_MAIN, 0.49, 0.53, condition_id=CID_MAIN),          # mid .51, spread .04
        _quote(20, ASSET_MAIN, 0.48, 0.52, row_asset=False),                # asset_id NULL row
        {  # BBO-only event, no price_changes
            "recv_ts_ns": T0 + 30 * NS, "condition_id": CID_MAIN,
            "asset_id": ASSET_MAIN, "event_type": "best_bid_ask",
            "price_changes": [], "best_bid": 0.485, "best_ask": 0.515,      # mid .50, spread .03
            "date": DAY, "hour": HOUR,
        },
        _quote(399, ASSET_MAIN, 0.59, 0.61),                               # mid .60, too early
        _quote(410, ASSET_MAIN, 0.54, 0.56),                               # mid .55, used
        _quote(600, ASSET_MAIN, 0.89, 0.91),                               # mid .90, ignored
        _quote(15, ASSET_WIDE, 0.40, 0.60, condition_id=CID_WIDE),          # spread .20
        _quote(405, ASSET_WIDE, 0.50, 0.52),                               # mid .51, spread .02
    ]
    trades = [
        _trade(100, CID_MAIN, ASSET_MAIN, 0.50, 100.0, "BUY"),
        _trade(110, CID_MAIN, ASSET_MAIN, 0.52, 100.0, "SELL"),
        _trade(100, CID_WIDE, ASSET_WIDE, 0.50, 10.0, "BUY"),
    ]
    return build_tape(tmp_path, events, trades)


@pytest.fixture
def late_quote_tape(tmp_path: Path) -> Path:
    """The only quote after the horizon arrives 61 s past it."""
    events = [
        _quote(10, ASSET_MAIN, 0.49, 0.53, condition_id=CID_MAIN),
        _quote(461, ASSET_MAIN, 0.60, 0.70),  # 100 + 300 + 61
    ]
    trades = [_trade(100, CID_MAIN, ASSET_MAIN, 0.50, 100.0, "BUY")]
    return build_tape(tmp_path, events, trades)


def _window(score) -> tuple[int, int]:
    return score.window_bounds(DATE, HOUR_FROM, HOUR_TO)


def _prepared(score, tape: Path, tmp_path: Path) -> tuple[duckdb.DuckDBPyConnection, int, int]:
    start, end = _window(score)
    con = duckdb.connect()
    score.load_fee_table(con, tmp_path / "snapshot.parquet",
                         dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc))
    score.load_asset_map(con, tape, start, end)
    score.build_quotes(con, tape, start, end)
    return con, start, end


# --------------------------------------------------------------- pure scoring
def test_half_spread_is_half_the_quote(score):
    assert score.half_spread(0.04) == pytest.approx(0.02)
    assert score.half_spread(None) is None


def test_edge_subtracts_markout_and_fee(score):
    assert score.edge_per_share(0.02, 0.01, 0.004) == pytest.approx(0.006)
    assert score.edge_per_share(0.02, 0.02, 0.004) < 0
    assert score.edge_per_share(0.02, 0.01, 0.0) == pytest.approx(0.01)


def test_edge_is_none_when_any_input_is_missing(score):
    assert score.edge_per_share(None, 0.01, 0.004) is None
    assert score.edge_per_share(0.02, None, 0.004) is None
    assert score.edge_per_share(0.02, 0.01, None) is None


def test_gate1_is_none_without_an_edge(score):
    assert score.gate1_pass(0.000001) is True
    assert score.gate1_pass(0.0) is False
    assert score.gate1_pass(-0.01) is False
    assert score.gate1_pass(None) is None


def test_markout_sign_is_the_takers(score):
    assert score.signed_markout("BUY", 0.50, 0.55) == pytest.approx(0.05)
    assert score.signed_markout("BUY", 0.50, 0.45) == pytest.approx(-0.05)
    assert score.signed_markout("SELL", 0.50, 0.45) == pytest.approx(0.05)
    assert score.signed_markout("SELL", 0.50, 0.55) == pytest.approx(-0.05)


def test_markout_of_unknown_side_or_missing_mid_is_none(score):
    assert score.signed_markout("", 0.50, 0.55) is None
    assert score.signed_markout("BUY", 0.50, None) is None


def test_weighted_markout_weights_by_shares_and_skips_unmatched(score):
    fills = [("BUY", 0.50, 0.55, 300.0), ("SELL", 0.50, 0.45, 100.0),
             ("BUY", 0.50, None, 999.0)]
    value, n = score.weighted_markout(fills)
    assert n == 2
    assert value == pytest.approx((0.05 * 300 + 0.05 * 100) / 400)
    assert score.weighted_markout([]) == (None, 0)


def test_fee_per_share_follows_the_schedule(score):
    linear = score.FeeSchedule(rate=0.04, exponent=1.0)
    crypto = score.FeeSchedule(rate=0.25, exponent=2.0)
    assert score.fee_per_share(0.50, linear) == pytest.approx(0.01)
    assert score.fee_per_share(0.50, crypto) == pytest.approx(0.25 * (0.5 * 0.5) ** 2)
    assert score.fee_per_share(0.50, score.FeeSchedule(rate=0.0)) == 0.0
    assert score.fee_per_share(None, linear) is None


def test_reward_per_capital_is_pool_over_min_capital(score):
    assert score.reward_per_capital(400.0, 200.0, 0.5) == pytest.approx(4.0)
    # a near-zero price cannot produce an infinite ratio
    assert score.reward_per_capital(1.0, 0.0001, 0.01) == pytest.approx(1.0)
    assert score.reward_per_capital(None, 10.0, 0.5) is None
    assert score.reward_per_capital(10.0, None, 0.5) is None
    assert score.reward_per_capital(10.0, 10.0, None) is None


def test_merge_gas_is_units_times_gwei(score):
    assert score.merge_gas_pol(40.0) == pytest.approx(78_000 * 40e-9)
    assert score.merge_gas_pol(None) is None
    assert score.merge_gas_usd(40.0, None) is None  # POL only unless told
    assert score.merge_gas_usd(40.0, 0.50) == pytest.approx(round(78_000 * 40e-9 * 0.5, 6))


def test_window_bounds_span_the_requested_hours(score):
    start, end = score.window_bounds(DATE, 3, 3)
    assert end - start == 3600 * NS
    day_start, day_end = score.window_bounds(DATE, None, None)
    assert day_end - day_start == 24 * 3600 * NS
    five, six = score.window_bounds(DATE, 5, None)
    assert six - five == 2 * 3600 * NS
    assert score.window_bounds(None, 5, None) is None


def test_horizon_label(score):
    assert score.horizon_label(300) == "5m"
    assert score.horizon_label(3600) == "1h"
    assert score.horizon_label(45) == "45s"


# ------------------------------------------------------------- DuckDB fixture
def test_quotes_union_both_event_types_and_map_nested_assets(score, main_tape, tmp_path):
    con, _, _ = _prepared(score, main_tape, tmp_path)
    spreads = score.spread_stats(con)
    row = spreads[CID_MAIN]
    assert row["quote_points"] == 6  # 5 price_change + 1 best_bid_ask
    assert row["n_assets"] == 1
    # spreads .04 .04 .03 .02 .02 .02 -> median of the two middle values
    assert row["spread_median"] == pytest.approx(0.025)
    assert row["mid_median"] == pytest.approx(0.53)
    con.close()


def test_trade_aggregates_and_fee(score, main_tape, tmp_path):
    con, start, end = _prepared(score, main_tape, tmp_path)
    trades, _ = score.trade_aggregates(con, main_tape, start, end)
    row = trades[CID_MAIN]
    assert row["trades"] == 2
    assert row["shares"] == pytest.approx(200.0)
    assert row["notional_usd"] == pytest.approx(102.0)
    assert row["trade_price_median"] == pytest.approx(0.51)
    assert row["fee_type"] == "politics_fees"
    expected = 0.04 * (100 * (0.5 * 0.5) + 100 * (0.52 * 0.48))
    assert row["fee_usd"] == pytest.approx(expected)
    assert row["fee_shares_covered"] == pytest.approx(200.0)
    assert row["fee_per_share"] == pytest.approx(expected / 200.0)

    wide = trades[CID_WIDE]
    assert wide["fee_type"] == "zero_fees"
    assert wide["fee_per_share"] == 0.0  # measured zero, not a missing fee
    con.close()


def test_markout_uses_first_quote_at_the_horizon(score, main_tape, tmp_path):
    con, start, end = _prepared(score, main_tape, tmp_path)
    mk = score.markout(con, main_tape, start, end, 300)
    row = mk[CID_MAIN]
    assert row["markout_trades"] == 2
    assert row["markout_matched"] == 2
    # BUY .50 -> mid .55 (+.05); SELL .52 -> mid .55 (-.03); equal size
    assert row["markout"] == pytest.approx(0.01)
    assert row["markout_median"] == pytest.approx(0.01)
    con.close()


def test_markout_drops_a_quote_more_than_60s_past_the_horizon(score, late_quote_tape, tmp_path):
    con, start, end = _prepared(score, late_quote_tape, tmp_path)
    mk = score.markout(con, late_quote_tape, start, end, 300)
    row = mk[CID_MAIN]
    assert row["markout_trades"] == 1
    assert row["markout_matched"] == 0
    assert row["markout"] is None
    con.close()


def test_gas_uses_the_window_median_base_fee(score, main_tape, tmp_path):
    con, start, end = _prepared(score, main_tape, tmp_path)
    gas = score.gas_stats(con, main_tape, start, end)
    assert gas["blocks"] == 3
    assert gas["median_gwei"] == pytest.approx(40.0)
    assert gas["gas_pol"] == pytest.approx(78_000 * 40e-9)
    con.close()


# ----------------------------------------------------------- end-to-end score
def _manifest(tmp_path: Path) -> Path:
    doc = {
        "count": 3,
        "markets": [
            {"condition_id": CID_MAIN, "question": "narrow", "daily_pool": 400.0,
             "min_size": 200.0, "max_spread": 5.5},
            {"condition_id": CID_WIDE, "question": "wide", "daily_pool": 100.0,
             "min_size": 50.0, "max_spread": 3.0},
            {"condition_id": CID_POOL_ONLY, "question": "pool only", "daily_pool": 25.0,
             "min_size": 10.0, "max_spread": 6.0},
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_collect_scores_every_condition_and_gates_on_edge(score, main_tape, tmp_path):
    con = duckdb.connect()
    start, end = _window(score)
    rows, stats = score.collect(
        con, main_tape, start, end,
        snapshot=tmp_path / "snapshot.parquet",
        manifest=score.load_manifest(_manifest(tmp_path)),
        horizon_s=300,
    )
    con.close()

    by_id = {r["condition_id"]: r for r in rows}
    assert set(by_id) == {CID_MAIN, CID_WIDE, CID_POOL_ONLY}

    narrow = by_id[CID_MAIN]
    assert narrow["half_spread_median"] == pytest.approx(0.0125)
    assert narrow["markout_5m"] == pytest.approx(0.01)
    assert narrow["fee_per_share"] == pytest.approx(0.009992, abs=1e-9)
    assert narrow["edge_per_share"] == pytest.approx(0.0125 - 0.01 - 0.009992, abs=1e-9)
    assert narrow["gate1"] is False
    assert narrow["markout_low_sample"] is True  # 2 matched < 30
    assert narrow["in_manifest"] is True
    assert narrow["question"] == "narrow"
    assert narrow["reward_per_capital"] == pytest.approx(400.0 / (200.0 * 0.51))
    assert narrow["merge_gas_pol"] == pytest.approx(78_000 * 40e-9)
    assert narrow["merge_gas_usd"] is None
    assert narrow["rank_edge"] == 2
    assert narrow["rank_pool"] == 1

    wide = by_id[CID_WIDE]
    assert wide["half_spread_median"] == pytest.approx(0.055)
    assert wide["edge_per_share"] == pytest.approx(0.045)
    assert wide["gate1"] is True
    assert wide["reward_per_capital"] == pytest.approx(100.0 / (50.0 * 0.50))
    assert wide["rank_edge"] == 1
    assert wide["rank_pool"] == 2

    pool_only = by_id[CID_POOL_ONLY]
    assert pool_only["quote_points"] == 0
    assert pool_only["trades"] == 0
    assert pool_only["edge_per_share"] is None
    assert pool_only["gate1"] is None  # never a fabricated FAIL
    assert pool_only["markout_low_sample"] is None
    assert pool_only["reward_per_capital"] is None  # no price to size capital
    assert pool_only["rank_pool"] == 3
    assert pool_only["rank_edge"] is None

    assert stats["conditions"] == 3
    assert stats["conditions_traded"] == 2
    assert stats["conditions_quoted"] == 2
    assert stats["conditions_scored"] == 2
    assert stats["conditions_gate1"] == 1
    assert stats["markout_trades"] == 3
    assert stats["markout_matched"] == 3
    assert stats["trade_rows"] == 3
    assert stats["gas"]["median_gwei"] == pytest.approx(40.0)


def test_render_states_coverage_and_measured_vs_proxy(score, main_tape, tmp_path):
    con = duckdb.connect()
    start, end = _window(score)
    rows, stats = score.collect(
        con, main_tape, start, end,
        snapshot=tmp_path / "snapshot.parquet",
        manifest=score.load_manifest(_manifest(tmp_path)),
    )
    con.close()
    text = score.render(rows, stats, tape=main_tape, top=20)
    assert "2026-11-05T03:00:00Z" in text and "2026-11-05T04:00:00Z" in text
    assert "markout(300s) coverage: 3/3" in text
    assert "measured columns:" in text and "proxy columns" in text
    assert "top 2 by measured edge" in text
    assert "top 3 by daily_pool" in text
    assert "n/a" in text  # the pool-only market never shows a fabricated zero


def test_write_parquet_carries_the_window_header(score, main_tape, tmp_path):
    con = duckdb.connect()
    start, end = _window(score)
    rows, stats = score.collect(
        con, main_tape, start, end,
        snapshot=tmp_path / "snapshot.parquet",
        manifest=score.load_manifest(_manifest(tmp_path)),
    )
    con.close()
    header = score.render(rows, stats, tape=main_tape, top=20)
    out = tmp_path / "market_scorecard.parquet"
    assert score.write_parquet(rows, out, header) == 3

    table = pq.read_table(out)
    assert table.num_rows == 3
    assert set(score.MEASURED_COLUMNS).difference(table.column_names) == set()
    assert set(score.PROXY_COLUMNS).difference(table.column_names) == set()
    meta = table.schema.metadata or {}
    assert b"market_scorecard.header" in meta
    assert b"2026-11-05T03:00:00Z" in meta[b"market_scorecard.header"]
    assert b"reward_per_capital" in meta[b"market_scorecard.proxy_columns"]

    csv_path = tmp_path / "market_scorecard.csv"
    score.write_csv(rows, csv_path)
    assert csv_path.read_text(encoding="utf-8").count("\n") == 4  # header + 3 rows
