"""Shared loader for the measured queue-conditioned fill model.

Calibrated by ``polymarketbot.tools.fill_calibration`` over 29 L3 asset-hours
(2,381,186 level rows -> 6,817 print observations, 2,184 fills).  The fitted
model in ``data/derived/fill_model.json`` is::

    logit P(fill) = b0 + b1 * queue_ratio_pre + b2 * log1p(pre_size_at_level)

where ``queue_ratio_pre = q_ahead / size_at_level`` on the row before a trade
print and the label is "the print removed more than the queue ahead".

The probability is print-conditional: it answers "given a trade print at this
level, how likely did it clear the queue ahead?" and is not an absolute fill
rate per unit time.  Callers rank queue positions with it; they must not read
it as an expected number of fills per epoch.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

from polymarketbot.logs import get_logger

log = get_logger("fill_model")

# src/polymarketbot/fill_model.py -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FILL_MODEL_PATH = _REPO_ROOT / "data" / "derived" / "fill_model.json"


def _stable_sigmoid(z: float) -> float:
    """Numerically stable logistic without an arbitrary argument clamp."""
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _parse_size_bound(label: str) -> float:
    """Upper bound of a measured size-bucket label (``<50``, ``250-1k``, ``>=5k``)."""
    text = label.strip()
    suffix = {"k": 1e3, "m": 1e6}

    def _number(token: str) -> float:
        token = token.strip()
        if not token:
            return float("inf")
        mult = suffix.get(token[-1].lower(), 1.0)
        return float(token[:-1] if mult != 1.0 else token) * mult

    if text.startswith(">="):
        return float("inf")
    if text.startswith("<"):
        return _number(text[1:])
    if "-" in text:
        return _number(text.split("-", 1)[1])
    raise ValueError(f"unrecognised fill-model size bucket label: {label!r}")


def _parse_ratio_bounds(label: str) -> Tuple[float, float]:
    """Inclusive bounds of a measured ratio-bucket label (``0.85-1``, ``1.0``)."""
    text = label.strip()
    if "-" in text:
        lo, hi = text.split("-", 1)
        return float(lo), float(hi)
    value = float(text)
    return value, value


@dataclass(frozen=True)
class FillModel:
    """Measured print-conditional fill probability from L3 queue telemetry."""

    intercept: float
    queue_ratio_coef: float
    log_size_coef: float
    observations: int
    fills: int
    base_rate: float
    # Measured fill-rate support across every dense by_ratio / by_size / cross-table
    # cell. Used as the extrapolation envelope instead of hand-set 0.01/0.99 caps.
    p_floor: float
    p_ceiling: float
    # (upper size bound in shares, measured P(fill)) from the by_size table.
    by_size: Tuple[Tuple[float, float], ...]
    # (ratio lo, ratio hi, measured P(fill)) from the isotonic by_ratio table.
    by_ratio: Tuple[Tuple[float, float, float], ...]

    def p_fill(self, queue_ratio: float, size_at_level: float) -> float:
        """Measured P(fill | queue ratio, displayed size at the level)."""
        ratio = min(1.0, max(0.0, float(queue_ratio)))
        size = max(0.0, float(size_at_level))
        z = self.intercept + self.queue_ratio_coef * ratio + self.log_size_coef * math.log1p(size)
        p = _stable_sigmoid(z)
        # fill_model.json by_ratio/by_size/table measured rates bound the support.
        return min(self.p_ceiling, max(self.p_floor, p))

    def p_fill_by_size(self, size_at_level: float) -> float:
        """Measured P(fill) for a level of this displayed size (by_size table)."""
        size = max(0.0, float(size_at_level))
        for upper, p in self.by_size:
            if size <= upper:
                return p
        return self.by_size[-1][1]

    def p_fill_by_ratio(self, queue_ratio: float) -> float:
        """Measured ratio-bucket P(fill) prior, unconditional on level size.

        Use this when only the queue ratio is known; it never extrapolates the
        logistic at an assumed size. Values come from the isotonic by_ratio fit
        (falling back to the raw by_ratio cells) in data/derived/fill_model.json.
        """
        ratio = min(1.0, max(0.0, float(queue_ratio)))
        for lo, hi, p in self.by_ratio:
            if lo <= ratio <= hi:
                return p
        return self.by_ratio[-1][2]


def load_fill_model(path: Optional[Path] = None) -> FillModel:
    """Load and validate ``data/derived/fill_model.json``; raise if unusable."""
    candidates = []
    if path is not None:
        candidates.append(Path(path))
    else:
        candidates.append(DEFAULT_FILL_MODEL_PATH)
        cwd_path = Path.cwd() / "data" / "derived" / "fill_model.json"
        if cwd_path != DEFAULT_FILL_MODEL_PATH:
            candidates.append(cwd_path)
    model_path = next((p for p in candidates if p.exists()), None)
    if model_path is None:
        raise FileNotFoundError(
            "Measured fill model not found; run "
            "python -m polymarketbot.tools.fill_calibration over "
            f"data/derived/l3_queue (looked for {[str(p) for p in candidates]})."
        )

    with open(model_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    if not payload.get("model_ok"):
        raise ValueError(
            f"fill model at {model_path} is not usable: {payload.get('model_reason')!r}"
        )
    logistic = payload.get("logistic") or {}
    coef = logistic.get("coef")
    if not logistic.get("ok") or not coef or len(coef) != 3:
        raise ValueError(f"fill model at {model_path} has no valid logistic coefficients")

    dense_rates = [
        float(row["p_fill"])
        for key in ("by_ratio", "by_size", "table")
        for row in (payload.get(key) or [])
        if row.get("status") == "ok" and row.get("p_fill") is not None
    ]
    if not dense_rates:
        raise ValueError(f"fill model at {model_path} has no dense measured fill-rate cells")

    labels = payload.get("size_buckets") or []
    size_rows = payload.get("by_size") or []
    by_size = tuple(
        (_parse_size_bound(str(label)), float(row["p_fill"]))
        for label, row in zip(labels, size_rows)
        if row.get("status") == "ok" and row.get("p_fill") is not None
    )
    if not by_size:
        raise ValueError(f"fill model at {model_path} has no measured by_size table")

    isotonic = payload.get("isotonic") or {}
    ratio_rows = isotonic.get("bins") or payload.get("by_ratio") or []
    by_ratio = []
    for row in ratio_rows:
        if row.get("status") != "ok":
            continue
        label = row.get("bucket")
        p = row.get("fitted_p_fill", row.get("p_fill"))
        if label is None or p is None:
            continue
        lo, hi = _parse_ratio_bounds(str(label))
        by_ratio.append((lo, hi, float(p)))
    if not by_ratio:
        raise ValueError(f"fill model at {model_path} has no measured by_ratio table")

    coverage = payload.get("coverage") or {}
    model = FillModel(
        intercept=float(coef[0]),
        queue_ratio_coef=float(coef[1]),
        log_size_coef=float(coef[2]),
        observations=int(coverage.get("observations") or logistic.get("n") or 0),
        fills=int(coverage.get("fills") or 0),
        base_rate=float(coverage.get("base_rate") or 0.0),
        p_floor=min(dense_rates),
        p_ceiling=max(dense_rates),
        by_size=by_size,
        by_ratio=tuple(by_ratio),
    )
    log.info(
        f"Loaded measured fill model {model_path} "
        f"(n={model.observations}, fills={model.fills}, "
        f"measured support [{model.p_floor:.4f}, {model.p_ceiling:.4f}])"
    )
    return model


@lru_cache(maxsize=1)
def get_fill_model() -> FillModel:
    """Process-wide cached measured fill model."""
    return load_fill_model()
