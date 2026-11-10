"""Measure the horizon-matched fill probability of a CTF maker quote.

The repo's ``data/derived/fill_model.json`` answers a *print-conditional*
question: given that a trade printed at a level, did it clear the queue ahead of
us?  That is not the question a resting quote asks.  A quote earns its edge only
if a print arrives at all, and the print-conditional model carries no arrival
rate - applied to a quote that rests for one engine tick it overstates the
per-leg fill probability by an order of magnitude (the paper soak quoted 1,262
legs per tick for 30 minutes and filled none).

This tool measures the horizon-matched quantity directly on the recorded L2
tape:

    observation = one placement of a buy at ``best_bid(t) + tick``
    fill        = a SELL print on that token with ``price <= quote`` and
                  ``ts in (t, t + horizon]`` whose size reaches the venue
                  minimum order (the paper engine's own fill rule, see
                  ``paper.pillars.reward.measured_fill_shares``)

Placements are sampled on the engine's own tick grid (``--grid-s``, default the
measured 5 s soak tick), so the sample is the population of quotes the engine
would actually have rested, not a hand-picked subset.  The best bid is read
as-of from the L2 event tape, not assumed from the print.

Output: ``data/derived/ctf_quote_fill_horizon.json`` - measured P(fill) for each
(horizon, tick, minimum order) cell with Clopper-Pearson bounds and full
coverage accounting.  Nothing here is a hand-set probability.
"""

from __future__ import annotations

import argparse
import glob
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from polymarketbot.logs import get_logger

log = get_logger("tools.ctf_quote_fill_horizon")

_REPO_ROOT = Path(__file__).resolve().parents[3]
TAPE_DIR = _REPO_ROOT / "data" / "tape"
DEFAULT_OUT = _REPO_ROOT / "data" / "derived" / "ctf_quote_fill_horizon.json"
DEFAULT_MARKOUT_OUT = _REPO_ROOT / "data" / "derived" / "ctf_maker_markout.json"

# Horizons are the requote lifetimes the engine can actually produce: one soak
# tick, a handful of ticks, and a full rotating-slice revisit.  They are
# measured durations (log: tick 4.4-5.1 s, full sweep every 48-52 ticks), not
# tuning knobs.
DEFAULT_HORIZONS_S: tuple[float, ...] = (5.0, 10.0, 30.0, 60.0, 225.0)
# Both Polymarket tick sizes are documented protocol constants; measuring the
# fill probability at each keeps the gate exact without inferring a market's
# tick from the tape.
DEFAULT_TICKS: tuple[float, ...] = (0.01, 0.001)
# Venue minimum order sizes to condition on.  A quote that fills fewer shares
# than the venue minimum is not a fill, and the paper engine discards it.
DEFAULT_MIN_FLOWS: tuple[float, ...] = (5.0, 20.0, 50.0, 200.0)


def _clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact binomial interval, so a sparse cell reports its uncertainty."""
    if n <= 0:
        return 0.0, 1.0
    if k <= 0:
        return 0.0, 1.0 - (alpha / 2.0) ** (1.0 / n)
    if k >= n:
        return (alpha / 2.0) ** (1.0 / n), 1.0
    from scipy.stats import beta  # local import: only the measurement needs scipy

    lo = beta.ppf(alpha / 2.0, k, n - k + 1)
    hi = beta.ppf(1.0 - alpha / 2.0, k + 1, n - k)
    return float(lo), float(hi)


def _partitions(kind: str, hours: Optional[Sequence[str]] = None) -> list[str]:
    """Hour partitions of one tape channel, oldest first."""
    pattern = str(TAPE_DIR / kind / "date=*" / "hour=*")
    parts = sorted(p for p in glob.glob(pattern) if Path(p).is_dir())
    if hours:
        keep = set(hours)
        parts = [p for p in parts if p.rsplit("hour=", 1)[-1] in keep]
    return parts


def _load_sell_prints(
    hours: Optional[Sequence[str]],
) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Per-token (ts, price, size) of every SELL print on the tape."""
    out: dict[str, list[tuple[float, float, float]]] = {}
    rows = 0
    for part in _partitions("clob_trades", hours):
        files = sorted(glob.glob(str(Path(part) / "*.parquet")))
        if not files:
            continue
        table = pq.read_table(files, columns=["recv_ts_ns", "asset_id", "side", "price", "size"])
        table = table.filter(pc.equal(pc.utf8_upper(table["side"]), "SELL"))
        if table.num_rows == 0:
            continue
        rows += table.num_rows
        for asset, ts, price, size in zip(
            table["asset_id"].to_pylist(),
            table["recv_ts_ns"].to_numpy(),
            table["price"].to_numpy(),
            table["size"].to_numpy(),
        ):
            if not asset or ts is None or price is None or size is None:
                continue
            if not np.isfinite(size) or size <= 0.0 or not np.isfinite(price):
                continue
            out.setdefault(asset, []).append((float(ts) * 1e-9, float(price), float(size)))
    log.info(f"tape SELL prints: {rows:,} rows over {len(out):,} tokens")
    series: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for asset, prints in out.items():
        prints.sort()
        series[asset] = (
            np.asarray([p[0] for p in prints], dtype=np.float64),
            np.asarray([p[1] for p in prints], dtype=np.float64),
            np.asarray([p[2] for p in prints], dtype=np.float64),
        )
    return series


