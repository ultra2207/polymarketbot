"""Profile wallets from the trade tape: what behaviour, not just what PnL.

Why the tape and not the leaderboard
------------------------------------
`wallet_cohort.parquet` ranks wallets by *today's* PnL, which is survivorship-biased and
says nothing about how they trade. The tape says how: how often, at what size, how much of
it rests versus crosses, how many markets, and whether the timing looks human.

That distinction is the whole of Track A. The cohort tells us *whom* to study; this tells us
*what they do*, and the answer is meant to be mostly negative - a list of behaviours that
are not replicable at our size.

The four behaviours that matter
-------------------------------
1. **Maker share.** Fraction of a wallet's volume that rested rather than crossed. The
   cohort already showed maker wallets draw 67.7 % of PnL from rebates, so this separates
   "earns the fee schedule" from "earns the forecast".
2. **Median notional.** Size dependence. A wallet trading $50 k clips is not a competitor
   at $250, and its behaviour is not evidence about our market.
3. **Market breadth.** Distinct conditions. A wallet in 40 markets is a specialist; one in
   5,000 is a screener. Different mechanisms, different replicability.
4. **Timing regularity.** Inter-trade intervals. A tight median interval with low variance
   is a bot; humans are bursty. This is a cheap latency proxy - we cannot measure reaction
   time, but we can see whether the behaviour is mechanical.

No forecast is made here and no PnL is computed: the tape does not carry per-wallet cost
basis, so a "profitable wallet" claim from it would be a guess. This is description only.

Usage
    uv run scripts/profile_wallets_tape.py --start 2026-01-01 --end 2026-10-01
    uv run scripts/profile_wallets_tape.py --min-trades 50 --top 500
"""

from __future__ import annotations

import argparse
import sys
import time
from calendar import monthrange
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "src")

import duckdb
from rich.console import Console
from rich.progress import (BarColumn, Progress, SpinnerColumn,
                           TaskProgressColumn, TextColumn, TimeElapsedColumn,
                           TimeRemainingColumn)

OUT = Path("data/processed")
TAPE = "data/raw/sii/trades.parquet"


