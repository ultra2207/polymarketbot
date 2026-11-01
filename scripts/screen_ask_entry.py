"""Executable-entry screen: buy at the quoted ask, not at the traded VWAP.

Why this exists
---------------
`docs/results-log.md` records that idea #1 (longshot / favourite bias) is dead
*after costs*, with one explicit caveat:

    What would change the verdict: measuring the quoted ask at entry rather than
    the VWAP (needs the book, i.e. 2026-04-13 onward), and a 2026-only window.

The traded VWAP is roughly the mid. A taker pays the ask, and the median quoted
spread is 2c, so roughly half of that is the difference between the two
measurements - about 1 pp of notional on a 0.85 favourite. That is the same
order as the surviving effect, which is why this test decides the idea rather
than merely refining it.

Method
------
1. Each resolved binary market contributes TWO observations, one per token,
   because either leg can be bought.
2. Entry is the **last book snapshot** inside `[end_date - 24h, end_date - 6h]`.
   Price paid is `best_ask`: the cheapest price at which a taker can buy that
   token at that instant.
3. `net_edge = payout - best_ask - fee_per_share`, with the fee from
   `schedule_at`, so a 2026 fill is priced at 2026 rates.
4. Standard errors are computed over **markets, not tokens**. The two tokens of
   a market are perfectly anti-correlated, so treating them as independent
   observations would understate the interval by roughly sqrt(2).

What this still cannot see
--------------------------
`best_ask` is the top of book. It is executable only up to `ask_shares_top`
shares, which is reported alongside so a result cannot be read as scalable
without checking it. Depth beyond the touch needs the full ladder.

Usage
    uv run scripts/screen_ask_entry.py
    uv run scripts/screen_ask_entry.py --book-dir data/processed/pmxt_reduced
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "src")

import duckdb

from polymarketbot.fees import schedule_at

OUT = Path("data/processed")
BUCKETS = [(0.00, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35),
           (0.35, 0.50), (0.50, 0.65), (0.65, 0.80), (0.80, 0.90),
           (0.90, 0.95), (0.95, 0.98), (0.98, 1.0001)]


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 8")
    con.execute("SET memory_limit = '6GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET TimeZone = 'UTC'")
    return con


def build_entries(con: duckdb.DuckDBPyConnection, book_glob: str,
                  max_h: int, min_h: int) -> None:
    """One row per (market, token): the executable entry and its payout."""
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE entries AS
        WITH tok AS (
            SELECT condition_id, token1 AS token_id, payout_0 AS payout,
                   TRY_CAST(end_date AS TIMESTAMP) AS end_ts, created_at
            FROM read_parquet('data/processed/market_labels.parquet')
            WHERE NOT coalesce(is_void, FALSE) AND resolved
              AND payout_0 IS NOT NULL AND token1 IS NOT NULL
            UNION ALL
            SELECT condition_id, token2 AS token_id, payout_1 AS payout,
                   TRY_CAST(end_date AS TIMESTAMP) AS end_ts, created_at
            FROM read_parquet('data/processed/market_labels.parquet')
            WHERE NOT coalesce(is_void, FALSE) AND resolved
              AND payout_1 IS NOT NULL AND token2 IS NOT NULL
        ),
        joined AS (
            SELECT t.condition_id, t.token_id, t.payout, t.end_ts, t.created_at,
                   b.ts, b.best_ask, b.ask_shares_top, b.ask_notional_2c,
                   row_number() OVER (
                       PARTITION BY t.condition_id, t.token_id
                       ORDER BY b.ts DESC
                   ) AS rn
            FROM tok t
            JOIN read_parquet('{book_glob}') b
              ON b.token_id = t.token_id
             AND b.ts >  t.end_ts - INTERVAL {max_h} HOUR
             AND b.ts <= t.end_ts - INTERVAL {min_h} HOUR
            WHERE b.best_ask IS NOT NULL AND b.best_ask > 0 AND b.best_ask < 1
        )
        SELECT condition_id, token_id, payout, end_ts, created_at,
               ts AS entry_ts, best_ask AS entry_price,
               ask_shares_top, ask_notional_2c,
               (end_ts - ts) AS lead_time
        FROM joined WHERE rn = 1
    """)


