"""One scan of positions -> a small cube. Every cross-section becomes a slice.

Why
---
`positions.parquet` is 259 M rows / 13.6 GB. Each test rescans it, which costs ~30 s even
though the answer is a handful of summary rows. All the questions asked so far are
cross-sections of the same position facts, so aggregate once at the finest resolution any
test needs, and never scan the big table again.

Cube dimensions (kept small deliberately):

    dec       wallet volume decile, 1 = largest (0 = below the $1k threshold)
    pbucket   price paid, 5 % buckets (0-19, -1 = no buys)
    outcome   realised payout (0 or 1, -1 = unresolved/missing)
    cbucket   fraction of the position closed, deciles (0-9)
    sbucket   notional size, decade buckets (0-8)

That is at most 11 x 21 x 3 x 10 x 9 = 62,370 rows - trivial to scan.

Measures stored per cell (all additive, so any roll-up is exact):

    n, bs, bc, ss, sc, payout_bs
    closed_pnl   = min(bs,ss) * (sell_vwap - buy_vwap)
    held_pnl     = (bs-ss)  * (payout - buy_vwap)      for bs > ss
    exit_pnl     = (ss-bs)  * (sell_vwap - payout)     for ss > bs

closed_pnl + held_pnl is the full round-trip decomposition; nothing downstream needs the
row-level data again.

Usage
-----
    uv run scripts/build_cube.py            # skips if the cube exists
    uv run scripts/build_cube.py --force
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import duckdb

from polymarketbot.logs import get_logger

POSITIONS = "data/processed/positions.parquet"
OUT = Path("data/processed/cube.parquet")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", default=POSITIONS)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()

    logger = get_logger("build_cube")
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
            WITH x AS (
                SELECT dec,
                       CASE WHEN bs > 0 THEN least(19, greatest(0, floor(20 * bc / bs)))::INT
                            ELSE -1 END AS pbucket,
                       CASE WHEN payout IS NULL THEN -1 WHEN payout > 0.5 THEN 1 ELSE 0 END AS outcome,
                       CASE WHEN bs + ss = 0 THEN 0
                            ELSE least(9, greatest(0,
                                 floor(10 * 2 * least(bs, ss) / (bs + ss))))::INT
                       END AS cbucket,
                       CASE WHEN bs * (bc / nullif(bs,0)) IS NULL THEN 0
                            WHEN bs * (bc / nullif(bs,0)) < 1 THEN 0
                            WHEN bs * (bc / nullif(bs,0)) < 10 THEN 1
                            WHEN bs * (bc / nullif(bs,0)) < 100 THEN 2
                            WHEN bs * (bc / nullif(bs,0)) < 1000 THEN 3
                            WHEN bs * (bc / nullif(bs,0)) < 10000 THEN 4
                            WHEN bs * (bc / nullif(bs,0)) < 100000 THEN 5
                            WHEN bs * (bc / nullif(bs,0)) < 1000000 THEN 6
                            WHEN bs * (bc / nullif(bs,0)) < 10000000 THEN 7
                            ELSE 8 END AS sbucket,
                       bs, bc, ss, sc, payout,
                       least(bs, ss) * ((CASE WHEN ss > 0 THEN sc / ss ELSE 0 END)
                                      - (CASE WHEN bs > 0 THEN bc / bs ELSE 0 END)) AS closed_pnl,
                       -- unresolved positions must NOT be valued at payout 0: that turns
                       -- every open position into a total loss and swamps the cube.
                       CASE WHEN payout IS NOT NULL AND bs > ss
                            THEN (bs - ss) * (payout - bc / nullif(bs,0))
                            ELSE 0 END AS held_pnl,
                       CASE WHEN payout IS NOT NULL AND ss > bs
                            THEN (ss - bs) * ((CASE WHEN ss > 0 THEN sc / ss ELSE 0 END) - payout)
                            ELSE 0 END AS exit_pnl
                FROM read_parquet('{args.positions}')
            )
            SELECT coalesce(dec, 0) AS dec, pbucket, outcome, cbucket, sbucket,
                   count(*) AS n,
                   count(*) FILTER (WHERE ss > 0)  AS n_closed,
                   count(*) FILTER (WHERE ss = 0 AND bs > 0) AS n_buy_only,
                   count(*) FILTER (WHERE bs > ss) AS n_held,
                   sum(bs) AS bs, sum(bc) AS bc, sum(ss) AS ss, sum(sc) AS sc,
                   sum(bs * coalesce(payout,0)) AS payout_bs,
                   sum(closed_pnl) AS closed_pnl,
                   sum(held_pnl)   AS held_pnl,
                   sum(exit_pnl)   AS exit_pnl
            FROM x
            GROUP BY 1, 2, 3, 4, 5
        ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    if out.exists():
        out.unlink()
    tmp.rename(out)

    n = con.sql(f"SELECT count(*) FROM read_parquet('{out}')").fetchall()[0][0]
    logger.info(f"{out}: {n:,} cells, {out.stat().st_size/1e6:.1f} MB, {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
