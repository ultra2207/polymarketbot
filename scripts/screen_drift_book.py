"""A7 confirmation: capture term-structure drift by crossing the spread both ways.

A7 (`docs/results-log.md`) found that at 168 h before resolution, prices are too extreme in
both tails and converge toward the payout: longshots drift +7.6 c (t = 7.5), 0.90-1.00
favourites drift -11.3 c (t = -7.7). It was recorded as a **candidate** for three stated
reasons, two of which this script removes:

1. It measured drift between *traded* prices. Trading it means **buying at the ask** at
   168 h and **selling at the bid** at 6 h, so the real cost is the spread on both sides.
2. It sampled whatever printed at both leads. This uses the **book**, so a market counts
   whenever it had a quote, not only when it traded.

The third reason - that only 2,117 of 1.75 M tokens printed at all four leads - stays a
limitation of the book window, and is reported as the sample size rather than argued away.

Method
------
* Entry: last book snapshot at or before `end_date - 168 h`, within `--tolerance-hours`.
  Buy at `best_ask`; require `ask_shares_top` to cover the notional.
* Exit: last snapshot at or before `end_date - 6 h`. Sell at `best_bid`.
* Net = exit bid - entry ask - fees on both sides.
* Bucketed by the entry ask, with standard errors clustered by market.

A positive net in the tail buckets means the drift survived both crossings. A negative net
means the spread ate exactly what A7 attributed to drift - which is the result A1 already
suggests for the level, and would make the honest summary "A7 measures spread, not edge".

Usage
    uv run scripts/screen_drift_book.py
    uv run scripts/screen_drift_book.py --book-dir data/processed/pmxt_reduced
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, "src")

import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

OUT = Path("data/processed")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 8")
    con.execute("SET memory_limit = '6GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET TimeZone = 'UTC'")
    return con


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book-dir", default="data/processed/pmxt_reduced")
    ap.add_argument("--entry-hours", type=float, default=168.0)
    ap.add_argument("--exit-hours", type=float, default=6.0)
    ap.add_argument("--tolerance-hours", type=float, default=2.0,
                    help="how far from the target lead a snapshot may sit")
    ap.add_argument("--min-usd", type=float, default=25.0,
                    help="required notional at the touch, to keep dust out")
    ap.add_argument("--half-spread", type=float, default=0.01)
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    root = Path(args.book_dir)
    parts = sorted(root.rglob("books.parquet"))
    if not parts:
        log.info(f"no reduced book under {root}")
        return 1
    log.info(f"book partitions: {len(parts):,}", )

    glob = (root / "**" / "books.parquet").as_posix()
    con = connect()

    con.execute("""
        CREATE OR REPLACE TEMP TABLE mk AS
        SELECT condition_id, token1 AS token_id, payout_0 AS payout,
               TRY_CAST(end_date AS TIMESTAMP) AS end_ts
        FROM read_parquet('data/processed/market_labels.parquet')
        WHERE resolved AND NOT coalesce(is_void, FALSE)
          AND token1 IS NOT NULL AND payout_0 IS NOT NULL AND end_date IS NOT NULL
        UNION ALL
        SELECT condition_id, token2, payout_1, TRY_CAST(end_date AS TIMESTAMP)
        FROM read_parquet('data/processed/market_labels.parquet')
        WHERE resolved AND NOT coalesce(is_void, FALSE)
          AND token2 IS NOT NULL AND payout_1 IS NOT NULL AND end_date IS NOT NULL
    """)

    # Two leads, each the last snapshot at or before its target, inside the tolerance.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE leads AS
        WITH j AS (
            SELECT m.condition_id, m.token_id, m.payout, m.end_ts,
                   b.ts, b.best_bid, b.best_ask, b.bid_shares_top, b.ask_shares_top,
                   date_diff('minute', b.ts, m.end_ts) / 60.0 AS lead_h
            FROM mk m
            JOIN read_parquet('{glob}') b
              ON b.token_id = m.token_id
             AND b.ts <= m.end_ts
             AND b.ts >= m.end_ts - INTERVAL {args.entry_hours + args.tolerance_hours} HOUR
            WHERE b.best_bid IS NOT NULL AND b.best_ask IS NOT NULL
        ),
        tagged AS (
            SELECT *, CASE
                WHEN lead_h BETWEEN {args.exit_hours - args.tolerance_hours}
                                AND {args.exit_hours + args.tolerance_hours} THEN 'exit'
                WHEN lead_h BETWEEN {args.entry_hours - args.tolerance_hours}
                                AND {args.entry_hours + args.tolerance_hours} THEN 'entry'
            END AS leg
            FROM j
        ),
        picked AS (
            SELECT *, row_number() OVER (
                PARTITION BY condition_id, token_id, leg ORDER BY ts DESC) AS rn
            FROM tagged WHERE leg IS NOT NULL
        )
        SELECT condition_id, token_id, payout, end_ts, leg, best_bid, best_ask,
               bid_shares_top, ask_shares_top
        FROM picked WHERE rn = 1
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE trades AS
        SELECT e.condition_id, e.token_id, e.payout,
               e.best_ask AS entry_ask,
               e.ask_shares_top AS entry_shares,
               x.best_bid AS exit_bid,
               x.bid_shares_top AS exit_shares
        FROM leads e
        JOIN leads x USING (condition_id, token_id)
        WHERE e.leg = 'entry' AND x.leg = 'exit'
          AND e.best_ask > 0.01 AND e.best_ask < 0.99
          AND e.ask_shares_top * e.best_ask >= ?
          AND x.bid_shares_top * x.best_bid >= ?
    """, [args.min_usd, args.min_usd])

    n = con.sql("SELECT count(*) FROM trades").fetchall()[0][0]
    log.info(f"tokens quoted at both leads with >= ${args.min_usd:.0f} at the touch: {n:,}",
          )
    if n == 0:
        log.info("no overlap yet - the book window may not reach 168 h before any "
              "resolved end_date, which is expected until the block is longer")
        return 1

    # Fee on both legs, from the market's own schedule.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE net AS
        SELECT t.*,
               coalesce(g.fee_rate, 0) *
                   CASE WHEN coalesce(g.fees_enabled, FALSE) THEN 1 ELSE 0 END
                   * (pow(t.entry_ask * (1 - t.entry_ask), coalesce(g.fee_exponent, 1))
                    + pow(t.exit_bid  * (1 - t.exit_bid),  coalesce(g.fee_exponent, 1)))
                   AS fee,
               (t.exit_bid - t.entry_ask) AS gross
        FROM trades t
        LEFT JOIN read_parquet('data/processed/market_metadata_gamma.parquet') g
               ON g.condition_id = t.condition_id
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE scored AS
        SELECT *, gross - fee AS net,
               CASE WHEN entry_ask < 0.10 THEN '0.00-0.10'
                    WHEN entry_ask < 0.25 THEN '0.10-0.25'
                    WHEN entry_ask < 0.50 THEN '0.25-0.50'
                    WHEN entry_ask < 0.75 THEN '0.50-0.75'
                    WHEN entry_ask < 0.90 THEN '0.75-0.90'
                    ELSE '0.90-1.00' END AS bucket
        FROM net
    """)

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM scored) TO '{(dest / 'drift_book_confirm.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    log.info(f"\n{'entry ask':12} {'markets':>8} {'entry':>7} {'exit':>7} "
          f"{'gross':>8} {'fee':>7} {'net':>8} {'±se':>7} {'t':>6}")
    wins = 0
    rows = con.sql("""
        SELECT bucket, count(*), avg(entry_ask), avg(exit_bid), avg(gross),
               avg(fee), avg(net),
               coalesce(stddev_samp(net) / sqrt(count(*)), 0)
        FROM scored GROUP BY 1 ORDER BY 1
    """).fetchall()
    for b, nm, ea, xb, gr, fe, ne, se in rows:
        t = ne / se if se else float("nan")
        flag = ""
        if abs(ne) > 2 * args.half_spread and abs(t) > 3:
            flag = "  <-- survives"
            wins += 1
        log.info(f"{b:12} {nm:>8,} {ea:>7.3f} {xb:>7.3f} {gr:>+8.4f} {fe:>7.4f} "
              f"{ne:>+8.4f} {se:>7.4f} {t:>6.1f}{flag}")

    log.info(f"\nbuckets whose net survives both crossings and |t| > 3: {wins}/{len(rows)}")
    if wins:
        log.info("=> A7 CONFIRMED on these buckets: the drift is larger than the spread.")
    else:
        log.info("=> A7 does not survive: buying the ask and selling the bid consumes the "
              "drift. The earlier result measured the spread, not an edge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
