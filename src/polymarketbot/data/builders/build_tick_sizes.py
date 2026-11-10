"""Materialise per-market tick size from Dome snapshots.

Dome is the only free source with historical tick size (2025-10-14 -> 2026-03-31).
`market_id` joins to SII `markets.id` for 163,908 of 163,908 Dome markets, so
this is a clean join.

Outside that window the tick size is unknown offline. The universe fills it
with 0.01 and flags it as assumed rather than pretending to know. PMXT's
`tick_size_change` events (Phase 4) can refine this later.

Usage
    uv run python -m polymarketbot.data.builders.build_tick_sizes
"""

from __future__ import annotations

import time
from pathlib import Path

from polymarketbot.db import connect
from polymarketbot.logs import get_logger
log = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]

OUT = REPO_ROOT / "data" / "processed"
DOME = REPO_ROOT / "data" / "raw" / "dome" / "dome_polymarket_2025-10_2026-03.parquet"
DEST = OUT / "market_tick.parquet"


def main() -> int:
    con = connect(threads=12, memory_limit="8GB")

    t0 = time.time()
    log.info("aggregating Dome tick sizes (339 M rows) ...")
    con.execute(f"""
        COPY (
            SELECT
                market_id,
                min(tick_size) AS tick_size,
                count(DISTINCT tick_size) AS n_tick_sizes
            FROM read_parquet('{DOME.as_posix()}')
            GROUP BY 1
        ) TO '{DEST.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    elapsed = time.time() - t0

    row = con.sql(f"""
        SELECT count(*) n, count(*) FILTER (WHERE n_tick_sizes > 1) multi
        FROM read_parquet('{DEST.as_posix()}')
    """).fetchall()[0]
    log.info(f"wrote {DEST}  ({row[0]:,} markets, {row[1]:,} with mixed ticks) in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
