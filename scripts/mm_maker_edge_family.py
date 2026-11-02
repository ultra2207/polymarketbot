"""Register #11, split by market family and month.

At-touch fill definition (from the register): a tape print priced within
0.05 c of the DOME best_ask (taker BUY) or best_bid (taker SELL), asof-joined
to the nearest book snapshot at or before the print.

Maker edge per fill = sign * (price - mid at print + 60 s), sign = +1 for a
taker BUY (the maker sold at the ask) and -1 for a taker SELL (maker bought
the bid).  Positive = the maker earned the spread.

Outputs, one file per month so a killed run costs one month:
  data/processed/mm/book_YYYYMM.parquet   reduced book (quote changes only)
  data/processed/mm/agg_YYYYMM.parquet    family x month x side aggregates

Usage: uv run python scripts/mm_maker_edge_family.py [YYYY-MM ...]
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from polymarketbot.logs import get_logger

DOME = "data/raw/dome/dome_polymarket_2025-10_2026-03.parquet"
TRADES = "data/raw/sii/trades.parquet"
MKT = "data/processed/markets_pit.parquet"
OUT = Path("data/processed/mm")

MONTHS = ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04"]

TOUCH_C = 0.05          # cents
BOOK_STALE_MS = 300_000  # accept a quote last changed up to 5 min before print
MID60_STALE_MS = 300_000
MID60_OFFSET_MS = 60_000

log = get_logger("mm_maker_edge_family")


def month_bounds(m: str) -> tuple[int, int]:
    y, mo = (int(x) for x in m.split("-"))
    start_ms = int(datetime(y, mo, 1, tzinfo=timezone.utc).timestamp() * 1000)
    ny, nmo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    end_ms = int(datetime(ny, nmo, 1, tzinfo=timezone.utc).timestamp() * 1000)
    return start_ms, end_ms


def token_category(con: duckdb.DuckDBPyConnection) -> None:
    path = OUT / "token_category.parquet"
    if path.exists():
        log.info("token_category cached ({} rows)", con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0])
        return
    t0 = time.time()
    con.execute(f"""
        COPY (
          WITH u AS (
            SELECT token1 AS token_id, category, volume FROM '{MKT}'
            UNION ALL
            SELECT token2 AS token_id, category, volume FROM '{MKT}'
          )
          SELECT token_id, category FROM (
            SELECT *, row_number() OVER (PARTITION BY token_id
                                         ORDER BY volume DESC NULLS LAST) rn
            FROM u
          ) WHERE rn = 1
        ) TO '{path}' (FORMAT parquet)
    """)
    log.info("token_category built in {:.1f}s", time.time() - t0)


def build_book(con: duckdb.DuckDBPyConnection, m: str) -> Path:
    path = OUT / f"book_{m.replace('-', '')}.parquet"
    if path.exists():
        return path
    lo, hi = month_bounds(m)
    t0 = time.time()
    # +90 s of next-month tail so a 60 s markout late on the last day exists
    con.execute(f"""
        COPY (
          SELECT token_id, timestamp_ms, best_bid, best_ask, mid FROM (
            SELECT *,
                   (best_bid, best_ask, mid) AS q,
                   lag((best_bid, best_ask, mid)) OVER (
                       PARTITION BY token_id ORDER BY timestamp_ms) AS qp
            FROM '{DOME}'
            WHERE timestamp_ms >= {lo} AND timestamp_ms < {hi + 90_000}
              AND best_bid > 0 AND best_ask < 1 AND best_bid < best_ask
          ) WHERE qp IS NULL OR q <> qp
        ) TO '{path}' (FORMAT parquet)
    """)
    n = con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0]
    log.info("{} book reduced rows={:,} in {:.1f}s", m, n, time.time() - t0)
    return path


def month_fills(con: duckdb.DuckDBPyConnection, m: str, book: Path) -> None:
    out = OUT / f"agg_{m.replace('-', '')}.parquet"
    if out.exists():
        return
    lo, hi = month_bounds(m)
    lo_s, hi_s = lo // 1000, hi // 1000
    t0 = time.time()

    # stage A: at-touch test only (one asof join), materialised small
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE f0 AS
        SELECT t.asset_id, t.ts_ms, t.t60, t.price, t.taker_direction,
               t.token_amount,
               CASE WHEN t.taker_direction = 'BUY' THEN 1 ELSE -1 END AS sgn,
               b.mid AS mid0
        FROM (
          SELECT asset_id, timestamp * 1000 AS ts_ms,
                 timestamp * 1000 + {MID60_OFFSET_MS} AS t60,
                 price, taker_direction, token_amount
          FROM '{TRADES}'
          WHERE timestamp >= {lo_s} AND timestamp < {hi_s}
            AND taker_direction IN ('BUY', 'SELL')
        ) t
        ASOF LEFT JOIN '{book}' b
          ON t.asset_id = b.token_id AND t.ts_ms >= b.timestamp_ms
        WHERE t.ts_ms - b.timestamp_ms <= {BOOK_STALE_MS}
          AND b.mid > 0 AND b.mid < 1
          AND (
             (t.taker_direction = 'BUY'  AND abs(t.price - b.best_ask) <= {TOUCH_C}/100)
          OR (t.taker_direction = 'SELL' AND abs(t.price - b.best_bid) <= {TOUCH_C}/100)
          )
    """)
    n0 = con.execute("SELECT count(*) FROM f0").fetchone()[0]
    log.info("{} at-touch candidates={:,} in {:.1f}s", m, n0, time.time() - t0)

    # stage B: 60 s markout on survivors only
    con.execute(f"""
        COPY (
          WITH f AS (
            SELECT f0.*, m2.mid AS mid60
            FROM f0
            ASOF LEFT JOIN '{book}' m2
              ON f0.asset_id = m2.token_id AND f0.t60 >= m2.timestamp_ms
            WHERE f0.t60 - m2.timestamp_ms <= {MID60_STALE_MS}
              AND m2.mid > 0 AND m2.mid < 1
          )
          SELECT coalesce(c.category, 'unknown') AS category,
                 '{m}' AS month,
                 f.taker_direction AS side,
                 CASE WHEN f.price < 0.1 THEN '0-0.1'
                      WHEN f.price < 0.25 THEN '0.1-0.25'
                      WHEN f.price < 0.5 THEN '0.25-0.5'
                      WHEN f.price < 0.75 THEN '0.5-0.75'
                      WHEN f.price < 0.9 THEN '0.75-0.9'
                      ELSE '0.9-1' END AS price_bucket,
                 count(*) AS fills,
                 sum(f.token_amount) AS shares,
                 sum(f.sgn * (f.price - f.mid60) * 100 * f.token_amount) AS pnl_cs,
                 sum(f.sgn * (f.price - f.mid60) * 100) AS sum_edge_c
          FROM f
          LEFT JOIN '{OUT / 'token_category.parquet'}' c ON f.asset_id = c.token_id
          GROUP BY ALL
        ) TO '{out}' (FORMAT parquet)
    """)
    n = con.execute(f"SELECT sum(fills) FROM '{out}'").fetchone()[0] or 0
    sh = con.execute(f"SELECT sum(shares) FROM '{out}'").fetchone()[0] or 0
    log.info("{} at-touch fills={:,} shares={:,.0f} in {:.1f}s", m, n, sh,
             time.time() - t0)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=6")
    con.execute("SET memory_limit='6GB'")
    con.execute("SET temp_directory='data/tmp_duck'")
    con.execute("SET preserve_insertion_order=false")
    token_category(con)
    todo = sys.argv[1:] or MONTHS
    for m in todo:
        book = build_book(con, m)
        month_fills(con, m, book)


if __name__ == "__main__":
    main()
