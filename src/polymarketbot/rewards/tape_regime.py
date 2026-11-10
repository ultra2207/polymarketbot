"""Per-market trading regime measured from the recorded CLOB trade tape.

Everything this module returns is counted on ``data/tape/clob_trades`` - the
venue's own prints - over a window that is stated in the result. Nothing is
read from a category table, and no value has a default: a market whose prints
cannot identify a statistic is **absent** from the returned mapping, so the
caller refuses it instead of pricing a fabricated regime.

Window
------
The window is the trailing ``TAPE_WINDOW_HOURS`` (24 h) of the tape, anchored
on the newest ``recv_ts_ns`` actually present - not on wall-clock now, because
a tape that stops writing must not silently start measuring an empty window.
Both bounds are returned in every row and logged, so a report can state exactly
what it measured.

Statistics
----------
``trades`` / ``trades_per_min``
    Print count inside the window, and that count per window minute. The rate
    is divided by the *window*, never by the market's own first-to-last span,
    so a market that printed twice inside a 30-second burst is not credited
    with four prints a minute for a day.
``mean_trade_size_shares`` / ``size_cv``
    Mean print size in outcome shares and its coefficient of variation. This is
    the quantity that caps how many shares one fill can take, so it is the one
    statistic the caller is required to have.
``realised_vol_bps_per_min``
    Realised variance rate of the market's **one-minute last-price series**:
    ``sqrt(sum(r_i^2) / sum(dt_i)) * 1e4`` in bps per minute, ``r_i`` the log
    return between consecutive non-empty minutes and ``dt_i`` their gap. The
    one-minute bucket is the unit the number is quoted in - the same unit the
    deleted category table claimed - and bucketing first is what keeps
    sub-second bid-ask bounce out of a "per-minute" statistic. ``None`` when the
    market's prints do not span two non-empty minutes: the estimate does not
    exist, and it is never reported as zero.
``jump_freq``
    Fraction of prints that moved the last price by more than the market's
    **published** reward band (``rewards_max_spread`` cents) - the distance at
    which a resting reward quote stops being scored and the position is simply
    on the wrong side. The threshold is the venue's own published number, not a
    multiplier chosen here.

Sample adequacy
---------------
``required_sample`` is computed from the market's *own* measured dispersion:
``ceil((Z * size_cv / REL_HALF_WIDTH) ** 2)`` - the print count at which the
mean print size carries a 95 % relative half-width of ``REL_HALF_WIDTH``. A
market that has not printed that often cannot identify its own mean print size,
so it is refused with that reason rather than summarised on a handful of
prints. Two prints (``MIN_PRINTS_FOR_DISPERSION``) is the absolute floor: below
it no dispersion exists at all.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import duckdb

from polymarketbot.logs import get_logger

logger = get_logger("rewards_tape_regime")

DEFAULT_TAPE_DIR = Path("data/tape")
TRADES_TABLE = "clob_trades"
# Stated measurement window: the trailing 24 h of recorded prints. 24 h spans a
# full session (overnight plus the US close) so a rate is not one hour's burst,
# and it is short enough that a market whose flow died is not kept alive by
# prints from a week ago.
TAPE_WINDOW_HOURS: float = 24.0
# Two prints are the minimum that can show *any* price change, so no dispersion
# (volatility, size CV) can exist below this count.
MIN_PRINTS_FOR_DISPERSION = 2
# Precision target, not an economic assumption: the mean print size caps the
# shares one fill can take, so the estimate must be within a factor of two at
# 95 % confidence. Z is the normal 95 % two-sided quantile.
SIZE_PRECISION_Z = 1.959963984540054
SIZE_REL_HALF_WIDTH = 0.5

NS_PER_HOUR = 3_600_000_000_000
NS_PER_MINUTE = 60_000_000_000


@dataclass(frozen=True)
class MarketRegime:
    """One market's tape regime: measured, window-stamped, self-describing."""

    condition_id: str
    window_start_ns: int
    window_end_ns: int
    trades: int
    trades_per_min: float
    mean_trade_size_shares: float
    size_cv: float
    realised_vol_bps_per_min: Optional[float]
    jump_freq: float
    span_minutes: float
    required_sample: int
    jump_band_cents: float

    @property
    def window_hours(self) -> float:
        return (self.window_end_ns - self.window_start_ns) / NS_PER_HOUR

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "window_start_ns": self.window_start_ns,
            "window_end_ns": self.window_end_ns,
            "window_hours": round(self.window_hours, 3),
            "trades": self.trades,
            "trades_per_min": round(self.trades_per_min, 6),
            "mean_trade_size_shares": round(self.mean_trade_size_shares, 4),
            "size_cv": round(self.size_cv, 4),
            "realised_vol_bps_per_min": (
                round(self.realised_vol_bps_per_min, 4)
                if self.realised_vol_bps_per_min is not None
                else None
            ),
            "jump_freq": round(self.jump_freq, 6),
            "span_minutes": round(self.span_minutes, 4),
            "required_sample": self.required_sample,
            "jump_band_cents": self.jump_band_cents,
        }


