"""Crypto 15-minute up/down sub-family: at-touch maker edge by month.

Same fill definition as scripts/mm_maker_edge_family.py, restricted to
`*-updown-15m-*` markets, reusing the reduced per-month books it wrote.

Usage: uv run python scripts/mm_crypto15m.py [YYYY-MM ...]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb

from polymarketbot.logs import get_logger

TRADES = "data/raw/sii/trades.parquet"
MKT = "data/processed/markets_pit.parquet"
OUT = Path("data/processed/mm")
AGG = OUT / "agg15m_*.parquet"

MONTHS = ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04"]
TOUCH_C = 0.05
BOOK_STALE_MS = 300_000
MID60_STALE_MS = 300_000
MID60_OFFSET_MS = 60_000

log = get_logger("mm_crypto15m")


def month_bounds(m: str) -> tuple[int, int]:
    from datetime import datetime, timezone
    y, mo = (int(x) for x in m.split("-"))
    start_ms = int(datetime(y, mo, 1, tzinfo=timezone.utc).timestamp() * 1000)
    ny, nmo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    end_ms = int(datetime(ny, nmo, 1, tzinfo=timezone.utc).timestamp() * 1000)
    return start_ms, end_ms


def token_set(con: duckdb.DuckDBPyConnection) -> None:
    path = OUT / "tokens_15m.parquet"
    if path.exists():
        return
    con.execute(f"""
        COPY (
          WITH m AS (
            SELECT * FROM '{MKT}' WHERE category = 'crypto'
              AND slug ILIKE '%updown-15m%'
          )
          SELECT DISTINCT token_id FROM (
            SELECT token1 AS token_id FROM m
            UNION ALL SELECT token2 FROM m
          ) WHERE token_id IS NOT NULL
        ) TO '{path}' (FORMAT parquet)
    """)
    log.info("tokens_15m rows={}",
             con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0])


def run_month(con: duckdb.DuckDBPyConnection, m: str) -> None:
    out = OUT / f"agg15m_{m.replace('-', '')}.parquet"
    if out.exists():
        return
    book = OUT / f"book_{m.replace('-', '')}.parquet"
    if not book.exists():
        raise SystemExit(f"missing {book}; run mm_maker_edge_family.py {m} first")
    lo, hi = month_bounds(m)
    lo_s, hi_s = lo // 1000, hi // 1000
    t0 = time.time()
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE b15 AS
        SELECT * FROM '{book}'
        WHERE token_id IN (SELECT token_id FROM '{OUT / 'tokens_15m.parquet'}')
    """)
    nb = con.execute("SELECT count(*) FROM b15").fetchone()[0]
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE f0 AS
        SELECT t.asset_id, t.t60, t.price, t.taker_direction, t.token_amount,
               CASE WHEN t.taker_direction = 'BUY' THEN 1 ELSE -1 END AS sgn
        FROM (
          SELECT asset_id, timestamp * 1000 + {MID60_OFFSET_MS} AS t60,
                 timestamp * 1000 AS ts_ms, price, taker_direction, token_amount
          FROM '{TRADES}'
          WHERE timestamp >= {lo_s} AND timestamp < {hi_s}
            AND taker_direction IN ('BUY', 'SELL')
            AND asset_id IN (SELECT token_id FROM '{OUT / 'tokens_15m.parquet'}')
        ) t
        ASOF LEFT JOIN b15 b
          ON t.asset_id = b.token_id AND t.ts_ms >= b.timestamp_ms
        WHERE t.ts_ms - b.timestamp_ms <= {BOOK_STALE_MS}
          AND b.mid > 0 AND b.mid < 1
          AND (
             (t.taker_direction = 'BUY'  AND abs(t.price - b.best_ask) <= {TOUCH_C}/100)
          OR (t.taker_direction = 'SELL' AND abs(t.price - b.best_bid) <= {TOUCH_C}/100)
          )
    """)
    n0 = con.execute("SELECT count(*) FROM f0").fetchone()[0]
    log.info("{} book15={:,} at-touch candidates={:,} ({:.0f}s)", m, nb, n0,
             time.time() - t0)
    con.execute(f"""
        COPY (
          SELECT '{m}' AS month, f.taker_direction AS side,
                 count(*) AS fills, sum(f.token_amount) AS shares,
                 sum(f.sgn * (f.price - f.mid60) * 100 * f.token_amount) AS pnl_cs,
                 sum(f.sgn * (f.price - f.mid60) * 100) AS sum_edge_c
          FROM (
            SELECT f0.*, m2.mid AS mid60
            FROM f0
            ASOF LEFT JOIN b15 m2
              ON f0.asset_id = m2.token_id AND f0.t60 >= m2.timestamp_ms
            WHERE f0.t60 - m2.timestamp_ms <= {MID60_STALE_MS}
              AND m2.mid > 0 AND m2.mid < 1
          ) f
          GROUP BY ALL
        ) TO '{out}' (FORMAT parquet)
    """)
    log.info("{} agg15m done in {:.0f}s", m, time.time() - t0)


def main() -> None:
    con = duckdb.connect()
    con.execute("SET threads=6")
    con.execute("SET memory_limit='6GB'")
    con.execute("SET temp_directory='data/tmp_duck'")
    con.execute("SET preserve_insertion_order=false")
    token_set(con)
    for m in sys.argv[1:] or MONTHS:
        run_month(con, m)


if __name__ == "__main__":
    main()
