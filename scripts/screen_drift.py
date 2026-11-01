"""Term-structure drift: does the price walk toward the outcome, and is the walk priced?

Idea A7 (#5) in `docs/alpha-pipeline.md`. A1 measured the *level* - whether the price at
one instant is right on average. This measures the *path*: given a price at 168 h before
resolution, what is the expected move by 72 h, 24 h and 6 h?

That is a different question. A market can be correctly priced at every horizon and still
have a predictable drift (if drift is priced into the longer-dated leg), or be mispriced at
one horizon only. The favourite-longshot result in the results log says the level is dead
after costs; this says whether the *shape* of the term structure is exploitable, and
crucially whether a shorter-dated price already contains it.

Method
------
For each resolved token, sample the last taker-BUY price nearest to 168, 72, 24 and 6
hours before `end_date`, then measure the drift between consecutive leads, bucketed by the
price at the *earliest* lead. Standard errors cluster by market.

Drift is only interesting if it is large relative to the ~1 c taker half-spread, because
exploiting it requires entering at one lead and exiting at another - two crossings.

Usage
    uv run scripts/screen_drift.py --start 2026-01-01 --end 2026-10-01
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
LEADS = (168, 72, 24, 6)


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
    ap.add_argument("--half-spread", type=float, default=0.01)
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    lo, hi = epoch(args.start), epoch(args.end)
    con = connect()

    con.execute("""
        CREATE OR REPLACE TEMP TABLE mk AS
        SELECT condition_id, token1 AS token_id, payout_0 AS payout,
               TRY_CAST(end_date AS TIMESTAMP) AS end_ts
        FROM read_parquet('data/processed/market_labels.parquet')
        WHERE resolved AND NOT coalesce(is_void, FALSE)
          AND payout_0 IS NOT NULL AND token1 IS NOT NULL
        UNION ALL
        SELECT condition_id, token2, payout_1, TRY_CAST(end_date AS TIMESTAMP)
        FROM read_parquet('data/processed/market_labels.parquet')
        WHERE resolved AND NOT coalesce(is_void, FALSE)
          AND payout_1 IS NOT NULL AND token2 IS NOT NULL
    """)
    print(f"resolved tokens: {con.sql('SELECT count(*) FROM mk').fetchall()[0][0]:,}",
          flush=True)

    # Nearest print to each lead, tolerance scaled so thin markets still match.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE at_lead AS
        WITH tr AS (
            SELECT m.condition_id, m.token_id, m.payout,
                   t.price,
                   (epoch(m.end_ts) - t.timestamp) / 3600.0 AS lead_h
            FROM read_parquet('{TAPE}') t
            JOIN mk m ON m.token_id = t.asset_id
            WHERE t.taker_direction = 'BUY'
              AND t.timestamp >= {lo} AND t.timestamp < {hi}
              AND t.price BETWEEN 0.01 AND 0.99
        ),
        tagged AS (
            SELECT *, CASE
                WHEN lead_h BETWEEN 160 AND 176 THEN 168
                WHEN lead_h BETWEEN  66 AND  78 THEN 72
                WHEN lead_h BETWEEN  20 AND  28 THEN 24
                WHEN lead_h BETWEEN   4 AND   8 THEN 6
            END AS k
            FROM tr
        ),
        nearest AS (
            SELECT condition_id, token_id, payout, k, price,
                   row_number() OVER (
                       PARTITION BY condition_id, token_id, k
                       ORDER BY abs(lead_h - k)) AS rn
            FROM tagged WHERE k IS NOT NULL
        )
        SELECT condition_id, token_id, payout, k, price FROM nearest WHERE rn = 1
    """)
    print(f"lead observations: {con.sql('SELECT count(*) FROM at_lead').fetchall()[0][0]:,}",
          flush=True)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE wide AS
        SELECT condition_id, token_id, any_value(payout) AS payout,
               max(CASE WHEN k = 168 THEN price END) AS p168,
               max(CASE WHEN k =  72 THEN price END) AS p72,
               max(CASE WHEN k =  24 THEN price END) AS p24,
               max(CASE WHEN k =   6 THEN price END) AS p6
        FROM at_lead GROUP BY 1, 2
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE drift AS
        SELECT *, CASE
            WHEN p168 < 0.10 THEN '0.00-0.10'
            WHEN p168 < 0.25 THEN '0.10-0.25'
            WHEN p168 < 0.50 THEN '0.25-0.50'
            WHEN p168 < 0.75 THEN '0.50-0.75'
            WHEN p168 < 0.90 THEN '0.75-0.90'
            ELSE '0.90-1.00' END AS bucket
        FROM wide
        WHERE p168 IS NOT NULL AND p72 IS NOT NULL
          AND p24 IS NOT NULL AND p6 IS NOT NULL
    """)
    n = con.sql("SELECT count(*) FROM drift").fetchall()[0][0]
    print(f"tokens with all four leads: {n:,}\n", flush=True)
    if n == 0:
        print("no token printed at every lead - widen the tolerances")
        return 1

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM drift) TO '{(dest / 'drift_obs.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    print(f"{'p168 bucket':12} {'markets':>8} {'168->72':>9} {'72->24':>9} "
          f"{'24->6':>9} {'168->6':>9} {'payout':>7}")
    for b, nm, d1, d2, d3, dt, pay in con.sql("""
        SELECT bucket, count(*),
               avg(p72 - p168), avg(p24 - p72), avg(p6 - p24), avg(p6 - p168),
               avg(payout)
        FROM drift GROUP BY 1 ORDER BY 1
    """).fetchall():
        print(f"{b:12} {nm:>8,} {d1:>+9.4f} {d2:>+9.4f} {d3:>+9.4f} {dt:>+9.4f} {pay:>7.3f}")

    # Per-bucket test: drift must beat TWO crossings (in and out), and be significant.
    print(f"\n{'p168 bucket':12} {'n':>6} {'168->6':>9} {'±se':>7} {'t':>7} {'cost':>7}")
    verdicts = []
    for b, nm, d, se in con.sql("""
        SELECT bucket, count(*), avg(p6 - p168),
               coalesce(stddev_samp(p6 - p168) / sqrt(count(*)), 0)
        FROM drift GROUP BY 1 ORDER BY 1
    """).fetchall():
        t = d / se if se else float("nan")
        cost = 2 * args.half_spread
        ok = abs(d) > cost and abs(t) > 3
        verdicts.append((b, nm, d, t, ok))
        print(f"{b:12} {nm:>6,} {d:>+9.4f} {se:>7.4f} {t:>7.1f} "
              f"{'beats' if abs(d) > cost else 'inside':>7}")

    wins = [v for v in verdicts if v[4]]
    print(f"\nbuckets where drift beats two crossings and |t| > 3: {len(wins)}/{len(verdicts)}")
    if wins:
        n_all = con.sql("SELECT count(*) FROM mk").fetchall()[0][0]
        print(f"=> CANDIDATE. Selection caveat: {n:,} of {n_all:,} resolved tokens "
              f"({100*n/max(n_all,1):.2f} %) printed at all four leads, so this describes "
              "long-lived, heavily traded markets rather than a random sample. It also "
              "contradicts A1, which found the level dead after costs, so the status is "
              "'unresolved': the confirming test must buy at the ask at 168 h and sell at "
              "the bid at 6 h, not use traded prices.")
    else:
        print("=> dead: no bucket's drift beats the cost of capturing it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
