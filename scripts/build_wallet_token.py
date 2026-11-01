"""Materialise the 45 GB trade tape into a compact per-(wallet, token, month) table.

Why
---
Every analysis so far rescans the tape: 45.85 GB, 212 partitions, ~118 M rows per month.
A full-window round trip takes ~7 minutes because it reads the whole tape, joins it to the
payout table and hash-aggregates 600 M rows - not because the arithmetic is hard. The same
job runs in seconds against a pre-aggregated table that fits in memory.

This script pays that cost exactly once. Everything downstream - round-trip PnL, fill
quality, reaction latency, coverage gaps - reads the small table instead.

Design
------
One parquet per month under `data/processed/wallet_token/`, skipped if already present, so
a kill loses at most the month in flight and the next run resumes (AGENTS.md).

Each row is one (wallet, token) pair for one month:

    wallet, asset_id, month,
    buy_sh, buy_cash, sell_sh, sell_cash,   -- token_amount and price*token_amount
    n_buy, n_sell,                          -- fill counts
    first_ts, last_ts                       -- for latency work

Both roles are unioned: a wallet's round trip spans the maker and taker legs, and a
maker-only view sees one side (it reported zero PnL for every large maker before this).

`price * token_amount` is used for cash rather than `usd_amount`, which is rounded to the
cent and is wrong by up to 50 % on sub-dollar fills.

Usage
-----
    uv run scripts/build_wallet_token.py --start 2026-04-01 --end 2026-10-01
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from polymarketbot.logs import get_logger

TRADES = Path("data/raw/sii/trades.parquet")
OUT = Path("data/processed/wallet_token")


def month_starts(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    out, cur = [], start
    while cur < end:
        if cur.month == 12:
            nxt = datetime(cur.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            nxt = datetime(cur.year, cur.month + 1, 1, tzinfo=timezone.utc)
        out.append((cur, min(nxt, end)))
        cur = nxt
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--memory", default="9GB")
    args = ap.parse_args()

    logger = get_logger("build_wallet_token")
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    OUT.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute(f"SET threads={args.threads}")
    con.execute(f"SET memory_limit='{args.memory}'")
    con.execute("SET temp_directory='data/tmp_duck'")
    con.execute("SET preserve_insertion_order=false")

    months = month_starts(start, end)
    done = 0
    for i, (lo, hi) in enumerate(months, 1):
        dest = OUT / f"{lo:%Y-%m}.parquet"
        if dest.exists():
            logger.info(f"[{i}/{len(months)}] {dest.name} present, skip")
            done += 1
            continue
        t0, t1 = int(lo.timestamp()), int(hi.timestamp())
        tmp = dest.with_suffix(".parquet.part")
        con.execute(f"""
            COPY (
                WITH fills AS (
                    SELECT t.maker AS wallet, t.asset_id, t.maker_direction AS direction,
                           t.price, t.token_amount, t.timestamp
                    FROM read_parquet('{TRADES}') t
                    WHERE t.timestamp >= {t0} AND t.timestamp < {t1}
                      AND t.maker IS NOT NULL AND t.taker IS NOT NULL AND t.maker <> t.taker
                      AND t.price > 0 AND t.price < 1 AND t.token_amount > 0
                      AND t.maker_direction IN ('BUY','SELL')
                    UNION ALL
                    SELECT t.taker, t.asset_id, t.taker_direction,
                           t.price, t.token_amount, t.timestamp
                    FROM read_parquet('{TRADES}') t
                    WHERE t.timestamp >= {t0} AND t.timestamp < {t1}
                      AND t.maker IS NOT NULL AND t.taker IS NOT NULL AND t.maker <> t.taker
                      AND t.price > 0 AND t.price < 1 AND t.token_amount > 0
                      AND t.taker_direction IN ('BUY','SELL')
                )
                SELECT wallet, asset_id, '{lo:%Y-%m}' AS month,
                       sum(CASE WHEN direction = 'BUY'  THEN token_amount END)           AS buy_sh,
                       sum(CASE WHEN direction = 'BUY'  THEN price * token_amount END)   AS buy_cash,
                       sum(CASE WHEN direction = 'SELL' THEN token_amount END)           AS sell_sh,
                       sum(CASE WHEN direction = 'SELL' THEN price * token_amount END)   AS sell_cash,
                       count(*) FILTER (WHERE direction = 'BUY')  AS n_buy,
                       count(*) FILTER (WHERE direction = 'SELL') AS n_sell,
                       min(timestamp) AS first_ts,
                       max(timestamp) AS last_ts
                FROM fills
                GROUP BY 1, 2
            ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        tmp.rename(dest)
        done += 1
        n = con.sql(f"SELECT count(*) FROM read_parquet('{dest}')").fetchall()[0][0]
        size = dest.stat().st_size / 1e6
        logger.info(f"[{i}/{len(months)}] {dest.name}: {n:,} pairs, {size:.0f} MB")

    total = sum(f.stat().st_size for f in OUT.glob("*.parquet")) / 1e9
    logger.info(f"{done}/{len(months)} months materialised, {total:.2f} GB total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