def _append_best_bid(
    table: "pa.Table",
    assets: list[np.ndarray],
    stamps: list[np.ndarray],
    bids: list[np.ndarray],
) -> None:
    """Reduce one partition file's book events to per-token best bids in place."""
    changes = table.filter(pc.equal(table["event_type"], "price_change"))
    if changes.num_rows:
        lists = pa.concat_arrays(changes.column("price_changes").chunks)
        flat = lists.flatten()
        counts = pc.list_value_length(changes.column("price_changes")).to_numpy()
        parent_ts = changes["recv_ts_ns"].to_numpy()
        stamps.append(np.repeat(parent_ts, counts.astype(np.int64)))
        assets.append(flat.field("asset_id").to_numpy(zero_copy_only=False))
        bids.append(flat.field("best_bid").to_numpy(zero_copy_only=False).astype(np.float64))

    books = table.filter(pc.equal(table["event_type"], "book"))
    if books.num_rows:
        best: list[float] = []
        for levels in books.column("bids").to_pylist():
            best.append(max((float(lv["price"]) for lv in levels), default=np.nan))
        assets.append(np.asarray(books["asset_id"].to_pylist(), dtype=object))
        stamps.append(books["recv_ts_ns"].to_numpy())
        bids.append(np.asarray(best, dtype=np.float64))


def _best_bid_series(part: str) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Per-token (ts, best_bid) series from one hour of the L2 event tape.

    ``price_change`` rows carry their book deltas in a nested list whose every
    element repeats the token and the resulting best bid; ``book`` rows carry
    the full ladder.  Both are reduced to the best bid, so the as-of lookup at a
    placement time is exact without reconstructing a whole ladder.
    """
    files = sorted(glob.glob(str(Path(part) / "*.parquet")))
    if not files:
        return {}

    assets: list[np.ndarray] = []
    stamps: list[np.ndarray] = []
    bids: list[np.ndarray] = []

    # One file at a time: concatenating every chunk of an hour's `asset_id`
    # string column first overflows Arrow's 2 GiB offset space (measured
    # "offset overflow while concatenating arrays" on a 4-horizon re-run).
    for file in files:
        table = pq.read_table(
            file,
            columns=["recv_ts_ns", "asset_id", "event_type", "price_changes", "bids"],
        )
        _append_best_bid(table, assets, stamps, bids)

    if not assets:
        return {}

    asset_arr = np.concatenate(assets)
    ts_arr = np.concatenate(stamps).astype(np.float64) * 1e-9
    bid_arr = np.concatenate(bids)
    keep = np.isfinite(bid_arr) & (bid_arr > 0.0) & np.isfinite(ts_arr)
    asset_arr, ts_arr, bid_arr = asset_arr[keep], ts_arr[keep], bid_arr[keep]

    codes, uniques = _factorize(asset_arr)
    order = np.lexsort((ts_arr, codes))
    codes = codes[order]
    ts_arr = ts_arr[order]
    bid_arr = bid_arr[order]
    bounds = np.searchsorted(codes, np.arange(len(uniques) + 1))

    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for i, asset in enumerate(uniques):
        lo, hi = bounds[i], bounds[i + 1]
        ts = ts_arr[lo:hi]
        bid = bid_arr[lo:hi]
        # The as-of lookup only needs the final state at an instant.
        unique_ts = np.unique(ts)
        if unique_ts.size != ts.size:
            bid = bid[np.searchsorted(ts, unique_ts, side="right") - 1]
            ts = unique_ts
        out[str(asset)] = (ts, bid)
    return out


def _factorize(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Integer codes for an object array of token ids, stable and dependency-free."""
    uniques, first = np.unique(values, return_index=True)
    order = np.argsort(first)
    uniques = uniques[order]
    index = {value: i for i, value in enumerate(uniques)}
    codes = np.fromiter((index[value] for value in values), dtype=np.int64, count=values.size)
    return codes, uniques


# ---------------------------------------------------------------------------
# Decay of the parity opportunity over the scan gap
#
# ``_condition_legs`` maps a condition to its two traded tokens; ``_decay``
# measures, for every complementary pair and every horizon, how much of the
# complete-set parity edge observed at ``t`` still exists at ``t + horizon``.
# ---------------------------------------------------------------------------
# A bid last updated longer ago than this is not a live book state, so the pair
# observation at that grid point is dropped rather than carried forward.  60 s
# is the observed inter-update gap of a market that is still quoting, not a
# tuning parameter.
DECAY_MAX_QUOTE_AGE_S = 60.0


