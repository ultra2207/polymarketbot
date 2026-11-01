"""Materialise ONE analysis-ready positions table so every test stops rebuilding it.

The problem
-----------
Every test (selection, disposition, lifecycle) rebuilt the same thing: collapse the 15 GB,
15-column monthly cache into ~250 M (wallet, token) positions, join the payout table, and
compute wallet deciles. That is ~2 minutes of scanning per test, and it is identical work
every time.

The fix
-------
Write it once, narrow and pre-joined:

    wallet, dec, asset_id, bs, bc, ss, sc, payout

- `dec` is the wallet's volume decile (1 = largest), computed once on the small wallet table.
- `payout` is left-joined, so unresolved tokens are kept with payout NULL rather than dropped
  (dropping them silently changes every downstream denominator).
- Only the seven columns any test uses; `month`, `n_maker`, `n_taker`, `first_ts`, `last_ts`
  and the role split are dropped after aggregation.

Downstream tests then do one sequential scan of ~7 columns instead of 15, with no join and
no window function.

Usage
-----
    uv run scripts/build_positions.py            # skips if the output exists
    uv run scripts/build_positions.py --force
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import duckdb

from polymarketbot.logs import get_logger

CACHE = "data/processed/wallet_token/*.parquet"
LABELS = "data/processed/market_labels.parquet"
OUT = Path("data/processed/positions.parquet")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()

    logger = get_logger("build_positions")
    out = Path(args.out)
    if out.exists() and not args.force:
        logger.info(f"{out} present, skip")
        return 0

    con = duckdb.connect()
    con.execute(f"SET threads={args.threads}")
    con.execute("SET memory_limit='10GB'")
    con.execute("SET temp_directory='data/tmp_duck'")
    con.execute("SET preserve_insertion_order=false")

    t0 = time.time()
    tmp = out.with_suffix(".parquet.part")
    con.execute(f"""
        COPY (
            WITH agg AS (
                SELECT wallet, asset_id,
                       sum(coalesce(m_buy_sh,0)   + coalesce(t_buy_sh,0))   AS bs,
                       sum(coalesce(m_buy_cash,0) + coalesce(t_buy_cash,0)) AS bc,
                       sum(coalesce(m_sell_sh,0)  + coalesce(t_sell_sh,0))  AS ss,
                       sum(coalesce(m_sell_cash,0)+ coalesce(t_sell_cash,0)) AS sc
                FROM read_parquet('{args.cache}')
                GROUP BY 1, 2
            ),
            w AS (
                SELECT wallet, ntile(10) OVER (ORDER BY sum(bc + sc) DESC) AS dec
                FROM agg GROUP BY wallet HAVING sum(bc + sc) >= 1000
            ),
            payout AS (
                SELECT token1 AS asset_id, payout_0 AS payout
                FROM read_parquet('{LABELS}')
                WHERE resolved AND NOT coalesce(is_void,false) AND token1 IS NOT NULL
                UNION ALL
                SELECT token2, payout_1
                FROM read_parquet('{LABELS}')
                WHERE resolved AND NOT coalesce(is_void,false) AND token2 IS NOT NULL
            )
            SELECT a.wallet, w.dec, a.asset_id, a.bs, a.bc, a.ss, a.sc, p.payout
            FROM agg a
            LEFT JOIN w       ON w.wallet   = a.wallet
            LEFT JOIN payout p ON p.asset_id = a.asset_id
        ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    if out.exists():
        out.unlink()
    tmp.rename(out)

    n = con.sql(f"SELECT count(*) FROM read_parquet('{out}')").fetchall()[0][0]
    size = out.stat().st_size / 1e9
    logger.info(f"{out}: {n:,} positions, {size:.2f} GB, {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
