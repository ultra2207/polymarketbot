"""Measure resolution timing and dispute rates from `/v2/resolutions`.

The original plan treated resolution timing as unavailable. It is available for
recent markets: the endpoint returns `resolved_at`, `resolved_block`,
`was_disputed`, `resolution_source` and sometimes `payouts`.

We join that to `markets_pit.end_date` to measure the gap between "eligible for
resolution" and "actually resolved". That gap is capital locked with no trading
— the basis of idea #26 in `plan.md`.

Fields are nullable by market era: older rows expose `last_update_timestamp`
instead of `resolved_at`, and `payouts` appears for only some resolutions.
A null means unavailable, never zero.

Usage
    uv run scripts/fetch_resolution_timing.py --n 2000
    uv run scripts/fetch_resolution_timing.py --n 500 --max-seconds 120
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, "src")

import duckdb

from polymarketbot.dataapi import DataAPIClient
from polymarketbot.logs import get_logger
log = get_logger(__name__)

OUT = Path("data/processed")
PIT = OUT / "markets_pit.parquet"
DEST = OUT / "resolution_timing.parquet"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-seconds", type=float, default=600.0)
    args = ap.parse_args()

    con = duckdb.connect()
    con.execute(f"CREATE VIEW pit AS SELECT * FROM read_parquet('{PIT.as_posix()}')")

    markets = con.sql(f"""
        SELECT condition_id, category, created_at, end_date, volume, is_void
        FROM pit
        WHERE condition_id IS NOT NULL
          AND NOT is_zero_volume
          AND created_at >= TIMESTAMPTZ '2026-05-01'
          AND closed = 1
        ORDER BY md5(condition_id)
        LIMIT {args.n}
    """).fetchall()
    log.info(f"sampled {len(markets):,} closed markets created after 2026-05-01", )
    if not markets:
        return 1

    def fetch(row) -> dict:
        cid, category, created, end, volume, is_void = row
        c = DataAPIClient(min_interval_s=0.05)
        try:
            res = c.resolutions(cid)
        except Exception:  # noqa: BLE001
            res = []
        r = res[0] if res else {}
        # resolved_at is an ISO string on newer rows; fall back to epoch fields.
        # Parse as UTC explicitly: time.mktime interprets as local time and
        # silently biases every lag by the local UTC offset (it produced an
        # exact -5.49 h shift on an IST machine).
        resolved_at = r.get("resolved_at")
        resolved_ts = None
        if isinstance(resolved_at, str) and resolved_at:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(resolved_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            resolved_ts = int(dt.timestamp())
        elif isinstance(resolved_at, (int, float)):
            resolved_ts = int(resolved_at)

        return {
            "condition_id": cid,
            "category": category,
            "created_at": str(created),
            "end_date": str(end),
            "volume": volume,
            "is_void": is_void,
            "status": r.get("status"),
            "was_disputed": r.get("was_disputed"),
            "was_arbitrated": r.get("was_arbitrated"),
            "resolution_source": r.get("resolution_source"),
            "resolved_at": resolved_at,
            "resolved_ts": resolved_ts,
            "resolved_block": r.get("resolved_block"),
            "payouts": r.get("payouts"),
            "price": r.get("price"),
            "last_update_timestamp": r.get("last_update_timestamp"),
        }

    started = time.time()
    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch, m): m for m in markets}
        for fut in as_completed(futures):
            if time.time() - started > args.max_seconds:
                for f in futures:
                    f.cancel()
                log.info("  budget reached", )
                break
            rows.append(fut.result())
            if len(rows) % 100 == 0:
                log.info(f"  {len(rows)}/{len(markets)}, {time.time()-started:.0f}s", )

    jsonl = OUT / "_resolution_timing.jsonl"
    with jsonl.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")

    con.execute(f"""
        COPY (SELECT * FROM read_json_auto('{jsonl.as_posix()}', format='newline_delimited'))
        TO '{DEST.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    jsonl.unlink(missing_ok=True)

    log.info(f"\nwrote {DEST} ({len(rows):,} rows)")
    log.info("\nresolution fields present:")
    log.info(con.sql(f"""
        SELECT count(*) n,
               count(status) with_status,
               count(resolved_ts) with_resolved_ts,
               count(payouts) with_payouts,
               count(*) FILTER (WHERE was_disputed) disputed
        FROM read_parquet('{DEST.as_posix()}')
    """).fetchall()[0])

    log.info("\nresolution lag (resolved_ts - end_date), hours, where both exist:")
    for r in con.sql(f"""
        SELECT category, count(*) n,
               round(median((resolved_ts - epoch(TRY_CAST(end_date AS TIMESTAMPTZ)))/3600.0), 2) med_h,
               round(quantile_cont((resolved_ts - epoch(TRY_CAST(end_date AS TIMESTAMPTZ)))/3600.0, 0.9), 2) p90_h
        FROM read_parquet('{DEST.as_posix()}')
        WHERE resolved_ts IS NOT NULL AND end_date IS NOT NULL
        GROUP BY 1 ORDER BY n DESC
    """).fetchall():
        log.info(f"  {r[0]:12} n={r[1]:>4}  median={r[2]:>8} h  p90={r[3]:>8} h")

    log.info("\ndisputes by category:")
    for r in con.sql(f"""
        SELECT category, count(*) n,
               count(*) FILTER (WHERE was_disputed) disputed,
               round(100.0*count(*) FILTER (WHERE was_disputed)/count(*), 1) pct
        FROM read_parquet('{DEST.as_posix()}')
        GROUP BY 1 ORDER BY n DESC
    """).fetchall():
        log.info(f"  {r[0]:12} n={r[1]:>4}  disputed={r[2]:>3}  {r[3]}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
