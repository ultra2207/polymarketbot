"""Per-market activity statistics from the trade tape.

This is the point-in-time backbone of the universe: "had this market traded by
time t?" is answered from fills with `timestamp <= t`, never from the current
`volume` column (which is a latest-snapshot value and leaks the future).

Zero-size fills are excluded here (0.87% of rows, `usd_amount = 0`), matching
the integrity finding in Phase 1.

Output
    data/processed/market_trade_stats.parquet

Usage
    uv run scripts/build_market_stats.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "src")

from polymarketbot.db import connect

OUT = Path("data/processed")
TRADES = Path("data/raw/sii/trades.parquet")
DEST = OUT / "market_trade_stats.parquet"


def main() -> int:
    con = connect(threads=12, memory_limit="8GB")

    print("aggregating 1.27 B fills by condition_id ...", flush=True)
    print("  (no distinct-counts here: they turn a 4 M-group aggregation into a")
    print("   per-group hash set over 1.27 B rows and effectively hang)", flush=True)
    con.execute(f"""
        COPY (
            SELECT
                condition_id,
                min(timestamp)                                    AS first_fill_ts,
                max(timestamp)                                    AS last_fill_ts,
                count(*)                                          AS fills,
                sum(usd_amount)                                   AS traded_usd,
                sum(token_amount)                                 AS traded_shares,
                count(*) FILTER (WHERE contract LIKE '%\\_V2' ESCAPE '\\') AS v2_fills,
                count(*) FILTER (WHERE contract NOT LIKE '%\\_V2' ESCAPE '\\') AS v1_fills
            FROM read_parquet('{TRADES.as_posix()}')
            WHERE usd_amount > 0
            GROUP BY 1
        ) TO '{DEST.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    n = con.sql(f"SELECT count(*) FROM read_parquet('{DEST.as_posix()}')").fetchall()[0][0]
    print(f"wrote {DEST}  ({n:,} conditions)")

    row = con.sql(f"""
        SELECT
            count(*) n,
            count(*) FILTER (WHERE v2_fills > 0 AND v1_fills > 0) mixed_protocol,
            count(*) FILTER (WHERE v2_fills > 0 AND v1_fills = 0) v2_only,
            count(*) FILTER (WHERE v1_fills > 0 AND v2_fills = 0) v1_only
        FROM read_parquet('{DEST.as_posix()}')
    """).fetchall()[0]
    print(f"  v1 only      {row[3]:>10,}")
    print(f"  v2 only      {row[2]:>10,}")
    print(f"  mixed        {row[1]:>10,}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
