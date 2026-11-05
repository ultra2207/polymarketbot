"""Lossless JSONL write-ahead log with hourly Parquet compaction.

Design
------
Every stream emits Python dicts. ``WriterHub.emit`` serialises one dict with
orjson and puts it on a bounded queue. A single writer thread appends the bytes
to ``<root>/_wal/<table>/<run_id>/<YYYYMMDDTHH>.jsonl`` and fsyncs every
``flush_seconds``. When the UTC hour rolls over (or the process stops), the
closed WAL is converted to one Parquet part:

    <root>/<table>/date=YYYY-MM-DD/hour=HH/part-<run_id>-<wal>.parquet

Compaction runs on a small thread pool so the writer keeps draining the queue.
A crash leaves the JSONL on disk; ``register()`` recovers any orphaned WAL for
that table on the next start. This is the "persist as you go" rule from
AGENTS.md: a kill costs at most the current 60 s window, never the hour.

Rows must use the field names and types of the table's Arrow schema. Missing
fields become null; unknown fields are ignored by ``pa.Table.from_pylist``.
"""

from __future__ import annotations

import concurrent.futures
import os
import queue
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson
import pyarrow as pa
import pyarrow.parquet as pq

from polymarketbot.logs import IST, get_logger

logger = get_logger("data.writer")

_HOUR_RE = re.compile(r"^(\d{8}T\d{2})")
_BATCH_MAX = 5000


