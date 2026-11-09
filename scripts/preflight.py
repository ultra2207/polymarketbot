"""Pre-download checks for the Polymarket data stack.

Three things, all cheap:

1. Real schema + row counts for each remote file, read from the Parquet footer
   only (no bulk download).
2. Market lifetime distribution, from the small `markets.parquet` (545 MB).
3. Composition checks: volume by creation year, how much of the book is the
   short-horizon crypto ("updown") market family, resolution-value shapes, and
   null rates on fields we plan to rely on.

Usage:
    uv run scripts/preflight.py
"""

from __future__ import annotations

import shutil

import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

SII = "https://huggingface.co/datasets/SII-WANGZJ/Polymarket_data/resolve/main"
MOOSE = "https://huggingface.co/datasets/moose-code/polymarket-onchain-v1/resolve/main"

FILES: dict[str, str] = {
    "SII trades": f"{SII}/trades.parquet",
    "SII markets": f"{SII}/markets.parquet",
    "SII users": f"{SII}/users.parquet",
    "SII quant": f"{SII}/quant.parquet",
    "SII orderfilled": f"{SII}/orderfilled.parquet",
    "MOOSE order_filled 2026-02": f"{MOOSE}/order_filled/year=2026/month=02.parquet",
    "PMXT v2 hour 2026-07-15T12": (
        "https://r2v2.pmxt.dev/polymarket_orderbook_2026-07-15T12.parquet"
    ),
    "Dome snapshots": (
        "https://huggingface.co/datasets/vpcapitano/polymarket-genesis-liquidity"
        "/resolve/main/data/dome_polymarket_2025-10_2026-03.parquet"
    ),
}


