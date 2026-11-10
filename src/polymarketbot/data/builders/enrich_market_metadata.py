"""Enrich markets with Gamma-only metadata.

Adds fields the trade tape cannot give us:

* `version` - position system ("v1" CTF vs "v2" PositionManager)
* `clob_token_ids` / `position_ids` - outcome identifiers per system
* `fee_schedule` (rate, exponent, rebate) and `fee_type`
* `tick_size`, `min_order_size`
* `neg_risk`, `uma_resolution_statuses`, `tag_labels`

Performance note (learned the hard way): measured latency is ~1 s per request,
not the ~0.1 s the throttle implies. A single-threaded run therefore projects to
~6.8 hours for 2.4 M markets, not ~40 minutes. This version uses worker threads
with thread-local sessions and a global target rate, and prints a *revised* ETA
from measured throughput after 50 batches.

Resumable: condition ids already in the output **or in the journal** are
skipped, so an interrupted run resumes rather than restarting.

Usage
    uv run python -m polymarketbot.data.builders.enrich_market_metadata --limit 200 --workers 4   # smoke test
    uv run python -m polymarketbot.data.builders.enrich_market_metadata --workers 16 --max-seconds 2400
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


import duckdb

from polymarketbot.gamma import GammaClient
from polymarketbot.logs import get_logger
log = get_logger(__name__)

OUT = Path(__file__).resolve().parents[4] / "data" / "processed"
PIT = OUT / "markets_pit.parquet"
DEST = OUT / "market_metadata_gamma.parquet"
JOURNAL = OUT / "_gamma_meta.jsonl"
BATCH = 100

_TLS = threading.local()
_WRITE_LOCK = threading.Lock()


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 4")
    return con


def existing(con: duckdb.DuckDBPyConnection) -> set[str]:
    done: set[str] = set()
    if DEST.exists():
        done |= {r[0] for r in con.sql(
            f"SELECT condition_id FROM read_parquet('{DEST.as_posix()}')"
        ).fetchall()}
    if JOURNAL.exists():
        with JOURNAL.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["condition_id"])
                except Exception:  # noqa: BLE001
                    continue
    return done


def candidates(con: duckdb.DuckDBPyConnection, done: set[str], limit: int | None) -> list[str]:
    rows = con.sql(f"""
        SELECT condition_id
        FROM read_parquet('{PIT.as_posix()}')
        WHERE NOT is_zero_volume
          AND created_at >= TIMESTAMPTZ '2024-01-01'
          AND condition_id IS NOT NULL
        ORDER BY md5(condition_id)
        {f'LIMIT {limit}' if limit else ''}
    """).fetchall()
    return [r[0] for r in rows if r[0] not in done]


def to_row(m: dict) -> dict:
    sched = GammaClient.fee_schedule(m)
    return {
        "condition_id": m.get("conditionId"),
        "gamma_id": m.get("id"),
        "version": m.get("version"),
        "clob_token_ids": GammaClient.parse_ids(m.get("clobTokenIds")),
        "position_ids": GammaClient.parse_ids(m.get("positionIds")),
        "neg_risk": m.get("negRisk"),
        "fees_enabled": m.get("feesEnabled"),
        "fee_type": m.get("feeType"),
        "fee_rate": sched.rate if sched else None,
        "fee_exponent": sched.exponent if sched else None,
        "fee_rebate": sched.rebate_rate if sched else None,
        "tick_size": GammaClient.tick_size(m),
        "min_order_size": GammaClient.min_order_size(m),
        "uma_resolution_statuses": m.get("umaResolutionStatuses"),
        "tag_labels": GammaClient.tag_labels(m),
        "gamma_end_date": m.get("endDate"),
        "gamma_closed": m.get("closed"),
    }


def flush_journal(con: duckdb.DuckDBPyConnection) -> int:
    if not JOURNAL.exists():
        return con.sql(f"SELECT count(*) FROM read_parquet('{DEST.as_posix()}')").fetchall()[0][0] \
            if DEST.exists() else 0

    # `uma_resolution_statuses` arrives as a JSON *string* (e.g. '["proposed"]')
    # even though the destination column is VARCHAR[]. read_json_auto infers it
    # as VARCHAR; parse it explicitly. `from_json` takes a JSON schema, not a
    # type name, hence '["VARCHAR"]'.
    new_sql = f"""
        SELECT
            condition_id, gamma_id, version, clob_token_ids, position_ids,
            neg_risk, fees_enabled, fee_type, fee_rate, fee_exponent, fee_rebate,
            tick_size, min_order_size,
            CASE WHEN uma_resolution_statuses IS NULL
                 THEN NULL
                 ELSE from_json(CAST(uma_resolution_statuses AS VARCHAR), '["VARCHAR"]')
            END AS uma_resolution_statuses,
            tag_labels, gamma_end_date, gamma_closed
        FROM read_json_auto('{JOURNAL.as_posix()}', format='newline_delimited')
    """
    if DEST.exists():
        old_sql = f"SELECT * FROM read_parquet('{DEST.as_posix()}')"
        merged = f"SELECT * FROM ({old_sql}) UNION ALL SELECT * FROM ({new_sql})"
    else:
        merged = new_sql

    con.execute(f"""
        COPY (
            SELECT * EXCLUDE (rn) FROM (
                SELECT *, row_number() OVER (PARTITION BY condition_id) AS rn
                FROM ({merged})
            ) WHERE rn = 1
        ) TO '{DEST.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    JOURNAL.unlink(missing_ok=True)
    return con.sql(f"SELECT count(*) FROM read_parquet('{DEST.as_posix()}')").fetchall()[0][0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--target-rate", type=float, default=25.0, help="requests/second total")
    ap.add_argument("--max-seconds", type=float, default=1800.0)
    args = ap.parse_args()

    con = connect()
    done = existing(con)
    todo = candidates(con, done, args.limit or None)
    log.info(f"already enriched: {len(done):,}   remaining: {len(todo):,}", )
    if not todo:
        log.info("nothing to do")
        return 0

    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    # Each worker sleeps so that workers / interval = target rate.
    per_client_interval = max(0.01, args.workers / args.target_rate)
    log.info(f"{len(batches)} batches, {args.workers} workers, target {args.target_rate:.0f} req/s "
          f"({per_client_interval:.2f}s/client)", )

    def client() -> GammaClient:
        if not hasattr(_TLS, "client"):
            _TLS.client = GammaClient(min_interval_s=per_client_interval)
        return _TLS.client

    def fetch(batch: list[str]) -> list[dict]:
        return [to_row(m) for m in client().markets(batch)]

    started = time.time()
    written = 0
    completed = 0
    mode = "a" if JOURNAL.exists() else "w"
    fh = JOURNAL.open(mode, encoding="utf-8")

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(fetch, b): b for b in batches}
            for fut in as_completed(futures):
                if time.time() - started > args.max_seconds:
                    for f in futures:
                        f.cancel()
                    log.info("  wall-clock budget reached; stopping", )
                    break
                completed += 1
                try:
                    rows = fut.result()
                except Exception as exc:  # noqa: BLE001
                    log.info(f"  batch failed: {type(exc).__name__}: {str(exc)[:100]}", )
                    continue
                with _WRITE_LOCK:
                    for row in rows:
                        if row["condition_id"]:
                            fh.write(json.dumps(row, default=str) + "\n")
                            written += 1
                    if completed % 50 == 0:
                        fh.flush()
                        elapsed = time.time() - started
                        rate = completed / max(elapsed, 1e-9)
                        eta = (len(batches) - completed) / max(rate, 1e-9)
                        log.info(f"  {completed}/{len(batches)} batches, {written:,} rows, "
                              f"{rate:.1f} batch/s, eta {eta/60:.1f} min", )
    finally:
        fh.close()

    rows = flush_journal(con)
    log.info(f"\nenriched {written:,} this run; {DEST} now holds {rows:,} rows "
          f"in {time.time() - started:.0f}s", )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
