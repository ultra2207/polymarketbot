"""Storage Sentinel & SOTA Resource Optimization for PolymarketBot.

Provides production-grade disk space management, SQLite micro-tuning (WAL,
incremental auto-vacuum, memory-capped temp stores), automated rolling partition
pruning, and filesystem safety guards for cloud VPS deployments.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from polymarketbot.logs import get_logger

log = get_logger("storage_sentinel")


@dataclass
class DiskStatus:
    total_gb: float
    used_gb: float
    free_gb: float
    free_pct: float
    is_safe: bool
    is_critical: bool


def get_disk_status(path: str | Path = ".", min_free_gb: float = 2.0, emergency_gb: float = 1.0) -> DiskStatus:
    """Return current disk usage statistics and safety thresholds."""
    total, used, free = shutil.disk_usage(str(path))
    total_gb = total / (1024 ** 3)
    used_gb = used / (1024 ** 3)
    free_gb = free / (1024 ** 3)
    free_pct = (free / total) * 100.0 if total > 0 else 0.0

    is_critical = free_gb < emergency_gb
    is_safe = free_gb >= min_free_gb

    return DiskStatus(
        total_gb=round(total_gb, 2),
        used_gb=round(used_gb, 2),
        free_gb=round(free_gb, 2),
        free_pct=round(free_pct, 1),
        is_safe=is_safe,
        is_critical=is_critical,
    )


def tune_sqlite_connection(conn: sqlite3.Connection, *, auto_vacuum: bool = True) -> None:
    """Apply SOTA SQLite pragmas for low-memory VPS & high-performance append safety.

    - WAL mode: non-blocking concurrent reads and append writes.
    - synchronous=NORMAL: reduces fsync frequency while preserving WAL durability.
    - auto_vacuum=INCREMENTAL: enables freelist page recycling back to the OS.
    - cache_size=-2000: limits SQLite page cache to ~2 MB RAM per connection.
    - temp_store=MEMORY: prevents small temp tables from writing ephemeral disk scratch.
    """
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA cache_size = -2000;")
        conn.execute("PRAGMA temp_store = MEMORY;")
        if auto_vacuum:
            # Note: auto_vacuum only takes effect on empty databases or after a VACUUM.
            current = conn.execute("PRAGMA auto_vacuum;").fetchone()
            if current and current[0] == 0:
                conn.execute("PRAGMA auto_vacuum = INCREMENTAL;")
                conn.execute("VACUUM;")
    except Exception as exc:
        log.warning(f"Could not apply full SQLite pragmas: {exc}")


def prune_sqlite_table(
    conn: sqlite3.Connection,
    table: str,
    time_column: str,
    retention_seconds: float,
    *,
    checkpoint_wal: bool = True,
) -> int:
    """Prune rows older than cutoff from a table and reclaim disk pages.

    Returns the number of deleted rows.
    """
    cutoff = time.time() - retention_seconds
    deleted = 0
    try:
        cur = conn.execute(f"DELETE FROM {table} WHERE {time_column} < ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()

        if deleted > 0:
            # Reclaim freed freelist pages directly to OS
            conn.execute("PRAGMA incremental_vacuum(500);")
            if checkpoint_wal:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.commit()
            log.info(f"Storage pruned {deleted} rows from table '{table}' (retention={retention_seconds / 86400:.1f}d)")
    except Exception as exc:
        log.warning(f"Prune failed on table '{table}': {exc}")
    return deleted


def prune_directory(
    root: str | Path,
    *,
    max_entries: int = 15,
    max_age_days: int = 14,
    marker_file: Optional[str] = None,
) -> int:
    """Retain only the most recent subdirectories or files, deleting stale records.

    If marker_file is provided, only subdirectories containing that marker are candidates.
    Returns the number of removed entries.
    """
    path = Path(root)
    if not path.exists():
        return 0

    removed = 0
    now = time.time()
    try:
        candidates = []
        for item in path.iterdir():
            if marker_file:
                if item.is_dir() and (item / marker_file).exists():
                    candidates.append(item)
            else:
                candidates.append(item)

        # Sort ascending by modified time (oldest first)
        candidates.sort(key=lambda p: p.stat().st_mtime)

        # 1. Prune excess entries beyond max_entries
        while len(candidates) > max_entries:
            oldest = candidates.pop(0)
            if oldest.is_dir():
                shutil.rmtree(oldest, ignore_errors=True)
            else:
                oldest.unlink(missing_ok=True)
            removed += 1

        # 2. Prune entries exceeding max_age_days
        for item in list(candidates):
            age_s = now - item.stat().st_mtime
            if age_s > (max_age_days * 86400.0):
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
                removed += 1
                candidates.remove(item)

        if removed > 0:
            log.info(f"Storage pruned {removed} stale items in '{path}' (max_entries={max_entries}, max_age={max_age_days}d)")
    except Exception as exc:
        log.warning(f"Directory pruning failed for '{path}': {exc}")

    return removed


def clean_ephemeral_caches(repo_root: str | Path = ".") -> int:
    """Clean up ephemeral scratch, pytest caches, and temporary DuckDB spill files."""
    root = Path(repo_root)
    cleaned = 0
    ephemeral_dirs = [
        root / ".pytest_cache",
        root / "duckdb_spill",
        root / ".tmp",
        root / "data" / "tmp",
    ]
    for d in ephemeral_dirs:
        if d.exists() and d.is_dir():
            try:
                shutil.rmtree(d, ignore_errors=True)
                cleaned += 1
            except Exception:
                pass
    return cleaned
