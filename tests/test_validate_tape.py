"""Tape validator tests: synthetic Parquet, replay state, summary aggregates."""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarketbot.data.schema import CLOB_EVENTS  # noqa: E402
from scripts.validate_tape import (  # noqa: E402
    BookState,
    compare_books,
    hour_parquet_parts,
    load_events,
    parquet_parts,
    replay,
    summarize,
)

ASSET = "107505882767731489358349912513945399560393482969656700824895970500493757150417"
OTHER_ASSET = "999"
HOUR = "20261104T12"


def _row(recv_ns: int, asset: str, event_type: str, **extra):
    row = {
        "recv_ts_ns": recv_ns,
        "recv_mono_ns": recv_ns,
        "asset_id": asset,
        "event_type": event_type,
    }
    row.update(extra)
    return row


def _write_tape(root: Path, rows: list[dict]) -> Path:
    part_dir = root / "clob_events" / "date=2026-11-04" / "hour=12"
    part_dir.mkdir(parents=True)
    path = part_dir / "part-test.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=CLOB_EVENTS), path)
    return path


@pytest.fixture()
def tape(tmp_path):
    rows = [
        _row(
            100,
            ASSET,
            "book",
            bids=[{"price": 0.40, "size": 10.0}, {"price": 0.38, "size": 5.0}],
            asks=[{"price": 0.42, "size": 7.0}, {"price": 0.45, "size": 3.0}],
        ),
        _row(
            200,
            ASSET,
            "price_change",
            price_changes=[
                {"price": 0.42, "size": 0.0, "side": "SELL"},
                {"price": 0.38, "size": 0.0, "side": "BUY"},
                {"price": 0.41, "size": 4.0, "side": "BUY"},
            ],
        ),
        _row(300, ASSET, "gap"),
        _row(
            400,
            OTHER_ASSET,
            "book",
            bids=[{"price": 0.10, "size": 1.0}],
            asks=[{"price": 0.90, "size": 1.0}],
        ),
    ]
    _write_tape(tmp_path, rows)
    return tmp_path


def test_hour_and_part_discovery(tape):
    assert len(parquet_parts(tape, "clob_events")) == 1
    assert len(hour_parquet_parts(tape, "clob_events", HOUR)) == 1
    assert hour_parquet_parts(tape, "clob_events", "20261104T13") == []
    with pytest.raises(ValueError):
        hour_parquet_parts(tape, "clob_events", "2026-11-04T12")


def test_summary_counts_parts_rows_assets_range_and_gaps(tape):
    con = duckdb.connect()
    try:
        rows = {row["table"]: row for row in summarize(con, tape)}
    finally:
        con.close()

    assert set(rows) == {"clob_events"}
    summary = rows["clob_events"]
    assert summary["parts"] == 1
    assert summary["rows"] == 4
    assert summary["assets"] == 2
    assert summary["min_ts"] == 100
    assert summary["max_ts"] == 400
    assert summary["gaps"] == 1


def test_replay_one_asset_applies_book_and_removes_levels(tape):
    con = duckdb.connect()
    try:
        events = load_events(con, tape, asset=ASSET, hour=HOUR, limit=1000)
    finally:
        con.close()

    assert len(events) == 3
    assert [event["event_type"] for event in events] == ["book", "price_change", "gap"]

    state = replay(events)
    assert state.snapshots == 1
    assert state.gaps == 1
    assert state.changes_applied == 3
    assert state.best_bid == pytest.approx(0.41)
    assert state.best_ask == pytest.approx(0.45)
    assert set(state.bids) == {0.41, 0.40}
    assert set(state.asks) == {0.45}


def test_replay_limit_and_asset_filter(tape):
    con = duckdb.connect()
    try:
        only_two = load_events(con, tape, asset=ASSET, hour=HOUR, limit=2)
        other = load_events(con, tape, asset=OTHER_ASSET, hour=HOUR, limit=10)
    finally:
        con.close()

    assert len(only_two) == 2
    assert all(row["asset_id"] == ASSET for row in only_two)
    assert len(other) == 1
    assert replay(other).best_bid == pytest.approx(0.10)


def test_replay_ignores_complement_token_changes(tmp_path):
    rows = [
        _row(
            100,
            ASSET,
            "book",
            bids=[{"price": 0.40, "size": 1.0}],
            asks=[{"price": 0.45, "size": 1.0}],
        ),
        _row(
            200,
            None,
            "price_change",
            price_changes=[
                {"asset_id": ASSET, "price": 0.40, "size": 2.0, "side": "BUY"},
                {"asset_id": OTHER_ASSET, "price": 0.50, "size": 9.0, "side": "BUY"},
            ],
        ),
    ]
    _write_tape(tmp_path, rows)
    con = duckdb.connect()
    events = load_events(con, tmp_path, asset=ASSET, hour=HOUR)
    assert len(events) == 2
    state = replay(events, ASSET)
    assert state.best_bid == 0.40
    assert state.changes_applied == 1
    assert state.changes_other_asset == 1
    con.close()


def test_missing_hour_is_a_clear_error(tape):
    con = duckdb.connect()
    try:
        with pytest.raises(FileNotFoundError, match="20261104T13"):
            load_events(con, tape, asset=ASSET, hour="20261104T13")
    finally:
        con.close()


def test_compare_books_reports_mismatch_without_raising():
    state = BookState()
    state.apply_book([{"price": 0.40, "size": 1.0}], [{"price": 0.45, "size": 1.0}])
    findings = compare_books(
        state,
        {"bids": [{"price": 0.39, "size": 1.0}], "asks": [{"price": 0.45, "size": 1.0}]},
    )
    assert any("best_bid" in finding for finding in findings)
    assert not any("best_ask" in finding for finding in findings)
    assert compare_books(state, {"bids": [{"price": 0.40, "size": 1.0}], "asks": [{"price": 0.45, "size": 1.0}]}) == []