def attach_fees(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE OR REPLACE TEMP TABLE priced AS
        SELECT e.*, c.category,
               m.fee_type, m.fee_rate, m.fee_exponent, m.fee_rebate
        FROM entries e
        LEFT JOIN read_parquet('data/processed/market_categories.parquet') c
               ON c.condition_id = e.condition_id
        LEFT JOIN read_parquet('data/processed/market_metadata_gamma.parquet') m
               ON m.condition_id = e.condition_id
    """)


def score(con: duckdb.DuckDBPyConnection, dest: Path) -> None:
    rows = con.execute("""
        SELECT condition_id, entry_price, payout, ask_shares_top,
               coalesce(category, 'other'), fee_type, fee_rate,
               fee_exponent, fee_rebate, created_at
        FROM priced
    """).fetchall()

    scored: list[tuple] = []
    for (cid, price, payout, size, cat, ftype, frate, fexp, freb, created) in rows:
        when = created or datetime.now(timezone.utc).isoformat()
        sched = schedule_at({"category": cat, "fee_type": ftype,
                             "fee_rate": frate, "fee_exponent": fexp,
                             "fee_rebate": freb}, when)
        # Taker fee is charged on the curve evaluated at the execution price.
        curve = price * (1.0 - price)
        fee_per_share = sched.rate * (curve ** sched.exponent) if sched.rate > 0 else 0.0
        scored.append((cid, price, payout, size, fee_per_share,
                       (payout - price) - fee_per_share))

    con.execute("CREATE OR REPLACE TEMP TABLE scored (condition_id VARCHAR, "
                "entry_price DOUBLE, payout DOUBLE, ask_shares_top DOUBLE, "
                "fee_per_share DOUBLE, net_edge DOUBLE)")
    con.executemany("INSERT INTO scored VALUES (?,?,?,?,?,?)", scored)

    con.execute(f"""
        COPY (SELECT * FROM scored) TO '{(dest / 'ask_entry_observations.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    # Market-level first, then across markets: the two legs are anti-correlated.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE buckets AS
        SELECT * FROM (VALUES {','.join(f"({lo},{hi})" for lo, hi in BUCKETS)})
            AS t(lo, hi)
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE market_level AS
        SELECT b.lo, b.hi, s.condition_id,
               avg(s.entry_price) AS px, avg(s.payout) AS pay,
               avg(s.fee_per_share) AS fee, avg(s.net_edge) AS edge,
               avg(s.ask_shares_top) AS size
        FROM scored s
        JOIN buckets b ON s.entry_price >= b.lo AND s.entry_price < b.hi
        GROUP BY 1, 2, 3
    """)
    con.execute(f"""
        COPY (SELECT * FROM market_level) TO '{(dest / 'ask_entry_by_market.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    print(f"\n{'price':>12} {'markets':>8} {'mean px':>8} {'payout':>7} "
          f"{'fee':>7} {'net edge':>9} {'±se':>7} {'t':>6} {'ask size':>9}")
    for lo, hi, n, px, pay, fee, edge, se, size in con.sql("""
        SELECT lo, hi, count(*) n,
               avg(px), avg(pay), avg(fee), avg(edge),
               coalesce(stddev_samp(edge) / sqrt(count(*)), 0) se,
               median(size)
        FROM market_level GROUP BY 1,2 ORDER BY 1
    """).fetchall():
        t = edge / se if se else float("nan")
        print(f"  [{lo:.2f},{hi:.2f}) {n:>8,} {px:>8.3f} {pay:>7.3f} "
              f"{fee*100:>6.2f}% {edge:>+9.4f} {se:>7.4f} {t:>6.1f} {size:>9,.0f}")

    total = con.sql("""
        SELECT count(DISTINCT condition_id) FROM scored
    """).fetchall()[0][0]
    print(f"\nobservations: {con.sql('SELECT count(*) FROM scored').fetchall()[0][0]:,}"
          f" across {total:,} markets")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-dir", default="data/processed/pmxt_reduced")
    ap.add_argument("--entry-max-hours", type=int, default=24)
    ap.add_argument("--entry-min-hours", type=int, default=6)
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    book_root = Path(args.book_dir)
    parts = sorted(book_root.rglob("books.parquet"))
    if not parts:
        print(f"no reduced book partitions under {book_root}")
        return 1
    print(f"book partitions: {len(parts):,}")

    glob = f"{(book_root / '**' / 'books.parquet').as_posix()}"
    con = connect()
    t0 = datetime.now(timezone.utc)
    build_entries(con, glob, args.entry_max_hours, args.entry_min_hours)
    n = con.sql("SELECT count(*) FROM entries").fetchall()[0][0]
    print(f"entry observations: {n:,}  ({datetime.now(timezone.utc)-t0})")
    if n == 0:
        print("no markets resolved inside the book window - the block is too short "
              "or no market both traded and resolved with a snapshot 6-24 h before "
              "resolution")
        return 1
    attach_fees(con)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    score(con, dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
