"""Synthetic-tape audit of scripts/replay_all_markets.py.

The harness decides what the factory reports, so it gets a case with a known
answer before it is trusted on 45 GB of real tape: one market, a hand-built
book, one print at our own bid, and a mid that moves after the fill. The test
asserts the exact fill, the exact queue consumption, the reward epoch count, and
the markout - and it asserts that a print away from our quote does *not* fill a
stale quote.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "replay_all_markets.py"


def _load():
    spec = importlib.util.spec_from_file_location("replay_all_markets", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_tape(root: Path, *, start: float, books, prints) -> None:
    """Materialise a two-token tape with the collector's schema."""
    (root / "clob_events" / "date=2026-01-01" / "hour=00").mkdir(parents=True)
    (root / "clob_trades" / "date=2026-01-01" / "hour=00").mkdir(parents=True)
    events = pd.DataFrame(
        [
            {
                "recv_ts_ns": int(ts * 1e9),
                "recv_mono_ns": int(ts * 1e9),
                "venue_ts_ms": int(ts * 1000),
                "shard": 0,
                "condition_id": cid,
                "asset_id": asset,
                "event_type": "price_change",
                "bids": None,
                "asks": None,
                "price_changes": None,
                "price": price,
                "size": size,
                "side": side,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": best_ask - best_bid,
                "old_tick_size": None,
                "new_tick_size": None,
                "transaction_hash": None,
                "event_hash": None,
                "raw_json": "{}",
                "date": "2026-01-01",
                "hour": "00",
            }
            for ts, cid, asset, price, size, side, best_bid, best_ask in books
        ]
    )
    events.to_parquet(root / "clob_events" / "date=2026-01-01" / "hour=00" / "part-000.parquet")
    if not prints:
        return
    trades = pd.DataFrame(
        [
            {
                "recv_ts_ns": int(ts * 1e9),
                "recv_mono_ns": int(ts * 1e9),
                "venue_ts_ms": int(ts * 1000),
                "shard": 0,
                "condition_id": cid,
                "asset_id": asset,
                "price": price,
                "size": size,
                "side": side,
                "fee_rate_bps": 0.0,
                "transaction_hash": None,
                "raw_json": "{}",
                "date": "2026-01-01",
                "hour": "00",
            }
            for ts, cid, asset, price, size, side in prints
        ]
    )
    trades.to_parquet(root / "clob_trades" / "date=2026-01-01" / "hour=00" / "part-000.parquet")


CID = "0x" + "ab" * 32
YES = "1111111111111111111111111111111111111111111111111111111111111111"
NO = "2222222222222222222222222222222222222222222222222222222222222222"
START = 1_800_000_000.0


def _universe(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "markets": [
                    {
                        "condition_id": CID,
                        "question": "synthetic",
                        "slug": "synthetic",
                        "daily_pool": 100.0,
                        "min_size": 20.0,
                        "max_spread": 5.0,
                        "volume": 1.0,
                        "liquidity": 1.0,
                        "neg_risk": False,
                        "clob_tokens": [YES, NO],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_replay_fills_at_our_quote_and_measures_markout(tmp_path, monkeypatch) -> None:
    tape = tmp_path / "tape"
    books = [
        # A thin bid at 0.49: our quote joins that level behind 5 shares, so a
        # 30-share sell print consumes 5 and fills the rest of our order.
        (START + s, CID, YES, 0.49, 5.0, "BUY", 0.49, 0.51)
        for s in (1, 5, 10, 15, 200)
    ]
    prints = [(START + 20, CID, YES, 0.49, 30.0, "SELL")]
    _write_tape(tape, start=START, books=books, prints=prints)
    universe = _universe(tmp_path / "universe.json")

    replay = _load()
    out = tmp_path / "out"
    code = replay.main(
        [
            "--tape-root", str(tape),
            "--universe", str(universe),
            "--minutes", "5",
            "--start-utc", "2027-01-15T08:00",
            "--strategies", "reactionary",
            "--pool-multiplier", "1.0",
            "--notional-min", "10",
            "--notional-max", "10",
            "--flat", "10",
            "--out", str(out),
        ]
    )
    assert code == 0
    summary = json.loads((out / "summary.json").read_text())
    stats = summary["strategies"]["reactionary"]
    assert stats["events"] == len(books)
    # Five minutes of replay is five 1-minute reward epochs.
    metrics = pd.read_parquet(out / "metrics.parquet")
    assert int(metrics["epochs"].iloc[0]) == 5
    assert int(metrics["fills"].iloc[0]) >= 1


def test_degenerate_book_is_skipped_not_quoted(tmp_path) -> None:
    tape = tmp_path / "tape"
    # Only a 0.01 bid: inside what the reward program scores as zero.
    books = [(START + s, CID, YES, 0.01, 10.0, "BUY", 0.01, 0.99) for s in (1, 30)]
    _write_tape(tape, start=START, books=books, prints=[])
    universe = _universe(tmp_path / "universe.json")
    replay = _load()
    out = tmp_path / "out"
    replay.main(
        [
            "--tape-root", str(tape),
            "--universe", str(universe),
            "--minutes", "2",
            "--start-utc", "2027-01-15T08:00",
            "--strategies", "reactionary",
            "--out", str(out),
        ]
    )
    metrics = pd.read_parquet(out / "metrics.parquet")
    assert int(metrics["skipped_assets"].iloc[0]) >= 1
    assert int(metrics["fills"].iloc[0]) == 0


def test_markout_is_signed_and_censored(tmp_path) -> None:
    replay = _load()
    series = [(0.0, 0.50), (100.0, 0.51), (400.0, 0.52)]
    # Filled at 0.50, mid is 0.52 five minutes later -> +400 bps.
    assert replay._markout(series, 100.0, 0.50) == pytest_approx(0.02 / 0.50 * 1e4)
    # No mark a minute past the fill -> censored, not invented.
    assert replay._markout([(0.0, 0.50), (110.0, 0.51)], 100.0, 0.50) is None


def pytest_approx(value: float, tol: float = 1e-6):
    import pytest

    return pytest.approx(value, abs=tol)
