"""Minute-partition parquet buffers: durability, quarantine, atomic replace."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from polymarketbot.tools.tape_buffer import (
    MinuteParquetBuffer,
    flush_frame,
    minute_part,
)

COLUMNS = ["recv_ts_ns", "asset_id", "price"]
DTYPES = {"recv_ts_ns": "int64", "price": "float64"}
# 2026-11-11 10:07:30 UTC -> minute 07
NOW = datetime(2026, 11, 11, 10, 7, 30, tzinfo=UTC).timestamp()


def _buffer(tmp_path) -> MinuteParquetBuffer:
    buf = MinuteParquetBuffer(tmp_path, COLUMNS, DTYPES)
    buf.set_meta({"tok": {"asset_id": "tok"}})
    return buf


def test_flush_writes_one_part_per_minute(tmp_path):
    buf = _buffer(tmp_path)
    assert buf.add("tok", {"recv_ts_ns": 1, "price": 0.5})
    assert buf.flush(now=NOW) == 1
    part = minute_part(tmp_path, NOW)
    assert part.name == "minute=07.parquet"
    assert part.parent.name == "hour=10"
    assert pd.read_parquet(part)["price"].tolist() == [0.5]
    assert buf.rows == []


def test_unknown_asset_rows_are_dropped(tmp_path):
    buf = _buffer(tmp_path)
    assert buf.add("other", {"recv_ts_ns": 1, "price": 0.5}) is False
    assert buf.flush(now=NOW) == 0


def test_second_flush_appends_within_the_same_minute(tmp_path):
    buf = _buffer(tmp_path)
    buf.add("tok", {"recv_ts_ns": 1, "price": 0.5})
    buf.flush(now=NOW)
    buf.add("tok", {"recv_ts_ns": 2, "price": 0.6})
    buf.flush(now=NOW + 5)
    frame = pd.read_parquet(minute_part(tmp_path, NOW))
    assert frame["recv_ts_ns"].tolist() == [1, 2]
    assert buf.written == 2


def test_failed_write_keeps_the_batch(tmp_path, monkeypatch):
    buf = _buffer(tmp_path)
    buf.add("tok", {"recv_ts_ns": 1, "price": 0.5})

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("polymarketbot.tools.tape_buffer.flush_frame", boom)
    with pytest.raises(OSError):
        buf.flush(now=NOW)
    # The rows are still buffered, so the next flush persists them.
    assert [row["recv_ts_ns"] for row in buf.rows] == [1]
    assert buf.written == 0
    monkeypatch.undo()
    assert buf.flush(now=NOW) == 1
    assert pd.read_parquet(minute_part(tmp_path, NOW))["recv_ts_ns"].tolist() == [1]


def test_corrupt_part_is_quarantined_not_overwritten(tmp_path):
    part = minute_part(tmp_path, NOW)
    part.write_bytes(b"not a parquet file")
    frame = pd.DataFrame([{"recv_ts_ns": 2, "asset_id": "tok", "price": 0.7}])
    flush_frame(frame, part)
    # Quarantined evidence must not still match the readers' `*.parquet` glob.
    quarantined = [p for p in part.parent.iterdir() if ".corrupt-" in p.name]
    assert len(quarantined) == 1
    assert quarantined[0].suffix != ".parquet"
    assert quarantined[0].read_bytes() == b"not a parquet file"
    assert pd.read_parquet(part)["recv_ts_ns"].tolist() == [2]


def test_no_temporary_file_survives_a_flush(tmp_path):
    frame = pd.DataFrame([{"recv_ts_ns": 1, "asset_id": "tok", "price": 0.5}])
    part = minute_part(tmp_path, NOW)
    flush_frame(frame, part)
    # A hidden ".minute=07.tmp" would be picked up by a `*.parquet` glob on a
    # crash; the replace means there is nothing left behind at all.
    assert [p.name for p in part.parent.iterdir()] == [part.name]
