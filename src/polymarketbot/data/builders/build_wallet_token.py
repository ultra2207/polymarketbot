"""Materialise the 45 GB trade tape into a compact per-(wallet, token, month) table.

Why
---
Every analysis so far rescans the tape: 45.85 GB, 212 partitions, ~118 M rows per month.
A full-window round trip takes ~7 minutes because it reads the whole tape, joins it to the
payout table and hash-aggregates 600 M rows - not because the arithmetic is hard. The same
job runs in seconds against a pre-aggregated table.

Design
------
One parquet per month under `data/processed/wallet_token/`, skipped if already present, so
a kill loses at most the month in flight and the next run resumes (AGENTS.md).

Each row is one (wallet, token) pair for one month, **split by role**. A wallet can supply
liquidity (maker) and consume it (taker) in the same market, and separating the two is what
distinguishes earning the spread from paying it:

    wallet, asset_id, month,
    m_buy_sh, m_buy_cash, m_sell_sh, m_sell_cash,   -- as maker
    t_buy_sh, t_buy_cash, t_sell_sh, t_sell_cash,   -- as taker
    n_maker, n_taker,
    first_ts, last_ts

`price * token_amount` is used for cash rather than `usd_amount`, which is rounded to the
cent and is wrong by up to 50 % on sub-dollar fills.

Usage
-----
    uv run python -m polymarketbot.data.builders.build_wallet_token --start 2026-04-01 --end 2026-10-01
    uv run python -m polymarketbot.data.builders.build_wallet_token --start 2026-04-01 --end 2026-10-01 --force
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from polymarketbot.logs import get_logger

REPO_ROOT = Path(__file__).resolve().parents[4]

TRADES = REPO_ROOT / "data" / "raw" / "sii" / "trades.parquet"
OUT = REPO_ROOT / "data" / "processed" / "wallet_token"


def month_starts(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    out, cur = [], start
    while cur < end:
        nxt = (
            datetime(cur.year + 1, 1, 1, tzinfo=UTC)
            if cur.month == 12
            else datetime(cur.year, cur.month + 1, 1, tzinfo=UTC)
        )
        out.append((cur, min(nxt, end)))
        cur = nxt
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--memory", default="9GB")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    logger = get_logger("build_wallet_token")
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    OUT.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute(f"SET threads={args.threads}")
    con.execute(f"SET memory_limit='{args.memory}'")
    con.execute("SET temp_directory='data/tmp_duck'")
    con.execute("SET preserve_insertion_order=false")

    months = month_starts(start, end)
    for i, (lo, hi) in enumerate(months, 1):
        dest = OUT / f"{lo:%Y-%m}.parquet"
        if dest.exists() and not args.force:
            logger.info(f"[{i}/{len(months)}] {dest.name} present, skip")
            continue
        t0, t1 = int(lo.timestamp()), int(hi.timestamp())
        tmp = dest.with_suffix(".parquet.part")
        con.execute(f"""
            COPY (
                WITH fills AS (
                    SELECT t.maker AS wallet, t.asset_id, 'maker' AS role,
                           t.maker_direction AS direction, t.price, t.token_amount, t.timestamp
                    FROM read_parquet('{TRADES}') t
                    WHERE t.timestamp >= {t0} AND t.timestamp < {t1}
                      AND t.maker IS NOT NULL AND t.taker IS NOT NULL AND t.maker <> t.taker
                      AND t.price > 0 AND t.price < 1 AND t.token_amount > 0
                      AND t.maker_direction IN ('BUY','SELL')
                    UNION ALL
                    SELECT t.taker, t.asset_id, 'taker',
                           t.taker_direction, t.price, t.token_amount, t.timestamp
                    FROM read_parquet('{TRADES}') t
                    WHERE t.timestamp >= {t0} AND t.timestamp < {t1}
                      AND t.maker IS NOT NULL AND t.taker IS NOT NULL AND t.maker <> t.taker
                      AND t.price > 0 AND t.price < 1 AND t.token_amount > 0
                      AND t.taker_direction IN ('BUY','SELL')
                )
                SELECT wallet, asset_id, '{lo:%Y-%m}' AS month,
                       sum(CASE WHEN role='maker' AND direction='BUY'  THEN token_amount END)         AS m_buy_sh,
                       sum(CASE WHEN role='maker' AND direction='BUY'  THEN price*token_amount END)   AS m_buy_cash,
                       sum(CASE WHEN role='maker' AND direction='SELL' THEN token_amount END)         AS m_sell_sh,
                       sum(CASE WHEN role='maker' AND direction='SELL' THEN price*token_amount END)   AS m_sell_cash,
                       sum(CASE WHEN role='taker' AND direction='BUY'  THEN token_amount END)         AS t_buy_sh,
                       sum(CASE WHEN role='taker' AND direction='BUY'  THEN price*token_amount END)   AS t_buy_cash,
                       sum(CASE WHEN role='taker' AND direction='SELL' THEN token_amount END)         AS t_sell_sh,
                       sum(CASE WHEN role='taker' AND direction='SELL' THEN price*token_amount END)   AS t_sell_cash,
                       count(*) FILTER (WHERE role='maker') AS n_maker,
                       count(*) FILTER (WHERE role='taker') AS n_taker,
                       min(timestamp) AS first_ts,
                       max(timestamp) AS last_ts
                FROM fills
                GROUP BY 1, 2
            ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        if dest.exists():
            dest.unlink()
        tmp.rename(dest)
        n = con.sql(f"SELECT count(*) FROM read_parquet('{dest}')").fetchall()[0][0]
        size = dest.stat().st_size / 1e6
        logger.info(f"[{i}/{len(months)}] {dest.name}: {n:,} pairs, {size:.0f} MB")

    total = sum(f.stat().st_size for f in OUT.glob("*.parquet")) / 1e9
    logger.info(f"{len(months)} months, {total:.2f} GB total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
