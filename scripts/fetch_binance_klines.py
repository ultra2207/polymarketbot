"""Binance 1-minute klines as the reference series for crypto up/down markets.

Why this and not Chainlink
--------------------------
`docs/alpha-pipeline.md` settled this: resolution labels already come from on-chain
payouts, so the reference series is only a **feature**, never a source of truth. Binance
klines are therefore sufficient, and Chainlink would only be needed to reproduce the
TWAP exactly - something we never have to do.

Source
------
`data.binance.vision` monthly archives, not the REST API. The API allows ~1,200 requests
per minute and returns 1,000 candles each; a month of 1-minute data per symbol is 43,200
candles, so the monthly dump is one download instead of 44. The dumps are zipped CSV with
a column header and millisecond epoch timestamps.

Storage
-------
One parquet per symbol-month, skipped when already present, so an interrupted run resumes
and an incremental month costs one file. Peak disk is one zip.

Usage
    uv run scripts/fetch_binance_klines.py --start 2025-01 --end 2026-10
    uv run scripts/fetch_binance_klines.py --symbols BTCUSDT,SOLUSDT --start 2026-01 --end 2026-03
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, "src")

import duckdb
import requests
from tqdm import tqdm
from polymarketbot.logs import get_logger
log = get_logger(__name__)

OUT = Path("data/raw/binance/klines")
TMP = Path(".tmp/binance")
BASE = "https://data.binance.vision/data/spot/monthly/klines"

# Column names as published by Binance for the monthly kline dumps.
COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]


def months(start: str, end: str) -> list[tuple[int, int]]:
    y0, m0 = (int(x) for x in start.split("-"))
    y1, m1 = (int(x) for x in end.split("-"))
    out = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def fetch_one(symbol: str, year: int, month: int, dest: Path) -> str:
    if dest.exists() and dest.stat().st_size > 0:
        return "skip"
    url = f"{BASE}/{symbol}/1m/{symbol}-1m-{year}-{month:02d}.zip"
    TMP.mkdir(parents=True, exist_ok=True)
    zpath = TMP / f"{symbol}-{year}-{month:02d}.zip"
    # These archives are ~2 MB, so a single buffered request is simpler and far more
    # reliable than a streamed one: streamed downloads from this host have twice stalled
    # mid-transfer and hung until the process was killed.
    try:
        r = requests.get(url, timeout=180)
        if r.status_code == 404:
            return "absent"
        r.raise_for_status()
        zpath.write_bytes(r.content)
    except Exception:  # noqa: BLE001 - network flakiness
        zpath.unlink(missing_ok=True)
        return "error"

    try:
        with zipfile.ZipFile(zpath) as z:
            name = z.namelist()[0]
            csv_path = TMP / f"{symbol}-{year}-{month:02d}.csv"
            with z.open(name) as src, csv_path.open("wb") as out:
                out.write(src.read())

        # Some dumps ship a header row, some do not. Probe rather than assume.
        with csv_path.open("r", encoding="utf-8", errors="ignore") as fh:
            first = fh.readline()
        header = "open_time" in first.lower()

        dest.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect()
        con.execute("SET threads = 4")
        cols = ", ".join(f"column{i}: 'VARCHAR'" for i in range(len(COLUMNS)))
        con.execute(f"""
            COPY (
                -- Binance stamps are microseconds in the monthly dumps and
                -- milliseconds in the daily ones; normalise to epoch seconds.
                SELECT CASE WHEN CAST(column0 AS BIGINT) > 100000000000000
                            THEN CAST(column0 AS BIGINT) / 1000000
                            ELSE CAST(column0 AS BIGINT) / 1000 END AS open_time_s,
                       CAST(column1 AS DOUBLE)  AS open,
                       CAST(column2 AS DOUBLE)  AS high,
                       CAST(column3 AS DOUBLE)  AS low,
                       CAST(column4 AS DOUBLE)  AS close,
                       CAST(column5 AS DOUBLE)  AS volume,
                       CAST(column7 AS DOUBLE)  AS quote_volume,
                       CAST(column8 AS BIGINT)  AS trades
                FROM read_csv('{csv_path.as_posix()}', header={str(header).lower()},
                              columns={{{cols}}})
            ) TO '{dest.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        con.close()
        csv_path.unlink(missing_ok=True)
        return "ok"
    except Exception as e:  # noqa: BLE001 - malformed month should not stop the run
        return f"parse_error:{type(e).__name__}"
    finally:
        zpath.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT")
    ap.add_argument("--start", default="2025-01")
    ap.add_argument("--end", default=None,
                    help="default: last complete month")
    args = ap.parse_args()

    today = date.today()
    end = args.end
    if end is None:
        # The current month is still being written by Binance; stop at the last full one.
        prev = today.replace(day=1) - timedelta(days=1)
        end = f"{prev.year}-{prev.month:02d}"

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    todo = months(args.start, end)
    log.info(f"{len(symbols)} symbols x {len(todo)} months -> {OUT}", )

    counts = {"ok": 0, "skip": 0, "absent": 0, "error": 0}
    for symbol in symbols:
        bar = tqdm(todo, desc=symbol, unit="mo", dynamic_ncols=True)
        for (y, m) in bar:
            dest = OUT / f"symbol={symbol}" / f"{y}-{m:02d}.parquet"
            status = fetch_one(symbol, y, m, dest)
            key = "ok" if status == "ok" else (
                "skip" if status == "skip" else
                "absent" if status == "absent" else "error")
            counts[key] += 1
            bar.set_postfix_str(status)
    log.info(f"\nfetched={counts['ok']} skipped={counts['skip']} "
          f"absent={counts['absent']} errors={counts['error']}")

    files = sorted(OUT.rglob("*.parquet"))
    if files:
        con = duckdb.connect()
        r = con.sql(f"""
            SELECT count(*) rows, count(DISTINCT symbol) symbols,
                   to_timestamp(min(open_time_s)) first,
                   to_timestamp(max(open_time_s)) last
            FROM read_parquet('{(OUT / '**' / '*.parquet').as_posix()}',
                              hive_partitioning=true)
        """).fetchall()[0]
        log.info(f"on disk: {r[0]:,} minutes across {r[1]} symbols, "
              f"{r[2]} -> {r[3]}")
    return 0 if counts["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
