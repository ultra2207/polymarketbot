"""Position lifecycle: do the biggest wallets hold to resolution, or close out?

Optimisation notes (AGENTS.md, SQL discipline)
----------------------------------------------
- Deciles are computed on `wallets` (~500 k rows), never on the joined position table
  (~250 M rows). A window function placed after a large join sorts every position row.
- The top decile is filtered into its own tiny table *before* positions are joined, so the
  second query never sees the other 90 %.
- Only the columns used are projected; buckets are integers and are formatted in Python.
- Each stage logs its row count so the reduction is observable.

Definitions
-----------
For a (wallet, token) position over the window:

    bs, ss        = shares bought / sold
    closed        = 2 * min(bs, ss) / (bs + ss)     1.0 = fully round-tripped, 0 = never sold
    buy_only      = ss = 0                            bought and never sold -> held
    sell_only     = bs = 0                            sold without buying -> minted/transferred
    mixed         = both sides

`buy_only` is the honest holding measure. `sell_only` is a market maker who minted a complete
set and sold one leg, which looks like an open position in the tape but is not one.
"""

from __future__ import annotations

import argparse

import duckdb

from polymarketbot.logs import get_logger
log = get_logger(__name__)

CACHE = "data/processed/wallet_token/*.parquet"
LABELS = "data/processed/market_labels.parquet"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=CACHE, help="parquet glob; one month for a smoke test")
    args = ap.parse_args()
    cache = args.cache
    logger = get_logger("analyse_positions")
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='10GB'")
    con.execute("SET temp_directory='data/tmp_duck'")

    logger.info("stage 1: collapse months -> (wallet, token) positions")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pr AS
        SELECT wallet, asset_id,
               sum(coalesce(m_buy_sh,0)  + coalesce(t_buy_sh,0))  AS bs,
               sum(coalesce(m_sell_sh,0) + coalesce(t_sell_sh,0)) AS ss,
               sum(coalesce(m_buy_cash,0)  + coalesce(t_buy_cash,0))  AS bc,
               sum(coalesce(m_sell_cash,0) + coalesce(t_sell_cash,0)) AS sc,
               sum(coalesce(m_buy_cash,0)  + coalesce(t_buy_cash,0)
                 + coalesce(m_sell_cash,0) + coalesce(t_sell_cash,0)) AS vol
        FROM read_parquet('{cache}')
        GROUP BY 1, 2
    """)
    logger.info(f"  positions: {con.sql('SELECT count(*) FROM pr').fetchall()[0][0]:,}")

    logger.info("stage 2: wallet totals (small) and deciles on the small table")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE rw AS
        SELECT wallet, vol, ntile(10) OVER (ORDER BY vol DESC) AS dec
        FROM (SELECT wallet, sum(vol) AS vol FROM pr GROUP BY 1)
        WHERE vol >= 1000
    """)
    logger.info(f"  ranked wallets: {con.sql('SELECT count(*) FROM rw').fetchall()[0][0]:,}")

    log.info("\n=== share of traded shares by position type, per wallet-volume decile ===")
    log.info("  dec   buy_only%   sell_only%   mixed%   mixed closed")
    for r in con.sql("""
        SELECT rw.dec,
               100.0*sum(pr.bs) FILTER (WHERE pr.ss = 0) / sum(pr.bs + pr.ss),
               100.0*sum(pr.ss) FILTER (WHERE pr.bs = 0) / sum(pr.bs + pr.ss),
               100.0*sum(pr.bs + pr.ss) FILTER (WHERE pr.bs > 0 AND pr.ss > 0) / sum(pr.bs + pr.ss),
               sum(least(pr.bs, pr.ss)) FILTER (WHERE pr.bs > 0 AND pr.ss > 0)
                 / nullif(sum((pr.bs + pr.ss)/2) FILTER (WHERE pr.bs > 0 AND pr.ss > 0), 0)
        FROM pr JOIN rw ON rw.wallet = pr.wallet
        GROUP BY rw.dec ORDER BY rw.dec
    """).fetchall():
        log.info(f"  {r[0]:>3}  {r[1]:>10.1f}  {r[2]:>11.1f}  {r[3]:>6.1f}   {r[4]:>6.3f}")

    logger.info("stage 3: top-decile positions only")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE top_w AS
        SELECT wallet FROM rw WHERE dec = 1
    """)
    log.info("\n=== top decile: closed fraction distribution (mixed positions) ===")
    log.info("  closed      positions    shares m")
    for r in con.sql("""
        SELECT b, count(*), sum(bs + ss)/1e6
        FROM (
            SELECT pr.bs, pr.ss,
                   least(9, greatest(0, floor(10 * 2.0*least(pr.bs, pr.ss)/nullif(pr.bs + pr.ss, 0))))::INT AS b
            FROM pr JOIN top_w ON top_w.wallet = pr.wallet
            WHERE pr.bs > 0 AND pr.ss > 0
        ) GROUP BY b ORDER BY b
    """).fetchall():
        log.info(f"  {r[0]*10:>3}-{r[0]*10+10:<3}%  {r[1]:>10,}  {r[2]:>10,.0f}")

    logger.info("stage 4: realised vs settled, with payout")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE payout AS
        SELECT token1 AS asset_id, payout_0 AS payout FROM read_parquet('{LABELS}')
        WHERE resolved AND NOT coalesce(is_void,false) AND token1 IS NOT NULL
        UNION ALL
        SELECT token2, payout_1 FROM read_parquet('{LABELS}')
        WHERE resolved AND NOT coalesce(is_void,false) AND token2 IS NOT NULL
    """)
    log.info("\n=== where the PnL comes from: round trip vs hold-to-resolution ===")
    log.info("  dec   realised$m  settled$m  realised share")
    for r in con.sql("""
        SELECT rw.dec,
               sum(least(bs, ss) * (CASE WHEN ss>0 THEN sc/ss ELSE 0 END
                                  - CASE WHEN bs>0 THEN bc/bs ELSE 0 END)) / 1e6,
               sum(CASE WHEN bs >= ss
                        THEN (bs-ss) * (p.payout - CASE WHEN bs>0 THEN bc/bs ELSE 0 END)
                        ELSE (ss-bs) * (CASE WHEN ss>0 THEN sc/ss ELSE 0 END - p.payout) END) / 1e6,
               sum(least(bs, ss) * (CASE WHEN ss>0 THEN sc/ss ELSE 0 END
                                  - CASE WHEN bs>0 THEN bc/bs ELSE 0 END))
                 / nullif(sum(least(bs, ss) * (CASE WHEN ss>0 THEN sc/ss ELSE 0 END
                                  - CASE WHEN bs>0 THEN bc/bs ELSE 0 END))
                        + sum(CASE WHEN bs >= ss
                        THEN (bs-ss) * (p.payout - CASE WHEN bs>0 THEN bc/bs ELSE 0 END)
                        ELSE (ss-bs) * (CASE WHEN ss>0 THEN sc/ss ELSE 0 END - p.payout) END), 0)
        FROM pr
        JOIN payout p ON p.asset_id = pr.asset_id
        JOIN rw ON rw.wallet = pr.wallet
        GROUP BY rw.dec ORDER BY rw.dec
    """).fetchall():
        log.info(f"  {r[0]:>3}  {r[1]:>+9.2f}  {r[2]:>+9.2f}  {r[3]:>14.3f}")


if __name__ == "__main__":
    main()
