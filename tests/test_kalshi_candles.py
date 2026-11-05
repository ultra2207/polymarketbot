"""Kalshi candlestick parsing, block splitting and resume semantics."""

from datetime import UTC, datetime

import pytest

from polymarketbot.kalshi_candles import (
    MAX_CANDLES_PER_REQUEST,
    iter_months,
    parse_candle,
    pending_units,
    request_blocks,
    units_for,
)

FULL = {
    "end_period_ts": 1793905680,
    "open_interest_fp": "332977.31",
    "volume_fp": "12.00",
    "price": {
        "open_dollars": "0.0400",
        "high_dollars": "0.0500",
        "low_dollars": "0.0300",
        "close_dollars": "0.0450",
        "previous_dollars": "0.0400",
    },
    "yes_bid": {"open_dollars": "0.04", "high_dollars": "0.04", "low_dollars": "0.03", "close_dollars": "0.04"},
    "yes_ask": {"open_dollars": "0.05", "high_dollars": "0.05", "low_dollars": "0.04", "close_dollars": "0.05"},
}


def test_parse_full_candle():
    row = parse_candle(FULL, ticker="T", series_ticker="S", interval_minutes=1)
    assert row["open"] == pytest.approx(0.04)
    assert row["close"] == pytest.approx(0.045)
    assert row["volume"] == pytest.approx(12.0)
    assert row["open_interest"] == pytest.approx(332977.31)
    assert row["yes_ask_close"] == pytest.approx(0.05)
    assert row["period_minutes"] == 1
    assert row["ts_utc"] == "2026-11-05T19:08:00Z"


def test_quiet_candle_keeps_missing_ohlc_null():
    raw = {"end_period_ts": 1793905680, "price": {"previous_dollars": "0.0400"},
           "volume_fp": "0.00", "yes_bid": {}, "yes_ask": {}}
    row = parse_candle(raw, ticker="T", series_ticker="S", interval_minutes=1)
    assert row["open"] is None and row["close"] is None
    assert row["previous_close"] == pytest.approx(0.04)
    assert row["yes_bid_close"] is None
    assert row["volume"] == pytest.approx(0.0)


def test_request_blocks_respect_cap():
    span = (MAX_CANDLES_PER_REQUEST - 1) * 60
    blocks = request_blocks(0, span * 2 + 10, 1)
    assert len(blocks) == 3
    assert blocks[0] == (0, span)
    assert blocks[-1][1] == span * 2 + 10
    for start, end in blocks:
        assert (end - start) // 60 <= MAX_CANDLES_PER_REQUEST
    assert request_blocks(10, 10, 1) == []


def test_iter_months_starts_at_calendar_month():
    start = int(datetime(2026, 9, 15, tzinfo=UTC).timestamp())
    end = int(datetime(2026, 11, 6, tzinfo=UTC).timestamp())
    months = list(iter_months(start, end))
    assert [m[0] for m in months] == ["2026-09", "2026-10", "2026-11"]
    first_start = datetime.fromtimestamp(months[0][1], UTC)
    assert (first_start.day, first_start.hour) == (1, 0)


def test_pending_units_skips_closed_month_and_refreshes_current(tmp_path):
    now = datetime.now(UTC)
    current = (now.year, now.month)
    prev_year, prev_month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    prev_start = int(datetime(prev_year, prev_month, 1, tzinfo=UTC).timestamp())
    cur_start = int(datetime(current[0], current[1], 1, tzinfo=UTC).timestamp())
    units = units_for([("T", "S")], prev_start, int(now.timestamp()))
    prev_path = tmp_path / f"month={prev_year:04d}-{prev_month:02d}" / "ticker=T.parquet"
    cur_path = tmp_path / f"month={current[0]:04d}-{current[1]:02d}" / "ticker=T.parquet"
    prev_path.parent.mkdir(parents=True)
    prev_path.write_bytes(b"x")
    cur_path.parent.mkdir(parents=True)
    cur_path.write_bytes(b"x")
    pending = pending_units(units, tmp_path)
    months = [datetime.fromtimestamp(u[2], UTC).strftime("%Y-%m") for u in pending]
    assert f"{current[0]:04d}-{current[1]:02d}" in months
    assert f"{prev_year:04d}-{prev_month:02d}" not in months
    assert cur_start in [u[2] for u in units]
