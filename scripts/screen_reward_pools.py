"""Liquidity-reward pools: what does quoting actually pay, per unit of capital?

Idea D1/E5 in `docs/alpha-pipeline.md`. Polymarket pays makers whose resting orders meet
`rewardsMinSize` and sit inside `rewardsMaxSpread`. Eligibility is a function of **quoting,
not of being filled**, which is why this is the one market-making structure that does not
need queue position.

The upper bound this computes
-----------------------------
The true payout is `(our qualifying size / total qualifying size) x rewardsDailyRate`, and
total qualifying size needs the book. So this takes the **best case**: assume we are the
only qualifier. That yields

    yield_ceiling = rewardsDailyRate / (rewardsMinSize x price)

If even the ceiling trails a risk-free rate, D1 is dead without touching the book. If the
ceiling is attractive, the book decides the real share.

Why the ceiling is the right first question
-------------------------------------------
The plan's kill criterion is "yield per unit capital trails a directional idea, or
qualifying size exceeds $250". Both are about capital efficiency, and neither needs to know
who else is quoting - the ceiling bounds both.

Data
----
`data/raw/gamma/snapshot_YYYY-MM-DD.parquet`, which now carries `rewards_daily_rate`,
`rewards_amount`, `rewardsMinSize` and `rewardsMaxSpread` per market. Snapshots before
2026-11-01 lack these fields and cannot answer the question.

Usage
    uv run scripts/screen_reward_pools.py
    uv run scripts/screen_reward_pools.py --date 2026-11-01
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
SNAPS = "data/raw/gamma/snapshot_*.parquet"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="default: the latest snapshot")
    ap.add_argument("--min-spread", type=float, default=0.02,
                    help="closest distance from mid we would quote, in price units")
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    files = sorted(Path("data/raw/gamma").glob("snapshot_*.parquet"))
    if not files:
        log.info("no gamma snapshots - run scripts/snapshot_gamma.py first")
        return 1
    use = ([f for f in files if args.date and args.date in f.name] or files)[-1]
    log.info(f"snapshot: {use.name}", )

    con = duckdb.connect()
    con.execute("SET threads = 8")
    con.execute("SET TimeZone = 'UTC'")
    src = use.as_posix()

    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{src}'").fetchall()}
    if "rewards_daily_rate" not in cols:
        log.info(f"{use.name} predates the reward columns; only 2026-11-01 onwards can be used")
        return 1

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pools AS
        SELECT conditionId AS condition_id, slug, category, volume, liquidity,
               coalesce(rewards_daily_rate, 0)  AS daily_rate,
               coalesce(rewardsMinSize, 0)      AS min_size,
               coalesce(rewardsMaxSpread, 0)    AS max_spread,
               coalesce(competitive, 0)         AS competitive
        FROM read_parquet('{src}')
        WHERE coalesce(rewards_daily_rate, 0) > 0
    """)

    n_all = con.sql(f"SELECT count(*) FROM read_parquet('{src}')").fetchall()[0][0]
    n_pool = con.sql("SELECT count(*) FROM pools").fetchall()[0][0]
    total = con.sql("SELECT sum(daily_rate) FROM pools").fetchall()[0][0] or 0.0
    log.info(f"markets snapshotted: {n_all:,}")
    log.info(f"markets with a reward pool: {n_pool:,} ({100*n_pool/max(n_all,1):.1f} %)")
    log.info(f"total paid out per day across pools: ${total:,.0f}")

    if n_pool == 0:
        return 0

    log.info(f"\n{'category':16} {'pools':>6} {'$/day':>10} {'med rate':>9} "
          f"{'med minSize':>12} {'med maxSpread':>14}")
    for cat, n, s, med, ms, sp in con.sql("""
        SELECT coalesce(category,'?'), count(*), sum(daily_rate),
               median(daily_rate), median(min_size), median(max_spread)
        FROM pools GROUP BY 1 ORDER BY 2 DESC
    """).fetchall():
        log.info(f"{str(cat)[:16]:16} {n:>6,} {s:>10,.0f} {med:>9,.1f} "
              f"{ms:>12,.0f} {sp:>14,.1f}")

    # The ceiling is degenerate and must not be read as a return.
    #
    # rewardsDailyRate is split pro-rata across everyone who qualifies, so "sole
    # qualifier at the minimum size" trivially captures the whole pool: a $3/day pool
    # with a 20-share minimum at $0.50 implies a 5,475 % annual return. That number is
    # arithmetically correct and analytically useless - it says nothing about what a
    # real quoter earns, because it holds constant the one thing that matters.
    #
    # What decides the yield is TOTAL QUALIFYING SIZE inside the reward band, which
    # only the order book can supply. So this screen reports the reward side
    # descriptively and defers the yield to the book.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE ceiling AS
        SELECT *, daily_rate AS usd_per_day,
               CASE WHEN min_size > 0 THEN 2 * min_size * 0.5 END AS capital_at_min,
               CASE WHEN min_size > 0
                    THEN daily_rate / nullif(2 * min_size * 0.5, 0) END AS daily_yield_at_min
        FROM pools
    """)

    r = con.sql("""
        SELECT count(*), median(min_size), median(max_spread), median(daily_rate),
               quantile_cont(daily_rate, 0.90), max(daily_rate)
        FROM ceiling WHERE min_size > 0
    """).fetchall()[0]
    log.info(f"\npools with a size requirement: {r[0]:,}")
    log.info(f"median minimum qualifying size: {r[1]:,.0f} shares")
    log.info(f"median max spread:              {r[2]:,.1f} cents")
    log.info(f"daily rate: median ${r[3]:,.1f}   p90 ${r[4]:,.1f}   max ${r[5]:,.1f}")

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM ceiling)
        TO '{(dest / 'reward_pools.parquet').as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    log.info("\nlargest pools by daily rate:")
    log.info(f"  {'$/day':>8} {'minSize':>8} {'maxSpread':>10} {'competitive':>12}")
    for dr, ms, sp, cp in con.sql("""
        SELECT daily_rate, min_size, max_spread, competitive
        FROM ceiling ORDER BY daily_rate DESC LIMIT 12
    """).fetchall():
        log.info(f"  {dr:>8,.1f} {ms:>8,.0f} {sp:>10,.1f} {cp:>12.2f}")

    log.info("\nThe yield CANNOT be computed from this snapshot. Pro-rata payout means the "
          "return is set by total qualifying size inside the reward band, which needs the "
          "order book. A minimum-size quote technically captures an entire pool, which is "
          "why no ceiling-based ranking is printed here - it would rank pools by size "
          "requirements, not by opportunity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
