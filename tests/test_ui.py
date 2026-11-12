"""Streamlit UI: module exists, compiles, imports without a runtime, reads the journal."""

from __future__ import annotations

import py_compile
import sqlite3
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "src" / "polymarketbot" / "ui" / "app.py"


def test_ui_files_exist():
    assert APP.exists()


def test_ui_syntax():
    py_compile.compile(str(APP), doraise=True)


def test_ui_imports_without_streamlit_runtime():
    import importlib

    module = importlib.import_module("polymarketbot.ui.app")
    # Split so the deleted package name is not itself a grep hit.
    assert "paper" + "_trading" not in APP.read_text(encoding="utf-8")
    assert module.read_snapshots(Path("does/not/exist.db")) == []
    assert module.read_trades(Path("does/not/exist.db")) == []
    assert module.read_lots(Path("does/not/exist.db")) == []


def test_ui_reads_the_live_paper_journal(tmp_path: Path):
    from polymarketbot.strategy.ledger import SCHEMA
    from polymarketbot.ui.app import (
        format_ts,
        lot_value,
        read_lots,
        read_snapshots,
        read_trades,
        summarise,
    )

    db = tmp_path / "paper_engine.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO snapshots (ts, strategy, cash, positions_value, equity)"
        " VALUES (100.0, 'reward', 900.0, 100.0, 1000.0),"
        "        (110.0, 'reward', 905.0, 120.0, 1025.0)"
    )
    conn.execute(
        "INSERT INTO trades (ts, strategy, kind, condition_id, token_id, shares,"
        " price, fee, cash_delta, detail)"
        " VALUES (105.0, 'reward', 'fill', '0xabc', 'tok', 10.0, 0.5, 0.01, -5.01, 'bid fill')"
    )
    conn.execute(
        "INSERT INTO lots (strategy, condition_id, token_id, shares, cost_usd,"
        " par_value, last_mark, tag)"
        " VALUES ('reward', '0xabc', 'tok', 10.0, 5.0, NULL, 0.5, 'maker')"
    )
    conn.commit()
    conn.close()

    snapshots = read_snapshots(db)
    trades = read_trades(db)
    lots = read_lots(db)
    assert [row["equity"] for row in snapshots] == [1000.0, 1025.0]
    assert trades[0]["kind"] == "fill"
    assert lot_value(lots[0]) == 5.0

    state = summarise(snapshots, trades)
    assert state["total_equity"] == 1025.0
    assert state["pnl"] == 25.0
    assert state["cash"] == 905.0
    assert state["positions_value"] == 120.0
    assert state["trade_count"] == 1
    assert state["rows"][0]["PnL"] == 25.0
    assert format_ts(None) == "never"
    assert "IST" in format_ts(100.0)


def test_ui_headline_fees_come_from_the_whole_journal(tmp_path: Path):
    """A 500-row display window must not become the reported fee total."""
    from polymarketbot.strategy.ledger import SCHEMA
    from polymarketbot.ui.app import read_totals, summarise

    db = tmp_path / "paper_engine.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO trades (ts, strategy, kind, condition_id, token_id, shares,"
        " price, fee, cash_delta, detail) VALUES (?, 'reward', 'fill', '0xabc',"
        " 'tok', 1.0, 0.5, ?, -1.0, '')",
        [(float(i), 0.01 * i) for i in range(1, 601)],
    )
    conn.commit()
    conn.close()

    totals = read_totals(db)
    assert totals["trade_count"] == 600
    assert totals["fees"] == pytest.approx(sum(0.01 * i for i in range(1, 601)))

    window = [
        {"strategy": "reward", "fee": 0.01, "kind": "fill"},
    ]
    state = summarise([], window, totals)
    assert state["trade_count"] == 600  # journal-wide, not the window
    assert state["window_trade_count"] == 1
    assert state["fees"] == pytest.approx(totals["fees"])
    assert state["window_fees"] == pytest.approx(0.01)


def test_ui_treats_an_empty_journal_file_as_no_data(tmp_path: Path):
    from polymarketbot.ui.app import read_snapshots

    empty = tmp_path / "paper_engine.db"
    empty.write_bytes(b"")
    assert read_snapshots(empty) == []


def _snapshot_db(tmp_path: Path, strategies=("reward", "ctf"), ticks=10) -> Path:
    from polymarketbot.strategy.ledger import SCHEMA

    db = tmp_path / "soak.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO snapshots (ts, strategy, cash, positions_value, equity)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (100.0 + tick, name, 1000.0 + tick, 0.0, 1000.0 + tick)
            for tick in range(ticks)
            for name in strategies
        ],
    )
    conn.commit()
    conn.close()
    return db


def test_chart_series_is_bounded_per_strategy(tmp_path: Path):
    from polymarketbot.ui.app import read_snapshots

    db = _snapshot_db(tmp_path, strategies=("reward", "ctf", "settle"), ticks=50)
    rows = read_snapshots(db, max_points=5)
    # Three strategies x five points: the bound is per line, not per table.
    assert len(rows) == 15
    per_strategy: dict[str, list[float]] = {}
    for row in rows:
        per_strategy.setdefault(row["strategy"], []).append(row["ts"])
    assert all(len(ts) == 5 for ts in per_strategy.values())
    assert all(ts == sorted(ts) for ts in per_strategy.values())
    # It is the *most recent* window: the newest tick is present, the first is not.
    assert max(row["ts"] for row in rows) == 149.0
    assert min(row["ts"] for row in rows) == 145.0


def test_headline_uses_journal_edges_not_the_chart_window(tmp_path: Path):
    """The chart window must not become the initial equity (that shrinks PnL)."""
    from polymarketbot.ui.app import read_snapshot_edges, read_snapshots, summarise

    db = _snapshot_db(tmp_path, strategies=("reward",), ticks=100)
    window = read_snapshots(db, max_points=5)
    first, last = read_snapshot_edges(db)
    assert first[0]["ts"] == 100.0 and first[0]["equity"] == 1000.0
    assert last[0]["ts"] == 199.0 and last[0]["equity"] == 1099.0

    state = summarise(window, [], None, edges=(first, last))
    assert state["total_equity"] == 1099.0
    assert state["initial_equity"] == 1000.0  # not the window's 1095.0
    assert state["pnl"] == 99.0

    window_state = summarise(window, [])
    # The window's first row (ts 195) would have been read as the initial equity.
    assert window_state["initial_equity"] == 1095.0
