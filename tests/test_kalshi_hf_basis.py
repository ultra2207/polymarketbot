"""Pure-function tests for scripts/report_kalshi_hf_basis.py.

The E1 harness decides what the report claims about the Kalshi-to-Polymarket
reprice, so each helper gets a synthetic case with a known answer: the clamped
logit, the null-safe basis, the ASOF staleness rule, the forward-return null
handling, the per-pair slope, the clustered summary, and the rank deciles. The
panel builder is exercised on a hand-built frame so the join is covered too. No
tape, no network - the script is loaded from its path and only its pure helpers
are called.
"""

from __future__ import annotations

import importlib.util
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SECOND_NS = 1_000_000_000
MINUTE_NS = 60 * SECOND_NS


@pytest.fixture(scope="module")
def hf():
    path = Path(__file__).resolve().parent.parent / "scripts" / "report_kalshi_hf_basis.py"
    spec = importlib.util.spec_from_file_location("report_kalshi_hf_basis_pure", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------- logit
def test_logit_is_zero_at_even_odds(hf) -> None:
    assert hf.logit([0.5])[0] == pytest.approx(0.0)


def test_logit_clamps_the_tails(hf) -> None:
    lo = float(hf.logit([0.0])[0])
    hi = float(hf.logit([1.0])[0])
    assert np.isfinite(lo) and np.isfinite(hi)
    assert lo == pytest.approx(math.log(1e-3 / (1.0 - 1e-3)))
    assert hi == pytest.approx(-lo)


# ---------------------------------------------------------------------- basis
def test_basis_is_the_logit_difference(hf) -> None:
    b = hf.basis([0.75], [0.5])
    assert b[0] == pytest.approx(math.log(3.0))


def test_basis_is_null_when_either_side_is_unusable(hf) -> None:
    b = hf.basis([0.6, np.nan, 0.6, 0.0, 1.0], [0.5, 0.5, np.nan, 0.5, 0.5])
    assert b[0] == pytest.approx(math.log(1.5))
    assert np.isnan(b[1]) and np.isnan(b[2]) and np.isnan(b[3]) and np.isnan(b[4])


def test_basis_rejects_mismatched_lengths(hf) -> None:
    with pytest.raises(ValueError):
        hf.basis([0.5], [0.5, 0.6])


# ------------------------------------------------------------------ asof join
def test_asof_pos_takes_the_last_quote_at_or_before_the_target(hf) -> None:
    src = [0, 10 * SECOND_NS, 20 * SECOND_NS]
    got = hf.asof_pos(src, [0, 5 * SECOND_NS, 10 * SECOND_NS, 19 * SECOND_NS], MINUTE_NS)
    assert got.tolist() == [0, 0, 1, 1]


def test_asof_pos_is_negative_before_the_first_quote(hf) -> None:
    got = hf.asof_pos([10 * SECOND_NS], [5 * SECOND_NS], MINUTE_NS)
    assert got.tolist() == [-1]


def test_asof_pos_drops_quotes_older_than_the_staleness_cap(hf) -> None:
    src = [0, 100 * SECOND_NS]
    got = hf.asof_pos(src, [60 * SECOND_NS, 61 * SECOND_NS], 60 * SECOND_NS)
    # exactly 60 s old is kept, one nanosecond more is missing
    assert got.tolist() == [0, -1]


def test_asof_pos_handles_empty_and_unsorted_input(hf) -> None:
    assert hf.asof_pos([], [0, 1], MINUTE_NS).tolist() == [-1, -1]
    with pytest.raises(ValueError):
        hf.asof_pos([5, 1], [10], MINUTE_NS)


# ---------------------------------------------------------- forward returns
def test_forward_return_pairs_the_quote_at_t_with_the_quote_at_t_plus_h(hf) -> None:
    ts = [0, 10 * SECOND_NS, 20 * SECOND_NS]
    mid = [0.4, 0.5, 0.6]
    fwd = hf.forward_returns(ts, mid, [0], [5 * SECOND_NS, 15 * SECOND_NS], MINUTE_NS)
    # nothing repriced inside 5 s, so the return is a measured zero
    assert float(fwd[5 * SECOND_NS][0]) == pytest.approx(0.0)
    assert float(fwd[15 * SECOND_NS][0]) == pytest.approx(
        hf.logit([0.5])[0] - hf.logit([0.4])[0]
    )


def test_forward_return_is_null_when_the_snapshot_has_no_quote(hf) -> None:
    ts = [0]
    mid = [0.4]
    fwd = hf.forward_returns(ts, mid, [-120 * SECOND_NS], [5 * SECOND_NS], MINUTE_NS)
    assert np.isnan(float(fwd[5 * SECOND_NS][0]))


def test_forward_return_is_null_when_the_later_quote_is_too_stale(hf) -> None:
    ts = [0]
    mid = [0.4]
    # both endpoints are 70 s past the only quote, i.e. beyond the 60 s cap
    fwd = hf.forward_returns(ts, mid, [70 * SECOND_NS], [5 * SECOND_NS], MINUTE_NS)
    assert np.isnan(float(fwd[5 * SECOND_NS][0]))


def test_forward_return_separates_null_from_zero(hf) -> None:
    ts = [0, 30 * SECOND_NS]
    mid = [0.4, 0.45]
    fwd = hf.forward_returns(
        ts, mid, [0, 58 * SECOND_NS], [60 * SECOND_NS], 60 * SECOND_NS
    )
    # snapshot at 0: t+h = 60 s picks up the 30 s quote, 30 s inside the cap
    assert float(fwd[60 * SECOND_NS][0]) == pytest.approx(
        hf.logit([0.45])[0] - hf.logit([0.4])[0]
    )
    # snapshot at 58 s: the base quote (30 s) is fresh, t+60 s is 88 s past it
    assert np.isnan(float(fwd[60 * SECOND_NS][1]))


def test_forward_return_requires_matching_lengths(hf) -> None:
    with pytest.raises(ValueError):
        hf.forward_returns([0], [0.5, 0.6], [0], [SECOND_NS], MINUTE_NS)


# --------------------------------------------------------------- regression
def test_ols_slope_recovers_an_exact_line(hf) -> None:
    x = np.arange(10, dtype=float)
    assert hf.ols_slope(x, 3.0 + 2.0 * x) == pytest.approx(2.0)


def test_ols_slope_drops_nan_rows(hf) -> None:
    x = np.array([0.0, 1.0, 2.0, np.nan, 3.0])
    y = np.array([0.0, 2.0, 4.0, 99.0, np.nan])
    assert hf.ols_slope(x, y) == pytest.approx(2.0)


def test_ols_slope_is_none_when_undefined(hf) -> None:
    assert hf.ols_slope([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None  # no variance in x
    assert hf.ols_slope([1.0, 2.0], [1.0, 2.0]) is None  # too few rows


def test_pair_regression_reports_slope_n_and_r(hf) -> None:
    x = np.array([0.0, 1.0, 2.0, 3.0])
    y = 1.0 + 0.5 * x
    reg = hf.pair_regression(x, y)
    assert reg is not None
    assert reg["slope"] == pytest.approx(0.5)
    assert reg["intercept"] == pytest.approx(1.0)
    assert reg["n"] == 4
    assert reg["r"] == pytest.approx(1.0)


def test_pair_regression_is_none_on_a_degenerate_frame(hf) -> None:
    assert hf.pair_regression([0.0, 0.0, 0.0], [1.0, 2.0, 3.0]) is None


# ------------------------------------------------------------ cluster stats
def test_cluster_stats_averages_pairs_with_a_standard_error(hf) -> None:
    st = hf.cluster_stats([1.0, 2.0, 3.0])
    assert st["pairs"] == 3
    assert st["mean"] == pytest.approx(2.0)
    assert st["sd"] == pytest.approx(1.0)
    assert st["se"] == pytest.approx(1.0 / math.sqrt(3.0))
    assert st["t"] == pytest.approx(2.0 * math.sqrt(3.0))
    assert st["positive"] == 3


def test_cluster_stats_ignores_none_and_flags_a_single_pair(hf) -> None:
    st = hf.cluster_stats([None, 0.4, float("nan")])
    assert st["pairs"] == 1
    assert st["mean"] == pytest.approx(0.4)
    assert st["se"] is None and st["t"] is None


def test_cluster_stats_on_nothing_is_all_none(hf) -> None:
    st = hf.cluster_stats([])
    assert st["pairs"] == 0 and st["mean"] is None and st["t"] is None


# ------------------------------------------------------------------ deciles
def test_decile_table_buckets_by_rank_and_counts_pairs(hf) -> None:
    basis_v = np.arange(20, dtype=float)
    ret = basis_v
    pairs = ["a"] * 10 + ["b"] * 10
    rows = hf.decile_table(basis_v, ret, pairs, n_deciles=10)
    assert len(rows) == 10
    assert all(r["n"] == 2 for r in rows)
    assert rows[0]["mean_ret"] == pytest.approx(0.5)  # rows 0,1
    assert rows[-1]["mean_ret"] == pytest.approx(18.5)  # rows 18,19
    assert rows[0]["pairs"] == 1 and rows[-1]["pairs"] == 1


def test_decile_table_drops_null_returns(hf) -> None:
    basis_v = [0.1, 0.2, np.nan, 0.4, 0.5]
    ret = [0.0, np.nan, 0.3, 0.05, 0.06]
    rows = hf.decile_table(basis_v, ret, ["a", "a", "b", "b", "c"], n_deciles=2)
    assert sum(r["n"] for r in rows) == 3
    # rank 0 and 1 share the first bucket (pairs a and b); rank 2 lands in the second
    assert [r["pairs"] for r in rows] == [2, 1]


def test_decile_table_splits_a_constant_basis(hf) -> None:
    basis_v = [0.5] * 20
    ret = list(range(20))
    rows = hf.decile_table(basis_v, ret, ["a"] * 20, n_deciles=10)
    assert len(rows) == 10
    assert sum(r["n"] for r in rows) == 20
    assert all(r["pairs"] == 1 for r in rows)


def test_decile_table_on_an_empty_frame(hf) -> None:
    assert hf.decile_table([], [], []) == []


def test_decile_table_rejects_mismatched_lengths(hf) -> None:
    with pytest.raises(ValueError):
        hf.decile_table([0.1, 0.2], [0.0], ["a", "b"])


# ------------------------------------------------------------- panel builder
def _pairs() -> list:
    return [
        {
            "ticker": "KXA",
            "cid": "cid-a",
            "yes_token": "tok-a",
            "series": "KXA",
            "method": "title_exact",
            "family": "other",
        }
    ]


def test_build_hour_rows_aligns_each_snapshot_to_the_last_quote(hf) -> None:
    snaps = pd.DataFrame(
        {
            "ticker": ["KXA", "KXA", "KXA"],
            "recv_ts_ns": [0, 20 * SECOND_NS, 200 * SECOND_NS],
            "k_mid": [0.6, 0.7, 0.5],
            "spread": [0.02, 0.02, 0.04],
        }
    )
    series = {"tok-a": (np.array([0, 10 * SECOND_NS], dtype=np.int64), np.array([0.5, 0.55]))}
    records, counters = hf.build_hour_rows(
        _pairs(), snaps, series, MINUTE_NS, (5 * SECOND_NS,)
    )
    # the 200 s snapshot has no quote inside the staleness cap
    assert counters["kalshi_rows"] == 3
    assert counters["aligned_rows"] == 2
    assert counters["snapshots_without_pm"] == 1
    first, second = records
    assert first["pm_mid"] == pytest.approx(0.5)
    assert first["basis"] == pytest.approx(
        hf.logit([0.6])[0] - hf.logit([0.5])[0]
    )
    assert second["pm_mid"] == pytest.approx(0.55)
    assert second["ret_5s"] == pytest.approx(0.0)  # no reprice inside the horizon


def test_build_hour_rows_drops_snapshots_without_a_kalshi_side(hf) -> None:
    snaps = pd.DataFrame(
        {
            "ticker": ["KXA", "KXA"],
            "recv_ts_ns": [0, 10 * SECOND_NS],
            "k_mid": [np.nan, 0.7],
            "spread": [np.nan, 0.02],
        }
    )
    series = {"tok-a": (np.array([0], dtype=np.int64), np.array([0.5]))}
    records, counters = hf.build_hour_rows(
        _pairs(), snaps, series, MINUTE_NS, (5 * SECOND_NS,)
    )
    assert counters["kalshi_usable"] == 1
    assert len(records) == 1
    assert records[0]["kalshi_mid"] == pytest.approx(0.7)


def test_build_hour_rows_without_a_pm_series_reports_no_alignment(hf) -> None:
    snaps = pd.DataFrame(
        {"ticker": ["KXA"], "recv_ts_ns": [0], "k_mid": [0.7], "spread": [0.02]}
    )
    records, counters = hf.build_hour_rows(
        _pairs(), snaps, {}, MINUTE_NS, (5 * SECOND_NS,)
    )
    assert records == []
    assert counters["snapshots_without_pm"] == 1


def test_build_hour_rows_on_an_empty_partition(hf) -> None:
    empty = pd.DataFrame(columns=["ticker", "recv_ts_ns", "k_mid", "spread"])
    records, counters = hf.build_hour_rows(
        _pairs(), empty, {}, MINUTE_NS, (5 * SECOND_NS,)
    )
    assert records == []
    assert counters["kalshi_rows"] == 0


# ------------------------------------------------------------- partition ids
def test_partition_label_formats_date_and_hour(hf) -> None:
    assert hf.partition_label("2026-11-05", "19") == "2026110519"


def test_hour_bounds_are_a_utc_hour(hf) -> None:
    start, end = hf.hour_bounds("2026-11-05", "19")
    assert end - start == 3600 * SECOND_NS
    assert datetime.fromtimestamp(start / 1e9, UTC).hour == 19


def test_partition_sealed_only_after_its_hour_ends(hf) -> None:
    start, end = hf.hour_bounds("2026-11-05", "19")
    assert hf.partition_sealed("2026-11-05", "19", start) is False
    assert hf.partition_sealed("2026-11-05", "19", end - 1) is False
    assert hf.partition_sealed("2026-11-05", "19", end) is True


def test_family_of_maps_the_known_series(hf) -> None:
    assert hf.family_of("KXBTC", "crypto_bound") == "crypto_ladder"
    assert hf.family_of("KX2YR", "treasury_bound") == "treasury"
    assert hf.family_of("KXFEDDECISION", None) == "fomc"
    assert hf.family_of("SENATEMS", "title_exact") == "elections"
    assert hf.family_of("KXLOLAWARD", "title_fuzzy") == "other"
