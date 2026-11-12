"""Fixed-grid histograms as the sufficient statistics for a pooled quantile.

A partition fit cannot report a pooled quantile: a quantile is a function of
the whole sample, and a mean of per-partition quantiles is not the pooled
quantile.  What *is* additive is a histogram on a common grid, so every fitter
emits its raw distribution as ``{edges, counts}`` with the bin resolution
stated next to it, and the combiner merges the counts and reads the quantile
off the merged CDF.

The grid is fixed by the field (not chosen from a partition's own range), so
every partial's histogram has the same edges and the counts add exactly.  A
partial measured on a different grid is refused rather than silently
re-binned: :func:`merge_histograms` raises :class:`HistogramMismatch`.

Layout of one histogram::

    {
      "scale": "linear" | "log",
      "lo": 0.0,            # first lower edge (log: must be > 0)
      "hi": 50.0,           # last upper edge
      "width": 0.05,        # linear only: bin width
      "per_decade": 100,    # log only: bins per decade
      "ratio": 1.0233,      # log only: upper/lower edge of one bin
      "bins": 1000,
      "counts": [0, 3, ...],# one integer per bin, lower-inclusive
      "below": 0,           # values below ``lo`` (log: > 0 but < lo)
      "above": 0,           # values at or above ``hi``
      "zeros": 0,           # log only: exact zeros, which a log bin cannot hold
      "total": 1000,
      "resolution": 0.05,   # linear: bin width
      "resolution_relative": ...,  # present on both: relative bin width
    }

The quantile is reported as the bin's geometric (log) or arithmetic (linear)
midpoint with that bin's resolution; that is the finest statement the
histogram supports.  ``histogram_quantile`` returns ``None`` when the rank
falls in the ``below``/``above`` tails, because those values were never
measured finely enough to name.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import numpy as np

#: Version of the sufficient-statistics block a partial must carry.
SCHEMA = 2

_LINEAR = "linear"
_LOG = "log"


def _finite_values(values: Iterable[float]) -> np.ndarray:
    """The finite entries as one flat ``float64`` array (``None`` dropped)."""
    if isinstance(values, np.ndarray):
        array = np.asarray(values, dtype=np.float64).reshape(-1)
    else:
        array = np.asarray(
            [value for value in values if value is not None], dtype=np.float64
        ).reshape(-1)
    return array[np.isfinite(array)]


def _bins(array: np.ndarray, index: np.ndarray, bins: int) -> list[int]:
    return np.bincount(
        np.clip(index, 0, bins - 1), minlength=bins
    ).astype(np.int64).tolist()


def linear_histogram(
    values: Iterable[float], *, lo: float, hi: float, width: float
) -> dict[str, Any]:
    """Dense histogram of ``values`` on ``[lo, hi)`` with a constant width."""
    if not hi > lo or not width > 0.0:
        raise ValueError("linear histogram needs hi > lo and width > 0")
    bins = round((hi - lo) / width)
    array = _finite_values(values)
    inside = array[(array >= lo) & (array < hi)]
    counts = _bins(inside, ((inside - lo) / width).astype(np.int64), bins)
    below = int(np.count_nonzero(array < lo))
    above = int(np.count_nonzero(array >= hi))
    total = int(array.size)
    return {
        "scale": _LINEAR,
        "lo": float(lo),
        "hi": float(hi),
        "width": float(width),
        "bins": bins,
        "counts": counts,
        "below": below,
        "above": above,
        "total": total,
        "resolution": float(width),
        "resolution_relative": float(width) / max((lo + hi) / 2.0, 1e-12),
        "resolution_kind": "bin width",
    }


def log_histogram(
    values: Iterable[float], *, lo: float, hi: float, per_decade: int
) -> dict[str, Any]:
    """Histogram of positive ``values`` on ``[lo, hi)`` with log-spaced bins."""
    if not lo > 0.0 or not hi > lo or per_decade < 1:
        raise ValueError("log histogram needs 0 < lo < hi and per_decade >= 1")
    ratio = 10.0 ** (1.0 / per_decade)
    bins = math.ceil(math.log10(hi / lo) * per_decade)
    elapsed = math.log(ratio)
    array = _finite_values(values)
    if bool((array < 0.0).any()):
        raise ValueError("log histogram cannot hold a negative value")
    positive = array[array > 0.0]
    inside = positive[(positive >= lo) & (positive < hi)]
    counts = _bins(inside, np.floor(np.log(inside / lo) / elapsed).astype(np.int64), bins)
    below = int(np.count_nonzero(positive < lo))
    above = int(np.count_nonzero(positive >= hi))
    zeros = int(np.count_nonzero(array == 0.0))
    total = int(array.size)
    return {
        "scale": _LOG,
        "lo": float(lo),
        "hi": float(hi),
        "per_decade": int(per_decade),
        "ratio": ratio,
        "bins": bins,
        "counts": counts,
        "below": below,
        "above": above,
        "zeros": zeros,
        "total": total,
        "resolution_relative": ratio - 1.0,
        "resolution_kind": "relative bin width",
    }


class HistogramMismatch(ValueError):
    """Two partials measured the same field on grids that cannot add."""


def is_histogram(value: Any) -> bool:
    """A dense histogram this module wrote, with an integer count per bin."""
    if not isinstance(value, Mapping):
        return False
    if value.get("scale") not in (_LINEAR, _LOG):
        return False
    counts = value.get("counts")
    if not isinstance(counts, list) or not counts:
        return False
    if int(value.get("bins") or 0) != len(counts):
        return False
    return all(isinstance(count, int) and count >= 0 for count in counts)


def histogram_grid(histogram: Mapping[str, Any]) -> tuple[Any, ...]:
    """The identity of a histogram's grid, for merge compatibility."""
    scale = histogram.get("scale")
    if scale == _LINEAR:
        return (_LINEAR, histogram.get("lo"), histogram.get("hi"), histogram.get("width"))
    if scale == _LOG:
        return (_LOG, histogram.get("lo"), histogram.get("hi"), histogram.get("ratio"))
    return (scale,)


