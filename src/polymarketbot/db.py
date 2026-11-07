"""Shared DuckDB connection settings.

Configures memory limit and thread allocations for local and cloud environments.
Supports DUCKDB_MEMORY_LIMIT and DUCKDB_THREADS environment variables to prevent
OOM on low-RAM cloud instances (e.g. 1GB-4GB VPS).
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

TEMP_DIR = Path("data/tmp")


def connect(
    threads: int | None = None,
    memory_limit: str | None = None,
    temp_dir: Path | str | None = TEMP_DIR,
) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()

    # Resolve memory limit: explicit parameter -> DUCKDB_MEMORY_LIMIT env -> default 1GB
    # 1GB ensures stability on low-RAM cloud VPS (e.g. Oracle Cloud Free Tier 1GB micro).
    mem = memory_limit or os.environ.get("DUCKDB_MEMORY_LIMIT", "1GB")

    # Resolve threads: explicit parameter -> DUCKDB_THREADS env -> default 2
    # Bounded to physical core count.
    physical = max(1, (os.cpu_count() or 2) // 2)
    env_threads = os.environ.get("DUCKDB_THREADS")
    if threads is not None:
        n = threads
    elif env_threads:
        try:
            n = int(env_threads)
        except ValueError:
            n = min(2, physical)
    else:
        n = min(2, physical)

    con.execute(f"SET threads = {n}")
    con.execute(f"SET memory_limit = '{mem}'")

    # These jobs produce unordered aggregates and Parquet files; preserving
    # insertion order costs memory for no benefit.
    con.execute("SET preserve_insertion_order = false")

    if temp_dir is not None:
        path = Path(temp_dir)
        path.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = '{path.resolve().as_posix()}'")

    return con
