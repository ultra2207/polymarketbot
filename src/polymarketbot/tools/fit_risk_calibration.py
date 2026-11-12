"""Fit the microstructure risk-gate coefficients from the recorded tape.

The gate (``polymarketbot.risk.unified.UnifiedRiskEngine``) consumes seven
numbers through ``live/base.py::load_risk_calibration``: ``c_depth``,
``z_score``, ``v_ref``, ``beta_vol``, ``pull_multiplier``,
``widen_offset_multiplier`` and ``consecutive_fills_threshold``.  Every one of
them is estimated here from ``data/tape/clob_events`` and
``data/tape/clob_trades``; the module has no calibration default.

Model
-----
``strategy/reward/microstructure.py`` implements the level-1
Cont-Kukanov-Stoikov order-flow imbalance on the top of book::

    dQb = qb - qb_prev        if bid == bid_prev
        = qb                  if bid >  bid_prev
        = -qb_prev            if bid <  bid_prev
    dQa = qa - qa_prev        if ask == ask_prev
        = qa                  if ask <  ask_prev
        = -qa_prev            if ask >  ask_prev
    OFI = dQb - dQa

and gates on the same quantity summed over the engine's window, against::

    threshold = max(c_depth * d_harm, z_score * pstdev(OFI window))
                * min(2.5, max(0.5, (volume_24h / v_ref) ** beta_vol))

with ``d_harm = 2*qb*qa/(qb+qa)`` the harmonic top-of-book depth.  The fit
therefore has to answer three separate questions, each from the tape:

1. **Impact** - how many ticks does the mid move per unit of
   ``|OFI| / d_harm``?  The inverse slope is the fraction of the harmonic depth
   that carries a one-tick move, i.e. ``c_depth``.
2. **Volume** - how does that impact scale with the token's 24 h traded
   volume?  The fitted log-log elasticity is ``beta_vol``; ``v_ref`` is the
   volume at which the elasticity is measured (the sample's geometric mean, the
   gauge that makes ``psi = 1`` for the median-scale market).
3. **Distribution** - at what quantile of the standardised OFI does the
   statistical branch fire?  The trigger budget is stated once (5 % of events)
   and the quantile is measured, never assumed to be 3 sigma.

The two decision multipliers are fitted by replaying both actions on the same
events against the measured markout table: the pull branch pays the net the
quote forgoes, the re-entry slippage and the forgone reward accrual, and the
widen branch keeps the measured markout while gaining ``k`` x the measured
micro-price drift of capture. The grid spans 0.1-100 in log steps so a
one-sided boundary cannot masquerade as an optimum, and the artifact says
explicitly when the answer is the corner. The streak threshold is read off the
measured run-length distribution of losing fills with a Student-t interval.

Usage
-----
    python -m polymarketbot.tools.fit_risk_calibration --probe
    python -m polymarketbot.tools.fit_risk_calibration \
        --hours 2026-11-06T05 2026-11-06T09 2026-11-06T13

Every stage prints the rows it read and wrote, so a run can be costed before it
is started.  The panel of OFI windows is cached at
``data/derived/risk_calibration_panel.parquet``; ``--rebuild`` re-reads the
tape.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.measurement import (
    DEFAULT_MAKER_MARKOUT_PATH,
    MakerMarkout,
    load_maker_markout,
)
from polymarketbot.tools.ctf_quote_fill_horizon import DEFAULT_MIN_FLOWS, _as_of
from polymarketbot.tools.pooling_stats import SCHEMA, linear_histogram
from polymarketbot.tools.tape_reader import read_tape

log = get_logger("fit_risk_calibration")

_REPO_ROOT = Path(__file__).resolve().parents[3]
EVENTS_ROOT = _REPO_ROOT / "data" / "tape" / "clob_events"
TRADES_ROOT = _REPO_ROOT / "data" / "tape" / "clob_trades"
PANEL_PATH = _REPO_ROOT / "data" / "derived" / "risk_calibration_panel.parquet"
OUTPUT_PATH = _REPO_ROOT / "data" / "derived" / "risk_calibration.json"
QUEUE_FIT_PATH = _REPO_ROOT / "data" / "derived" / "queue_fit.json"

# Per-hour measurements, so a bounded invocation resumes from the last finished
# hour instead of re-reading the tape: the OFI window panel and the fill replay
# each cost one full read of the hour's event parts.
PANEL_CACHE = _REPO_ROOT / "data" / "derived" / "risk_panel"
STREAK_CACHE = _REPO_ROOT / "data" / "derived" / "risk_streak"


def _read_json(path: Path) -> dict[str, Any]:
    """The artifact's object, or an empty one when it is absent or unreadable."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


# The engine evaluates a quote at its reprice cadence (``live/base.py``) and
# compares the OFI summed over the last 10 events to the threshold.  Both are
# properties of the engine, not fitted quantities; they are recorded in the
# artifact's definition so a reader can see which horizon the fit prices.
DEFAULT_HORIZON_S = 2.0
OFI_WINDOW_EVENTS = 10

# The statistical branch fires on the top 5 % of measured events.  This is the
# one preference in the artifact: it is the operator's trigger budget, stated
# once, and ``z_score`` is the measured quantile that implements it.
TRIGGER_BUDGET = 0.05

# Minimum placements for a streak-length cell to be reported.
MIN_STREAK_SAMPLES = 30

# The grid the standardised-OFI histogram is measured on.  The pooled
# ``z_score`` is read off the merged counts, so every partition must use this
# one grid: 0.05 of |sum of 10-event OFI| / pstdev per bin, 0..50.  The
# measured 5 % tail sits near 8, i.e. ~160 bins from the edge, and the tail
# counts above 50 are refused rather than named.
Z_SCORE_HISTOGRAM = {"lo": 0.0, "hi": 50.0, "width": 0.05}

# Prices are quoted on a 0.01 or 0.001 tick, so the integer milli-price of a
# level is exact and 1001 distinct milli-prices cover a binary token's range.
# It is the stride of the (token, price) level key, chosen so two tokens can
# never collide on one level.
_PRICE_CODE_STRIDE = 1001

#: The columns of one window row: what the trigger fit regresses on and what the
#: pull/widen replay prices.  Named once so the per-hour cache writes exactly the
#: rows the fit reads, including for an hour that produced no window at all.
_WINDOW_COLUMNS = (
    "asset",
    "ts_ns",
    "tick",
    "windows",
    "n_events",
    "ofi_sum",
    "d_harm",
    "mid0",
    "mid1",
    "bid0",
    "ask0",
    "bid1",
    "ask1",
    "qb0",
    "qa0",
    "micro0",
)

# Search grid for the pull/widen pair. The grid is a search range, not a
# calibration: the optimum is reported together with the objective value and
# whether it sits on the physical boundary. The pull span is log-spaced 0.1-100
# so that "pull on every triggered event" (0.1) and "never pull" (100) are both
# inside the grid: a one-sided grid turned the old edge value into a false
# interior optimum.
PULL_MULTIPLIER_GRID = tuple(float(value) for value in np.geomspace(0.1, 100.0, 25))
# Widening cannot be negative (a quote at or above the touch is the touch), so
# 0.0 is the physical "never widen" boundary rather than a grid edge.
WIDEN_MULTIPLIER_GRID = (0.0, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0)


# ---------------------------------------------------------------------------
# Top-of-book reconstruction
# ---------------------------------------------------------------------------
def _flatten_changes(table: pa.Table) -> tuple[np.ndarray, ...]:
    """``(ts, asset, price, size, is_buy, best_bid, best_ask)`` per level delta."""
    changes = table.filter(pc.equal(table["event_type"], "price_change"))
    if changes.num_rows == 0:
        empty = np.empty(0)
        return (empty.astype(np.int64),) + (empty.astype(object),) * 1 + (
            empty,
            empty,
        ) + (empty.astype(bool),) + (empty, empty)
    flat = pa.concat_arrays(changes.column("price_changes").chunks).flatten()
    counts = pc.list_value_length(changes.column("price_changes")).to_numpy()
    ts = np.repeat(changes["recv_ts_ns"].to_numpy(), counts.astype(np.int64))
    asset = flat.field("asset_id").to_numpy(zero_copy_only=False)
    price = flat.field("price").to_numpy(zero_copy_only=False).astype(np.float64)
    size = flat.field("size").to_numpy(zero_copy_only=False).astype(np.float64)
    side = flat.field("side").to_numpy(zero_copy_only=False)
    best_bid = (
        flat.field("best_bid").to_numpy(zero_copy_only=False).astype(np.float64)
    )
    best_ask = (
        flat.field("best_ask").to_numpy(zero_copy_only=False).astype(np.float64)
    )
    return ts, asset, price, size, side == "BUY", best_bid, best_ask


