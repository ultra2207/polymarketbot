"""Reduce PMXT hourly order-book archives to compact, queryable book state.

Why this is not a "download and replay" job
-------------------------------------------
The full PMXT v2 window is 2,861 hourly files, ~371 MB each, ~1.1 TB total, and
this machine has ~295 GB free. The archive can therefore never be materialised:
each hour must be streamed, reduced, and deleted.

What the raw hour actually contains (measured 2026-06-01T00)
-----------------------------------------------------------
    68,840,573 rows   73,367 distinct assets
    price_change 68,617,192   book 150,736   last_trade_price 72,269   tick_size_change 376

Three traps, all verified against the data rather than the docs:

1. **`side` is the book side, not the aggressor.** `BUY` rows never price above
   `best_ask` and `SELL` rows never below `best_bid`; the BUY prices observed
   were 0.01-0.47 and the SELL prices 0.53-0.99. So BUY -> bid ladder,
   SELL -> ask ladder. Reading `side` as taker direction inverts the book.
2. **`best_bid` / `best_ask` on `price_change` rows are stale and unusable.**
   For the busiest asset they held 0.50/0.51 across 15 consecutive events while
   the ladder moved through 0.01 -> 0.11 and 0.96 -> 0.99. A top-of-book series
   built from these columns is a frozen book. They are ignored here.
3. **`book` snapshots are sparse.** 8 anchors in 45 minutes for an asset with
   200,075 events, and only 8,238 of 73,367 assets have any anchor in a given
   hour. State must be reconstructed, and reconstructed across hour boundaries:
   an asset's first anchor may be in a previous file.

Method
------
Per (asset_id, side, price) the archive is a timeline of size changes. The book
at time t is the last size at or before t, keeping only non-zero sizes:

    state(t) = { (side, price, size) | last size <= t, size > 0 }

That is a temporal "last value per key" join, which DuckDB does vectorised with
ASOF JOIN — no per-event Python replay.

Output: one row per (token_id, bucket) with best bid/ask, depth inside 1c and
2c, and the top N levels per side. The exit-policy comparison needs the book at
decision points across a whole holding period, so cadence is a knob: 900 s costs
~1/3 of what 300 s costs.

Usage
    uv run scripts/reduce_pmxt.py --file <hour.parquet>             # one file
    uv run scripts/reduce_pmxt.py --dir <external>/pmxt             # whole archive
    uv run scripts/reduce_pmxt.py --start 2026-06-01T00 --hours 24  # ad-hoc download

`--dir` is the expected mode: the 1.1 TB raw archive is downloaded out of band
to an external drive (see docs/pmxt-runbook.md) and reduced in place. It never
deletes the raw files, and it is resumable — an existing output partition is
skipped.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "src")

import duckdb
import requests

PMXT = "https://r2v2.pmxt.dev/polymarket_orderbook_{stamp}.parquet"
OUT = Path("data/processed/pmxt_reduced")
SCRATCH = Path(".tmp/pmxt_raw")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 8")
    con.execute("SET memory_limit = '8GB'")
    con.execute("SET TimeZone = 'UTC'")
    return con


def fetch_hour(stamp: str, dest: Path) -> Path | None:
    """Download one hourly file. Returns None when the hour does not exist."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = PMXT.format(stamp=stamp)
    tmp = dest.with_suffix(".part")
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            if r.status_code == 404:
                return None
            r.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 22):
                    fh.write(chunk)
        tmp.replace(dest)
        return dest
    except requests.HTTPError:
        tmp.unlink(missing_ok=True)
        return None
    except Exception:  # noqa: BLE001 - network flakiness
        tmp.unlink(missing_ok=True)
        return None