def _utc_hour(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y%m%dT%H")


def _hour_parts(hour: str) -> tuple[str, str]:
    return f"{hour[:4]}-{hour[4:6]}-{hour[6:8]}", hour[9:11]


class WriterHub:
    """Bounded queue -> JSONL WAL -> hourly Parquet parts."""

    def __init__(
        self,
        root: str | Path,
        *,
        flush_seconds: float = 60.0,
        queue_max_rows: int = 500_000,
        compaction_workers: int = 2,
        part_seconds: float = 600.0,
    ) -> None:
        self.root = Path(root)
        self.run_id = datetime.now(IST).strftime("%Y%m%dT%H%M%S")
        self.flush_seconds = max(5.0, float(flush_seconds))
        self.part_seconds = max(60.0, float(part_seconds))
        self._schemas: dict[str, pa.Schema] = {}
        self._queue: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=max(1, queue_max_rows))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._spill_lock = threading.Lock()
        self._files: dict[str, Any] = {}
        self._parts: dict[str, tuple[str, int]] = {}
        self._part_started: dict[str, float] = {}
        self._last_flush = time.time()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, compaction_workers), thread_name_prefix="parquet"
        )
        self._futures: list[concurrent.futures.Future] = []
        self._futures_lock = threading.Lock()
        self.rows_emitted = 0
        self.rows_written = 0
        self.spills = 0
        self.compaction_errors = 0
        self._thread = threading.Thread(target=self._run, name="writer-hub", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ tables
    def register(self, table: str, schema: pa.Schema) -> None:
        """Register a table and recover any WAL left by a previous run."""
        with self._lock:
            if table in self._schemas:
                return
            self._schemas[table] = schema
        for wal in sorted((self.root / "_wal" / table).glob("*/*.jsonl")):
            self._submit_compaction(table, wal)

    def schema(self, table: str) -> pa.Schema:
        return self._schemas[table]

    # ------------------------------------------------------------------ emit
    def emit(self, table: str, row: dict[str, Any]) -> None:
        """Append one row. Never blocks the caller on disk I/O."""
        if table not in self._schemas:
            raise KeyError(f"table {table!r} is not registered")
        line = orjson.dumps(row)
        try:
            self._queue.put_nowait((table, line))
            with self._lock:
                self.rows_emitted += 1
        except queue.Full:
            self._spill(table, line)

    def _spill(self, table: str, line: bytes) -> None:
        """Queue overflow goes straight to disk; rare, and never silent."""
        hour = _utc_hour()
        path = self.root / "_wal" / table / self.run_id / f"{hour}.overflow.jsonl"
        with self._spill_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as fh:
                fh.write(line)
                fh.write(b"\n")
            self.spills += 1
            with self._lock:
                self.rows_emitted += 1

    # ------------------------------------------------------------------ writer
    def _file_for(self, table: str) -> Any:
        now = time.time()
        hour = _utc_hour(now)
        current = self._parts.get(table)
        rotate = (
            table not in self._files
            or current is None
            or current[0] != hour
            or (now - self._part_started.get(table, now)) >= self.part_seconds
        )
        if not rotate:
            return self._files[table]
        if table in self._files:
            self._files[table].flush()
            os.fsync(self._files[table].fileno())
            self._files[table].close()
            old = self._files.pop(table)
            self._submit_compaction(table, Path(old.name))
        seq = 0 if current is None or current[0] != hour else current[1] + 1
        path = self.root / "_wal" / table / self.run_id / f"{hour}-{seq:02d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._files[table] = open(path, "ab", buffering=1 << 20)
        self._parts[table] = (hour, seq)
        self._part_started[table] = now
        return self._files[table]

    def _run(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=0.25)
            except queue.Empty:
                if self._stop.is_set():
                    break
                self._maybe_flush()
                continue
            batch = [first]
            while len(batch) < _BATCH_MAX:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            for table, line in batch:
                fh = self._file_for(table)
                fh.write(line)
                fh.write(b"\n")
            self._maybe_flush()

    def _maybe_flush(self) -> None:
        now = time.time()
        if now - self._last_flush < self.flush_seconds:
            return
        self._last_flush = now
        for fh in list(self._files.values()):
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except OSError as exc:
                logger.warning(f"WAL fsync failed: {exc}")

    # ------------------------------------------------------------------ compact
    def _submit_compaction(self, table: str, wal: Path) -> None:
        if not wal.exists() or wal.stat().st_size == 0:
            return
        with self._futures_lock:
            self._futures.append(self._executor.submit(self._compact, table, wal))
            self._futures = [f for f in self._futures if not f.done()]

    def _compact(self, table: str, wal: Path) -> None:
        schema = self._schemas.get(table)
        if schema is None:
            logger.error(f"no schema registered for {table}; leaving {wal} in place")
            return
        try:
            rows: list[dict[str, Any]] = []
            with open(wal, "rb") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(orjson.loads(line))
            if not rows:
                wal.unlink(missing_ok=True)
                return
            table_obj = pa.Table.from_pylist(rows, schema=schema)
            hour = _HOUR_RE.match(wal.stem)
            hour_key = hour.group(1) if hour else _utc_hour()
            date_part, hour_part = _hour_parts(hour_key)
            dest_dir = self.root / table / f"date={date_part}" / f"hour={hour_part}"
            dest_dir.mkdir(parents=True, exist_ok=True)
            # A recovered WAL comes from an older run; its source run id must
            # stay in the part name or it collides with this run's first part
            # (same hour, same sequence) and `os.replace` silently drops data.
            source_run = wal.parent.name
            prefix = (
                f"part-recovered-{source_run}"
                if source_run != self.run_id
                else f"part-{self.run_id}"
            )
            dest = dest_dir / f"{prefix}-{wal.stem}.parquet"
            tmp = dest.with_suffix(".parquet.tmp")
            pq.write_table(
                table_obj,
                tmp,
                compression="zstd",
                compression_level=7,
                use_dictionary=True,
                row_group_size=100_000,
            )
            os.replace(tmp, dest)
            wal.unlink(missing_ok=True)
            with self._lock:
                self.rows_written += len(rows)
            logger.info(f"compacted {wal.name}: {len(rows):,} rows -> {dest.name}")
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.compaction_errors += 1
            logger.error(f"compaction failed for {wal}: {exc}")

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, float]:
        with self._lock:
            return {
                "queue_depth": float(self._queue.qsize()),
                "rows_emitted": float(self.rows_emitted),
                "rows_written": float(self.rows_written),
                "spills": float(self.spills),
                "compaction_errors": float(self.compaction_errors),
            }

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)
        for table, fh in list(self._files.items()):
            try:
                fh.flush()
                os.fsync(fh.fileno())
                fh.close()
            except OSError:
                pass
            self._files.pop(table, None)
            hour, seq = self._parts[table]
            wal = self.root / "_wal" / table / self.run_id / f"{hour}-{seq:02d}.jsonl"
            self._submit_compaction(table, wal)
        # Overflow files are compacted only on shutdown, so a concurrent spill
        # can never be half-read.
        for overflow in sorted((self.root / "_wal").glob("*/*/*.overflow.jsonl")):
            table = overflow.parents[1].name
            self._submit_compaction(table, overflow)
        with self._futures_lock:
            pending = list(self._futures)
        for future in pending:
            try:
                future.result(timeout=60)
            except Exception:  # noqa: BLE001
                pass
        self._executor.shutdown(wait=True)
