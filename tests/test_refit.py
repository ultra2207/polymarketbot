from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from polymarketbot.tools import refit


def _partition_tree(root: Path, labels: list[str]) -> None:
    for label in labels:
        date, hour = label.split("T")
        for table in ("clob_events", "clob_trades"):
            folder = root / table / f"date={date}" / f"hour={hour}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "part.parquet").write_bytes(b"")


def _write_partial(
    root: Path,
    artifact: str,
    label: str,
    *,
    sample: object,
    value: float,
    start: str,
    end: str,
    window: bool = True,
    partial: bool = False,
    extra: dict | None = None,
) -> Path:
    path = root / artifact / f"{label}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": "2026-11-01T00:00:00+00:00",
        "sample_size": sample,
        "signal": value,
        "partial": partial,
    }
    if window:
        payload["window"] = {"start": start, "end": end}
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_complete_partitions_needs_both_tables(tmp_path: Path) -> None:
    tape = tmp_path / "tape"
    for label in ("2026-11-04T00", "2026-11-04T01"):
        date, hour = label.split("T")
        for table in ("clob_events", "clob_trades"):
            (tape / table / f"date={date}" / f"hour={hour}").mkdir(parents=True)
            (tape / table / f"date={date}" / f"hour={hour}" / "p.parquet").write_bytes(b"")
    # Events only: not a complete measurement.
    (tape / "clob_events" / "date=2026-11-05" / "hour=02").mkdir(parents=True)
    (tape / "clob_events" / "date=2026-11-05" / "hour=02" / "p.parquet").write_bytes(b"")
    # Trades only: likewise.
    (tape / "clob_trades" / "date=2026-11-05" / "hour=03").mkdir(parents=True)
    (tape / "clob_trades" / "date=2026-11-05" / "hour=03" / "p.parquet").write_bytes(b"")

    partitions = refit.complete_partitions(tape)

    assert [item.label for item in partitions] == ["2026-11-04T00", "2026-11-04T01"]


