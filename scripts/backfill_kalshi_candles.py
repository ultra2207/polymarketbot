#!/usr/bin/env python3
"""Backfill Kalshi 1-minute candlesticks for the mapped tickers.

The unit of work is one (ticker, calendar month). Each unit is one Parquet file
under ``data/raw/kalshi/candles/month=YYYY-MM/ticker=<ticker>.parquet``; an
existing file is skipped, so a run that hits its time cap is progress and the
next run resumes. A per-ticker call is requested in blocks below the vendor's
5,000-bar cap (3 days of 1-minute bars).

Usage
-----
    .venv/Scripts/python.exe scripts/backfill_kalshi_candles.py --limit 5
    .venv/Scripts/python.exe scripts/backfill_kalshi_candles.py --days 90
    .venv/Scripts/python.exe scripts/backfill_kalshi_candles.py --mapping data/derived/kalshi_mapping_v2.parquet
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from polymarketbot.kalshi_candles import (  # noqa: E402
    fetch_candles,
    pending_units,
    units_for,
)
from polymarketbot.logs import get_logger  # noqa: E402
from polymarketbot.net import install_doh_fallback  # noqa: E402

log = get_logger("backfill_kalshi_candles")

DEFAULT_MAPPING = Path("data/derived/kalshi_mapping_v2.parquet")
FALLBACK_MAPPING = Path("data/derived/kalshi_mapping.parquet")
DEFAULT_OUT = Path("data/raw/kalshi/candles")
HOST = "api.elections.kalshi.com"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mapping", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--interval", type=int, default=1, choices=(1, 60, 1440))
    ap.add_argument("--limit", type=int, default=0, help="cap tickers; 0 = all mapped")
    ap.add_argument("--offset", type=int, default=0, help="skip the first N selected tickers")
    ap.add_argument("--ticker", default="", help="one ticker only")
    ap.add_argument("--pause", type=float, default=0.05)
    return ap.parse_args(argv)


def load_pairs(path: Path) -> list[tuple[str, str]]:
    import pandas as pd

    frame = pd.read_parquet(path)
    if "kalshi_ticker" not in frame.columns:
        raise SystemExit(f"{path} has no kalshi_ticker column")
    pairs: list[tuple[str, str]] = []
    for row in frame.itertuples(index=False):
        ticker = getattr(row, "kalshi_ticker", None)
        series = getattr(row, "series_ticker", None) or str(ticker).split("-", 1)[0]
        if ticker:
            pairs.append((str(ticker), str(series)))
    # de-duplicate, keep order
    seen: set[str] = set()
    unique = []
    for ticker, series in pairs:
        if ticker in seen:
            continue
        seen.add(ticker)
        unique.append((ticker, series))
    return unique


def write_unit(path: Path, rows: list[dict[str, Any]]) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    tmp = path.with_suffix(".tmp.parquet")
    frame.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    mapping = args.mapping or (DEFAULT_MAPPING if DEFAULT_MAPPING.exists() else FALLBACK_MAPPING)
    if not mapping.exists():
        log.error(f"mapping not found: {mapping}")
        return 1
    pairs = load_pairs(mapping)
    if args.ticker:
        pairs = [(t, s) for t, s in pairs if t == args.ticker]
    if args.offset:
        pairs = pairs[args.offset :]
    if args.limit:
        pairs = pairs[: args.limit]
    if not pairs:
        log.error("no tickers selected")
        return 1

    end_ts = int(time.time())
    start_ts = int((datetime.now(UTC) - timedelta(days=args.days)).timestamp())
    units = units_for(pairs, start_ts, end_ts)
    pending = pending_units(units, args.out)
    log.info(
        f"mapping={mapping.name} tickers={len(pairs)} window={args.days}d "
        f"interval={args.interval}m units={len(units)} pending={len(pending)} out={args.out}"
    )
    if not pending:
        log.info("nothing pending")
        return 0

    install_doh_fallback({HOST})
    from polymarketbot.data.streams.kalshi import EdgePool, _new_session

    pool = EdgePool(HOST)
    ip = pool.first_reachable()
    session = _new_session(ip) if ip else None
    if session is None:
        import requests

        session = requests.Session()
    log.info(f"edge={ip or 'direct'}")

    started = time.monotonic()
    written_units = 0
    written_rows = 0
    errors = 0
    requests = 0
    for index, (ticker, series, month_start, month_end) in enumerate(pending, 1):
        month = datetime.fromtimestamp(month_start, UTC).strftime("%Y-%m")
        path = args.out / f"month={month}" / f"ticker={ticker}.parquet"
        result = fetch_candles(
            session,
            host=HOST,
            series_ticker=series,
            ticker=ticker,
            start_ts=month_start,
            end_ts=month_end,
            interval_minutes=args.interval,
        )
        requests += result.requests
        errors += result.errors
        if not result.rows:
            log.warning(f"[{index}/{len(pending)}] {ticker} {month}: no candles ({result.errors} errors)")
            continue
        write_unit(path, result.rows)
        written_units += 1
        written_rows += len(result.rows)
        log.info(
            f"[{index}/{len(pending)}] {ticker} {month}: {len(result.rows)} candles "
            f"req={result.requests} err={result.errors} elapsed={time.monotonic() - started:.1f}s"
        )
        if args.pause:
            time.sleep(args.pause)

    elapsed = time.monotonic() - started
    log.info(
        f"done: units={written_units} rows={written_rows} requests={requests} errors={errors} "
        f"elapsed={elapsed:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