def _condition_legs(part: str) -> dict[str, tuple[str, str]]:
    """``condition_id -> (asset_a, asset_b)`` for binary conditions in one hour.

    Only conditions with exactly two traded tokens are complementary: a
    multi-outcome condition's legs do not sum to par.
    """
    files = sorted(glob.glob(str(Path(part) / "*.parquet")))
    if not files:
        return {}
    table = pq.read_table(files, columns=["condition_id", "asset_id"]).combine_chunks()
    out: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for condition, asset in zip(
        table["condition_id"].to_pylist(), table["asset_id"].to_pylist()
    ):
        if not condition or not asset:
            continue
        bucket = seen.setdefault(condition, set())
        if asset in bucket:
            continue
        bucket.add(asset)
        out.setdefault(condition, []).append(asset)
    return {
        condition: (assets[0], assets[1])
        for condition, assets in out.items()
        if len(assets) == 2
    }


def _decay(
    series: dict[str, tuple[np.ndarray, np.ndarray]],
    legs: dict[str, tuple[str, str]],
    grid_s: float,
    horizons: Sequence[float],
    tick: float,
    max_quote_age_s: float,
) -> tuple[dict[float, dict[str, float]], int, int]:
    """Survival of the complete-set parity edge over each horizon.

    For every complementary pair the two best-bid series are put on the grid
    ``t = start, start + grid_s, ...``.  At ``t`` the engine would quote each
    leg one tick above its best bid, so

        pair_edge(t) = 1 - (best_bid_a(t) + best_bid_b(t) + 2 * tick)

    is the per-share profit if both resting legs fill at those prices.  The
    measurement reports, over every grid point where ``pair_edge(t) > 0``:

    * ``survival_any``    - share where ``pair_edge(t + h)`` is still ``> 0``;
    * ``retained_edge``   - mean of ``max(pair_edge(t + h), 0) / pair_edge(t)``;
    * ``still_best_bid``  - share where both quotes are still at or above the
      best bid at ``t + h``, i.e. neither resting order fell behind the book.

    ``still_best_bid`` is the fill-relevant one: a quote that is behind the book
    is not first in queue, so it fills only on the way down.
    """
    cells: dict[float, dict[str, float]] = {
        h: {
            "observations": 0.0,
            "opportunities": 0.0,
            "survivals": 0.0,
            "retained_sum": 0.0,
            "best_bid_survivals": 0.0,
            "edge_sum": 0.0,
            "edge_future_sum": 0.0,
        }
        for h in horizons
    }
    pairs = 0
    observations = 0
    for condition, (asset_a, asset_b) in legs.items():
        ser_a = series.get(asset_a)
        ser_b = series.get(asset_b)
        if ser_a is None or ser_b is None:
            continue
        ts_a, bid_a = ser_a
        ts_b, bid_b = ser_b
        if ts_a.size < 2 or ts_b.size < 2:
            continue
        start = max(float(ts_a[0]), float(ts_b[0]))
        stop = min(float(ts_a[-1]), float(ts_b[-1]))
        if stop <= start:
            continue
        grid = np.arange(start, stop, grid_s)
        if grid.size < 2:
            continue
        ia = np.searchsorted(ts_a, grid, side="right") - 1
        ib = np.searchsorted(ts_b, grid, side="right") - 1
        ok = (ia >= 0) & (ib >= 0)
        if not np.any(ok):
            continue
        grid, ia, ib = grid[ok], ia[ok], ib[ok]
        a_bid, b_bid = bid_a[ia], bid_b[ib]
        # Drop pairs whose book state is older than the quote-age bound.
        fresh = (grid - ts_a[ia] <= max_quote_age_s) & (grid - ts_b[ib] <= max_quote_age_s)
        if not np.any(fresh):
            continue
        grid, a_bid, b_bid = grid[fresh], a_bid[fresh], b_bid[fresh]
        quote_a, quote_b = a_bid + tick, b_bid + tick
        edge = 1.0 - (quote_a + quote_b)
        if edge.size < 2:
            continue
        pairs += 1
        observations += int(edge.size)
        for h in horizons:
            shift = int(round(h / grid_s))
            if shift <= 0 or edge.size <= shift:
                continue
            # Only compare grid points that are exactly ``shift`` steps apart in
            # wall time, so a dropped (stale) observation cannot be counted.
            e_now = edge[:-shift]
            e_next = edge[shift:]
            ahead = np.abs((grid[shift:] - grid[:-shift]) - h) <= 1e-9
            opportunity = (e_now > 0.0) & ahead
            n = int(opportunity.sum())
            if n == 0:
                continue
            cell = cells[h]
            cell["observations"] += float(n)
            cell["opportunities"] += float(n)
            cell["survivals"] += float((e_next[opportunity] > 0.0).sum())
            cell["retained_sum"] += float(
                np.sum(np.maximum(e_next[opportunity], 0.0) / e_now[opportunity])
            )
            cell["best_bid_survivals"] += float(
                (
                    (a_bid[shift:][opportunity] <= quote_a[:-shift][opportunity])
                    & (b_bid[shift:][opportunity] <= quote_b[:-shift][opportunity])
                ).sum()
            )
            cell["edge_sum"] += float(e_now[opportunity].sum())
            cell["edge_future_sum"] += float(e_next[opportunity].sum())
    return cells, pairs, observations