def epoch(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    # 15.6 GB total on this machine with the PMXT reducer also running. 12 threads
    # overran the limit and OOM'd on the first month; fewer threads and a smaller
    # cap are slower per chunk and actually finish.
    con.execute("SET threads = 6")
    con.execute("SET memory_limit = '5GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET temp_directory = '.tmp/duckdb_spill'")
    con.execute("SET TimeZone = 'UTC'")
    return con


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-10-01")
    ap.add_argument("--top-wallets", type=int, default=2000,
                    help="timing is computed only for this many largest wallets")
    ap.add_argument("--min-trades", type=int, default=200,
                    help="ignore wallets below this so medians are not dust")
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    lo, hi = epoch(args.start), epoch(args.end)
    con = connect()
    console = Console()

    # Work month by month. One monolithic GROUP BY over the whole window gives no
    # progress at all: a run either returns or nothing at all is known about it.
    # Monthly chunks have a bounded cost each, give the bar a real ETA, and keep peak
    # memory to one month of fills.
    months = []
    y, m = map(int, args.start.split("-")[:2])
    ey, em = map(int, args.end.split("-")[:2])
    while (y, m) <= (ey, em):
        m_lo = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())
        last = monthrange(y, m)[1]
        m_hi = int(datetime(y, m, last, 23, 59, 59, tzinfo=timezone.utc).timestamp()) + 1
        months.append((max(m_lo, lo), min(m_hi, hi)))
        m += 1
        if m > 12:
            y, m = y + 1, 1

    console.print(f"window {args.start} -> {args.end}: {len(months)} monthly chunks")

    con.execute(
        "CREATE OR REPLACE TEMP TABLE prof (wallet VARCHAR, fills BIGINT, "
        "volume DOUBLE, maker_share DOUBLE, hour_sd DOUBLE, mean_hour_utc DOUBLE)"
    )

    bar = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TextColumn("elapsed"),
        TimeRemainingColumn(),
        TextColumn("left"),
        console=console,
    )

    with bar:
        task = bar.add_task("profiling", total=len(months))
        for i, (m_lo, m_hi) in enumerate(months, 1):
            label = datetime.fromtimestamp(m_lo, tz=timezone.utc).strftime("%Y-%m")
            bar.update(task, description=f"{label} ({i}/{len(months)})")
            con.execute(f"""
                INSERT INTO prof
                SELECT w,
                       count(*)                                    AS fills,
                       sum(usd_amount)                             AS volume,
                       sum(CASE WHEN role = 'maker' THEN usd_amount ELSE 0 END)
                           / nullif(sum(usd_amount), 0)            AS maker_share,
                       stddev_samp(hour(to_timestamp(timestamp)))  AS hour_sd,
                       avg(hour(to_timestamp(timestamp)))          AS mean_hour_utc
                FROM (
                    SELECT CASE WHEN r.role = 'maker' THEN t.maker ELSE t.taker END AS w,
                           r.role, t.usd_amount, t.timestamp
                    FROM read_parquet('{TAPE}') t
                    CROSS JOIN (SELECT 'maker' AS role UNION ALL SELECT 'taker') r
                    WHERE t.timestamp >= {m_lo} AND t.timestamp < {m_hi}
                      AND t.usd_amount > 0
                      AND CASE WHEN r.role = 'maker' THEN t.maker ELSE t.taker END
                          IS NOT NULL
                )
                GROUP BY 1
            """)
            n = con.sql("SELECT count(*) FROM prof").fetchall()[0][0]
            bar.update(task, advance=1, description=f"{label} -> {n:,} wallet-months")

    console.print("merging monthly rows ...")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE merged AS
        SELECT wallet, sum(fills) AS fills,
               sum(volume) AS volume,
               sum(volume) / nullif(sum(fills), 0) AS mean_notional,
               sum(maker_share * volume) / nullif(sum(volume), 0) AS maker_share,
               max(hour_sd) AS hour_sd,
               max(mean_hour_utc) AS median_hour_utc
        FROM prof GROUP BY 1
        HAVING sum(fills) >= {args.min_trades}
    """)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE topw AS
        SELECT wallet FROM merged ORDER BY volume DESC LIMIT {args.top_wallets}
    """)
    n_top = con.sql("SELECT count(*) FROM topw").fetchall()[0][0]
    console.print(f"sketch pass and timing for the top {n_top:,} wallets by volume")

    # Sketch aggregates only over the top slice: ~2k groups instead of ~200k, which is
    # the difference between a few MB and a few GB of per-group state.
    with bar:
        t3 = bar.add_task("sketches", total=len(months))
        con.execute("CREATE OR REPLACE TEMP TABLE detail (wallet VARCHAR, markets BIGINT, "
                    "median_notional DOUBLE, active_hours BIGINT)")
        for i, (m_lo, m_hi) in enumerate(months, 1):
            label = datetime.fromtimestamp(m_lo, tz=timezone.utc).strftime("%Y-%m")
            bar.update(t3, description=f"sketch {label} ({i}/{len(months)})")
            con.execute(f"""
                INSERT INTO detail
                SELECT w,
                       approx_count_distinct(condition_id),
                       approx_quantile(usd_amount, 0.5),
                       approx_count_distinct(CAST(timestamp // 3600 AS BIGINT))
                FROM (
                    SELECT CASE WHEN r.role = 'maker' THEN t.maker ELSE t.taker END AS w,
                           r.role, t.usd_amount, t.condition_id, t.timestamp
                    FROM read_parquet('{TAPE}') t
                    CROSS JOIN (SELECT 'maker' AS role UNION ALL SELECT 'taker') r
                    WHERE t.timestamp >= {m_lo} AND t.timestamp < {m_hi}
                      AND t.usd_amount > 0
                      AND CASE WHEN r.role = 'maker' THEN t.maker ELSE t.taker END
                          IN (SELECT wallet FROM topw)
                )
                GROUP BY 1
            """)
            bar.update(t3, advance=1)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE detail_merged AS
        SELECT wallet, max(markets) AS markets_busiest_month,
               approx_quantile(median_notional, 0.5) AS median_notional,
               max(active_hours) AS active_hours_busiest_month
        FROM detail GROUP BY 1
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE merged2 AS
        SELECT m.*, d.markets_busiest_month, d.median_notional,
               d.active_hours_busiest_month
        FROM merged m LEFT JOIN detail_merged d USING (wallet)
    """)
    con.execute("DROP TABLE merged")
    con.execute("ALTER TABLE merged2 RENAME TO merged")


    with bar:
        task2 = bar.add_task("gaps", total=len(months))
        con.execute(
            "CREATE OR REPLACE TEMP TABLE gaps (wallet VARCHAR, "
            "median_gap_s DOUBLE, gap_cv DOUBLE, subsec_gaps BIGINT)"
        )
        for i, (m_lo, m_hi) in enumerate(months, 1):
            label = datetime.fromtimestamp(m_lo, tz=timezone.utc).strftime("%Y-%m")
            bar.update(task2, description=f"gaps {label} ({i}/{len(months)})")
            con.execute(f"""
                INSERT INTO gaps
                WITH f AS (
                    SELECT CASE WHEN r.role = 'maker' THEN t.maker ELSE t.taker END
                               AS wallet,
                           t.timestamp
                    FROM read_parquet('{TAPE}') t
                    CROSS JOIN (SELECT 'maker' AS role UNION ALL SELECT 'taker') r
                    WHERE t.timestamp >= {m_lo} AND t.timestamp < {m_hi}
                      AND CASE WHEN r.role = 'maker' THEN t.maker ELSE t.taker END
                          IS NOT NULL
                ),
                t AS (
                    SELECT f.wallet, f.timestamp,
                           f.timestamp - lag(f.timestamp) OVER (
                               PARTITION BY f.wallet ORDER BY f.timestamp) AS gap_s
                    FROM f JOIN topw USING (wallet)
                )
                SELECT wallet, approx_quantile(gap_s, 0.5),
                       stddev_samp(gap_s) / nullif(avg(gap_s), 0),
                       count(*) FILTER (WHERE gap_s <= 1)
                FROM t WHERE gap_s IS NOT NULL AND gap_s > 0
                GROUP BY 1
            """)
            bar.update(task2, advance=1)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE gaps_merged AS
        SELECT wallet, approx_quantile(median_gap_s, 0.5) AS median_gap_s,
               max(gap_cv) AS gap_cv, sum(subsec_gaps) AS subsec_gaps
        FROM gaps GROUP BY 1
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE final AS
        SELECT p.*, g.median_gap_s, g.gap_cv, g.subsec_gaps,
               CASE WHEN p.maker_share >= 0.7 THEN 'maker'
                    WHEN p.maker_share <= 0.3 THEN 'taker'
                    ELSE 'mixed' END AS role_mix,
               CASE WHEN g.median_gap_s IS NOT NULL AND g.median_gap_s < 60
                         AND coalesce(g.gap_cv, 9) < 1.5 THEN 'mechanical'
                    ELSE 'bursty' END AS rhythm
        FROM merged p LEFT JOIN gaps_merged g USING (wallet)
        WHERE p.volume > 0
    """)

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM final ORDER BY volume DESC)
        TO '{(dest / 'wallet_tape_profile.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    total = con.sql("SELECT count(*) FROM final").fetchall()[0][0]
    vol = con.sql("SELECT sum(volume) FROM final").fetchall()[0][0] or 0
    print(f"\nwallets profiled: {total:,}  covering ${vol/1e9:.2f} B of volume")
    print("markets = distinct markets in the busiest month (lower bound); "
          "medians are approx_quantile")

    print(f"\n{'role mix':10} {'wallets':>8} {'volume $m':>10} {'med notional':>13} "
          f"{'med markets':>12}")
    for rm, nm, v, mn, mm in con.sql("""
        SELECT role_mix, count(*), sum(volume)/1e6, median(median_notional),
               median(markets_busiest_month)
        FROM final GROUP BY 1 ORDER BY 3 DESC
    """).fetchall():
        print(f"{rm:10} {nm:>8,} {v:>10,.0f} {mn:>13,.0f} {mm:>12,.0f}")

    print(f"\n{'rhythm':10} {'wallets':>8} {'volume $m':>10} {'med gap s':>10}")
    for rh, nm, v, mg in con.sql("""
        SELECT rhythm, count(*), sum(volume)/1e6, median(median_gap_s)
        FROM final GROUP BY 1 ORDER BY 3 DESC
    """).fetchall():
        print(f"{rh:10} {nm:>8,} {v:>10,.0f} {mg or 0:>10,.0f}")

    print("\ntop 12 wallets by volume:")
    print(f"  {'maker%':>7} {'vol $m':>8} {'notional':>9} {'markets':>8} "
          f"{'gap s':>8} {'rhythm':>10}")
    for ms, v, mn, mk, gp, rh in con.sql("""
        SELECT maker_share*100, volume/1e6, median_notional, markets_busiest_month,
               median_gap_s, rhythm
        FROM final ORDER BY volume DESC LIMIT 12
    """).fetchall():
        print(f"  {ms:>6.0f}% {v:>8,.1f} {mn:>9,.0f} {mk:>8,.0f} "
              f"{gp or 0:>8,.0f} {rh:>10}")

    big = con.sql("""
        SELECT count(*), sum(volume)/1e6 FROM final WHERE median_notional >= 1000
    """).fetchall()[0]
    print(f"\nwallets whose MEDIAN fill is >= $1,000: {big[0]:,} "
          f"(${big[1]:,.0f} M of volume) - not competitors at our size")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
