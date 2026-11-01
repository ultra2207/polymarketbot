"""Ladder monotonicity: probability must be monotone in the strike.

Idea A3 (#3) in `docs/alpha-pipeline.md`. A ladder is a set of markets in one event that
differ only by a threshold - `bitcoin-above-100k`, `-above-105k`, ... - so the YES
probability must be **non-increasing** as the strike rises. An inversion means two legs
contradict each other, and the pair can be traded against the ladder.

Data check first
----------------
3,454 events carry at least three `above-` markets, covering 33,423 resolved markets, so
this is not a rare structure. Slugs encode the strike as `0pt03` (0.03), `108k` (108,000)
or `109pt5k` (109,500); the parser below handles all three forms.

Method
------
* All legs are priced from the last taker-BUY price inside the **same** one-hour window,
  `[end_date - 24 h, end_date - 23 h]`, so the comparison is simultaneous by construction.
* Legs are ordered by strike. A violation is a pair where the higher strike trades at a
  **higher** price than the lower one.
* Only legs whose own print carried at least `--min-usd` are compared, because a leg you
  cannot buy is not a tradable inversion.

Kill criterion (from the pipeline): **≤ 1 tick, < 1 min, or 1-share size only**. A tick is
0.01 on nearly every market, so an inversion must exceed 1 ¢ to count at all.

Usage
    uv run scripts/screen_ladder.py --start 2026-01-01 --end 2026-10-01
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "src")

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
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-10-01")
    ap.add_argument("--min-usd", type=float, default=25.0)
    ap.add_argument("--tick", type=float, default=0.01)
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    lo, hi = epoch(args.start), epoch(args.end)
    con = connect()

    # Strike parsing: `109pt5k` -> 109.5 * 1000, `108k` -> 108000, `0pt03` -> 0.03.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE strike AS
        WITH raw AS (
            SELECT condition_id, event_id, slug, token1, end_date,
                   regexp_extract(slug, '-above-([0-9ptk]+)', 1) AS tok
            FROM read_parquet('data/processed/market_labels.parquet')
            WHERE resolved AND NOT coalesce(is_void, FALSE)
              AND slug LIKE '%-above-%' AND event_id IS NOT NULL
              AND token1 IS NOT NULL AND end_date IS NOT NULL
        )
        SELECT condition_id, event_id, slug, token1, end_date, tok,
               TRY_CAST(
                   CASE WHEN tok LIKE '%k'
                        THEN replace(replace(tok, 'pt', '.'), 'k', '')
                        ELSE replace(tok, 'pt', '.') END AS DOUBLE)
               * CASE WHEN tok LIKE '%k' THEN 1000 ELSE 1 END AS strike,
               TRY_CAST(end_date AS TIMESTAMP) AS end_ts
        FROM raw WHERE tok <> ''
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE ladder AS
        SELECT s.* FROM strike s
        JOIN (SELECT event_id FROM strike GROUP BY 1 HAVING count(*) >= 3) e
          USING (event_id)
        WHERE s.strike IS NOT NULL
    """)
    n_mk = con.sql("SELECT count(*) FROM ladder").fetchall()[0][0]
    n_ev = con.sql("SELECT count(DISTINCT event_id) FROM ladder").fetchall()[0][0]
    print(f"ladder legs: {n_mk:,} across {n_ev:,} events", flush=True)

    # One price per leg, inside a single hour, 24-23 h before end_date.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE px AS
        WITH t AS (
            SELECT l.condition_id, l.event_id, l.strike, l.token1,
                   x.price, x.usd_amount, x.timestamp,
                   row_number() OVER (
                       PARTITION BY l.condition_id ORDER BY x.timestamp DESC) AS rn
            FROM ladder l
            JOIN read_parquet('{TAPE}') x ON x.asset_id = l.token1
            WHERE x.taker_direction = 'BUY'
              AND x.timestamp >= {lo} AND x.timestamp < {hi}
              AND x.price BETWEEN 0.01 AND 0.99
              AND x.timestamp >= epoch(l.end_ts) - 86400
              AND x.timestamp <  epoch(l.end_ts) - 82800
        )
        SELECT condition_id, event_id, strike, price, usd_amount
        FROM t WHERE rn = 1 AND usd_amount >= {args.min_usd}
    """)

    n_px = con.sql("SELECT count(*) FROM px").fetchall()[0][0]
    print(f"legs priced in the common hour with >= ${args.min_usd:.0f}: {n_px:,}", flush=True)
    if n_px < 3:
        print("too few priced legs inside the common hour")
        return 1

    # A violation is an adjacent pair where the higher strike is priced higher.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pairs AS
        SELECT event_id,
               count(*) FILTER (WHERE inv > {args.tick})     AS n_viol,
               count(*)                                      AS n_pairs,
               max(inv)                                      AS worst_inv,
               sum(CASE WHEN inv > {args.tick} THEN inv ELSE 0 END) AS total_inv
        FROM (
            SELECT event_id,
                   lead(price) OVER (PARTITION BY event_id ORDER BY strike) - price AS inv
            FROM px
        )
        WHERE inv IS NOT NULL
        GROUP BY 1
    """)

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM pairs) TO '{(dest / 'ladder_pairs.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    tot = con.sql("""
        SELECT count(*), sum(n_pairs), sum(n_viol),
               count(*) FILTER (WHERE n_viol > 0),
               max(worst_inv), avg(worst_inv) FILTER (WHERE n_viol > 0)
        FROM pairs
    """).fetchall()[0]
    n_ev_priced, n_pairs, n_viol, n_ev_viol, worst, mean_worst = tot
    print(f"\nevents with >= 3 priced legs: {n_ev_priced:,}")
    print(f"adjacent strike pairs:        {n_pairs:,}")
    print(f"inversions > {args.tick:.2f}:            {n_viol:,} "
          f"({100 * n_viol / max(n_pairs, 1):.2f} % of pairs)")
    print(f"events containing one:        {n_ev_viol:,} "
          f"({100 * n_ev_viol / max(n_ev_priced, 1):.1f} %)")
    print(f"worst inversion:              {worst:.4f}  "
          f"(mean of worst-per-event {mean_worst or 0:.4f})")

    if n_viol == 0:
        print("\n=> DEAD: no inversion exceeds one tick on a simultaneous, buyable quote")
        return 0

    print("\nlargest inversions:")
    for ev, s_lo, s_hi, p_lo, p_hi, inv in con.sql("""
        WITH o AS (
            SELECT event_id, strike, price,
                   lead(strike) OVER (PARTITION BY event_id ORDER BY strike) AS s_hi,
                   lead(price)  OVER (PARTITION BY event_id ORDER BY strike) AS p_hi
            FROM px
        )
        SELECT event_id, strike, s_hi, price, p_hi, p_hi - price
        FROM o WHERE p_hi - price > ? ORDER BY 6 DESC LIMIT 10
    """, [args.tick]).fetchall():
        print(f"  event={str(ev)[:12]} strikes {s_lo:>10,.2f} -> {s_hi:>10,.2f}  "
              f"prices {p_lo:.3f} -> {p_hi:.3f}  inversion {inv:+.4f}")
    print("\nNote: an inversion is only tradable if the pair can be bought against the "
          "rest of the ladder at size; that needs resting depth, i.e. the book.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