def _last_change_per_level(
    ts_rank: np.ndarray,
    level: np.ndarray,
    size: np.ndarray,
    state_level: np.ndarray,
    state_rank: np.ndarray,
    stride: int,
) -> np.ndarray:
    """Size of the most recent change at each state's own price level.

    ``level`` is the (token, price) level of every *change*; ``state_level`` the
    (token, best price) level of every state.  Both sides are keyed as
    ``level * stride + time rank`` so a single ``searchsorted`` resolves the
    as-of lookup without a Python loop over a million deltas.
    """
    change_key = level * stride + ts_rank
    order = np.argsort(change_key, kind="stable")
    change_key = change_key[order]
    change_size = size[order]
    state_key = state_level * stride + state_rank
    pos = np.searchsorted(change_key, state_key, side="right") - 1
    known = pos >= 0
    safe = np.maximum(pos, 0)
    found = np.where(known, change_size[safe], np.nan)
    same_level = known & (change_key[safe] // stride == state_level)
    found = np.where(same_level & np.isfinite(found) & (found > 0.0), found, np.nan)
    return found


def _tob_states(path: Path) -> dict[str, np.ndarray]:
    """Per-token top-of-book states from one event part.

    A state is emitted whenever the best prices change or a level delta lands
    on the current best, i.e. whenever ``OFI`` can move.  Best sizes come from
    the last observed delta at that price, so a level that was quoted below the
    touch is known when it later becomes the touch.
    """
    table = pq.read_table(
        path, columns=["recv_ts_ns", "event_type", "price_changes"]
    )
    ts, asset, price, size, is_buy, best_bid, best_ask = _flatten_changes(table)
    # The arrow table is several hundred MB of level deltas per part; the numpy
    # columns above are all this read needs, and holding both was what ran the
    # process out of memory on a loaded host.
    del table
    if ts.size == 0:
        empty = np.empty(0)
        return {
            "ts": empty.astype(np.int64),
            "asset": empty.astype(object),
            "bid": empty,
            "qb": empty,
            "ask": empty,
            "qa": empty,
        }

    code, tokens = pd.factorize(asset)
    order = np.lexsort((ts, code))
    ts, code = ts[order], code[order].astype(np.int64)
    price, size = price[order], size[order]
    is_buy = is_buy[order]
    best_bid, best_ask = best_bid[order], best_ask[order]

    prev_bid = np.empty_like(best_bid)
    prev_ask = np.empty_like(best_ask)
    prev_bid[0] = np.nan
    prev_ask[0] = np.nan
    prev_bid[1:] = best_bid[:-1]
    prev_ask[1:] = best_ask[:-1]
    starts = np.flatnonzero(np.diff(code) != 0) + 1
    prev_bid[starts] = np.nan
    prev_ask[starts] = np.nan

    changed = (
        ~np.isfinite(prev_bid)
        | ~np.isfinite(prev_ask)
        | (best_bid != prev_bid)
        | (best_ask != prev_ask)
    )
    at_touch = (is_buy & (price == best_bid)) | (~is_buy & (price == best_ask))
    state = changed | at_touch
    state &= (
        np.isfinite(best_bid)
        & np.isfinite(best_ask)
        & (best_bid > 0.0)
        & (best_ask >= best_bid)
        & (best_ask < 1.0)
    )
    if not np.any(state):
        empty = np.empty(0)
        return {
            "ts": empty.astype(np.int64),
            "asset": empty.astype(object),
            "bid": empty,
            "qb": empty,
            "ask": empty,
            "qa": empty,
        }

    # A level key only has to be injective, not sorted.  Prices are quoted on a
    # 0.001 tick at the finest, so the integer milli-price is the level code and
    # the code stride is every milli-price a binary token can hold.  The
    # previous `np.unique(concatenate(price, best_bid, best_ask))` built a
    # 6.8M-element union per part (a 52 MiB copy) and was the allocation that
    # failed on a loaded host; it also let a price index exceed the asset stride
    # and collide two tokens' levels.
    price_code = np.rint(price * 1000.0).astype(np.int64)
    level = code * _PRICE_CODE_STRIDE + price_code

    _, ts_index = np.unique(ts, return_inverse=True)
    rank_stride = int(ts_index.max()) + 2
    state_level = (
        code[state] * _PRICE_CODE_STRIDE
        + np.rint(best_bid[state] * 1000.0).astype(np.int64)
    )
    state_rank = ts_index[state]

    qb = _last_change_per_level(
        ts_index[is_buy],
        level[is_buy],
        size[is_buy],
        state_level,
        state_rank,
        rank_stride,
    )
    qa = _last_change_per_level(
        ts_index[~is_buy],
        level[~is_buy],
        size[~is_buy],
        code[state] * _PRICE_CODE_STRIDE
        + np.rint(best_ask[state] * 1000.0).astype(np.int64),
        state_rank,
        rank_stride,
    )
    return {
        "ts": ts[state],
        "asset": tokens[code[state]],
        "bid": best_bid[state],
        "qb": qb,
        "ask": best_ask[state],
        "qa": qa,
    }


def _ofi_events(states: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Cont-Kukanov-Stoikov level-1 OFI at every top-of-book state.

    Same arithmetic as ``MicrostructureEngine.update``; a state whose previous
    best size is unknown yields a NaN (not a zero) so it cannot enter a fit.
    """
    ts = states["ts"]
    asset = states["asset"]
    bid, qb, ask, qa = states["bid"], states["qb"], states["ask"], states["qa"]
    if ts.size < 2:
        states["ofi"] = np.full(ts.size, np.nan)
        states["mid"] = (bid + ask) / 2.0
        states["micro"] = states["mid"]
        return states

    code, _ = pd.factorize(asset)
    order = np.lexsort((ts, code))
    arrays = {k: states[k][order] for k in ("ts", "asset", "bid", "qb", "ask", "qa")}
    ts = arrays["ts"]
    asset = arrays["asset"]
    bid, qb, ask, qa = arrays["bid"], arrays["qb"], arrays["ask"], arrays["qa"]
    code = code[order]

    prev_bid = np.roll(bid, 1)
    prev_ask = np.roll(ask, 1)
    prev_qb = np.roll(qb, 1)
    prev_qa = np.roll(qa, 1)
    starts = np.flatnonzero(np.diff(code) != 0) + 1
    first = np.concatenate([[0], starts])
    for arr in (prev_bid, prev_ask, prev_qb, prev_qa):
        arr[first] = np.nan

    delta_b = np.where(
        bid > prev_bid,
        qb,
        np.where(bid == prev_bid, qb - prev_qb, -prev_qb),
    )
    delta_a = np.where(
        ask < prev_ask,
        qa,
        np.where(ask == prev_ask, qa - prev_qa, -prev_qa),
    )
    known = (
        np.isfinite(qb)
        & np.isfinite(prev_qb)
        & np.isfinite(qa)
        & np.isfinite(prev_qa)
    )
    ofi = np.where(known, delta_b - delta_a, np.nan)
    total = qb + qa
    micro = np.where(total > 0.0, (qb * ask + qa * bid) / total, (bid + ask) / 2.0)

    states.update(
        {
            "ts": ts,
            "asset": asset,
            "bid": bid,
            "qb": qb,
            "ask": ask,
            "qa": qa,
            "ofi": ofi,
            "mid": (bid + ask) / 2.0,
            "micro": micro,
        }
    )
    return states


# ---------------------------------------------------------------------------
# Window panel
# ---------------------------------------------------------------------------
def _tick_of(bid: np.ndarray, ask: np.ndarray) -> float:
    """The price grid a token quotes on, inferred from its own prices."""
    prices = np.concatenate([bid[np.isfinite(bid)], ask[np.isfinite(ask)]])
    if prices.size == 0:
        return float("nan")
    # A price off the cent grid (e.g. 0.231) can only exist on a 0.001 tick.
    cents = np.abs(prices * 100.0 - np.round(prices * 100.0)) > 1e-6
    return 0.001 if bool(np.any(cents)) else 0.01


def _window_rows(
    states: dict[str, np.ndarray],
    *,
    horizon_s: float,
    stale_s: float,
    standardised: list[float] | None = None,
) -> Iterable[dict[str, Any]]:
    """Non-overlapping ``horizon_s`` windows per token with their summed OFI.

    ``mid0``/``mid1`` are the as-of mids at the window edges; a stale edge
    leaves the move unmeasured (NaN) rather than substituting a carry-forward
    that would read as a zero move.  Windows without a single measured OFI
    event are not observations and are dropped.
    """
    ts = states["ts"]
    if ts.size < 2:
        return []
    bid, ask = states["bid"], states["ask"]
    qb, qa = states["qb"], states["qa"]
    ofi = states["ofi"]
    mid = states["mid"]
    micro = states["micro"]
    depth = 2.0 * qb * qa / (qb + qa)
    asset = states["asset"]
    code, _ = pd.factorize(asset)
    bounds = np.searchsorted(code, np.arange(code.max() + 2))

    rows: list[dict[str, Any]] = []
    H = float(horizon_s)
    for token_code in range(code.max() + 1):
        lo, hi = bounds[token_code], bounds[token_code + 1]
        if hi - lo < 2:
            continue
        t = ts[lo:hi]
        span = float(t[-1] - t[0]) / 1e9
        n_win = int(span // H)
        if n_win < 1:
            continue
        token = states["asset"][lo]
        tick = _tick_of(bid[lo:hi], ask[lo:hi])
        if not np.isfinite(tick):
            continue

        rel = (t - t[0]) / 1e9
        wid = np.minimum((rel // H).astype(np.int64), n_win - 1)
        valid = np.isfinite(ofi[lo:hi])
        ofi_sum = np.bincount(wid, weights=np.where(valid, ofi[lo:hi], 0.0), minlength=n_win)
        n_events = np.bincount(wid, weights=valid.astype(np.float64), minlength=n_win)
        known_depth = np.isfinite(depth[lo:hi])
        depth_sum = np.bincount(
            wid, weights=np.where(known_depth, depth[lo:hi], 0.0), minlength=n_win
        )
        depth_n = np.bincount(
            wid, weights=known_depth.astype(np.float64), minlength=n_win
        )
        d_harm = np.where(depth_n > 0.0, depth_sum / np.maximum(depth_n, 1.0), np.nan)

        edges = t[0] + np.int64(round(H * 1e9)) * np.arange(n_win + 1)
        idx = np.searchsorted(t, edges, side="right") - 1
        idx = np.maximum(idx, 0)
        fresh = (edges - t[idx]) <= np.int64(round(stale_s * 1e9))
        mid_edge = np.where(fresh, mid[lo:hi][idx], np.nan)
        bid_edge = np.where(fresh, bid[lo:hi][idx], np.nan)
        ask_edge = np.where(fresh, ask[lo:hi][idx], np.nan)
        if standardised is not None:
            _append_standardised(ofi[lo:hi], standardised)

        keep = n_events >= 1.0
        start_ok = fresh[:-1] & keep
        for k in np.flatnonzero(start_ok):
            i = int(idx[k])
            rows.append(
                {
                    "asset": token,
                    "ts_ns": int(edges[k]),
                    "tick": tick,
                    "windows": n_win,
                    "n_events": float(n_events[k]),
                    "ofi_sum": float(ofi_sum[k]),
                    "d_harm": float(d_harm[k]) if np.isfinite(d_harm[k]) else None,
                    "mid0": float(mid_edge[k]),
                    "mid1": float(mid_edge[k + 1]),
                    "bid0": float(bid_edge[k]),
                    "ask0": float(ask_edge[k]),
                    "bid1": float(bid_edge[k + 1]),
                    "ask1": float(ask_edge[k + 1]),
                    "qb0": float(qb[lo:hi][i]),
                    "qa0": float(qa[lo:hi][i]),
                    "micro0": float(micro[lo:hi][i]),
                }
            )
    return rows


def _append_standardised(ofi: np.ndarray, out: list[float]) -> None:
    """``|sum of 10 events| / pstdev`` at every event, as the engine computes it."""
    if ofi.size < OFI_WINDOW_EVENTS:
        return
    values = np.where(np.isfinite(ofi), ofi, np.nan)
    # A finite run is required: the engine's window is the last 10 *events*.
    if not np.all(np.isfinite(values)):
        values = values.copy()
        values[~np.isfinite(values)] = 0.0
    window = np.lib.stride_tricks.sliding_window_view(values, OFI_WINDOW_EVENTS)
    total = window.sum(axis=1)
    std = window.std(axis=1, ddof=0)
    usable = std > 0.0
    out.extend(np.abs(total[usable]) / std[usable])


def _panel_cache_path(date: str, hour: str, horizon_s: float) -> tuple[Path, Path]:
    """Per-hour window cache, keyed by the window horizon it was measured at."""
    folder = PANEL_CACHE / f"horizon={horizon_s:g}" / f"date={date}"
    return folder / f"hour={hour}.parquet", folder / f"hour={hour}.std.parquet"


def _panel_hour(
    job: tuple[str, str, float, float, int | None, float | None],
) -> tuple[str, str, list[dict[str, Any]], list[float], bool]:
    """Windows and standardised events of one hour partition (or the first part).

    A finished hour is cached at :data:`PANEL_CACHE` so a bounded invocation
    that runs out of time before the fit resumes from the measurement instead
    of re-reading the tape.  A truncated read (``max_files``) is never cached:
    half an hour is not that hour.
    """
    date, hour, horizon_s, stale_s, max_files, deadline = job
    cache, std_cache = _panel_cache_path(date, hour, horizon_s)
    if max_files is None and cache.exists() and std_cache.exists():
        frame = pd.read_parquet(cache)
        events = pd.read_parquet(std_cache)["value"].to_numpy().tolist()
        log.info(f"{date} {hour}: {frame.shape[0]:,} cached windows")
        return date, hour, frame.to_dict("records"), events, True

    parts = _event_parts(date, hour)
    if max_files is not None:
        parts = parts[:max_files]
    log.info(f"{date} {hour}: {len(parts)} event part(s)")
    rows: list[dict[str, Any]] = []
    standardised: list[float] = []
    for path in parts:
        if deadline is not None and time.time() > deadline:
            log.warning(
                f"{date} {hour}: budget exhausted after {len(rows):,} windows; "
                "the hour is not cached and the fit is partial"
            )
            return date, hour, [], [], False
        started = time.time()
        states = _ofi_events(_tob_states(path))
        part_rows = _window_rows(
            states,
            horizon_s=horizon_s,
            stale_s=stale_s,
            standardised=standardised,
        )
        rows.extend(part_rows)
        log.info(
            f"  {path.name}: {states['ts'].size:,} states -> "
            f"{len(part_rows):,} windows in {time.time() - started:.1f}s"
        )
    if max_files is None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        for path, frame in (
            (cache, pd.DataFrame(rows, columns=_WINDOW_COLUMNS)),
            (std_cache, pd.DataFrame({"value": standardised})),
        ):
            temporary = path.with_suffix(path.suffix + ".tmp")
            frame.to_parquet(temporary, index=False)
            temporary.replace(path)
    return date, hour, rows, standardised, True


def _collect_panel(
    hours: Sequence[tuple[str, str]],
    *,
    horizon_s: float,
    stale_s: float,
    max_files: int | None,
    standardised: list[float],
    workers: int = 1,
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    jobs = [
        (date, hour, float(horizon_s), float(stale_s), max_files, deadline)
        for date, hour in hours
    ]
    rows: list[dict[str, Any]] = []
    complete = True
    if workers <= 1 or len(jobs) <= 1:
        for job in jobs:
            _date, _hour, part_rows, events, done = _panel_hour(job)
            rows.extend(part_rows)
            standardised.extend(events)
            complete = complete and done
        return rows, complete
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        for _date, _hour, part_rows, events, done in pool.map(_panel_hour, jobs):
            rows.extend(part_rows)
            standardised.extend(events)
            complete = complete and done
    return rows, complete


def _event_parts(date: str, hour: str) -> list[Path]:
    folder = EVENTS_ROOT / f"date={date}" / f"hour={hour}"
    if not folder.is_dir():
        raise FileNotFoundError(f"no event partition at {folder}")
    return sorted(folder.glob("*.parquet"))


# ---------------------------------------------------------------------------
# 24 h traded volume
# ---------------------------------------------------------------------------
def _hourly_volume() -> dict[tuple[str, str, str], float]:
    """Recorded share volume per ``(asset, date, hour)`` from the print tape."""
    out: dict[tuple[str, str, str], float] = {}
    for date_dir in sorted(TRADES_ROOT.glob("date=*")):
        date = date_dir.name.split("=", 1)[1]
        for hour_dir in sorted(date_dir.glob("hour=*")):
            hour = hour_dir.name.split("=", 1)[1]
            files = sorted(hour_dir.glob("*.parquet"))
            if not files:
                continue
            table = read_tape(files, columns=["asset_id", "size"])
            frame = pd.DataFrame(
                {
                    "asset": table["asset_id"].to_pylist(),
                    "size": table["size"].to_numpy(),
                }
            )
            frame = frame[np.isfinite(frame["size"]) & (frame["size"] > 0.0)]
            if frame.empty:
                continue
            grouped = frame.groupby("asset")["size"].sum()
            for asset, value in grouped.items():
                out[(str(asset), date, hour)] = float(value)
    return out


def _trailing_volume(
    hourly: dict[tuple[str, str, str], float], date: str, hour: str
) -> tuple[dict[str, float], int]:
    """Trailing 24 h share volume per token, and how many hourly buckets exist."""
    current = pd.Timestamp(f"{date}T{hour}:00:00", tz="UTC")
    buckets: list[tuple[str, str]] = []
    for back in range(1, 25):
        stamp = current - pd.Timedelta(hours=back)
        buckets.append((stamp.strftime("%Y-%m-%d"), stamp.strftime("%H")))
    covered = [b for b in buckets if any(k[1:] == b for k in hourly)]
    totals: dict[str, float] = {}
    for (asset, b_date, b_hour), value in hourly.items():
        if (b_date, b_hour) in covered:
            totals[asset] = totals.get(asset, 0.0) + value
    return totals, len(covered)


# ---------------------------------------------------------------------------
# Fits
# ---------------------------------------------------------------------------
def _ols(x: np.ndarray, y: np.ndarray) -> dict[str, float]:
    """Slope, intercept, R² and HC1 standard errors for a single regressor."""
    n = int(x.size)
    design = np.column_stack([np.ones(n), x])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    fit = design @ beta
    resid = y - fit
    tss = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid**2).sum()) / tss if tss > 0.0 else float("nan")
    # HC1 covariance: robust to the heteroskedastic impact noise.
    xtx_inv = np.linalg.inv(design.T @ design)
    meat = design.T @ (design * (resid**2)[:, None])
    cov = xtx_inv @ meat @ xtx_inv * (n / max(n - 2, 1))
    se = np.sqrt(np.diag(cov))
    return {
        "intercept": float(beta[0]),
        "slope": float(beta[1]),
        "se_intercept": float(se[0]),
        "se_slope": float(se[1]),
        "r2": r2,
        "n": float(n),
    }


def _ci(estimate: float, se: float, n: int) -> list[float] | None:
    if not np.isfinite(estimate) or not np.isfinite(se) or n < 3:
        return None
    # Normal approximation is adequate at the sample sizes used here and keeps
    # the fit free of a scipy dependency.
    return [estimate - 1.96 * se, estimate + 1.96 * se]


def _logistic(design: np.ndarray, target: np.ndarray, iterations: int = 60) -> dict:
    """IRLS logistic fit with its covariance and McFadden pseudo-R²."""
    beta = np.zeros(design.shape[1])
    covariance = np.zeros((design.shape[1], design.shape[1]))
    for _ in range(iterations):
        eta = design @ beta
        probability = 1.0 / (1.0 + np.exp(-eta))
        weight = np.clip(probability * (1.0 - probability), 1e-12, None)
        working = eta + (target - probability) / weight
        information = design.T @ (design * weight[:, None])
        information += np.eye(design.shape[1]) * 1e-9
        covariance = np.linalg.inv(information)
        updated = covariance @ (design.T @ (weight * working))
        if np.max(np.abs(updated - beta)) < 1e-10:
            beta = updated
            break
        beta = updated
    eta = design @ beta
    probability = np.clip(1.0 / (1.0 + np.exp(-eta)), 1e-12, 1.0 - 1e-12)
    log_likelihood = float(
        np.sum(target * np.log(probability) + (1.0 - target) * np.log(1.0 - probability))
    )
    base = float(np.clip(target.mean(), 1e-12, 1.0 - 1e-12))
    null = float(np.sum(target * np.log(base) + (1.0 - target) * np.log(1.0 - base)))
    return {
        "coefficients": beta,
        "covariance": covariance,
        "pseudo_r2": 1.0 - log_likelihood / null if null < 0.0 else float("nan"),
        "log_likelihood": log_likelihood,
    }


def _gradient_ci(
    estimate: float, covariance: np.ndarray, gradient: np.ndarray
) -> list[float]:
    variance = float(gradient @ covariance @ gradient)
    standard_error = math.sqrt(max(variance, 0.0))
    return [estimate - 1.96 * standard_error, estimate + 1.96 * standard_error]


def fit_ofi(panel: pd.DataFrame) -> dict[str, Any]:
    """Estimate ``c_depth``, ``beta_vol`` and ``v_ref`` from the window panel.

    The gate's threshold is a *trigger*: the depth-normalised flow at which the
    mid moves one tick in the flow's own direction.  The fit therefore prices
    the measured probability of that move::

        P(hit | x, V) = logistic(a + b * log x + c * log V)
        x = |sum of OFI over the reprice horizon| / d_harm

    The threshold is the flow at which that probability is one half (the stated
    target: the trigger fires where a move is more likely than not), which
    inverts to::

        log(c_depth) = -a/b + beta_vol * log(v_ref)
        beta_vol     = -c/b

    with ``v_ref`` the sample's geometric-mean trailing-24h volume, the gauge at
    which ``psi = 1`` for the median-scale market.  The linear impulse
    regression ``|dmid| ~ |OFI|/d_harm`` is fitted too and reported as evidence:
    on this tape it is *not* identified and it is not the object the gate uses.
    """
    frame = panel.copy()
    frame = frame[
        (frame["tick"] > 0.0)
        & (frame["d_harm"] > 0.0)
        & np.isfinite(frame["ofi_sum"])
        & (frame["mid0"] > 0.0)
        & (frame["mid1"] > 0.0)
    ].copy()
    frame["dmid"] = (frame["mid1"] - frame["mid0"]) / frame["tick"]
    frame["x"] = frame["ofi_sum"].abs() / frame["d_harm"]
    frame["hit"] = (
        (np.sign(frame["dmid"]) == np.sign(frame["ofi_sum"]))
        & (frame["dmid"].abs() >= 0.5)
    )
    frame = frame[frame["x"] > 0.0].copy()

    moving_windows = frame[frame["dmid"] != 0.0]
    result: dict[str, Any] = {
        "windows": int(frame.shape[0]),
        "tokens": int(frame["asset"].nunique()),
        "hit_rate": float(frame["hit"].mean()),
        "moving_windows": int(moving_windows.shape[0]),
        "sign_agreement_moving": float(
            (np.sign(moving_windows["dmid"]) == np.sign(moving_windows["ofi_sum"])).mean()
        )
        if moving_windows.shape[0]
        else None,
        "directional_one_tick_share_moving": float(
            frame.loc[frame["dmid"] != 0.0, "hit"].mean()
        )
        if moving_windows.shape[0]
        else None,
    }

    # Impulse-magnitude regression, reported as evidence only.  It is
    # conditional on the mid actually moving: log(0) is undefined, and the
    # unconditional slope would price a move that did not happen.
    moving = frame[frame["dmid"] != 0.0].copy()
    moving["log_x"] = np.log(moving["x"])
    moving["log_y"] = np.log(moving["dmid"].abs())
    shape = _ols(moving["log_x"].to_numpy(), moving["log_y"].to_numpy())
    result["impulse_exponent"] = shape["slope"]
    result["impulse_exponent_ci"] = _ci(
        shape["slope"], shape["se_slope"], int(shape["n"])
    )
    result["impulse_r2"] = shape["r2"]
    result["impulse_n"] = int(shape["n"])

    sample = frame[
        np.isfinite(frame["volume_24h"]) & (frame["volume_24h"] > 0.0)
    ].copy()
    if sample.shape[0] < 100 or sample["volume_24h"].nunique() < 2:
        result["volume_windows"] = int(sample.shape[0])
        result["identified"] = False
        return result

    log_volume = np.log(sample["volume_24h"].to_numpy())
    log_x = np.log(sample["x"].to_numpy())
    target = sample["hit"].to_numpy().astype(np.float64)
    design = np.column_stack([np.ones(target.size), log_x, log_volume])
    model = _logistic(design, target)
    a, b_x, c_v = model["coefficients"]
    covariance = model["covariance"]
    v_ref = float(math.exp(float(log_volume.mean())))

    if b_x == 0.0:
        result["identified"] = False
        result["volume_windows"] = int(target.size)
        return result

    beta_vol = -c_v / b_x
    log_c_depth = -a / b_x + beta_vol * math.log(v_ref)
    # Delta method: beta = -c/b and log c_depth = -a/b + beta*mean(log V).
    beta_gradient = np.array([0.0, c_v / b_x**2, -1.0 / b_x])
    depth_gradient = np.array(
        [
            -1.0 / b_x,
            a / b_x**2 + math.log(v_ref) * c_v / b_x**2,
            -math.log(v_ref) / b_x,
        ]
    )
    beta_ci = _gradient_ci(beta_vol, covariance, beta_gradient)
    depth_ci = _gradient_ci(log_c_depth, covariance, depth_gradient)
    result.update(
        {
            "identified": True,
            "volume_windows": int(target.size),
            "participation_hits": int(target.sum()),
            "trigger_pseudo_r2": model["pseudo_r2"],
            "coef_log_x": float(b_x),
            "coef_log_x_se": float(math.sqrt(covariance[1, 1])),
            "coef_log_volume": float(c_v),
            "coef_log_volume_se": float(math.sqrt(covariance[2, 2])),
            "v_ref": v_ref,
            "beta_vol": float(beta_vol),
            "beta_vol_ci": beta_ci,
            "c_depth": float(math.exp(log_c_depth)),
            "c_depth_ci": [math.exp(depth_ci[0]), math.exp(depth_ci[1])],
        }
    )
    return result


def standardised_quantile(values: Sequence[float], budget: float) -> float | None:
    """Empirical ``1 - budget`` quantile of the standardised OFI."""
    array = np.asarray([v for v in values if np.isfinite(v)], dtype=np.float64)
    if array.size == 0:
        return None
    return float(np.quantile(array, 1.0 - budget))


def _deepest_horizon(markout: MakerMarkout) -> float:
    """The horizon with the most measured fills, i.e. the best-priced drift.

    The pull/widen replay charges the adverse drift a filled leg actually pays
    over the life of the position, not over one reprice.  The horizon with the
    largest measured fill sample is the one whose drift is measured most
    precisely; the whole grid is also reported at the shortest horizon so the
    sensitivity is visible.
    """
    totals: dict[float, int] = {}
    for horizon, _tick, _value, fills in markout.cells:
        totals[float(horizon)] = totals.get(float(horizon), 0) + int(fills)
    return max(totals, key=totals.get)


def _nearest_horizon(markout: MakerMarkout) -> float:
    """Smallest horizon the markout table measured for every quoted tick."""
    ticks = sorted({float(cell[1]) for cell in markout.cells if cell[3] > 0})
    if not ticks:
        raise ValueError("markout table carries no measured cell")
    horizons = []
    for tick in ticks:
        supported = markout.supported_horizons(tick)
        if not supported:
            raise ValueError(f"markout table has no measured fill for tick {tick}")
        horizons.append(float(supported[0]))
    return max(horizons)


# ---------------------------------------------------------------------------
# Pull/widen replay
# ---------------------------------------------------------------------------
def _fill_flow(
    frame: pd.DataFrame,
    prints: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
    quotes: np.ndarray,
    horizon_s: float,
    min_flow: float,
) -> np.ndarray:
    """Whether a resting buy quote filled within the horizon.

    A buy at ``quote`` is price priority over every bid at or below it, so a
    SELL print at or below ``quote + half tick`` would have traded with it; the
    cumulative printed size inside the horizon must reach the minimum order
    size.  This is the standard price-priority fill rule; the exact-price
    variant in ``ctf_quote_fill_horizon`` is deliberately more conservative.
    Rows are grouped by ``(quote, tick)`` so the half-tick tolerance is the
    row's own tick.
    """
    filled = np.zeros(frame.shape[0], dtype=bool)
    times = frame["ts_ns"].to_numpy().astype(np.float64) * 1e-9
    assets = frame["asset"].to_numpy()
    ticks = frame["tick"].to_numpy()
    for asset in np.unique(assets):
        rows = np.flatnonzero(assets == asset)
        series = prints.get(str(asset))
        if series is None:
            continue
        sell_ts, sell_price, sell_size = series
        if sell_ts.size == 0:
            continue
        keys = {
            (round(float(quotes[i]), 9), float(ticks[i])) for i in rows
        }
        for (quote, tick) in keys:
            selected = rows[
                np.isclose(quotes[rows], quote, atol=1e-12)
                & np.isclose(ticks[rows], tick, atol=1e-12)
            ]
            if selected.size == 0:
                continue
            exact = sell_price <= quote + 0.5 * tick
            et, es = sell_ts[exact], sell_size[exact]
            if et.size == 0:
                continue
            cumulative = np.concatenate(([0.0], np.cumsum(es)))
            lo = np.searchsorted(et, times[selected], side="right")
            hi = np.searchsorted(et, times[selected] + horizon_s, side="right")
            flow = cumulative[hi] - cumulative[lo]
            filled[selected] = flow >= min_flow
    return filled


def replay_actions(
    frame: pd.DataFrame,
    prints: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
    *,
    markout: MakerMarkout,
    horizon_s: float,
    min_flow: float,
    threshold: np.ndarray,
    accrual_per_share_sec: float = 0.0,
) -> dict[str, Any]:
    """Grid-search ``(pull_multiplier, widen_offset_multiplier)`` on the tape.

    Both actions are priced per decision event as the *regret* against the
    measured outcome of the resting quote (minimised, so negative is profit):

    * the fill the quote would have taken pays ``markout - capture`` per share,
      with ``capture = mid0 - quote`` and ``markout`` the measured adverse drift
      of that tick and horizon from ``ctf_maker_markout.json``;
    * **widen** by ``k`` x the measured micro-price drift: the quote moves down
      to ``bid0 + tick - k*drift``, so a fill earns ``k*drift`` more capture and
      pays the same measured markout - a deeper quote is not credited with a
      smaller drift, because that reduction was never measured;
    * **pull**: the quote takes no fill and pays no markout, but it forgoes the
      net it would have earned, pays the re-entry slippage
      ``max(0, bid_best(t+h) - bid_best(t))`` it must cross to regain the
      position, and forgoes ``accrual_per_share_sec * horizon_s`` of reward
      accrual while it is out of the book. The accrual rate is the measured
      queue-value accrual (``queue_fit.json``); with no measured rate the pull
      branch understates its own cost and the artifact says so.

    The reported ``pull_minus_widen`` is the paired per-event difference with a
    market-clustered interval: it is the test of whether the corner is a
    preference or noise.
    """
    threshold = np.asarray(threshold, dtype=np.float64)
    measured_markout = _markout_by_tick(
        markout, horizon_s, frame["tick"].to_numpy()
    )
    keep = np.isfinite(measured_markout)
    frame = frame[keep].reset_index(drop=True)
    threshold = threshold[keep]
    measured_markout = measured_markout[keep]
    drift = np.abs(frame["micro0"].to_numpy() - frame["mid0"].to_numpy())
    ticks = frame["tick"].to_numpy()
    capture = frame["mid0"].to_numpy() - frame["bid0"].to_numpy() - ticks
    slip = np.maximum(0.0, frame["bid1"].to_numpy() - frame["bid0"].to_numpy())
    ofi = np.abs(frame["ofi_sum"].to_numpy())
    forgone_accrual = max(0.0, float(accrual_per_share_sec)) * float(horizon_s)

    base_fill = _fill_flow(
        frame, prints, frame["bid0"].to_numpy() + ticks, horizon_s, min_flow
    )
    net_if_filled = measured_markout - capture
    pull_regret = base_fill * net_if_filled + slip + forgone_accrual

    grid_rows = []
    widen_regrets: dict[float, np.ndarray] = {}
    for widen_multiplier in WIDEN_MULTIPLIER_GRID:
        offset = widen_multiplier * drift
        quote = frame["bid0"].to_numpy() + ticks - offset
        fill = _fill_flow(frame, prints, quote, horizon_s, min_flow)
        widen_regret = fill * (measured_markout - (capture + offset))
        widen_regrets[widen_multiplier] = widen_regret
        for pull_multiplier in PULL_MULTIPLIER_GRID:
            extreme = ofi >= threshold * pull_multiplier
            cost = np.where(extreme, pull_regret, widen_regret)
            grid_rows.append(
                {
                    "pull_multiplier": float(pull_multiplier),
                    "widen_offset_multiplier": float(widen_multiplier),
                    "objective": float(np.mean(cost)),
                    "pull_share": float(np.mean(extreme)),
                    "widen_fill_rate": float(np.mean(fill[~extreme])) if np.any(~extreme) else None,
                    "pull_fill_rate": float(np.mean(base_fill[extreme])) if np.any(extreme) else None,
                }
            )
    best = min(grid_rows, key=lambda row: row["objective"])
    pull_at_edge = best["pull_multiplier"] in (PULL_MULTIPLIER_GRID[0], PULL_MULTIPLIER_GRID[-1])
    widen_at_edge = best["widen_offset_multiplier"] == WIDEN_MULTIPLIER_GRID[0]
    curve = sorted(
        (
            row["pull_multiplier"],
            row["objective"],
        )
        for row in grid_rows
        if row["widen_offset_multiplier"] == best["widen_offset_multiplier"]
    )
    # Both corners are tested: the objective rising with the multiplier is the
    # "pull on everything" corner and falling is the "never pull" corner.  A
    # one-sided test reported only the first, so a monotone decrease toward
    # "never pull" was labelled an interior optimum.
    rises = all(
        curve[index][1] <= curve[index + 1][1] + 1e-12
        for index in range(len(curve) - 1)
    )
    falls = all(
        curve[index][1] >= curve[index + 1][1] - 1e-12
        for index in range(len(curve) - 1)
    )
    monotone = bool(rises or falls)
    direction = "rising" if rises else "falling" if falls else "mixed"
    # The search cannot go past an edge, so an edge minimum *is* the corner: the
    # grid value only implements a policy, it is not a fitted optimum.  The
    # monotone test is evidence that the corner is the tape's answer rather than
    # a coarse grid's, and it is reported beside it.
    corner = None
    corner_objective = None
    if pull_at_edge:
        corner = (
            "pull_all"
            if best["pull_multiplier"] <= PULL_MULTIPLIER_GRID[0]
            else "never_pull"
        )
        corner_objective = float(curve[0][1] if corner == "pull_all" else curve[-1][1])

    delta = pull_regret - widen_regrets[0.0]
    cluster = np.unique(frame["asset"].to_numpy(), return_inverse=True)[1]
    counts = np.bincount(cluster)
    sums = np.bincount(cluster, weights=delta)
    clusters = int(counts.size)
    scale = clusters / max(clusters - 1, 1)
    se = math.sqrt(float(sums @ sums) / delta.size**2 * scale) if clusters > 1 else float("nan")
    mean_delta = float(np.mean(delta))
    return {
        "events": int(frame.shape[0]),
        "horizon_s": float(horizon_s),
        "min_flow_shares": float(min_flow),
        "accrual_per_share_sec": float(accrual_per_share_sec),
        "forgone_accrual_per_share": forgone_accrual,
        "grid": grid_rows,
        "best_grid_point": best,
        "corner": corner,
        "at_corner": corner is not None,
        "corner_objective": corner_objective,
        "pull_curve": [
            {"pull_multiplier": float(multiplier), "objective": float(value)}
            for multiplier, value in curve
        ],
        "pull_at_edge": bool(pull_at_edge),
        "widen_at_boundary": bool(widen_at_edge),
        "monotone_in_pull": bool(monotone),
        "pull_curve_direction": direction,
        "accrual_included": bool(accrual_per_share_sec > 0.0),
        "pull_always": float(np.mean(pull_regret)),
        "widen_always": float(np.mean(widen_regrets[0.0])),
        "pull_minus_widen": {
            "mean": mean_delta,
            "se": se,
            "ci95": [mean_delta - 1.96 * se, mean_delta + 1.96 * se]
            if math.isfinite(se) and se > 0.0
            else None,
            "clusters": clusters,
            "n": int(delta.size),
            "definition": (
                "per-event mean of (pull regret - never-widen regret); negative "
                "means pulling on every triggered event beats widening"
            ),
        },
        "objective": (
            "mean over decision events of the regret of the chosen action: "
            "fill*(markout - capture) for a fill, plus re-entry slippage and "
            "forgone reward accrual when the quote is pulled"
        ),
    }


def _markout_by_tick(
    markout: MakerMarkout, horizon_s: float, ticks: np.ndarray
) -> np.ndarray:
    """Measured adverse markout per share at the horizon, NaN where unmeasured."""
    out = np.full(ticks.shape, np.nan)
    for tick in np.unique(ticks):
        value = markout.markout(horizon_s, float(tick))
        if value is not None:
            out[ticks == tick] = float(value)
    return out


# ---------------------------------------------------------------------------
# Adverse-fill streaks
# ---------------------------------------------------------------------------
def _part_mid_series(path: Path) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Per-token ``(ts, best_bid, best_ask)`` from one event part.

    ``price_change`` rows carry the resulting touch, which is all the fill
    replay needs for the mid and the markout; the full ``book`` ladder is not
    read because a ladder per subscription is far larger than the deltas and
    carries no touch information the deltas do not.
    """
    table = pq.read_table(path, columns=["recv_ts_ns", "event_type", "price_changes"])
    changes = table.filter(pc.equal(table["event_type"], "price_change"))
    if changes.num_rows == 0:
        return {}
    flat = pa.concat_arrays(changes.column("price_changes").chunks).flatten()
    counts = pc.list_value_length(changes.column("price_changes")).to_numpy()
    ts = np.repeat(changes["recv_ts_ns"].to_numpy(), counts.astype(np.int64))
    asset = flat.field("asset_id").to_numpy(zero_copy_only=False)
    bid = flat.field("best_bid").to_numpy(zero_copy_only=False).astype(np.float64)
    ask = flat.field("best_ask").to_numpy(zero_copy_only=False).astype(np.float64)
    keep = np.isfinite(bid) & np.isfinite(ask) & (bid > 0.0) & (ask > bid)
    asset, ts, bid, ask = asset[keep], ts[keep], bid[keep], ask[keep]
    code, tokens = pd.factorize(asset)
    seq = np.lexsort((ts, code))
    code, ts, bid, ask = code[seq], ts[seq], bid[seq], ask[seq]
    bounds = np.searchsorted(code, np.arange(tokens.size + 1))
    out: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for i, token in enumerate(tokens):
        lo, hi = bounds[i], bounds[i + 1]
        if hi - lo < 2:
            continue
        unique_ts = np.unique(ts[lo:hi])
        last = np.searchsorted(ts[lo:hi], unique_ts, side="right") - 1
        out[str(token)] = (
            unique_ts.astype(np.float64) * 1e-9,
            bid[lo:hi][last],
            ask[lo:hi][last],
        )
    return out


def _tob_mid_series(part: Path) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Per-token ``(ts, best_bid, best_ask)`` for one hour of event parts.

    The parts are reduced one at a time and merged per token, because
    factorising an hour's 13 million token ids at once is slower than twelve
    5-minute factorisations.
    """
    merged: dict[str, list[tuple[np.ndarray, np.ndarray, np.ndarray]]] = {}
    for path in sorted(part.glob("*.parquet")):
        for token, series in _part_mid_series(path).items():
            merged.setdefault(token, []).append(series)
    out: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for token, pieces in merged.items():
        ts = np.concatenate([piece[0] for piece in pieces])
        bid = np.concatenate([piece[1] for piece in pieces])
        ask = np.concatenate([piece[2] for piece in pieces])
        order = np.argsort(ts, kind="stable")
        out[token] = (ts[order], bid[order], ask[order])
    return out


def _sell_prints(date: str, hour: str) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Per-token ``(ts, price, size)`` of every SELL print in one hour."""
    folder = TRADES_ROOT / f"date={date}" / f"hour={hour}"
    files = sorted(folder.glob("*.parquet")) if folder.is_dir() else []
    if not files:
        return {}
    table = read_tape(files, columns=["recv_ts_ns", "asset_id", "side", "price", "size"])
    table = table.filter(pc.equal(pc.utf8_upper(table["side"]), "SELL"))
    out: dict[str, list[tuple[float, float, float]]] = {}
    for asset, ts, price, size in zip(
        table["asset_id"].to_pylist(),
        table["recv_ts_ns"].to_numpy(),
        table["price"].to_numpy(),
        table["size"].to_numpy(),
    ):
        if not asset or not np.isfinite(size) or size <= 0.0 or not np.isfinite(price):
            continue
        out.setdefault(asset, []).append((float(ts) * 1e-9, float(price), float(size)))
    series: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for asset, prints in out.items():
        prints.sort()
        series[asset] = (
            np.asarray([p[0] for p in prints], dtype=np.float64),
            np.asarray([p[1] for p in prints], dtype=np.float64),
            np.asarray([p[2] for p in prints], dtype=np.float64),
        )
    return series


def _streak_cache_path(date: str, hour: str, grid_s: float, horizon_s: float) -> Path:
    """Per-hour fill replay cache, keyed by the placement grid it was measured at."""
    folder = (
        STREAK_CACHE
        / f"grid={grid_s:g}-horizon={horizon_s:g}"
        / f"date={date}"
    )
    return folder / f"hour={hour}.parquet"


def _fill_hour(
    job: tuple[str, str, float, float, float, float, float | None],
) -> tuple[list[dict[str, Any]], bool]:
    """Fills of one hour partition (the unit a worker can run alone)."""
    date, hour, grid_s, horizon_s, min_flow, stale_s, deadline = job
    cache = _streak_cache_path(date, hour, grid_s, horizon_s)
    if cache.exists():
        frame = pd.read_parquet(cache)
        log.info(f"  {date} {hour}: {frame.shape[0]:,} cached fills")
        return frame.to_dict("records"), True
    if deadline is not None and time.time() > deadline:
        log.warning(f"  {date} {hour}: budget exhausted before the fill replay")
        return [], False
    records: list[dict[str, Any]] = []
    prints = _sell_prints(date, hour)
    folder = EVENTS_ROOT / f"date={date}" / f"hour={hour}"
    if not folder.is_dir() or not prints:
        log.info(f"  {date} {hour}: no prints, no streak sample")
        return records, True
    series = _tob_mid_series(folder)
    for asset, (ts, bid, ask) in series.items():
        if ts.size < 2:
            continue
        start, stop = ts[0], ts[-1] - horizon_s
        if stop <= start:
            continue
        grid = np.arange(start, stop, grid_s)
        if grid.size == 0:
            continue
        idx = np.searchsorted(ts, grid, side="right") - 1
        ok = idx >= 0
        grid = grid[ok]
        idx = idx[ok]
        bid_now = bid[idx]
        mid_now = 0.5 * (bid_now + ask[idx])
        sell = prints.get(asset)
        if sell is None:
            continue
        sell_ts, sell_price, sell_size = sell
        if sell_ts.size == 0:
            continue
        tick = _tick_of(bid[idx], ask[idx])
        if not np.isfinite(tick):
            continue
        quote = bid_now + tick
        half = 0.5 * tick
        for value in np.unique(quote):
            if value >= 1.0:
                continue
            selected = quote == value
            exact = sell_price <= value + half
            et, es = sell_ts[exact], sell_size[exact]
            if et.size == 0:
                continue
            cumulative = np.concatenate(([0.0], np.cumsum(es)))
            lo = np.searchsorted(et, grid[selected], side="right")
            hi = np.searchsorted(et, grid[selected] + horizon_s, side="right")
            flow = cumulative[hi] - cumulative[lo]
            hit = flow >= min_flow
            if not np.any(hit):
                continue
            target = cumulative[lo[hit]] + min_flow
            completing = np.searchsorted(cumulative, target, side="left") - 1
            completing = np.clip(completing, lo[hit], et.size - 1)
            fill_ts = et[completing]
            entry = _as_of(ts, bid, fill_ts, stale_s)
            exit_mark = _as_of(ts, bid, fill_ts + horizon_s, stale_s)
            good = np.isfinite(entry) & np.isfinite(exit_mark)
            placement = mid_now[selected][hit]
            for j in np.flatnonzero(good):
                records.append(
                    {
                        "asset": asset,
                        "ts_ns": float(fill_ts[j]) * 1e9,
                        "tick": float(tick),
                        "capture": float(placement[j]) - float(value),
                        "markout": float(entry[j] - exit_mark[j]),
                    }
                )
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_suffix(".tmp.parquet")
    pd.DataFrame(
        records,
        columns=["asset", "ts_ns", "tick", "capture", "markout"],
    ).to_parquet(temporary, index=False)
    temporary.replace(cache)
    return records, True


def fill_sequences(
    hours: Sequence[tuple[str, str]],
    *,
    grid_s: float,
    horizon_s: float,
    min_flow: float,
    stale_s: float = 60.0,
    workers: int = 1,
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Per-token fills of a resting buy at ``best_bid + tick``, in time order.

    A fill is a SELL print at or below the quote (within half a tick) whose
    cumulative size inside the horizon reaches the minimum order size: the
    quote is price priority over every bid at or below it.  Placements are on a
    grid of ``grid_s`` with the horizon equal to the grid, so one print cannot
    fill two placements of the same token and the resulting sequence is a
    genuine run of fills.  Captures and markouts are per share:
    ``capture = mid(placement) - quote`` and
    ``markout = bid(fill) - bid(fill + horizon)``, the same sign convention as
    ``ctf_maker_markout.json``.  Each finished hour is cached, so a bounded
    invocation resumes from the hours already replayed.
    """
    jobs = [
        (
            date,
            hour,
            float(grid_s),
            float(horizon_s),
            float(min_flow),
            float(stale_s),
            deadline,
        )
        for date, hour in hours
    ]
    records: list[dict[str, Any]] = []
    complete = True
    if workers <= 1 or len(jobs) <= 1:
        for job in jobs:
            part, done = _fill_hour(job)
            records.extend(part)
            complete = complete and done
        return records, complete
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        for part, done in pool.map(_fill_hour, jobs):
            records.extend(part)
            complete = complete and done
    return records, complete


def streak_analysis(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Run-length distribution of losing fills and the conditional next fill.

    ``net = capture - markout`` is the per-share result of a fill.  A fill is
    *losing* when ``net < 0``, i.e. when the measured adverse markout exceeds
    the spread capture.  The reported conditional is
    ``E[net of a fill | the n fills immediately before it were all losing]``;
    the threshold is the shortest run n at which that expectation is negative
    with a two-sided 95 % interval (Student t on n - 1 degrees of freedom)
    excluding zero.  Cells thinner than :data:`MIN_STREAK_SAMPLES` are reported
    but cannot set the threshold: a run of five fills is not a distribution.
    """
    if not records:
        return {"fills": 0}
    from scipy import stats

    frame = pd.DataFrame(records)
    frame["net"] = frame["capture"] - frame["markout"]
    frame = frame.sort_values(["asset", "ts_ns"])
    streak = np.zeros(frame.shape[0], dtype=np.int64)
    previous_asset = None
    run = 0
    for i, (asset, net) in enumerate(zip(frame["asset"], frame["net"])):
        if asset != previous_asset:
            run = 0
            previous_asset = asset
        streak[i] = run
        run = run + 1 if net < 0.0 else 0
    frame["streak"] = streak

    rows = []
    threshold = None
    net = frame["net"].to_numpy()
    markout = frame["markout"].to_numpy()
    for length in range(11):
        mask = frame["streak"].to_numpy() == length
        n = int(mask.sum())
        if n == 0:
            continue
        mean_next = float(np.mean(net[mask]))
        se = float(np.std(net[mask], ddof=1) / math.sqrt(n)) if n > 1 else float("nan")
        half_width = (
            float(stats.t.ppf(0.975, n - 1)) * se
            if n > 1 and math.isfinite(se) and se > 0.0
            else float("nan")
        )
        rows.append(
            {
                "streak": length,
                "n": n,
                "mean_fill_net": mean_next,
                "mean_fill_markout": float(np.mean(markout[mask])),
                "se_fill_net": se,
                "ci95": [mean_next - half_width, mean_next + half_width]
                if math.isfinite(half_width)
                else None,
                # Sufficient statistics: the pooled conditional mean, its
                # variance and the pooled markout add over partitions, so the
                # combiner can rebuild the run-length distribution and its
                # threshold without averaging per-partition means.
                "sum_fill_net": float(np.sum(net[mask])),
                "sum_fill_net_sq": float(np.sum(net[mask] ** 2)),
                "sum_fill_markout": float(np.sum(markout[mask])),
            }
        )
        # The threshold is the shortest run whose conditional mean is both
        # negative and distinguishable from zero: acting on a run whose
        # interval still contains zero would fire on noise.
        distinguishable = bool(
            math.isfinite(half_width) and mean_next + half_width < 0.0
        )
        if (
            threshold is None
            and length >= 1
            and n >= MIN_STREAK_SAMPLES
            and mean_next < 0.0
            and distinguishable
        ):
            threshold = length
    adverse = int((frame["net"] < 0.0).sum())
    return {
        "fills": int(frame.shape[0]),
        "adverse_fills": adverse,
        "adverse_share": float(adverse / max(frame.shape[0], 1)),
        "captures": int(frame["tick"].nunique()),
        "interval": "Student t, 95 %, n - 1 degrees of freedom",
        "min_cell": MIN_STREAK_SAMPLES,
        "run_length": rows,
        "threshold": threshold,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def _panel_frame(rows: list[dict[str, Any]], hourly: dict, hours: Sequence[tuple[str, str]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError("the event tape produced no OFI windows")
    frame["date"] = [
        pd.Timestamp(int(ts), unit="ns", tz="UTC").strftime("%Y-%m-%d")
        for ts in frame["ts_ns"]
    ]
    frame["hour"] = [
        pd.Timestamp(int(ts), unit="ns", tz="UTC").strftime("%H")
        for ts in frame["ts_ns"]
    ]
    volume = np.full(frame.shape[0], np.nan)
    covered: dict[tuple[str, str], int] = {}
    for date, hour in hours:
        totals, buckets = _trailing_volume(hourly, date, hour)
        covered[(date, hour)] = buckets
        mask = (frame["date"] == date) & (frame["hour"] == hour)
        volume[mask] = frame.loc[mask, "asset"].map(totals).to_numpy(dtype=np.float64)
    frame["volume_24h"] = volume
    frame["volume_coverage_hours"] = [
        covered.get((date, hour), 0) for date, hour in zip(frame["date"], frame["hour"])
    ]
    return frame


def _interval_text(interval: Sequence[float] | None) -> str:
    """A 95 % interval as text, for an artifact field that must survive a merge."""
    if not interval or len(interval) != 2:
        return "not measured"
    return f"[{float(interval[0]):.6g}, {float(interval[1]):.6g}]"


def _write_partial(
    path: Path,
    *,
    started: float,
    stage: str,
    hours: Sequence[tuple[str, str]],
    detail: str,
    windows: int,
) -> int:
    """Record an unfinished bounded run without publishing a measurement.

    The artifact is written ``partial: true`` so the refit combiner skips it: a
    fit that stopped inside its budget has no coefficient to report, and a
    guessed one would reach the live engine.  The finished per-hour work is
    cached, so the next invocation continues from it rather than re-reading the
    tape.
    """
    payload = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "elapsed_s": round(time.time() - started, 1),
        "partial": True,
        "stage": stage,
        "window": {
            "start": None,
            "end": None,
            "partitions": [f"{date}T{hour}" for date, hour in hours],
        },
        "sample_size": {"windows": int(windows), "hours": len(hours)},
        "source": {"events": str(EVENTS_ROOT), "trades": str(TRADES_ROOT)},
        "definition": {"partial": detail},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_jsonable(payload), indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    print(f"partial after {time.time() - started:.1f}s at stage {stage}: {detail}")
    return 0


def _parse_hours(values: Sequence[str]) -> list[tuple[str, str]]:
    out = []
    for value in values:
        if "T" not in value:
            raise ValueError(f"hour must be YYYY-MM-DDTHH, got {value!r}")
        date, hour = value.split("T")
        out.append((date, hour))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hours",
        nargs="+",
        default=["2026-11-06T05", "2026-11-06T09", "2026-11-06T13"],
        help="event partitions (YYYY-MM-DDTHH) to fit from",
    )
    parser.add_argument("--horizon", type=float, default=DEFAULT_HORIZON_S)
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--streak-hours", nargs="+", default=None)
    parser.add_argument("--streak-grid", type=float, default=None)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="processes for the per-hour panel and streak replay",
    )
    parser.add_argument("--probe", action="store_true", help="one part, count only")
    parser.add_argument("--panel", type=Path, default=PANEL_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=0.0,
        help=(
            "stop starting new hour work past this budget and write a partial "
            "artifact; 0 means no budget"
        ),
    )
    args = parser.parse_args(argv)

    started = time.time()
    deadline = started + args.max_seconds if args.max_seconds > 0.0 else None
    hours = _parse_hours(args.hours)
    if args.rebuild:
        # `--rebuild` means re-read the tape: drop the per-hour caches this run
        # would otherwise resume from.
        for date, hour in hours:
            for path in _panel_cache_path(date, hour, args.horizon):
                path.unlink(missing_ok=True)
    markout = load_maker_markout(DEFAULT_MAKER_MARKOUT_PATH)
    markout_horizon = _nearest_horizon(markout)
    action_horizon = _deepest_horizon(markout)
    # The fill model's minimum order size: the smallest flow the repo's
    # measured fill table prices, so the replay uses the same execution.
    min_flow = float(DEFAULT_MIN_FLOWS[0])
    # Reward accrual a resting bid earns per share per second, measured by the
    # queue-value replay in queue_fit.json. A pull forgoes it while it is out
    # of the book; with no measured rate the pull branch is missing that cost
    # and the artifact records `accrual_source: absent` rather than a number.
    queue_fit = _read_json(QUEUE_FIT_PATH)
    accrual_block = queue_fit.get("reward_accrual_usd_per_share_sec") or {}
    accrual_rate = accrual_block.get("p50") if isinstance(accrual_block, dict) else None
    if accrual_rate is None:
        accrual_rate = queue_fit.get("reward_accrual_usd_per_share_sec")
    accrual_source = (
        "queue_fit.json measured accrual" if accrual_rate is not None else "absent"
    )
    accrual_rate = float(accrual_rate) if accrual_rate is not None else 0.0
    if not args.probe and accrual_rate <= 0.0:
        # Pulling is only comparable to widening when the pull pays the reward
        # it forgoes while out of the book.  Without a measured rate the pull
        # branch is missing a cost, so the corner decision would be biased
        # toward pulling: refuse to publish either multiplier.
        print(
            "not identified: queue_fit.json carries no measured "
            "reward_accrual_usd_per_share_sec, so the pull branch cannot be "
            "priced; run the queue fit first"
        )
        return 1

    if args.probe:
        date, hour = hours[0]
        part = _event_parts(date, hour)[0]
        probe_started = time.time()
        states = _ofi_events(_tob_states(part))
        known = np.isfinite(states["qb"]) & np.isfinite(states["qa"])
        standardised: list[float] = []
        rows = _window_rows(
            states,
            horizon_s=args.horizon,
            stale_s=args.horizon,
            standardised=standardised,
        )
        moves = [r for r in rows if math.isfinite(r["mid1"]) and r["mid1"] != r["mid0"]]
        print(
            f"probe {part.name}: {states['ts'].size:,} states "
            f"({int(known.sum()):,} with both best sizes), {len(rows):,} windows "
            f"({len(moves):,} with a measured move), "
            f"{len(standardised):,} standardised events, "
            f"{time.time() - probe_started:.1f}s"
        )
        return 0

    streak_hours = _parse_hours(args.streak_hours) if args.streak_hours else hours
    # One placement per horizon: the grid is the shortest measured markout
    # horizon, so a print cannot fill two placements of the same token.
    streak_grid = float(args.streak_grid or markout_horizon)
    if args.rebuild:
        for date, hour in streak_hours:
            _streak_cache_path(date, hour, streak_grid, streak_grid).unlink(
                missing_ok=True
            )
    # The fill replay runs first: it is the other full read of the hour, it is
    # cached per hour, and running it before the panel means a bounded
    # invocation always advances the measurement instead of repeating the
    # replay after a panel-only run.
    records, streak_complete = fill_sequences(
        streak_hours,
        grid_s=streak_grid,
        horizon_s=streak_grid,
        min_flow=min_flow,
        workers=args.workers,
        deadline=deadline,
    )
    if not streak_complete:
        return _write_partial(
            args.output,
            started=started,
            stage="streak",
            hours=hours,
            detail=(
                "the invocation budget ended before every hour's fill replay; "
                f"the finished hours are cached under {STREAK_CACHE} and the "
                "next invocation resumes from them"
            ),
            windows=len(records),
        )
    streak = streak_analysis(records)
    print(f"streak fill sample: {streak.get('fills')}")

    std_path = args.panel.with_suffix(".std.parquet")
    if (
        args.panel.exists()
        and std_path.exists()
        and not args.rebuild
        and args.max_files is None
    ):
        panel = pd.read_parquet(args.panel)
        standardised = pd.read_parquet(std_path)["value"].to_numpy().tolist()
        log.info(
            f"panel: {panel.shape[0]:,} cached windows, "
            f"{len(standardised):,} standardised events from {args.panel}"
        )
    else:
        standardised = []
        rows, panel_complete = _collect_panel(
            hours,
            horizon_s=args.horizon,
            stale_s=args.horizon,
            max_files=args.max_files,
            standardised=standardised,
            deadline=deadline,
        )
        if not panel_complete:
            return _write_partial(
                args.output,
                started=started,
                stage="panel",
                hours=hours,
                detail=(
                    "the invocation budget ended before every hour partition was "
                    "measured; the finished hours are cached under "
                    f"{PANEL_CACHE} and the next invocation resumes from them"
                ),
                windows=len(rows),
            )
        hourly = _hourly_volume()
        panel = _panel_frame(rows, hourly, hours)
        if args.max_files is None:
            args.panel.parent.mkdir(parents=True, exist_ok=True)
            panel.to_parquet(args.panel, index=False)
            pd.DataFrame({"value": standardised}).to_parquet(std_path, index=False)
        log.info(f"panel: {panel.shape[0]:,} windows -> {args.panel}")

    if deadline is not None and time.time() > deadline:
        return _write_partial(
            args.output,
            started=started,
            stage="fit",
            hours=hours,
            detail=(
                "the invocation budget ended once the window panel was measured; "
                f"the panel is cached under {PANEL_CACHE} and the next invocation "
                "fits and replays from it"
            ),
            windows=int(panel.shape[0]),
        )

    z_score = standardised_quantile(standardised, TRIGGER_BUDGET)
    if z_score is None:
        z_score = float(panel["ofi_sum"].abs().max() or float("nan"))
        log.warning("standardised OFI empty; z_score falls back to the max window sum")

    ofi_fit = fit_ofi(panel)
    c_depth = ofi_fit.get("c_depth")
    v_ref = ofi_fit.get("v_ref")
    beta_vol = ofi_fit.get("beta_vol")
    if not all(v is not None and np.isfinite(v) and v > 0 for v in (c_depth, v_ref, beta_vol)):
        print(
            "not identified: impact/volume coefficients are missing -> "
            f"c_depth={c_depth} v_ref={v_ref} beta_vol={beta_vol}"
        )
        return 1

    # Decision events: the gate's window where the flow is toxic on the bid.
    threshold = (
        c_depth
        * panel["d_harm"].to_numpy()
        * np.power(
            np.maximum(panel["volume_24h"].fillna(v_ref).to_numpy(), 1e-9) / v_ref,
            beta_vol,
        )
    )
    panel["threshold"] = threshold
    # The gate only acts on a triggered window, so the replay prices the rule
    # on exactly the events where it chooses between pulling and widening.
    events = panel[
        (panel["ofi_sum"] < 0.0)
        & (panel["ofi_sum"].abs() >= panel["threshold"])
        & np.isfinite(panel["d_harm"])
        & np.isfinite(panel["bid0"])
        & np.isfinite(panel["bid1"])
        & np.isfinite(panel["micro0"])
        & np.isfinite(panel["mid0"])
    ].copy()
    print(f"triggered decision events: {events.shape[0]:,}")

    if deadline is not None and time.time() > deadline:
        return _write_partial(
            args.output,
            started=started,
            stage="replay",
            hours=hours,
            detail=(
                "the invocation budget ended before the pull/widen replay; the "
                "window panel and the OFI fit are reproducible from the cached "
                "panel, so the next invocation starts at the replay"
            ),
            windows=int(panel.shape[0]),
        )

    prints: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    action_fit: dict[str, Any] = {"events": 0}
    if not events.empty:
        prints = {}
        for date, hour in hours:
            for asset, series in _sell_prints(date, hour).items():
                prints.setdefault(asset, series)
        event_threshold = events["threshold"].to_numpy()
        action_fit = replay_actions(
            events,
            prints,
            markout=markout,
            horizon_s=action_horizon,
            min_flow=min_flow,
            threshold=event_threshold,
            accrual_per_share_sec=accrual_rate,
        )
        print(f"action replay best grid point: {action_fit['best_grid_point']}")
        action_fit["accrual_rate_source"] = accrual_source
        sensitivity = replay_actions(
            events,
            prints,
            markout=markout,
            horizon_s=markout_horizon,
            min_flow=min_flow,
            threshold=event_threshold,
            accrual_per_share_sec=accrual_rate,
        )
        # The established control: the same replay with the share price only.
        action_fit["sensitivity"] = {
            "horizon_s": sensitivity["horizon_s"],
            "best_grid_point": sensitivity["best_grid_point"],
            "corner": sensitivity["corner"],
            "pull_at_edge": sensitivity["pull_at_edge"],
            "monotone_in_pull": sensitivity["monotone_in_pull"],
            "pull_minus_widen": sensitivity["pull_minus_widen"],
        }

    optimum = action_fit.get("best_grid_point") or {}
    measured_pull = float(optimum.get("pull_multiplier", PULL_MULTIPLIER_GRID[-1]))
    measured_widen = float(optimum.get("widen_offset_multiplier", 0.0))
    corner = action_fit.get("corner")
    pull_at_corner = corner is not None
    widen_at_boundary = bool(action_fit.get("widen_at_boundary"))
    # The corner is a policy, not a number to read off: at the low pull edge the
    # measured rule is "pull on every triggered event, never widen", and at the
    # high edge it is "never pull, widen by the measured offset". Reporting the
    # grid value alone let a boundary look like an interior optimum.
    if corner == "pull_all":
        pull_multiplier = float(PULL_MULTIPLIER_GRID[0])
        policy = "pull on every triggered event; never widen"
    elif corner == "never_pull":
        pull_multiplier = float(PULL_MULTIPLIER_GRID[-1])
        policy = "never pull; widen by the measured offset"
    else:
        pull_multiplier = measured_pull
        policy = "interior optimum: pull only on the most extreme flow"
    # The engine refuses a non-positive widen offset, and 0 is the measured
    # boundary (a quote at the touch cannot be widened by less than nothing).
    # Under a pull-everything corner the widen branch is unreachable, so the
    # closest deployable offset is reported and the measured boundary travels
    # in the field beside it.
    positive_widen = [value for value in WIDEN_MULTIPLIER_GRID if value > 0.0]
    widen_multiplier = (
        measured_widen if measured_widen > 0.0 else float(min(positive_widen))
    )
    consecutive = streak.get("threshold")
    if consecutive is None:
        print("not identified: the adverse-fill run length never turns negative")
        return 1

    window_start = pd.Timestamp(int(panel["ts_ns"].min()), unit="ns", tz="UTC")
    window_end = pd.Timestamp(
        int(panel["ts_ns"].max()) + int(args.horizon * 1e9), unit="ns", tz="UTC"
    )
    sample = {
        "windows": int(panel.shape[0]),
        "tokens": int(panel["asset"].nunique()),
        "events": int(action_fit.get("events", 0)),
        # `fills` is deliberately not here: the streak replay is measured over
        # `streak_hours`, which is the same sample in every partial, and the
        # refit combiner sums every leaf under `sample_size`.  The count travels
        # in fields.consecutive_fills_threshold instead.
        "hours": [f"{d}T{h}" for d, h in hours],
        "streak_hours": [f"{d}T{h}" for d, h in streak_hours],
        "volume_windows": ofi_fit.get("volume_windows"),
        "trigger_windows": ofi_fit.get("windows"),
    }

    artifact = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "elapsed_s": round(time.time() - started, 1),
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "sample_size": sample,
        "source": {
            "events": str(EVENTS_ROOT),
            "trades": str(TRADES_ROOT),
            "markout": str(DEFAULT_MAKER_MARKOUT_PATH),
            "horizon_s": args.horizon,
            "markout_horizon_s": markout_horizon,
            "action_horizon_s": action_horizon,
            "streak_grid_s": streak_grid,
            "ofi_window_events": OFI_WINDOW_EVENTS,
            "trigger_budget": TRIGGER_BUDGET,
            "min_flow_shares": min_flow,
            "accrual_per_share_sec": accrual_rate,
            "accrual_rate_source": accrual_source,
            "pull_grid": list(PULL_MULTIPLIER_GRID),
            "widen_grid": list(WIDEN_MULTIPLIER_GRID),
        },
        "definition": {
            "ofi": "Cont-Kukanov-Stoikov level-1 order flow imbalance on the top of book, "
            "computed exactly as MicrostructureEngine.update: dQb adjusted for a bid "
            "price increase (new size) or decrease (-previous size), minus the mirror "
            "dQa for the ask.",
            "trigger": "logistic regression of the measured directional one-tick move "
            "(sign(dmid) == sign(OFI), |dmid| >= one tick) on log(|OFI|/d_harm) and "
            "log(trailing-24h volume) over non-overlapping reprice-horizon windows; "
            "c_depth and beta_vol are the coefficients that put the trigger at P(hit)="
            "0.5. This is the object the gate compares flow against; the linear "
            "impulse regression is reported as evidence and is not identified.",
            "aggregation": "the fit sums OFI over the reprice horizon (the decision "
            "interval the mid move is measured over); the engine compares its own "
            "10-event sum to the resulting threshold, so on a token whose events do "
            "not span exactly one horizon the two windows differ in length.",
            "v_ref": "geometric mean of the measured trailing-24h traded volume over the "
            "impact sample; the gauge that makes psi = 1 for the median-scale market.",
            "beta_vol": "-coef(log volume)/coef(log x): the multiple of the "
            "depth-normalised trigger added per e-fold of trailing-24h volume.",
            "z_score": "empirical (1 - trigger_budget) quantile of "
            "|sum of 10 event OFI| / pstdev(10 event OFI), the exact quantity the "
            "engine's statistical branch compares z_score against.",
            "pull_widen": action_fit.get("objective"),
            "pull_branch": "a pulled quote forgoes the net (capture - markout) of the "
            "fill it would have taken, pays the re-entry slippage max(0, bid(t+h) - "
            "bid(t)) it must cross to regain the position, and forgoes the measured "
            "reward accrual for the time it is out of the book",
            "widen_branch": "a widened quote keeps the measured markout of its tick "
            "and gains k x the measured micro-price drift of capture; the drift is "
            "not assumed to fall with the offset because that reduction is unmeasured",
            "streak": "conditional mean net (capture - markout) of the fill following a "
            "run of n consecutive losing fills, pooled over every hour in "
            "sample_size.streak_hours (one run-length distribution, not a mean of "
            "per-hour thresholds); the threshold is the shortest run at which that "
            "conditional mean is negative with a 95% interval excluding zero and "
            f"at least {MIN_STREAK_SAMPLES} fills.",
        },
        "ofi": {
            "c_depth": c_depth,
            "z_score": z_score,
            "v_ref": v_ref,
            "beta_vol": beta_vol,
            "pull_multiplier": pull_multiplier,
            "widen_offset_multiplier": widen_multiplier,
            "consecutive_fills_threshold": int(consecutive),
        },
        "fields": {
            "c_depth": {
                "value": c_depth,
                "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
                "sample_size": ofi_fit.get("windows"),
                "definition": "depth-normalised OFI at which P(one-tick move in the "
                "flow direction) reaches one half, times the volume scale at v_ref",
                "estimator": "logistic trigger fit, delta-method interval",
                # The interval is measured per partition. The refit combiner
                # writes a sample-weighted mean of the values and would carry
                # the first partial's interval beside it, so the intervals
                # travel as per-partition labels and no combined value is
                # published with an interval measured on one partition.
                "ci_by_partition": [
                    f"{date}T{hour}: {_interval_text(ofi_fit.get('c_depth_ci'))}"
                    for date, hour in hours
                ],
                "pseudo_r2": ofi_fit.get("trigger_pseudo_r2"),
                "sign_agreement_moving": ofi_fit.get("sign_agreement_moving"),
                "source": "clob_events price_change top-of-book deltas",
            },
            "v_ref": {
                "value": v_ref,
                "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
                "sample_size": ofi_fit.get("volume_windows"),
                "definition": "trailing-24h traded shares at which psi = 1",
                "estimator": "geometric mean of the sample's trailing-24h volume",
                "source": "clob_trades; hourly buckets covering the trailing 24h",
            },
            "beta_vol": {
                "value": beta_vol,
                "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
                "sample_size": ofi_fit.get("volume_windows"),
                "definition": "elasticity of the depth-normalised impact to 24h volume",
                "estimator": "ratio of the logistic volume and flow coefficients, delta-method interval",
                "ci_by_partition": [
                    f"{date}T{hour}: {_interval_text(ofi_fit.get('beta_vol_ci'))}"
                    for date, hour in hours
                ],
                "pseudo_r2": ofi_fit.get("trigger_pseudo_r2"),
                "source": "clob_events + clob_trades",
            },
            "z_score": {
                "value": z_score,
                "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
                "sample_size": len(standardised),
                "definition": f"standardised OFI quantile at the {TRIGGER_BUDGET:.0%} trigger budget",
                "estimator": "empirical quantile of |10-event OFI sum| / pstdev",
                "trigger_budget": TRIGGER_BUDGET,
                "source": "clob_events top-of-book deltas",
            },
            "pull_multiplier": {
                "value": pull_multiplier,
                "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
                "sample_size": action_fit.get("events"),
                "definition": "multiple of the OFI threshold above which the quote is pulled",
                "at_corner": pull_at_corner,
                "corner": corner,
                "measured_grid_point": measured_pull,
                "policy": policy,
                "monotone_in_pull": action_fit.get("monotone_in_pull"),
                "pull_curve_direction": action_fit.get("pull_curve_direction"),
                # Lists of strings are unioned by the refit combiner while
                # booleans and numbers are taken from the first partial, so the
                # per-partition agreement on the corner travels as labels.
                "corner_partitions": [f"{date}T{hour}" for date, hour in hours]
                if pull_at_corner
                else [],
                "monotone_partitions": [f"{date}T{hour}" for date, hour in hours]
                if action_fit.get("monotone_in_pull")
                else [],
                "policy_by_partition": [f"{date}T{hour}: {policy}" for date, hour in hours],
                "note": (
                    "corner solution, not an optimum: the best point of the replay "
                    "is on the pull grid edge, so the measured rule is "
                    f"{policy!r} and the written value is the grid end that "
                    "implements it. No grid-edge value is reported as an optimum; "
                    "monotone_in_pull is the evidence that the corner is the "
                    "tape's answer rather than a coarse grid's."
                )
                if pull_at_corner
                else "interior optimum: the objective has an interior minimum",
                "estimator": "grid search minimising the per-event replay regret",
                "grid": list(PULL_MULTIPLIER_GRID),
                "best_grid_point": action_fit.get("best_grid_point"),
                "objective_at_corner": action_fit.get("corner_objective"),
                "pull_curve": action_fit.get("pull_curve"),
                "pull_cost": (
                    "forgone net of the fill it would have taken "
                    "(capture - measured per-event markout) + re-entry slippage "
                    "max(0, bid(t+h) - bid(t)) + forgone reward accrual "
                    f"{action_fit.get('forgone_accrual_per_share')!r} per share"
                ),
                "accrual_per_share_sec": action_fit.get("accrual_per_share_sec"),
                "accrual_included": action_fit.get("accrual_included"),
                "pull_minus_widen": action_fit.get("pull_minus_widen"),
                "source": "clob_events + clob_trades + ctf_maker_markout.json",
            },
            "widen_offset_multiplier": {
                "value": widen_multiplier,
                "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
                "sample_size": action_fit.get("events"),
                "definition": "widening offset in multiples of the measured micro-price drift",
                "estimator": "grid search minimizing the same replay objective",
                "grid": list(WIDEN_MULTIPLIER_GRID),
                "at_boundary": widen_at_boundary,
                "measured_grid_point": measured_widen,
                "boundary_partitions": [f"{date}T{hour}" for date, hour in hours]
                if widen_at_boundary
                else [],
                "grid_point_by_partition": [
                    f"{date}T{hour}: {measured_widen:g}" for date, hour in hours
                ],
                "pooling": (
                    "the refit combiner writes the sample-weighted mean of the "
                    "per-partition measured grid points; the combined number is a "
                    "pooled policy parameter, not a single measured optimum, and "
                    "the per-partition values are in grid_point_by_partition"
                ),
                "note": (
                    "never widen: the measured optimum is the physical boundary 0 "
                    "(a quote at the touch cannot be widened further). The engine "
                    "requires a positive multiple, and under the pull-everything "
                    "corner this branch is never reached, so the smallest positive "
                    "offset is written and is behaviourally inert"
                )
                if widen_at_boundary
                else "interior optimum: the objective has an interior minimum",
                "best_grid_point": action_fit.get("best_grid_point"),
                "source": "clob_events + clob_trades + ctf_maker_markout.json",
            },
            "consecutive_fills_threshold": {
                "value": int(consecutive),
                "window": {
                    "start": pd.Timestamp(
                        int(min((r["ts_ns"] for r in records), default=0)), unit="ns", tz="UTC"
                    ).isoformat(),
                    "end": pd.Timestamp(
                        int(max((r["ts_ns"] for r in records), default=0)), unit="ns", tz="UTC"
                    ).isoformat(),
                },
                # The fill sample is pooled over `streak_hours` and identical in
                # every partial, so it is not named `sample_size` (which the
                # combiner sums) and carries its own hours beside it.
                "fills": streak.get("fills"),
                "streak_hours": [f"{d}T{h}" for d, h in streak_hours],
                "run_length": streak.get("run_length"),
                "min_cell": streak.get("min_cell"),
                "definition": "length of a losing-fill run after which the next "
                "fill's measured net (capture - markout) is negative with a 95% "
                "interval excluding zero",
                "estimator": "run-length distribution of (capture - markout) per fill",
                "source": "clob_events + clob_trades",
            },
        },
        "ofi_model": ofi_fit,
        "action_replay": action_fit,
        "adverse_streak": streak,
        # Raw sufficient statistics for the two quantiles the artifact's
        # fields are.  ``z_score`` is read off this histogram's merged CDF
        # (0.05 per bin) and the streak threshold off the additive sums in
        # ``adverse_streak.run_length``; neither is a per-partition quantile.
        "sufficient_statistics": {
            "schema": SCHEMA,
            "ofi_abs": linear_histogram(standardised, **Z_SCORE_HISTOGRAM),
            "streak_hours": [f"{d}T{h}" for d, h in streak_hours],
            "streak_fills": streak.get("fills"),
            "streak_min_cell": streak.get("min_cell"),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_jsonable(artifact), indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(f"wrote {args.output} in {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
