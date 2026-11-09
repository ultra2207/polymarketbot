"""Profile a job before running it at full scale.

Implements the AGENTS.md rule: measure, project, then run.

Two modes:

  script   run a Python entry point under cProfile and print the top functions
           by cumulative and own time.

  sql      run EXPLAIN ANALYZE on a DuckDB query and report the actual plan
           timings, so you can see which operator dominates.

Examples
    uv run scripts/profile_job.py script scripts/build_universe.py
    uv run scripts/profile_job.py sql "SELECT count(*) FROM read_parquet('data/raw/sii/trades.parquet')"
    uv run scripts/profile_job.py sql-file queries/heavy.sql

Notes
-----
* For a large job, profile a *slice* first (one month, one partition) and scale
  the measured time. Pass a bounded query rather than the full one.
* cProfile adds overhead to Python-level code. It is still the right tool for
  finding accidental per-row work; it does not speed up DuckDB, which releases
  the GIL and does the real work in C++.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import runpy
import sys
import time
from pathlib import Path

import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)


def profile_script(path: Path, top: int) -> int:
    if not path.exists():
        log.info(f"no such file: {path}")
        return 1

    log.info(f"profiling {path} ...")
    profiler = cProfile.Profile()
    started = time.perf_counter()
    try:
        profiler.enable()
        runpy.run_path(str(path), run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (0, None):
            log.info(f"script exited with {exc.code}")
    finally:
        profiler.disable()
    elapsed = time.perf_counter() - started

    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.sort_stats("cumulative")

    log.info(f"\nwall time: {elapsed:.2f}s")
    log.info("\ntop by cumulative time:")
    stats.print_stats(top)
    log.info(stream.getvalue())

    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).sort_stats("tottime").print_stats(top)
    log.info("\ntop by own time (where the CPU actually is):")
    log.info(stream.getvalue())
    return 0


def profile_sql(query: str) -> int:
    con = duckdb.connect()
    con.execute("SET threads = 8")
    con.execute("SET memory_limit = '6GB'")

    log.info("EXPLAIN ANALYZE:\n")
    started = time.perf_counter()
    rows = con.sql(f"EXPLAIN ANALYZE {query}").fetchall()
    elapsed = time.perf_counter() - started
    text = "\n".join(str(r[1]) for r in rows)

    # DuckDB prints a plan tree; surface the timing lines.
    for line in text.splitlines():
        if any(k in line for k in ("Total Time", "└─", "├─", "Rows", "cost")):
            log.info(line)

    log.info(f"\nwall time: {elapsed:.2f}s")
    log.info("\nfull plan saved to data/processed/_last_plan.txt")
    Path("data/processed").mkdir(parents=True, exist_ok=True)
    Path("data/processed/_last_plan.txt").write_text(text, encoding="utf-8")
    return 0


def main() -> int:
    # DuckDB's EXPLAIN ANALYZE uses box-drawing characters; the default Windows
    # console encoding (cp1252) cannot encode them.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["script", "sql", "sql-file"])
    ap.add_argument("target", help="path for script/sql-file, query text for sql")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    if args.mode == "script":
        return profile_script(Path(args.target), args.top)
    if args.mode == "sql-file":
        return profile_sql(Path(args.target).read_text(encoding="utf-8"))
    return profile_sql(args.target)


if __name__ == "__main__":
    sys.exit(main())