def measure_decay(
    hours: Optional[Sequence[str]] = None,
    grid_s: float = 5.0,
    horizons: Sequence[float] = DEFAULT_HORIZONS_S,
    tick: float = 0.01,
    max_quote_age_s: float = DECAY_MAX_QUOTE_AGE_S,
) -> dict:
    """Measure how much of the parity edge survives each scan gap."""
    started = time.time()
    parts = _partitions("clob_events", hours)
    if not parts:
        raise FileNotFoundError(f"no clob_events partitions under {TAPE_DIR / 'clob_events'}")
    totals: dict[float, dict[str, float]] = {
        h: {
            "observations": 0.0,
            "opportunities": 0.0,
            "survivals": 0.0,
            "retained_sum": 0.0,
            "best_bid_survivals": 0.0,
            "edge_sum": 0.0,
            "edge_future_sum": 0.0,
        }
        for h in horizons
    }
    pairs = 0
    observations = 0
    hours_done = 0
    for part in parts:
        series = _best_bid_series(part)
        legs = _condition_legs(part)
        if not series or not legs:
            continue
        cells, hour_pairs, hour_obs = _decay(
            series, legs, grid_s, horizons, tick, max_quote_age_s
        )
        for h, cell in cells.items():
            for key, value in cell.items():
                totals[h][key] += value
        pairs += hour_pairs
        observations += hour_obs
        hours_done += 1
        log.info(
            f"decay {Path(part).parent.name}/{Path(part).name}: {hour_pairs:,} binary "
            f"pairs, {hour_obs:,} observations ({hours_done}/{len(parts)} hours, "
            f"{round(time.time() - started, 1)}s)"
        )

    cells_out = []
    for h in horizons:
        cell = totals[h]
        n = cell["opportunities"]
        lo, hi = _clopper_pearson(int(cell["survivals"]), int(n))
        cells_out.append(
            {
                "horizon_s": h,
                "tick": tick,
                "opportunities": int(n),
                "survivals": int(cell["survivals"]),
                "survival_any": (cell["survivals"] / n) if n else None,
                "survival_ci_low": lo if n else None,
                "survival_ci_high": hi if n else None,
                "retained_edge": (cell["retained_sum"] / n) if n else None,
                "still_best_bid": (cell["best_bid_survivals"] / n) if n else None,
                "mean_edge_at_t": (cell["edge_sum"] / n) if n else None,
                "mean_edge_at_horizon": (cell["edge_future_sum"] / n) if n else None,
            }
        )
    return {
        "elapsed_s": round(time.time() - started, 1),
        "definition": {
            "observation": (
                "a complementary pair at grid time t, both legs quoted at best_bid + tick, "
                "conditioned on pair_edge(t) > 0"
            ),
            "pair_edge": "1 - (best_bid_a + best_bid_b + 2*tick), per share, before fees and gas",
            "survival_any": "share of opportunities whose pair_edge is still > 0 at t + horizon",
            "retained_edge": "mean of max(pair_edge(t+h), 0) / pair_edge(t)",
            "still_best_bid": (
                "share of opportunities where both quotes now at t are still at or above the "
                "best bid at t + horizon"
            ),
            "grid_s": grid_s,
            "horizons_s": list(horizons),
            "tick": tick,
            "max_quote_age_s": max_quote_age_s,
            "hours_measured": hours_done,
            "partitions_matching": len(parts),
        },
        "coverage": {"binary_pairs": pairs, "observations": observations},
        "cells": cells_out,
    }


