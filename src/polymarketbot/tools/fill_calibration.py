"""Calibrate maker fill probability from the synthetic L3 queue telemetry.

What is measured
----------------
`data/derived/l3_queue` replays the CLOB tape into one row per level event.
Each level carries a *shadow order* resting behind the observed queue:
``q_ahead`` is the size in front of it, ``q_behind`` the size behind it, and
``survival = 0`` means a removal consumed the whole queue ahead of it, i.e. the
shadow order's turn came.

Raw ``queue_ratio`` is the ratio *after* the event, so it is 0 on exactly the
rows where ``survival = 0``: bucketing it would be a tautology. The covariate
here is the state *before* the event, taken from the previous row at the same
``(asset, side, price)`` inside the same replay file::

    queue_ratio_pre = pre_q_ahead / pre_size_at_level

One observation is a **trade print claimed at the level** (``fill_size > 0``
and the previous row at that key is known and not flagged as a discontinuity).
Rows with a removal but no print in the match window are cancels, not fills,
and are counted separately as coverage, not as observations.

The label is ``survival == 0`` on that print row: the print removed more than
the queue ahead of the shadow order, so the shadow order was filled.

Model
-----
* An empirical table ``P(fill | queue_ratio_pre bucket x pre_size bucket)``
  with counts and Wilson 95% intervals. A cell with fewer than
  ``--min-obs`` observations is reported ``n/a``.
* Weighted isotonic (PAV) regression of ``P(fill)`` on the ratio buckets,
  constrained non-increasing (more queue ahead -> less likely to be reached).
* A hand-rolled logistic (Newton/IRLS, numpy only)

      logit P(fill) = b0 + b1 * queue_ratio_pre + b2 * log1p(pre_size)

  with Wald standard errors. ``b1 < 0`` is the monotone direction the data must
  show; if it does not, the module says so instead of shipping a model.

Output
------
``data/derived/fill_model.json`` (machine readable) and
``data/derived/fill_model.txt`` (the printed calibration table). Both carry the
window, sample size, asset-hours and coverage. Nothing is invented: a bucket or
a fit that the sample cannot support is written as ``n/a``/``null`` with counts.

Usage
-----
    python -m polymarketbot.tools.fill_calibration [--root data/derived/l3_queue]
                                                   [--out data/derived/fill_model.json]
                                                   [--min-obs 100]

Input is **not** derivable from this checkout: the producer of
``data/derived/l3_queue`` (``polymarketbot.data.l3_queue``) was deleted in commit
999ec0a, so this tool can re-fit an existing tree but cannot build one from the tape.
An empty ``--root`` is an error, not a zero-observation model.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from polymarketbot.logs import get_logger

log = get_logger("calibrate_fill_model")

DEFAULT_ROOT = Path("data/derived/l3_queue")
DEFAULT_OUT = Path("data/derived/fill_model.json")
DEFAULT_MIN_OBS = 100
Z95 = 1.959963984540054


# ------------------------------------------------------------------ bucketing
# Upper edges are exclusive; the last bucket is exactly ratio == 1.
RATIO_EDGES: tuple[tuple[str, float, float], ...] = (
    ("0-0.05", 0.0, 0.05),
    ("0.05-0.15", 0.05, 0.15),
    ("0.15-0.3", 0.15, 0.3),
    ("0.3-0.5", 0.3, 0.5),
    ("0.5-0.7", 0.5, 0.7),
    ("0.7-0.85", 0.7, 0.85),
    ("0.85-1", 0.85, 1.0),
    ("1.0", 1.0, math.inf),
)
SIZE_EDGES: tuple[tuple[str, float, float], ...] = (
    ("<50", 0.0, 50.0),
    ("50-250", 50.0, 250.0),
    ("250-1k", 250.0, 1000.0),
    ("1k-5k", 1000.0, 5000.0),
    (">=5k", 5000.0, math.inf),
)
RATIO_LABELS = tuple(label for label, _, _ in RATIO_EDGES)
SIZE_LABELS = tuple(label for label, _, _ in SIZE_EDGES)


def _bucket(value: float, edges: Sequence[tuple[str, float, float]]) -> str:
    for label, lo, hi in edges:
        if lo <= value < hi:
            return label
    return edges[-1][0]


def ratio_bucket(ratio: float) -> str:
    return _bucket(ratio, RATIO_EDGES)


def size_bucket(size: float) -> str:
    return _bucket(size, SIZE_EDGES)


# --------------------------------------------------------------- observation
# The source view is named ``l3_rows`` and must carry ``filename`` and
# ``file_row_number`` so the lag stays inside one replay file: each file is an
# independent replay with its own anchors, so a level must not lag across files.
OBSERVATION_SQL = """
WITH lagged AS (
    SELECT
        recv_ts_ns, asset_id, side, price, filename,
        size_at_level, q_ahead, q_behind, fill_size, cancel_size, survival,
        attribution, discontinuity,
        lag(q_ahead) OVER w      AS pre_ahead,
        lag(size_at_level) OVER w AS pre_size,
        lag(discontinuity) OVER w AS pre_discontinuity
    FROM l3_rows
    WINDOW w AS (
        PARTITION BY filename, asset_id, side, price
        ORDER BY recv_ts_ns, file_row_number
    )
)
SELECT recv_ts_ns, asset_id, filename, pre_ahead, pre_size,
       fill_size, cancel_size, survival, size_at_level, q_behind,
       pre_discontinuity
