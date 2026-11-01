"""Lifecycle-stage inefficiency: does calibration error depend on how old the market is?

Idea A5 in `docs/alpha-pipeline.md`. The stored screen rows already hold a per-market
entry VWAP, its payout and its fee, so the only thing missing is the market's **age at
entry**, which is a join away.

Why age could matter
--------------------
A market created and resolved inside a day has had almost no time for information to
arrive; a market that has been open for a month has been priced, argued over and
arbitraged repeatedly. If mispricing is a function of time-under-scrutiny, calibration
error should shrink with age. If it does *not*, then "new markets are inefficient" is
false and one whole family of ideas is removed.

Method
------
* Entry is the stored T-24 h -> T-6 h slot, so time-to-resolution is roughly constant by
  construction. Age is the only variable that moves.
* Age buckets are powers of two so each bucket spans the same width in log time.
* Standard errors are clustered by market: the two tokens of a market are anti-correlated.

Kill criterion (from the pipeline): **flat across age**.

Usage
    uv run scripts/screen_lifecycle.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, "src")

import duckdb

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
    ap.add_argument("--half-spread", type=float, default=0.01,
                    help="taker cost above the mid; the median quoted spread is ~2c")
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    con = connect()

    # Entry happens at a known offset from end_date; use the midpoint of the slot.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE obs AS
        SELECT r.condition_id, r.token_id, r.vwap, r.payout, r.net_edge,
               r.fee_frac, r.category,
               TRY_CAST(l.created_at AS TIMESTAMP) AS created_ts,
               TRY_CAST(l.end_date   AS TIMESTAMP) AS end_ts,
               date_diff('hour',
                   TRY_CAST(l.created_at AS TIMESTAMP),
                   TRY_CAST(l.end_date AS TIMESTAMP) - INTERVAL 15 HOUR) AS age_hours,
               date_diff('hour',
                   TRY_CAST(l.created_at AS TIMESTAMP),
                   TRY_CAST(l.end_date AS TIMESTAMP)) AS life_hours
        FROM read_parquet('data/processed/screen_calibration_rows.parquet') r
        JOIN read_parquet('data/processed/market_labels.parquet') l
          ON l.condition_id = r.condition_id
        WHERE NOT coalesce(r.is_void, FALSE)
          AND r.vwap IS NOT NULL AND r.payout IS NOT NULL
    """)

    n = con.sql("SELECT count(*) FROM obs").fetchall()[0][0]
    print(f"observations with resolvable age: {n:,}")
    if n == 0:
        print("no rows joined - check created_at/end_date formats")
        return 1

    neg = con.sql("SELECT count(*) FROM obs WHERE age_hours < 0").fetchall()[0][0]
    print(f"   of which age < 0 (end before creation, dropped): {neg:,}")

    con.execute("""
        CREATE OR REPLACE TEMP TABLE binned AS
        SELECT *, CASE
            WHEN age_hours <  24 THEN '< 1 d'
            WHEN age_hours <  72 THEN '1-3 d'
            WHEN age_hours < 168 THEN '3-7 d'
            WHEN age_hours < 720 THEN '7-30 d'
            ELSE '> 30 d' END AS age_bin
        FROM obs WHERE age_hours >= 0
    """)

    # Cluster by market before computing standard errors.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE by_market AS
        SELECT age_bin, condition_id,
               avg(vwap) AS px, avg(payout) AS pay,
               avg(net_edge) AS net, avg(fee_frac) AS fee,
               any_value(life_hours) AS life_hours
        FROM binned GROUP BY 1, 2
    """)

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM by_market) TO '{(dest / 'lifecycle_by_market.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    print(f"\n{'age at entry':12} {'markets':>8} {'mean px':>8} {'payout':>7} "
          f"{'calib gap':>10} {'±se':>7} {'net edge':>9} {'±se':>7} {'med life':>9}")
    for bin_, n_m, px, pay, gap, gse, net, nse, life in con.sql("""
        SELECT age_bin, count(*), avg(px), avg(pay),
               avg(pay - px),
               coalesce(stddev_samp(pay - px) / sqrt(count(*)), 0),
               avg(net),
               coalesce(stddev_samp(net) / sqrt(count(*)), 0),
               median(life_hours)
        FROM by_market GROUP BY 1
        ORDER BY CASE age_bin WHEN '< 1 d' THEN 1 WHEN '1-3 d' THEN 2
                              WHEN '3-7 d' THEN 3 WHEN '7-30 d' THEN 4 ELSE 5 END
    """).fetchall():
        print(f"{bin_:12} {n_m:>8,} {px:>8.3f} {pay:>7.3f} {gap:>+10.4f} "
              f"{gse:>7.4f} {net:>+9.4f} {nse:>7.4f} {life:>9,.0f}")

    # Two questions, both required before this is interesting:
    # (1) is the pattern monotone in age, and (2) does any bin clear the taker cost?
    bins = con.sql("""
        SELECT age_bin, count(*), avg(pay - px) gap,
               coalesce(stddev_samp(pay - px) / sqrt(count(*)), 0) se
        FROM by_market GROUP BY 1
        ORDER BY CASE age_bin WHEN '< 1 d' THEN 1 WHEN '1-3 d' THEN 2
                              WHEN '3-7 d' THEN 3 WHEN '7-30 d' THEN 4 ELSE 5 END
    """).fetchall()
    gaps = [b[2] for b in bins]
    deltas = [gaps[i + 1] - gaps[i] for i in range(len(gaps) - 1)]
    monotone = all(d <= 0 for d in deltas) or all(d >= 0 for d in deltas)
    best = max(gaps)
    print(f"\nmonotone in age: {monotone} (deltas {['%+.4f' % d for d in deltas]})")
    print(f"largest gap {best:+.4f} vs taker half-spread {args.half_spread:.4f}")

    if best < args.half_spread:
        print("=> DEAD: every age bin sits inside the ~1c taker half-spread. "
              "Age does not predict a tradable error.")
    elif not monotone:
        print("=> NOT AN AGE EFFECT: the gap is non-monotone, so it is a composition "
              "artefact of which markets live in each bucket, not a lifecycle signal.")
    else:
        print("=> candidate: monotone and above cost - check spreads before believing it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