def merge_histograms(histograms: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Add the counts of histograms measured on one common grid."""
    usable = [histogram for histogram in histograms if histogram]
    if not usable:
        raise HistogramMismatch("no histogram to merge")
    grid = histogram_grid(usable[0])
    bins = int(usable[0].get("bins") or 0)
    counts = [0] * bins
    below = above = zeros = total = 0
    for histogram in usable:
        if histogram_grid(histogram) != grid or int(histogram.get("bins") or 0) != bins:
            raise HistogramMismatch(
                f"histogram grids differ: {histogram_grid(histogram)} != {grid}"
            )
        for index, count in enumerate(histogram.get("counts") or ()):
            counts[index] += int(count)
        below += int(histogram.get("below") or 0)
        above += int(histogram.get("above") or 0)
        zeros += int(histogram.get("zeros") or 0)
        total += int(histogram.get("total") or 0)
    merged = {key: value for key, value in usable[0].items() if key != "counts"}
    merged.update(
        {
            "counts": counts,
            "below": below,
            "above": above,
            "total": total,
            "partials": len(usable),
        }
    )
    if grid[0] == _LOG:
        merged["zeros"] = zeros
    return merged


def _bin_bounds(histogram: Mapping[str, Any], index: int) -> tuple[float, float]:
    lo = float(histogram["lo"])
    if histogram.get("scale") == _LOG:
        ratio = float(histogram["ratio"])
        return lo * ratio**index, lo * ratio ** (index + 1)
    width = float(histogram["width"])
    return lo + width * index, lo + width * (index + 1)


def _bin_point(histogram: Mapping[str, Any], lower: float, upper: float) -> float:
    if histogram.get("scale") == _LOG:
        return math.sqrt(lower * upper)
    return (lower + upper) / 2.0


def histogram_quantile(
    histogram: Mapping[str, Any], quantile: float
) -> dict[str, Any] | None:
    """Read ``quantile`` off the merged CDF.

    Returns ``{"value", "resolution", "resolution_relative", "bin",
    "rank", "total", "cdf"}`` or ``None`` when the rank falls in a tail the
    grid did not cover finely enough to name a value.  ``cdf`` is the merged
    cumulative fraction through that bin, so the caller can report exactly
    where on the measured distribution the value sits.
    """
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be in [0, 1]")
    total = int(histogram.get("total") or 0)
    if total <= 0:
        return None
    target = quantile * total
    counts = histogram.get("counts") or ()
    scale = histogram.get("scale")
    cumulative = 0.0
    if scale == _LOG and int(histogram.get("zeros") or 0) > 0:
        zeros = int(histogram["zeros"])
        if target <= zeros:
            return {
                "value": 0.0,
                "resolution": 0.0,
                "resolution_relative": None,
                "bin": [0.0, 0.0],
                "rank": target,
                "total": total,
                "cdf": zeros / total,
            }
        cumulative += zeros
    below = int(histogram.get("below") or 0)
    if target <= cumulative + below:
        return None
    cumulative += below
    for index, count in enumerate(counts):
        if count <= 0:
            continue
        cumulative += count
        if target <= cumulative:
            lower, upper = _bin_bounds(histogram, index)
            return {
                "value": _bin_point(histogram, lower, upper),
                "resolution": (
                    upper - lower
                    if scale == _LINEAR
                    else float(histogram.get("resolution_relative") or (upper / lower - 1.0))
                ),
                "resolution_relative": (
                    (upper - lower) / max(_bin_point(histogram, lower, upper), 1e-12)
                    if scale == _LINEAR
                    else float(histogram.get("resolution_relative") or (upper / lower - 1.0))
                ),
                "bin": [lower, upper],
                "rank": target,
                "total": total,
                "cdf": cumulative / total,
            }
    return None


def quantile_summary(
    histogram: Mapping[str, Any] | None, quantile: float
) -> dict[str, Any] | None:
    """JSON-safe record of a quantile and the histogram resolution it carries."""
    if histogram is None:
        return None
    result = histogram_quantile(histogram, quantile)
    if result is None:
        return None
    result["quantile_probability"] = float(quantile)
    result["histogram_bins"] = int(histogram.get("bins") or 0)
    result["histogram_scale"] = histogram.get("scale")
    return result