FROM lagged
WHERE fill_size > 0 AND attribution <> 'anchor'
"""


def prepare(con: duckdb.DuckDBPyConnection, root: Path) -> int:
    """Create the ``l3_rows`` view over every asset-hour file under ``root``."""
    files = sorted(Path(root).glob("**/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet files under {root}")
    paths = [path.as_posix().replace("'", "''") for path in files]
    listing = "[" + ", ".join(f"'{path}'" for path in paths) + "]"
    con.execute(
        "CREATE OR REPLACE VIEW l3_rows AS "
        f"SELECT * FROM read_parquet({listing}, filename = true, file_row_number = true)"
    )
    return len(paths)


def observation_rows(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Every claimed print with the level state that preceded it."""
    cursor = con.execute(OBSERVATION_SQL)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def build_observations(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Attach covariate, bucket and label; count why an observation is dropped.

    The fill label is ``survival == 0``: the removal that this print explains
    consumed more than the queue ahead of the shadow order.
    """
    kept: list[dict[str, Any]] = []
    excluded = {
        "no_previous_row": 0,
        "previous_row_discontinuity": 0,
        "empty_previous_level": 0,
    }
    for row in rows:
        pre_ahead = row.get("pre_ahead")
        pre_size = row.get("pre_size")
        if pre_ahead is None or pre_size is None:
            excluded["no_previous_row"] += 1
            continue
        if row.get("pre_discontinuity"):
            excluded["previous_row_discontinuity"] += 1
            continue
        if float(pre_size) <= 0.0:
            excluded["empty_previous_level"] += 1
            continue
        ratio = float(pre_ahead) / float(pre_size)
        # A replay can never build a queue ahead larger than the level itself;
        # clamp a rounding artefact instead of dropping the row.
        ratio = min(max(ratio, 0.0), 1.0)
        removed = float(row["fill_size"]) + float(row["cancel_size"] or 0.0)
        died = int(row["survival"]) == 0
        kept.append(
            {
                "recv_ts_ns": int(row["recv_ts_ns"]),
                "asset_id": row["asset_id"],
                "filename": row["filename"],
                "ratio": ratio,
                "pre_size": float(pre_size),
                "fill_size": float(row["fill_size"]),
                "filled": 1 if died else 0,
                # Strict penetration: the removal went past the queue ahead,
                # which is the narrower reading of "our order was reached".
                "filled_strict": 1 if died and removed > float(pre_ahead) + 1e-9 else 0,
                "ratio_bucket": ratio_bucket(ratio),
                "size_bucket": size_bucket(float(pre_size)),
            }
        )
    return kept, excluded


# ------------------------------------------------------------------ statistics
def wilson(fills: int, n: int, z: float = Z95) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = fills / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def summarise(
    observations: Sequence[dict[str, Any]],
    key: str,
    labels: Sequence[str],
    min_obs: int,
    label_key: str = "filled",
) -> list[dict[str, Any]]:
    """One row per bucket: counts, ``P(fill)`` and a Wilson interval, or n/a."""
    buckets: dict[str, list[int]] = {label: [0, 0] for label in labels}
    for obs in observations:
        cell = buckets[obs[key]]
        cell[0] += 1
        cell[1] += obs[label_key]
    out: list[dict[str, Any]] = []
    for label in labels:
        n, fills = buckets[label]
        ok = n >= min_obs
        low, high = wilson(fills, n) if ok else (None, None)
        out.append(
            {
                "bucket": label,
                "n": n,
                "fills": fills,
                "p_fill": (fills / n) if ok else None,
                "ci_low": low,
                "ci_high": high,
                "status": "ok" if ok else "n/a",
            }
        )
    return out


def cross_table(
    observations: Sequence[dict[str, Any]],
    ratio_labels: Sequence[str],
    size_labels: Sequence[str],
    min_obs: int,
) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], list[int]] = {
        (r, s): [0, 0] for r in ratio_labels for s in size_labels
    }
    for obs in observations:
        cell = counts[(obs["ratio_bucket"], obs["size_bucket"])]
        cell[0] += 1
        cell[1] += obs["filled"]
    out: list[dict[str, Any]] = []
    for r in ratio_labels:
        for s in size_labels:
            n, fills = counts[(r, s)]
            ok = n >= min_obs
            low, high = wilson(fills, n) if ok else (None, None)
            out.append(
                {
                    "ratio_bucket": r,
                    "size_bucket": s,
                    "n": n,
                    "fills": fills,
                    "p_fill": (fills / n) if ok else None,
                    "ci_low": low,
                    "ci_high": high,
                    "status": "ok" if ok else "n/a",
                }
            )
    return out


def isotonic_decreasing(
    labels: Sequence[str],
    ns: Sequence[int],
    fills: Sequence[int],
    min_n: int = 0,
) -> dict[str, Any]:
    """Weighted PAV with the constraint that P(fill) never rises with the ratio.

    Inputs follow increasing ``queue_ratio``; the fitted sequence must be
    non-increasing. A bucket with fewer than ``min_n`` observations is not
    fitted at all: it keeps its raw rate for the record and is reported
    ``n/a``, and the monotone constraint is enforced across the buckets that
    remain, in their original order.
    """
    blocks: list[dict[str, Any]] = []
    for label, n, f in zip(labels, ns, fills):
        if int(n) < min_n or int(n) == 0:
            continue
        blocks.append({"labels": [label], "n": int(n), "fills": int(f)})
        while len(blocks) >= 2:
            prev, cur = blocks[-2], blocks[-1]
            prev_rate = prev["fills"] / prev["n"] if prev["n"] else math.inf
            cur_rate = cur["fills"] / cur["n"] if cur["n"] else math.inf
            if prev_rate >= cur_rate:
                break
            merged = {
                "labels": prev["labels"] + cur["labels"],
                "n": prev["n"] + cur["n"],
                "fills": prev["fills"] + cur["fills"],
            }
            blocks[-2:] = [merged]

    fitted: list[float | None] = [None] * len(labels)
    index = {label: i for i, label in enumerate(labels)}
    block_rows: list[dict[str, Any]] = []
    for block in blocks:
        rate = block["fills"] / block["n"] if block["n"] else None
        for label in block["labels"]:
            fitted[index[label]] = rate
        block_rows.append(
            {
                "from_bucket": block["labels"][0],
                "to_bucket": block["labels"][-1],
                "n": block["n"],
                "fills": block["fills"],
                "p_fill": rate,
            }
        )

    raw = [f / n if n else None for n, f in zip(ns, fills)]
    raw_increasing_steps = 0
    previous: float | None = None
    for n, value in zip(ns, raw):
        if value is None or int(n) < min_n:
            continue
        if previous is not None and value > previous:
            raw_increasing_steps += 1
        previous = value
    return {
        "direction": "non-increasing in queue_ratio_pre",
        "bins": [
            {
                "bucket": label,
                "n": int(n),
                "fills": int(f),
                "raw_p_fill": r,
                "fitted_p_fill": fit,
                "status": "ok" if fit is not None else "n/a",
            }
            for label, n, f, r, fit in zip(labels, ns, fills, raw, fitted)
        ],
        "blocks": block_rows,
        "raw_increasing_steps": raw_increasing_steps,
    }


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -60.0, 60.0)))


def fit_logistic(
    X: np.ndarray,
    y: np.ndarray,
    *,
    max_iter: int = 100,
    tol: float = 1e-10,
) -> dict[str, Any]:
    """Newton/IRLS logistic regression, numpy only.

    Returns coefficients, Wald standard errors, the negative log-likelihood and
    whether the iteration converged. Tiny ridge keeps the Hessian invertible on
    degenerate samples; it moves a coefficient by far less than its standard
    error.
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    n, k = X.shape
    if n < k + 2 or y.min() == y.max():
        return {"ok": False, "reason": f"degenerate sample: n={n}, classes={sorted(set(y.tolist()))}"}

    beta = np.zeros(k, dtype=float)
    ridge = np.eye(k) * 1e-9
    converged = False
    iterations = 0
    for iterations in range(1, max_iter + 1):
        mu = _sigmoid(X @ beta)
        weights = np.clip(mu * (1.0 - mu), 1e-12, None)
        gradient = X.T @ (y - mu)
        hessian = X.T @ (X * weights[:, None]) + ridge
        step = np.linalg.solve(hessian, gradient)
        beta = beta + step
        if float(np.max(np.abs(step))) < tol:
            converged = True
            break

    mu = _sigmoid(X @ beta)
    weights = np.clip(mu * (1.0 - mu), 1e-12, None)
    hessian = X.T @ (X * weights[:, None]) + ridge
    cov = np.linalg.inv(hessian)
    se = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    eps = 1e-12
    nll = float(-np.sum(y * np.log(mu + eps) + (1.0 - y) * np.log(1.0 - mu + eps)))
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(se > 0, beta / se, np.nan)
    return {
        "ok": True,
        "coef": beta.tolist(),
        "se": se.tolist(),
        "z": [None if not np.isfinite(v) else float(v) for v in z],
        "nll": nll,
        "iterations": iterations,
        "converged": converged,
        "n": int(n),
        "features": ["intercept", "queue_ratio_pre", "log1p(pre_size_at_level)"],
    }


def logistic_section(
    observations: Sequence[dict[str, Any]], label_key: str = "filled"
) -> dict[str, Any]:
    if not observations:
        return {"ok": False, "reason": "no observations"}
    X = np.array(
        [
            [1.0, obs["ratio"], math.log1p(obs["pre_size"])]
            for obs in observations
        ],
        dtype=float,
    )
    y = np.array([obs[label_key] for obs in observations], dtype=float)
    fit = fit_logistic(X, y)
    if fit["ok"]:
        # The monotone constraint the model claims is on the queue ratio. The
        # size term is reported separately: a constant size column (every
        # synthetic row has the same depth) cannot have a sign.
        fit["monotone_direction_ok"] = bool(fit["coef"][1] < 0.0)
        fit["size_direction_ok"] = None if float(np.std(X[:, 2])) == 0.0 else bool(fit["coef"][2] < 0.0)
    return fit


# ------------------------------------------------------------------- report
def render(
    *,
    table: Sequence[dict[str, Any]],
    by_ratio: Sequence[dict[str, Any]],
    by_size: Sequence[dict[str, Any]],
    iso: dict[str, Any],
    logistic: dict[str, Any],
    sensitivity: dict[str, Any],
    meta: dict[str, Any],
) -> str:
    lines: list[str] = []
    add = lines.append
    add("Fill calibration from synthetic L3 queue telemetry")
    add("=" * 78)
    add(f"source            : {meta['glob']}")
    add(f"files / asset-hours: {meta['files']} files, {meta['asset_hours']} asset-hours, {meta['assets']} assets")
    add(f"window (UTC)      : {meta['window_start']} .. {meta['window_end']}")
    add(f"level rows scanned: {meta['level_rows']:,}")
    add(f"print events      : {meta['print_events']:,}")
    add(f"observations used : {meta['observations']:,} (fills {meta['fills']:,}, base rate {meta['base_rate']:.4f})")
    add(
        "excluded          : "
        + ", ".join(f"{k}={v}" for k, v in meta["excluded"].items())
    )
    add(f"min obs per cell  : {meta['min_observations']}")
    add("")
    add("Label: survival==0 on a print row = the print removed more than the queue ahead")
    add("Covariate: queue_ratio_pre = q_ahead / size_at_level on the previous row of the level")
    add("")
    add("P(fill) by queue_ratio_pre")
    add(f"{'ratio bucket':<14}{'n':>8}{'fills':>8}{'P(fill)':>10}{'95% CI':>20}  status")
    for row in by_ratio:
        ci = (
            f"[{row['ci_low']:.4f},{row['ci_high']:.4f}]" if row["p_fill"] is not None else ""
        )
        rate = f"{row['p_fill']:.4f}" if row["p_fill"] is not None else "n/a"
        add(f"{row['bucket']:<14}{row['n']:>8}{row['fills']:>8}{rate:>10}{ci:>20}  {row['status']}")
    add("")
    add("P(fill) by pre-event displayed size at the level")
    add(f"{'size bucket':<14}{'n':>8}{'fills':>8}{'P(fill)':>10}{'95% CI':>20}  status")
    for row in by_size:
        ci = (
            f"[{row['ci_low']:.4f},{row['ci_high']:.4f}]" if row["p_fill"] is not None else ""
        )
        rate = f"{row['p_fill']:.4f}" if row["p_fill"] is not None else "n/a"
        add(f"{row['bucket']:<14}{row['n']:>8}{row['fills']:>8}{rate:>10}{ci:>20}  {row['status']}")
    add("")
    add("Cross table P(fill | ratio x size): n / fills / P(fill)")
    header = "ratio\\size    " + "".join(f"{label:>18}" for label in SIZE_LABELS)
    add(header)
    cells = {(row["ratio_bucket"], row["size_bucket"]): row for row in table}
    for label in RATIO_LABELS:
        parts = []
        for size in SIZE_LABELS:
            row = cells[(label, size)]
            if row["p_fill"] is None:
                parts.append(f"{'n/a(':>11}{row['n']:>3})")
            else:
                parts.append(f"{row['n']:>6}/{row['fills']:>4} {row['p_fill']:>5.3f}")
        add(f"{label:<14}" + "".join(f"{part:>18}" for part in parts))
    add("")
    add("Isotonic fit (weighted PAV, non-increasing in queue_ratio_pre)")
    add(f"{'ratio bucket':<14}{'n':>8}{'raw':>9}{'fitted':>9}")
    for row in iso["bins"]:
        raw = f"{row['raw_p_fill']:.4f}" if row["raw_p_fill"] is not None else "n/a"
        fit = f"{row['fitted_p_fill']:.4f}" if row["fitted_p_fill"] is not None else "n/a"
        add(f"{row['bucket']:<14}{row['n']:>8}{raw:>9}{fit:>9}")
    add(f"raw increasing steps: {iso['raw_increasing_steps']} -> pooled into {len(iso['blocks'])} block(s)")
    add("")
    if logistic.get("ok"):
        add("Logistic: logit P(fill) = b0 + b1*ratio + b2*log1p(pre_size)")
        for name, coef, se, z in zip(
            logistic["features"], logistic["coef"], logistic["se"], logistic["z"]
        ):
            add(f"  {name:<26} {coef:>10.4f}  se {se:>8.4f}  z {z if z is None else round(z, 2)}")
        add(
            f"  n={logistic['n']}  nll={logistic['nll']:.2f}  "
            f"iters={logistic['iterations']}  converged={logistic['converged']}  "
            f"ratio_direction_ok={logistic['monotone_direction_ok']}  "
            f"size_direction_ok={logistic.get('size_direction_ok')}"
        )
    else:
        add(f"Logistic: n/a ({logistic.get('reason')})")
    add("")
    add("Note: ratio 1.0 means nothing rests behind the shadow order, so a print there can only")
    add("      fill by emptying the level; under strict penetration that bucket is 0 by construction.")
    add(f"Sensitivity — {sensitivity['label']}")
    add(
        f"  fills {sensitivity['fills']:,} of {meta['observations']:,}; "
        f"{sensitivity['boundary_rows']:,} boundary row(s) (level emptied exactly at the queue ahead)"
    )
    add(f"{'ratio bucket':<14}{'n':>8}{'fills':>8}{'P(fill)':>10}")
    for row in sensitivity["by_ratio"]:
        rate = f"{row['p_fill']:.4f}" if row["p_fill"] is not None else f"n/a (n={row['n']})"
        add(f"{row['bucket']:<14}{row['n']:>8}{row['fills']:>8}{rate:>10}")
    strict = sensitivity["logistic"]
    if strict.get("ok"):
        add(
            f"  logistic ratio slope {strict['coef'][1]:.4f} "
            f"(se {strict['se'][1]:.4f})  direction_ok={strict['monotone_direction_ok']}"
        )
    else:
        add(f"  logistic n/a ({strict.get('reason')})")
    add("")
    add(
        f"Verdict: {'model usable' if meta['model_ok'] else 'n/a: ' + meta['model_reason']}"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------- main
def calibrate(
    observations: Sequence[dict[str, Any]],
    *,
    min_obs: int = DEFAULT_MIN_OBS,
) -> dict[str, Any]:
    """Pure modelling step: tables, isotonic bins and the logistic fit."""
    by_ratio = summarise(observations, "ratio_bucket", RATIO_LABELS, min_obs)
    by_size = summarise(observations, "size_bucket", SIZE_LABELS, min_obs)
    table = cross_table(observations, RATIO_LABELS, SIZE_LABELS, min_obs)

    usable = [row for row in by_ratio if row["status"] == "ok"]
    iso_bins = isotonic_decreasing(
        [row["bucket"] for row in by_ratio],
        [row["n"] for row in by_ratio],
        [row["fills"] for row in by_ratio],
        min_n=min_obs,
    )
    logistic = logistic_section(observations)

    # Sensitivity: the narrower "removal went past the queue ahead" label. A
    # level that empties exactly at the queue ahead is counted as survived
    # here, so the two labels bracket the ambiguous boundary rows.
    by_ratio_strict = summarise(
        observations, "ratio_bucket", RATIO_LABELS, min_obs, label_key="filled_strict"
    )
    logistic_strict = logistic_section(observations, label_key="filled_strict")
    strict_fills = sum(obs["filled_strict"] for obs in observations)
    boundary = sum(obs["filled"] for obs in observations) - strict_fills

    total = len(observations)
    fills = sum(obs["filled"] for obs in observations)
    reasons: list[str] = []
    if total < min_obs:
        reasons.append(f"{total} observations < min_obs={min_obs}")
    if len(usable) < 2:
        reasons.append(f"only {len(usable)} ratio bucket(s) with n>={min_obs}")
    if not logistic.get("ok"):
        reasons.append(str(logistic.get("reason")))
    elif not logistic.get("monotone_direction_ok"):
        reasons.append("logistic slope on queue_ratio_pre is not negative")

    return {
        "min_observations": min_obs,
        "observations": total,
        "fills": fills,
        "base_rate": (fills / total) if total else None,
        "by_ratio": by_ratio,
        "by_size": by_size,
        "table": table,
        "isotonic": iso_bins,
        "logistic": logistic,
        "sensitivity": {
            "label": "strict penetration: survival==0 and removed > pre_q_ahead",
            "fills": strict_fills,
            "boundary_rows": boundary,
            "by_ratio": by_ratio_strict,
            "logistic": logistic_strict,
            "direction_ok": bool(logistic_strict.get("ok") and logistic_strict.get("monotone_direction_ok")),
        },
        "model_ok": not reasons,
        "model_reason": "; ".join(reasons) if reasons else None,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--min-obs", type=int, default=DEFAULT_MIN_OBS)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = datetime.now(UTC)

    files = sorted(args.root.glob("**/*.parquet"))
    if not files:
        log.error("no parquet files under {}", args.root)  # noqa: PLE1205 - loguru str.format
        return 1
    log.info("loading {} asset-hour file(s) under {}", len(files), args.root)  # noqa: PLE1205 - loguru str.format

    con = duckdb.connect()
    try:
        con.execute("SET threads = 2")
        prepare(con, args.root)
        meta_row = con.execute(
            """
            SELECT count(*) AS level_rows,
                   count(DISTINCT filename) AS files,
                   count(DISTINCT asset_id) AS assets,
                   count(DISTINCT regexp_extract(filename, 'date=([0-9-]+)', 1)
                                 || 'T' || regexp_extract(filename, 'hour=([0-9]+)', 1)) AS partitions,
                   min(recv_ts_ns) AS first_ns,
                   max(recv_ts_ns) AS last_ns
            FROM l3_rows
            """
        ).fetchone()
        level_rows, n_files, n_assets, n_partitions, first_ns, last_ns = meta_row
        raw = observation_rows(con)
    finally:
        con.close()

    observations, excluded = build_observations(raw)
    log.info(  # noqa: PLE1205 - loguru str.format
        "{} level rows, {} print events, {} usable observations",
        level_rows,
        len(raw),
        len(observations),
    )

    result = calibrate(observations, min_obs=args.min_obs)

    window_start = datetime.fromtimestamp(first_ns / 1e9, tz=UTC).isoformat()
    window_end = datetime.fromtimestamp(last_ns / 1e9, tz=UTC).isoformat()
    meta = {
        "glob": str(args.root / "**" / "*.parquet"),
        "files": int(n_files),
        "asset_hours": int(n_files),  # one replay file per asset-hour
        "partitions": int(n_partitions),
        "assets": int(n_assets),
        "window_start": window_start,
        "window_end": window_end,
        "level_rows": int(level_rows),
        "print_events": len(raw),
        "observations": result["observations"],
        "fills": result["fills"],
        "base_rate": result["base_rate"] or 0.0,
        "excluded": excluded,
        "min_observations": args.min_obs,
        "model_ok": result["model_ok"],
        "model_reason": result["model_reason"] or "insufficient sample for a model",
    }

    text = render(
        table=result["table"],
        by_ratio=result["by_ratio"],
        by_size=result["by_size"],
        iso=result["isotonic"],
        logistic=result["logistic"],
        sensitivity=result["sensitivity"],
        meta=meta,
    )
    for line in text.splitlines():
        log.info(line) if line else log.info("")

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "elapsed_s": round((datetime.now(UTC) - started).total_seconds(), 2),
        "source": {
            "glob": meta["glob"],
            "files": meta["files"],
            "asset_hours": meta["asset_hours"],
            "partitions": meta["partitions"],
            "assets": meta["assets"],
            "window_start": window_start,
            "window_end": window_end,
        },
        "definition": {
            "observation": "a trade print claimed at a level, with the previous row of that level as its pre-state",
            "covariate": "queue_ratio_pre = pre_q_ahead / pre_size_at_level",
            "size_bucket_on": "pre_size_at_level (displayed depth before the print)",
            "label": "survival == 0 on the print row (the print removed more than the queue ahead)",
            "label_boundary": "a level that empties exactly at the queue ahead is counted as a fill; sensitivity section re-prices it as a survival",
            "excluded": excluded,
            "note": "rows with a removal but no claimed print in the match window are cancels and are not observations",
        },
        "coverage": {
            "level_rows": meta["level_rows"],
            "print_events": meta["print_events"],
            "observations": result["observations"],
            "fills": result["fills"],
            "base_rate": result["base_rate"],
        },
        "min_observations": args.min_obs,
        "ratio_buckets": list(RATIO_LABELS),
        "size_buckets": list(SIZE_LABELS),
        "by_ratio": result["by_ratio"],
        "by_size": result["by_size"],
        "table": result["table"],
        "isotonic": result["isotonic"],
        "logistic": result["logistic"],
        "sensitivity": result["sensitivity"],
        "model_ok": result["model_ok"],
        "model_reason": result["model_reason"],
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    summary = args.out.with_suffix(".txt")
    summary.write_text(text + "\n", encoding="utf-8")
    log.info("wrote {} and {}", args.out, summary)  # noqa: PLE1205 - loguru str.format

    if not result["model_ok"]:
        log.warning("no usable model: {}", result["model_reason"])  # noqa: PLE1205 - loguru str.format
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
