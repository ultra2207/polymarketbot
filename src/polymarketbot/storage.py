"""Storage helpers: SQLite tuning, journal opening, partition pruning, disk guards.

What is on the live path and what is an operator call:

* :func:`open_journal_connection` / :func:`run_with_busy_retry` - the contract a
  journal caller should use (WAL + busy timeout + bounded lock retry). The paper
  ledger opens through :func:`open_journal_connection`; the live engine's
  ``_init_sqlite`` applies the same pragmas (WAL + ``synchronous=NORMAL`` +
  ``busy_timeout``) on its own connection, and its commits are serialised by
  the engine lock.
* :func:`prune_directory` - called by ``live/recorder.py`` after every session
  to bound the live tape.
* :func:`get_disk_status` / :func:`clean_ephemeral_caches` / :func:`prune_sqlite_table`
  - operator calls; nothing schedules them.
* :func:`tune_sqlite_connection` - the pragmas behind the opener, usable on a
  connection the caller already owns.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from polymarketbot.logs import get_logger

log = get_logger("storage_sentinel")

# SQLite identifiers are interpolated into DDL/DML here, so they are checked
# rather than trusted: a caller that passes a table name from config must not be
# able to concatenate arbitrary SQL into the statement.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: A writer that loses the lock should wait for the other connection instead of
#: failing the statement. WAL readers do not block writers; rollback-journal
#: readers do, and the dashboard reads the same file the engine writes.
BUSY_TIMEOUT_MS = 5000


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


def tune_sqlite_connection(
    conn: sqlite3.Connection,
    *,
    auto_vacuum: bool = True,
    strict: bool = False,
    busy_timeout_ms: int = BUSY_TIMEOUT_MS,
) -> None:
    """Apply low-memory, append-safe pragmas to a connection the caller owns.

    - WAL mode: non-blocking concurrent reads and append writes.
    - synchronous=NORMAL: reduces fsync frequency while preserving WAL durability.
    - busy_timeout: wait for the other connection instead of failing instantly.
    - auto_vacuum=INCREMENTAL: enables freelist page recycling back to the OS.
    - cache_size=-2000: limits SQLite page cache to ~2 MB RAM per connection.
    - temp_store=MEMORY: prevents small temp tables from writing ephemeral disk scratch.

    ``strict`` re-raises instead of warning, for a caller that must know whether
    the journal really is in WAL mode (a silent downgrade is what a concurrent
    reader/writer pair cannot afford).
    """
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)};")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA cache_size = -2000;")
        conn.execute("PRAGMA temp_store = MEMORY;")
        if auto_vacuum:
            # auto_vacuum only takes effect on an empty database or after a
            # VACUUM, and VACUUM cannot run inside a transaction. The caller's
            # connection may have an open write, so the rewrite is refused
            # rather than silently committing the caller's work.
            current = conn.execute("PRAGMA auto_vacuum;").fetchone()
            if current and current[0] == 0:
                conn.execute("PRAGMA auto_vacuum = INCREMENTAL;")
                if conn.in_transaction:
                    log.warning(
                        "auto_vacuum=INCREMENTAL set but not applied: the connection "
                        "has an open transaction, so VACUUM was skipped"
                    )
                else:
                    conn.execute("VACUUM;")
    except sqlite3.Error as exc:
        if strict:
            raise
        log.warning(f"Could not apply full SQLite pragmas: {exc}")


def is_busy_error(exc: BaseException) -> bool:
    """True when SQLite rejected the statement only because another connection holds the lock."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return True
    text = str(exc).lower()
    return "database is locked" in text or "database is busy" in text


def run_with_busy_retry[T](
    fn: Callable[[], T], *, attempts: int = 6, base_delay_s: float = 0.05
) -> T:
    """Run ``fn``, retrying only the "another connection holds the lock" error.

    Bounded (``attempts``) with capped exponential backoff: a permanent lock is
    reported, not waited on forever. Any other ``sqlite3.Error`` propagates on
    the first call. ``fn`` must be safe to re-run, i.e. it owns its transaction
    (``execute`` + ``commit``) so a retry cannot half-apply a write.
    """
    last: sqlite3.OperationalError | None = None
    for attempt in range(max(1, attempts)):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not is_busy_error(exc) or attempt >= max(1, attempts) - 1:
                raise
            last = exc
            time.sleep(min(base_delay_s * (2 ** attempt), 1.0))
    raise last  # pragma: no cover - the loop always returns or raises


def open_journal_connection(
    path: str | Path,
    *,
    check_same_thread: bool = False,
    busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    attempts: int = 6,
) -> sqlite3.Connection:
    """Open a journal database with WAL, a busy timeout and a bounded open retry.

    The paper/live journals are written by the engine and read by other
    processes (the dashboard, the reconciler). In rollback-journal mode a reader
    blocks the writer, so a dashboard refresh could make a journal insert fail
    with ``database is locked``; the engine then lost that row while its
    in-memory book had already moved. WAL removes the reader/writer conflict and
    the timeout covers the remaining writer/writer window.

    Opening is retried only on a busy/locked error, ``attempts`` times, so a
    stale ``-wal`` lock from a crashed process is reported rather than hanging.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    last: sqlite3.OperationalError | None = None
    for attempt in range(max(1, attempts)):
        conn = sqlite3.connect(
            str(target),
            check_same_thread=check_same_thread,
            timeout=busy_timeout_ms / 1000.0,
        )
        try:
            tune_sqlite_connection(
                conn,
                auto_vacuum=False,
                strict=True,
                busy_timeout_ms=busy_timeout_ms,
            )
            mode = conn.execute("PRAGMA journal_mode;").fetchone()
            if not mode or str(mode[0]).lower() != "wal":
                raise sqlite3.OperationalError(
                    f"journal_mode is {mode[0] if mode else None!r}, not 'wal'"
                )
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return conn
        except sqlite3.OperationalError as exc:
            conn.close()
            if not is_busy_error(exc) or attempt >= max(1, attempts) - 1:
                raise
            last = exc
            time.sleep(min(0.05 * (2 ** attempt), 1.0))
    raise last  # pragma: no cover - the loop always returns or raises


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
    for name in (table, time_column):
        if not _IDENTIFIER.match(name):
            raise ValueError(f"unsafe SQL identifier: {name!r}")
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
    except sqlite3.Error as exc:
        log.warning(f"Prune failed on table '{table}': {exc}")
    return deleted


def prune_directory(
    root: str | Path,
    *,
    max_entries: int = 15,
    max_age_days: int = 14,
    marker_file: str | None = None,
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

        # Sort ascending by modified time (oldest first). Filesystems with a
        # coarse mtime stamp (FAT, and every writer inside the same clock tick)
        # produce ties, so the name breaks them instead of leaving the order to
        # directory enumeration.
        candidates.sort(key=lambda p: (p.stat().st_mtime, p.name))

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
    except OSError as exc:
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
            except OSError:
                pass
    return cleaned
