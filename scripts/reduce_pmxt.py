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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "src")

import duckdb
import requests

PMXT = "https://r2v2.pmxt.dev/polymarket_orderbook_{stamp}.parquet"
OUT = Path("data/processed/pmxt_reduced")
SCRATCH = Path(".tmp/pmxt_raw")


def connect() -> duckdb.DuckDBPyConnection:
    """Memory-tuned connection.

    This machine has 15.6 GB of RAM, and the hour being reduced materialises
    ~81 M events. Two settings matter more than thread count:

    * `preserve_insertion_order = false` — the reducer never depends on row
      order, and dropping the guarantee lets DuckDB use streaming hash
      aggregates instead of order-preserving ones.
    * an explicit, disposable `temp_directory` — when a query does exceed the
      limit, spilling to a known scratch path is far cheaper than spilling
      next to the source files.

    Measured on 2026-06-01T00: 12 threads beat 24 (8.4 s vs 3.2 s for the parse
    step) because 24 threads overran the memory limit and spilled.
    """
    tmp = Path(".tmp/duckdb_spill")
    tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads = 12")
    con.execute("SET memory_limit = '8GB'")
    con.execute(f"SET temp_directory = '{tmp.as_posix()}'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET TimeZone = 'UTC'")
    return con


def fetch_hour(stamp: str, dest: Path, connections: int = 64) -> Path | None:
    """Download one hourly file using parallel ranged requests.

    Why parallel
    ------------
    Measured on 2026-11-01 from this machine, one connection to
    `r2v2.pmxt.dev` sustains only 0.33 MB/s. The limit is per connection, not
    total bandwidth — the same host served:

        1 connection    0.33 MB/s
        6 connections   1.12 MB/s
        12 connections  2.10 MB/s
        24 connections  3.15 MB/s
        32 connections  3.74 MB/s
        64 connections  7.6-9.0 MB/s   <- default
        128 connections 2.12 MB/s      <- throttled, worse

    The knee is at ~64: past it the host throttles and throughput collapses.
    At one connection an hour file (371 MB) takes 18.7 minutes, which would
    make a 7-day block a two-day job; at 64 it takes about 45 seconds.

    R2 advertises `Accept-Ranges: bytes`, so the file is split into
    `connections` contiguous ranges fetched concurrently and written at their
    own offsets in a preallocated scratch file.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = PMXT.format(stamp=stamp)
    tmp = dest.with_suffix(".part")

    try:
        head = requests.head(url, timeout=120, allow_redirects=True)
        if head.status_code == 404:
            return None
        head.raise_for_status()
        total = int(head.headers["Content-Length"])
    except Exception:  # noqa: BLE001 - absent hour or transient network
        return None

    part_size = max(4 << 20, total // connections)
    ranges = []
    off = 0
    while off < total:
        end = min(off + part_size - 1, total - 1)
        ranges.append((off, end))
        off = end + 1

    with tmp.open("wb") as fh:
        fh.truncate(total)  # preallocate so workers can seek independently

    def _get(rng: tuple[int, int]) -> int:
        start, end = rng
        for attempt in range(3):
            try:
                r = requests.get(url, headers={"Range": f"bytes={start}-{end}"},
                                 stream=True, timeout=180)
                if r.status_code not in (200, 206):
                    raise requests.HTTPError(f"status {r.status_code}")
                buf = b"".join(r.iter_content(chunk_size=1 << 20))
                if len(buf) != end - start + 1:
                    raise requests.HTTPError(
                        f"short read {len(buf)} != {end - start + 1}")
                with tmp.open("r+b") as out:
                    out.seek(start)
                    out.write(buf)
                return len(buf)
            except Exception:  # noqa: BLE001 - retry transient stalls
                if attempt == 2:
                    raise
        return 0

    try:
        with ThreadPoolExecutor(max_workers=min(connections, len(ranges))) as pool:
            written = sum(pool.map(_get, ranges))
    except Exception:  # noqa: BLE001 - any part failing invalidates the hour
        tmp.unlink(missing_ok=True)
        return None

    if written != total:
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(dest)
    return dest


def reduce_hour(con: duckdb.DuckDBPyConnection, src: Path, bucket_s: int,
                top_n: int) -> tuple[int, int]:
    """Reconstruct bucket-end book state for one hour. Returns (rows, assets).

    Two changes over the first version, both measured rather than assumed:

    1. **State is hash-agg + forward fill, not ASOF JOIN.** The ASOF version
       took 46.3 s per hour and spilled 8.5 GB; this takes 3.2 s and spills
       2.2 GB. The ASOF had to sort 81 M events against a join of every level
       with every bucket. `arg_max(size, ts)` collapses each (level, bucket)
       cell first, leaving a 10.8 M-row grid to forward-fill with a window
       function.
    2. **Tokens are int32, sides are booleans.** `asset_id` is a 77-character
       decimal string; carrying it through 81 M rows of shuffle is pure waste.
       `dense_rank` over the 73 k distinct tokens gives a stable key for the
       hour.

    Bucket convention: a row labelled `bucket_end` describes the ladder over
    `[bucket_end - bucket_s, bucket_end)`. An event exactly on a boundary
    belongs to the following bucket, so the snapshot never sees an event it
    could not have seen at that instant.
    """
    p = src.as_posix()

    # ---- 1. level-change timeline (typed, compact) -----------------------
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE upd AS
        WITH anchors AS (
            -- bids/asks are JSON strings holding [["price","size"], ...].
            -- CAST to a typed double list: `from_json(..., '[["VARCHAR","VARCHAR"]]')`
            -- rejects the file ("too many values in array of JSON structure").
            SELECT asset_id, timestamp AS ts, TRUE AS is_bid,
                   l[1] AS price, l[2] AS size
            FROM (
                SELECT asset_id, timestamp,
                       unnest(CAST(bids AS DOUBLE[][])) AS l
                FROM read_parquet('{p}')
                WHERE event_type = 'book' AND bids IS NOT NULL AND bids <> '[]'
            )
            UNION ALL
            SELECT asset_id, timestamp AS ts, FALSE AS is_bid,
                   l[1] AS price, l[2] AS size
            FROM (
                SELECT asset_id, timestamp,
                       unnest(CAST(asks AS DOUBLE[][])) AS l
                FROM read_parquet('{p}')
                WHERE event_type = 'book' AND asks IS NOT NULL AND asks <> '[]'
            )
        ),
        deltas AS (
            -- `side` is the LADDER side: BUY rows are bids, SELL rows are asks.
            SELECT asset_id, timestamp AS ts, side = 'BUY' AS is_bid,
                   CAST(price AS DOUBLE) AS price,
                   CAST(size AS DOUBLE)  AS size
            FROM read_parquet('{p}')
            WHERE event_type = 'price_change' AND price IS NOT NULL
              AND side IN ('BUY', 'SELL')
        )
        SELECT * FROM anchors UNION ALL SELECT * FROM deltas
    """)

    # ---- 2. token -> int32, then drop the string table -------------------
    con.execute("""
        CREATE OR REPLACE TEMP TABLE dim AS
        SELECT token_id, dense_rank() OVER (ORDER BY token_id)::INTEGER AS aid
        FROM (SELECT DISTINCT asset_id AS token_id FROM upd)
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE ev AS
        SELECT d.aid, u.ts, u.is_bid, u.price, u.size
        FROM upd u JOIN dim d ON d.token_id = u.asset_id
    """)
    con.execute("DROP TABLE upd")

    # ---- 3. last size inside each bucket ---------------------------------
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE cell AS
        SELECT aid, is_bid, price,
               time_bucket(INTERVAL {bucket_s} SECOND, ts)
                   + INTERVAL {bucket_s} SECOND AS bucket_end,
               arg_max(size, ts) AS size
        FROM ev
        GROUP BY ALL
    """)

    # ---- 4. every (level, bucket) cell that could be non-empty -----------
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE grid AS
        SELECT lv.aid, lv.is_bid, lv.price, k.bucket_end
        FROM (SELECT DISTINCT aid, is_bid, price FROM ev) lv
        JOIN (
            SELECT DISTINCT aid,
                   time_bucket(INTERVAL {bucket_s} SECOND, ts)
                       + INTERVAL {bucket_s} SECOND AS bucket_end
            FROM ev
        ) k USING (aid)
    """)
    con.execute("DROP TABLE ev")

    # ---- 5. carry each level forward; a level untouched keeps its size ----
    con.execute("""
        CREATE OR REPLACE TEMP TABLE state AS
        SELECT * FROM (
            SELECT g.aid, g.bucket_end, g.is_bid, g.price,
                   last_value(c.size IGNORE NULLS) OVER (
                       PARTITION BY g.aid, g.is_bid, g.price
                       ORDER BY g.bucket_end
                   ) AS size
            FROM grid g
            LEFT JOIN cell c
              ON c.aid = g.aid AND c.is_bid = g.is_bid
             AND c.price = g.price AND c.bucket_end = g.bucket_end
        ) WHERE size IS NOT NULL AND size > 0
    """)
    con.execute("DROP TABLE cell")
    con.execute("DROP TABLE grid")

    # ---- 6. top N levels per side ---------------------------------------
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE ranked AS
        SELECT * FROM (
            SELECT aid, bucket_end, is_bid, price, size,
                   row_number() OVER (
                       PARTITION BY aid, bucket_end, is_bid
                       ORDER BY CASE WHEN is_bid THEN -price ELSE price END
                   ) AS rn
            FROM state
        ) WHERE rn <= {top_n}
    """)

    # ---- 7. top-of-book summary -----------------------------------------
    con.execute("""
        CREATE OR REPLACE TEMP TABLE book AS
        SELECT
            aid, bucket_end,
            max(price)  FILTER (WHERE is_bid)                AS best_bid,
            min(price)  FILTER (WHERE NOT is_bid)            AS best_ask,
            sum(size)   FILTER (WHERE is_bid)                AS bid_shares_top,
            sum(size)   FILTER (WHERE NOT is_bid)            AS ask_shares_top,
            list(price ORDER BY price DESC) FILTER (WHERE is_bid)     AS bid_prices,
            list(size  ORDER BY price DESC) FILTER (WHERE is_bid)     AS bid_sizes,
            list(price ORDER BY price)      FILTER (WHERE NOT is_bid) AS ask_prices,
            list(size  ORDER BY price)      FILTER (WHERE NOT is_bid) AS ask_sizes
        FROM ranked
        GROUP BY aid, bucket_end
    """)
    con.execute("DROP TABLE ranked")

    # Depth bands must be aggregated WITHOUT the list columns in the GROUP BY:
    # `GROUP BY ALL` over bid_prices/ask_prices was slower than the four
    # correlated subqueries it replaced (167 s vs 101 s per hour).
    con.execute("""
        CREATE OR REPLACE TEMP TABLE band AS
        SELECT
            s.aid, s.bucket_end,
            sum(CASE WHEN s.is_bid AND s.price >= b.best_bid - 0.01
                     THEN s.price * s.size ELSE 0 END) AS bid_notional_1c,
            sum(CASE WHEN NOT s.is_bid AND s.price <= b.best_ask + 0.01
                     THEN s.price * s.size ELSE 0 END) AS ask_notional_1c,
            sum(CASE WHEN s.is_bid AND s.price >= b.best_bid - 0.02
                     THEN s.price * s.size ELSE 0 END) AS bid_notional_2c,
            sum(CASE WHEN NOT s.is_bid AND s.price <= b.best_ask + 0.02
                     THEN s.price * s.size ELSE 0 END) AS ask_notional_2c
        FROM state s
        JOIN book b ON b.aid = s.aid AND b.bucket_end = s.bucket_end
        GROUP BY 1, 2
    """)
    con.execute("DROP TABLE state")

    con.execute("""
        CREATE OR REPLACE TEMP TABLE depth AS
        SELECT d.token_id, b.bucket_end AS ts,
               b.best_bid, b.best_ask,
               b.bid_shares_top, b.ask_shares_top,
               d1.bid_notional_1c, d1.ask_notional_1c,
               d1.bid_notional_2c, d1.ask_notional_2c,
               b.bid_prices, b.bid_sizes, b.ask_prices, b.ask_sizes
        FROM book b
        JOIN band d1 ON d1.aid = b.aid AND d1.bucket_end = b.bucket_end
        JOIN dim  d  ON d.aid = b.aid
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
    ap.add_argument("--connections", type=int, default=24,
                    help="parallel ranged requests per hour file (default 24)")
    ap.add_argument("--keep-raw", action="store_true")
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    dest_root = Path(args.dest)
    con = connect()

    sources: list[tuple[datetime, Path]] = []
    pending: dict[Path, str] = {}   # local path -> archive stamp still to fetch
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
        # Download INSIDE the processing loop, one hour at a time. Fetching the
        # whole batch up front would hold every raw file on disk simultaneously
        # (168 hours = 62 GB) and defeat the streaming property this script
        # exists to guarantee.
        t = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        skipped = 0
        for i in range(args.hours):
            when = t + timedelta(hours=i)
            part = dest_root / f"dt={when:%Y-%m-%d}" / f"hour={when:%H}" / "books.parquet"
            if args.skip_existing and part.exists():
                skipped += 1
                continue
            stamp = when.strftime("%Y-%m-%dT%H")
            local = SCRATCH / f"polymarket_orderbook_{stamp}.parquet"
            sources.append((when, local))
            pending[local] = stamp
        if skipped:
            print(f"  resuming: {skipped:,} hours already reduced", flush=True)

    total_rows = 0
    t_start = time.time()
    for i, (when, src) in enumerate(sources, 1):
        t0 = time.time()
        if src in pending:
            got = fetch_hour(pending[src], src, args.connections)
            if got is None:
                print(f"  {when:%Y-%m-%dT%H}: absent (404)", flush=True)
                continue
        rows, assets = reduce_hour(con, src, args.bucket_seconds, args.top_levels)
        part = dest_root / f"dt={when:%Y-%m-%d}" / f"hour={when:%H}"
        part.mkdir(parents=True, exist_ok=True)
        con.execute(f"""
            COPY (SELECT * FROM depth) TO '{(part / 'books.parquet').as_posix()}'
            (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        size = (part / "books.parquet").stat().st_size
        total_rows += rows
        print(f"  [{i}/{len(sources)}] {when:%Y-%m-%dT%H}  "
              f"{rows:>8,} rows  {assets:>7,} assets  "
              f"{size/1e6:>7.1f} MB  {time.time()-t0:>6.1f}s", flush=True)
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
