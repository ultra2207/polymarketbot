"""Where does the top decile's selection edge live? By category and market size.

Setup
-----
The selection gap from `report.py` is real: at the same price bucket the top decile's buys
resolve better than everyone else's (gap up to +0.10 in the 70-85 % range, and negative in
longshots). Payout is a property of the token, so at an identical price the only way two
groups can differ is by **buying different tokens**. This script asks which tokens.

Method
------
Join every position to its market (category, volume, end date), then compare the realised
payout rate of top-decile buys against everyone else's, within the same category and price
bucket. Same price, same category, different selection.

Smoke test: `--limit 3000000` runs the identical SQL on a 3 M-row slice in a few seconds.
Full window: omit `--limit`.
"""

from __future__ import annotations

import argparse

import duckdb

from polymarketbot.logs import get_logger
log = get_logger(__name__)

POSITIONS = "data/processed/positions/*.parquet"
MARKETS = "data/processed/markets_pit.parquet"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="row cap for the smoke test")
    ap.add_argument("--min-positions", type=int, default=20000)
    args = ap.parse_args()

    logger = get_logger("analyse_categories")
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='10GB'")
    con.execute("SET temp_directory='data/tmp_duck'")

    logger.info("building token -> market map")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE tok AS
        SELECT token1 AS asset_id, coalesce(category,'other') AS category, volume
        FROM read_parquet('{MARKETS}') WHERE token1 IS NOT NULL
        UNION ALL
        SELECT token2, coalesce(category,'other'), volume
        FROM read_parquet('{MARKETS}') WHERE token2 IS NOT NULL
    """)
    logger.info(f"  tokens: {con.sql('SELECT count(*) FROM tok').fetchall()[0][0]:,}")

    src = (
        f"(SELECT * FROM read_parquet('{POSITIONS}') LIMIT {args.limit})"
        if args.limit
        else f"read_parquet('{POSITIONS}')"
    )

    logger.info("aggregating by category and price bucket")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE agg AS
        SELECT t.category,
               least(19, greatest(0, floor(20 * p.bc / nullif(p.bs,0))))::INT AS pbucket,
               CASE WHEN p.dec = 1 THEN 1 ELSE 0 END AS top,
               count(*) AS n,
               sum(p.bs) AS bs,
               sum(p.bc) AS bc,
               sum(p.bs * p.payout) AS payout_bs
        FROM {src} p
        JOIN tok t ON t.asset_id = p.asset_id
        WHERE p.payout IS NOT NULL AND p.bs > 0 AND p.dec IS NOT NULL
        GROUP BY 1, 2, 3
    """)

    log.info("\n=== payout minus price, by category (volume-weighted over all buckets) ===")
    log.info("  category            top pos   top edge | rest pos   rest edge |    gap")
    for r in con.execute("""
        SELECT category,
               sum(n) FILTER (WHERE top = 1), 
               (sum(payout_bs) FILTER (WHERE top = 1) - sum(bc) FILTER (WHERE top = 1))
                 / nullif(sum(bs) FILTER (WHERE top = 1), 0),
               sum(n) FILTER (WHERE top = 0),
               (sum(payout_bs) FILTER (WHERE top = 0) - sum(bc) FILTER (WHERE top = 0))
                 / nullif(sum(bs) FILTER (WHERE top = 0), 0)
        FROM agg
        GROUP BY category
        HAVING sum(n) >= ?
        ORDER BY sum(n) DESC
    """, [args.min_positions]).fetchall():
        if r[2] is None or r[4] is None:
            continue
        log.info(f"  {r[0]:<18} {r[1]:>9,}  {r[2]:>+8.4f} | {r[3]:>9,}  {r[4]:>+9.4f} | {r[2]-r[4]:>+8.4f}")

    log.info("\n=== the same, for the 70-85 % price band only ===")
    log.info("  category            top pos   top edge | rest pos   rest edge |    gap")
    for r in con.execute("""
        SELECT category,
               sum(n) FILTER (WHERE top = 1),
               (sum(payout_bs) FILTER (WHERE top = 1) - sum(bc) FILTER (WHERE top = 1))
                 / nullif(sum(bs) FILTER (WHERE top = 1), 0),
               sum(n) FILTER (WHERE top = 0),
               (sum(payout_bs) FILTER (WHERE top = 0) - sum(bc) FILTER (WHERE top = 0))
                 / nullif(sum(bs) FILTER (WHERE top = 0), 0)
        FROM agg WHERE pbucket BETWEEN 14 AND 16
        GROUP BY category
        HAVING sum(n) >= ?
        ORDER BY sum(n) DESC
    """, [args.min_positions]).fetchall():
        if r[2] is None or r[4] is None:
            continue
        log.info(f"  {r[0]:<18} {r[1]:>9,}  {r[2]:>+8.4f} | {r[3]:>9,}  {r[4]:>+9.4f} | {r[2]-r[4]:>+8.4f}")


if __name__ == "__main__":
    main()
