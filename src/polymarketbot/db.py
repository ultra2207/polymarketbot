"""Shared DuckDB connection settings.

Every script used to hardcode `threads = 8`, which leaves most of a 24-thread
machine idle. Measured on the one-month tape aggregation:

    threads=8   1.61 s
    threads=12  1.41 s
    threads=16  1.36 s
    threads=20  1.28 s

So the gain is real but bounded (~20%): these jobs are I/O- and
decompression-bound, not CPU-bound. Adding more threads past the physical core
count mostly adds contention. The defaults below reflect that measurement
rather than a guess.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

TEMP_DIR = Path("data/tmp")


def connect(
    threads: int | None = None,
    memory_limit: str = "8GB",
    temp_dir: Path | str | None = TEMP_DIR,
) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()

    # Never exceed physical cores; hyperthreads add little for scan workloads.
    physical = max(1, (os.cpu_count() or 8) // 2)
    n = threads or min(16, physical)
    con.execute(f"SET threads = {n}")
    con.execute(f"SET memory_limit = '{memory_limit}'")

    # These jobs produce unordered aggregates and Parquet files; preserving
    # insertion order costs memory for no benefit.
    con.execute("SET preserve_insertion_order = false")

    if temp_dir is not None:
        path = Path(temp_dir)
        path.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = '{path.resolve().as_posix()}'")

    return con
