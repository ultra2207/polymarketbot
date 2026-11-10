"""Streamlit UI: module exists, compiles, imports without a runtime, reads the journal."""

from __future__ import annotations

import py_compile
import sqlite3
from pathlib import Path

APP = Path("src/polymarketbot/ui/app.py")


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


def test_ui_treats_an_empty_journal_file_as_no_data(tmp_path: Path):
    from polymarketbot.ui.app import read_snapshots

    empty = tmp_path / "paper_engine.db"
    empty.write_bytes(b"")
    assert read_snapshots(empty) == []
