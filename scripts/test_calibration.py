"""Is the market really mispricing sports, or is the average-price bucket an artefact?

The concern
-----------
Positions are bucketed by a wallet's *average* buy price. A wallet that accumulates over many
fills has an average that need not equal the price at any single decision, so "price 0.475
resolved YES 59.7 %" may be a composition effect rather than a market mispricing.

The clean test
--------------
Ignore wallets entirely. For each resolved token, take the **volume-weighted trade price in a
fixed window before the market's end** (default 12 h to 6 h before), then ask: of the tokens
whose price in that window was in bucket X, what fraction resolved YES?

If that fraction tracks the price, the market is calibrated and the wallet result needs
another explanation. If the fraction sits above the price for favourites, the mispricing is a
property of the market and is exploitable without copying anyone.

Resumable: one parquet per month, skipped when present (AGENTS.md).

    uv run scripts/test_calibration.py --months 2026-05 2026-06
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from polymarketbot.logs import get_logger
log = get_logger(__name__)

TRADES = "data/raw/sii/trades.parquet"
MARKETS = "data/processed/markets_pit.parquet"
LABELS = "data/processed/market_labels.parquet"
OUT = Path("data/processed/calibration")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", nargs="+", required=True, help="e.g. 2026-05 2026-06")
    ap.add_argument("--from-h", type=float, default=12.0)
    ap.add_argument("--to-h", type=float, default=6.0)
    ap.add_argument("--cat", type=int, default=2, help="1=crypto 2=sports, 0=all")
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()

    logger = get_logger("test_calibration")
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET threads={args.threads}")
    con.execute("SET memory_limit='10GB'")
    con.execute("SET temp_directory='data/tmp_duck'")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE tok AS
        SELECT token1 AS asset_id, epoch(TRY_CAST(end_date AS TIMESTAMP)) AS end_ts,
               coalesce(category,'other') AS cat
        FROM read_parquet('{MARKETS}') WHERE token1 IS NOT NULL
        UNION ALL
        SELECT token2, epoch(TRY_CAST(end_date AS TIMESTAMP)), coalesce(category,'other')
        FROM read_parquet('{MARKETS}') WHERE token2 IS NOT NULL
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE payout AS
        SELECT token1 AS asset_id, payout_0 AS payout FROM read_parquet('{LABELS}')
        WHERE resolved AND NOT coalesce(is_void,false) AND token1 IS NOT NULL
        UNION ALL
        SELECT token2, payout_1 FROM read_parquet('{LABELS}')
        WHERE resolved AND NOT coalesce(is_void,false) AND token2 IS NOT NULL
    """)
    cat_filter = "" if args.cat == 0 else f"AND t.cat = '{ {1:'crypto',2:'sports',3:'weather'}.get(args.cat,'') }'"

    for m in args.months:
        dest = OUT / f"{m}_cat{args.cat}_{int(args.from_h)}-{int(args.to_h)}h.parquet"
        if dest.exists():
            logger.info(f"{dest.name} present, skip")
            continue
        lo = datetime.fromisoformat(f"{m}-01").replace(tzinfo=UTC)
        hi = (
            datetime(lo.year + 1, 1, 1, tzinfo=UTC)
            if lo.month == 12
            else datetime(lo.year, lo.month + 1, 1, tzinfo=UTC)
        )
        t0, t1 = int(lo.timestamp()), int(hi.timestamp())
        tmp = dest.with_suffix(".parquet.part")
        con.execute(f"""
            COPY (
                WITH w AS (
                    SELECT tr.asset_id,
                           sum(tr.price * tr.token_amount) / nullif(sum(tr.token_amount),0) AS vwap,
                           sum(tr.token_amount) AS shares
                    FROM read_parquet('{TRADES}') tr
                    JOIN tok t ON t.asset_id = tr.asset_id
                    WHERE tr.timestamp >= {t0} AND tr.timestamp < {t1}
                      AND tr.token_amount > 0 AND tr.price > 0 AND tr.price < 1
                      AND t.end_ts IS NOT NULL
                      AND tr.timestamp >= t.end_ts - {int(args.from_h*3600)}
                      AND tr.timestamp <  t.end_ts - {int(args.to_h*3600)}
                      {cat_filter}
                    GROUP BY 1
                )
                SELECT w.asset_id, w.vwap, w.shares, p.payout,
                       least(19, greatest(0, floor(20 * w.vwap)))::INT AS pbucket
                FROM w JOIN payout p ON p.asset_id = w.asset_id
            ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        if dest.exists():
            dest.unlink()
        tmp.rename(dest)
        n = con.sql(f"SELECT count(*) FROM read_parquet('{dest}')").fetchall()[0][0]
        logger.info(f"{dest.name}: {n:,} tokens")

    log.info("\n=== calibration: price in the 12-6h window vs realised outcome ===")
    log.info("  price band   tokens      shares m    payout rate   priced   gap")
    for r in con.execute(f"""
        SELECT pbucket, count(*), sum(shares)/1e6,
               sum(payout*shares)/nullif(sum(shares),0),
               avg(vwap)
        FROM read_parquet('{OUT}/*.parquet') GROUP BY pbucket ORDER BY pbucket
    """).fetchall():
        if r[1] < 50:
            continue
        log.info(f"  {r[0]*5:>3}-{r[0]*5+5:<3}%  {r[1]:>7,}  {r[2]:>10,.0f}   "
              f"{r[3]:>11.3f}   {r[4]:>6.3f}  {r[3]-r[4]:>+6.3f}")


if __name__ == "__main__":
    main()
