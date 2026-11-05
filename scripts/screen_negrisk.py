"""Neg-risk coherence: mutually exclusive legs must sum to 1.

Idea A2 in `docs/alpha-pipeline.md`. In a neg-risk event the outcomes are mutually
exclusive and exactly one pays, so the YES prices across legs must sum to 1. They do not
always, because each leg is quoted independently.

Two bounds, and the second is the one usually ignored
-----------------------------------------------------
* **Buy-all-YES**: if `sum(ask_YES) < 1`, buy one YES of every leg; exactly one pays 1.
* **Buy-all-NO**: if `sum(ask_NO) < k - 1`, buy one NO of every leg. Every leg except the
  winner pays 1, so the payout is `k - 1`. Negative-risk conversion makes this real: a NO
  in every other leg converts into one YES.

Costs are the sum of per-leg fees plus, importantly, the fact that a leg with no quote is
not purchasable at all. Only events where **every** leg printed inside the same minute are
scored, so a "violation" cannot be manufactured by a missing leg.

Why this is stronger evidence than calibration screens
------------------------------------------------------
It is arithmetic on simultaneous quotes, not a forecast. Nothing here requires being right
about an outcome; it requires both sides of an inequality to be buyable at once.

What this still cannot see
--------------------------
Last-traded price is a lower bound on the ask only when a taker bought at that instant, and
it goes stale exactly where a violation would live - the same limitation that made E1
unconfirmable from the tape. A short persistence window is therefore reported, and any
survivor needs book confirmation.

Usage
    uv run scripts/screen_negrisk.py --start 2026-04-01 --end 2026-07-01
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "src")

from polymarketbot.logs import IST

import duckdb

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
    ap.add_argument("--start", default="2026-04-01")
    ap.add_argument("--end", default="2026-07-01")
    ap.add_argument("--min-usd", type=float, default=25.0)
    ap.add_argument("--min-net", type=float, default=0.005)
    ap.add_argument("--min-legs", type=int, default=3)
    ap.add_argument("--bucket-seconds", type=int, default=300,
                    help="legs must all quote inside this window; 60 is too strict "
                         "for 5+ leg events and yields a handful of snapshots")
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    lo, hi = epoch(args.start), epoch(args.end)
    con = connect()

    # Neg-risk events: the flag lives on the Gamma metadata, the grouping on the label.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE legs AS
        SELECT l.condition_id, l.event_id, l.token1 AS yes_token, l.token2 AS no_token
        FROM read_parquet('data/processed/market_labels.parquet') l
        JOIN read_parquet('data/processed/market_metadata_gamma.parquet') g
          ON g.condition_id = l.condition_id
        WHERE coalesce(g.neg_risk, FALSE)
          AND l.event_id IS NOT NULL
          AND l.token1 IS NOT NULL AND l.token2 IS NOT NULL
    """)
    n_legs = con.sql("SELECT count(*) FROM legs").fetchall()[0][0]
    n_events = con.sql("SELECT count(DISTINCT event_id) FROM legs").fetchall()[0][0]
    print(f"neg-risk legs: {n_legs:,} across {n_events:,} events", flush=True)

    # Last taker-BUY price per leg per minute, for both sides of each leg.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE px AS
        WITH L AS (
            SELECT condition_id, event_id, yes_token AS token_id, 'yes' AS side
            FROM legs
            UNION ALL
            SELECT condition_id, event_id, no_token, 'no' FROM legs
        ),
        t AS (
            SELECT l.event_id, l.condition_id, l.side, x.price, x.usd_amount,
                   x.timestamp, x.timestamp // {args.bucket_seconds} AS m
            FROM read_parquet('{TAPE}') x
            JOIN L l ON l.token_id = x.asset_id
            WHERE x.taker_direction = 'BUY'
              AND x.timestamp >= {lo} AND x.timestamp < {hi}
              AND x.price BETWEEN 0.01 AND 0.99
        ),
        ranked AS (
            SELECT *, row_number() OVER (
                PARTITION BY event_id, condition_id, side, m
                ORDER BY timestamp DESC) AS rn
            FROM t
        )
        SELECT event_id, condition_id, side, m, price, usd_amount
        FROM ranked WHERE rn = 1 AND usd_amount >= {args.min_usd}
    """)

    # Events where EVERY leg printed on that side inside the same minute.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE summ AS
        WITH need AS (
            SELECT event_id, count(*) AS k FROM legs GROUP BY 1
            HAVING count(*) >= {args.min_legs}
        ),
        agg AS (
            SELECT p.event_id, p.side, p.m,
                   sum(p.price) AS total,
                   count(*)     AS got,
                   count(DISTINCT p.condition_id) AS got_legs
            FROM px p GROUP BY 1, 2, 3
        )
        SELECT a.event_id, a.side, a.m, a.total, n.k,
               CASE WHEN a.side = 'yes' THEN 1.0 - a.total
                    ELSE (n.k - 1.0) - a.total END AS gross
        FROM agg a JOIN need n USING (event_id)
        WHERE a.got_legs = n.k
    """)

    n_sum = con.sql("SELECT count(*) FROM summ").fetchall()[0][0]
    print(f"complete simultaneous snapshots: {n_sum:,}", flush=True)
    if n_sum == 0:
        print("no event had every leg quoted inside one minute")
        return 1

    # Fees: one taker fee per leg actually bought.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE scored AS
        SELECT s.event_id, s.side, s.m, s.total, s.k, s.gross,
               coalesce(f.fee_total, 0) AS fee,
               s.gross - coalesce(f.fee_total, 0) AS net
        FROM summ s
        LEFT JOIN (
            SELECT p.event_id, p.side, p.m,
                   sum(CASE WHEN coalesce(g.fees_enabled, FALSE)
                            THEN coalesce(g.fee_rate, 0)
                                 * pow(p.price * (1 - p.price), coalesce(g.fee_exponent, 1))
                            ELSE 0 END) AS fee_total
            FROM px p
            LEFT JOIN read_parquet('data/processed/market_metadata_gamma.parquet') g
                   ON g.condition_id = p.condition_id
            GROUP BY 1, 2, 3
        ) f ON f.event_id = s.event_id AND f.side = s.side AND f.m = s.m
    """)

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM scored) TO '{(dest / 'negrisk_cells.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    print(f"\n{'side':5} {'snapshots':>10} {'gross>0':>9} {'net>min':>9} "
          f"{'best gross':>11} {'median legs':>12}")
    for side, n, gpos, over, best, k in con.sql(f"""
        SELECT side, count(*),
               sum(CASE WHEN gross > 0 THEN 1 ELSE 0 END),
               sum(CASE WHEN net > {args.min_net} THEN 1 ELSE 0 END),
               max(gross), median(k)
        FROM scored GROUP BY 1 ORDER BY 1
    """).fetchall():
        print(f"{side:5} {n:>10,} {gpos:>9,} {over:>9,} {best:>11.4f} {k:>12,.0f}")

    # Persistence, same discipline as E1: a blip is not an opportunity.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE runs AS
        WITH hit AS (
            SELECT event_id, side, m, net FROM scored WHERE net > {args.min_net}
        ),
        grp AS (
            SELECT *, m - row_number() OVER (
                PARTITION BY event_id, side ORDER BY m) AS g
            FROM hit
        )
        SELECT event_id, side, count(*) AS minutes, max(net) AS best_net
        FROM grp GROUP BY event_id, side, g
    """)
    r = con.sql("""
        SELECT count(*) FILTER (WHERE minutes >= 2),
               max(minutes), median(minutes)
        FROM runs
    """).fetchall()[0]
    print(f"\nruns lasting >= 2 minutes: {r[0]:,} "
          f"(longest {r[1] or 0} min, median {r[2] or 0:.0f})")

    print("\ntop net violations:")
    for ev, side, m, net, k in con.sql(f"""
        SELECT event_id, side, m, net, k FROM scored
        WHERE net > {args.min_net} ORDER BY net DESC LIMIT 10
    """).fetchall():
        ts = datetime.fromtimestamp(m * args.bucket_seconds, tz=IST).strftime("%Y-%m-%d %H:%M")
        print(f"  {ts} {side:3} net={net:+.4f} legs={k} {str(ev)[:20]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
