"""Normalise the trade tape into one canonical `trades` table.

Why not query `sii/trades.parquet` directly
-------------------------------------------
It is 45.8 GB and ordered by time, not by market, so every per-market question
is a full scan of 1.27 B rows. Answers do not change; they just cost minutes
instead of seconds. This writes the same rows minus two things that are provably
noise, partitioned by month, so a market-scoped or date-scoped query reads a
handful of files.

Dropped, with evidence
----------------------
* **Zero-size fills** — `usd_amount = 0 OR token_amount = 0`: 10,974,552 rows
  across the tape (0.87 %). They are dust, and they distort trade counts,
  average size and any volume-at-price fill inference.
* **Self-trades** — `maker = taker`: 905 rows over four years.
* **Taker summary events** — `taker` equal to an exchange contract. SII already
  excludes them from this file (verified: 0 rows), so this is a guard, not a fix.
* **`transaction_hash` / `log_index` / `block_number`** — dropped from the
  canonical table because a 64-char random hex string per row is incompressible
  and would add ~40 GB across the tape. They stay in the raw file for audits.

Kept, and why it matters
------------------------
`asset_id` is the ERC-1155 token the row is denominated in, and `nonusdc_side`
says which outcome that is (`token1` / `token2`). `price` is that token's price;
the other token trades implicitly at `1 - price`. Do **not** sum `token_amount`
across `token1` and `token2` rows — they are different assets. `maker_side` is
the maker's direction on that token, and it is always the opposite of the
taker's (verified: 0 rows where they agree).

Output
------
data/processed/trades/year=YYYY/month=MM/trades.parquet

Usage
    uv run python -m polymarketbot.data.builders.build_trades         # all months
    uv run python -m polymarketbot.data.builders.build_trades --months 2026-06
    uv run python -m polymarketbot.data.builders.build_trades --force
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]

RAW = REPO_ROOT / "data" / "raw" / "sii" / "trades.parquet"
OUT = REPO_ROOT / "data" / "processed" / "trades"

# Contract addresses emitted as `taker` on summary events. Guard, not a fix.
EXCHANGES = (
    "0x4bfb41d5b3570defd03c39a9a4d8debd8b8982e",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
    "0xe111180000d2663c0091e4f400237545b87b996b",
    "0xe2222d279d744050d28e00520010520000310f59",
)


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 12")
    con.execute("SET memory_limit = '8GB'")
    con.execute("SET TimeZone = 'UTC'")
    return con


def months(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.sql(f"""
        SELECT DISTINCT strftime(to_timestamp(timestamp), '%Y-%m') AS m
        FROM read_parquet('{RAW.as_posix()}')
        ORDER BY m
    """).fetchall()
    return [r[0] for r in rows]


def build(con: duckdb.DuckDBPyConnection, month: str, force: bool) -> tuple[int, int]:
    year, mon = month.split("-")
    dest = OUT / f"year={year}" / f"month={mon}" / "trades.parquet"
    if dest.exists() and not force:
        n = con.sql(f"SELECT count(*) FROM read_parquet('{dest.as_posix()}')").fetchall()[0][0]
        return n, dest.stat().st_size

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Numeric bounds, not `strftime(to_timestamp(ts), '%Y-%m') = ...`: a
    # function call on the column defeats Parquet row-group pruning, so the
    # strftime version read all 1.27 B rows for a single month (>8 min with no
    # output). Comparing the raw epoch column prunes to the month's row groups.
    start = int(datetime(int(year), int(mon), 1, tzinfo=timezone.utc).timestamp())
    nxt = (datetime(int(year), int(mon), 1, tzinfo=timezone.utc)
           + timedelta(days=32)).replace(day=1)
    end = int(nxt.timestamp())
    guard = ", ".join(f"'{e}'" for e in EXCHANGES)
    con.execute(f"""
        COPY (
            SELECT
                condition_id,
                asset_id                    AS token_id,
                timestamp                   AS ts,
                CAST(price        AS FLOAT) AS price,
                CAST(token_amount AS FLOAT) AS size_shares,
                CAST(usd_amount   AS FLOAT) AS size_usdc,
                maker_direction             AS maker_side,
                maker,
                taker,
                nonusdc_side,
                contract
            FROM read_parquet('{RAW.as_posix()}')
            WHERE timestamp >= {start} AND timestamp < {end}
              AND usd_amount   > 0
              AND token_amount > 0
              AND maker <> taker
              AND lower(taker) NOT IN ({guard})
        ) TO '{dest.as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)
    """)
    n = con.sql(f"SELECT count(*) FROM read_parquet('{dest.as_posix()}')").fetchall()[0][0]
    return n, dest.stat().st_size


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", nargs="*", help="e.g. 2026-06 2026-07")
    ap.add_argument("--force", action="store_true", help="rebuild existing partitions")
    args = ap.parse_args()

    if not RAW.exists():
        log.info(f"missing {RAW}")
        return 1

    con = connect()
    todo = args.months or months(con)
    log.info(f"{len(todo)} month(s)\n")

    total = 0
    t_all = time.time()
    for i, m in enumerate(todo, 1):
        t0 = time.time()
        n, size = build(con, m, args.force)
        total += n
        log.info(f"  [{i}/{len(todo)}] {m}  {n:>12,} fills  {size/1e9:>6.2f} GB  "
              f"{time.time()-t0:>6.1f}s")

    log.info(f"\n{total:,} fills  {(time.time()-t_all)/60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
