from __future__ import annotations

import json
import time

import pyarrow.parquet as pq

from polymarketbot.data.schema import HEALTH_METRICS
from polymarketbot.data.writer import WriterHub


def test_writer_compacts_to_hourly_parquet(tmp_path):
    hub = WriterHub(tmp_path, flush_seconds=5, queue_max_rows=1000)
    hub.register("health_metrics", HEALTH_METRICS)
    for i in range(3):
        hub.emit(
            "health_metrics",
            {
                "recv_ts_ns": time.time_ns(),
                "stream": "test",
                "metric": "rows",
                "value": float(i),
                "labels_json": None,
            },
        )
    hub.close()

    parts = list((tmp_path / "health_metrics").glob("date=*/hour=*/*.parquet"))
    assert parts, "no parquet part written"
    rows = []
    for part in parts:
        rows.extend(pq.read_table(part).to_pylist())
    assert len(rows) == 3
    assert sorted(r["value"] for r in rows) == [0.0, 1.0, 2.0]
    assert not list((tmp_path / "_wal").glob("**/*.jsonl"))


def test_writer_rotates_parts(tmp_path):
    hub = WriterHub(tmp_path, flush_seconds=5, part_seconds=60)
    hub.register("health_metrics", HEALTH_METRICS)
    row = {
        "recv_ts_ns": time.time_ns(),
        "stream": "test",
        "metric": "rows",
        "value": 0.0,
        "labels_json": None,
    }
    first = hub._file_for("health_metrics")
    first.write(json.dumps(row).encode() + b"\n")
    first.flush()
    hub._part_started["health_metrics"] -= 120.0
    second = hub._file_for("health_metrics")
    assert first is not second
    row["value"] = 1.0
    second.write(json.dumps(row).encode() + b"\n")
    second.flush()
    hub.close()
    parts = list((tmp_path / "health_metrics").glob("date=*/hour=*/*.parquet"))
    assert len(parts) == 2


def test_writer_recovers_orphaned_wal(tmp_path):
    wal_dir = tmp_path / "_wal" / "health_metrics" / "oldrun"
    wal_dir.mkdir(parents=True)
    (wal_dir / "20260101T00.jsonl").write_text(
        '{"recv_ts_ns": 1, "stream": "test", "metric": "x", "value": 1.0, "labels_json": null}\n',
        encoding="utf-8",
    )
    hub = WriterHub(tmp_path, flush_seconds=5, queue_max_rows=1000)
    hub.register("health_metrics", HEALTH_METRICS)
    hub.close()

    parts = list((tmp_path / "health_metrics").glob("date=2026-01-01/hour=00/*.parquet"))
    assert parts, "orphaned WAL was not recovered"
    assert parts[0].name.startswith("part-recovered-oldrun")
    assert pq.read_table(parts[0]).num_rows == 1