def _evaluate(
    series: dict[str, tuple[np.ndarray, np.ndarray]],
    prints: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
    grid_s: float,
    horizons: Sequence[float],
    ticks: Sequence[float],
    min_flows: Sequence[float],
) -> tuple[dict[tuple[float, float, float], list[int]], int, dict[float, list[int]]]:
    """Count placements and fills for every (horizon, tick, min_flow) cell."""
    # _evaluate fills a third slot (exact-price fills) that the decay path does
    # not use, so it carries the same width.
    cells: dict[tuple[float, float, float], list[int]] = {
        (h, t, f): [0, 0, 0] for h in horizons for t in ticks for f in min_flows
    }
    # Per-tick incidence: how many of the quoted tokens ever printed a SELL at
    # or below the quote at any point in the hour.  This is the most generous
    # horizon there is, i.e. "would this token have filled at all".
    incidence: dict[float, list[int]] = {t: [0, 0] for t in ticks}
    max_h = max(horizons)
    placements = 0
    for asset, (ts, bid) in series.items():
        if ts.size < 2:
            continue
        start, stop = ts[0], ts[-1] - max_h
        if stop <= start:
            continue
        grid = np.arange(start, stop, grid_s)
        if grid.size == 0:
            continue
        idx = np.searchsorted(ts, grid, side="right") - 1
        ok = idx >= 0
        grid, quote_bid = grid[ok], bid[idx[ok]]
        if grid.size == 0:
            continue
        placements += int(grid.size)
        empty = np.empty(0)
        sell_ts, sell_price, sell_size = prints.get(asset, (empty, empty, empty))

        for tick in ticks:
            quote = quote_bid + tick
            incidence[tick][0] += 1
            if sell_ts.size and np.any(sell_price <= float(quote.max()) + 1e-12):
                # The token printed at or below its best quote at some point.
                if float(quote.max()) >= float(sell_price.min()):
                    incidence[tick][1] += 1
            # Placements sharing a quote price share one filtered print series,
            # so the crossing test is exact and the work is one cumsum per
            # distinct quote price instead of one pass per placement.
            for quote_value in np.unique(quote):
                sel = quote == quote_value
                if sell_ts.size:
                    # Two events, both counted:
                    #   crossing - a SELL print at or below our price: what the
                    #              maker gate historically priced, an upper bound.
                    #   exact    - a SELL print AT our price (within half a tick):
                    #              the event the paper engine books, because the
                    #              tape records the maker price a taker got, so a
                    #              print at our price is a taker who hit the level
                    #              we stand on. A print strictly below it filled
                    #              somebody else.
                    # Measured 2026-11-10: exact is 10.75 % of crossing at a 225 s
                    # horizon, so a gate charging the crossing probability prices
                    # nine fills for every one the execution model can produce.
                    mask = sell_price <= quote_value + 1e-12
                    st, ss = sell_ts[mask], sell_size[mask]
                    half = 0.5 * float(tick)
                    exact_mask = (sell_price >= quote_value - half) & (
                        sell_price <= quote_value + half
                    )
                    et, es = sell_ts[exact_mask], sell_size[exact_mask]
                else:
                    st, ss = sell_ts, sell_size
                    et, es = sell_ts, sell_size
                if st.size:
                    cumulative = np.concatenate(([0.0], np.cumsum(ss)))
                    lo = np.searchsorted(st, grid[sel], side="right")
                    exact_cumulative = np.concatenate(([0.0], np.cumsum(es)))
                    exact_lo = np.searchsorted(et, grid[sel], side="right")
                    for horizon in horizons:
                        hi = np.searchsorted(st, grid[sel] + horizon, side="right")
                        flow = cumulative[hi] - cumulative[lo]
                        exact_hi = np.searchsorted(et, grid[sel] + horizon, side="right")
                        exact_flow = exact_cumulative[exact_hi] - exact_cumulative[exact_lo]
                        for min_flow in min_flows:
                            cell = cells[(horizon, tick, min_flow)]
                            cell[0] += int(sel.sum())
                            cell[1] += int((flow >= min_flow).sum())
                            cell[2] += int((exact_flow >= min_flow).sum())
                else:
                    for horizon in horizons:
                        for min_flow in min_flows:
                            cell = cells[(horizon, tick, min_flow)]
                            cell[0] += int(sel.sum())
    return cells, placements, incidence


# ---------------------------------------------------------------------------
# Measured adverse markout of one filled leg
#
# The maker gate prices a three-outcome bet, and the outcome that carries its
# tail is *one leg filling alone*: the measured exact-price fill probability is
# ~3e-4 per house horizon against ``p^2`` ~ 1e-7 for the lock.  Exiting that
# naked leg is not free.  The ledger marks it at the live best bid, one tick
# under the quote, and the mark drifts further while the leg is held.  This
# measures the drift on the same tape, with the same placement and fill rule as
# the fill table above:
#
#     placement = a resting buy at best_bid(t) + tick
#     fill      = the first SELL print at the quote (within half a tick) whose
#                 cumulative size since the placement reaches the venue minimum
#     markout   = best_bid(at fill) - best_bid(at fill + horizon)
#
# A positive markout is the per-share loss the drift inflicts on the leg.  It is
# reported separately from the entry slip (``quote - best_bid(at fill)``) so the
# gate's live one-tick unwind estimate can be checked against it rather than
# assumed.  Nothing is interpolated or extrapolated: a horizon without a
# measured cell has no markout, and the gate refuses without one.
# ---------------------------------------------------------------------------
def _as_of(
    stamps: np.ndarray, values: np.ndarray, targets: np.ndarray, stale_after_s: float
) -> np.ndarray:
    """Value as of each target time, NaN where the state is missing or stale."""
    index = np.searchsorted(stamps, targets, side="right") - 1
    out = np.full(targets.shape, np.nan, dtype=np.float64)
    ok = index >= 0
    if not np.any(ok):
        return out
    safe = np.maximum(index, 0)
    found = values[safe]
    age = targets - stamps[safe]
    keep = ok & np.isfinite(found) & (found > 0.0) & (age <= stale_after_s)
    out[keep] = found[keep]
    return out