def section(title: str) -> None:
    log.info(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def file_metadata(con: duckdb.DuckDBPyConnection) -> None:
    section("1. file schemas and row counts (footer reads only)")

    for label, url in FILES.items():
        log.info(f"\n--- {label}")
        try:
            meta = con.sql(f"""
                SELECT num_rows, num_row_groups, format_version
                FROM parquet_file_metadata('{url}')
            """).fetchall()[0]
            log.info(f"    rows={meta[0]:,}  row_groups={meta[1]:,}  format={meta[2]}")
        except Exception as exc:  # noqa: BLE001
            log.info(f"    metadata failed: {type(exc).__name__}: {exc}")
            continue

        try:
            cols = con.sql(f"""
                SELECT name, duckdb_type
                FROM parquet_schema('{url}')
                WHERE num_children IS NULL
                ORDER BY column_id
            """).fetchall()
            names = [f"{c}:{t}" for c, t in cols]
            log.info("    columns: " + ", ".join(names[:14]) + (" ..." if len(names) > 14 else ""))
        except Exception as exc:  # noqa: BLE001
            log.info(f"    schema failed: {type(exc).__name__}: {exc}")


def market_shape(con: duckdb.DuckDBPyConnection) -> None:
    section("2. market lifetime and composition (SII markets.parquet)")

    url = FILES["SII markets"]
    con.execute(f"""
        CREATE OR REPLACE VIEW m AS
        SELECT
            *,
            TRY_CAST(created_at AS TIMESTAMPTZ) AS c,
            TRY_CAST(end_date   AS TIMESTAMPTZ) AS e,
            CAST(volume AS DOUBLE)              AS vol
        FROM read_parquet('{url}')
    """)

    total = con.sql("SELECT count(*), sum(vol) FROM m").fetchall()[0]
    log.info(f"markets={total[0]:,}  total volume=${total[1]:,.0f}")

    log.info("\nlifetime (end_date - created_at), days:")
    log.info("   year    n          p10     p50     p90     share<1d")
    rows = con.sql("""
        SELECT
            date_part('year', c) AS yr,
            count(*) AS n,
            quantile_cont(epoch(e - c) / 86400.0, 0.10) AS p10,
            quantile_cont(epoch(e - c) / 86400.0, 0.50) AS p50,
            quantile_cont(epoch(e - c) / 86400.0, 0.90) AS p90,
            avg(CASE WHEN epoch(e - c) < 86400 THEN 1.0 ELSE 0.0 END) * 100 AS under_1d
        FROM m
        WHERE c IS NOT NULL AND e IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """).fetchall()
    for yr, n, p10, p50, p90, u1 in rows:
        log.info(f"   {int(yr)}  {n:>9,}  {p10:7.2f} {p50:7.2f} {p90:7.2f}  {u1:6.1f}%")

    log.info("\nvolume by creation year:")
    log.info("   year    markets      volume($B)   updown share of volume")
    rows = con.sql("""
        SELECT
            date_part('year', c) AS yr,
            count(*) AS n,
            sum(vol) / 1e9 AS vol_b,
            sum(CASE WHEN lower(slug) LIKE '%updown%'
                       OR lower(slug) LIKE '%up-or-down%'
                     THEN vol ELSE 0 END) / nullif(sum(vol), 0) * 100 AS updown_pct
        FROM m
        GROUP BY 1 ORDER BY 1
    """).fetchall()
    for yr, n, vol_b, up in rows:
        log.info(f"   {int(yr)}  {n:>9,}  {vol_b:9.2f}   {up:6.1f}%" if up is not None
              else f"   {int(yr)}  {n:>9,}  {vol_b:9.2f}        n/a")

    log.info("\nupdown family as share of all markets:")
    row = con.sql("""
        SELECT
            avg(CASE WHEN lower(slug) LIKE '%updown%'
                       OR lower(slug) LIKE '%up-or-down%' THEN 1.0 ELSE 0.0 END) * 100
        FROM m
    """).fetchall()[0]
    log.info(f"   {row[0]:.1f}% of markets")

    log.info("\nnull / empty rates on fields we rely on:")
    row = con.sql("""
        SELECT
            avg(CASE WHEN end_date IS NULL THEN 1.0 ELSE 0.0 END) * 100,
            avg(CASE WHEN created_at IS NULL THEN 1.0 ELSE 0.0 END) * 100,
            avg(CASE WHEN outcome_prices IS NULL OR outcome_prices = '' THEN 1.0 ELSE 0.0 END) * 100,
            avg(CASE WHEN vol IS NULL OR vol = 0 THEN 1.0 ELSE 0.0 END) * 100
        FROM m
    """).fetchall()[0]
    for label, pct in zip(
        ["end_date", "created_at", "outcome_prices", "volume=0"], row
    ):
        log.info(f"   {label:16} {pct:6.2f}%")

    log.info("\ntop 10 outcome_prices shapes (how resolution is encoded):")
    rows = con.sql("""
        SELECT outcome_prices, count(*) AS n
        FROM m
        GROUP BY 1 ORDER BY n DESC LIMIT 10
    """).fetchall()
    for val, n in rows:
        log.info(f"   {str(val)[:40]:42} {n:>9,}")

    log.info("\ndata-quality flags by creation year:")
    log.info("   year   zero-vol   end<created   void(0.5/0.5)   non-binary final")
    rows = con.sql("""
        SELECT
            date_part('year', c) AS yr,
            avg(CASE WHEN vol = 0 THEN 1.0 ELSE 0.0 END) * 100          AS zero_vol,
            avg(CASE WHEN e < c THEN 1.0 ELSE 0.0 END) * 100            AS end_before_create,
            avg(CASE WHEN outcome_prices LIKE '%0.5%0.5%' THEN 1.0 ELSE 0.0 END) * 100 AS void_5050,
            avg(CASE WHEN outcome_prices NOT LIKE '%''0'', ''1''%'
                      AND outcome_prices NOT LIKE '%''1'', ''0''%'
                     THEN 1.0 ELSE 0.0 END) * 100                       AS non_binary
        FROM m
        WHERE c IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """).fetchall()
    for yr, zv, ebc, void, nb in rows:
        log.info(f"   {int(yr)}  {zv:8.1f}%  {ebc:11.1f}%  {void:13.1f}%  {nb:15.1f}%")


def main() -> None:
    free_gb = shutil.disk_usage(".").free / 1e9
    log.info(f"free disk on this drive: {free_gb:,.1f} GB")

    con = duckdb.connect()
    con.execute("SET threads = 4")
    file_metadata(con)
    market_shape(con)


if __name__ == "__main__":
    main()