@dataclass
class RegimeMeasurement:
    """Result of one batch measurement: the regimes and why others are missing."""

    window_start_ns: int
    window_end_ns: int
    regimes: Dict[str, MarketRegime] = field(default_factory=dict)
    refusals: Dict[str, str] = field(default_factory=dict)

    def reason_for(self, condition_id: str) -> str:
        return self.refusals.get(
            str(condition_id), "unmeasured: market was not part of the measurement"
        )


def _trades_glob(tape: Path) -> str:
    return Path(tape, TRADES_TABLE).as_posix() + "/**/*.parquet"


def latest_tape_ns(
    con: duckdb.DuckDBPyConnection, tape: Path = DEFAULT_TAPE_DIR
) -> Optional[int]:
    """Newest ``recv_ts_ns`` in the tape, or ``None`` when the tape is empty."""
    row = con.execute(
        f"SELECT MAX(recv_ts_ns) FROM read_parquet('{_trades_glob(tape)}')"
    ).fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0])


def required_sample_for(size_cv: Optional[float]) -> int:
    """Prints needed for the mean size to reach the stated relative precision."""
    if size_cv is None or not math.isfinite(size_cv):
        return MIN_PRINTS_FOR_DISPERSION
    needed = (SIZE_PRECISION_Z * float(size_cv) / SIZE_REL_HALF_WIDTH) ** 2
    return max(MIN_PRINTS_FOR_DISPERSION, int(math.ceil(needed)))


_MEASURE_SQL = """
WITH req(condition_id, band_cents) AS (VALUES {req_values}),
t AS (
    SELECT condition_id, recv_ts_ns, price, size
    FROM read_parquet('{glob}')
    WHERE recv_ts_ns >= {start_ns} AND recv_ts_ns < {end_ns}
      AND condition_id IS NOT NULL AND price IS NOT NULL AND size IS NOT NULL
      AND price > 0 AND price < 1 AND size > 0
      AND condition_id IN (SELECT condition_id FROM req)
),
prints AS (
    SELECT condition_id, recv_ts_ns, price, size,
           lag(price) OVER w AS prev_price
    FROM t
    WINDOW w AS (PARTITION BY condition_id ORDER BY recv_ts_ns)
),
by_print AS (
    SELECT p.condition_id,
           count(*) AS n,
           avg(p.size) AS mean_size,
           stddev_samp(p.size) AS sd_size,
           max(p.recv_ts_ns) - min(p.recv_ts_ns) AS span_ns,
           sum(CASE WHEN p.prev_price IS NOT NULL
                     AND abs(p.price - p.prev_price) * 100.0 > r.band_cents
                    THEN 1 ELSE 0 END) AS jumps
    FROM prints p JOIN req r USING (condition_id)
    GROUP BY 1
),
minutes AS (
    SELECT condition_id, recv_ts_ns // {ns_per_minute} AS minute_idx,
           arg_max(price, recv_ts_ns) AS last_price
    FROM t
    GROUP BY 1, 2
),
by_minute AS (
    SELECT m.condition_id,
           lag(m.last_price) OVER w2 AS prev_price,
           (m.minute_idx - lag(m.minute_idx) OVER w2) AS gap_minutes,
           m.last_price
    FROM minutes m
    WINDOW w2 AS (PARTITION BY m.condition_id ORDER BY m.minute_idx)
),
by_vol AS (
    SELECT v.condition_id,
           sum(ln(v.last_price / v.prev_price) * ln(v.last_price / v.prev_price)) AS sumsq,
           sum(v.gap_minutes) AS sum_minutes
    FROM by_minute v
    WHERE v.prev_price IS NOT NULL AND v.gap_minutes > 0
    GROUP BY 1
)
SELECT b.condition_id, b.n, b.mean_size, b.sd_size, b.span_ns, b.jumps,
       v.sumsq, v.sum_minutes, r.band_cents
FROM by_print b
JOIN req r USING (condition_id)
LEFT JOIN by_vol v USING (condition_id)
"""
# The nanosecond-per-minute divisor is a unit definition, so it is baked in
# once (``str.format`` must not see it as a field).
_MEASURE_SQL = _MEASURE_SQL.replace("{ns_per_minute}", str(NS_PER_MINUTE))


