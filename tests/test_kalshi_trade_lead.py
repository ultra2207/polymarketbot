"""Pure-function tests for scripts/report_kalshi_trade_lead.py.

The H2 harness decides whether Kalshi initiates Polymarket trades, so the sign
conventions and the next-print search are the parts that must be right: a NO
buy is a sell of YES, a NO-token buy is a sell of YES at the condition level,
the search is strictly after the event, and the null must never sample inside a
Kalshi burst. No tape, no network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "report_kalshi_trade_lead.py"


def _load():
    spec = importlib.util.spec_from_file_location("kalshi_trade_lead", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lead = _load()


def test_kalshi_sign_maps_both_field_generations() -> None:
    assert lead.kalshi_sign("yes", None) == 1
    assert lead.kalshi_sign("no", None) == -1
    assert lead.kalshi_sign(None, "YES") == 1
    assert lead.kalshi_sign(None, "NO") == -1
    assert lead.kalshi_sign(None, None) is None
    assert lead.kalshi_sign("weird", "unknown") is None


def test_pm_sign_flips_the_no_token() -> None:
    assert lead.pm_sign("BUY", yes_token=True) == 1
    assert lead.pm_sign("SELL", yes_token=True) == -1
    assert lead.pm_sign("BUY", yes_token=False) == -1
    assert lead.pm_sign("SELL", yes_token=False) == 1
    assert lead.pm_sign("merge", yes_token=True) is None


def test_next_latency_is_strictly_after_and_censored() -> None:
    ts = np.array([100.0, 101.0, 103.0])
    assert lead.next_latency(ts, 100.0) == 1.0
    assert lead.next_latency(ts, 100.5) == 0.5
    assert lead.next_latency(ts, 103.0) is None
    assert lead.next_latency(ts, 50.0, max_latency=60.0) == 50.0


def test_next_sign_returns_the_signed_first_print() -> None:
    ts = np.array([10.0, 12.0])
    signs = np.array([-1.0, 1.0])
    latency, sign = lead.next_sign(ts, signs, 10.0)
    assert latency == 2.0 and sign == 1.0
    latency, sign = lead.next_sign(ts, signs, 12.0)
    assert latency is None and sign is None


def test_null_never_samples_inside_a_kalshi_burst() -> None:
    blocked = np.array([100.0, 100.5])
    draw = lead.null_times(0.0, 200.0, 50, blocked=blocked, guard=5.0, seed=3)
    assert len(draw) > 0
    assert np.all(np.abs(draw - 100.0) > 5.0)


def test_reaction_rows_marks_agreement_and_blocks() -> None:
    rows = lead.reaction_rows(
        [10.0, 20.0, 30.0],
        [1, -1, 1],
        np.array([10.5, 25.0]),
        np.array([1.0, -1.0]),
        max_latency=60.0,
        block_flags=[False, True, False],
        sizes=[10.0, 500.0, 20.0],
    )
    assert rows[0]["latency_s"] == 0.5 and rows[0]["agree"] is True
    assert rows[1]["latency_s"] == 5.0 and rows[1]["agree"] is True
    assert rows[2]["latency_s"] is None and rows[2]["agree"] is None
    assert [r["is_block"] for r in rows] == [False, True, False]


def test_summarize_counts_only_inside_the_horizon() -> None:
    rows = [
        {"latency_s": 0.5, "agree": True, "is_block": False, "size": 1.0},
        {"latency_s": 0.9, "agree": False, "is_block": False, "size": 1.0},
        {"latency_s": 9.0, "agree": True, "is_block": False, "size": 1.0},
        {"latency_s": None, "agree": None, "is_block": False, "size": 1.0},
    ]
    stats = lead.summarize(rows, horizon=1.0)
    assert stats["n"] == 4
    assert stats["n_within"] == 2
    assert stats["p_within"] == 0.5
    assert stats["agree"] == 0.5
    assert np.isclose(stats["mean_latency_s"], 0.7)


def test_parse_iso_epoch_handles_kalshi_timestamps() -> None:
    assert lead.parse_iso_epoch("2026-11-06T00:00:00Z") is not None
    assert lead.parse_iso_epoch("2026-11-06T00:00:00+00:00") is not None
    assert lead.parse_iso_epoch(None) is None
    assert lead.parse_iso_epoch("garbage") is None
