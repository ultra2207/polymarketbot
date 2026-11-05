"""Market tape recorder tests: Parquet layout, depth, trades, flush cadence.

No network: book and trade events are injected directly through the recorder's
feed listeners and the sampler runs on a fake clock.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarketbot.simulator.feed import MarketTrade, build_book_snapshot
from polymarketbot.simulator.tape import MarketTapeRecorder

CONDITION = "0xCONDITION"
TOKEN_YES = "1111"
TOKEN_NO = "2222"
T0 = 1_700_000_000.0  # fixed UTC instant


def make_recorder(tmp_path, **kwargs) -> MarketTapeRecorder:
    clock = kwargs.pop("clock", None)
    return MarketTapeRecorder(
        condition_id=CONDITION,
        token_yes=TOKEN_YES,
        token_no=TOKEN_NO,
        out_dir=tmp_path / "tape",
        clock=clock,
        **kwargs,
    )


def feed_books(recorder: MarketTapeRecorder) -> None:
    recorder.on_book(
        build_book_snapshot(
            TOKEN_YES,
            [{"price": 0.40, "size": 10.0}, {"price": 0.38, "size": 5.0}],
            [{"price": 0.42, "size": 7.0}, {"price": 0.45, "size": 3.0}],
            timestamp=T0,
        )
    )
    recorder.on_book(
        build_book_snapshot(
            TOKEN_NO,
            [{"price": 0.57, "size": 4.0}],
            [{"price": 0.60, "size": 6.0}],
            timestamp=T0,
        )
    )


def test_snapshot_columns_depths_and_trades(tmp_path):
    recorder = make_recorder(tmp_path, flush_interval=3600.0)
    feed_books(recorder)
    recorder.on_trade(MarketTrade(asset_id=TOKEN_YES, price=0.40, size=10.0, side="BUY", timestamp=T0))
    recorder.on_trade(MarketTrade(asset_id=TOKEN_YES, price=0.50, size=10.0, side="SELL", timestamp=T0))

    row = recorder.sample(now=T0 + 1.0)
    assert row is not None
    assert row["condition_id"] == CONDITION
    assert row["token_yes"] == TOKEN_YES
    assert row["token_no"] == TOKEN_NO
    assert row["bid_yes"] == pytest.approx(0.40)
    assert row["ask_yes"] == pytest.approx(0.42)
    assert row["mid_yes"] == pytest.approx(0.41)
    assert row["bid_no"] == pytest.approx(0.57)
    assert row["ask_no"] == pytest.approx(0.60)
    assert row["mid_no"] == pytest.approx(0.585)

    # Depth within 0.5c of the touch excludes the second level; 2c includes it.
    assert row["depth_yes_bid_0.5c"] == pytest.approx(10.0)
    assert row["depth_yes_bid_2c"] == pytest.approx(15.0)
    assert row["depth_yes_ask_0.5c"] == pytest.approx(7.0)
    assert row["depth_yes_ask_3.5c"] == pytest.approx(10.0)
    assert row["depth_no_bid_0.5c"] == pytest.approx(4.0)
    assert row["depth_no_ask_0.5c"] == pytest.approx(6.0)

    assert row["trades_since_last_count"] == 2
    assert row["trades_since_last_signed_volume"] == pytest.approx(0.0)
    assert row["trades_since_last_vwap"] == pytest.approx(0.45)

    # Trades reset after each sample.
    second = recorder.sample(now=T0 + 2.0)
    assert second["trades_since_last_count"] == 0
    assert second["trades_since_last_vwap"] is None


def test_parquet_hour_file_written_and_reloaded(tmp_path):
    recorder = make_recorder(tmp_path, flush_interval=3600.0)
    feed_books(recorder)
    for i in range(12):
        recorder.sample(now=T0 + i * 0.5)
    written = recorder.flush()
    assert len(written) == 1
    path = Path(list(written)[0])
    assert path.name.endswith(".parquet")
    assert path.exists()
    assert written[str(path)] == 12

    frame = pd.read_parquet(path)
    assert len(frame) == 12
    assert list(frame["mid_yes"].notna()) == [True] * 12
    assert list(frame["mid_no"].notna()) == [True] * 12
    assert "depth_yes_bid_3.5c" in frame.columns
    assert "trades_since_last_signed_volume" in frame.columns

    # A temp file never survives an atomic replace.
    assert not list(path.parent.glob("*.tmp"))


def test_flush_interval_writes_within_one_minute(tmp_path):
    clock = [T0]
    recorder = make_recorder(tmp_path, flush_interval=60.0, clock=lambda: clock[0])
    feed_books(recorder)
    recorder.sample()
    assert not list((tmp_path / "tape").glob("*.parquet"))

    clock[0] = T0 + 30.0
    recorder.sample()
    assert not list((tmp_path / "tape").glob("*.parquet"))

    clock[0] = T0 + 61.0
    recorder.sample()
    files = list((tmp_path / "tape").glob("*.parquet"))
    assert len(files) == 1
    assert len(pd.read_parquet(files[0])) == 3


def test_second_run_skips_hour_already_on_disk(tmp_path):
    first = make_recorder(tmp_path, flush_interval=3600.0)
    feed_books(first)
    for i in range(5):
        first.sample(now=T0 + i)
    first.flush(now=T0 + 5.0)

    second = make_recorder(tmp_path, flush_interval=3600.0)
    existing = second.existing_hours()
    assert len(existing) == 1
    feed_books(second)
    assert second.sample(now=T0 + 10.0) is None
    assert second.flush(now=T0 + 11.0) == {}
    assert second.output_files()[0].exists()


def test_mutated_existing_hour_is_not_truncated(tmp_path):
    first = make_recorder(tmp_path, flush_interval=3600.0)
    feed_books(first)
    for i in range(3):
        first.sample(now=T0 + i)
    first.flush(now=T0 + 3.0)
    path = first.output_files()[0]
    original = pd.read_parquet(path)

    second = make_recorder(tmp_path, flush_interval=3600.0)
    feed_books(second)
    second.sample(now=T0 + 30.0)
    second.flush(now=T0 + 31.0)
    assert len(pd.read_parquet(path)) == len(original)


def test_hour_rollover_flushes_and_retires_previous_hour(tmp_path):
    clock = [T0]
    recorder = make_recorder(tmp_path, flush_interval=7200.0, clock=lambda: clock[0])
    feed_books(recorder)
    recorder.sample()
    next_hour = T0 + 3660.0
    clock[0] = next_hour
    recorder.sample()
    written = recorder.flush()
    assert len(written) == 2
    assert len(list((tmp_path / "tape").glob("*.parquet"))) == 2