def test_combine_weights_by_sample_and_unions_window(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    _write_partial(
        partials,
        "queue_fit",
        "2026-11-04T00",
        sample={"prints": 400},
        value=0.2,
        start="2026-11-04T00:00:00+00:00",
        end="2026-11-04T01:00:00+00:00",
    )
    _write_partial(
        partials,
        "queue_fit",
        "2026-11-04T01",
        sample={"prints": 600},
        value=0.6,
        start="2026-11-04T01:00:00+00:00",
        end="2026-11-04T02:00:00+00:00",
    )

    combined = refit.combine_artifact(
        "queue_fit", partials_root=partials, derived_dir=tmp_path / "derived"
    )

    assert combined is not None
    assert combined["signal"] == pytest.approx(0.44)
    assert combined["sample_size"]["prints"] == 1000
    assert combined["window"]["start"] == "2026-11-04T00:00:00+00:00"
    assert combined["window"]["end"] == "2026-11-04T02:00:00+00:00"
    assert combined["partitions"] == ["2026-11-04T00", "2026-11-04T01"]
    assert combined["partial"] is False
    assert combined["combination"]["minimum_sample"] == 1000.0
    assert combined["combination"]["sample"] == 1000.0
    assert combined["combination"]["rejected"] == []
    on_disk = json.loads((tmp_path / "derived" / "queue_fit.json").read_text())
    assert on_disk == combined
    assert not list((tmp_path / "derived").glob(".*.tmp"))


def test_combine_rejects_partial_without_window_or_sample(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    _write_partial(
        partials,
        "queue_fit",
        "2026-11-04T00",
        sample={"prints": 2000},
        value=0.1,
        start="2026-11-04T00:00:00+00:00",
        end="2026-11-04T01:00:00+00:00",
        window=False,
    )
    _write_partial(
        partials,
        "queue_fit",
        "2026-11-04T01",
        sample={"prints": 0},
        value=0.2,
        start="2026-11-04T01:00:00+00:00",
        end="2026-11-04T02:00:00+00:00",
    )
    _write_partial(
        partials,
        "queue_fit",
        "2026-11-04T02",
        sample={"prints": 2000},
        value=0.3,
        start="2026-11-04T02:00:00+00:00",
        end="2026-11-04T03:00:00+00:00",
    )

    combined = refit.combine_artifact(
        "queue_fit", partials_root=partials, derived_dir=tmp_path / "derived"
    )

    assert combined is not None
    assert combined["partitions"] == ["2026-11-04T02"]
    assert combined["signal"] == pytest.approx(0.3)
    rejected = {item["partition"]: item["reason"] for item in combined["combination"]["rejected"]}
    assert set(rejected) == {"2026-11-04T00", "2026-11-04T01"}
    assert "window" in rejected["2026-11-04T00"]
    assert "sample_size" in rejected["2026-11-04T01"]


def test_combine_refuses_a_single_thin_partition(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    derived = tmp_path / "derived"
    _write_partial(
        partials,
        "queue_fit",
        "2026-11-04T00",
        sample={"prints": 999},
        value=0.2,
        start="2026-11-04T00:00:00+00:00",
        end="2026-11-04T01:00:00+00:00",
    )

    assert (
        refit.combine_artifact(
            "queue_fit", partials_root=partials, derived_dir=derived
        )
        is None
    )
    assert not (derived / "queue_fit.json").exists()

    _write_partial(
        partials,
        "queue_fit",
        "2026-11-04T01",
        sample={"prints": 1},
        value=0.3,
        start="2026-11-04T01:00:00+00:00",
        end="2026-11-04T02:00:00+00:00",
    )
    # Two thin partitions whose *sum* clears the floor are a measurement.
    combined = refit.combine_artifact(
        "queue_fit", partials_root=partials, derived_dir=derived
    )
    assert combined is not None
    assert combined["combination"]["sample"] == 1000.0


def test_combine_derives_corner_from_the_pooled_gain(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    derived = tmp_path / "derived"
    _write_partial(
        partials,
        "deskew_calibration",
        "2026-11-04T00",
        sample={"round_trips": 100},
        value=0.0,
        start="2026-11-04T00:00:00+00:00",
        end="2026-11-04T01:00:00+00:00",
        extra={
            "skew_aggressiveness": 0.0,
            "corner": True,
            "definition": {"corner": "documentation text"},
            "sufficient_statistics": {
                "schema": 2,
                "skew": {
                    "placements": 10.0,
                    "grid": [
                        {"phi": 0.0, "net_total": 0.10, "fills": 5},
                        {"phi": 0.25, "net_total": 0.15, "fills": 4},
                    ],
                },
            },
        },
    )
    _write_partial(
        partials,
        "deskew_calibration",
        "2026-11-04T01",
        sample={"round_trips": 100},
        value=0.0,
        start="2026-11-04T01:00:00+00:00",
        end="2026-11-04T02:00:00+00:00",
        extra={
            "skew_aggressiveness": 0.5,
            "corner": False,
            "definition": {"corner": "documentation text"},
            "sufficient_statistics": {
                "schema": 2,
                "skew": {
                    "placements": 10.0,
                    "grid": [
                        {"phi": 0.0, "net_total": 0.06, "fills": 3},
                        {"phi": 0.25, "net_total": 0.20, "fills": 3},
                    ],
                },
            },
        },
    )

    combined = refit.combine_artifact(
        "deskew_calibration", partials_root=partials, derived_dir=derived
    )

    assert combined is not None
    # Summed per-placement capture: 0.35/20 at 0.25 beats 0.16/20 at zero.
    assert combined["skew_aggressiveness"] == pytest.approx(0.25)
    assert combined["corner"] is False
    assert combined["definition"]["corner"] == "documentation text"

    # Two measured zero corners stay a corner.
    _write_partial(
        partials,
        "deskew_calibration",
        "2026-11-04T01",
        sample={"round_trips": 100},
        value=0.0,
        start="2026-11-04T01:00:00+00:00",
        end="2026-11-04T02:00:00+00:00",
        extra={
            "skew_aggressiveness": 0.0,
            "corner": True,
            "sufficient_statistics": {
                "schema": 2,
                "skew": {
                    "placements": 10.0,
                    "grid": [
                        {"phi": 0.0, "net_total": 0.20, "fills": 5},
                        {"phi": 0.25, "net_total": 0.01, "fills": 1},
                    ],
                },
            },
        },
    )
    combined = refit.combine_artifact(
        "deskew_calibration", partials_root=partials, derived_dir=derived
    )
    assert combined is not None
    assert combined["skew_aggressiveness"] == pytest.approx(0.0)
    assert combined["corner"] is True


def test_combine_derives_pooled_policy_and_agreement(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    derived = tmp_path / "derived"
    common = {
        "sample_size": {"windows": 100},
        "value": 0.0,
        "start": "2026-11-04T00:00:00+00:00",
        "end": "2026-11-04T01:00:00+00:00",
    }
    _write_partial(
        partials,
        "risk_calibration",
        "2026-11-04T00",
        sample={"windows": 100},
        value=0.0,
        start=common["start"],
        end=common["end"],
        extra={
            "fields": {
                "pull_multiplier": {
                    "corner": "never_pull",
                    "at_corner": True,
                    "policy": "never pull; widen by the measured offset",
                    "monotone_in_pull": True,
                    "grid": [0.1, 1.0],
                },
                "widen_offset_multiplier": {
                    "at_boundary": False,
                    "grid": [0.0, 0.5],
                },
            },
            "source": {"pull_grid": [0.1, 1.0], "widen_grid": [0.0, 0.5]},
            "ofi": {"consecutive_fills_threshold": 2.0},
        },
    )
    _write_partial(
        partials,
        "risk_calibration",
        "2026-11-04T01",
        sample={"windows": 100},
        value=0.0,
        start=common["start"],
        end=common["end"],
        extra={
            "fields": {
                "pull_multiplier": {
                    "corner": None,
                    "at_corner": False,
                    "policy": "interior optimum: pull only on the most extreme flow",
                    "monotone_in_pull": False,
                    "grid": [0.1, 2.0],
                },
                "widen_offset_multiplier": {
                    "at_boundary": True,
                    "grid": [0.0, 1.0],
                },
            },
            "source": {"pull_grid": [0.1, 2.0], "widen_grid": [0.0, 1.0]},
            "ofi": {"consecutive_fills_threshold": 2.0},
        },
    )

    combined = refit.combine_artifact(
        "risk_calibration", partials_root=partials, derived_dir=derived
    )

    assert combined is not None
    pull = combined["fields"]["pull_multiplier"]
    assert pull["corner"] is None
    assert pull["at_corner"] is False
    assert pull["policy"].startswith("mixed policy")
    assert pull["monotone_in_pull"] is False
    assert pull["grid"] == [0.1, 1.0, 2.0]
    assert combined["fields"]["widen_offset_multiplier"]["at_boundary"] is False
    assert combined["fields"]["widen_offset_multiplier"]["grid"] == [0.0, 0.5, 1.0]
    assert combined["source"]["pull_grid"] == [0.1, 1.0, 2.0]
    assert combined["ofi"]["consecutive_fills_threshold"] == pytest.approx(2.0)

    # All partials at the same corner keep the corner policy.
    _write_partial(
        partials,
        "risk_calibration",
        "2026-11-04T01",
        sample={"windows": 100},
        value=0.0,
        start=common["start"],
        end=common["end"],
        extra={
            "fields": {
                "pull_multiplier": {
                    "corner": "never_pull",
                    "at_corner": True,
                    "policy": "never pull; widen by the measured offset",
                    "monotone_in_pull": True,
                    "grid": [0.1, 1.0],
                },
                "widen_offset_multiplier": {"at_boundary": False, "grid": [0.0, 0.5]},
            },
            "source": {"pull_grid": [0.1, 1.0], "widen_grid": [0.0, 0.5]},
            "ofi": {"consecutive_fills_threshold": 2.0},
        },
    )
    combined = refit.combine_artifact(
        "risk_calibration", partials_root=partials, derived_dir=derived
    )
    assert combined is not None
    pull = combined["fields"]["pull_multiplier"]
    assert pull["corner"] == "never_pull"
    assert pull["at_corner"] is True
    assert pull["policy"] == "never pull; widen by the measured offset"
    assert pull["monotone_in_pull"] is True


def test_risk_command_and_helper_pass_pooled_streak_hours(tmp_path: Path) -> None:
    partition = refit.parse_partition("2026-11-04T01")
    partitions = [partition, refit.parse_partition("2026-11-04T03")]
    assert refit._risk_streak_hours(tmp_path, partitions) == [
        "2026-11-04T01",
        "2026-11-04T03",
    ]

    (tmp_path / "risk_calibration.json").write_text(
        json.dumps({"sample_size": {"streak_hours": ["2026-11-04T01", "2026-11-04T07"]}}),
        encoding="utf-8",
    )
    assert refit._risk_streak_hours(tmp_path, partitions) == [
        "2026-11-04T01",
        "2026-11-04T07",
    ]

    command = refit._risk_command(
        "python",
        partition,
        tmp_path / "out.json",
        tmp_path / "panel.parquet",
        60.0,
        1,
        streak_hours=["2026-11-04T01", "2026-11-04T07"],
    )
    index = command.index("--streak-hours")
    assert command[index + 1 : index + 3] == [
        "2026-11-04T01",
        "2026-11-04T07",
    ]
    assert command[command.index("--max-files") + 1] == "1"


def test_block_resumes_cursor_and_skips_completed(tmp_path: Path, monkeypatch) -> None:
    tape = tmp_path / "tape"
    labels = [
        "2026-11-04T00",
        "2026-11-04T01",
        "2026-11-04T02",
        "2026-11-04T03",
    ]
    _partition_tree(tape, labels)
    calls: list[str] = []

    def fake_fit(artifact, partition, *, partials_root, **kwargs):
        calls.append(partition.label)
        for name in ("queue_fit", "queue_value"):
            _write_partial(
                partials_root,
                name,
                partition.label,
                sample={"prints": 2000} if name == "queue_fit" else 5,
                value=0.1,
                start=f"{partition.date}T{partition.hour}:00:00+00:00",
                end=f"{partition.date}T{partition.hour}:59:59+00:00",
                extra={
                    # A current-schema partial: the pooled fields need these
                    # totals, and a legacy partial without them is re-fit.
                    "fields": {
                        "trade_arrival_rate_shares_sec": {
                            "touch_shares": 1000.0,
                            "active_asset_seconds": 1000.0,
                        },
                        "annual_capital_hurdle_rate": {
                            "market_yields": [
                                {
                                    "capital": 500.0,
                                    "quote": 0.5,
                                    "gross_usd_per_day": 10.0,
                                    "share": 0.1,
                                    "competing_score": 100.0,
                                    "cost_usd_per_day": 0.0,
                                }
                            ]
                        },
                        "reward_accrual_usd_per_share_sec": {"values": [1e-9]},
                    }
                },
            )
        return True, "fake"

    monkeypatch.setattr(refit, "_run_fit_for_artifact", fake_fit)
    common = {
        "artifacts": ["queue_fit"],
        "tape_root": tape,
        "derived_dir": tmp_path / "derived",
        "partials_root": tmp_path / "derived" / "fit_partials",
        "state_path": tmp_path / "derived" / "refit_state.json",
    }

    first = refit.run_block(max_partitions=2, **common)
    assert first["processed"] == labels[:2]
    assert first["cursor"] == 2

    second = refit.run_block(max_partitions=2, **common)
    assert second["processed"] == labels[2:]
    assert second["cursor"] == 0

    calls.clear()
    third = refit.run_block(max_partitions=1, **common)

    assert third["skipped"] is True
    assert calls == []
    state = json.loads(common["state_path"].read_text())
    assert state["cursor"] == 0
    for label in labels:
        assert state["partitions"][label]["queue_fit"]["status"] == "ok"


def test_failed_partition_is_skipped_without_retry(tmp_path: Path, monkeypatch) -> None:
    tape = tmp_path / "tape"
    labels = ["2026-11-04T00", "2026-11-04T01"]
    _partition_tree(tape, labels)
    derived = tmp_path / "derived"
    state_path = derived / "refit_state.json"
    state = refit.load_state(state_path)
    refit._mark(
        state,
        refit.parse_partition(labels[0]),
        "queue_fit",
        "failed",
        detail="unidentifiable",
    )
    refit.atomic_write_json(state_path, state)
    calls: list[str] = []

    def fake_fit(artifact, partition, *, partials_root, **kwargs):
        calls.append(partition.label)
        _write_partial(
            partials_root,
            "queue_fit",
            partition.label,
            sample={"prints": 2000},
            value=0.1,
            start="2026-11-04T01:00:00+00:00",
            end="2026-11-04T01:59:59+00:00",
        )
        return True, "fake"

    monkeypatch.setattr(refit, "_run_fit_for_artifact", fake_fit)
    result = refit.run_block(
        max_partitions=1,
        artifacts=["queue_fit"],
        tape_root=tape,
        derived_dir=derived,
        partials_root=derived / "fit_partials",
        state_path=state_path,
    )

    assert result["processed"] == [labels[1]]
    assert calls == [labels[1]]


def test_budget_partial_is_retried_without_retry_failed(
    tmp_path: Path, monkeypatch
) -> None:
    tape = tmp_path / "tape"
    label = "2026-11-04T00"
    _partition_tree(tape, [label])
    derived = tmp_path / "derived"
    state_path = derived / "refit_state.json"
    calls: list[str] = []

    def fake_fit(artifact, partition, *, partials_root, **kwargs):
        calls.append(partition.label)
        path = partials_root / "queue_fit" / f"{partition.label}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if len(calls) == 1:
            path.write_text(
                json.dumps(
                    {
                        "partial": True,
                        "stage": "panel",
                        "window": {"start": None, "end": None},
                        "sample_size": {"windows": 10},
                    }
                ),
                encoding="utf-8",
            )
            return False, "fitter reported partial=true (budget exhausted)"
        _write_partial(
            partials_root,
            "queue_fit",
            partition.label,
            sample={"prints": 2000},
            value=0.1,
            start="2026-11-04T00:00:00+00:00",
            end="2026-11-04T00:59:59+00:00",
        )
        return True, "fake"

    monkeypatch.setattr(refit, "_run_fit_for_artifact", fake_fit)
    common = {
        "max_partitions": 1,
        "artifacts": ["queue_fit"],
        "tape_root": tape,
        "derived_dir": derived,
        "partials_root": derived / "fit_partials",
        "state_path": state_path,
    }

    first = refit.run_block(**common)
    assert first["partial"] == [f"{label}:queue_fit"]
    assert first["failed"] == []
    state = json.loads(state_path.read_text())
    assert state["partitions"][label]["queue_fit"]["status"] == "partial"

    second = refit.run_block(**common)
    assert second["processed"] == [label]
    assert second["partial"] == []
    assert calls == [label, label]
    state = json.loads(state_path.read_text())
    assert state["partitions"][label]["queue_fit"]["status"] == "ok"


def test_retry_failed_retries_the_recorded_partition(tmp_path: Path, monkeypatch) -> None:
    tape = tmp_path / "tape"
    labels = ["2026-11-04T00", "2026-11-04T01"]
    _partition_tree(tape, labels)
    derived = tmp_path / "derived"
    state_path = derived / "refit_state.json"
    state = refit.load_state(state_path)
    refit._mark(
        state,
        refit.parse_partition(labels[0]),
        "queue_fit",
        "failed",
        detail="timed out",
    )
    refit.atomic_write_json(state_path, state)
    calls: list[str] = []

    def fake_fit(artifact, partition, *, partials_root, **kwargs):
        calls.append(partition.label)
        _write_partial(
            partials_root,
            "queue_fit",
            partition.label,
            sample={"prints": 2000},
            value=0.1,
            start=f"{partition.date}T{partition.hour}:00:00+00:00",
            end=f"{partition.date}T{partition.hour}:59:59+00:00",
        )
        return True, "fake"

    monkeypatch.setattr(refit, "_run_fit_for_artifact", fake_fit)
    result = refit.run_block(
        max_partitions=1,
        artifacts=["queue_fit"],
        tape_root=tape,
        derived_dir=derived,
        partials_root=derived / "fit_partials",
        state_path=state_path,
        retry_failed=True,
    )

    assert result["processed"] == [labels[0]]
    assert calls == [labels[0]]
    state = json.loads(state_path.read_text())
    assert state["partitions"][labels[0]]["queue_fit"]["status"] == "ok"


def test_queue_value_failure_is_blocked_and_does_not_stop_other_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    tape = tmp_path / "tape"
    label = "2026-11-04T00"
    _partition_tree(tape, [label])
    derived = tmp_path / "derived"
    state_path = derived / "refit_state.json"
    calls: list[str] = []

    def fake_fit(artifact, partition, *, partials_root, **kwargs):
        calls.append(artifact)
        if artifact == "queue_fit":
            return False, "timed out after 1.0s"
        _write_partial(
            partials_root,
            "risk_calibration",
            partition.label,
            sample={"windows": 20000},
            value=0.5,
            start=f"{partition.date}T{partition.hour}:00:00+00:00",
            end=f"{partition.date}T{partition.hour}:59:59+00:00",
        )
        return True, "fake"

    monkeypatch.setattr(refit, "_run_fit_for_artifact", fake_fit)
    result = refit.run_block(
        max_partitions=1,
        artifacts=["queue_fit", "risk_calibration"],
        tape_root=tape,
        derived_dir=derived,
        partials_root=derived / "fit_partials",
        state_path=state_path,
    )

    assert calls == ["queue_fit", "risk_calibration"]
    assert result["failed"] == [f"{label}:queue_fit"]
    state = json.loads(state_path.read_text())
    entry = state["partitions"][label]
    assert entry["queue_fit"]["status"] == "failed"
    assert entry["queue_value"]["status"] == "blocked"
    assert "produced with queue_fit" in entry["queue_value"]["detail"]
    assert entry["risk_calibration"]["status"] == "ok"
    # The independent artifact was published even though the dependent one is
    # missing.
    assert (derived / "risk_calibration.json").exists()
    assert not (derived / "queue_fit.json").exists()


def test_timeout_leaves_inspectable_partial(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        refit,
        "_run_command",
        lambda *args, **kwargs: refit.FitOutput(
            -1, "", "worker died", timed_out=True
        ),
    )
    partials = tmp_path / "partials"
    ok, detail = refit._run_fit_for_artifact(
        "queue_fit",
        refit.parse_partition("2026-11-04T00"),
        python="python",
        partials_root=partials,
        derived_dir=tmp_path / "derived",
        timeout_s=60.0,
        risk_max_files=1,
        cwd=tmp_path,
    )

    assert ok is False
    assert "timed out" in detail
    for name in ("queue_fit", "queue_value"):
        payload = json.loads(
            (partials / name / "2026-11-04T00.json").read_text()
        )
        assert payload["partial"] is True
        assert payload["timed_out"] is True
        assert "timed out" in payload["error"]
        assert payload["command"]
        assert refit.partial_is_complete(partials / name / "2026-11-04T00.json") is False


def test_fresh_state_uses_the_first_run_timeout(tmp_path: Path, monkeypatch) -> None:
    tape = tmp_path / "tape"
    labels = ["2026-11-04T00", "2026-11-04T01"]
    _partition_tree(tape, labels)
    derived = tmp_path / "derived"
    timeouts: list[float] = []

    def fake_fit(artifact, partition, *, partials_root, timeout_s, **kwargs):
        timeouts.append(timeout_s)
        _write_partial(
            partials_root,
            "queue_fit",
            partition.label,
            sample={"prints": 2000},
            value=0.1,
            start=f"{partition.date}T{partition.hour}:00:00+00:00",
            end=f"{partition.date}T{partition.hour}:59:59+00:00",
        )
        return True, "fake"

    monkeypatch.setattr(refit, "_run_fit_for_artifact", fake_fit)
    common = {
        "max_partitions": 1,
        "artifacts": ["queue_fit"],
        "tape_root": tape,
        "derived_dir": derived,
        "partials_root": derived / "fit_partials",
        "state_path": derived / "refit_state.json",
        "partition_timeout_s": 60.0,
        "first_run_timeout_s": 300.0,
    }

    refit.run_block(**common)
    refit.run_block(**common)

    # The first bounded run is cold (300 s); the next block is warm (60 s).
    assert timeouts == [300.0, 60.0]


def test_invocation_budget_defers_the_next_partition(tmp_path: Path, monkeypatch) -> None:
    tape = tmp_path / "tape"
    labels = ["2026-11-04T00", "2026-11-04T01"]
    _partition_tree(tape, labels)
    derived = tmp_path / "derived"
    clock = {"t": 0.0}
    monkeypatch.setattr(refit.time, "monotonic", lambda: clock["t"])

    def fake_fit(artifact, partition, *, partials_root, **kwargs):
        clock["t"] += 10.0
        _write_partial(
            partials_root,
            "queue_fit",
            partition.label,
            sample={"prints": 2000},
            value=0.1,
            start=f"{partition.date}T{partition.hour}:00:00+00:00",
            end=f"{partition.date}T{partition.hour}:59:59+00:00",
        )
        return True, "fake"

    monkeypatch.setattr(refit, "_run_fit_for_artifact", fake_fit)
    result = refit.run_block(
        max_partitions=2,
        artifacts=["queue_fit"],
        tape_root=tape,
        derived_dir=derived,
        partials_root=derived / "fit_partials",
        state_path=derived / "refit_state.json",
        invocation_budget_s=5.0,
    )

    assert result["processed"] == [labels[0]]
    assert result["budget_exhausted"] is True
    assert result["cursor"] == 1


def test_reload_swaps_queue_and_invalidates_deskew(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue_fit.json"
    deskew_path = tmp_path / "deskew_calibration.json"
    queue_path.write_text(
        json.dumps(
            {
                "trade_arrival_rate_shares_sec": 0.2,
                "cancellation_rate_intensity": 0.01,
                "kyle_lambda": 0.001,
                "volatility_annualized": 40.0,
                "annual_capital_hurdle_rate": 0.5,
                "window": {"start": "a", "end": "b"},
                "partitions": ["2026-11-04T00"],
            }
        ),
        encoding="utf-8",
    )
    deskew_path.write_text(
        json.dumps({"window": {"start_utc": "a", "end_utc": "b"}}),
        encoding="utf-8",
    )
    engine = SimpleNamespace(
        migration_engine=SimpleNamespace(queue_model=None),
        _deskew_optimizer=object(),
        _deskew_disabled_logged=True,
        _fit_artifact_stamps={},
    )
    paths = {"queue_fit": queue_path, "deskew_calibration": deskew_path}

    from polymarketbot.live.base import reload_fit_artifacts

    assert reload_fit_artifacts(engine, paths) == []

    queue_path.write_text(
        json.dumps(
            {
                "trade_arrival_rate_shares_sec": 0.4,
                "cancellation_rate_intensity": 0.02,
                "kyle_lambda": 0.002,
                "volatility_annualized": 50.0,
                "annual_capital_hurdle_rate": 0.6,
                "window": {"start": "c", "end": "d"},
                "partitions": ["2026-11-04T00", "2026-11-04T01"],
            }
        ),
        encoding="utf-8",
    )
    stamp = queue_path.stat().st_mtime_ns + 1_000_000
    os.utime(queue_path, ns=(stamp, stamp))
    deskew_stamp = deskew_path.stat().st_mtime_ns + 1_000_000
    os.utime(deskew_path, ns=(deskew_stamp, deskew_stamp))

    reloaded = reload_fit_artifacts(engine, paths)

    assert reloaded == ["queue_fit", "deskew_calibration"]
    assert engine.migration_engine.queue_model.trade_rate == pytest.approx(0.4)
    assert engine._deskew_optimizer is None
    # The stamp is stored at the tick boundary; the next tick must not swap
    # the same files again.
    assert reload_fit_artifacts(engine, paths) == []


def test_supervisor_block_cadence(monkeypatch) -> None:
    from polymarketbot.live.supervisor import refit as supervisor_refit

    launches: list[float] = []

    def fake_launch(ctx, now):
        launches.append(now)
        ctx.last_refit_at = now

    monkeypatch.setattr(supervisor_refit, "_launch", fake_launch)
    ctx = SimpleNamespace(
        args=SimpleNamespace(refit_every=3600.0),
        last_refit_at=-10_000.0,
        refit_task=None,
        refit_failures=0,
    )

    asyncio.run(supervisor_refit.supervise(ctx, now=10.0))
    assert launches == [10.0]

    asyncio.run(supervisor_refit.supervise(ctx, now=100.0))
    assert launches == [10.0]

    asyncio.run(supervisor_refit.supervise(ctx, now=3611.0))
    assert launches == [10.0, 3611.0]


def test_supervisor_block_command_is_bounded() -> None:
    from polymarketbot.live.supervisor import refit as supervisor_refit

    ctx = SimpleNamespace(
        args=SimpleNamespace(refit_max_partitions=2, refit_partition_timeout=60.0)
    )
    command = supervisor_refit._block_command(ctx)

    assert command[1:3] == ["-m", "polymarketbot.tools.refit"]
    assert command[command.index("--max-partitions") + 1] == "2"
    assert command[command.index("--partition-timeout") + 1] == "60.0"


def test_supervisor_starts_exactly_one_block_with_a_fake_clock(monkeypatch) -> None:
    from polymarketbot.live.supervisor import refit as supervisor_refit

    launches: list[float] = []

    def fake_launch(ctx, now):
        launches.append(now)
        ctx.last_refit_at = now
        ctx.refit_task = object()

    monkeypatch.setattr(supervisor_refit, "_launch", fake_launch)
    monkeypatch.setattr(
        supervisor_refit, "time", SimpleNamespace(time=lambda: 100.0)
    )
    ctx = SimpleNamespace(
        args=SimpleNamespace(refit_every=3600.0),
        last_refit_at=0.0,
        refit_task=None,
        refit_failures=0,
    )

    supervisor_refit.start(ctx)
    supervisor_refit.start(ctx)
    supervisor_refit.start(ctx)

    assert launches == [100.0]

    disabled = SimpleNamespace(
        args=SimpleNamespace(refit_every=0.0),
        last_refit_at=0.0,
        refit_task=None,
        refit_failures=0,
    )
    supervisor_refit.start(disabled)
    assert launches == [100.0]


def test_shutdown_reaps_an_in_flight_refit() -> None:
    from polymarketbot.live.supervisor import refit as supervisor_refit

    async def scenario() -> None:
        started = asyncio.Event()
        cleaned: list[bool] = []

        async def fake_block(ctx) -> None:
            started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                cleaned.append(True)
                ctx.refit_process = None

        ctx = SimpleNamespace(refit_task=None, refit_process=object())
        ctx.refit_task = asyncio.create_task(fake_block(ctx))
        await started.wait()

        # A cancelled refit is an expected shutdown event: shutdown must return
        # normally and leave no task behind, so the runner never sees the
        # CancelledError.
        await supervisor_refit.shutdown(ctx)

        assert ctx.refit_task is None
        assert cleaned == [True]

    asyncio.run(scenario())


def test_supervisor_collects_a_finished_block_then_launches_on_the_cadence(
    monkeypatch,
) -> None:
    from polymarketbot.live.supervisor import refit as supervisor_refit

    launches: list[float] = []

    def fake_launch(ctx, now):
        launches.append(now)
        ctx.last_refit_at = now
        ctx.refit_task = object()

    monkeypatch.setattr(supervisor_refit, "_launch", fake_launch)
    done = SimpleNamespace(
        done=lambda: True,
        cancelled=lambda: False,
        exception=lambda: None,
    )
    ctx = SimpleNamespace(
        args=SimpleNamespace(refit_every=3600.0),
        last_refit_at=100.0,
        refit_task=done,
        refit_failures=0,
    )

    asyncio.run(supervisor_refit.supervise(ctx, now=3699.0))
    assert launches == []
    assert ctx.refit_task is None

    asyncio.run(supervisor_refit.supervise(ctx, now=3700.0))
    assert launches == [3700.0]


# ---------------------------------------------------------------------------
# Per-field pooling: averaging a per-partition fit is not always the pooled fit.
# ---------------------------------------------------------------------------
def test_combine_forces_a_rejected_kyle_lambda_to_zero(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    _write_partial(
        partials,
        "queue_fit",
        "2026-11-04T00",
        sample={"prints": 2000},
        value=0.0,
        start="2026-11-04T00:00:00+00:00",
        end="2026-11-04T01:00:00+00:00",
        extra={
            "kyle_lambda": 0.002,
            "fields": {
                "kyle_lambda": {
                    "value": 0.002,
                    "identified": True,
                    "estimator": "event_study_fixed_effects",
                    "events": 1000,
                    "markets": 10,
                    "specifications": {"10s": {"coefficient": 0.002}},
                }
            },
        },
    )
    _write_partial(
        partials,
        "queue_fit",
        "2026-11-04T01",
        sample={"prints": 2000},
        value=0.0,
        start="2026-11-04T01:00:00+00:00",
        end="2026-11-04T02:00:00+00:00",
        extra={
            "kyle_lambda": 0.004,
            "fields": {
                "kyle_lambda": {
                    "value": 0.004,
                    "identified": False,
                    "estimator": "rejected (lambda = 0)",
                    "events": 1000,
                    "markets": 10,
                    "specifications": {"10s": {"coefficient": 0.0001}},
                    "rejection": {"rule": "no positive interval"},
                }
            },
        },
    )

    combined = refit.combine_artifact(
        "queue_fit", partials_root=partials, derived_dir=tmp_path / "derived"
    )

    assert combined is not None
    assert combined["kyle_lambda"] == 0.0
    field = combined["fields"]["kyle_lambda"]
    assert field["value"] == 0.0
    assert field["identified"] is False
    # The diagnostic slope and rejection evidence stay; they are not averaged.
    assert field["specifications"] == {"10s": {"coefficient": 0.0001}}
    assert field["rejection"] == {"rule": "no positive interval"}


def test_combine_cancellation_pools_summed_shares_over_time(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    for label, rate, cancelled, depth in (
        ("2026-11-04T00", 0.01, 10.0, 1000.0),
        ("2026-11-04T01", 0.9, 90.0, 1000.0),
    ):
        _write_partial(
            partials,
            "queue_fit",
            label,
            sample={"prints": 2000},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra={
                "cancellation_rate_intensity": rate,
                "fields": {
                    "cancellation_rate_intensity": {
                        "value": rate,
                        "cancelled_shares": cancelled,
                        "depth_seconds": depth,
                        "assets": 2.0,
                    }
                },
            },
        )

    combined = refit.combine_artifact(
        "queue_fit", partials_root=partials, derived_dir=tmp_path / "derived"
    )

    assert combined is not None
    # Weighted mean of the rates would be 0.455; the ratio of totals is 0.05.
    assert combined["cancellation_rate_intensity"] == pytest.approx(0.05)
    field = combined["fields"]["cancellation_rate_intensity"]
    assert field["value"] == pytest.approx(0.05)
    assert field["cancelled_shares"] == pytest.approx(100.0)
    assert field["depth_seconds"] == pytest.approx(2000.0)


def test_combine_volatility_is_a_seconds_weighted_rms(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    for label, value, seconds in (
        ("2026-11-04T00", 10.0, 3.0),
        ("2026-11-04T01", 30.0, 1.0),
    ):
        _write_partial(
            partials,
            "queue_fit",
            label,
            sample={"prints": 2000},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra={
                "volatility_annualized": value,
                "fields": {
                    "volatility_annualized": {
                        "value": value,
                        "seconds": seconds,
                        "sample_size": 100,
                    }
                },
            },
        )

    combined = refit.combine_artifact(
        "queue_fit", partials_root=partials, derived_dir=tmp_path / "derived"
    )

    assert combined is not None
    # Arithmetic weighted mean is 15; sqrt(sum(v^2 * dt) / sum(dt)) = sqrt(300).
    assert combined["volatility_annualized"] == pytest.approx(math.sqrt(300.0))
    assert combined["fields"]["volatility_annualized"]["value"] == pytest.approx(
        math.sqrt(300.0)
    )
    assert combined["fields"]["volatility_annualized"]["seconds"] == pytest.approx(4.0)


def test_combine_carries_an_unpoolable_hurdle_and_arrival(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    derived = tmp_path / "derived"
    derived.mkdir()
    (derived / "queue_fit.json").write_text(
        json.dumps(
            {
                "annual_capital_hurdle_rate": 0.5,
                "trade_arrival_rate_shares_sec": 0.19,
                "fields": {
                    "annual_capital_hurdle_rate": {
                        "value": 0.5,
                        "common_capital_usd": 490.0,
                        "distribution": {"p90": 0.5},
                    },
                    "trade_arrival_rate_shares_sec": {"value": 0.19},
                },
            }
        ),
        encoding="utf-8",
    )
    for label, hurdle, arrival in (
        ("2026-11-04T00", 10.0, 1.0),
        ("2026-11-04T01", 20.0, 3.0),
    ):
        _write_partial(
            partials,
            "queue_fit",
            label,
            sample={"prints": 2000},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra={
                "annual_capital_hurdle_rate": hurdle,
                "trade_arrival_rate_shares_sec": arrival,
                "fields": {
                    "annual_capital_hurdle_rate": {
                        "value": hurdle,
                        "distribution": {"p90": hurdle},
                    },
                    "trade_arrival_rate_shares_sec": {"value": arrival},
                },
            },
        )

    combined = refit.combine_artifact(
        "queue_fit", partials_root=partials, derived_dir=derived
    )

    assert combined is not None
    assert combined["annual_capital_hurdle_rate"] == pytest.approx(0.5)
    assert combined["trade_arrival_rate_shares_sec"] == pytest.approx(0.19)
    assert combined["fields"]["annual_capital_hurdle_rate"]["unreconciled"] is True
    assert combined["fields"]["trade_arrival_rate_shares_sec"]["unreconciled"] is True
    assert set(combined["combination"]["unreconciled"]) >= {
        "annual_capital_hurdle_rate",
        "trade_arrival_rate_shares_sec",
    }
    assert "annual_capital_hurdle_rate" in combined["unreconciled_fields"]


def test_combine_pools_the_hurdle_at_one_common_capital(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    for label, capital, gross, share, competing in (
        ("2026-11-04T00", 100.0, 5.0, 0.5, 100.0),
        ("2026-11-04T01", 200.0, 10.0, 0.5, 100.0),
    ):
        _write_partial(
            partials,
            "queue_fit",
            label,
            sample={"prints": 2000},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra={
                "fields": {
                    "annual_capital_hurdle_rate": {
                        "value": None,
                        "common_capital_usd": capital,
                        "market_yields": [
                            {
                                "quote": 0.5,
                                "capital": capital,
                                "gross_usd_per_day": gross,
                                "cost_usd_per_day": 0.0,
                                "share": share,
                                "competing_score": competing,
                            }
                        ],
                    }
                }
            },
        )

    combined = refit.combine_artifact(
        "queue_fit", partials_root=partials, derived_dir=tmp_path / "derived"
    )

    assert combined is not None
    field = combined["fields"]["annual_capital_hurdle_rate"]
    assert field["common_capital_usd"] == pytest.approx(200.0)
    assert field["distribution"]["count"] == 2
    assert combined["annual_capital_hurdle_rate"] == pytest.approx(
        field["distribution"]["p90"]
    )
    # The 100-capital market is rescaled to 200 before the percentile.
    assert combined["annual_capital_hurdle_rate"] > 0.0
    assert "annual_capital_hurdle_rate" not in combined["combination"]["unreconciled"]


def test_combine_carries_an_unpoolable_reward_accrual(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    derived = tmp_path / "derived"
    derived.mkdir()
    (derived / "queue_fit.json").write_text(
        json.dumps(
            {
                "reward_accrual_usd_per_share_sec": 1e-09,
                "fields": {"reward_accrual_usd_per_share_sec": {"value": 1e-09}},
            }
        ),
        encoding="utf-8",
    )
    for label, p50 in (("2026-11-04T00", 2e-09), ("2026-11-04T01", 4e-09)):
        _write_partial(
            partials,
            "queue_fit",
            label,
            sample={"prints": 2000},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra={
                "reward_accrual_usd_per_share_sec": p50,
                "fields": {
                    "reward_accrual_usd_per_share_sec": {
                        "value": p50,
                        "distribution": {"p50": p50},
                    }
                },
            },
        )

    combined = refit.combine_artifact(
        "queue_fit", partials_root=partials, derived_dir=derived
    )

    assert combined is not None
    assert combined["reward_accrual_usd_per_share_sec"] == pytest.approx(1e-09)
    assert combined["fields"]["reward_accrual_usd_per_share_sec"]["unreconciled"] is True


def test_combine_risk_c_depth_is_a_median_with_spread(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    for index, depth in enumerate((1.0, 100.0, 3.0)):
        label = f"2026-11-04T0{index}"
        _write_partial(
            partials,
            "risk_calibration",
            label,
            sample={"windows": 100},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra={
                "ofi": {
                    "c_depth": depth,
                    "v_ref": 100.0 * 10**index,
                    "beta_vol": 0.1,
                    "consecutive_fills_threshold": 2,
                },
                "sample_size": {"windows": 100, "volume_windows": 1},
            },
        )

    combined = refit.combine_artifact(
        "risk_calibration", partials_root=partials, derived_dir=tmp_path / "derived"
    )

    assert combined is not None
    assert combined["ofi"]["c_depth"] == pytest.approx(3.0)
    field = combined["fields"]["c_depth"]
    assert field["value"] == pytest.approx(3.0)
    assert field["median"] == pytest.approx(3.0)
    assert field["spread"] == {"min": 1.0, "max": 100.0, "count": 3}
    # v_ref is a geometric mean, not the arithmetic mean of 5050.
    assert combined["ofi"]["v_ref"] == pytest.approx(1000.0)


def test_combine_reserve_diversification_weights_by_pairs(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    for label, correlation, pairs in (
        ("2026-11-04T00", 0.5, 10.0),
        ("2026-11-04T01", -0.5, 90.0),
    ):
        _write_partial(
            partials,
            "reserve_calibration",
            label,
            sample={"horizon_shocks": 100},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra={
                "jump_shock_factor": 0.6,
                "diversification_weight": 0.5,
                "diagnostics": {
                    "correlation": {
                        "mean_correlation": correlation,
                        "pairs": pairs,
                    }
                },
            },
        )

    combined = refit.combine_artifact(
        "reserve_calibration", partials_root=partials, derived_dir=tmp_path / "derived"
    )

    assert combined is not None
    # Pair-weighted mean correlation = -0.4, so diversification = 1.4.
    assert combined["diversification_weight"] == pytest.approx(1.4)
    assert combined["diagnostics"]["correlation"]["mean_correlation"] == pytest.approx(-0.4)


def test_combine_marks_reserve_quantiles_unreconciled(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    derived = tmp_path / "derived"
    derived.mkdir()
    (derived / "reserve_calibration.json").write_text(
        json.dumps({"jump_shock_factor": 0.2, "base_gas_actions": 1.0,
                    "per_market_gas_actions": 0.1}),
        encoding="utf-8",
    )
    for label, jump in (("2026-11-04T00", 0.5), ("2026-11-04T01", 0.7)):
        _write_partial(
            partials,
            "reserve_calibration",
            label,
            sample={"horizon_shocks": 100},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra={"jump_shock_factor": jump},
        )

    combined = refit.combine_artifact(
        "reserve_calibration", partials_root=partials, derived_dir=derived
    )

    assert combined is not None
    assert combined["jump_shock_factor"] == pytest.approx(0.2)
    assert combined["diagnostics"]["jump_abs_quantile"] == pytest.approx(0.2)
    assert "jump_shock_factor" in combined["combination"]["unreconciled"]


def test_combine_marks_deskew_percentiles_unreconciled(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    derived = tmp_path / "derived"
    derived.mkdir()
    (derived / "deskew_calibration.json").write_text(
        json.dumps({"holding_min_seconds": 10.0, "holding_max_seconds": 100.0}),
        encoding="utf-8",
    )
    for label, low, high in (
        ("2026-11-04T00", 1.0, 50.0),
        ("2026-11-04T01", 20.0, 200.0),
    ):
        _write_partial(
            partials,
            "deskew_calibration",
            label,
            sample={"round_trips": 100},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra={"holding_min_seconds": low, "holding_max_seconds": high},
        )

    combined = refit.combine_artifact(
        "deskew_calibration", partials_root=partials, derived_dir=derived
    )

    assert combined is not None
    assert combined["holding_min_seconds"] == pytest.approx(10.0)
    assert combined["holding_max_seconds"] == pytest.approx(100.0)
    assert "holding_min_seconds" in combined["combination"]["unreconciled"]


def test_combine_pools_the_replay_grid_by_lowest_regret(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    for label in ("2026-11-04T00", "2026-11-04T01"):
        _write_partial(
            partials,
            "risk_calibration",
            label,
            sample={"windows": 100},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra={
                "action_replay": {
                    "events": 100.0,
                    "grid": [
                        {
                            "pull_multiplier": 0.1,
                            "widen_offset_multiplier": 0.0,
                            "objective": -0.1,
                        },
                        {
                            "pull_multiplier": 2.0,
                            "widen_offset_multiplier": 0.0,
                            "objective": -0.2,
                        },
                    ],
                }
            },
        )

    combined = refit.combine_artifact(
        "risk_calibration", partials_root=partials, derived_dir=tmp_path / "derived"
    )

    assert combined is not None
    # The objective is regret: the minimum, not the maximum, is the optimum.
    assert combined["ofi"]["pull_multiplier"] == pytest.approx(2.0)
    assert combined["ofi"]["widen_offset_multiplier"] == pytest.approx(0.0)
    assert combined["fields"]["pull_multiplier"]["best_grid_point"]["objective"] == pytest.approx(
        -0.2
    )


def test_measure_touch_rate_records_the_poolable_totals() -> None:
    from polymarketbot.tools.fit_queue_model import measure_touch_rate

    books = {"quote_rows": {"asset": [(0.0, 0.5, 0.6)]}}
    prints = {"by_asset": {"asset": [(30.0, 0.5, 10.0, "buy")]}}

    measured = measure_touch_rate(books, prints)

    # The rate is the EWMA; the two totals are what a pooled rate uses.
    assert measured["touch_shares"] == pytest.approx(10.0)
    assert measured["active_asset_seconds"] == pytest.approx(60.0)
    assert measured["value"] == pytest.approx(10.0 / 60.0)


# ---------------------------------------------------------------------------
# Sufficient-statistics pooling
# ---------------------------------------------------------------------------
def _risk_sufficient(values: list[float], *, hours: list[str], fills: int,
                     run_length: list[dict] | None = None) -> dict:
    from polymarketbot.tools.pooling_stats import linear_histogram

    return {
        "sufficient_statistics": {
            "schema": 2,
            "ofi_abs": linear_histogram(values, lo=0.0, hi=50.0, width=0.05),
            "streak_hours": hours,
            "streak_fills": fills,
            "streak_min_cell": 30,
        },
        "adverse_streak": {
            "fills": fills,
            "adverse_fills": fills // 2,
            "min_cell": 30,
            "run_length": run_length or [],
            "threshold": None,
        },
    }


def test_histogram_quantile_reports_the_bin_resolution() -> None:
    from polymarketbot.tools.pooling_stats import (
        linear_histogram,
        merge_histograms,
        quantile_summary,
    )

    left = linear_histogram([1.0] * 8, lo=0.0, hi=10.0, width=1.0)
    right = linear_histogram([9.0] * 2, lo=0.0, hi=10.0, width=1.0)
    merged = merge_histograms([left, right])
    summary = quantile_summary(merged, 0.9)

    assert summary is not None
    # 90 % of 10 pooled values: the rank falls in the [9, 10) bin.
    assert summary["bin"] == [9.0, 10.0]
    assert summary["value"] == pytest.approx(9.5)
    assert summary["resolution"] == pytest.approx(1.0)
    assert summary["cdf"] == pytest.approx(1.0)
    assert summary["total"] == 10


def test_combine_pools_risk_z_score_from_merged_histograms(tmp_path: Path) -> None:
    from polymarketbot.tools.pooling_stats import (
        linear_histogram,
        merge_histograms,
        quantile_summary,
    )

    partials = tmp_path / "partials"
    values = {
        "2026-11-04T00": [0.5, 1.5, 7.9],
        "2026-11-04T01": [2.5, 3.5],
        "2026-11-04T02": [8.1, 8.2, 8.3, 8.4],
    }
    for label, sample in values.items():
        _write_partial(
            partials,
            "risk_calibration",
            label,
            sample={"windows": 100},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra={
                "ofi": {"z_score": 99.0},
                "fields": {"z_score": {"value": 99.0, "trigger_budget": 0.05}},
                **_risk_sufficient(sample, hours=["2026-11-04T00"], fills=10),
            },
        )

    combined = refit.combine_artifact(
        "risk_calibration", partials_root=partials, derived_dir=tmp_path / "derived"
    )

    assert combined is not None
    expected = quantile_summary(
        merge_histograms(
            [
                linear_histogram(sample, lo=0.0, hi=50.0, width=0.05)
                for sample in values.values()
            ]
        ),
        0.95,
    )
    assert expected is not None
    assert combined["ofi"]["z_score"] == pytest.approx(expected["value"])
    assert combined["fields"]["z_score"]["quantile"]["resolution"] == pytest.approx(0.05)
    assert combined["fields"]["z_score"]["quantile"]["bin"] == expected["bin"]
    assert "z_score" not in combined["combination"]["unreconciled"]
    # The partial-only block is evidence, not a combined average of counts.
    assert "sufficient_statistics" not in combined


def test_combine_pools_the_streak_distribution_once_per_hour_set(tmp_path: Path) -> None:
    from polymarketbot.tools.fit_risk_calibration import streak_analysis

    partials = tmp_path / "partials"
    rows = [
        {
            "streak": 1,
            "n": 40,
            "sum_fill_net": -0.40,
            "sum_fill_net_sq": 0.02,
            "sum_fill_markout": 0.20,
        }
    ]
    for label in ("2026-11-04T00", "2026-11-04T01"):
        # Both partials measured the same streak_hours, so their identical
        # statistics must not be summed twice.
        _write_partial(
            partials,
            "risk_calibration",
            label,
            sample={"windows": 100},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra=_risk_sufficient([1.0], hours=["2026-11-04T00"], fills=40,
                                   run_length=rows),
        )

    combined = refit.combine_artifact(
        "risk_calibration", partials_root=partials, derived_dir=tmp_path / "derived"
    )

    assert combined is not None
    assert combined["adverse_streak"]["fills"] == 40
    assert combined["adverse_streak"]["run_length"][0]["n"] == 40
    assert combined["adverse_streak"]["threshold"] == 1
    assert combined["ofi"]["consecutive_fills_threshold"] == 1
    assert "consecutive_fills_threshold" not in combined["combination"]["unreconciled"]
    # The additive sums rebuild the fitter's own conditional mean.
    assert combined["adverse_streak"]["run_length"][0]["mean_fill_net"] == pytest.approx(
        -0.01
    )
    del streak_analysis  # imported only to document the estimator's origin


def _deskew_sufficient(
    *,
    holdings: list[float],
    expiry: list[float],
    rates: list[float],
    ofi: list[float],
    drift: list[float],
    placements: float,
    grid: list[tuple[float, float, int]],
    gas_usd: float,
    denominator: float,
) -> dict:
    from polymarketbot.tools.pooling_stats import log_histogram

    return {
        "sufficient_statistics": {
            "schema": 2,
            "holding_seconds": log_histogram(
                holdings, lo=0.01, hi=1e7, per_decade=100
            ),
            "expiry_fraction": log_histogram(
                expiry, lo=1e-9, hi=1.0, per_decade=100
            ),
            "print_rates_per_min": log_histogram(
                rates, lo=1e-4, hi=1e4, per_decade=50
            ),
            "abs_ofi": log_histogram(ofi, lo=1e-3, hi=1e6, per_decade=50),
            "abs_drift_cents": log_histogram(
                drift, lo=1e-3, hi=100.0, per_decade=50
            ),
            "skew": {
                "placements": placements,
                "grid": [
                    {"phi": phi, "net_total": net, "fills": fills}
                    for phi, net, fills in grid
                ],
            },
            "merge_gas": {"gas_usd": gas_usd, "denominator": denominator},
            "holding_quantiles": [0.10, 0.90],
            "expiry_quantile": 0.90,
            "loss_probability": 0.01,
        }
    }


def test_combine_pools_deskew_quantiles_and_the_skew_argmax(tmp_path: Path) -> None:
    from polymarketbot.tools.pooling_stats import (
        log_histogram,
        merge_histograms,
        quantile_summary,
    )

    partials = tmp_path / "partials"
    holdings = {
        "2026-11-04T00": [1.0, 2.0, 3.0],
        "2026-11-04T01": [10.0, 20.0],
        "2026-11-04T02": [100.0],
    }
    for label, sample in holdings.items():
        _write_partial(
            partials,
            "deskew_calibration",
            label,
            sample={"round_trips": 100},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra={
                "holding_min_seconds": 999.0,
                "holding_max_seconds": 999.0,
                "holding_expiry_fraction": 999.0,
                "holding_turnover_cycles": 999.0,
                "ofi_toxicity_threshold": 999.0,
                "drift_toxicity_cents": 999.0,
                "skew_aggressiveness": 999.0,
                **_deskew_sufficient(
                    holdings=sample,
                    expiry=[0.1, 0.2],
                    rates=[10.0, 20.0],
                    ofi=[1.0, 2.0, 3.0],
                    drift=[0.5, 1.5],
                    placements=10.0,
                    # phi 0.005 earns more per placement when summed; a mean
                    # of per-partition argmaxes cannot see that.
                    grid=[(0.0, 0.02, 5), (0.005, 0.03, 4)],
                    gas_usd=0.001,
                    denominator=1.0,
                ),
            },
        )

    combined = refit.combine_artifact(
        "deskew_calibration", partials_root=partials, derived_dir=tmp_path / "derived"
    )

    assert combined is not None
    merged = merge_histograms(
        [
            log_histogram(sample, lo=0.01, hi=1e7, per_decade=100)
            for sample in holdings.values()
        ]
    )
    low = quantile_summary(merged, 0.10)
    high = quantile_summary(merged, 0.90)
    assert low is not None and high is not None
    assert combined["holding_min_seconds"] == pytest.approx(
        max(0.1, low["value"])
    )
    assert combined["holding_max_seconds"] == pytest.approx(max(1.0, high["value"]))
    assert combined["skew_aggressiveness"] == pytest.approx(0.005)
    assert combined["diagnostics"]["skew_fills_at_gain"] == 12
    # Three partitions each charge 0.001 gas against one unit of batch x
    # spread: the pooled ratio of sums is 0.003 / 3.0, not the sum of ratios.
    assert combined["merge_gas_fraction"] == pytest.approx(0.001)
    unreconciled = combined["combination"]["unreconciled"]
    for field in (
        "holding_min_seconds",
        "holding_max_seconds",
        "holding_expiry_fraction",
        "holding_turnover_cycles",
        "ofi_toxicity_threshold",
        "drift_toxicity_cents",
        "skew_aggressiveness",
        "merge_gas_fraction",
    ):
        assert field not in unreconciled
    assert combined["pooled_quantiles"]["holding_max_seconds"]["resolution_relative"] > 0


def test_combine_pools_reserve_jump_and_merge_actions(tmp_path: Path) -> None:
    from polymarketbot.tools.pooling_stats import (
        linear_histogram,
        merge_histograms,
        quantile_summary,
    )

    partials = tmp_path / "partials"
    shocks = {
        "2026-11-04T00": [0.1, 0.2, 0.3],
        "2026-11-04T01": [0.4, 0.5],
        "2026-11-04T02": [0.6],
    }
    markets = {
        "2026-11-04T00": {"a": {"fills": 2, "hours": 1}, "b": {"fills": 1, "hours": 1}},
        "2026-11-04T01": {"a": {"fills": 3, "hours": 1}},
        "2026-11-04T02": {"b": {"fills": 1, "hours": 1}},
    }
    for label, sample in shocks.items():
        _write_partial(
            partials,
            "reserve_calibration",
            label,
            sample={"horizon_shocks": 100},
            value=0.0,
            start=f"{label}:00:00+00:00",
            end=f"{label}:59:59+00:00",
            extra={
                "jump_shock_factor": 999.0,
                "base_gas_actions": 999.0,
                "per_market_gas_actions": 999.0,
                "diagnostics": {"correlation": {"mean_correlation": 0.0, "pairs": 10}},
                "sufficient_statistics": {
                    "schema": 2,
                    "jump_abs": linear_histogram(
                        sample, lo=0.0, hi=2.0, width=0.002
                    ),
                    "jump_probability": 0.01,
                    "merge_actions": {
                        "horizon_seconds": 3600.0,
                        "fills_per_batch": 1.0,
                        "hours_total": float(
                            sum(cell["hours"] for cell in markets[label].values())
                        ),
                        "markets": markets[label],
                    },
                },
            },
        )

    combined = refit.combine_artifact(
        "reserve_calibration", partials_root=partials, derived_dir=tmp_path / "derived"
    )

    assert combined is not None
    expected = quantile_summary(
        merge_histograms(
            [
                linear_histogram(sample, lo=0.0, hi=2.0, width=0.002)
                for sample in shocks.values()
            ]
        ),
        0.99,
    )
    assert expected is not None
    assert combined["jump_shock_factor"] == pytest.approx(expected["value"])
    # Merged counts: a=(2+3)/2h, b=(1+1)/2h; horizon/3600 = 1h, batch 1.
    assert combined["base_gas_actions"] == pytest.approx(2.5 + 1.0)
    assert combined["per_market_gas_actions"] == pytest.approx(1.75)
    assert combined["diagnostics"]["merged_markets"] == 2
    assert combined["diagnostics"]["jump_abs_quantile"] == pytest.approx(
        expected["value"]
    )
    assert "jump_shock_factor" not in combined["combination"]["unreconciled"]
    assert "base_gas_actions" not in combined["combination"]["unreconciled"]
    assert "per_market_gas_actions" not in combined["combination"]["unreconciled"]


def test_a_stale_partial_is_re_fit_instead_of_carried(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    label = "2026-11-04T00"
    partition = refit.parse_partition(label)
    # Complete, but written before the sufficient-statistics schema.
    _write_partial(
        partials,
        "risk_calibration",
        label,
        sample={"windows": 100},
        value=0.0,
        start=f"{label}:00:00+00:00",
        end=f"{label}:59:59+00:00",
        extra={"ofi": {"z_score": 4.0}},
    )

    assert refit._pending({}, partition, "risk_calibration", partials, retry_failed=False)
    combined = refit.combine_artifact(
        "risk_calibration", partials_root=partials, derived_dir=tmp_path / "derived"
    )
    assert combined is not None
    reason = combined["combination"]["unreconciled"]["z_score"]
    assert "re-fit" in reason
    assert "2026-11-04T00" in reason

    # A partial that carries the histogram is complete and is not re-fit.
    _write_partial(
        partials,
        "risk_calibration",
        label,
        sample={"windows": 100},
        value=0.0,
        start=f"{label}:00:00+00:00",
        end=f"{label}:59:59+00:00",
        extra={
            "ofi": {"z_score": 4.0},
            **_risk_sufficient(
                [1.0, 2.0],
                hours=[label],
                fills=10,
                run_length=[
                    {
                        "streak": 1,
                        "n": 31,
                        "sum_fill_net": -0.31,
                        "sum_fill_net_sq": 0.02,
                        "sum_fill_markout": 0.1,
                    }
                ],
            ),
        },
    )
    assert not refit._pending(
        {}, partition, "risk_calibration", partials, retry_failed=False
    )
