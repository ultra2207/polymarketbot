"""Pure-function tests for scripts/measure_lead_time.py.

The Gate 3 verdict is the part that must never be wrong in the optimistic
direction, so PASS/FAIL/INCONCLUSIVE/NO_DATA each get a synthetic case. No tape,
no network: the module is loaded from the script path and only its pure helpers
are called.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MS = 1_000_000
SECOND = 1_000_000_000


@pytest.fixture(scope="module")
def measure():
    path = Path(__file__).resolve().parent.parent / "scripts" / "measure_lead_time.py"
    spec = importlib.util.spec_from_file_location("measure_lead_time_two_legs", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ reactions
def test_reactions_take_the_first_move_after_each_binance_tick(measure):
    binance = [1_000 * MS, 2_000 * MS, 5_000 * MS]
    clob = [1_150 * MS, 2_050 * MS]
    assert measure._reactions(binance, clob, 500 * MS) == [150 * MS, 50 * MS]


def test_reactions_drop_ticks_with_no_move_inside_the_gap(measure):
    binance = [1_000 * MS]
    clob = [3_000 * MS]
    assert measure._reactions(binance, clob, 500 * MS) == []


def test_reactions_count_a_move_at_the_same_instant_as_zero(measure):
    assert measure._reactions([1_000 * MS], [1_000 * MS], 500 * MS) == [0]


def test_reactions_with_a_delay_are_the_noise_floor(measure):
    binance = [1_000 * MS]
    clob = [1_150 * MS, 6_200 * MS]
    assert measure._reactions(binance, clob, 500 * MS) == [150 * MS]
    assert measure._reactions(binance, clob, 500 * MS, 5_000 * MS) == [200 * MS]


# ---------------------------------------------------------------------- stats
def test_stats_reports_percentiles_and_the_gate_share(measure):
    stats = measure._stats([50 * MS, 150 * MS, 250 * MS, 350 * MS])
    assert stats["n"] == 4
    assert stats["min_ms"] == 50.0
    assert stats["p50_ms"] == 200.0
    assert stats["p90_ms"] == 320.0
    assert stats["share_after_gate"] == 0.75


def test_stats_shifts_by_the_clock_correction(measure):
    stats = measure._stats([50 * MS, 150 * MS], shift_ns=100 * MS)
    assert stats["min_ms"] == 150.0
    assert stats["share_after_gate"] == 1.0


def test_stats_of_nothing_is_an_empty_row(measure):
    assert measure._stats([]) == {"n": 0}


# --------------------------------------------------------------------- verdict
def test_verdict_fails_when_the_point_lead_is_below_the_gate(measure):
    quote = [200 * MS, 300 * MS, 400 * MS]
    taker = [250 * MS, 350 * MS, 450 * MS]  # p50 350 -> lead 50 ms
    lead = measure._lead_verdict(quote, taker, [-10.0, 10.0])
    assert lead["lead_ms"] == 50.0
    assert lead["verdict"] == measure.FAIL


def test_verdict_fails_even_inside_a_wide_bracket(measure):
    quote = [100 * MS, 300 * MS, 500 * MS]
    taker = [100 * MS, 320 * MS, 500 * MS]  # lead 20 ms
    lead = measure._lead_verdict(quote, taker, [-198.9, 160.8])
    assert lead["verdict"] == measure.FAIL


def test_verdict_passes_with_a_tight_sampling_interval(measure):
    quote = [200 * MS, 300 * MS, 400 * MS] * 30
    taker = [400 * MS, 500 * MS, 600 * MS] * 30  # p50 500 -> lead 200 ms
    lead = measure._lead_verdict(quote, taker, [0.0, 50.0])
    assert lead["lead_ms"] == 200.0
    assert lead["bracket_width_ms"] == 50.0
    assert lead["lead_ci_ms"][0] >= 100.0
    assert lead["verdict"] == measure.PASS


def test_verdict_passes_at_exactly_the_gate(measure):
    quote = [300 * MS] * 40
    taker = [400 * MS] * 40  # lead exactly 100 ms
    lead = measure._lead_verdict(quote, taker, [-50.0, 49.9])
    assert lead["lead_ms"] == 100.0
    assert lead["verdict"] == measure.PASS


def test_verdict_is_inconclusive_when_sampling_crosses_the_gate(measure):
    quote = [100 * MS, 500 * MS] * 40
    taker = [300 * MS, 700 * MS] * 40
    lead = measure._lead_verdict(quote, taker, [-10.0, 10.0])
    assert lead["lead_ms"] == 200.0
    assert lead["lead_ci_ms"][0] < 100.0
    assert lead["verdict"] == measure.INCONCLUSIVE


def test_verdict_with_our_path_uses_the_margin_and_the_clock_bracket(measure):
    quote = [300 * MS] * 40
    taker = [1400 * MS] * 40
    lead = measure._lead_verdict(quote, taker, [-160.0, 160.0], our_path_ms=200.0)
    assert lead["margin_ms"] == 1200.0
    assert lead["verdict"] == measure.PASS


def test_verdict_without_a_clock_bracket_uses_sampling_only(measure):
    lead = measure._lead_verdict([300 * MS] * 40, [500 * MS] * 40, None)
    assert lead["lead_ms"] == 200.0
    assert lead["bracket_ms"] is None
    assert lead["verdict"] == measure.PASS


def test_verdict_without_taker_moves_is_no_data(measure):
    lead = measure._lead_verdict([300 * MS, 400 * MS], [], [-10.0, 10.0])
    assert lead["quote_p50_ms"] == 350.0
    assert lead["taker_p50_ms"] is None
    assert lead["lead_ms"] is None
    assert lead["verdict"] == measure.NO_DATA


def test_verdict_without_quote_moves_is_no_data(measure):
    lead = measure._lead_verdict([], [300 * MS], [-10.0, 10.0])
    assert lead["verdict"] == measure.NO_DATA


def test_verdict_lead_cancels_the_shared_clock_correction(measure):
    quote = [200 * MS, 300 * MS, 400 * MS]
    taker = [400 * MS, 500 * MS, 600 * MS]
    plain = measure._lead_verdict(quote, taker, [-198.9, 160.8])
    shifted = measure._lead_verdict(quote, taker, [-198.9, 160.8], shift_ns=-80 * MS)
    assert shifted["quote_p50_ms"] == plain["quote_p50_ms"] - 80.0
    assert shifted["taker_p50_ms"] == plain["taker_p50_ms"] - 80.0
    assert shifted["lead_ms"] == plain["lead_ms"]
    assert shifted["verdict"] == plain["verdict"]
