"""Pure-function tests for polymarketbot.tools.fill_calibration.

The calibration must not be able to point the wrong way: if more queue ahead
made a fill *more* likely, the model would be nonsense and the module has to
say so. These tests feed it synthetic rows with a known monotone relation and
check the pre-state reconstruction, the bucket probabilities, the isotonic fit
and the logistic sign. No tape, no network, no L3 scan: only the module's pure
helpers are called.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pyarrow as pa
import pytest

from polymarketbot.tools import fill_calibration

NS = 1_000_000_000
RATIOS = (0.0, 0.1, 0.25, 0.4, 0.6, 0.8, 0.95, 1.0)


@pytest.fixture(scope="module")
def fill_model():
    return fill_calibration


# ------------------------------------------------------------------ fixtures
L3_COLUMNS = [
    "recv_ts_ns",
    "asset_id",
    "side",
    "price",
    "size_at_level",
    "q_ahead",
    "q_behind",
    "fill_size",
    "cancel_size",
    "survival",
    "attribution",
    "discontinuity",
    "filename",
    "file_row_number",
]


def _row(
    *,
    ts_ns: int,
    price: float = 0.5,
    size: float,
    ahead: float,
    behind: float = 0.0,
    fill: float = 0.0,
    cancel: float = 0.0,
    survival: int = 1,
    attribution: str = "increase",
    discontinuity: bool = False,
    asset: str = "A",
    side: str = "bid",
    row_number: int = 0,
) -> dict:
    return {
        "recv_ts_ns": ts_ns,
        "asset_id": asset,
        "side": side,
        "price": price,
        "size_at_level": size,
        "q_ahead": ahead,
        "q_behind": behind,
        "fill_size": fill,
        "cancel_size": cancel,
        "survival": survival,
        "attribution": attribution,
        "discontinuity": discontinuity,
        "filename": "date=2026-11-04/hour=01/asset=A.parquet",
        "file_row_number": row_number,
    }


def _register(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> None:
    table = pa.Table.from_pylist(rows, schema=pa.schema([
        ("recv_ts_ns", pa.int64()),
        ("asset_id", pa.string()),
        ("side", pa.string()),
        ("price", pa.float64()),
        ("size_at_level", pa.float64()),
        ("q_ahead", pa.float64()),
        ("q_behind", pa.float64()),
        ("fill_size", pa.float64()),
        ("cancel_size", pa.float64()),
        ("survival", pa.int8()),
        ("attribution", pa.string()),
        ("discontinuity", pa.bool_()),
        ("filename", pa.string()),
        ("file_row_number", pa.int64()),
    ]))
    assert table.column_names == L3_COLUMNS
    con.register("l3_rows", table)


def _observations(fill_model, rows: list[dict]):
    con = duckdb.connect()
    try:
        _register(con, rows)
        raw = fill_model.observation_rows(con)
    finally:
        con.close()
    return fill_model.build_observations(raw)


def _synthetic_observations(fill_model, *, min_bucket_fills: int = 20) -> list[dict]:
    """200 print events per ratio bucket, fill rate falling with the ratio."""
    observations: list[dict] = []
    for index, ratio in enumerate(RATIOS):
        n = 200
        fills = 190 - index * 22
        assert fills >= min_bucket_fills
        for j in range(n):
            filled = 1 if j < fills else 0
            # Depth varies so the logistic's size term is identifiable; every
            # row stays inside the 250-1k size bucket.
            pre_size = 300.0 + (j % 12) * 40.0
            observations.append(
                {
                    "recv_ts_ns": 1_000 * NS + j,
                    "asset_id": "A",
                    "filename": "f",
                    "ratio": ratio,
                    "pre_size": pre_size,
                    "fill_size": 30.0,
                    "filled": filled,
                    # Only the buckets where the removal strictly passed the
                    # queue ahead are strict; keep the sensitivity path dense.
                    "filled_strict": filled if ratio < 1.0 else 0,
                    "ratio_bucket": fill_model.ratio_bucket(ratio),
                    "size_bucket": fill_model.size_bucket(pre_size),
                }
            )
    return observations


# ------------------------------------------------- pre-state and label
def test_pre_state_is_the_previous_row_of_the_same_level(fill_model):
    rows = [
        # A level with 10 shares ahead of the shadow order at size 10 -> ratio 1.
        _row(ts_ns=1 * NS, size=10.0, ahead=10.0, row_number=0),
        # Unrelated level on the other side, must not be the pre-state.
        _row(ts_ns=2 * NS, size=40.0, ahead=4.0, side="ask", row_number=1),
        # Print consumes 10 -> the level empties, the replay marks survival 0.
        _row(
            ts_ns=3 * NS,
            size=0.0,
            ahead=0.0,
            fill=10.0,
            survival=0,
            attribution="fill",
            row_number=2,
        ),
    ]
    observations, excluded = _observations(fill_model, rows)

    assert len(observations) == 1
    obs = observations[0]
    assert obs["ratio"] == pytest.approx(1.0)
    assert obs["pre_size"] == pytest.approx(10.0)
    assert obs["filled"] == 1
    assert obs["ratio_bucket"] == "1.0"
    assert excluded == {
        "no_previous_row": 0,
        "previous_row_discontinuity": 0,
        "empty_previous_level": 0,
    }


def test_ratio_is_taken_from_the_row_before_the_print(fill_model):
    rows = [
        _row(ts_ns=1 * NS, size=100.0, ahead=100.0, row_number=0),
        # Someone joins behind: ahead stays 100, size grows to 200 -> ratio 0.5.
        _row(ts_ns=2 * NS, size=200.0, ahead=100.0, behind=100.0, row_number=1),
        _row(
            ts_ns=3 * NS,
            size=150.0,
            ahead=50.0,
            behind=100.0,
            fill=50.0,
            survival=1,
            attribution="fill",
            row_number=2,
        ),
    ]
    observations, _ = _observations(fill_model, rows)

    assert [round(obs["ratio"], 6) for obs in observations] == [pytest.approx(0.5)]
    assert observations[0]["filled"] == 0
    assert observations[0]["ratio_bucket"] == "0.5-0.7"


def test_print_without_a_previous_row_is_excluded_not_invented(fill_model):
    rows = [
        _row(ts_ns=1 * NS, size=5.0, ahead=0.0, fill=5.0, survival=0, attribution="fill")
    ]
    observations, excluded = _observations(fill_model, rows)

    assert observations == []
    assert excluded["no_previous_row"] == 1


def test_a_discontinuity_before_the_print_is_excluded(fill_model):
    rows = [
        _row(ts_ns=1 * NS, size=50.0, ahead=50.0, row_number=0),
        _row(
            ts_ns=2 * NS,
            size=50.0,
            ahead=50.0,
            attribution="anchor",
            discontinuity=True,
            row_number=1,
        ),
        _row(
            ts_ns=3 * NS,
            size=40.0,
            ahead=40.0,
            fill=10.0,
            survival=0,
            attribution="fill",
            row_number=2,
        ),
    ]
    observations, excluded = _observations(fill_model, rows)

    assert observations == []
    assert excluded["previous_row_discontinuity"] == 1


def test_strict_label_requires_the_removal_to_pass_the_queue_ahead(fill_model):
    rows = [
        _row(ts_ns=1 * NS, size=10.0, ahead=10.0, row_number=0),
        # Exactly the queue ahead: the level empties, boundary fill.
        _row(
            ts_ns=2 * NS,
            size=0.0,
            ahead=0.0,
            fill=10.0,
            survival=0,
            attribution="fill",
            row_number=1,
        ),
    ]
    observations, _ = _observations(fill_model, rows)
    assert observations[0]["filled"] == 1
    assert observations[0]["filled_strict"] == 0


# --------------------------------------------------------------- bucketing
def test_bucket_edges(fill_model):
    assert fill_model.ratio_bucket(0.0) == "0-0.05"
    assert fill_model.ratio_bucket(0.0499) == "0-0.05"
    assert fill_model.ratio_bucket(0.05) == "0.05-0.15"
    assert fill_model.ratio_bucket(0.8499) == "0.7-0.85"
    assert fill_model.ratio_bucket(0.85) == "0.85-1"
    assert fill_model.ratio_bucket(1.0) == "1.0"

    assert fill_model.size_bucket(0.0) == "<50"
    assert fill_model.size_bucket(49.9) == "<50"
    assert fill_model.size_bucket(50.0) == "50-250"
    assert fill_model.size_bucket(250.0) == "250-1k"
    assert fill_model.size_bucket(5000.0) == ">=5k"
    assert fill_model.size_bucket(1e9) == ">=5k"


# ------------------------------------------------------- empirical buckets
def test_bucket_probabilities_fall_with_the_queue_ahead(fill_model):
    observations = _synthetic_observations(fill_model)
    by_ratio = fill_model.summarise(
        observations, "ratio_bucket", fill_model.RATIO_LABELS, min_obs=100
    )

    rates = [row["p_fill"] for row in by_ratio]
    assert all(rate is not None for rate in rates)
    assert rates == sorted(rates, reverse=True)
    assert rates[0] > 0.9 and rates[-1] < 0.2
    assert all(row["n"] == 200 for row in by_ratio)
    assert by_ratio[0]["fills"] == 190


def test_sparse_bucket_is_reported_na_with_its_count(fill_model):
    observations = _synthetic_observations(fill_model)
    observations = [obs for obs in observations if obs["ratio_bucket"] != "0.7-0.85"]
    observations += [
        dict(obs, ratio=0.75, ratio_bucket="0.7-0.85") for obs in observations[:3]
    ]

    by_ratio = fill_model.summarise(
        observations, "ratio_bucket", fill_model.RATIO_LABELS, min_obs=100
    )
    sparse = next(row for row in by_ratio if row["bucket"] == "0.7-0.85")
    assert sparse["n"] == 3
    assert sparse["p_fill"] is None
    assert sparse["ci_low"] is None and sparse["ci_high"] is None
    assert sparse["status"] == "n/a"

    dense = next(row for row in by_ratio if row["bucket"] == "0-0.05")
    assert dense["status"] == "ok"
    assert 0.0 < dense["ci_low"] < dense["p_fill"] < dense["ci_high"] < 1.0


def test_cross_table_marks_thin_cells_na_and_prices_dense_ones(fill_model):
    observations = _synthetic_observations(fill_model)
    # Move three low-ratio events into a rare size bucket.
    for obs in observations[:3]:
        obs["size_bucket"] = ">=5k"
    table = fill_model.cross_table(
        observations, fill_model.RATIO_LABELS, fill_model.SIZE_LABELS, min_obs=100
    )
    cells = {(row["ratio_bucket"], row["size_bucket"]): row for row in table}
    assert len(table) == len(fill_model.RATIO_LABELS) * len(fill_model.SIZE_LABELS)
    assert cells[("0-0.05", ">=5k")]["status"] == "n/a"
    assert cells[("0-0.05", ">=5k")]["n"] == 3
    assert cells[("0-0.05", "250-1k")]["status"] == "ok"
    assert cells[("0-0.05", "250-1k")]["n"] == 197


# --------------------------------------------------------------- isotonic
def test_isotonic_is_monotone_and_pools_the_violating_step(fill_model):
    labels = ["a", "b", "c", "d"]
    ns = [100, 100, 100, 100]
    fills = [80, 40, 60, 5]  # a rise at c violates non-increasing
    iso = fill_model.isotonic_decreasing(labels, ns, fills)

    fitted = [row["fitted_p_fill"] for row in iso["bins"]]
    assert fitted == sorted(fitted, reverse=True)
    assert iso["raw_increasing_steps"] == 1
    # b and c get pooled onto one weighted mean.
    assert fitted[1] == pytest.approx(0.5)
    assert fitted[2] == pytest.approx(0.5)
    assert len(iso["blocks"]) == 3
    block = next(b for b in iso["blocks"] if b["from_bucket"] == "b")
    assert (block["to_bucket"], block["n"], block["fills"]) == ("c", 200, 100)


def test_isotonic_skips_a_bucket_below_min_observations(fill_model):
    iso = fill_model.isotonic_decreasing(
        ["a", "b", "c"], [100, 5, 100], [80, 5, 10], min_n=20
    )
    bins = {row["bucket"]: row for row in iso["bins"]}
    assert bins["b"]["status"] == "n/a"
    assert bins["b"]["fitted_p_fill"] is None
    assert bins["b"]["raw_p_fill"] == pytest.approx(1.0)
    assert [row["from_bucket"] for row in iso["blocks"]] == ["a", "c"]
    assert bins["a"]["fitted_p_fill"] == pytest.approx(0.8)
    assert bins["c"]["fitted_p_fill"] == pytest.approx(0.1)


def test_isotonic_keeps_a_monotone_sequence_untouched(fill_model):
    iso = fill_model.isotonic_decreasing(
        ["a", "b", "c"], [10, 10, 10], [8, 5, 1]
    )
    assert [row["fitted_p_fill"] for row in iso["bins"]] == pytest.approx([0.8, 0.5, 0.1])
    assert iso["raw_increasing_steps"] == 0
    assert len(iso["blocks"]) == 3


def test_isotonic_on_the_synthetic_table_matches_the_raw_rates(fill_model):
    observations = _synthetic_observations(fill_model)
    by_ratio = fill_model.summarise(
        observations, "ratio_bucket", fill_model.RATIO_LABELS, min_obs=100
    )
    iso = fill_model.isotonic_decreasing(
        [row["bucket"] for row in by_ratio],
        [row["n"] for row in by_ratio],
        [row["fills"] for row in by_ratio],
    )
    fitted = [row["fitted_p_fill"] for row in iso["bins"]]
    raw = [row["raw_p_fill"] for row in iso["bins"]]

    assert fitted == pytest.approx(raw, abs=1e-12)
    assert fitted == sorted(fitted, reverse=True)


# --------------------------------------------------------------- logistic
def test_logistic_recovers_the_negative_ratio_slope(fill_model):
    rng = np.random.default_rng(7)
    n = 4000
    ratio = rng.uniform(0.0, 1.0, n)
    log_size = rng.uniform(0.0, 8.0, n)
    truth = np.array([1.0, -2.0, -0.3])
    X = np.column_stack([np.ones(n), ratio, log_size])
    prob = 1.0 / (1.0 + np.exp(-(X @ truth)))
    y = (rng.uniform(size=n) < prob).astype(float)

    fit = fill_model.fit_logistic(X, y)

    assert fit["ok"] and fit["converged"]
    assert fit["coef"][1] < 0.0
    assert abs(fit["coef"][1] - truth[1]) < 0.4
    assert abs(fit["coef"][2] - truth[2]) < 0.15
    assert all(se > 0 for se in fit["se"])
    assert fit["z"][1] < -5.0
    assert fit["n"] == n


def test_logistic_refuses_a_single_class(fill_model):
    X = np.array([[1.0, 0.2], [1.0, 0.9]])
    fit = fill_model.fit_logistic(X, np.zeros(2))
    assert fit["ok"] is False
    assert "degenerate" in fit["reason"]


def test_synthetic_calibration_points_the_expected_way(fill_model):
    observations = _synthetic_observations(fill_model)
    result = fill_model.calibrate(observations, min_obs=100)

    assert result["model_ok"] is True
    assert result["observations"] == len(observations)
    assert result["logistic"]["coef"][1] < 0.0
    assert result["logistic"]["monotone_direction_ok"] is True
    fitted = [row["fitted_p_fill"] for row in result["isotonic"]["bins"]]
    assert fitted == sorted(fitted, reverse=True)
    # The sensitivity label is 0 at ratio 1.0 by construction and still falls.
    strict = [row["p_fill"] for row in result["sensitivity"]["by_ratio"]]
    assert strict[0] > strict[-2]


def test_calibrate_refuses_a_tiny_sample(fill_model):
    observations = _synthetic_observations(fill_model)[:20]
    result = fill_model.calibrate(observations, min_obs=100)

    assert result["model_ok"] is False
    assert "min_obs" in result["model_reason"]
    assert result["base_rate"] is not None
    assert all(row["status"] == "n/a" for row in result["by_ratio"])
    # The raw rate is still recorded for the record; nothing is fitted.
    assert result["isotonic"]["bins"][0]["raw_p_fill"] is not None
    assert result["isotonic"]["bins"][0]["fitted_p_fill"] is None
    assert result["isotonic"]["bins"][0]["status"] == "n/a"
    assert result["isotonic"]["blocks"] == []


def test_calibrate_refuses_a_wrong_direction(fill_model):
    # A sample where fills *rise* with the queue ahead is the one relation the
    # model must never ship: the verdict has to reject it, not fit it.
    observations = [
        {
            "recv_ts_ns": i,
            "asset_id": "A",
            "filename": "f",
            "ratio": ratio,
            "pre_size": 100.0,
            "fill_size": 1.0,
            "filled": 1 if i >= 200 else 0,
            "filled_strict": 1 if i >= 200 else 0,
            "ratio_bucket": fill_model.ratio_bucket(ratio),
            "size_bucket": fill_model.size_bucket(100.0),
        }
        for i, ratio in enumerate(np.linspace(0.0, 1.0, 400))
    ]
    result = fill_model.calibrate(observations, min_obs=30)

    assert result["logistic"]["coef"][1] > 0.0
    assert result["logistic"]["monotone_direction_ok"] is False
    assert result["model_ok"] is False
    assert "not negative" in result["model_reason"]


def test_wilson_interval_brackets_the_point_estimate(fill_model):
    low, high = fill_model.wilson(30, 100)
    assert 0.0 < low < 0.30 < high < 1.0
    assert high - low < 0.25
    assert fill_model.wilson(0, 0) == (None, None)
    low, high = fill_model.wilson(0, 50)
    assert abs(low) < 1e-12 and 0.0 < high < 0.1


def test_report_renders_na_where_the_sample_is_thin(fill_model):
    observations = _synthetic_observations(fill_model)[:20]
    result = fill_model.calibrate(observations, min_obs=100)
    meta = {
        "glob": "x",
        "files": 1,
        "asset_hours": 1,
        "assets": 1,
        "window_start": "a",
        "window_end": "b",
        "level_rows": 20,
        "print_events": 20,
        "observations": result["observations"],
        "fills": result["fills"],
        "base_rate": result["base_rate"],
        "excluded": {"no_previous_row": 0},
        "min_observations": 100,
        "model_ok": result["model_ok"],
        "model_reason": result["model_reason"],
    }
    text = fill_model.render(
        table=result["table"],
        by_ratio=result["by_ratio"],
        by_size=result["by_size"],
        iso=result["isotonic"],
        logistic=result["logistic"],
        sensitivity=result["sensitivity"],
        meta=meta,
    )
    assert "n/a" in text
    assert "Verdict: n/a" in text
    assert "0.5-0.7" in text
