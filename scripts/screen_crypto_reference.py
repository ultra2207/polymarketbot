"""Crypto up/down versus the reference price: does the market misprice distance?

Idea C1 in `docs/alpha-pipeline.md`. Resolution labels come from on-chain payouts, so the
Binance series is only a *feature*. What it adds is the one thing the market price cannot
tell us on its own: **how far the underlying sits from the strike**.

Why that matters
----------------
Unconditional calibration is already known to be dead (`docs/results-log.md`, idea #1), and
the reason is that a price of 0.85 pools very different situations. Conditioning on distance
separates them: a 0.85 on a strike the asset has already passed is a near-certainty, while a
0.85 on a strike 2 % away with a day left is a coin flip being overpriced. If the market
misprices, it should show up as a gap between price and realised payout **within** a distance
bucket.

Method
------
* Parse asset and strike from the slug (`bitcoin-above-66800-on-april-1-2026-1pm-et`).
* Entry price is the stored calibration VWAP for the T-24 h -> T-6 h slot.
* Reference price is the Binance 1-minute close at the entry instant, joined with an ASOF
  join so no future bar is used.
* `distance = (reference - strike) / strike`, bucketed; realised payout per bucket is the
  ground truth.

The no-lookahead rule is enforced by joining the *last bar at or before* the entry time.

Usage
    uv run scripts/screen_crypto_reference.py
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
KLINES = "data/raw/binance/klines/**/*.parquet"


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 8")
    con.execute("SET memory_limit = '6GB'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET TimeZone = 'UTC'")
    return con


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry-offset-hours", type=float, default=12.0,
                    help="hours before end_date at which the reference price is read. "
                         "The stored entry VWAP spans T-24..T-6 h, so the result must be "
                         "stable across offsets or it is a timestamp artefact.")
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    con = connect()
    off = args.entry_offset_hours
    if not Path("data/raw/binance/klines").exists():
        log.info("no klines on disk - run scripts/fetch_binance_klines.py first")
        return 1

    con.execute("""
        CREATE OR REPLACE VIEW kl AS
        SELECT symbol, open_time_s, close
        FROM read_parquet('data/raw/binance/klines/**/*.parquet',
                          hive_partitioning=true)
    """)
    k = con.sql("SELECT count(*) FROM kl").fetchall()[0][0]
    log.info(f"klines on disk: {k:,} minutes", )

    con.execute("""
        CREATE OR REPLACE TEMP TABLE mk AS
        SELECT m.condition_id, m.token1 AS token_id, m.slug,
               TRY_CAST(m.end_date AS TIMESTAMP) AS end_ts,
               m.payout_0 AS payout,
               CASE
                   WHEN m.slug LIKE 'bitcoin-%'  OR m.slug LIKE 'btc-%'      THEN 'BTCUSDT'
                   WHEN m.slug LIKE 'ethereum-%' OR m.slug LIKE 'eth-%'      THEN 'ETHUSDT'
                   WHEN m.slug LIKE 'solana-%'   OR m.slug LIKE 'sol-%'      THEN 'SOLUSDT'
                   WHEN m.slug LIKE 'xrp-%'                                  THEN 'XRPUSDT'
               END AS symbol,
               TRY_CAST(
                   CASE WHEN regexp_extract(m.slug, '-above-([0-9ptk]+)', 1) LIKE '%k'
                        THEN replace(replace(regexp_extract(m.slug, '-above-([0-9ptk]+)', 1),
                                             'pt', '.'), 'k', '')
                        ELSE replace(regexp_extract(m.slug, '-above-([0-9ptk]+)', 1),
                                     'pt', '.') END AS DOUBLE)
               * CASE WHEN regexp_extract(m.slug, '-above-([0-9ptk]+)', 1) LIKE '%k'
                      THEN 1000 ELSE 1 END AS strike
        FROM read_parquet('data/processed/market_labels.parquet') m
        WHERE m.resolved AND NOT coalesce(m.is_void, FALSE)
          AND m.slug LIKE '%-above-%' AND m.token1 IS NOT NULL AND m.payout_0 IS NOT NULL
    """)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE base AS
        SELECT mk.*, r.vwap AS price,
               epoch(mk.end_ts) - CAST({off} * 3600 AS BIGINT) AS entry_s
        FROM mk
        JOIN read_parquet('data/processed/screen_calibration_rows.parquet') r
          ON r.token_id = mk.token_id
        WHERE mk.symbol IS NOT NULL AND mk.strike IS NOT NULL AND mk.strike > 0
          AND mk.end_ts IS NOT NULL
    """)
    n = con.sql("SELECT count(*) FROM base").fetchall()[0][0]
    log.info(f"markets with a strike, an entry price and a symbol: {n:,}", )
    if n == 0:
        log.info("nothing joined - check the kline coverage window against entry times")
        return 1

    # ASOF join: last bar at or before the entry instant. No future data by construction.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE with_ref AS
        SELECT b.*, k.close AS reference,
               (k.close - b.strike) / b.strike AS distance
        FROM base b
        -- The right side of an ASOF join must be sorted on the asof key, otherwise
        -- DuckDB matches arbitrary rows. Without the ORDER BY every market matched
        -- the same bar and 'distance' was meaningless but looked plausible.
        ASOF LEFT JOIN (
            SELECT symbol, open_time_s, close FROM kl ORDER BY symbol, open_time_s
        ) k
          ON k.symbol = b.symbol AND k.open_time_s <= b.entry_s
        WHERE k.close IS NOT NULL
    """)
    n2 = con.sql("SELECT count(*) FROM with_ref").fetchall()[0][0]
    log.info(f"matched to a reference bar: {n2:,}", )
    if n2 == 0:
        log.info("no reference bars matched - check that kline coverage reaches the entry times")
        return 1

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM with_ref) TO '{(dest / 'crypto_reference_obs.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE binned AS
        SELECT *, CASE
            WHEN distance < -0.10 THEN '< -10%'
            WHEN distance < -0.02 THEN '-10..-2%'
            WHEN distance <  0.00 THEN '-2..0%'
            WHEN distance <  0.02 THEN '0..2%'
            WHEN distance <  0.10 THEN '2..10%'
            ELSE '> 10%' END AS dist_bin
        FROM with_ref
    """)

    log.info(f"\n{'distance':12} {'markets':>8} {'mean px':>8} {'payout':>7} "
          f"{'gap':>8} {'±se':>7} {'t':>6}")
    for b, nm, px, pay, gap, se in con.sql("""
        SELECT dist_bin, count(*), avg(price), avg(payout),
               avg(payout - price),
               coalesce(stddev_samp(payout - price) / sqrt(count(*)), 0)
        FROM binned GROUP BY 1
        ORDER BY min(distance)
    """).fetchall():
        t = gap / se if se else float("nan")
        log.info(f"{b:12} {nm:>8,} {px:>8.3f} {pay:>7.3f} {gap:>+8.4f} {se:>7.4f} {t:>6.1f}")

    # The question that matters: does conditioning on distance beat the price itself?
    brier = con.sql("""
        SELECT avg((price - payout) * (price - payout)),
               avg((CASE WHEN distance > 0 THEN 1.0 ELSE 0.0 END - payout)
                   * (CASE WHEN distance > 0 THEN 1.0 ELSE 0.0 END - payout))
        FROM with_ref
    """).fetchall()[0]
    log.info(f"\nBrier: market price {brier[0]:.5f} vs a naive 'is it already past the strike' "
          f"rule {brier[1]:.5f}")
    log.info("(the naive rule ignores time-to-resolution and volatility entirely, so beating it "
          "is a low bar; the point is how far behind it the price is)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
