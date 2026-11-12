"""Tape reader: parts written by different recorders have different column types."""

from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from polymarketbot.tools.tape_reader import TapeSchemaError, read_tape, tape_files


def _part(root, date: str, hour: str, minute: str, table: pa.Table):
    dest = root / f"date={date}" / f"hour={hour}"
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"minute={minute}.parquet"
    pq.write_table(table, path)
    return path


def _basic(**overrides) -> pa.Table:
    data = {
        "recv_ts_ns": pa.array([1, 2], pa.int64()),
        "asset_id": pa.array(["tok", "tok"], pa.large_string()),
        "price": pa.array([0.5, 0.6], pa.float64()),
        "date": pa.array([dt.date(2026, 11, 11)] * 2, pa.date32()),
        "hour": pa.array([6, 6], pa.int64()),
    }
    data.update(overrides)
    return pa.table(data)


def test_reads_parts_whose_null_typed_columns_disagree(tmp_path):
    """The real tape has fee_rate_bps as double in one part and null in another."""
    _part(tmp_path, "2026-11-11", "06", "00", _basic(fee_rate_bps=pa.array([None, None], pa.null())))
    _part(tmp_path, "2026-11-11", "06", "01", _basic(fee_rate_bps=pa.array([1.0, 2.0], pa.float64())))

    # The naive reader (and pd.read_parquet) refuses the tree: the hive column
    # type conflict or the null-vs-double cast, depending on what it hits first.
    with pytest.raises((pa.ArrowTypeError, pa.ArrowNotImplementedError)):
        pq.read_table(tmp_path)

    table = read_tape(tmp_path)
    assert table.num_rows == 4
    assert table.schema.field("fee_rate_bps").type == pa.float64()
    assert table.column("fee_rate_bps").to_pylist() == [None, None, 1.0, 2.0]


def test_widens_integer_and_string_widths(tmp_path):
    _part(tmp_path, "2026-11-11", "06", "00",
          _basic(shard=pa.array([0, 1], pa.int16()), tx=pa.array(["a", "b"], pa.string())))
    _part(tmp_path, "2026-11-11", "06", "01",
          _basic(shard=pa.array([2, 3], pa.int64()), tx=pa.array(["c", "d"], pa.large_string())))

    table = read_tape(tmp_path)
    assert table.schema.field("shard").type == pa.int64()
    assert table.schema.field("tx").type == pa.large_string()
    assert table.column("shard").to_pylist() == [0, 1, 2, 3]


def test_partition_columns_come_from_the_files_not_the_directory(tmp_path):
    """`date=`/`hour=` in the path must not be re-typed as dictionary<string>."""
    _part(tmp_path, "2026-11-11", "06", "00", _basic())
    table = read_tape(tmp_path)
    assert table.schema.field("date").type == pa.date32()
    assert table.column("date").to_pylist() == [dt.date(2026, 11, 11)] * 2


def test_missing_column_is_typed_null(tmp_path):
    _part(tmp_path, "2026-11-11", "06", "00", _basic())
    _part(tmp_path, "2026-11-11", "06", "01",
          _basic().drop_columns(["price"]))

    table = read_tape(tmp_path, columns=["asset_id", "price"])
    assert table.column("price").to_pylist() == [0.5, 0.6, None, None]
    assert table.num_rows == 4


def test_irreconcilable_types_name_the_column_and_a_part(tmp_path):
    _part(tmp_path, "2026-11-11", "06", "00", _basic(price=pa.array([1, 2], pa.int64())))
    _part(tmp_path, "2026-11-11", "06", "01", _basic())
    with pytest.raises(TapeSchemaError) as excinfo:
        read_tape(tmp_path)
    message = str(excinfo.value)
    assert "price" in message and "minute=" in message


def test_empty_tape_is_a_refusal(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_tape(tmp_path)


def test_tape_files_filters_by_hour(tmp_path):
    _part(tmp_path, "2026-11-11", "06", "00", _basic())
    _part(tmp_path, "2026-11-11", "07", "00", _basic())
    assert len(tape_files(tmp_path)) == 2
    assert [p.parent.name for p in tape_files(tmp_path, hours=["07"])] == ["hour=07"]