def measure_regimes(
    bands_cents: Mapping[str, float],
    *,
    tape: Path = DEFAULT_TAPE_DIR,
    window_hours: float = TAPE_WINDOW_HOURS,
    con: Optional[duckdb.DuckDBPyConnection] = None,
) -> RegimeMeasurement:
    """Measure each requested market's regime from one pass over the tape.

    ``bands_cents`` maps ``condition_id`` to that market's **published**
    ``rewards_max_spread`` in cents, which is the jump threshold. A market with
    no published band is not measurable (the jump statistic needs it) and is
    refused with that reason.
    """
    empties: Dict[str, str] = {}
    wanted: Dict[str, float] = {}
    for cid, band in bands_cents.items():
        key = str(cid or "")
        if not key:
            continue
        if band is None or not math.isfinite(float(band)) or float(band) <= 0.0:
            empties[key] = f"unmeasured: no published reward band (got {band!r})"
            continue
        wanted[key] = float(band)
    if not wanted:
        return RegimeMeasurement(
            window_start_ns=0, window_end_ns=0, regimes={}, refusals=empties
        )

    own_con = con is None
    if con is None:
        con = duckdb.connect()
    try:
        newest = latest_tape_ns(con, tape)
        if newest is None:
            for cid in wanted:
                empties[cid] = "unmeasured: trade tape is empty"
            return RegimeMeasurement(
                window_start_ns=0, window_end_ns=0, regimes={}, refusals=empties
            )
        window_end_ns = int(newest) + 1
        window_start_ns = window_end_ns - int(window_hours * NS_PER_HOUR)
        req_values = ", ".join("(?, ?)" for _ in wanted)
        params: list[Any] = []
        for cid, band in wanted.items():
            params.extend([cid, float(band)])
        rows = con.execute(
            _MEASURE_SQL.format(
                glob=_trades_glob(tape),
                start_ns=window_start_ns,
                end_ns=window_end_ns,
                req_values=req_values,
            ),
            params,
        ).fetchall()
    finally:
        if own_con:
            con.close()

    window_minutes = max(1e-9, window_hours * 60.0)
    regimes: Dict[str, MarketRegime] = {}
    for (
        cid,
        n,
        mean_size,
        sd_size,
        span_ns,
        jumps,
        sumsq,
        sum_minutes,
        band,
    ) in rows:
        trades = int(n or 0)
        if trades <= 0 or mean_size is None:
            empties[cid] = "unmeasured: no qualifying prints in the tape window"
            continue
        if trades < MIN_PRINTS_FOR_DISPERSION:
            empties[cid] = (
                f"unmeasured: {trades} print(s) in the {window_hours:.0f} h window; "
                f"{MIN_PRINTS_FOR_DISPERSION} needed for any dispersion estimate"
            )
            continue
        size_cv = (
            float(sd_size) / float(mean_size)
            if sd_size is not None and float(mean_size) > 0.0
            else None
        )
        required = required_sample_for(size_cv)
        if trades < required:
            empties[cid] = (
                f"unmeasured: {trades} prints < {required} needed for the mean size "
                f"to reach a 95 % relative half-width of {SIZE_REL_HALF_WIDTH:g} "
                f"(measured size CV {size_cv:.3f})"
            )
            continue
        vol_bps: Optional[float] = None
        if sumsq is not None and sum_minutes is not None and float(sum_minutes) > 0.0:
            vol_bps = (
                math.sqrt(max(0.0, float(sumsq)) / float(sum_minutes)) * 1e4
            )
        regimes[cid] = MarketRegime(
            condition_id=cid,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
            trades=trades,
            trades_per_min=trades / window_minutes,
            mean_trade_size_shares=float(mean_size),
            size_cv=float(size_cv) if size_cv is not None else float("nan"),
            realised_vol_bps_per_min=vol_bps,
            jump_freq=float(jumps or 0) / trades,
            span_minutes=float(span_ns) / NS_PER_MINUTE,
            required_sample=required,
            jump_band_cents=float(band),
        )
    missing = {
        cid: "unmeasured: no qualifying prints in the tape window" for cid in wanted
    }
    missing.update(empties)
    for cid in regimes:
        missing.pop(cid, None)
    if regimes:
        logger.info(
            f"tape regime measured for {len(regimes)}/{len(wanted)} markets over the "
            f"{window_hours:.0f} h window ending at recv_ts_ns={window_end_ns} "
            f"({sum(r.trades for r in regimes.values()):,} prints, "
            f"mean size "
            f"{sum(r.mean_trade_size_shares for r in regimes.values()) / len(regimes):.2f} "
            "shares)"
        )
    if missing:
        counts: Dict[str, int] = {}
        for reason in missing.values():
            counts[reason] = counts.get(reason, 0) + 1
        logger.info(
            f"tape regime unmeasured for {len(missing)} market(s): "
            + "; ".join(f"{count}x {reason}" for reason, count in sorted(counts.items()))
        )
    return RegimeMeasurement(
        window_start_ns=window_start_ns,
        window_end_ns=window_end_ns,
        regimes=regimes,
        refusals=missing,
    )


