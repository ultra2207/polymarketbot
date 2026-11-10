"""Enrich slug-derived categories with Gamma tags, for the markets that need it.

Only markets that the rule set could not classify (`category = 'other'`, with
actual volume, from 2024 onward) are fetched. That is ~107 k markets, i.e.
~1,100 bounded requests, not 40 k.

Gamma's `category` field is null for most markets, so the signal used here is
the market's **tag labels** plus its slug, run through the same ordered rules
as `configs/categories.yaml`. The result is an override, not a replacement:
`build_categories.py` prefers a non-`other` override.

Resumable: completed condition ids are read back from the overrides file and
skipped. `--max-seconds` is a hard wall-clock budget, because this talks to a
flaky remote and must not run unbounded.

Usage
    uv run python -m polymarketbot.data.builders.enrich_categories_gamma --max-seconds 600
    uv run python -m polymarketbot.data.builders.enrich_categories_gamma --limit-batches 5   # profiling run
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


import duckdb
import requests

from polymarketbot.universe.categories import classify
from polymarketbot.logs import get_logger
log = get_logger(__name__)

OUT = Path(__file__).resolve().parents[4] / "data" / "processed"
PIT = OUT / "markets_pit.parquet"
OVERRIDES = OUT / "gamma_category_overrides.parquet"
JOURNAL = OUT / "_gamma_overrides.jsonl"
GAMMA = "https://gamma-api.polymarket.com/markets/keyset"


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 4")
    return con


def existing_ids(con: duckdb.DuckDBPyConnection) -> set[str]:
    if not OVERRIDES.exists():
        return set()
    rows = con.sql(f"SELECT condition_id FROM read_parquet('{OVERRIDES.as_posix()}')").fetchall()
    return {r[0] for r in rows}


def candidate_ids(con: duckdb.DuckDBPyConnection, done: set[str]) -> list[str]:
    rows = con.sql(f"""
        SELECT condition_id
        FROM read_parquet('{PIT.as_posix()}')
        WHERE category = 'other'
          AND NOT is_zero_volume
          AND created_at >= TIMESTAMPTZ '2024-01-01'
        ORDER BY md5(condition_id)
    """).fetchall()
    return [r[0] for r in rows if r[0] not in done]


def fetch_batch(session: requests.Session, ids: list[str], attempts: int = 4):
    params = ([("condition_ids", i) for i in ids]
              + [("closed", "any"), ("include_tag", "true"), ("limit", "100")])
    for i in range(attempts):
        try:
            r = session.get(GAMMA, params=params, timeout=30)
            if r.status_code != 200:
                time.sleep(1.0 * (i + 1))
                continue
            return r.json().get("markets") or []
        except Exception:  # noqa: BLE001
            time.sleep(1.0 * (i + 1))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-batches", type=int, default=0, help="0 = all")
    ap.add_argument("--max-seconds", type=float, default=900.0)
    ap.add_argument("--batch", type=int, default=100)
    args = ap.parse_args()

    con = connect()
    done = existing_ids(con)
    todo = candidate_ids(con, done)
    log.info(f"already enriched: {len(done):,}   remaining: {len(todo):,}", )

    if not todo:
        log.info("nothing to do")
        return 0

    batches = [todo[i:i + args.batch] for i in range(0, len(todo), args.batch)]
    if args.limit_batches:
        batches = batches[: args.limit_batches]

    log.info(f"projection: {len(batches)} batches x ~0.5 s = ~{len(batches) * 0.5 / 60:.1f} min", )
    session = requests.Session()
    session.headers["User-Agent"] = "polymarketbot-research/0.1"

    started = time.time()
    written = 0
    improved = 0
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if JOURNAL.exists() else "w"

    with JOURNAL.open(mode, encoding="utf-8") as fh:
        for n, batch in enumerate(batches, 1):
            if time.time() - started > args.max_seconds:
                log.info(f"  wall-clock budget reached after {n - 1} batches")
                break
            markets = fetch_batch(session, batch)
            if markets is None:
                log.info(f"  batch {n}: fetch failed, skipping")
                continue
            for m in markets:
                tags = [t.get("label") or "" for t in (m.get("tags") or [])]
                text = " ".join(tags) + " " + (m.get("slug") or "")
                cat = classify(text)
                if cat != "other":
                    improved += 1
                fh.write(json.dumps({
                    "condition_id": m.get("conditionId"),
                    "gamma_category": m.get("category"),
                    "tag_labels": tags,
                    "derived_category": cat,
                }) + "\n")
                written += 1
            if n % 25 == 0:
                log.info(f"  {n}/{len(batches)} batches, {written:,} rows, "
                      f"{improved:,} improved, {time.time() - started:.0f}s",
                      )

    log.info(f"\nfetched {written:,} markets, {improved:,} moved out of 'other' "
          f"({100 * improved / max(1, written):.1f}%) in {time.time() - started:.0f}s")

    if JOURNAL.exists():
        con.execute(f"""
            COPY (
                SELECT * EXCLUDE (rn) FROM (
                    SELECT *, row_number() OVER (PARTITION BY condition_id) AS rn
                    FROM read_json_auto('{JOURNAL.as_posix()}', format='newline_delimited')
                    WHERE condition_id IS NOT NULL
                ) WHERE rn = 1
            ) TO '{OVERRIDES.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        JOURNAL.unlink(missing_ok=True)
        n = con.sql(f"SELECT count(*) FROM read_parquet('{OVERRIDES.as_posix()}')").fetchall()[0][0]
        log.info(f"wrote {OVERRIDES} ({n:,} overrides)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
