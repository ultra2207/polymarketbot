"""Complete-set arbitrage within one market: ask(YES) + ask(NO) < 1.

Idea E1 in `docs/alpha-pipeline.md`. If both legs can be bought for less than $1 they
merge back into $1, so the trade needs no forecast and no speed - only fees to clear.

The trap, found on the first pass
---------------------------------
A naive filter of "last taker-BUY price per leg, per minute, summed under 1" reported
12.8 % of all minute-buckets as arbitrage, with a minimum sum of 0.011. Those were dust
trades on near-dead markets: `(0.001, 0.010)` for $5.79, `(0.010, 0.030)` for $0.66. A
last-traded price is not a quotable ask, and it is not available in size. Two filters are
therefore mandatory, and both are conservative:

* **size** - the leg's own trade must be at least `--min-usd`, so a $0.31 print cannot
  manufacture a 98 % "edge";
* **price band** - both legs inside `[--min-price, 1 - --min-price]`. At 0.001 a sum below
  1 is guaranteed by resolution mechanics rather than by mispricing.

Persistence is measured, not assumed: an opportunity that vanishes inside a minute is not
executable. Each qualifying condition records the length of its longest consecutive run.

What this still cannot see
--------------------------
Last-traded price is a lower bound on the ask only if a taker bought at that instant.
Real confirmation needs the book, which is what the PMXT block provides. This screen
therefore produces *candidates with fees netted*, and B1/B2 confirm them.

Usage
    uv run scripts/screen_complete_set.py --start 2026-01-01 --end 2026-10-01
    uv run scripts/screen_complete_set.py --min-usd 50 --min-net 0.01
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
TAPE = "data/raw/sii/trades.parquet"


def epoch(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 12")
    con.execute("SET memory_limit = '8GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET TimeZone = 'UTC'")
    return con


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-10-01")
    ap.add_argument("--min-usd", type=float, default=25.0,
                    help="minimum notional on each leg for the print to count")
    ap.add_argument("--min-price", type=float, default=0.02,
                    help="both legs must sit inside [p, 1-p]")
    ap.add_argument("--min-net", type=float, default=0.005,
                    help="net-of-fee profit per share required to count")
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    lo, hi = epoch(args.start), epoch(args.end)
    con = connect()

    print(f"scanning {args.start} -> {args.end} "
          f"(min_usd={args.min_usd}, band={args.min_price})", flush=True)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE cell AS
        WITH b AS (
            SELECT condition_id, asset_id, price, timestamp, usd_amount,
                   timestamp // 60 AS m
            FROM read_parquet('{TAPE}')
            WHERE taker_direction = 'BUY'
              AND timestamp >= {lo} AND timestamp < {hi}
              AND price BETWEEN {args.min_price} AND {1 - args.min_price}
        ),
        last AS (
            SELECT condition_id, asset_id, m, price, usd_amount,
                   row_number() OVER (
                       PARTITION BY condition_id, asset_id, m
                       ORDER BY timestamp DESC) AS rn
            FROM b
        )
        SELECT condition_id, m,
               sum(price)          AS ask_sum,
               min(price)          AS p_min,
               max(price)          AS p_max,
               count(*)            AS legs,
               min(usd_amount)     AS usd_min_leg,
               sum(usd_amount)     AS usd_total
        FROM last WHERE rn = 1
        GROUP BY 1, 2
        HAVING count(*) = 2 AND min(usd_amount) >= {args.min_usd}
    """)

    n_cell = con.sql("SELECT count(*) FROM cell").fetchall()[0][0]
    print(f"quoteable minute-cells: {n_cell:,}", flush=True)
    if n_cell == 0:
        print("nothing passed the size and price filters")
        return 1

    # Fee per leg, from the market's own schedule.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE meta AS
        SELECT c.condition_id, c.m, c.ask_sum, c.p_min, c.p_max, c.usd_total,
               coalesce(cat.category, 'other') AS category,
               m.fee_type, m.fee_rate, m.fee_exponent, m.fee_rebate
        FROM cell c
        LEFT JOIN read_parquet('data/processed/market_categories.parquet') cat
               ON cat.condition_id = c.condition_id
        LEFT JOIN read_parquet('data/processed/market_metadata_gamma.parquet') m
               ON m.condition_id = c.condition_id
    """)

    # Fees are computed in SQL, not in a per-row Python loop. At ~1.6 M cells the
    # schedule_at() loop took longer than the whole tape scan it followed.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE scored AS
        SELECT condition_id, m, ask_sum, p_min, p_max, usd_total, category,
               coalesce(fee, 0) AS fee,
               1.0 - ask_sum - coalesce(fee, 0) AS net
        FROM (
            SELECT c.condition_id, c.m, c.ask_sum, c.p_min, c.p_max, c.usd_total,
                   coalesce(cat.category, 'other') AS category,
                   CASE WHEN coalesce(g.fees_enabled, FALSE)
                        THEN coalesce(g.fee_rate, 0) * (
                             pow(c.p_min * (1 - c.p_min), coalesce(g.fee_exponent, 1))
                           + pow(c.p_max * (1 - c.p_max), coalesce(g.fee_exponent, 1)))
                        ELSE 0 END AS fee
            FROM cell c
            LEFT JOIN read_parquet('data/processed/market_categories.parquet') cat
                   ON cat.condition_id = c.condition_id
            LEFT JOIN read_parquet('data/processed/market_metadata_gamma.parquet') g
                   ON g.condition_id = c.condition_id
        )
    """)

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM scored) TO '{(dest / 'complete_set_cells.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    # Persistence: longest consecutive run of seconds in which the state held.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE runs AS
        WITH hit AS (
            SELECT condition_id, m, net, ask_sum, category
            FROM scored WHERE net > {args.min_net}
        ),
        grp AS (
            SELECT *, m - row_number() OVER (PARTITION BY condition_id ORDER BY m) AS g
            FROM hit
        )
        SELECT condition_id, count(*) AS minutes, max(net) AS best_net,
               sum(1.0 - ask_sum) AS gross_per_share, any_value(category) AS category
        FROM grp GROUP BY condition_id, g
    """)
    con.execute(f"""
        COPY (SELECT * FROM runs) TO '{(dest / 'complete_set_runs.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    print()
    print(f"{'category':22} {'cells':>9} {'net>0':>8} {'net>min':>8} "
          f"{'best net':>9} {'med usd':>9}")
    for cat, cells, pos, over_min, best, usd in con.sql(f"""
        SELECT s.category, count(*),
               sum(CASE WHEN s.net > 0 THEN 1 ELSE 0 END),
               sum(CASE WHEN s.net > {args.min_net} THEN 1 ELSE 0 END),
               max(s.net), median(s.usd_total)
        FROM scored s GROUP BY 1 ORDER BY count(*) DESC
    """).fetchall():
        print(f"{cat:22} {cells:>9,} {pos:>8,} {over_min:>8,} {best:>9.4f} {usd:>9,.0f}")

    summary = con.sql(f"""
        SELECT count(*) n_runs, sum(minutes) mins,
               max(minutes) longest,
               median(minutes) med_len
        FROM runs WHERE minutes >= 2
    """).fetchall()[0]
    print(f"\nruns lasting >= 2 consecutive minutes: {summary[0]:,} "
          f"(longest {summary[2] or 0} min, median {summary[3] or 0:.0f} min)")

    print("\ntop candidates by net edge:")
    for cid, m, net, mins, usd, cat in con.sql(f"""
        SELECT s.condition_id, s.m, s.net, r.minutes, s.usd_total, s.category
        FROM scored s LEFT JOIN runs r ON r.condition_id = s.condition_id
        WHERE s.net > {args.min_net} ORDER BY s.net DESC LIMIT 12
    """).fetchall():
        ts = datetime.fromtimestamp(m * 60, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"  {ts} {cat[:16]:16} net={net:+.4f} run={mins} min  "
              f"usd={usd:>7,.0f}  {cid[:14]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