def _markout_evaluate(
    series: dict[str, tuple[np.ndarray, np.ndarray]],
    prints: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
    grid_s: float,
    horizons: Sequence[float],
    ticks: Sequence[float],
    min_flow: float,
) -> dict[tuple[float, float], list[float]]:
    """Markout, entry slip and fill delay for every (horizon, tick) fill sample."""
    cells: dict[tuple[float, float], list[float]] = {
        (h, t): [0.0, 0.0, 0.0, 0.0, 0.0] for h in horizons for t in ticks
    }
    max_h = max(horizons)
    for asset, (ts, bid) in series.items():
        if ts.size < 2:
            continue
        start, stop = ts[0], ts[-1] - max_h
        if stop <= start:
            continue
        grid = np.arange(start, stop, grid_s)
        if grid.size == 0:
            continue
        idx = np.searchsorted(ts, grid, side="right") - 1
        ok = idx >= 0
        if not np.any(ok):
            continue
        grid, quote_bid = grid[ok], bid[idx[ok]]
        empty = np.empty(0)
        sell_ts, sell_price, sell_size = prints.get(asset, (empty, empty, empty))
        if sell_ts.size == 0:
            continue
        for tick in ticks:
            quote = quote_bid + tick
            half = 0.5 * float(tick)
            for quote_value in np.unique(quote):
                sel = quote == quote_value
                if not np.any(sel):
                    continue
                exact = (sell_price >= quote_value - half) & (
                    sell_price <= quote_value + half
                )
                et, es = sell_ts[exact], sell_size[exact]
                if et.size == 0:
                    continue
                cumulative = np.concatenate(([0.0], np.cumsum(es)))
                lo = np.searchsorted(et, grid[sel], side="right")
                for horizon in horizons:
                    hi = np.searchsorted(et, grid[sel] + horizon, side="right")
                    flow = cumulative[hi] - cumulative[lo]
                    filled = flow >= min_flow
                    if not np.any(filled):
                        continue
                    # First print that completes the minimum order for these
                    # placements; cumulative sums are monotone, so one lookup
                    # resolves it without a per-placement loop.  ``cum[k]`` is
                    # the size of ``et[:k]``, so the completing print is the
                    # one *before* the index where the target is first reached.
                    target = cumulative[lo[filled]] + min_flow
                    completing = np.searchsorted(cumulative, target, side="left") - 1
                    completing = np.maximum(completing, lo[filled])
                    completing = np.minimum(completing, et.size - 1)
                    fill_ts = et[completing]
                    entry = _as_of(ts, bid, fill_ts, horizon)
                    exit_mark = _as_of(ts, bid, fill_ts + horizon, horizon)
                    good = np.isfinite(entry) & np.isfinite(exit_mark)
                    if not np.any(good):
                        continue
                    drift = entry[good] - exit_mark[good]
                    slip = float(quote_value) - entry[good]
                    delay = fill_ts[good] - grid[sel][filled][good]
                    cell = cells[(horizon, tick)]
                    cell[0] += float(good.sum())
                    cell[1] += float(drift.sum())
                    cell[2] += float(np.square(drift).sum())
                    cell[3] += float(slip.sum())
                    cell[4] += float(delay.sum())
    return cells