def reduce_hour(con: duckdb.DuckDBPyConnection, src: Path, bucket_s: int,
                top_n: int) -> tuple[int, int]:
    """Reconstruct bucket-end book state for one hour. Returns (rows, assets)."""
    # ---- level-change timeline ------------------------------------------
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE upd AS
        WITH anchors AS (
            -- bids/asks are JSON strings holding [["price","size"], ...].
            -- CAST to a typed double list: `from_json(..., '[["VARCHAR","VARCHAR"]]')`
            -- rejects the file ("too many values in array of JSON structure").
            SELECT asset_id, timestamp AS ts, 'bid' AS side,
                   l[1] AS price, l[2] AS size
            FROM (
                SELECT asset_id, timestamp,
                       unnest(CAST(bids AS DOUBLE[][])) AS l
                FROM read_parquet('{src.as_posix()}')
                WHERE event_type = 'book' AND bids IS NOT NULL AND bids <> '[]'
            )
            UNION ALL
            SELECT asset_id, timestamp AS ts, 'ask' AS side,
                   l[1] AS price, l[2] AS size
            FROM (
                SELECT asset_id, timestamp,
                       unnest(CAST(asks AS DOUBLE[][])) AS l
                FROM read_parquet('{src.as_posix()}')
                WHERE event_type = 'book' AND asks IS NOT NULL AND asks <> '[]'
            )
        ),
        deltas AS (
            -- `side` is the LADDER side: BUY rows are bids, SELL rows are asks.
            SELECT asset_id, timestamp AS ts,
                   CASE WHEN side = 'BUY' THEN 'bid' ELSE 'ask' END AS side,
                   CAST(price AS DOUBLE) AS price,
                   CAST(size AS DOUBLE)  AS size
            FROM read_parquet('{src.as_posix()}')
            WHERE event_type = 'price_change' AND price IS NOT NULL
              AND side IN ('BUY', 'SELL')
        )
        SELECT * FROM anchors UNION ALL SELECT * FROM deltas
    """)

    # Bucket END timestamps. A bucket labelled 15:15 covers (15:00, 15:15].
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE keys AS
        SELECT DISTINCT asset_id,
               time_bucket(INTERVAL {bucket_s} SECOND, ts) + INTERVAL {bucket_s} SECOND AS bucket_end
        FROM upd
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE levels AS
        SELECT DISTINCT asset_id, side, price FROM upd
    """)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE state AS
        SELECT k.asset_id, k.bucket_end, lv.side, lv.price, u.size
        FROM keys k
        JOIN levels lv USING (asset_id)
        ASOF LEFT JOIN upd u
          ON u.asset_id = lv.asset_id
         AND u.side     = lv.side
         AND u.price    = lv.price
         AND u.ts      <= k.bucket_end
        WHERE u.size IS NOT NULL AND u.size > 0
    """)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE ranked AS
        SELECT * FROM (
            SELECT asset_id, bucket_end, side, price, size,
                   row_number() OVER (
                       PARTITION BY asset_id, bucket_end, side
                       ORDER BY CASE WHEN side = 'bid' THEN -price ELSE price END
                   ) AS rn
            FROM state
        ) WHERE rn <= {top_n}
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE book AS
        SELECT
            asset_id AS token_id,
            bucket_end AS ts,
            max(price)  FILTER (WHERE side = 'bid')                AS best_bid,
            min(price)  FILTER (WHERE side = 'ask')                AS best_ask,
            sum(price * size) FILTER (WHERE side = 'bid')          AS bid_notional_top,
            sum(price * size) FILTER (WHERE side = 'ask')          AS ask_notional_top,
            sum(size)   FILTER (WHERE side = 'bid')                AS bid_shares_top,
            sum(size)   FILTER (WHERE side = 'ask')                AS ask_shares_top,
            list(price ORDER BY price DESC) FILTER (WHERE side = 'bid') AS bid_prices,
            list(size  ORDER BY price DESC) FILTER (WHERE side = 'bid') AS bid_sizes,
            list(price ORDER BY price)      FILTER (WHERE side = 'ask') AS ask_prices,
            list(size  ORDER BY price)      FILTER (WHERE side = 'ask') AS ask_sizes
        FROM ranked
        GROUP BY asset_id, bucket_end
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE depth AS
        SELECT b.token_id, b.ts, b.best_bid, b.best_ask,
               b.bid_notional_top, b.ask_notional_top,
               b.bid_shares_top, b.ask_shares_top,
               b.bid_prices, b.bid_sizes, b.ask_prices, b.ask_sizes,
               -- notional inside 1c / 2c of the touch: the number that governs
               -- slippage at a $250 order, unlike whole-book totals.
               coalesce((SELECT sum(s.price * s.size) FROM state s
                         WHERE s.asset_id = b.token_id AND s.bucket_end = b.ts
                           AND s.side = 'bid' AND s.price >= b.best_bid - 0.01), 0) AS bid_notional_1c,
               coalesce((SELECT sum(s.price * s.size) FROM state s
                         WHERE s.asset_id = b.token_id AND s.bucket_end = b.ts
                           AND s.side = 'ask' AND s.price <= b.best_ask + 0.01), 0) AS ask_notional_1c,
               coalesce((SELECT sum(s.price * s.size) FROM state s
                         WHERE s.asset_id = b.token_id AND s.bucket_end = b.ts
                           AND s.side = 'bid' AND s.price >= b.best_bid - 0.02), 0) AS bid_notional_2c,
               coalesce((SELECT sum(s.price * s.size) FROM state s
                         WHERE s.asset_id = b.token_id AND s.bucket_end = b.ts
                           AND s.side = 'ask' AND s.price <= b.best_ask + 0.02), 0) AS ask_notional_2c
        FROM book b
        WHERE b.best_bid IS NOT NULL AND b.best_ask IS NOT NULL
    """)

    rows = con.sql("SELECT count(*) FROM depth").fetchall()[0][0]
    assets = con.sql("SELECT count(DISTINCT token_id) FROM depth").fetchall()[0][0]
    return rows, assets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="reduce a single local hour file")
    ap.add_argument("--dir", help="reduce every hour file already on disk (no download)")
    ap.add_argument("--skip-existing", action="store_true", default=True,
                    help="skip hours whose output partition already exists")
    ap.add_argument("--start", help="first hour, e.g. 2026-06-01T00")
    ap.add_argument("--hours", type=int, default=1)
    ap.add_argument("--bucket-seconds", type=int, default=900,
                    help="book snapshot cadence (default 900 = 15 min)")
    ap.add_argument("--top-levels", type=int, default=5)
    ap.add_argument("--keep-raw", action="store_true")
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    dest_root = Path(args.dest)
    con = connect()

    sources: list[tuple[datetime, Path]] = []
    if args.dir:
        # Handoff mode: the raw archive lives on an external drive and must
        # never be deleted by this script.
        root = Path(args.dir)
        if not root.exists():
            print(f"missing {root}")
            return 1
        files = sorted(root.rglob("polymarket_orderbook_*.parquet"))
        if not files:
            print(f"no hour files under {root}")
            return 1
        for f in files:
            stamp = f.stem.replace("polymarket_orderbook_", "")
            try:
                when = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
            except ValueError:
                print(f"  skipping unparseable name: {f.name}")
                continue
            part = dest_root / f"dt={when:%Y-%m-%d}" / f"hour={when:%H}" / "books.parquet"
            if args.skip_existing and part.exists():
                continue
            sources.append((when, f))
        print(f"local hours to reduce: {len(sources):,} "
              f"(of {len(files):,} on disk)")
    elif args.file:
        f = Path(args.file)
        if not f.exists():
            print(f"missing {f}")
            return 1
        stamp = f.stem.replace("polymarket_orderbook_", "")
        sources.append((datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc), f))
    else:
        if not args.start:
            print("pass --file or --start")
            return 1
        t = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        for i in range(args.hours):
            when = t + timedelta(hours=i)
            stamp = when.strftime("%Y-%m-%dT%H")
            local = SCRATCH / f"polymarket_orderbook_{stamp}.parquet"
            got = fetch_hour(stamp, local)
            if got is None:
                print(f"  {stamp}: absent (404)")
                continue
            sources.append((when, got))

    total_rows = 0
    t_start = time.time()
    for i, (when, src) in enumerate(sources, 1):
        t0 = time.time()
        rows, assets = reduce_hour(con, src, args.bucket_seconds, args.top_levels)
        part = dest_root / f"dt={when:%Y-%m-%d}" / f"hour={when:%H}"
        part.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"""
            COPY (SELECT * FROM depth) TO '{(part / 'books.parquet').as_posix()}'
            (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        size = (part / "books.parquet").stat().st_size
        total_rows += rows
        print(f"  [{i}/{len(sources)}] {when:%Y-%m-%dT%H}  "
              f"{rows:>8,} rows  {assets:>7,} assets  "
              f"{size/1e6:>7.1f} MB  {time.time()-t0:>6.1f}s")
        # Never delete a file the operator downloaded; only our own scratch copy.
        if not args.keep_raw and not args.file and not args.dir:
            src.unlink(missing_ok=True)

    if sources:
        elapsed = time.time() - t_start
        print(f"\n{len(sources)} hour(s) in {elapsed/60:.1f} min — "
              f"{total_rows:,} rows, {elapsed/len(sources):.1f}s/hour")
        print(f"extrapolated to 2,861 hours: "
              f"{elapsed/len(sources)*2861/3600:.1f} h compute")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
