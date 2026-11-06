"""A paper session must survive being killed: snapshots are taken as it runs."""

from __future__ import annotations

import glob
from pathlib import Path
from types import SimpleNamespace

from polymarketbot.trading.session_recorder import PaperSessionRecorder


class _Quote:
    def __init__(self, side: str) -> None:
        self.side = side

    def to_dict(self) -> dict:
        return {"side": self.side, "price": 0.49, "size": 10.0}


def _engine() -> SimpleNamespace:
    market = SimpleNamespace(question="Q", rate_per_day=10.0)
    return SimpleNamespace(
        markets={"0xabc": market},
        positions={},
        fills=[],
        epoch_snapshots=[],
        virtual_quotes={"tok": {"bid": _Quote("BUY"), "ask": _Quote("SELL")}},
        get_portfolio_summary=lambda: {"current_cash_usd": 1.0},
    )


def test_snapshot_writes_without_stopping(tmp_path: Path) -> None:
    """The only write being in stop() is how a killed run loses everything."""
    rec = PaperSessionRecorder(_engine(), out_root=tmp_path, session_id="s1")
    rec.start()
    before = glob.glob(str(tmp_path / "s1" / "quotes" / "*.parquet"))
    rec.snapshot()
    after = glob.glob(str(tmp_path / "s1" / "quotes" / "*.parquet"))
    assert len(after) > len(before)
    assert rec._stopped_ns is None  # still running


def test_stop_finalises_the_manifest(tmp_path: Path) -> None:
    rec = PaperSessionRecorder(_engine(), out_root=tmp_path, session_id="s2")
    rec.start()
    rec.stop("run_complete")
    import json

    payload = json.loads((tmp_path / "s2" / "session.json").read_text(encoding="utf-8"))
    assert payload["end"]["reason"] == "run_complete"
    assert payload["end"]["rows_written"]["quotes"] >= 2