def measure_markout(
    hours: Optional[Sequence[str]] = None,
    grid_s: float = 5.0,
    horizons: Sequence[float] = DEFAULT_HORIZONS_S,
    ticks: Sequence[float] = DEFAULT_TICKS,
    min_flow: float = DEFAULT_MIN_FLOWS[0],
) -> dict:
    """Measure the adverse markout of a single filled leg on the recorded tape."""
    started = time.time()
    prints = _load_sell_prints(hours)
    parts = _partitions("clob_events", hours)
    if not parts:
        raise FileNotFoundError(f"no clob_events partitions under {TAPE_DIR / 'clob_events'}")
    totals: dict[tuple[float, float], list[float]] = {
        (h, t): [0.0, 0.0, 0.0, 0.0, 0.0] for h in horizons for t in ticks
    }
    hours_done = 0
    for part in parts:
        series = _best_bid_series(part)
        if not series:
            continue
        hour_cells = _markout_evaluate(series, prints, grid_s, horizons, ticks, min_flow)
        for key, values in hour_cells.items():
            for i, value in enumerate(values):
                totals[key][i] += value
        hours_done += 1
        log.info(
            f"markout {Path(part).parent.name}/{Path(part).name}: {len(series):,} tokens "
            f"({hours_done}/{len(parts)} hours, {round(time.time() - started, 1)}s)"
        )

    by_cell = []
    for (horizon, tick), (n, drift_sum, drift_sq, slip_sum, delay_sum) in sorted(
        totals.items()
    ):
        count = int(n)
        mean_drift = (drift_sum / count) if count else None
        stderr = None
        if count > 1 and mean_drift is not None:
            variance = max(0.0, drift_sq / count - mean_drift * mean_drift)
            stderr = float(np.sqrt(variance / count))
        by_cell.append(
            {
                "horizon_s": horizon,
                "tick": tick,
                "fills": count,
                "mean_markout_usd_per_share": mean_drift,
                "stderr": stderr,
                "ci_low": (mean_drift - 1.96 * stderr) if stderr is not None else None,
                "ci_high": (mean_drift + 1.96 * stderr) if stderr is not None else None,
                "mean_entry_slip_usd_per_share": (slip_sum / count) if count else None,
                "mean_fill_delay_s": (delay_sum / count) if count else None,
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": round(time.time() - started, 1),
        "source": {
            "events_glob": str(TAPE_DIR / "clob_events" / "date=*" / "hour=*"),
            "trades_glob": str(TAPE_DIR / "clob_trades" / "date=*" / "hour=*"),
            "hours_measured": hours_done,
            "partitions_matching": len(parts),
            "hour_filter": list(hours) if hours else None,
        },
        "definition": {
            "placement": "a resting buy at best_bid(t) + tick, sampled every grid_s",
            "fill": (
                "the first SELL print at the quote (within half a tick) whose cumulative "
                "size since the placement reaches min_flow_shares"
            ),
            "markout": (
                "best_bid(at fill) - best_bid(at fill + horizon), USDC per share; positive "
                "is the adverse drift the single-leg branch pays after it fills"
            ),
            "entry_slip": "quote - best_bid(at fill), USDC per share",
            "stale_after_s": "a mark whose last book update is older than the horizon is dropped",
            "grid_s": grid_s,
            "horizons_s": list(horizons),
            "ticks": list(ticks),
            "min_flow_shares": min_flow,
        },
        "coverage": {
            "fills": int(sum(cell["fills"] for cell in by_cell)),
            "cells_with_fills": int(sum(1 for cell in by_cell if cell["fills"])),
        },
        "cells": by_cell,
    }


def measure(
    hours: Optional[Sequence[str]] = None,
    grid_s: float = 5.0,
    horizons: Sequence[float] = DEFAULT_HORIZONS_S,
    ticks: Sequence[float] = DEFAULT_TICKS,
    min_flows: Sequence[float] = DEFAULT_MIN_FLOWS,
) -> dict:
    """Run the measurement over the recorded tape and return the artifact."""
    started = time.time()
    prints = _load_sell_prints(hours)
    parts = _partitions("clob_events", hours)
    if not parts:
        raise FileNotFoundError(f"no clob_events partitions under {TAPE_DIR / 'clob_events'}")

    # [placements, crossing fills, exact-price fills]
    cells: dict[tuple[float, float, float], list[int]] = {
        (h, t, f): [0, 0, 0] for h in horizons for t in ticks for f in min_flows
    }
    placements = 0
    tokens = 0
    hours_done = 0
    incidence_total: dict[float, list[int]] = {t: [0, 0] for t in ticks}
    for part in parts:
        series = _best_bid_series(part)
        if not series:
            continue
        hour_cells, hour_placements, hour_incidence = _evaluate(
            series, prints, grid_s, horizons, ticks, min_flows
        )
        for key, (n, k, k_exact) in hour_cells.items():
            cells[key][0] += n
            cells[key][1] += k
            cells[key][2] += k_exact
        for tick, (quoted, crossed) in hour_incidence.items():
            incidence_total[tick][0] += quoted
            incidence_total[tick][1] += crossed
        placements += hour_placements
        tokens = max(tokens, len(series))
        hours_done += 1
        log.info(
            f"{Path(part).parent.name}/{Path(part).name}: {len(series):,} tokens, "
            f"{hour_placements:,} placements ({hours_done}/{len(parts)} hours, "
            f"{round(time.time() - started, 1)}s)"
        )

    by_cell = []
    for (horizon, tick, min_flow), (n, k, k_exact) in sorted(cells.items()):
        lo, hi = _clopper_pearson(k, n)
        exact_lo, exact_hi = _clopper_pearson(k_exact, n)
        by_cell.append(
            {
                "horizon_s": horizon,
                "tick": tick,
                "min_flow_shares": min_flow,
                "placements": n,
                # crossing: a print at or below the quote (historical definition;
                # an upper bound on what a resting order can receive).
                "fills": k,
                "p_fill": (k / n) if n else None,
                "ci_low": lo if n else None,
                "ci_high": hi if n else None,
                # exact: a print at the quote, the event the paper execution model
                # books and therefore the one the maker gate must price.
                "fills_exact": k_exact,
                "p_fill_exact": (k_exact / n) if n else None,
                "ci_low_exact": exact_lo if n else None,
                "ci_high_exact": exact_hi if n else None,
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": round(time.time() - started, 1),
        "source": {
            "events_glob": str(TAPE_DIR / "clob_events" / "date=*" / "hour=*"),
            "trades_glob": str(TAPE_DIR / "clob_trades" / "date=*" / "hour=*"),
            "hours_measured": hours_done,
            "partitions_matching": len(parts),
            "hour_filter": list(hours) if hours else None,
        },
        "definition": {
            "observation": "a resting buy at best_bid(t) + tick, sampled every grid_s on the L2 tape",
            "fill": (
                "a SELL print with price <= quote and ts in (t, t+horizon] whose size "
                "reaches min_flow_shares"
            ),
            "grid_s": grid_s,
            "horizons_s": list(horizons),
            "ticks": list(ticks),
            "min_flows_shares": list(min_flows),
            "note": (
                "the print-conditional model in fill_model.json carries no arrival rate; "
                "these cells are the horizon-matched probability the maker gate needs"
            ),
        },
        "incidence": [
            {
                "tick": tick,
                "quoted_tokens": quoted,
                "tokens_with_a_crossing_print": crossed,
                "incidence": (crossed / quoted) if quoted else None,
            }
            for tick, (quoted, crossed) in sorted(incidence_total.items())
        ],
        "coverage": {
            "placements": placements,
            "max_tokens_per_hour": tokens,
            "sell_prints": int(sum(ts.size for ts, _, _ in prints.values())),
            "tokens_with_sell_prints": len(prints),
        },
        "cells": by_cell,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--grid-s", type=float, default=5.0)
    parser.add_argument(
        "--hours", nargs="*", default=None,
        help="restrict to these hour partitions (e.g. 00 12); default all",
    )
    parser.add_argument(
        "--decay-only", action="store_true",
        help="measure parity-edge decay and merge it into the existing artifact",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help=(
            "merge only the requested cells into the existing artifact instead of "
            "rewriting it; a bounded run must never destroy measurements it did not take"
        ),
    )
    parser.add_argument(
        "--horizons", nargs="*", type=float, default=None,
        help=(
            "requote horizons to measure; pass the operating scan gap "
            "(sweep_ticks * measured tick) when it exceeds the default 225 s"
        ),
    )
    parser.add_argument(
        "--markout", action="store_true",
        help=(
            "measure the adverse markout of a single filled leg instead of the fill "
            "table, writing --markout-out"
        ),
    )
    parser.add_argument("--markout-out", default=str(DEFAULT_MARKOUT_OUT))
    args = parser.parse_args(argv)

    if args.markout:
        # Measure the horizons the shipped fill table can price, so the gate
        # always has a markout cell for a horizon it could quote at.  Reading
        # them off the table keeps the two artifacts on one grid without a
        # hand-written list.
        if args.horizons:
            horizons = tuple(args.horizons)
        elif Path(args.out).exists():
            table = json.loads(Path(args.out).read_text(encoding="utf-8"))
            horizons = tuple(sorted({float(c["horizon_s"]) for c in table["cells"]}))
        else:
            horizons = DEFAULT_HORIZONS_S
        artifact = measure_markout(
            hours=args.hours, grid_s=args.grid_s, horizons=horizons
        )
        target = Path(args.markout_out)
        if args.merge and target.exists():
            prior = json.loads(target.read_text(encoding="utf-8"))
            keyed = {(c["horizon_s"], c["tick"]): c for c in prior.get("cells", [])}
            for cell in artifact["cells"]:
                keyed[(cell["horizon_s"], cell["tick"])] = cell
            prior["cells"] = [keyed[key] for key in sorted(keyed)]
            prior["last_merge"] = {
                "generated_at": artifact["generated_at"],
                "source": artifact["source"],
                "coverage": artifact["coverage"],
                "horizons": list(horizons),
            }
            artifact = prior
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(artifact, fh, indent=2)
        log.info(f"wrote {target} ({artifact['coverage']['fills']:,} measured fills)")
        for cell in artifact["cells"]:
            log.info(
                f"  horizon {cell['horizon_s']:>5.0f}s tick {cell['tick']:<6g} "
                f"markout={cell['mean_markout_usd_per_share']} "
                f"slip={cell['mean_entry_slip_usd_per_share']} n={cell['fills']:,}"
            )
        return 0

    horizons = tuple(args.horizons) if args.horizons else DEFAULT_HORIZONS_S

    out = Path(args.out)
    if args.decay_only:
        artifact = json.loads(out.read_text(encoding="utf-8"))
        artifact["decay"] = measure_decay(
            hours=args.hours, grid_s=args.grid_s, horizons=horizons
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(artifact, fh, indent=2)
        for cell in artifact["decay"]["cells"]:
            log.info(
                f"  gap {cell['horizon_s']:>5.0f}s  survive={cell['survival_any']} "
                f"[{cell['survival_ci_low']}, {cell['survival_ci_high']}] "
                f"kept={cell['retained_edge']} best_bid={cell['still_best_bid']} "
                f"n={cell['opportunities']:,}"
            )
        log.info(f"wrote {out} decay section")
        return 0

    artifact = measure(
        hours=args.hours, grid_s=args.grid_s, horizons=horizons
    )
    if args.merge and out.exists():
        prior = json.loads(out.read_text(encoding="utf-8"))
        keyed = {
            (cell["horizon_s"], cell["tick"], cell["min_flow_shares"]): cell
            for cell in prior.get("cells", [])
        }
        replaced = 0
        for cell in artifact["cells"]:
            key = (cell["horizon_s"], cell["tick"], cell["min_flow_shares"])
            if key in keyed:
                replaced += 1
            keyed[key] = cell
        prior["cells"] = [keyed[key] for key in sorted(keyed)]
        prior["last_merge"] = {
            "generated_at": artifact["generated_at"],
            "source": artifact["source"],
            "coverage": artifact["coverage"],
            "horizons": list(horizons),
            "cells_replaced": replaced,
        }
        artifact = prior
        log.info(
            f"merged {len(horizons)} horizon(s) at {args.grid_s:g}s grid: "
            f"{replaced} cell(s) replaced, {len(keyed)} cell(s) total"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, indent=2)
    log.info(
        f"wrote {out} ({artifact['coverage']['placements']:,} placements, "
        f"{artifact['coverage']['sell_prints']:,} SELL prints, "
        f"{artifact['source']['hours_measured']}/{artifact['source']['partitions_matching']} hours"
    )
    for cell in artifact["cells"]:
        if cell["tick"] == 0.01 and cell["min_flow_shares"] == 5.0:
            log.info(
                f"  horizon {cell['horizon_s']:>5.0f}s  p_fill={cell['p_fill']} "
                f"[{cell['ci_low']}, {cell['ci_high']}] n={cell['placements']:,} k={cell['fills']:,}"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
