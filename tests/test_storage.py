"""Storage helpers: disk guards, SQLite pragmas, journal opening, pruning."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from polymarketbot.storage import (
    clean_ephemeral_caches,
    get_disk_status,
    open_journal_connection,
    prune_directory,
    prune_sqlite_table,
    run_with_busy_retry,
    tune_sqlite_connection,
)


def test_disk_status_retrieval(tmp_path: Path):
    status = get_disk_status(tmp_path, min_free_gb=0.1, emergency_gb=0.05)
    assert status.total_gb > 0.0
    assert status.free_gb > 0.0
    assert 0.0 <= status.free_pct <= 100.0
    assert status.is_safe is True
    assert status.is_critical is False


def test_sqlite_pragmas_and_incremental_vacuum(tmp_path: Path):
    db_file = tmp_path / "test_tuned.db"
    conn = sqlite3.connect(str(db_file))
    tune_sqlite_connection(conn, auto_vacuum=True)

    # Verify pragmas
    journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert journal_mode.upper() == "WAL"

    auto_vac = conn.execute("PRAGMA auto_vacuum;").fetchone()[0]
    assert auto_vac == 2  # 2 is INCREMENTAL

    conn.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, ts REAL, val TEXT)")
    now = time.time()
    # Insert 100 rows, 50 older than 10 days
    old_ts = now - (15 * 86400.0)
    for i in range(50):
        conn.execute("INSERT INTO test_data (ts, val) VALUES (?, ?)", (old_ts, f"old_{i}" * 100))
    for i in range(50):
        conn.execute("INSERT INTO test_data (ts, val) VALUES (?, ?)", (now, f"new_{i}" * 100))
    conn.commit()

    count_before = conn.execute("SELECT count(*) FROM test_data").fetchone()[0]
    assert count_before == 100

    # Prune rows older than 10 days (864,000s)
    deleted = prune_sqlite_table(conn, "test_data", "ts", retention_seconds=10 * 86400.0)
    assert deleted == 50

    count_after = conn.execute("SELECT count(*) FROM test_data").fetchone()[0]
    assert count_after == 50

    conn.close()


def test_directory_pruning(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir()

    # Create 8 dummy session folders with marker. The mtime is set explicitly:
    # `time.sleep(0.01)` between creates still ties on a coarse-mtime volume and
    # made this test order-dependent on directory enumeration.
    base = time.time() - 3600.0
    for i in range(8):
        s_dir = root / f"sess_{i}"
        s_dir.mkdir()
        (s_dir / "session.json").write_text("{}", encoding="utf-8")
        os.utime(s_dir / "session.json", (base + i, base + i))
        os.utime(s_dir, (base + i, base + i))

    assert len(list(root.iterdir())) == 8

    # Keep only 4 most recent
    removed = prune_directory(root, max_entries=4, max_age_days=30, marker_file="session.json")
    assert removed == 4
    remaining = list(root.iterdir())
    assert len(remaining) == 4
    # The oldest (0, 1, 2, 3) must be gone, newest (4, 5, 6, 7) must remain
    names = {p.name for p in remaining}
    assert "sess_7" in names
    assert "sess_0" not in names


def test_clean_ephemeral_caches(tmp_path: Path):
    spill = tmp_path / "duckdb_spill"
    spill.mkdir()
    (spill / "temp.bin").write_bytes(b"temp")

    pytest_cache = tmp_path / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / "v").mkdir()

    cleaned = clean_ephemeral_caches(tmp_path)
    assert cleaned >= 2
    assert not spill.exists()
    assert not pytest_cache.exists()


def test_open_journal_connection_is_wal_and_readable_while_writing(tmp_path: Path):
    db = tmp_path / "journal.db"
    conn = open_journal_connection(db)
    assert conn.execute("PRAGMA journal_mode;").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout;").fetchone()[0] > 0
    conn.execute("CREATE TABLE trades (ts REAL)")
    conn.execute("INSERT INTO trades VALUES (1.0)")
    conn.commit()

    # The dashboard's read-only connection must not block the writer.
    reader = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    assert reader.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
    conn.execute("INSERT INTO trades VALUES (2.0)")
    conn.commit()
    assert reader.execute("SELECT count(*) FROM trades").fetchone()[0] == 2
    reader.close()
    conn.close()


def test_open_journal_connection_gives_up_on_a_held_lock(tmp_path: Path):
    db = tmp_path / "locked.db"
    holder = sqlite3.connect(db)
    holder.execute("CREATE TABLE t (a INTEGER)")
    holder.commit()
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            # Bounded: two attempts with a 50 ms busy timeout, not a hang.
            open_journal_connection(db, busy_timeout_ms=50, attempts=2)
    finally:
        holder.rollback()
        holder.close()
    # With the lock released the same call succeeds.
    open_journal_connection(db, busy_timeout_ms=50, attempts=2).close()


def test_run_with_busy_retry_only_retries_locked_errors():
    calls: list[int] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert run_with_busy_retry(flaky, attempts=4, base_delay_s=0.0) == "ok"
    assert len(calls) == 3

    def broken() -> None:
        raise sqlite3.OperationalError("no such table: trades")

    with pytest.raises(sqlite3.OperationalError):
        run_with_busy_retry(broken, attempts=4, base_delay_s=0.0)

    def always_locked() -> None:
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        run_with_busy_retry(always_locked, attempts=3, base_delay_s=0.0)
