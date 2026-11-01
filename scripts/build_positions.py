"""Materialise analysis-ready positions, partitioned so a timeout never loses the work.

Design change (AGENTS.md: every long operation must save incrementally)
----------------------------------------------------------------------
The previous version wrote one 14 GB file at the end. A 600-second cap killed it at 8.9 GB
and lost everything. This version writes **one file per wallet-hash bucket**, skips buckets
already present, and is therefore resumable:

    data/processed/positions/00.parquet   (later: 01, 02, 03)

Wallet deciles are global (a wallet's rank depends on all its volume), so they are computed
once into a separate small file by `--deciles-only` and joined by every bucket. That file is
~470 k rows.

    uv run scripts/build_positions.py --deciles-only
    uv run scripts/build_positions.py --bucket 0 --buckets 4
    ... 1, 2, 3

Columns
-------
    wallet, dec, asset_id, bs, bc, ss, sc, maker_share, payout, ttl_h

- `maker_share` = maker cash / total cash in that token (1.0 pure maker, 0.0 pure taker)
- `ttl_h` = hours from first fill to the market's end date (NULL when unknown)
- `payout` is left-joined so unresolved tokens are kept with NULL, never coerced to 0.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import duckdb

from polymarketbot.logs import get_logger

CACHE = "data/processed/wallet_token/*.parquet"
LABELS = "data/processed/market_labels.parquet"
MARKETS = "data/processed/markets_pit.parquet"
OUTDIR = Path("data/processed/positions")
DECILES = Path("data/processed/wallet_deciles.parquet")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--bucket", type=int, default=-1)
    ap.add_argument("--buckets", type=int, default=4)
    ap.add_argument("--deciles-only", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()

    logger = get_logger("build_positions")
    OUTDIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET threads={args.threads}")
    con.execute("SET memory_limit='10GB'")
    con.execute("SET temp_directory='data/tmp_duck'")
    con.execute("SET preserve_insertion_order=false")

    if args.deciles_only:
        if DECILES.exists() and not args.force:
            logger.info(f"{DECILES} present, skip")
            return 0
        t0 = time.time()
        tmp = DECILES.with_suffix(".parquet.part")
        con.execute(f"""
            COPY (
                SELECT wallet, ntile(10) OVER (ORDER BY vol DESC) AS dec
                FROM (
                    SELECT wallet,
                           sum(coalesce(m_buy_cash,0) + coalesce(t_buy_cash,0)
                             + coalesce(m_sell_cash,0) + coalesce(t_sell_cash,0)) AS vol
                    FROM read_parquet('{args.cache}') GROUP BY 1
                ) WHERE vol >= 1000
            ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        if DECILES.exists():
            DECILES.unlink()
        tmp.rename(DECILES)
        n = con.sql(f"SELECT count(*) FROM read_parquet('{DECILES}')").fetchall()[0][0]
        logger.info(f"{DECILES}: {n:,} wallets, {time.time()-t0:.0f}s")
        return 0

    if args.bucket < 0:
        raise SystemExit("pass --bucket and --buckets, or --deciles-only")
    if not DECILES.exists():
        raise SystemExit(f"{DECILES} missing; run --deciles-only first")

    dest = OUTDIR / f"{args.bucket:02d}.parquet"
    if dest.exists() and not args.force:
        logger.info(f"{dest} present, skip")
        return 0

    t0 = time.time()
    tmp = dest.with_suffix(".parquet.part")
    con.execute(f"""
        COPY (
            WITH agg AS (
                SELECT wallet, asset_id,
                       sum(coalesce(m_buy_sh,0)   + coalesce(t_buy_sh,0))   AS bs,
                       sum(coalesce(m_buy_cash,0) + coalesce(t_buy_cash,0)) AS bc,
                       sum(coalesce(m_sell_sh,0)  + coalesce(t_sell_sh,0))  AS ss,
                       sum(coalesce(m_sell_cash,0)+ coalesce(t_sell_cash,0)) AS sc,
                       sum(coalesce(m_buy_cash,0) + coalesce(m_sell_cash,0))
                         / nullif(sum(coalesce(m_buy_cash,0) + coalesce(m_sell_cash,0)
                                    + coalesce(t_buy_cash,0) + coalesce(t_sell_cash,0)), 0)
                         AS maker_share,
                       min(first_ts) AS first_ts
                FROM read_parquet('{args.cache}')
                WHERE hash(wallet) % {args.buckets} = {args.bucket}
                GROUP BY 1, 2
            ),
            payout AS (
                SELECT token1 AS asset_id, payout_0 AS payout FROM read_parquet('{LABELS}')
                WHERE resolved AND NOT coalesce(is_void,false) AND token1 IS NOT NULL
                UNION ALL
                SELECT token2, payout_1 FROM read_parquet('{LABELS}')
                WHERE resolved AND NOT coalesce(is_void,false) AND token2 IS NOT NULL
            ),
            tok_end AS (
                SELECT token1 AS asset_id, epoch(TRY_CAST(end_date AS TIMESTAMP)) AS end_ts
                FROM read_parquet('{MARKETS}') WHERE token1 IS NOT NULL
                UNION ALL
                SELECT token2, epoch(TRY_CAST(end_date AS TIMESTAMP))
                FROM read_parquet('{MARKETS}') WHERE token2 IS NOT NULL
            )
            SELECT a.wallet, d.dec, a.asset_id, a.bs, a.bc, a.ss, a.sc,
                   a.maker_share, p.payout,
                   CASE WHEN e.end_ts IS NOT NULL AND a.first_ts IS NOT NULL
                        THEN (e.end_ts - a.first_ts) / 3600.0 END AS ttl_h
            FROM agg a
            LEFT JOIN read_parquet('{DECILES}') d ON d.wallet = a.wallet
            LEFT JOIN payout p ON p.asset_id = a.asset_id
            LEFT JOIN tok_end e ON e.asset_id = a.asset_id
        ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    if dest.exists():
        dest.unlink()
    tmp.rename(dest)
    n = con.sql(f"SELECT count(*) FROM read_parquet('{dest}')").fetchall()[0][0]
    logger.info(f"{dest}: {n:,} positions, {dest.stat().st_size/1e9:.2f} GB, {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
