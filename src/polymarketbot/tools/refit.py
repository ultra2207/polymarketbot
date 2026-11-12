"""Incremental, resumable refit of the tape-fitted live artifacts.

Every fitter under :mod:`polymarketbot.tools` already fits a *window* of the
tape.  The live engine needs the same artifacts updated while it runs, but a
full-tape fit is minutes of CPU and cannot be repeated on a supervision tick.
This tool therefore treats one complete ``date=*/hour=*`` partition as the
unit of work:

* run each fitter once per partition into
  ``data/derived/fit_partials/<artifact>/<partition>.json``;
* skip a partition whose partial is already there and complete;
* remember the next partition in ``data/derived/refit_state.json`` so a killed
  run resumes instead of refitting;
* combine the partials into the artifact the live engine reads, atomically,
  carrying ``window``, ``sample_size``, ``generated_at`` and ``partitions``.

A partition is complete when both ``data/tape/clob_events`` and
``data/tape/clob_trades`` hold a part for the same ``date/hour``: every fitter
below reads at least one of them, and half a partition is not a measurement.

The combination is per-field, because different measured quantities pool
differently and a mean of per-partition fits is not always the pooled fit:

* additive rates (a ratio of two measured totals) are recombined as a
  ratio of summed numerators and denominators — cancellation intensity from
  cancelled shares over depth-seconds, arrival from touch shares over
  active asset-seconds when the partials record those totals;
* realised variance is recombined from the per-partition ``seconds`` and
  annualised values as ``sqrt(sum(value^2 * seconds) / sum(seconds))``;
* coefficients (``c_depth``, ``beta_vol``) are pooled as the median of the
  per-partition fits with the spread recorded, because a pooled-panel refit
  is not derivable from fit outputs;
* grid-search optima (pull/widen multipliers) are re-pooled by the
  event-weighted mean objective on each grid cell and the cell that
  maximises it, not by averaging the per-partition argmins;
* ``v_ref`` is a geometric mean, so it pools as the sample-weighted
  geometric mean of the per-partition geometric means;
* quantiles, percentiles and tail thresholds are read off the *merged*
  per-partition histograms (``sufficient_statistics`` in the partials): the
  counts add on a fixed grid, the quantile is taken on the merged CDF, and the
  bin resolution is published next to the value in ``pooled_quantiles``.  A
  run-length distribution pools from its additive per-cell sums; a grid argmax
  pools from summed objective and fill counts;
* a field whose definition is a quantile, percentile or rejected
  coefficient and whose raw observations are not in the partials is
  **not** overwritten with a mean: the last published artifact value is
  carried forward and the field is stamped ``unreconciled`` with a
  ``pooling`` note (``kyle_lambda`` is forced to exactly ``0.0`` when the
  pooled identification is rejected).  A complete partial from an older
  schema is treated as pending and re-fit for that partition instead.

Everything else keeps the sample-size weighted mean.  The weight is the
artifact's own primary sample count (``prints`` for the queue fit,
``windows`` for the risk fit, ``round_trips`` for the de-skew fit,
``horizon_shocks`` for the reserve fit), and it is recorded in
``combination`` together with the rule used per field.  Windows are unioned
and sample counts are summed; the result is stamped ``partial: false``
because it covers the union of the listed partitions.

Bounded work
------------
``--max-partitions`` (default ``1``) caps the partitions one invocation may
process.  Each fitter subprocess is bounded by ``--partition-timeout``
(default 60 s); a fresh state's first invocation gets ``--first-run-timeout``
(default 300 s) because the interpreter, the panel cache and the measured
inputs are cold.  ``--invocation-budget`` (default 600 s) stops the block from
*starting* another fit past the ceiling, so a supervision block has a known
upper bound.  A timed-out or unidentifiable fit is recorded in the state as
``failed`` and skipped by later runs unless ``--retry-failed`` is given; it
never writes a partial that the combiner would mistake for a finished
measurement.  When the fitter wrote nothing before the timeout the block
leaves a ``partial: true`` diagnostic file at the output path (command,
elapsed and error), so the timeout is inspectable and a retry overwrites it.

Expected cost per one-hour partition, measured on the 2026-11-04 tape with a
warm environment: ``queue_fit``/``queue_value`` 10-70 s depending on the
hour's event-part count (11 s for the 5-part 00 hour, 69 s for the 12-part 02
hour), ``risk_calibration`` 12-42 s with the default ``--risk-max-files 1``
(42 s when the per-hour fill replay is cold).  De-skew and reserve are the
expensive artifacts (de-skew measured 60 s+ on some hours) and are the reason
the default per-fit ceiling is 60 s rather than 5 s.

``queue_value`` is not independent: ``fit_queue_model`` writes it in the same
invocation as ``queue_fit``.  A failed queue fit therefore marks
``queue_value`` as ``blocked`` (not failed) and the other artifacts in the
partition still run.

Risk partials reuse the live artifact's ``sample_size.streak_hours`` (or every
complete partition before an artifact exists) as the fitter's
``--streak-hours``, so a refit-driven partial reproduces the pooled
``consecutive_fills_threshold`` instead of measuring a thin one-partition
streak.

A full refit of the 2026-11-04..10 tape is 63 complete partitions, i.e. 63
one-partition blocks at the default ``--max-partitions 1`` (the supervisor's
default cadence reaches the same count one block per hour).

Usage
-----
    python -m polymarketbot.tools.refit --max-partitions 1
    python -m polymarketbot.tools.refit --max-partitions 2 --artifact queue_fit
    python -m polymarketbot.tools.refit --artifact risk_calibration \
        --partitions 2026-11-04T01,2026-11-04T02
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import statistics
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.tools.pooling_stats import (
    SCHEMA,
    HistogramMismatch,
    is_histogram,
    merge_histograms,
    quantile_summary,
)

log = get_logger("refit")

_REPO_ROOT = Path(__file__).resolve().parents[3]
TAPE_ROOT = _REPO_ROOT / "data" / "tape"
DERIVED_DIR = _REPO_ROOT / "data" / "derived"
PARTIALS_ROOT = DERIVED_DIR / "fit_partials"
STATE_PATH = DERIVED_DIR / "refit_state.json"

DEFAULT_MAX_PARTITIONS = 1
DEFAULT_PARTITION_TIMEOUT_S = 60.0
#: A fresh state has no warm panel/caches; the first bounded run may take 300 s.
DEFAULT_FIRST_RUN_TIMEOUT_S = 300.0
#: The whole invocation stops starting new fits past this ceiling.
DEFAULT_INVOCATION_BUDGET_S = 600.0

#: Artifact -> the sample-size cell that weights its combination.  The path is
#: read from each partial; a partial without a positive value on this path is
#: rejected rather than weighted by a guessed 1.0.
ARTIFACTS: dict[str, tuple[str, ...]] = {
    "queue_fit": ("sample_size", "prints"),
    "queue_value": ("sample_size",),
    "risk_calibration": ("sample_size", "windows"),
    "deskew_calibration": ("sample_size", "round_trips"),
    "reserve_calibration": ("sample_size", "horizon_shocks"),
}

#: Artifacts written by one fitter invocation.
_QUEUE_FAMILY = ("queue_fit", "queue_value")

#: Minimum combined sample, in the units of :data:`ARTIFACTS`, before an
#: artifact may be published.  These are the fitters' own floors, not invented
#: statistics: ``fit_queue_model`` refuses a print-level fit below
#: ``KYLE_MIN_EVENTS`` (1000 samples), ``fit_risk_calibration`` refuses a
#: streak cell below ``MIN_STREAK_SAMPLES`` (30 fills/windows), the
#: ``--min-bucket`` default of ``fit_deskew_calibration`` is 50 observations,
#: and the ``--min-obs`` default of ``fit_reserve`` is 3 shocks.  A queue-value
#: trial is a replay of one measured book, so one trial is a measurement.
MIN_SAMPLES: dict[str, float] = {
    "queue_fit": 1000.0,
    "queue_value": 1.0,
    "risk_calibration": 30.0,
    "deskew_calibration": 50.0,
    "reserve_calibration": 3.0,
}

#: Fit order inside one partition.  ``reserve_calibration`` depends on the
#: combined de-skew artifact, so it runs after ``deskew_calibration``.
FIT_ORDER = (
    "queue_fit",
    "queue_value",
    "risk_calibration",
    "deskew_calibration",
    "reserve_calibration",
)

_STATE_VERSION = 1

#: Version of the sufficient-statistics block a partial must carry to feed the
#: pooled quantiles.  A complete partial below this version is re-fit for that
#: partition instead of contributing a per-partition quantile the combiner
#: cannot pool.
POOLING_STATS_VERSION = SCHEMA


# ---------------------------------------------------------------------------
# Partitions
# ---------------------------------------------------------------------------
@dataclass(frozen=True, order=True)
class Partition:
    """One ``date=YYYY-MM-DD/hour=HH`` directory pair."""

    date: str
    hour: str

    @property
    def label(self) -> str:
        return f"{self.date}T{self.hour}"

    @property
    def window_label(self) -> str:
        return f"{self.date} {self.hour}"


def _table_partitions(root: Path) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    if not root.is_dir():
        return found
    for date_dir in root.glob("date=*"):
        if not date_dir.is_dir():
            continue
        date = date_dir.name.split("=", 1)[1]
        for hour_dir in date_dir.glob("hour=*"):
            if not hour_dir.is_dir():
                continue
            if any(hour_dir.glob("*.parquet")):
                found.add((date, hour_dir.name.split("=", 1)[1]))
    return found


def complete_partitions(tape_root: str | Path = TAPE_ROOT) -> list[Partition]:
    """Every date/hour with both an event part and a trade part, oldest first."""
    root = Path(tape_root)
    events = _table_partitions(root / "clob_events")
    trades = _table_partitions(root / "clob_trades")
    return [Partition(date, hour) for date, hour in sorted(events & trades)]


def parse_partition(text: str) -> Partition:
    """Accept ``2026-11-04T01`` or ``2026-11-04 01`` (and ``01:00``)."""
    cleaned = text.strip().replace(" ", "T")
    if "T" not in cleaned:
        raise ValueError(f"not a date/hour partition: {text!r}")
    date, hour = cleaned.split("T", 1)
    hour = hour.split(":", 1)[0].zfill(2)
    if len(date) != 10 or len(hour) != 2 or not hour.isdigit():
        raise ValueError(f"not a date/hour partition: {text!r}")
    return Partition(date, hour)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _empty_state() -> dict[str, Any]:
    return {
        "version": _STATE_VERSION,
        "updated_at": None,
        "cursor": 0,
        "partitions": {},
        "artifacts": {},
    }


def load_state(path: str | Path = STATE_PATH) -> dict[str, Any]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_state()
    except (OSError, ValueError) as exc:
        log.warning(f"refit state {target} unreadable ({exc}); starting a fresh cursor")
        return _empty_state()
    if not isinstance(payload, dict):
        return _empty_state()
    state = _empty_state()
    state.update(payload)
    if not isinstance(state.get("partitions"), dict):
        state["partitions"] = {}
    if not isinstance(state.get("artifacts"), dict):
        state["artifacts"] = {}
    return state


def atomic_write_json(path: str | Path, payload: Any) -> None:
    """Write ``payload`` so a reader sees either the old file or the new one."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    text = json.dumps(_finite(payload), indent=2, allow_nan=False) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