# --- cached batch measurement -------------------------------------------------

_REGIME_CACHE: Dict[str, RegimeMeasurement] = {}
_REGIME_CACHE_AT: Dict[str, float] = {}
_REGIME_LOCK = threading.Lock()


def _cache_key(
    bands_cents: Mapping[str, float], tape: Path, window_hours: float
) -> str:
    return (
        f"{Path(tape).as_posix()}|{window_hours}|"
        + ",".join(f"{c}:{float(b):.6f}" for c, b in sorted(bands_cents.items()) if b)
    )


def measure_regimes_cached(
    bands_cents: Mapping[str, float],
    *,
    tape: Path = DEFAULT_TAPE_DIR,
    window_hours: float = TAPE_WINDOW_HOURS,
    ttl_seconds: float = 300.0,
) -> RegimeMeasurement:
    """``measure_regimes`` memoised for ``ttl_seconds`` per (window, market set).

    A scan measures the same markets every few minutes; the tape cannot change
    faster than it is written, so the TTL is the caller's own scan cadence.
    """
    key = _cache_key(bands_cents, tape, window_hours)
    now = time.time()
    with _REGIME_LOCK:
        cached = _REGIME_CACHE.get(key)
        stamp = _REGIME_CACHE_AT.get(key)
        if cached is not None and stamp is not None and (now - stamp) < ttl_seconds:
            return cached
    measured = measure_regimes(bands_cents, tape=tape, window_hours=window_hours)
    with _REGIME_LOCK:
        _REGIME_CACHE[key] = measured
        _REGIME_CACHE_AT[key] = time.time()
    return measured


def reset_regime_cache() -> None:
    """Drop the cached measurements (test isolation)."""
    with _REGIME_LOCK:
        _REGIME_CACHE.clear()
        _REGIME_CACHE_AT.clear()
