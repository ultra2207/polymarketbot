"""Kalshi candlestick fetching and parsing.

The vendor returns one object per bar with `end_period_ts` (Unix seconds), the
trade-price OHLC nested under `price`, and separate `yes_bid` / `yes_ask` OHLC
sub-objects, all as dollar strings. A quiet bar carries only
`price.previous_dollars`; the parser leaves the missing OHLC as `None` rather
than inventing a flat bar at zero.

Verified against the live API on 2026-11-06:
`GET /trade-api/v2/series/{series}/markets/{ticker}/candlesticks?start_ts=&end_ts=&period_interval=`
returns 200 with `{"candlesticks": [...], "ticker": "..."}`. The endpoint caps a
request at 5,000 bars, so a 1-minute backfill is requested in multi-day blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

INTERVALS = (1, 60, 1440)
MAX_CANDLES_PER_REQUEST = 5_000


def _flt(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_candle(raw: dict[str, Any], *, ticker: str, series_ticker: str, interval_minutes: int) -> dict[str, Any]:
    """Flatten one vendor candle. Missing fields stay ``None``."""
    price = raw.get("price") or {}
    bid = raw.get("yes_bid") or {}
    ask = raw.get("yes_ask") or {}
    ts = raw.get("end_period_ts")
    ts_seconds = int(ts) if ts is not None else None
    return {
        "ticker": ticker,
        "series_ticker": series_ticker,
        "period_minutes": int(interval_minutes),
        "end_period_ts": ts_seconds,
        "ts_utc": (
            datetime.fromtimestamp(ts_seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if ts_seconds is not None
            else None
        ),
        "open": _flt(price.get("open_dollars")),
        "high": _flt(price.get("high_dollars")),
        "low": _flt(price.get("low_dollars")),
        "close": _flt(price.get("close_dollars")),
        "previous_close": _flt(price.get("previous_dollars")),
        "yes_bid_open": _flt(bid.get("open_dollars")),
        "yes_bid_high": _flt(bid.get("high_dollars")),
        "yes_bid_low": _flt(bid.get("low_dollars")),
        "yes_bid_close": _flt(bid.get("close_dollars")),
        "yes_ask_open": _flt(ask.get("open_dollars")),
        "yes_ask_high": _flt(ask.get("high_dollars")),
        "yes_ask_low": _flt(ask.get("low_dollars")),
        "yes_ask_close": _flt(ask.get("close_dollars")),
        "volume": _flt(raw.get("volume_fp")),
        "open_interest": _flt(raw.get("open_interest_fp")),
    }


def request_blocks(start_ts: int, end_ts: int, interval_minutes: int) -> list[tuple[int, int]]:
    """Split ``[start_ts, end_ts)`` into blocks below the 5,000-bar cap.

    A block is ``MAX_CANDLES_PER_REQUEST`` bars wide, minus one bar of slack so
    an inclusive boundary never overflows the cap.
    """
    if end_ts <= start_ts:
        return []
    span = (MAX_CANDLES_PER_REQUEST - 1) * 60 * int(interval_minutes)
    blocks: list[tuple[int, int]] = []
    cursor = int(start_ts)
    end_ts = int(end_ts)
    while cursor < end_ts:
        block_end = min(end_ts, cursor + span)
        blocks.append((cursor, block_end))
        cursor = block_end
    return blocks


def candle_url(host: str, series_ticker: str, ticker: str) -> str:
    return (
        f"https://{host}/trade-api/v2/series/{series_ticker}"
        f"/markets/{ticker}/candlesticks"
    )


@dataclass
class CandleFetch:
    rows: list[dict[str, Any]]
    requests: int = 0
    errors: int = 0


def fetch_candles(
    session: Any,
    *,
    host: str,
    series_ticker: str,
    ticker: str,
    start_ts: int,
    end_ts: int,
    interval_minutes: int = 1,
    timeout: float = 25.0,
) -> CandleFetch:
    """Fetch every bar in the window, one block at a time."""
    out = CandleFetch(rows=[])
    for block_start, block_end in request_blocks(start_ts, end_ts, interval_minutes):
        out.requests += 1
        try:
            response = session.get(
                candle_url(host, series_ticker, ticker),
                params={
                    "start_ts": block_start,
                    "end_ts": block_end,
                    "period_interval": int(interval_minutes),
                },
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001 - transport; counted, not retried here
            out.errors += 1
            continue
        if response.status_code != 200:
            out.errors += 1
            continue
        payload = response.json()
        for raw in payload.get("candlesticks") or []:
            out.rows.append(
                parse_candle(
                    raw,
                    ticker=ticker,
                    series_ticker=series_ticker,
                    interval_minutes=interval_minutes,
                )
            )
    return out


def iter_months(start_ts: int, end_ts: int) -> Iterator[tuple[str, int, int]]:
    """Yield ``(YYYY-MM, month_start_ts, month_end_ts)`` covering the window."""
    cursor = datetime.fromtimestamp(int(start_ts), UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    end = datetime.fromtimestamp(int(end_ts), UTC)
    while cursor < end:
        month_key = cursor.strftime("%Y-%m")
        if cursor.month == 12:
            next_month = cursor.replace(year=cursor.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            next_month = cursor.replace(month=cursor.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
        window_end = min(next_month, end)
        yield month_key, int(cursor.timestamp()), int(window_end.timestamp())
        cursor = next_month


def units_for(
    tickers: Sequence[tuple[str, str]],
    start_ts: int,
    end_ts: int,
) -> list[tuple[str, str, int, int]]:
    """``(ticker, series, month_start, month_end)`` work units, newest month first."""
    units = [
        (ticker, series, month_start, month_end)
        for ticker, series in tickers
        for _month, month_start, month_end in iter_months(start_ts, end_ts)
    ]
    return sorted(units, key=lambda unit: unit[2], reverse=True)


def pending_units(units: Iterable[tuple[str, str, int, int]], out_dir: Any) -> list[tuple[str, str, int, int]]:
    """Drop units whose parquet already exists under ``out_dir``."""
    from pathlib import Path

    root = Path(out_dir)
    current_month = datetime.now(UTC).strftime("%Y-%m")
    pending = []
    for unit in units:
        ticker, _series, month_start, _month_end = unit
        month = datetime.fromtimestamp(month_start, UTC).strftime("%Y-%m")
        path = root / f"month={month}" / f"ticker={ticker}.parquet"
        # The current month is still growing: never trust an existing file for it.
        if month == current_month or not path.exists():
            pending.append(unit)
    return pending