def _finite(value: Any) -> Any:
    """JSON-safe copy: non-finite floats become ``None``."""
    if isinstance(value, dict):
        return {str(key): _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        return value
    return value


# ---------------------------------------------------------------------------
# Partials
# ---------------------------------------------------------------------------
def partial_path(
    artifact: str,
    partition: Partition,
    partials_root: str | Path = PARTIALS_ROOT,
) -> Path:
    return Path(partials_root) / artifact / f"{partition.label}.json"


def _read_partial(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def partial_is_complete(path: Path) -> bool:
    """A readable object whose fitter did not run out of budget."""
    payload = _read_partial(path)
    if payload is None:
        return False
    return payload.get("partial") is not True


def _window_span(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """``(start, end)`` from either the ``start``/``start_utc`` spelling."""
    window = payload.get("window")
    if not isinstance(window, Mapping):
        return None, None
    start = window.get("start") or window.get("start_utc")
    end = window.get("end") or window.get("end_utc")
    return (
        str(start) if start not in (None, "") else None,
        str(end) if end not in (None, "") else None,
    )


def _sample_value(payload: Mapping[str, Any], artifact: str) -> float | None:
    """The partial's own sample on the weighting path, or ``None``."""
    current: Any = payload
    for key in ARTIFACTS[artifact]:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return _numeric(current)


def partial_problem(payload: Mapping[str, Any], artifact: str) -> str | None:
    """Why a partial cannot enter the combination, or ``None`` when it can.

    A partial is only a measurement when the fitter finished (``partial`` is
    false), it recorded the window it measured, and it reports a positive
    sample on the artifact's own weighting path.
    """
    if payload.get("error") and payload.get("artifact"):
        reason = str(payload["error"]).strip().splitlines()
        tail = reason[-1][:300] if reason else "unknown error"
        return f"failed fit: {tail}"
    if payload.get("partial") is True:
        return "fitter reported partial=true (budget exhausted)"
    start, end = _window_span(payload)
    if start is None or end is None:
        return "window is missing a measured start/end"
    sample = _sample_value(payload, artifact)
    if sample is None or sample <= 0.0:
        return f"sample_size.{'.'.join(ARTIFACTS[artifact])} is missing or not positive"
    return None


_DESKEW_HISTOGRAMS = (
    "holding_seconds",
    "expiry_fraction",
    "print_rates_per_min",
    "abs_ofi",
    "abs_drift_cents",
)


def _has_pooling_stats(payload: Mapping[str, Any], artifact: str) -> bool:
    """Whether a complete partial carries the raw statistics its pool needs.

    Only the three artifacts whose pooled fields are quantiles (or pooled
    counts) are checked: ``queue_fit`` pools ratio-of-sums totals that predate
    this schema, and ``queue_value`` is a replay table.
    """
    if artifact == "queue_fit":
        touch = _path_get(payload, ("fields", "trade_arrival_rate_shares_sec"))
        hurdle = _path_get(payload, ("fields", "annual_capital_hurdle_rate"))
        accrual = _path_get(payload, ("fields", "reward_accrual_usd_per_share_sec"))
        return (
            isinstance(touch, Mapping)
            and _numeric(touch.get("touch_shares")) is not None
            and _numeric(touch.get("active_asset_seconds")) not in (None, 0.0)
            and isinstance(hurdle, Mapping)
            and isinstance(hurdle.get("market_yields"), list)
            and bool(hurdle["market_yields"])
            and isinstance(accrual, Mapping)
            and isinstance(accrual.get("values"), list)
            and bool(accrual["values"])
        )
    if artifact not in (
        "risk_calibration",
        "deskew_calibration",
        "reserve_calibration",
    ):
        return True
    block = payload.get("sufficient_statistics")
    if not isinstance(block, Mapping):
        return False
    if int(block.get("schema") or 0) < POOLING_STATS_VERSION:
        return False
    if artifact == "risk_calibration":
        if not is_histogram(block.get("ofi_abs")):
            return False
        rows = _path_get(payload, ("adverse_streak", "run_length"))
        return isinstance(rows, list) and bool(rows) and all(
            isinstance(row, Mapping) and "sum_fill_net" in row for row in rows
        )
    if artifact == "deskew_calibration":
        if not all(is_histogram(block.get(name)) for name in _DESKEW_HISTOGRAMS):
            return False
        skew = block.get("skew")
        gas = block.get("merge_gas")
        return (
            isinstance(skew, Mapping)
            and isinstance(skew.get("grid"), list)
            and bool(skew["grid"])
            and isinstance(gas, Mapping)
            and _numeric(gas.get("denominator")) not in (None, 0.0)
        )
    if not is_histogram(block.get("jump_abs")):
        return False
    actions = block.get("merge_actions")
    return isinstance(actions, Mapping) and isinstance(actions.get("markets"), Mapping)


def _write_failure_marker(
    path: Path,
    artifact: str,
    partition: Partition,
    *,
    detail: str,
    command: Sequence[str] | None,
    timed_out: bool,
) -> None:
    """Leave a ``partial: true`` diagnostic when a fit produced no file.

    An existing readable partial is never overwritten: it is either a complete
    measurement (which a retry failed to replace) or the fitter's own partial
    with its own diagnostics.  This marker exists so a killed subprocess still
    leaves the operator the command, the elapsed time and the error.
    """
    if _read_partial(path) is not None:
        return
    atomic_write_json(
        path,
        {
            "generated_at": _now_iso(),
            "artifact": artifact,
            "partition": partition.label,
            "window": {"start": None, "end": None},
            "partial": True,
            "timed_out": bool(timed_out),
            "error": detail[:4000],
            "command": list(command) if command else None,
        },
    )


def _is_resumable_partial(path: Path) -> bool:
    """A fitter-written ``partial: true`` that can resume next invocation.

    The fitters write their own partial (with a ``stage`` and cached finished
    hours) when they stop inside a budget.  The block's failure marker also has
    ``partial: true`` but carries ``artifact``/``error``/``timed_out``: that is
    a hard failure, not resumable work.
    """
    payload = _read_partial(path)
    return (
        payload is not None
        and payload.get("partial") is True
        and "artifact" not in payload
    )


def _status(state: Mapping[str, Any], partition: Partition, artifact: str) -> str | None:
    entry = state.get("partitions", {}).get(partition.label, {})
    if not isinstance(entry, dict):
        return None
    item = entry.get(artifact)
    if not isinstance(item, dict):
        return None
    status = item.get("status")
    return str(status) if status is not None else None


def _pending(
    state: Mapping[str, Any],
    partition: Partition,
    artifact: str,
    partials_root: Path,
    *,
    retry_failed: bool,
) -> bool:
    status = _status(state, partition, artifact)
    if status in ("failed", "blocked") and not retry_failed:
        return False
    if (
        artifact == "queue_value"
        and not retry_failed
        # Produced by the queue fitter in the same invocation; never schedule
        # it on its own while its producer is recorded as failed.
        and _status(state, partition, "queue_fit") == "failed"
    ):
        return False
    path = partial_path(artifact, partition, partials_root)
    if not partial_is_complete(path):
        return True
    # A complete partial from an older schema carries per-partition quantiles,
    # which cannot be pooled: re-fit that partition rather than carrying them.
    payload = _read_partial(path)
    return payload is not None and not _has_pooling_stats(payload, artifact)


def _mark(
    state: dict[str, Any],
    partition: Partition,
    artifact: str,
    status: str,
    *,
    detail: str | None = None,
    path: Path | None = None,
) -> None:
    entry = state["partitions"].setdefault(partition.label, {})
    item: dict[str, Any] = {"status": status, "at": _now_iso()}
    if detail:
        item["detail"] = detail[:4000]
    if path is not None:
        item["partial"] = str(path)
    entry[artifact] = item


# ---------------------------------------------------------------------------
# Fit subprocesses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FitOutput:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _terminate_tree(proc: subprocess.Popen[str]) -> None:
    """Kill a fitter and any worker processes it started.

    ``fit_queue_model`` uses a ``ProcessPoolExecutor``. Killing only the parent
    leaves those workers holding the output pipes, so a plain
    ``subprocess.run(timeout=...)`` can block long after the deadline.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except (OSError, ProcessLookupError):
        with suppress(OSError):
            proc.kill()


def _run_command(
    command: Sequence[str],
    *,
    timeout_s: float,
    cwd: Path,
) -> FitOutput:
    env = os.environ.copy()
    source = str(cwd / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source if not existing else source + os.pathsep + existing
    proc = subprocess.Popen(
        list(command),
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10.0)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return FitOutput(-1, stdout or "", stderr or "", timed_out=True)
    return FitOutput(int(proc.returncode or 0), stdout or "", stderr or "")


def _budget(timeout_s: float) -> float:
    """Leave room for interpreter start-up inside the subprocess timeout."""
    return max(10.0, float(timeout_s) - 5.0)


def _queue_command(
    python: str,
    partition: Partition,
    out_fit: Path,
    out_value: Path,
    timeout_s: float,
) -> list[str]:
    return [
        python,
        "-m",
        "polymarketbot.tools.fit_queue_model",
        "--date",
        partition.date,
        "--hours",
        partition.hour,
        "--budget-seconds",
        f"{_budget(timeout_s):.1f}",
        "--kyle-dates",
        partition.date,
        "--kyle-hours",
        partition.label,
        "--kyle-workers",
        "1",
        "--kyle-budget-seconds",
        f"{_budget(timeout_s):.1f}",
        "--out",
        str(out_fit),
        "--value-out",
        str(out_value),
    ]


def _risk_command(
    python: str,
    partition: Partition,
    out: Path,
    panel: Path,
    timeout_s: float,
    max_files: int | None,
    streak_hours: Sequence[str] | None = None,
) -> list[str]:
    command = [
        python,
        "-m",
        "polymarketbot.tools.fit_risk_calibration",
        "--hours",
        partition.label,
        "--output",
        str(out),
        "--panel",
        str(panel),
        # Stop inside the budget and write the fitter's own resumable
        # ``partial: true`` (with the finished hours cached) instead of being
        # killed at the subprocess deadline.
        "--max-seconds",
        f"{_budget(timeout_s):.1f}",
    ]
    if streak_hours:
        # The streak threshold is pooled over the same hours as the live
        # artifact, not recomputed from this one partition.  A per-partition
        # streak is thin and would disagree with the pooled policy.
        command.extend(["--streak-hours", *[str(hour) for hour in streak_hours]])
    if max_files is not None:
        command.extend(["--max-files", str(int(max_files))])
    return command


def _deskew_command(
    python: str,
    partition: Partition,
    out: Path,
    absent_risk: Path,
    timeout_s: float,
) -> list[str]:
    return [
        python,
        "-m",
        "polymarketbot.tools.fit_deskew_calibration",
        "--dates",
        partition.date,
        "--hours",
        partition.hour,
        "--out",
        str(out),
        # Force the per-partition toxicity measurement: reusing a combined risk
        # artifact here would leak a value from another window into this one.
        "--risk-calibration",
        str(absent_risk),
        "--max-seconds",
        f"{_budget(timeout_s):.1f}",
    ]


def _reserve_command(
    python: str,
    partition: Partition,
    out: Path,
    deskew_artifact: Path,
    timeout_s: float,
) -> list[str]:
    return [
        python,
        "-m",
        "polymarketbot.tools.fit_reserve",
        "--dates",
        partition.date,
        "--hours",
        partition.hour,
        "--out",
        str(out),
        "--deskew-calibration",
        str(deskew_artifact),
        "--max-seconds",
        f"{_budget(timeout_s):.1f}",
    ]


def _output_tail(result: FitOutput) -> str:
    """Both streams: fitters log to stderr and print the refusal to stdout."""
    parts: list[str] = []
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    if stderr:
        parts.append(stderr[-3000:])
    if stdout:
        parts.append("stdout:\n" + stdout[-1000:])
    return "\n".join(parts)[-4000:]


def _risk_streak_hours(
    derived_dir: str | Path,
    partitions: Sequence[Partition],
) -> list[str]:
    """The hours the live risk artifact pooled its streak over.

    The existing artifact records ``sample_size.streak_hours``; reusing that set
    makes a refit-driven partial reproduce the live ``consecutive_fills_threshold``
    instead of measuring a thin one-partition streak.  With no artifact yet,
    the first pass pools the full set of complete partitions.
    """
    artifact = _read_partial(Path(derived_dir) / "risk_calibration.json")
    if artifact is not None and artifact.get("partial") is not True:
        sample = artifact.get("sample_size")
        if isinstance(sample, Mapping):
            hours = sample.get("streak_hours")
            if isinstance(hours, list):
                cleaned = [
                    str(hour)
                    for hour in hours
                    if isinstance(hour, str) and "T" in hour
                ]
                if cleaned:
                    return cleaned
    return [partition.label for partition in partitions]


def _run_fit_for_artifact(
    artifact: str,
    partition: Partition,
    *,
    python: str,
    partials_root: Path,
    derived_dir: Path,
    timeout_s: float,
    risk_max_files: int | None,
    streak_hours: Sequence[str] | None = None,
    cwd: Path,
) -> tuple[bool, str]:
    """Run the producing fitter and report whether a complete partial exists."""
    out = partial_path(artifact, partition, partials_root)
    if artifact in _QUEUE_FAMILY:
        out_value = partial_path("queue_value", partition, partials_root)
        command = _queue_command(python, partition, out, out_value, timeout_s)
    elif artifact == "risk_calibration":
        command = _risk_command(
            python,
            partition,
            out,
            out.with_suffix(".panel.parquet"),
            timeout_s,
            risk_max_files,
            streak_hours=streak_hours,
        )
    elif artifact == "deskew_calibration":
        command = _deskew_command(
            python,
            partition,
            out,
            partials_root / "_none" / "risk_calibration.json",
            timeout_s,
        )
    elif artifact == "reserve_calibration":
        deskew = derived_dir / "deskew_calibration.json"
        if not deskew.exists():
            return False, (
                f"reserve fit needs a combined de-skew artifact at {deskew}; "
                "run --artifact deskew_calibration first"
            )
        command = _reserve_command(python, partition, out, deskew, timeout_s)
    else:  # pragma: no cover - ARTIFACTS is closed
        return False, f"unknown artifact {artifact}"

    started = time.time()
    result = _run_command(command, timeout_s=timeout_s, cwd=cwd)
    elapsed = time.time() - started
    produced_paths = (
        [partial_path(name, partition, partials_root) for name in _QUEUE_FAMILY]
        if artifact in _QUEUE_FAMILY
        else [out]
    )

    def _failed(detail: str, *, timed_out: bool = False) -> tuple[bool, str]:
        for name, path in zip(
            _QUEUE_FAMILY if artifact in _QUEUE_FAMILY else (artifact,),
            produced_paths,
        ):
            _write_failure_marker(
                path,
                name,
                partition,
                detail=detail,
                command=command,
                timed_out=timed_out,
            )
        return False, detail

    if result.timed_out:
        return _failed(f"timed out after {elapsed:.1f}s", timed_out=True)
    if result.returncode != 0:
        return _failed(f"exit {result.returncode}: {_output_tail(result)}")
    if not partial_is_complete(out):
        payload = _read_partial(out)
        if payload is not None and payload.get("partial") is True:
            return _failed("fitter reported partial=true (budget exhausted)")
        return _failed(f"fitter wrote no readable partial at {out}")
    return True, f"{elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Combination
# ---------------------------------------------------------------------------
def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _weight(payload: Mapping[str, Any], weight_path: Sequence[str]) -> float:
    current: Any = payload
    for key in weight_path:
        if not isinstance(current, Mapping):
            return 1.0
        current = current.get(key)
    number = _numeric(current)
    return number if number is not None and number > 0.0 else 1.0


def _combine_window(values: Sequence[Any]) -> dict[str, Any]:
    dicts = [value for value in values if isinstance(value, Mapping)]
    if not dicts:
        return {}
    result: dict[str, Any] = {}
    keys: list[str] = []
    for item in dicts:
        for key in item:
            if key not in keys:
                keys.append(key)
    for key in keys:
        items = [item.get(key) for item in dicts if key in item]
        if key == "partitions":
            merged: list[str] = []
            for item in items:
                if isinstance(item, list):
                    for entry in item:
                        if isinstance(entry, str) and entry not in merged:
                            merged.append(entry)
            result[key] = sorted(merged)
            continue
        strings = [item for item in items if isinstance(item, str) and item]
        if strings and ("start" in key or key in ("from", "since")):
            result[key] = min(strings)
        elif strings and ("end" in key or key in ("to", "until")):
            result[key] = max(strings)
        else:
            result[key] = next(
                (item for item in items if item is not None),
                None,
            )
    return result


def _sum_samples(values: Sequence[Any]) -> Any:
    first = next((value for value in values if value is not None), None)
    if isinstance(first, Mapping):
        result: dict[str, Any] = {}
        keys: list[str] = []
        for value in values:
            if isinstance(value, Mapping):
                for key in value:
                    if key not in keys:
                        keys.append(key)
        for key in keys:
            items = [
                value.get(key) for value in values if isinstance(value, Mapping) and key in value
            ]
            result[key] = _sum_samples(items)
        return result
    if isinstance(first, list):
        merged: list[Any] = []
        for value in values:
            if isinstance(value, list):
                for entry in value:
                    if entry not in merged:
                        merged.append(entry)
        return merged
    numbers = [number for number in (_numeric(value) for value in values) if number is not None]
    if numbers:
        total = sum(numbers)
        return int(total) if all(float(item).is_integer() for item in numbers) else total
    return first


def _merge_source(values: Sequence[Any]) -> Any:
    first = next((value for value in values if value is not None), None)
    if isinstance(first, Mapping):
        result: dict[str, Any] = {}
        keys: list[str] = []
        for value in values:
            if isinstance(value, Mapping):
                for key in value:
                    if key not in keys:
                        keys.append(key)
        for key in keys:
            items = [
                value.get(key) for value in values if isinstance(value, Mapping) and key in value
            ]
            result[key] = _merge_source(items)
        return result
    if isinstance(first, list):
        merged: list[Any] = []
        for value in values:
            if isinstance(value, list):
                for entry in value:
                    if entry not in merged:
                        merged.append(entry)
        return merged
    return first


def _combine_value(
    values: Sequence[Any],
    key: str,
    weights: Sequence[float],
) -> Any:
    present = [value for value in values if value is not None]
    if not present:
        return None
    if key == "window":
        return _combine_window(values)
    if key == "sample_size":
        return _sum_samples(values)
    if key == "partitions":
        merged: list[str] = []
        for value in values:
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, str) and entry not in merged:
                        merged.append(entry)
        return sorted(merged)
    if key == "source":
        return _merge_source(values)
    if key in ("definition", "preference"):
        return present[0]
    if key == "elapsed_s":
        numbers = [number for number in (_numeric(value) for value in values) if number is not None]
        return max(numbers) if numbers else present[0]
    if key == "budget_seconds":
        numbers = [number for number in (_numeric(value) for value in values) if number is not None]
        return max(numbers) if numbers else present[0]

    first = present[0]
    if isinstance(first, Mapping):
        result: dict[str, Any] = {}
        keys: list[str] = []
        for value in values:
            if isinstance(value, Mapping):
                for name in value:
                    if name not in keys:
                        keys.append(name)
        for name in keys:
            items = [value.get(name) if isinstance(value, Mapping) else None for value in values]
            result[name] = _combine_value(items, name, weights)
        return result
    if isinstance(first, list):
        if all(isinstance(entry, str) for value in present if isinstance(value, list) for entry in value):
            merged: list[str] = []
            for value in values:
                if isinstance(value, list):
                    for entry in value:
                        if isinstance(entry, str) and entry not in merged:
                            merged.append(entry)
            return merged
        # A diagnostic sample (e.g. queue_value["sample"]) is not poolable.
        return first
    if isinstance(first, bool):
        return first
    numbers = [number for number in (_numeric(value) for value in values) if number is not None]
    if numbers:
        if len(numbers) == 1 or all(number == numbers[0] for number in numbers):
            return numbers[0]
        total_weight = 0.0
        total = 0.0
        for value, weight in zip(values, weights):
            number = _numeric(value)
            if number is None:
                continue
            total += number * weight
            total_weight += weight
        if total_weight > 0.0:
            return total / total_weight
        return sum(numbers) / len(numbers)
    return first


def _leaf_values(
    payloads: Sequence[Mapping[str, Any]],
    *path: str,
) -> list[Any]:
    values: list[Any] = []
    for payload in payloads:
        current: Any = payload
        for key in path:
            if not isinstance(current, Mapping):
                current = None
                break
            current = current.get(key)
        values.append(current)
    return values


def _all_same(values: Sequence[Any]) -> Any:
    """The shared value, or ``None`` when any partial differs or is missing."""
    if not values or any(value is None for value in values):
        return None
    first = values[0]
    return first if all(value == first for value in values) else None


def _all_true(values: Sequence[Any]) -> bool | None:
    if not values or any(value is None for value in values):
        return None
    return all(bool(value) for value in values)


def _numeric_union(values: Sequence[Any]) -> list[float]:
    numbers: set[float] = set()
    for value in values:
        items = value if isinstance(value, list) else [value]
        for item in items:
            number = _numeric(item)
            if number is not None:
                numbers.add(number)
    return sorted(numbers)


#: Boolean leaves that describe per-partition agreement.  A pooled artifact
#: cannot take the first partial's answer: it is true only when every partial
#: agrees.
_AGREEMENT_BOOLS = {
    "at_boundary",
    "monotone_in_pull",
    "pull_at_edge",
    "widen_at_boundary",
}
_AGREEMENT_STRINGS = {"policy", "pull_curve_direction"}


def _apply_pooled_agreement(
    combined: dict[str, Any],
    payloads: Sequence[Mapping[str, Any]],
    path: tuple[str, ...] = (),
) -> None:
    """Derive pooled policy/corner/agreement leaves instead of copying first.

    ``corner`` is the shared corner only when every partial reports the same
    one; otherwise the pooled policy is interior (``corner = None``).
    ``at_corner`` follows that derivation.  ``monotone_in_pull`` and the widen
    boundary are true only when every partial agrees.  Numeric grids are
    unioned; diagnostic lists of dicts are left to the generic combiner.
    """
    for key, value in list(combined.items()):
        if key in ("definition", "preference", "window"):
            # Documentation and the measured span are not pooled policy.
            continue
        values = _leaf_values(payloads, *path, key)
        if key == "corner":
            corner = _all_same(values)
            combined[key] = corner
            if "at_corner" in combined:
                combined["at_corner"] = corner is not None
            continue
        if key == "at_corner":
            if "corner" not in combined:
                combined[key] = _all_true(values)
            continue
        if key in _AGREEMENT_BOOLS:
            combined[key] = _all_true(values)
            continue
        if key in _AGREEMENT_STRINGS:
            same = _all_same(values)
            if same is None and key == "policy":
                same = "mixed policy across partitions; see policy_by_partition"
            elif same is None:
                same = "mixed"
            combined[key] = same
            continue
        if key in ("grid", "pull_grid", "widen_grid"):
            union = _numeric_union(values)
            if union:
                combined[key] = union
            continue
        if isinstance(value, dict):
            _apply_pooled_agreement(value, payloads, path + (key,))


def _path_get(root: Any, path: Sequence[str]) -> Any:
    current = root
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _path_set(root: dict[str, Any], path: Sequence[str], value: Any) -> None:
    current: dict[str, Any] = root
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


def _numeric_sum(payloads: Sequence[Mapping[str, Any]], *path: str) -> float:
    total = 0.0
    for payload in payloads:
        number = _numeric(_path_get(payload, path))
        if number is not None:
            total += number
    return total


def _quantile_value(values: Sequence[float], quantile: float) -> float | None:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, round(quantile * (len(ordered) - 1))))
    return ordered[index]


def _read_artifact(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _carry_previous(
    combined: dict[str, Any],
    previous: Mapping[str, Any] | None,
    *,
    field: str,
    top_path: Sequence[str],
    note: str,
    pooling: dict[str, str],
    unreconciled: dict[str, str],
    mean_fallback: bool = True,
) -> Any:
    """Keep the last published value for a field the partials cannot pool.

    ``field`` names the ``fields.<name>`` metadata block (when one exists);
    ``top_path`` is the live key the loaders read.  The previous artifact is
    the only source of a value measured over the *whole* sample rather than
    derived by averaging per-partition fits.  ``mean_fallback=False`` refuses
    the weighted-mean copy the combiner made: a mean of per-partition
    quantiles is never a published value.
    """
    top = _numeric(_path_get(previous, top_path)) if previous else None
    if top is None and previous:
        top = _numeric(_path_get(previous, ("fields", field, "value")))
    if top is None and mean_fallback:
        top = _numeric(_path_get(combined, top_path))

    carried = _path_get(previous, ("fields", field)) if previous else None
    if not isinstance(carried, dict):
        carried = _path_get(combined, ("fields", field))
    if isinstance(carried, dict):
        carried = copy.deepcopy(carried)
        carried["value"] = top
        carried["unreconciled"] = True
        carried["pooling"] = note
        _path_set(combined, ("fields", field), carried)
    _path_set(combined, top_path, top)
    pooling[field] = "carried forward (unreconciled): " + note
    unreconciled[field] = note
    return top


def _pool_queue_fields(
    combined: dict[str, Any],
    payloads: Sequence[Mapping[str, Any]],
    previous: Mapping[str, Any] | None,
    pooling: dict[str, str],
    unreconciled: dict[str, str],
) -> None:
    # Cancellation intensity is a ratio of two measured totals, both additive
    # over disjoint time partitions.  Pooling the rate itself is not the same
    # number and is biased by the per-partition depth.
    cancelled = _numeric_sum(payloads, "fields", "cancellation_rate_intensity", "cancelled_shares")
    depth_seconds = _numeric_sum(payloads, "fields", "cancellation_rate_intensity", "depth_seconds")
    if depth_seconds > 0.0:
        value = cancelled / depth_seconds
        _path_set(combined, ("cancellation_rate_intensity",), value)
        _path_set(combined, ("fields", "cancellation_rate_intensity", "value"), value)
        _path_set(combined, ("fields", "cancellation_rate_intensity", "cancelled_shares"), cancelled)
        _path_set(combined, ("fields", "cancellation_rate_intensity", "depth_seconds"), depth_seconds)
        pooling["cancellation_rate_intensity"] = (
            "ratio of summed cancelled shares to summed depth-seconds"
        )

    # Realised variance is additive: sum(r^2)/sum(dt).  Each partial reports
    # its annualised value and the elapsed seconds, so the pooled annualised
    # value is the seconds-weighted root-mean-square of those values.
    squared_seconds = 0.0
    seconds = 0.0
    for payload in payloads:
        field = _path_get(payload, ("fields", "volatility_annualized"))
        value = _numeric(field.get("value")) if isinstance(field, Mapping) else None
        elapsed = _numeric(field.get("seconds")) if isinstance(field, Mapping) else None
        if value is None or elapsed is None or elapsed <= 0.0:
            continue
        squared_seconds += value * value * elapsed
        seconds += elapsed
    if seconds > 0.0:
        value = math.sqrt(squared_seconds / seconds)
        _path_set(combined, ("volatility_annualized",), value)
        _path_set(combined, ("fields", "volatility_annualized", "value"), value)
        _path_set(combined, ("fields", "volatility_annualized", "seconds"), seconds)
        pooling["volatility_annualized"] = (
            "sqrt(sum(value^2 * seconds) / sum(seconds)) over pooled returns"
        )

    # Kyle lambda is a fitted coefficient, not an averageable rate.  The
    # pooled artifact can carry the coefficient only when every contribution
    # identified it; otherwise the definition says the term is exactly zero
    # and the per-partition event-study evidence stays under ``fields``.
    diagnostics = [
        field
        for field in (_path_get(payload, ("fields", "kyle_lambda")) for payload in payloads)
        if isinstance(field, Mapping)
    ]
    identified = [field.get("identified") for field in diagnostics]
    if diagnostics and not all(value is True for value in identified):
        source = next(
            (field for field in diagnostics if field.get("identified") is not True),
            diagnostics[0],
        )
        carried = copy.deepcopy(source)
        carried["value"] = 0.0
        carried["identified"] = False
        carried["estimator"] = "rejected (lambda = 0)"
        carried["valuation_term"] = (
            "removed: lambda = 0, so the Kyle adverse-selection term is exactly "
            "zero and the queue valuation prices no impact"
        )
        carried["partitions"] = [
            str(label) for label in combined.get("partitions") or []
        ]
        carried["events"] = _numeric_sum(payloads, "fields", "kyle_lambda", "events")
        carried["markets"] = _numeric_sum(payloads, "fields", "kyle_lambda", "markets")
        combined_window = _path_get(combined, ("fields", "kyle_lambda", "window"))
        if combined_window is not None:
            carried["window"] = copy.deepcopy(combined_window)
        carried["pooling"] = (
            "top-level forced to 0.0: at least one partition rejected the "
            "signed-size slope, so the pooled identification is rejected; the "
            "specifications and rejection evidence are the first rejected "
            "partition's, not an average of coefficients"
        )
        _path_set(combined, ("kyle_lambda",), 0.0)
        _path_set(combined, ("fields", "kyle_lambda"), carried)
        pooling["kyle_lambda"] = "rejected -> exactly 0.0 (never average a rejected coefficient)"
        unreconciled.pop("kyle_lambda", None)
    elif diagnostics:
        weights = [
            _numeric(_path_get(payload, ("sample_size", "prints"))) or 1.0
            for payload in payloads
        ]
        numbers = [
            _numeric(_path_get(payload, ("kyle_lambda",))) for payload in payloads
        ]
        total = sum(weight for number, weight in zip(numbers, weights) if number is not None)
        value = (
            sum(number * weight for number, weight in zip(numbers, weights) if number is not None)
            / total
            if total > 0.0
            else None
        )
        if value is not None:
            _path_set(combined, ("kyle_lambda",), value)
            _path_set(combined, ("fields", "kyle_lambda", "value"), value)
            pooling["kyle_lambda"] = "sample-weighted mean of identified coefficients"

    # Arrival is shares/time, so only the two totals pool it; the partial's
    # EWMA value is not a total and a mean of EWMAs is not a pooled rate.
    touch_shares = 0.0
    asset_seconds = 0.0
    complete = True
    for payload in payloads:
        field = _path_get(payload, ("fields", "trade_arrival_rate_shares_sec"))
        shares = _numeric(field.get("touch_shares")) if isinstance(field, Mapping) else None
        elapsed = _numeric(field.get("active_asset_seconds")) if isinstance(field, Mapping) else None
        if shares is None or elapsed is None:
            complete = False
            break
        touch_shares += shares
        asset_seconds += elapsed
    if complete and asset_seconds > 0.0:
        value = touch_shares / asset_seconds
        _path_set(combined, ("trade_arrival_rate_shares_sec",), value)
        _path_set(combined, ("fields", "trade_arrival_rate_shares_sec", "value"), value)
        _path_set(combined, ("fields", "trade_arrival_rate_shares_sec", "touch_shares"), touch_shares)
        _path_set(
            combined,
            ("fields", "trade_arrival_rate_shares_sec", "active_asset_seconds"),
            asset_seconds,
        )
        pooling["trade_arrival_rate_shares_sec"] = (
            "ratio of summed touch shares to summed active asset-seconds"
        )
    else:
        _carry_previous(
            combined,
            previous,
            field="trade_arrival_rate_shares_sec",
            top_path=("trade_arrival_rate_shares_sec",),
            note=(
                "the partials record an EWMA rate and print counts, not total "
                "touch shares and active asset-seconds, so a mean of rates is "
                "not the pooled per-second rate"
            ),
            pooling=pooling,
            unreconciled=unreconciled,
        )

    # Capital hurdle is a p90 at one common deployable capital.  Pooling is
    # exact only when the partials carry the per-market yields and their
    # capital bases; otherwise retain the last whole-sample value.
    market_yields: list[dict[str, Any]] = []
    complete = True
    for payload in payloads:
        field = _path_get(payload, ("fields", "annual_capital_hurdle_rate"))
        entries = field.get("market_yields") if isinstance(field, Mapping) else None
        if not isinstance(entries, list):
            complete = False
            break
        market_yields.extend(entry for entry in entries if isinstance(entry, Mapping))
    if complete and market_yields:
        capitals = [
            _numeric(entry.get("capital")) for entry in market_yields
        ]
        capital = max((value for value in capitals if value is not None), default=None)
        yields: list[float] = []
        if capital and capital > 0.0:
            for entry in market_yields:
                quote = _numeric(entry.get("quote"))
                base_capital = _numeric(entry.get("capital"))
                gross = _numeric(entry.get("gross_usd_per_day"))
                share = _numeric(entry.get("share"))
                competing = _numeric(entry.get("competing_score"))
                cost = _numeric(entry.get("cost_usd_per_day")) or 0.0
                if (
                    quote is None
                    or base_capital is None
                    or base_capital <= 0.0
                    or gross is None
                    or competing is None
                    or share is None
                ):
                    continue
                if share >= 1.0:
                    gross_at = gross
                elif share <= 0.0:
                    # Outside the reward band or below the venue minimum: the
                    # measured payout is exactly zero at every capital.
                    gross_at = 0.0
                else:
                    our_score = share * competing / (1.0 - share)
                    scaled_score = our_score * capital / base_capital
                    gross_at = (gross / share) * scaled_score / (competing + scaled_score)
                scaled_cost = cost * capital / base_capital
                yields.append((gross_at - scaled_cost) / capital * 365.25)
        value = _quantile_value(yields, 0.9)
        if value is not None:
            _path_set(combined, ("annual_capital_hurdle_rate",), value)
            _path_set(combined, ("fields", "annual_capital_hurdle_rate", "value"), value)
            _path_set(
                combined,
                ("fields", "annual_capital_hurdle_rate", "common_capital_usd"),
                capital,
            )
            _path_set(
                combined,
                ("fields", "annual_capital_hurdle_rate", "distribution"),
                {
                    "p10": _quantile_value(yields, 0.1),
                    "p50": _quantile_value(yields, 0.5),
                    "p90": value,
                    "mean": sum(yields) / len(yields),
                    "min": min(yields),
                    "max": max(yields),
                    "count": len(yields),
                },
            )
            _path_set(
                combined,
                ("fields", "annual_capital_hurdle_rate", "sample_size"),
                len(yields),
            )
            _path_set(
                combined,
                ("fields", "annual_capital_hurdle_rate", "market_yields"),
                None,
            )
            pooling["annual_capital_hurdle_rate"] = (
                "p90 of the pooled per-market net yields recomputed at the "
                f"global common capital {capital:g}"
            )
            unreconciled.pop("annual_capital_hurdle_rate", None)
    if "annual_capital_hurdle_rate" not in pooling:
        _carry_previous(
            combined,
            previous,
            field="annual_capital_hurdle_rate",
            top_path=("annual_capital_hurdle_rate",),
            note=(
                "a pooled p90 needs the per-market net-yield distribution at "
                "one common capital; the partials carry only their own p90 at "
                "their own capital"
            ),
            pooling=pooling,
            unreconciled=unreconciled,
        )

    # Reward accrual is a median over measured trials.  Summary quantiles
    # cannot be pooled into a median; use the raw values when a partial has
    # them, otherwise retain the last whole-sample value.
    accrual_values: list[float] = []
    complete = True
    for payload in payloads:
        field = _path_get(payload, ("fields", "reward_accrual_usd_per_share_sec"))
        raw = field.get("values") if isinstance(field, Mapping) else None
        if not isinstance(raw, list):
            complete = False
            break
        accrual_values.extend(
            value for value in (_numeric(item) for item in raw) if value is not None
        )
    if complete and accrual_values:
        value = statistics.median(accrual_values)
        _path_set(combined, ("reward_accrual_usd_per_share_sec",), value)
        _path_set(combined, ("fields", "reward_accrual_usd_per_share_sec", "value"), value)
        pooling["reward_accrual_usd_per_share_sec"] = (
            "median of the pooled accrual-measured trial values"
        )
    else:
        _carry_previous(
            combined,
            previous,
            field="reward_accrual_usd_per_share_sec",
            top_path=("reward_accrual_usd_per_share_sec",),
            note=(
                "the definition is a median over accrual-measured trials; the "
                "partials carry summary quantiles, not the trial values"
            ),
            pooling=pooling,
            unreconciled=unreconciled,
        )


def _pooled_grid_optimum(
    payloads: Sequence[Mapping[str, Any]],
) -> tuple[float, float, float, dict[str, float]] | None:
    """Event-weighted pooled argmin (lowest regret) over the replay grid."""
    totals: dict[tuple[float, float], tuple[float, float]] = {}
    for payload in payloads:
        replay = payload.get("action_replay")
        grid = replay.get("grid") if isinstance(replay, Mapping) else None
        if not isinstance(grid, list):
            continue
        weight = _numeric(replay.get("events")) if isinstance(replay, Mapping) else None
        if weight is None or weight <= 0.0:
            weight = _numeric(
                _path_get(payload, ("fields", "pull_multiplier", "pull_minus_widen", "n"))
            )
        weight = weight if weight is not None and weight > 0.0 else 1.0
        for entry in grid:
            if not isinstance(entry, Mapping):
                continue
            pull = _numeric(entry.get("pull_multiplier"))
            widen = _numeric(entry.get("widen_offset_multiplier"))
            objective = _numeric(entry.get("objective"))
            if pull is None or widen is None or objective is None:
                continue
            key = (pull, widen)
            weighted, total = totals.get(key, (0.0, 0.0))
            totals[key] = (weighted + objective * weight, total + weight)
    if not totals:
        return None
    best = min(
        totals.items(),
        key=lambda item: item[1][0] / item[1][1] if item[1][1] > 0.0 else math.inf,
    )
    (pull, widen), (weighted, total) = best
    # Average the selected cell's measured summaries too, so the published
    # ``best_grid_point`` describes the pooled cell rather than one partition.
    summary_totals: dict[str, tuple[float, float]] = {}
    for payload in payloads:
        replay = payload.get("action_replay")
        grid = replay.get("grid") if isinstance(replay, Mapping) else None
        if not isinstance(grid, list):
            continue
        weight = _numeric(replay.get("events")) if isinstance(replay, Mapping) else None
        weight = weight if weight is not None and weight > 0.0 else 1.0
        for entry in grid:
            if not isinstance(entry, Mapping):
                continue
            if (
                _numeric(entry.get("pull_multiplier")) != pull
                or _numeric(entry.get("widen_offset_multiplier")) != widen
            ):
                continue
            for name, value in entry.items():
                if name in ("pull_multiplier", "widen_offset_multiplier", "objective"):
                    continue
                number = _numeric(value)
                if number is None:
                    continue
                weighted_sum, total_weight = summary_totals.get(name, (0.0, 0.0))
                summary_totals[name] = (
                    weighted_sum + number * weight,
                    total_weight + weight,
                )
    summary = {
        name: weighted_sum / total_weight
        for name, (weighted_sum, total_weight) in summary_totals.items()
        if total_weight > 0.0
    }
    return pull, widen, weighted / total if total > 0.0 else 0.0, summary


def _partition_histograms(
    payloads: Sequence[Mapping[str, Any]],
    labels: Sequence[str],
    key: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The partials' histograms for ``key`` and the partitions that lack one."""
    histograms: list[dict[str, Any]] = []
    missing: list[str] = []
    for payload, label in zip(payloads, labels):
        value = _path_get(payload, ("sufficient_statistics", key))
        if is_histogram(value):
            histograms.append(dict(value))
        else:
            missing.append(label)
    return histograms, missing


def _merged_or_none(
    payloads: Sequence[Mapping[str, Any]],
    labels: Sequence[str],
    key: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Merge one field's histograms, or name why the merge cannot be exact."""
    histograms, missing = _partition_histograms(payloads, labels, key)
    if missing:
        shown = ", ".join(missing[:6])
        more = "" if len(missing) <= 6 else f" (+{len(missing) - 6} more)"
        return None, (
            f"{len(missing)} of {len(labels)} partial(s) carry no sufficient "
            f"statistics for {key}: {shown}{more}; re-fit those partitions "
            "instead of carrying a per-partition quantile"
        )
    try:
        return merge_histograms(histograms), None
    except HistogramMismatch as error:
        return None, f"the {key} histograms cannot be merged: {error}"


def _write_pooled_quantile(
    combined: dict[str, Any],
    *,
    field: str,
    top_path: Sequence[str],
    summary: Mapping[str, Any],
    rule: str,
    pooling: dict[str, str],
    unreconciled: dict[str, str],
) -> None:
    """Publish a quantile read off the merged CDF with its bin resolution."""
    value = float(summary["value"])
    _path_set(combined, top_path, value)
    _path_set(combined, ("pooled_quantiles", field), dict(summary))
    block = _path_get(combined, ("fields", field))
    if isinstance(block, dict):
        block["value"] = value
        block["quantile"] = dict(summary)
        block["estimator"] = rule
    pooling[field] = rule
    unreconciled.pop(field, None)


def _mark_unpooled_quantile(
    combined: dict[str, Any],
    previous: Mapping[str, Any] | None,
    *,
    field: str,
    top_path: Sequence[str],
    reason: str,
    pooling: dict[str, str],
    unreconciled: dict[str, str],
) -> None:
    """Keep the last whole-sample value; state the exact missing statistic."""
    _carry_previous(
        combined,
        previous,
        field=field,
        top_path=top_path,
        note=reason,
        pooling=pooling,
        unreconciled=unreconciled,
        mean_fallback=False,
    )


def _pooled_streak(
    payloads: Sequence[Mapping[str, Any]], labels: Sequence[str]
) -> tuple[dict[str, Any] | None, str | None]:
    """Rebuild the run-length distribution from its additive partition sums.

    ``streak_hours`` is the same sample in every partial by construction, so
    partials that measured the same hour set contribute their statistics
    *once*; distinct hour sets are disjoint fills and add.  The threshold is
    then the fitter's own rule applied to the pooled cells.
    """
    groups: dict[tuple[str, ...], tuple[float, Mapping[str, Any]]] = {}
    missing: list[str] = []
    for payload, label in zip(payloads, labels):
        block = payload.get("sufficient_statistics")
        rows = _path_get(payload, ("adverse_streak", "run_length"))
        if not isinstance(block, Mapping) or not isinstance(rows, list) or not rows:
            missing.append(label)
            continue
        if not all(
            isinstance(row, Mapping) and "sum_fill_net" in row for row in rows
        ):
            missing.append(label)
            continue
        fills = _numeric(block.get("streak_fills")) or 0.0
        hours = block.get("streak_hours")
        key = tuple(str(hour) for hour in hours) if isinstance(hours, list) else ()
        current = groups.get(key)
        if current is None or fills > current[0]:
            groups[key] = (fills, payload)
    if missing:
        shown = ", ".join(missing[:6])
        more = "" if len(missing) <= 6 else f" (+{len(missing) - 6} more)"
        return None, (
            f"{len(missing)} of {len(labels)} partial(s) carry no additive "
            f"streak sums: {shown}{more}; re-fit those partitions"
        )
    if not groups:
        return None, "no partial carries a measured adverse-fill streak"

    cells: dict[int, dict[str, float]] = {}
    fills = 0.0
    adverse = 0.0
    min_cell = 30
    for _sample, payload in groups.values():
        block = payload["sufficient_statistics"]
        min_cell = int(_numeric(block.get("streak_min_cell")) or min_cell)
        streak = payload.get("adverse_streak")
        if isinstance(streak, Mapping):
            fills += _numeric(streak.get("fills")) or 0.0
            adverse += _numeric(streak.get("adverse_fills")) or 0.0
        for row in streak.get("run_length") or ():
            length = int(row.get("streak") or 0)
            cell = cells.setdefault(
                length,
                {"n": 0.0, "sum_net": 0.0, "sum_sq": 0.0, "sum_markout": 0.0},
            )
            cell["n"] += float(row.get("n") or 0)
            cell["sum_net"] += _numeric(row.get("sum_fill_net")) or 0.0
            cell["sum_sq"] += _numeric(row.get("sum_fill_net_sq")) or 0.0
            cell["sum_markout"] += _numeric(row.get("sum_fill_markout")) or 0.0

    from scipy import stats

    rows: list[dict[str, Any]] = []
    threshold: int | None = None
    for length in sorted(cells):
        cell = cells[length]
        n = int(cell["n"])
        if n <= 0:
            continue
        mean = cell["sum_net"] / n
        se = (
            math.sqrt(
                max(cell["sum_sq"] - n * mean * mean, 0.0) / (n - 1) / n
            )
            if n > 1
            else float("nan")
        )
        half = (
            float(stats.t.ppf(0.975, n - 1)) * se
            if n > 1 and math.isfinite(se) and se > 0.0
            else float("nan")
        )
        rows.append(
            {
                "streak": length,
                "n": n,
                "mean_fill_net": mean,
                "mean_fill_markout": cell["sum_markout"] / n,
                "se_fill_net": se,
                "ci95": [mean - half, mean + half] if math.isfinite(half) else None,
            }
        )
        distinguishable = bool(math.isfinite(half) and mean + half < 0.0)
        if (
            threshold is None
            and length >= 1
            and n >= min_cell
            and mean < 0.0
            and distinguishable
        ):
            threshold = length
    return (
        {
            "fills": int(fills),
            "adverse_share": (adverse / fills) if fills > 0 else None,
            "min_cell": min_cell,
            "run_length": rows,
            "threshold": threshold,
        },
        None,
    )


def _pool_risk_fields(
    combined: dict[str, Any],
    payloads: Sequence[Mapping[str, Any]],
    previous: Mapping[str, Any] | None,
    pooling: dict[str, str],
    unreconciled: dict[str, str],
    labels: Sequence[str] = (),
) -> None:
    def coefficients(path: Sequence[str]) -> list[float]:
        return [
            value
            for value in (_numeric(_path_get(payload, path)) for payload in payloads)
            if value is not None
        ]

    # c_depth is a logistic coefficient fitted per partition.  A pooled-panel
    # refit is not derivable from fit outputs, so the pooled coefficient is
    # the median with its spread, never the mean of coefficients.
    depths = coefficients(("ofi", "c_depth"))
    if depths:
        median = statistics.median(depths)
        _path_set(combined, ("ofi", "c_depth"), median)
        _path_set(combined, ("fields", "c_depth", "value"), median)
        _path_set(combined, ("fields", "c_depth", "median"), median)
        _path_set(
            combined,
            ("fields", "c_depth", "estimator"),
            "median of per-partition logistic fits, min/max spread recorded",
        )
        _path_set(
            combined,
            ("fields", "c_depth", "spread"),
            {
                "min": min(depths),
                "max": max(depths),
                "count": len(depths),
            },
        )
        _path_set(combined, ("ofi_model", "c_depth"), median)
        pooling["c_depth"] = (
            "median of per-partition logistic coefficients with min/max spread "
            "(a pooled-panel refit is not in the partials)"
        )

    # z_score is an empirical (1 - trigger_budget) quantile of the
    # standardised OFI.  Each partial carries the histogram of that sample, so
    # the pooled quantile is read off the merged counts: a mean of
    # per-partition quantiles is not the pooled quantile.
    budget = 0.05
    for payload in payloads:
        value = _numeric(_path_get(payload, ("fields", "z_score", "trigger_budget")))
        if value is not None:
            budget = value
            break
    merged, gap = _merged_or_none(payloads, list(labels), "ofi_abs")
    summary = quantile_summary(merged, 1.0 - budget) if merged is not None else None
    if summary is not None:
        _write_pooled_quantile(
            combined,
            field="z_score",
            top_path=("ofi", "z_score"),
            summary=summary,
            rule=(
                "empirical "
                f"{1.0 - budget:g} quantile of the merged per-partition "
                "|standardised 10-event OFI| histogram"
            ),
            pooling=pooling,
            unreconciled=unreconciled,
        )
    else:
        _mark_unpooled_quantile(
            combined,
            previous,
            field="z_score",
            top_path=("ofi", "z_score"),
            reason=gap
            or (
                "the merged standardised-OFI CDF has no count at the trigger "
                f"budget {budget:g}; the histogram's support does not cover it"
            ),
            pooling=pooling,
            unreconciled=unreconciled,
        )

    # v_ref is a geometric mean, so it pools as a weighted geometric mean of
    # the per-partition geometric means, weighted by the volume sample.
    log_sum = 0.0
    weight_sum = 0.0
    for payload in payloads:
        value = _numeric(_path_get(payload, ("ofi", "v_ref")))
        weight = _numeric(_path_get(payload, ("sample_size", "volume_windows")))
        if weight is None:
            weight = _numeric(_path_get(payload, ("sample_size", "windows")))
        if value is None or value <= 0.0 or weight is None or weight <= 0.0:
            continue
        log_sum += math.log(value) * weight
        weight_sum += weight
    if weight_sum > 0.0:
        value = math.exp(log_sum / weight_sum)
        _path_set(combined, ("ofi", "v_ref"), value)
        _path_set(combined, ("fields", "v_ref", "value"), value)
        _path_set(combined, ("ofi_model", "v_ref"), value)
        pooling["v_ref"] = "sample-weighted geometric mean of per-partition geometric means"

    betas = coefficients(("ofi", "beta_vol"))
    if betas:
        median = statistics.median(betas)
        _path_set(combined, ("ofi", "beta_vol"), median)
        _path_set(combined, ("fields", "beta_vol", "value"), median)
        _path_set(combined, ("fields", "beta_vol", "median"), median)
        _path_set(
            combined,
            ("fields", "beta_vol", "estimator"),
            "median of per-partition ratio coefficients, min/max spread recorded",
        )
        _path_set(
            combined,
            ("fields", "beta_vol", "spread"),
            {"min": min(betas), "max": max(betas), "count": len(betas)},
        )
        _path_set(combined, ("ofi_model", "beta_vol"), median)
        pooling["beta_vol"] = (
            "median of per-partition ratio coefficients with min/max spread"
        )

    optimum = _pooled_grid_optimum(payloads)
    if optimum is not None:
        pull, widen, objective, summary = optimum
        _path_set(combined, ("ofi", "pull_multiplier"), pull)
        _path_set(combined, ("ofi", "widen_offset_multiplier"), widen)
        _path_set(combined, ("fields", "pull_multiplier", "value"), pull)
        _path_set(combined, ("fields", "pull_multiplier", "measured_grid_point"), pull)
        _path_set(
            combined,
            ("fields", "pull_multiplier", "estimator"),
            "event-weighted pooled argmin of the per-partition replay grid",
        )
        _path_set(combined, ("fields", "widen_offset_multiplier", "value"), widen)
        _path_set(
            combined,
            ("fields", "widen_offset_multiplier", "measured_grid_point"),
            widen,
        )
        _path_set(
            combined,
            ("fields", "widen_offset_multiplier", "estimator"),
            "event-weighted pooled argmin of the per-partition replay grid",
        )
        for path in (
            ("fields", "pull_multiplier", "best_grid_point"),
            ("fields", "widen_offset_multiplier", "best_grid_point"),
            ("action_replay", "best_grid_point"),
        ):
            _path_set(combined, path + ("pull_multiplier",), pull)
            _path_set(combined, path + ("widen_offset_multiplier",), widen)
            _path_set(combined, path + ("objective",), objective)
            for name, value in summary.items():
                _path_set(combined, path + (name,), value)
        pooling["pull_multiplier"] = (
            "event-weighted pooled argmin (lowest replay regret) over the "
            "per-partition grid"
        )
        pooling["widen_offset_multiplier"] = (
            "event-weighted pooled argmin (lowest replay regret) over the "
            "per-partition grid"
        )
        pooled_note = (
            "pooled replay argmin: the grid cell with the lowest "
            "event-weighted mean regret; per-partition optima are not averaged"
        )
        _path_set(combined, ("fields", "pull_multiplier", "note"), pooled_note)
        _path_set(combined, ("fields", "widen_offset_multiplier", "note"), pooled_note)
    else:
        for name, path in (
            ("pull_multiplier", ("ofi", "pull_multiplier")),
            ("widen_offset_multiplier", ("ofi", "widen_offset_multiplier")),
        ):
            values = coefficients(path)
            if not values:
                continue
            median = statistics.median(values)
            _path_set(combined, path, median)
            _path_set(combined, ("fields", name, "value"), median)
            _path_set(combined, ("fields", name, "median"), median)
            pooling[name] = (
                "median of per-partition grid optima (no replay grid in the partials)"
            )

    # The streak threshold is read off the pooled run-length distribution, not
    # agreed or mediated across per-partition thresholds: the fitter's own
    # "negative with a 95 % interval excluding zero, at least min_cell fills"
    # rule is applied to the summed cells.
    streak, gap = _pooled_streak(payloads, list(labels))
    if streak is not None:
        threshold = streak.get("threshold")
        if threshold is not None:
            _path_set(combined, ("ofi", "consecutive_fills_threshold"), threshold)
            block = _path_get(combined, ("fields", "consecutive_fills_threshold"))
            if isinstance(block, dict):
                block["value"] = int(threshold)
        for name in ("fills", "adverse_share", "min_cell", "run_length", "threshold"):
            _path_set(combined, ("adverse_streak", name), streak.get(name))
        pooling["consecutive_fills_threshold"] = (
            "shortest run whose pooled conditional mean net is negative with "
            "a 95 % Student-t interval excluding zero and at least "
            f"{streak.get('min_cell')} pooled fills"
        )
        unreconciled.pop("consecutive_fills_threshold", None)
    else:
        thresholds = coefficients(("ofi", "consecutive_fills_threshold"))
        if thresholds:
            value = (
                thresholds[0]
                if all(value == thresholds[0] for value in thresholds)
                else statistics.median(thresholds)
            )
            _path_set(combined, ("ofi", "consecutive_fills_threshold"), value)
            _path_set(combined, ("fields", "consecutive_fills_threshold", "value"), value)
            _path_set(combined, ("adverse_streak", "threshold"), value)
            pooling["consecutive_fills_threshold"] = (
                "median of per-partition streak thresholds (unpoolable)"
            )
            unreconciled["consecutive_fills_threshold"] = gap or (
                "the partials carry no additive streak sums"
            )


def _pool_deskew_fields(
    combined: dict[str, Any],
    payloads: Sequence[Mapping[str, Any]],
    previous: Mapping[str, Any] | None,
    pooling: dict[str, str],
    unreconciled: dict[str, str],
    labels: Sequence[str] = (),
) -> None:
    labels = list(labels)

    # ``skew_aggressiveness`` is the argmax of net capture per placement.  The
    # partials carry the summed net and fill counts on the shared gain grid, so
    # the pooled objective at each grid point is sum(net) / sum(placements) and
    # the pooled value is that grid's argmax -- not a mean of per-partition
    # optima.
    cells: dict[float, dict[str, float]] = {}
    placements = 0.0
    missing: list[str] = []
    for payload, label in zip(payloads, labels):
        block = payload.get("sufficient_statistics")
        skew = block.get("skew") if isinstance(block, Mapping) else None
        grid = skew.get("grid") if isinstance(skew, Mapping) else None
        measured = _numeric(skew.get("placements")) if isinstance(skew, Mapping) else None
        if (
            not isinstance(grid, list)
            or not grid
            or measured is None
            or measured <= 0.0
        ):
            missing.append(label)
            continue
        placements += measured
        for entry in grid:
            if not isinstance(entry, Mapping):
                continue
            phi = _numeric(entry.get("phi"))
            net = _numeric(entry.get("net_total"))
            fills = _numeric(entry.get("fills"))
            if phi is None or net is None or fills is None:
                continue
            cell = cells.setdefault(phi, {"net": 0.0, "fills": 0.0})
            cell["net"] += net
            cell["fills"] += fills
    if cells and placements > 0.0 and not missing:
        phi, cell = max(
            cells.items(), key=lambda item: (item[1]["net"] / placements, -item[0])
        )
        _path_set(combined, ("skew_aggressiveness",), phi)
        _path_set(
            combined,
            ("diagnostics", "skew_fills_at_gain"),
            int(cell["fills"]),
        )
        _path_set(
            combined,
            ("diagnostics", "skew_pooled_objective"),
            cell["net"] / placements,
        )
        pooling["skew_aggressiveness"] = (
            "pooled argmax of summed net capture / summed placements over the "
            "merged gain grid"
        )
        unreconciled.pop("skew_aggressiveness", None)
    else:
        reason = (
            "grid argmax of net capture; a mean of per-partition optima is "
            "not the pooled optimum"
        )
        if missing:
            shown = ", ".join(missing[:6])
            more = "" if len(missing) <= 6 else f" (+{len(missing) - 6} more)"
            reason = (
                f"{len(missing)} of {len(labels)} partial(s) carry no skew-grid "
                f"totals: {shown}{more}; re-fit those partitions"
            )
        _carry_previous(
            combined,
            previous,
            field="skew_aggressiveness",
            top_path=("skew_aggressiveness",),
            note=reason,
            pooling=pooling,
            unreconciled=unreconciled,
            mean_fallback=False,
        )

    # Holding bounds: 10th/90th percentiles of the merged holding histogram,
    # clamped exactly as the fitter clamps them.
    holdings, gap = _merged_or_none(payloads, labels, "holding_seconds")
    for field, quantile, floor, rule in (
        (
            "holding_min_seconds",
            0.10,
            0.1,
            (
                "10th percentile of matched round-trip holding, from the merged "
                "per-partition holding histogram"
            ),
        ),
        (
            "holding_max_seconds",
            0.90,
            1.0,
            (
                "90th percentile of matched round-trip holding, from the merged "
                "per-partition holding histogram"
            ),
        ),
    ):
        summary = quantile_summary(holdings, quantile) if holdings else None
        if summary is not None:
            clamped = max(floor, float(summary["value"]))
            summary = {
                **summary,
                "value": clamped,
                "clamped_to": floor if clamped == floor else None,
            }
            _write_pooled_quantile(
                combined,
                field=field,
                top_path=(field,),
                summary=summary,
                rule=rule,
                pooling=pooling,
                unreconciled=unreconciled,
            )
        else:
            _mark_unpooled_quantile(
                combined,
                previous,
                field=field,
                top_path=(field,),
                reason=gap
                or "the merged holding CDF has no count at this percentile",
                pooling=pooling,
                unreconciled=unreconciled,
            )

    # Expiry fraction: 90th percentile of holding / remaining life.
    expiry, gap = _merged_or_none(payloads, labels, "expiry_fraction")
    expiry_quantile = 0.90
    for payload in payloads:
        block = payload.get("sufficient_statistics")
        value = _numeric(block.get("expiry_quantile")) if isinstance(block, Mapping) else None
        if value is not None:
            expiry_quantile = value
            break
    summary = (
        quantile_summary(expiry, expiry_quantile) if expiry is not None else None
    )
    if summary is not None:
        clamped = min(1.0, max(1e-9, float(summary["value"])))
        summary = {
            **summary,
            "value": clamped,
            "clamped_to": 1.0 if clamped == 1.0 else None,
        }
        _write_pooled_quantile(
            combined,
            field="holding_expiry_fraction",
            top_path=("holding_expiry_fraction",),
            summary=summary,
            rule=(
                f"{expiry_quantile:g} percentile of round-trip holding / "
                "remaining life, from the merged per-partition histogram"
            ),
            pooling=pooling,
            unreconciled=unreconciled,
        )
    else:
        _mark_unpooled_quantile(
            combined,
            previous,
            field="holding_expiry_fraction",
            top_path=("holding_expiry_fraction",),
            reason=gap
            or "the merged expiry-fraction CDF has no count at this percentile",
            pooling=pooling,
            unreconciled=unreconciled,
        )

    # Turnover cycles: median holding / (60 / median per-asset print rate).
    rates, rate_gap = _merged_or_none(payloads, labels, "print_rates_per_min")
    holding_median = quantile_summary(holdings, 0.5) if holdings else None
    rate_median = quantile_summary(rates, 0.5) if rates else None
    if holding_median is not None and rate_median is not None:
        rate = max(float(rate_median["value"]), 1e-9)
        value = max(1e-9, float(holding_median["value"]) * rate / 60.0)
        summary = {
            "value": value,
            "holding_median": holding_median,
            "print_rate_median_per_min": rate_median,
            "resolution_relative": math.hypot(
                float(holding_median.get("resolution_relative") or 0.0),
                float(rate_median.get("resolution_relative") or 0.0),
            ),
        }
        _write_pooled_quantile(
            combined,
            field="holding_turnover_cycles",
            top_path=("holding_turnover_cycles",),
            summary=summary,
            rule=(
                "median of the merged holding histogram / (60 / median of the "
                "merged per-asset print-rate histogram)"
            ),
            pooling=pooling,
            unreconciled=unreconciled,
        )
    else:
        _mark_unpooled_quantile(
            combined,
            previous,
            field="holding_turnover_cycles",
            top_path=("holding_turnover_cycles",),
            reason=gap or rate_gap or "the merged holding or print-rate CDF is empty",
            pooling=pooling,
            unreconciled=unreconciled,
        )

    # Toxicity tails: the (1 - alpha) quantiles of the merged |OFI| and
    # |micro-price drift| histograms.
    alpha = 0.01
    for payload in payloads:
        block = payload.get("sufficient_statistics")
        value = _numeric(block.get("loss_probability")) if isinstance(block, Mapping) else None
        if value is not None:
            alpha = value
            break
    for field, key, rule in (
        (
            "ofi_toxicity_threshold",
            "abs_ofi",
            (
                "1 - alpha quantile of |rolling 10-event CKS OFI| from the "
                "merged per-partition histogram"
            ),
        ),
        (
            "drift_toxicity_cents",
            "abs_drift_cents",
            (
                "1 - alpha quantile of |micro-price drift| in cents from the "
                "merged per-partition histogram"
            ),
        ),
    ):
        merged, gap = _merged_or_none(payloads, labels, key)
        summary = (
            quantile_summary(merged, 1.0 - alpha) if merged is not None else None
        )
        if summary is not None:
            _write_pooled_quantile(
                combined,
                field=field,
                top_path=(field,),
                summary=summary,
                rule=f"{rule} (alpha = {alpha:g})",
                pooling=pooling,
                unreconciled=unreconciled,
            )
        else:
            _mark_unpooled_quantile(
                combined,
                previous,
                field=field,
                top_path=(field,),
                reason=gap
                or "the merged toxicity CDF has no count at the tail probability",
                pooling=pooling,
                unreconciled=unreconciled,
            )

    # Merge gas: the gas paid and the batch x spread it is charged against add
    # over partitions, so the fraction is a ratio of those sums.
    gas_total = 0.0
    gas_base = 0.0
    gas_missing: list[str] = []
    for payload, label in zip(payloads, labels):
        block = payload.get("sufficient_statistics")
        gas = block.get("merge_gas") if isinstance(block, Mapping) else None
        paid = _numeric(gas.get("gas_usd")) if isinstance(gas, Mapping) else None
        base = _numeric(gas.get("denominator")) if isinstance(gas, Mapping) else None
        if paid is None or base is None or base <= 0.0:
            gas_missing.append(label)
            continue
        gas_total += paid
        gas_base += base
    if gas_base > 0.0 and not gas_missing:
        value = max(1e-9, gas_total / gas_base)
        _path_set(combined, ("merge_gas_fraction",), value)
        pooling["merge_gas_fraction"] = (
            "ratio of summed merge gas to the summed batch x spread it is "
            "charged against"
        )
        unreconciled.pop("merge_gas_fraction", None)
    else:
        reason = (
            "merge gas totals are missing from "
            f"{len(gas_missing)} partial(s); re-fit those partitions"
            if gas_missing
            else "no partial carries a merge gas denominator"
        )
        _carry_previous(
            combined,
            previous,
            field="merge_gas_fraction",
            top_path=("merge_gas_fraction",),
            note=reason,
            pooling=pooling,
            unreconciled=unreconciled,
            mean_fallback=False,
        )


def _pool_reserve_fields(
    combined: dict[str, Any],
    payloads: Sequence[Mapping[str, Any]],
    previous: Mapping[str, Any] | None,
    pooling: dict[str, str],
    unreconciled: dict[str, str],
    labels: Sequence[str] = (),
) -> None:
    labels = list(labels)

    # jump_shock_factor is a (1 - alpha) quantile of |horizon shock|, read off
    # the merged per-partition histogram rather than averaged.
    probability = 0.01
    for payload in payloads:
        block = payload.get("sufficient_statistics")
        value = _numeric(block.get("jump_probability")) if isinstance(block, Mapping) else None
        if value is None:
            preference = payload.get("preference")
            value = (
                _numeric(preference.get("jump_probability"))
                if isinstance(preference, Mapping)
                else None
            )
        if value is not None:
            probability = value
            break
    merged, gap = _merged_or_none(payloads, labels, "jump_abs")
    summary = (
        quantile_summary(merged, 1.0 - probability) if merged is not None else None
    )
    if summary is not None:
        _write_pooled_quantile(
            combined,
            field="jump_shock_factor",
            top_path=("jump_shock_factor",),
            summary=summary,
            rule=(
                f"{1.0 - probability:g} quantile of |horizon shock| from the "
                "merged per-partition histogram"
            ),
            pooling=pooling,
            unreconciled=unreconciled,
        )
    else:
        _mark_unpooled_quantile(
            combined,
            previous,
            field="jump_shock_factor",
            top_path=("jump_shock_factor",),
            reason=gap
            or "the merged |horizon shock| CDF has no count at the tail probability",
            pooling=pooling,
            unreconciled=unreconciled,
        )

    # The two gas-action fields are a sum and a median over the measured
    # markets.  The partials carry per-market crossing/hours counts, which add
    # across partitions for the same market, so the pooled expectation is one
    # action per market from the merged counts -- not a sum of per-partition
    # sums over overlapping market sets.
    markets: dict[str, list[float]] = {}
    constants: list[tuple[float, float, float]] = []
    missing: list[str] = []
    for payload, label in zip(payloads, labels):
        block = payload.get("sufficient_statistics")
        actions = block.get("merge_actions") if isinstance(block, Mapping) else None
        cells = actions.get("markets") if isinstance(actions, Mapping) else None
        horizon = _numeric(actions.get("horizon_seconds")) if isinstance(actions, Mapping) else None
        batch = _numeric(actions.get("fills_per_batch")) if isinstance(actions, Mapping) else None
        hours_total = _numeric(actions.get("hours_total")) if isinstance(actions, Mapping) else None
        if (
            not isinstance(cells, Mapping)
            or horizon is None
            or horizon <= 0.0
            or batch is None
            or batch <= 0.0
            or hours_total is None
            or hours_total <= 0.0
        ):
            missing.append(label)
            continue
        constants.append((hours_total, horizon, batch))
        for asset, cell in cells.items():
            if not isinstance(cell, Mapping):
                continue
            fills = _numeric(cell.get("fills"))
            hours = _numeric(cell.get("hours"))
            if fills is None or hours is None or hours <= 0.0:
                continue
            entry = markets.setdefault(str(asset), [0.0, 0.0])
            entry[0] += fills
            entry[1] += hours
    if markets and constants and not missing:
        weight = sum(item[0] for item in constants)
        horizon_hours = sum(item[0] * item[1] for item in constants) / weight / 3600.0
        fills_per_batch = sum(item[0] * item[2] for item in constants) / weight
        per_market = {
            asset: (fills / hours) * horizon_hours / fills_per_batch
            for asset, (fills, hours) in markets.items()
            if hours > 0.0
        }
        if per_market:
            base = float(sum(per_market.values()))
            median = float(statistics.median(per_market.values()))
            _path_set(combined, ("base_gas_actions",), base)
            _path_set(combined, ("per_market_gas_actions",), median)
            pooling["base_gas_actions"] = (
                "sum over pooled markets of (crossings / hours) x horizon / "
                "fills-per-batch, from the merged per-market counts"
            )
            pooling["per_market_gas_actions"] = (
                "median over pooled markets of (crossings / hours) x horizon / "
                "fills-per-batch, from the merged per-market counts"
            )
            unreconciled.pop("base_gas_actions", None)
            unreconciled.pop("per_market_gas_actions", None)
            _path_set(combined, ("diagnostics", "merged_markets"), len(per_market))
        else:
            missing = ["(every pooled market has zero measured hours)"]
    if "base_gas_actions" in unreconciled or missing:
        reason = (
            f"{len(missing)} of {len(labels)} partial(s) carry no per-market "
            f"merge counts: {', '.join(missing[:6])}; re-fit those partitions"
            if missing
            else "the partials carry no per-market merge counts"
        )
        for field in ("base_gas_actions", "per_market_gas_actions"):
            _carry_previous(
                combined,
                previous,
                field=field,
                top_path=(field,),
                note=reason,
                pooling=pooling,
                unreconciled=unreconciled,
                mean_fallback=False,
            )

    # The carried top-level values are the measured statistics; keep their
    # diagnostic mirrors consistent instead of leaving the weight-mean copies.
    for diagnostic, field in (
        ("jump_abs_quantile", "jump_shock_factor"),
        ("base_actions", "base_gas_actions"),
        ("per_market_actions", "per_market_gas_actions"),
    ):
        value = _numeric(combined.get(field))
        if value is not None:
            _path_set(combined, ("diagnostics", diagnostic), value)

    # Diversification is 1 - mean pairwise correlation.  The mean correlation
    # pools over disjoint measured pairs, so the pair counts are the weights.
    correlation = 0.0
    pairs = 0.0
    for payload in payloads:
        cell = _path_get(payload, ("diagnostics", "correlation"))
        if not isinstance(cell, Mapping):
            continue
        mean = _numeric(cell.get("mean_correlation"))
        count = _numeric(cell.get("pairs"))
        if mean is None or count is None or count <= 0.0:
            continue
        correlation += mean * count
        pairs += count
    if pairs > 0.0:
        mean_correlation = correlation / pairs
        value = 1.0 - mean_correlation
        _path_set(combined, ("diversification_weight",), value)
        _path_set(combined, ("diagnostics", "correlation", "mean_correlation"), mean_correlation)
        _path_set(combined, ("diagnostics", "correlation", "pairs"), pairs)
        pooling["diversification_weight"] = (
            "1 - pair-weighted mean pairwise correlation of pooled shocks"
        )


def _apply_pooled_risk_policy(
    combined: dict[str, Any], payloads: Sequence[Mapping[str, Any]]
) -> None:
    """Derive the risk gate's edge/policy leaves from the *pooled* argmin.

    ``_apply_pooled_agreement`` derives ``at_corner``/``at_boundary`` from
    agreement between partitions, which is wrong once the combiner has chosen
    one pooled grid cell: the pooled optimum at the upper pull edge is a
    corner, and a zero widen offset is the lower boundary.  The per-partition
    evidence lists are kept alongside.
    """
    pull_field = _path_get(combined, ("fields", "pull_multiplier"))
    widen_field = _path_get(combined, ("fields", "widen_offset_multiplier"))
    if not isinstance(pull_field, Mapping) or not isinstance(widen_field, Mapping):
        return
    pulls: set[float] = set()
    widens: set[float] = set()
    for payload in payloads:
        grid = _path_get(payload, ("action_replay", "grid"))
        if not isinstance(grid, list):
            continue
        for entry in grid:
            if not isinstance(entry, Mapping):
                continue
            pull = _numeric(entry.get("pull_multiplier"))
            widen = _numeric(entry.get("widen_offset_multiplier"))
            if pull is not None:
                pulls.add(pull)
            if widen is not None:
                widens.add(widen)
    pull_value = _numeric(_path_get(combined, ("ofi", "pull_multiplier")))
    widen_value = _numeric(_path_get(combined, ("ofi", "widen_offset_multiplier")))
    if (
        pull_value is None
        or widen_value is None
        or not pulls
        or not widens
    ):
        return

    at_corner = pull_value == max(pulls)
    pull_field["at_corner"] = at_corner
    pull_field["corner"] = "never_pull" if at_corner else None
    if at_corner:
        action = "MAINTAIN" if widen_value <= 0.0 else "widen by the measured offset"
        pull_field["policy"] = (
            "never pull at any tested threshold; widen offset measured "
            f"{widen_value:g} -> {action}"
        )
    else:
        pull_field["policy"] = "interior optimum: pull only on the most extreme flow"

    at_boundary = widen_value == min(widens)
    widen_field["at_boundary"] = at_boundary
    if at_boundary and widen_value <= 0.0:
        widen_field["policy"] = (
            "no widening at any tested level: maintain quotes on non-extreme toxicity"
        )
    elif not at_boundary:
        widen_field["policy"] = "interior optimum: widen by the measured offset"

    widen_enabled = widen_value > 0.0
    _path_set(combined, ("ofi", "widen_action_enabled"), widen_enabled)
    widen_field["widen_action_enabled"] = widen_enabled


def _apply_special_pooling(
    artifact: str,
    combined: dict[str, Any],
    payloads: Sequence[Mapping[str, Any]],
    previous: Mapping[str, Any] | None,
    labels: Sequence[str] = (),
) -> tuple[dict[str, str], dict[str, str]]:
    """Apply the field's own pooling rule instead of a blanket weighted mean."""
    pooling: dict[str, str] = {}
    unreconciled: dict[str, str] = {}
    if artifact == "queue_fit":
        _pool_queue_fields(combined, payloads, previous, pooling, unreconciled)
    elif artifact == "risk_calibration":
        _pool_risk_fields(
            combined, payloads, previous, pooling, unreconciled, labels
        )
    elif artifact == "deskew_calibration":
        _pool_deskew_fields(
            combined, payloads, previous, pooling, unreconciled, labels
        )
    elif artifact == "reserve_calibration":
        _pool_reserve_fields(
            combined, payloads, previous, pooling, unreconciled, labels
        )
    return pooling, unreconciled


def combine_artifact(
    artifact: str,
    *,
    partials_root: str | Path = PARTIALS_ROOT,
    derived_dir: str | Path = DERIVED_DIR,
) -> dict[str, Any] | None:
    """Reduce every complete partial into the live artifact, atomically.

    A partial only enters the reduction when its fitter finished, its window
    records a measured start/end, and its own sample is positive.  The combined
    artifact is only written when the summed sample reaches the fitter's floor
    (:data:`MIN_SAMPLES`); a single thin partition therefore cannot publish a
    fit that the fitter itself would not have accepted.
    """
    root = Path(partials_root) / artifact
    if not root.is_dir():
        return None
    partials: list[tuple[Partition, dict[str, Any]]] = []
    rejected: list[dict[str, str]] = []
    for path in sorted(root.glob("*.json")):
        try:
            partition = parse_partition(path.stem)
        except ValueError:
            continue
        payload = _read_partial(path)
        if payload is None:
            rejected.append({"partition": path.stem, "reason": "unreadable partial"})
            continue
        problem = partial_problem(payload, artifact)
        if problem is not None:
            rejected.append({"partition": partition.label, "reason": problem})
            continue
        partials.append((partition, payload))
    # Exact pooling needs each partial's sufficient statistics. A repository
    # mid-migration may hold only legacy partials (quantiles, no statistics):
    # then the field-level carry path below reports what cannot be pooled,
    # rather than producing no artifact at all. As soon as at least one
    # migrated partial exists, only migrated partials enter the reduction, so a
    # legacy quantile can never be mean-averaged with a pooled one.
    migrated = [
        item for item in partials if _has_pooling_stats(item[1], artifact)
    ]
    if migrated and len(migrated) < len(partials):
        kept = {id(item[1]) for item in migrated}
        for partition, payload in partials:
            if id(payload) not in kept:
                rejected.append(
                    {
                        "partition": partition.label,
                        "reason": "no sufficient statistics; excluded from exact pooling",
                    }
                )
        partials = migrated
    if not partials:
        if rejected:
            log.warning(
                f"not combining {artifact}: no usable partial "
                f"({len(rejected)} rejected, e.g. {rejected[0]['partition']}: "
                f"{rejected[0]['reason']})"
            )
        return None

    partials.sort(key=lambda item: item[0])
    payloads = [payload for _, payload in partials]
    labels = [partition.label for partition, _ in partials]
    weights = [_weight(payload, ARTIFACTS[artifact]) for payload in payloads]
    total_sample = sum(weights)
    minimum = MIN_SAMPLES[artifact]
    if total_sample < minimum:
        log.warning(
            f"not combining {artifact}: {len(partials)} partial(s) cover "
            f"{total_sample:g} < minimum {minimum:g} "
            f"({'.'.join(ARTIFACTS[artifact])}); the fitter's own floor is not met"
        )
        return None

    combined: dict[str, Any] = {}
    keys: list[str] = []
    for payload in payloads:
        for key in payload:
            if key not in keys:
                keys.append(key)
    for key in keys:
        if key in ("generated_at", "partial", "combination"):
            continue
        values = [payload.get(key) for payload in payloads]
        if key in ("definition", "preference"):
            combined[key] = _combine_value(values, key, weights)
        else:
            combined[key] = _combine_value(values, key, weights)

    previous = _read_artifact(Path(derived_dir) / f"{artifact}.json")
    combined["partitions"] = labels
    pooling, unreconciled = _apply_special_pooling(
        artifact, combined, payloads, previous, labels
    )
    # The merged histograms are the partials' evidence; a weighted mean of
    # their count vectors is meaningless, so the combined artifact carries the
    # per-field quantile summary (``pooled_quantiles``) instead.
    combined.pop("sufficient_statistics", None)

    _apply_pooled_agreement(combined, payloads)
    if artifact == "risk_calibration":
        _apply_pooled_risk_policy(combined, payloads)

    # A corner is not a pooled boolean: it is the boundary of the pooled gain.
    # Taking the first partial's ``corner`` would publish ``corner=true`` next
    # to a positive weighted-mean gain when the set mixes the zero corner with
    # interior fits.
    if "corner" in combined and "skew_aggressiveness" in combined:
        gain = _numeric(combined.get("skew_aggressiveness"))
        if gain is not None:
            combined["corner"] = gain == 0.0

    combined["generated_at"] = _now_iso()
    combined["partial"] = False
    combined["partitions"] = labels
    combined.setdefault("window", {})
    if isinstance(combined["window"], dict):
        combined["window"]["partitions"] = [
            partition.window_label for partition, _ in partials
        ]
    combined["combination"] = {
        "method": (
            "per-field pooling: ratio of summed totals for intensities, "
            "seconds-weighted root-mean-square for realised variance, "
            "medians for fitted coefficients, pooled argmax for replay grids, "
            "quantiles read off merged per-partition histograms with their bin "
            "resolution, and last-valid carry-forward marked unreconciled only "
            "for a statistic a partial genuinely lacks"
        ),
        "weight": ".".join(ARTIFACTS[artifact]),
        "sample": total_sample,
        "minimum_sample": minimum,
        "partials": len(partials),
        "partitions": labels,
        "rejected": rejected,
        "pooling": pooling,
        "unreconciled": unreconciled,
        "combined_at": combined["generated_at"],
        "note": (
            "windows are unioned and sample counts are summed; each numeric "
            "field uses the pooling rule recorded in ``pooling``. A field in "
            "``unreconciled`` was not recomputed because a partial carries no "
            "raw observations for that statistic; its last published value is "
            "retained and the reason is recorded."
        ),
    }
    if unreconciled:
        combined["unreconciled_fields"] = sorted(unreconciled)
    atomic_write_json(Path(derived_dir) / f"{artifact}.json", combined)
    log.info(
        f"combined {artifact}: {len(partials)} partial(s) "
        f"{labels[0]}..{labels[-1]} sample={total_sample:g} "
        f"-> {Path(derived_dir) / f'{artifact}.json'}"
    )
    return combined


# ---------------------------------------------------------------------------
# Block driver
# ---------------------------------------------------------------------------
def _select_artifacts(requested: Sequence[str] | None) -> list[str]:
    if not requested:
        return [name for name in FIT_ORDER if name in ARTIFACTS]
    selected: list[str] = []
    for name in requested:
        if name not in ARTIFACTS:
            raise ValueError(f"unknown artifact {name!r}; choose from {sorted(ARTIFACTS)}")
        if name not in selected:
            selected.append(name)
    return [name for name in FIT_ORDER if name in selected]


def _is_first_run(state: Mapping[str, Any]) -> bool:
    """A state with no partition entry has never run a fit.

    Only that first bounded run gets the 300 s cold-start ceiling; any later
    block (including one resuming a fitter-written partial) uses 60 s.
    """
    return not state.get("partitions")


def run_block(
    *,
    max_partitions: int = DEFAULT_MAX_PARTITIONS,
    artifacts: Sequence[str] | None = None,
    partition_timeout_s: float = DEFAULT_PARTITION_TIMEOUT_S,
    first_run_timeout_s: float = DEFAULT_FIRST_RUN_TIMEOUT_S,
    invocation_budget_s: float = DEFAULT_INVOCATION_BUDGET_S,
    partition_filter: Iterable[str] | None = None,
    retry_failed: bool = False,
    risk_max_files: int | None = 1,
    python: str = sys.executable,
    tape_root: str | Path = TAPE_ROOT,
    derived_dir: str | Path = DERIVED_DIR,
    partials_root: str | Path = PARTIALS_ROOT,
    state_path: str | Path = STATE_PATH,
    cwd: str | Path = _REPO_ROOT,
) -> dict[str, Any]:
    """Process at most ``max_partitions`` pending partitions (0 = all).

    A partition gets ``first_run_timeout_s`` on the first invocation after a
    fresh state (cold caches) and ``partition_timeout_s`` afterwards.  No new
    fit starts once ``invocation_budget_s`` has elapsed; the cursor is saved so
    the next invocation resumes at the deferred partition.
    """
    selected = _select_artifacts(artifacts)
    derived = Path(derived_dir)
    partials = Path(partials_root)
    state_file = Path(state_path)
    workdir = Path(cwd)
    if partials_root == PARTIALS_ROOT and derived_dir != DERIVED_DIR:
        partials = derived / "fit_partials"
    if state_path == STATE_PATH and derived_dir != DERIVED_DIR:
        state_file = derived / "refit_state.json"

    all_partitions = complete_partitions(tape_root)
    partitions = all_partitions
    if partition_filter:
        wanted = {parse_partition(item) for item in partition_filter}
        partitions = [partition for partition in partitions if partition in wanted]
    if not partitions:
        log.warning("no complete partitions to refit")
        return {"processed": [], "failed": [], "partial": [], "skipped": True}

    streak_hours = (
        _risk_streak_hours(derived, all_partitions)
        if "risk_calibration" in selected
        else None
    )

    state = load_state(state_file)
    fit_timeout_s = (
        float(first_run_timeout_s)
        if _is_first_run(state)
        else float(partition_timeout_s)
    )
    invocation_started = time.monotonic()
    cursor = int(state.get("cursor") or 0) % len(partitions)
    candidates: list[tuple[int, Partition]] = []
    for offset in range(len(partitions)):
        index = (cursor + offset) % len(partitions)
        partition = partitions[index]
        pending = [
            artifact
            for artifact in selected
            if _pending(
                state,
                partition,
                artifact,
                partials,
                retry_failed=retry_failed,
            )
        ]
        if pending:
            candidates.append((index, partition))
    if not candidates:
        log.info(
            f"refit: every selected artifact has a complete partial for "
            f"{len(partitions)} partition(s); nothing to do (cursor={cursor})"
        )
        return {"processed": [], "failed": [], "partial": [], "skipped": True}

    limit = len(candidates) if int(max_partitions) <= 0 else int(max_partitions)
    chosen = candidates[:limit]
    processed: list[str] = []
    failed: list[str] = []
    partial: list[str] = []
    last_index = cursor - 1
    budget_exhausted = False

    for index, partition in chosen:
        if budget_exhausted:
            break
        pending_all = [
            artifact
            for artifact in selected
            if _pending(
                state,
                partition,
                artifact,
                partials,
                retry_failed=retry_failed,
            )
        ]
        # One queue command writes both queue artifacts; run it once.
        ran_queue = False
        for artifact in FIT_ORDER:
            if artifact not in selected:
                continue
            if artifact not in pending_all:
                continue
            if artifact in _QUEUE_FAMILY:
                if ran_queue:
                    continue
                ran_queue = True
                producer = "queue_fit"
            else:
                producer = artifact
            elapsed = time.monotonic() - invocation_started
            if elapsed >= float(invocation_budget_s):
                budget_exhausted = True
                log.info(
                    f"refit: invocation budget {float(invocation_budget_s):.0f}s "
                    f"reached before {partition.label}:{producer}; deferring it "
                    "to the next block"
                )
                break
            if producer == "reserve_calibration":
                # Reserve prices the de-skew horizon; consume the combined
                # artifact from the partials already on disk, not a stale one.
                combine_artifact(
                    "deskew_calibration",
                    partials_root=partials,
                    derived_dir=derived,
                )
            log.info(f"refit {partition.label}: {producer}")
            # A fit may use the remaining invocation budget but never more than
            # the partition's own ceiling.
            fit_budget = fit_timeout_s
            remaining = float(invocation_budget_s) - elapsed
            if remaining < fit_budget:
                fit_budget = max(10.0, remaining)
            ok, detail = _run_fit_for_artifact(
                producer,
                partition,
                python=python,
                partials_root=partials,
                derived_dir=derived,
                timeout_s=fit_budget,
                risk_max_files=risk_max_files,
                streak_hours=streak_hours,
                cwd=workdir,
            )
            produced = (
                _QUEUE_FAMILY
                if producer == "queue_fit"
                else (producer,)
            )
            for name in produced:
                path = partial_path(name, partition, partials)
                if ok and partial_is_complete(path):
                    _mark(state, partition, name, "ok", detail=detail, path=path)
                elif name == producer:
                    status = "partial" if _is_resumable_partial(path) else "failed"
                    _mark(state, partition, name, status, detail=detail, path=path)
                elif partial_is_complete(path):
                    # The dependent output from an earlier run is still a
                    # finished measurement of this partition; keep it.
                    _mark(
                        state,
                        partition,
                        name,
                        "ok",
                        detail=f"kept the previous partial after {producer}: {detail}",
                        path=path,
                    )
                else:
                    _mark(
                        state,
                        partition,
                        name,
                        "blocked",
                        detail=f"{name} is produced with {producer}; {detail}",
                        path=path,
                    )
            if not ok:
                if _is_resumable_partial(
                    partial_path(producer, partition, partials)
                ):
                    # The fitter stopped inside its budget and cached the
                    # finished stages: the next block resumes it.
                    partial.append(f"{partition.label}:{producer}")
                    log.info(
                        f"refit {partition.label}: {producer} paused on budget; "
                        "the next block resumes it"
                    )
                else:
                    failed.append(f"{partition.label}:{producer}")
                    log.warning(f"refit {partition.label}: {producer} failed: {detail}")
            # A reserve fit needs the de-skew artifact from the same partials.
            if producer == "deskew_calibration":
                combine_artifact(
                    "deskew_calibration",
                    partials_root=partials,
                    derived_dir=derived,
                )
        if budget_exhausted:
            # Do not advance the cursor past a partition that still has work.
            break
        last_index = index
        processed.append(partition.label)
        if int(max_partitions) > 0 and len(processed) >= int(max_partitions):
            break
        # Save after every partition so a kill resumes at the next one.
        state["cursor"] = (index + 1) % len(partitions)
        state["updated_at"] = _now_iso()
        atomic_write_json(state_file, state)

    # Combine every selected artifact from whatever finished partials exist.
    # ``queue_value`` is combined independently: a missing dependent never
    # blocks ``queue_fit``, and a rejected/thin partial never publishes.
    for artifact in selected:
        combined = combine_artifact(
            artifact,
            partials_root=partials,
            derived_dir=derived,
        )
        if combined is not None:
            state["artifacts"][artifact] = {
                "partitions": combined.get("partitions"),
                "window": combined.get("window"),
                "combined_at": combined.get("generated_at"),
                "sample": combined.get("combination", {}).get("sample"),
            }
    if last_index >= 0:
        state["cursor"] = (last_index + 1) % len(partitions)
    else:
        state["cursor"] = cursor
    state["updated_at"] = _now_iso()
    atomic_write_json(state_file, state)

    log.info(
        f"refit block: {len(processed)} partition(s) processed, "
        f"{len(failed)} failed, {len(partial)} resumable, cursor={state['cursor']}"
        + (", invocation budget exhausted" if budget_exhausted else "")
    )
    return {
        "processed": processed,
        "failed": failed,
        "partial": partial,
        "skipped": False,
        "cursor": state["cursor"],
        "artifacts": selected,
        "budget_exhausted": budget_exhausted,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact",
        action="append",
        choices=sorted(ARTIFACTS),
        default=None,
        help="artifact to refit (repeatable; default: every artifact)",
    )
    parser.add_argument(
        "--max-partitions",
        type=int,
        default=DEFAULT_MAX_PARTITIONS,
        help="partitions this invocation may process (default: 1; 0 = all pending)",
    )
    parser.add_argument(
        "--partitions",
        nargs="+",
        default=None,
        help="restrict work to these date/hour partitions (YYYY-MM-DDTHH)",
    )
    parser.add_argument(
        "--partition-timeout",
        type=float,
        default=DEFAULT_PARTITION_TIMEOUT_S,
        help=(
            "seconds one fitter subprocess may run once the state is warm "
            f"(default: {DEFAULT_PARTITION_TIMEOUT_S:.0f})"
        ),
    )
    parser.add_argument(
        "--first-run-timeout",
        type=float,
        default=DEFAULT_FIRST_RUN_TIMEOUT_S,
        help=(
            "seconds one fitter may run on a fresh state, before any partial "
            f"exists (default: {DEFAULT_FIRST_RUN_TIMEOUT_S:.0f})"
        ),
    )
    parser.add_argument(
        "--invocation-budget",
        type=float,
        default=DEFAULT_INVOCATION_BUDGET_S,
        help=(
            "stop starting new fits after this many seconds; the cursor keeps "
            f"the deferred partition (default: {DEFAULT_INVOCATION_BUDGET_S:.0f})"
        ),
    )
    parser.add_argument(
        "--risk-max-files",
        type=int,
        default=1,
        help=(
            "event parts the risk fit may read per partition (default: 1). "
            "Use -1 for every part; the fitter then has no per-partition cap."
        ),
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="retry partitions recorded as failed by an earlier block",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help=(
            "delete the recorded cursor/failures before running; completed "
            "partials on disk are kept and still skipped"
        ),
    )
    parser.add_argument(
        "--recombine-only",
        action="store_true",
        help=(
            "skip fitting and re-reduce the existing partials into the live "
            "artifacts; reports every field still carried as unreconciled"
        ),
    )
    parser.add_argument(
        "--state",
        default=str(STATE_PATH),
        help=f"resumable cursor (default: {STATE_PATH})",
    )
    parser.add_argument(
        "--derived-dir",
        default=str(DERIVED_DIR),
        help=f"artifact directory (default: {DERIVED_DIR})",
    )
    parser.add_argument(
        "--tape-dir",
        default=str(TAPE_ROOT),
        help=f"tape root (default: {TAPE_ROOT})",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter used for fitter subprocesses (default: this one)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.partition_timeout <= 0.0:
        raise SystemExit("--partition-timeout must be positive")
    if args.first_run_timeout <= 0.0:
        raise SystemExit("--first-run-timeout must be positive")
    if args.invocation_budget <= 0.0:
        raise SystemExit("--invocation-budget must be positive")
    if args.reset_state:
        Path(args.state).unlink(missing_ok=True)
    if args.recombine_only:
        artifacts = args.artifact or sorted(ARTIFACTS)
        summary: dict[str, Any] = {}
        for name in artifacts:
            combined = combine_artifact(
                name,
                partials_root=Path(args.derived_dir) / "fit_partials",
                derived_dir=args.derived_dir,
            )
            if combined is None:
                summary[name] = {"written": False}
                continue
            summary[name] = {
                "written": True,
                "partial": combined.get("partial"),
                "partitions": len(combined.get("partitions") or []),
                "unreconciled": (
                    (combined.get("combination") or {}).get("unreconciled") or {}
                ),
            }
        print(json.dumps(_finite(summary), indent=2))
        return 0
    risk_max_files = None if args.risk_max_files < 0 else int(args.risk_max_files)
    result = run_block(
        max_partitions=args.max_partitions,
        artifacts=args.artifact,
        partition_timeout_s=args.partition_timeout,
        first_run_timeout_s=args.first_run_timeout,
        invocation_budget_s=args.invocation_budget,
        partition_filter=args.partitions,
        retry_failed=args.retry_failed,
        risk_max_files=risk_max_files,
        python=args.python,
        tape_root=args.tape_dir,
        derived_dir=args.derived_dir,
        partials_root=Path(args.derived_dir) / "fit_partials",
        state_path=args.state,
    )
    if result.get("skipped"):
        print("refit: nothing to do")
        return 0
    print(json.dumps(_finite(result), indent=2))
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
