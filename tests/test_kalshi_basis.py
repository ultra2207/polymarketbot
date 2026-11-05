"""Pure-function tests for scripts/report_kalshi_basis.py.

The basis and lead-lag helpers decide what the report claims about two venues
that quote the same event, so each one gets a synthetic case with a known
answer: bucketing, staleness, alignment on the bucket grid, lag direction, the
n/a floors, and the direction check. No tape, no network - the script is loaded
from its path and only its pure helpers are called.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

SECOND_NS = 1_000_000_000
MINUTE_NS = 60 * SECOND_NS


@pytest.fixture(scope="module")
def kbasis():
    path = Path(__file__).resolve().parent.parent / "scripts" / "report_kalshi_basis.py"
    spec = importlib.util.spec_from_file_location("report_kalshi_basis_pure", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ bucketing
def test_bucket_last_keeps_the_last_quote_in_each_bucket(kbasis) -> None:
    ts = [0, 30 * SECOND_NS, 59 * SECOND_NS, 60 * SECOND_NS, 61 * SECOND_NS]
    mid = [0.10, 0.20, 0.30, 0.40, 0.50]
    buckets, values = kbasis.bucket_last(ts, mid, MINUTE_NS)
    assert buckets.tolist() == [0, 1]
    assert values.tolist() == [0.30, 0.50]


def test_bucket_last_accepts_unsorted_input_and_drops_nan(kbasis) -> None:
    ts = [90 * SECOND_NS, 10 * SECOND_NS, 70 * SECOND_NS, 20 * SECOND_NS]
    mid = [0.9, 0.1, float("nan"), 0.2]
    buckets, values = kbasis.bucket_last(ts, mid, MINUTE_NS)
    assert buckets.tolist() == [0, 1]
    # 90s is the last quote of bucket 1; 70s is NaN, 20s is the last finite of bucket 0
    assert values.tolist() == [0.2, 0.9]


def test_bucket_last_on_no_quotes_is_empty(kbasis) -> None:
    buckets, values = kbasis.bucket_last([], [], MINUTE_NS)
    assert buckets.size == 0 and values.size == 0


def test_bucket_last_rejects_mismatched_lengths(kbasis) -> None:
    with pytest.raises(ValueError):
        kbasis.bucket_last([0, 1], [0.5], MINUTE_NS)


# --------------------------------------------------------------- forward fill
def test_fill_grid_carries_a_quote_until_the_staleness_bound(kbasis) -> None:
    grid = kbasis.fill_grid([10], [0.40], 10, 15, 2)
    assert grid[0] == pytest.approx(0.40)
    assert grid[2] == pytest.approx(0.40)  # bucket 12: two buckets old
    assert np.isnan(grid[3]) and np.isnan(grid[5])  # past the bound: unknown


def test_fill_grid_is_nan_before_the_first_quote(kbasis) -> None:
    grid = kbasis.fill_grid([12], [0.55], 10, 13, 5)
    assert np.isnan(grid[0]) and np.isnan(grid[1])
    assert grid[2] == pytest.approx(0.55) and grid[3] == pytest.approx(0.55)


def test_fill_grid_ignores_quotes_outside_the_grid(kbasis) -> None:
    grid = kbasis.fill_grid([5, 20], [0.1, 0.9], 10, 12, 5)
    assert np.isnan(grid).all()


def test_fill_grid_uses_the_latest_quote_per_cell(kbasis) -> None:
    grid = kbasis.fill_grid([10, 10], [0.2, 0.3], 10, 11, 0)
    assert grid[0] == pytest.approx(0.3)
    assert np.isnan(grid[1])  # max_stale_buckets=0: next bucket unknown


# ------------------------------------------------------------ change alignment
def test_aligned_changes_drops_buckets_where_a_side_is_stale(kbasis) -> None:
    poly = [0.20, 0.30, float("nan"), 0.40]
    kalshi = [0.10, 0.10, 0.10, 0.20]
    ids, dp, dk = kbasis.aligned_changes(poly, kalshi, 10)
    # bucket 11 is the only pair where both venues are defined on both sides
    assert ids.tolist() == [11]
    assert dp.tolist() == pytest.approx([0.10])
    assert dk.tolist() == pytest.approx([0.00])


def test_aligned_changes_returns_empty_for_a_single_bucket(kbasis) -> None:
    ids, dp, dk = kbasis.aligned_changes([0.2], [0.1], 10)
    assert ids.size == 0 and dp.size == 0 and dk.size == 0


def test_aligned_changes_reports_the_bucket_the_change_ends_in(kbasis) -> None:
    ids, dp, _ = kbasis.aligned_changes([0.1, 0.2, 0.4], [0.1, 0.1, 0.1], 7)
    assert ids.tolist() == [8, 9]
    assert dp.tolist() == pytest.approx([0.1, 0.2])


# ---------------------------------------------------------------- lag pairing
def test_pair_at_lag_pairs_with_the_shifted_bucket(kbasis) -> None:
    poly_ids, poly_changes = [10, 11, 12], [1.0, 2.0, 3.0]
    kal_ids, kal_changes = [11, 12, 13], [10.0, 20.0, 30.0]

    x, y = kbasis.pair_at_lag(poly_ids, poly_changes, kal_ids, kal_changes, 1)
    assert x.tolist() == [1.0, 2.0, 3.0]  # 10->11, 11->12, 12->13 all present
    assert y.tolist() == [10.0, 20.0, 30.0]

    x, y = kbasis.pair_at_lag(poly_ids, poly_changes, kal_ids, kal_changes, 0)
    assert x.tolist() == [2.0, 3.0]
    assert y.tolist() == [10.0, 20.0]

    x, y = kbasis.pair_at_lag(poly_ids, poly_changes, kal_ids, kal_changes, -1)
    assert x.tolist() == [3.0]
    assert y.tolist() == [10.0]


def test_pair_at_lag_with_no_overlap_is_empty(kbasis) -> None:
    x, y = kbasis.pair_at_lag([10], [1.0], [10], [1.0], 5)
    assert x.size == 0 and y.size == 0


# ------------------------------------------------------------------- pearson
def test_pearson_matches_a_known_relationship(kbasis) -> None:
    x = np.array([1.0, 2.0, 3.0, 4.0])
    assert kbasis.pearson(x, 2.0 * x + 3.0) == pytest.approx(1.0)
    assert kbasis.pearson(x, -x) == pytest.approx(-1.0)


def test_pearson_needs_three_pairs(kbasis) -> None:
    assert kbasis.pearson([1.0, 2.0], [1.0, 2.0]) is None


def test_pearson_of_a_constant_series_is_undefined(kbasis) -> None:
    assert kbasis.pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None


# -------------------------------------------------------------------- z score
def test_zscore_has_zero_mean_and_unit_variance(kbasis) -> None:
    z = kbasis.zscore([1.0, 2.0, 3.0])
    assert z is not None
    assert float(z.mean()) == pytest.approx(0.0)
    assert float(z.std()) == pytest.approx(1.0)


def test_zscore_of_a_constant_series_is_none(kbasis) -> None:
    assert kbasis.zscore([0.5, 0.5, 0.5, 0.5]) is None


# ------------------------------------------------------------------ lag table
def _leading_series(kbasis, lead: int, n: int = 40, seed: int = 7):
    """Polymarket random walk and a Kalshi series lagging it by ``lead`` buckets."""
    rng = np.random.default_rng(seed)
    poly = np.cumsum(rng.normal(0.0, 0.01, n)) + 0.5
    kalshi = poly.copy()
    if lead > 0:
        kalshi[:lead] = poly[0]
        kalshi[lead:] = poly[:-lead]
    ids = np.arange(1, n, dtype=np.int64)
    dp = np.diff(poly)
    dk = np.diff(kalshi)
    return ids, dp, ids, dk


def test_lag_table_peaks_at_the_true_lead(kbasis) -> None:
    ids, dp, kids, dk = _leading_series(kbasis, lead=2)
    rows = kbasis.lag_table(ids, dp, kids, dk, max_lag=5, min_pairs=5)
    best = kbasis.peak_lag(rows)
    assert best is not None
    assert best["lag"] == 2  # Polymarket moved first, Kalshi followed two buckets later
    assert best["corr"] == pytest.approx(1.0)


def test_lag_table_detects_a_kalshi_lead_as_a_negative_lag(kbasis) -> None:
    ids, dp, kids, dk = _leading_series(kbasis, lead=2)
    rows = kbasis.lag_table(kids, dk, ids, dp, max_lag=5, min_pairs=5)
    best = kbasis.peak_lag(rows)
    assert best is not None and best["lag"] == -2


def test_lag_table_reports_counts_for_every_lag(kbasis) -> None:
    ids, dp, kids, dk = _leading_series(kbasis, lead=1, n=20)
    rows = kbasis.lag_table(ids, dp, kids, dk, max_lag=3, min_pairs=2)
    assert [r["lag"] for r in rows] == [-3, -2, -1, 0, 1, 2, 3]
    counts = [r["n"] for r in rows]
    assert counts[3] == max(counts)  # lag 0 keeps every pair
    assert counts[0] == counts[-1] and counts[0] < counts[3]
    assert all(r["corr"] is not None for r in rows if r["n"] >= 2)


def test_lag_table_marks_thin_lags_n_a(kbasis) -> None:
    ids, dp, kids, dk = _leading_series(kbasis, lead=0, n=4)
    rows = kbasis.lag_table(ids, dp, kids, dk, max_lag=2, min_pairs=10)
    assert all(r["corr"] is None for r in rows)
    assert all(r["n"] <= 3 for r in rows)


# ------------------------------------------------------------- pooled table
def test_pooled_lag_table_pools_markets_on_the_same_clock(kbasis) -> None:
    s1 = _leading_series(kbasis, lead=1, n=30, seed=1)
    s2 = _leading_series(kbasis, lead=1, n=30, seed=2)
    series = [
        {"poly_ids": s1[0], "poly_changes": s1[1], "kalshi_ids": s1[2], "kalshi_changes": s1[3]},
        {"poly_ids": s2[0], "poly_changes": s2[1], "kalshi_ids": s2[2], "kalshi_changes": s2[3]},
    ]
    rows = kbasis.pooled_lag_table(series, max_lag=2, min_pairs=5, min_panel=10)
    at_one = next(r for r in rows if r["lag"] == 1)
    assert at_one["n_markets"] == 2
    assert at_one["n_pairs"] == 28 * 2  # 29 changes per market, one lost to the lag
    assert at_one["corr"] == pytest.approx(1.0)


def test_pooled_lag_table_excludes_thin_and_constant_markets(kbasis) -> None:
    good = _leading_series(kbasis, lead=1, n=30, seed=3)
    thin = _leading_series(kbasis, lead=1, n=4, seed=4)
    flat = ([1, 2, 3, 4, 5], [0.0] * 5, [1, 2, 3, 4, 5], [0.0] * 5)
    series = [
        {"poly_ids": good[0], "poly_changes": good[1], "kalshi_ids": good[2], "kalshi_changes": good[3]},
        {"poly_ids": thin[0], "poly_changes": thin[1], "kalshi_ids": thin[2], "kalshi_changes": thin[3]},
        {"poly_ids": flat[0], "poly_changes": flat[1], "kalshi_ids": flat[2], "kalshi_changes": flat[3]},
    ]
    rows = kbasis.pooled_lag_table(series, max_lag=1, min_pairs=5, min_panel=10)
    at_zero = next(r for r in rows if r["lag"] == 0)
    assert at_zero["n_markets"] == 1


def test_pooled_lag_table_is_n_a_below_the_panel_floor(kbasis) -> None:
    s = _leading_series(kbasis, lead=1, n=8, seed=5)
    series = [{"poly_ids": s[0], "poly_changes": s[1], "kalshi_ids": s[2], "kalshi_changes": s[3]}]
    rows = kbasis.pooled_lag_table(series, max_lag=1, min_pairs=2, min_panel=100)
    assert all(r["corr"] is None for r in rows)


def test_peak_lag_ignores_n_a_rows(kbasis) -> None:
    rows = [{"lag": -1, "n": 5, "corr": None}, {"lag": 0, "n": 40, "corr": 0.2},
            {"lag": 1, "n": 9, "corr": -0.4}]
    best = kbasis.peak_lag(rows)
    assert best is not None and best["lag"] == 1
    assert kbasis.peak_lag([{"lag": 0, "n": 1, "corr": None}]) is None


# --------------------------------------------------------------------- basis
def test_basis_summary_is_poly_minus_kalshi(kbasis) -> None:
    summary = kbasis.basis_summary([0.50, 0.56, 0.44], [0.48, 0.55, 0.45], min_buckets=3)
    assert summary["n"] == 3
    assert summary["basis_mean"] == pytest.approx((0.02 + 0.01 - 0.01) / 3, abs=1e-12)
    assert summary["basis_med_abs"] == pytest.approx(0.01, abs=1e-12)
    assert summary["poly_mid_mean"] == pytest.approx(0.50)
    assert summary["kalshi_mid_mean"] == pytest.approx(0.493333, abs=1e-6)


def test_basis_summary_below_the_floor_is_none_not_zero(kbasis) -> None:
    summary = kbasis.basis_summary([0.50], [0.50], min_buckets=5)
    assert summary["n"] == 1
    assert summary["basis_mean"] is None
    assert summary["basis_med_abs"] is None


def test_basis_summary_drops_stale_cells(kbasis) -> None:
    summary = kbasis.basis_summary(
        [0.50, float("nan"), 0.60], [0.48, 0.40, 0.58], min_buckets=2
    )
    assert summary["n"] == 2
    assert summary["basis_mean"] == pytest.approx(0.02, abs=1e-12)


def test_basis_summary_rejects_mismatched_lengths(kbasis) -> None:
    with pytest.raises(ValueError):
        kbasis.basis_summary([0.1, 0.2], [0.1], min_buckets=1)


# --------------------------------------------------------------- orientation
def test_orientation_ok_accepts_the_token_closest_to_kalshi(kbasis) -> None:
    assert kbasis.orientation_ok(0.14, 0.86, 0.135) is True
    assert kbasis.orientation_ok(0.86, 0.14, 0.135) is False


def test_orientation_without_a_second_token_is_unknown(kbasis) -> None:
    assert kbasis.orientation_ok(0.14, None, 0.135) is None
    assert kbasis.orientation_ok(0.14, 0.86, None) is None


# ----------------------------------------------------------------- formatting
def test_fmt_opt_prints_n_a_instead_of_a_number(kbasis) -> None:
    assert kbasis.fmt_opt(None).strip() == "n/a"
    assert kbasis.fmt_opt(float("nan")).strip() == "n/a"
    assert kbasis.fmt_opt(0.1234, 8, 3).strip() == "0.123"


def test_format_lag_rows_shows_counts_and_n_a(kbasis) -> None:
    rows = [
        {"lag": -1, "n_pairs": 0, "n_markets": 0, "corr": None},
        {"lag": 0, "n_pairs": 1234, "n_markets": 7, "corr": 0.42},
    ]
    text = "\n".join(kbasis.format_lag_rows(rows))
    assert "n/a" in text
    assert "1,234" in text
    assert "0.4200" in text
