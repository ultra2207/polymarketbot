"""Crypto up/down vs the reference, with both prices read at the same instant.

Why this exists
---------------
`screen_crypto_reference.py` compared a Binance reference price against the *stored
calibration VWAP*, which spans T-24 h to T-6 h. The reference is one instant, so the two
were never aligned, and the result duly moved when the reference clock was nudged: the
`2..10 %` bucket changed sign between offsets, and `-2..0 %` shrank by two thirds.

An effect whose magnitude tracks your measurement clock is measuring your clock. This
version removes the clock entirely: **the market price is the last taker trade at or
before a fixed lead**, and the reference is the kline at that same trade's timestamp.
Both come from the same second.

Why it matters beyond method
----------------------------
A7 found far-side prices too extreme at 168 h. If that is real it should also appear here,
as a mispricing conditioned on how far the underlying sits from the strike - and it should
be stable when the lead is varied, which is exactly what the VWAP version failed.

Usage
    uv run scripts/screen_crypto_aligned.py --lead-hours 12
    uv run scripts/screen_crypto_aligned.py --lead-hours 6 --min-usd 50
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "src")

import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

OUT = Path("data/processed")
TAPE = "data/raw/sii/trades.parquet"
KLINES = "data/raw/binance/klines/**/*.parquet"


def epoch(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 12")
    con.execute("SET memory_limit = '8GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET TimeZone = 'UTC'")
    return con


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead-hours", type=float, default=12.0)
    ap.add_argument("--window-minutes", type=float, default=30.0,
                    help="how far before the lead a trade may sit and still count")
    ap.add_argument("--min-usd", type=float, default=10.0)
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-10-01")
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    con = connect()
    lo, hi = epoch(args.start), epoch(args.end)
    lead = int(args.lead_hours * 3600)
    wmin = int(args.window_minutes * 60)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE mk AS
        SELECT condition_id, token1 AS token_id, payout_0 AS payout,
               TRY_CAST(end_date AS TIMESTAMP) AS end_ts,
               CASE WHEN slug LIKE 'bitcoin-%'  OR slug LIKE 'btc-%' THEN 'BTCUSDT'
                    WHEN slug LIKE 'ethereum-%' OR slug LIKE 'eth-%' THEN 'ETHUSDT'
                    WHEN slug LIKE 'solana-%'   OR slug LIKE 'sol-%' THEN 'SOLUSDT'
                    WHEN slug LIKE 'xrp-%'                          THEN 'XRPUSDT' END AS symbol,
               TRY_CAST(
                   CASE WHEN regexp_extract(slug, '-above-([0-9ptk]+)', 1) LIKE '%k'
                        THEN replace(replace(regexp_extract(slug,'-above-([0-9ptk]+)',1),'pt','.'),'k','')
                        ELSE replace(regexp_extract(slug,'-above-([0-9ptk]+)',1),'pt','.') END AS DOUBLE)
               * CASE WHEN regexp_extract(slug, '-above-([0-9ptk]+)', 1) LIKE '%k'
                      THEN 1000 ELSE 1 END AS strike
        FROM read_parquet('data/processed/market_labels.parquet')
        WHERE resolved AND NOT coalesce(is_void, FALSE) AND slug LIKE '%-above-%'
          AND token1 IS NOT NULL AND payout_0 IS NOT NULL AND end_date IS NOT NULL
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE mk2 AS
        SELECT *, epoch(end_ts) AS end_s FROM mk
        WHERE symbol IS NOT NULL AND strike IS NOT NULL AND strike > 0
    """)
    n = con.sql("SELECT count(*) FROM mk2").fetchall()[0][0]
    log.info(f"crypto above markets: {n:,}", )

    # Market price: last taker BUY inside a narrow window before the lead.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE px AS
        WITH t AS (
            SELECT m.condition_id, m.token_id, m.payout, m.symbol, m.strike,
                   x.price, x.usd_amount, x.timestamp,
                   row_number() OVER (PARTITION BY m.condition_id ORDER BY x.timestamp DESC) AS rn
            FROM mk2 m
            JOIN read_parquet('{TAPE}') x ON x.asset_id = m.token_id
            WHERE x.taker_direction = 'BUY'
              AND x.timestamp >= {lo} AND x.timestamp < {hi}
              AND x.timestamp <= m.end_s - {lead}
              AND x.timestamp >  m.end_s - {lead} - {wmin}
              AND x.price BETWEEN 0.005 AND 0.995
        )
        SELECT condition_id, token_id, payout, symbol, strike, price, timestamp
        FROM t WHERE rn = 1 AND usd_amount >= {args.min_usd}
    """)
    m = con.sql("SELECT count(*) FROM px").fetchall()[0][0]
    log.info(f"markets with a qualifying print: {m:,}", )
    if m == 0:
        log.info("no prints inside the window - widen --window-minutes")
        return 1

    # Reference at that exact second: the last kline at or before the print.
    # The right side must be explicitly ordered or the ASOF join matches arbitrary rows -
    # that bug produced a plausible-looking table earlier today.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE aligned AS
        WITH kl AS (
            SELECT symbol, open_time_s, close
            FROM read_parquet('{KLINES}', hive_partitioning=true)
            ORDER BY symbol, open_time_s
        )
        SELECT p.condition_id, p.token_id, p.payout, p.symbol, p.strike,
               p.price, p.timestamp, k.close AS reference,
               (k.close - p.strike) / p.strike AS distance
        FROM px p
        ASOF LEFT JOIN kl k
          ON k.symbol = p.symbol AND k.open_time_s <= p.timestamp
        WHERE k.close IS NOT NULL
    """)
    a = con.sql("SELECT count(*) FROM aligned").fetchall()[0][0]
    log.info(f"matched to a kline: {a:,}", )
    if a == 0:
        return 1

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM aligned)
        TO '{(dest / 'crypto_aligned_obs.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    log.info(f"\n{'distance':12} {'markets':>8} {'mkt px':>8} {'payout':>7} "
          f"{'gap':>8} {'±se':>7} {'t':>6}")
    for b, nm, p_, pay, gap, se in con.sql("""
        SELECT CASE WHEN distance < -0.10 THEN '< -10%'
                    WHEN distance < -0.02 THEN '-10..-2%'
                    WHEN distance <  0.00 THEN '-2..0%'
                    WHEN distance <  0.02 THEN '0..2%'
                    WHEN distance <  0.10 THEN '2..10%'
                    ELSE '> 10%' END b,
               count(*), avg(price), avg(payout), avg(payout - price),
               coalesce(stddev_samp(payout - price) / sqrt(count(*)), 0)
        FROM aligned GROUP BY 1 ORDER BY min(distance)
    """).fetchall():
        t = gap / se if se else float("nan")
        log.info(f"{b:12} {nm:>8,} {p_:>8.3f} {pay:>7.3f} {gap:>+8.4f} {se:>7.4f} {t:>6.1f}")

    overall = con.sql("""
        SELECT avg((price - payout) * (price - payout)) FROM aligned
    """).fetchall()[0]
    log.info(f"\nBrier (market price): {overall[0]:.5f} on {a:,} aligned observations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
