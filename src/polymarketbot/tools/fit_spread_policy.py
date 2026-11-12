"""Measure the maker spread policy: P(fill) and adverse markout by tick depth.

The spread optimizer has to answer one question with a number: how many ticks
from the mid should a rewarded quote rest at? The revenue side of that is the
venue's quadratic score (published, and priced by ``strategy.reward.scoring``);
the cost side is the adverse drift a *filled* leg pays. The fill probability
that connects them is not a free parameter either: a FIFO queue has no fill
parameter at all. A resting order sits behind whatever size is displayed at its
price when it lands, every later change to that level is observable, and every
print at or through the price is observable, so replaying
:class:`polymarketbot.risk.queue_position.QueuePositionTracker` over the L2 tape
*measures* P(fill) at every depth.

This tool is the depth-indexed version of ``fit_queue_model``'s queue-value
replay. That artifact places quotes at ``best_bid + tick`` and behind it
(offsets 0, 1, 2, keyed relative to the touch); the spread policy needs the same
measurement keyed on the **integer distance from the mid**, over the whole band
(``0 .. floor(v / tick)``), so it can price every candidate the optimizer is
allowed to pick.

Measurement, per scope ``(tick, rewardsMaxSpread)`` and depth ``k``:

``p_fill``
    Share of placements filled within ``horizon_s``. The quote price is the
    observed mid minus ``k`` ticks, floored onto the venue grid (never rounded
    up: the quote may not cross the mid), and never at or above the best ask.
    A placement is filled when the replayed FIFO consumption is positive, the
    same rule ``fit_queue_model`` uses.
``p_fill_ci_low`` / ``p_fill_ci_high``
    Wilson score interval at 95 %. Error is binomial: with ``f`` fills out of
    ``n`` placements the standard error is ``sqrt(p(1-p)/n)``; the Wilson form
    is used because depths inside the band routinely measure ``f = 0``, where
    the plain normal interval collapses to a point mass and would claim a
    measured certainty it does not have.
``adverse_markout_cents``
    For placements that filled, ``mid(at fill) - mid(at fill + horizon_s)`` in
    cents per share (positive = the drift a buy pays), with its standard error
    ``sd / sqrt(f)``. This is the ``ctf_maker_markout`` definition applied at
    the fill depth instead of at the touch.
``entry_edge_cents``
    ``mid(at fill) - quote``, the measured edge the fill is worth before the
    drift. Carried for the strategy's telemetry; the optimizer's stated
    objective does not credit it.

The measured arrival rate, cancellation intensity and print rate that drive a
FIFO queue are *inputs to this replay* (the tape is the arrival/cancel/print
process); ``queue_fit.json`` carries their pooled summary and is copied into
the artifact as provenance, not as a fitted parameter.

Cost is dominated by parsing book snapshots, as in ``fit_queue_model``. The
run is bounded (``--budget-seconds``, capped at 600) and a budget stop writes a
valid file with ``partial`` set. One compute process.

Usage::

    python -m polymarketbot.tools.fit_spread_policy --hours 13 --budget-seconds 60
    python -m polymarketbot.tools.fit_spread_policy --hours 13 --budget-seconds 600
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import UTC, datetime
from typing import Any

from polymarketbot.logs import get_logger
from polymarketbot.risk.queue_position import QueuePositionTracker
from polymarketbot.strategy.books import qualifying_score
from polymarketbot.tools.fit_queue_model import (
    DERIVED,
    TAPE,
    _as_book,
    _latest_partition,
    _mid_at,
    _with_own_order,
    load_books,
    load_prints,
    load_universe,
    mid_series,
)
from polymarketbot.tools.tape_reader import tape_files

log = get_logger("fit_spread_policy")

OUTPUT_PATH = DERIVED / "spread_policy_fill_by_ticks.json"
QUEUE_FIT_PATH = DERIVED / "queue_fit.json"

DEFAULT_HORIZON_S = 400.0
"""Trial and markout horizon: the measured fill horizon of ``queue_value.json``."""

DEFAULT_MAX_TICKS = 10
DEFAULT_SNAPSHOTS = 1_800
"""Placement snapshots to replay. Trials = snapshots x depths; the budget stop wins."""

MAX_BUDGET_SECONDS = 600.0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure P(fill) and adverse markout of a resting bid by tick depth",
    )
    parser.add_argument("--date", default=None, help="tape date partition (default: latest)")
    parser.add_argument(
        "--hours",
        default=None,
        help="comma-separated hour partitions, e.g. 12,13 (default: latest hour)",
    )
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=300.0,
        help=f"wall-clock budget (max {MAX_BUDGET_SECONDS:.0f})",
    )
    parser.add_argument(
        "--horizon-s", type=float, default=DEFAULT_HORIZON_S, help="fill/markout horizon"
    )
    parser.add_argument(
        "--max-ticks", type=int, default=DEFAULT_MAX_TICKS, help="deepest measured depth"
    )
    parser.add_argument(
        "--snapshots",
        type=int,
        default=DEFAULT_SNAPSHOTS,
        help="placement snapshots to sample across the window",
    )
    return parser.parse_args(argv)


def quote_price_at_depth(
    mid_price: float,
    depth_ticks: int,
    tick: float,
    best_ask: float,
) -> float | None:
    """Grid price of a bid ``depth_ticks`` ticks below ``mid_price``.

    The floor keeps the quote at or below the mid (a bid may never cross it) and
    at or below the venue grid; the epsilon absorbs ``0.505 / 0.001`` evaluating
    to ``504.9999...`` in binary floating point. ``None`` when the result is not
    a tradeable price or would rest at or through the best ask.
    """
    if tick <= 0.0:
        return None
    requested = float(mid_price) - float(depth_ticks) * float(tick)
    if requested <= 0.0:
        return None
    steps = math.floor(requested / tick + 1e-6)
    price = round(steps * tick, 6)
    if not 0.0 < price < 1.0:
        return None
    if price >= best_ask - 1e-12:
        return None
    return price


def _wilson(fills: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """95 % Wilson score interval for a binomial proportion (``(0, 0)`` -> ``(0, 1)``)."""
    if trials <= 0:
        return 0.0, 1.0
    p = fills / trials
    denominator = 1.0 + z * z / trials
    centre = (p + z * z / (2.0 * trials)) / denominator
    margin = z * math.sqrt(max(0.0, p * (1.0 - p) / trials + z * z / (4.0 * trials * trials))) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _binomial_stderr(fills: int, trials: int) -> float:
    """Standard error of a measured fill proportion."""
    if trials <= 0:
        return 0.0
    p = fills / trials
    return math.sqrt(max(0.0, p * (1.0 - p)) / trials)


def _bucket() -> dict[str, float]:
    return {
        "trials": 0.0,
        "fills": 0.0,
        "markout_sum": 0.0,
        "markout_sq_sum": 0.0,
        "markout_n": 0.0,
        "entry_sum": 0.0,
        "entry_n": 0.0,
    }


def _hazard_provenance(
    prints: dict[str, Any], window_s: float
) -> dict[str, Any]:
    """Copy the pooled FIFO rates the replay embodies, with their source path."""
    try:
        payload = json.loads(QUEUE_FIT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"source": str(QUEUE_FIT_PATH), "error": str(exc)}
    assets_with_prints = len(prints.get("by_asset") or {})
    measured_prints = int(prints.get("rows") or 0)
    return {
        "source": str(QUEUE_FIT_PATH),
        "generated_at": payload.get("generated_at"),
        "trade_arrival_rate_shares_sec": payload.get("trade_arrival_rate_shares_sec"),
        "cancellation_rate_intensity": payload.get("cancellation_rate_intensity"),
        "prints_measured": measured_prints,
        "assets_with_prints": assets_with_prints,
        "prints_per_sec_per_market": (
            measured_prints / (assets_with_prints * window_s)
            if assets_with_prints > 0 and window_s > 0.0
            else None
        ),
        "prints_per_sec_note": (
            "measured on this run's tape window; the replay consumes the print "
            "stream itself, so this is the summary of what it replayed"
        ),
    }


def replay_by_depth(
    *,
    universe: dict[str, dict[str, Any]],
    books: dict[str, Any],
    prints: dict[str, Any],
    series: dict[str, list[tuple[float, float]]],
    windows: dict[str, Any],
    horizon_s: float,
    max_ticks: int,
    sample_snapshots: int,
    deadline: float,
    started: float,
) -> tuple[dict[str, dict[int, dict[str, float]]], dict[str, Any]]:
    """Replay one bid per ``(scope, depth)`` per sampled snapshot; aggregate.

    ``windows`` carries, per scope, the running competing-score samples so the
    artifact states the book the fill probabilities were measured against.
    """
    token_to_condition = {
        token: condition_id
        for condition_id, params in universe.items()
        for token in params["tokens"]
    }
    candidates = [
        (asset, snap_index)
        for asset, snaps in books["book_rows"].items()
        if asset in token_to_condition
        for snap_index in range(len(snaps))
    ]
    if not candidates:
        return {}, {"candidates": 0, "snapshots": 0, "trials": 0, "truncated": False}

    stride = max(1, math.ceil(len(candidates) / max(1, sample_snapshots)))
    sampled = candidates[::stride]

    buckets: dict[str, dict[int, dict[str, float]]] = {}
    snapshots = 0
    trials = 0
    truncated = False

    for asset, snap_index in sampled:
        if time.time() >= deadline:
            truncated = True
            break
        params = universe[token_to_condition[asset]]
        snaps = books["book_rows"][asset]
        ts, bids, asks = snaps[snap_index]
        if not bids or not asks:
            continue
        best_bid_price = bids[0][0]
        best_ask_price = asks[0][0]
        if not (0.0 < best_bid_price < best_ask_price < 1.0):
            continue
        entry_mid = (best_bid_price + best_ask_price) * 0.5
        tick = float(params["tick"])
        band = float(params["max_spread"])
        size = float(params["min_size"])
        if size <= 0.0 or band <= 0.0 or tick <= 0.0:
            continue

        # Depths the reward band can pay for at all: ``band`` is in cents and
        # ``tick`` in dollars, so the reachable grid is ``band / (100 * tick)``.
        band_ticks = math.floor(band / (100.0 * tick) + 1e-9)
        prices: dict[int, float] = {}
        for depth in range(min(max_ticks, band_ticks) + 1):
            price = quote_price_at_depth(entry_mid, depth, tick, best_ask_price)
            if price is not None:
                prices[depth] = price
        if not prices:
            continue

        # Competing in-band score of the *pair* at this snapshot: the same
        # denominator the venue divides by, measured on both legs' books.
        other_leg = [token for token in params["tokens"] if token != asset]
        competing: float | None = None
        if other_leg:
            other_snaps = books["book_rows"].get(other_leg[0])
            if other_snaps:
                prior = [snap for snap in other_snaps if snap[0] <= ts]
                if prior:
                    other_bids, other_asks = prior[-1][1], prior[-1][2]
                    if other_bids and other_asks:
                        other_mid = (other_bids[0][0] + other_asks[0][0]) * 0.5
                        if 0.0 < other_mid < 1.0:
                            competing = qualifying_score(
                                _as_book(bids, asks),
                                max_spread_cents=band,
                                mid_price=entry_mid,
                            ) + qualifying_score(
                                _as_book(other_bids, other_asks),
                                max_spread_cents=band,
                                mid_price=other_mid,
                            )
        if competing is not None:
            for scope in (f"tick={tick:g}", f"tick={tick:g}|band={band:g}"):
                windows.setdefault(scope, []).append(competing)

        end = ts + horizon_s
        timeline: list[tuple[float, int, Any]] = [
            (snap[0], 0, snap) for snap in snaps if ts < snap[0] <= end
        ]
        timeline += [
            (p[0], 1, p) for p in prints["by_asset"].get(asset, []) if ts < p[0] <= end
        ]
        timeline.sort(key=lambda item: (item[0], item[1]))

        trackers: dict[int, QueuePositionTracker] = {}
        for depth, price in prices.items():
            tracker = QueuePositionTracker()
            tracker.on_entry("probe", asset, "BUY", price, size, ts, bids, asks)
            trackers[depth] = tracker
        filled_at: dict[int, float | None] = {depth: None for depth in trackers}

        for event_ts, kind, payload in timeline:
            for depth, tracker in trackers.items():
                before = tracker.progress("probe")
                if kind == 0:
                    tracker.on_book(
                        asset,
                        _with_own_order(payload[1], prices[depth], size),
                        payload[2],
                        event_ts,
                    )
                else:
                    _ts, print_price, print_size, side = payload
                    tracker.on_trade(asset, print_price, print_size, side, event_ts)
                after = tracker.progress("probe")
                if (
                    filled_at[depth] is None
                    and after is not None
                    and after.filled
                    and (before is None or not before.filled)
                ):
                    filled_at[depth] = event_ts

        snapshots += 1
        for depth, tracker in trackers.items():
            progress = tracker.progress("probe")
            if progress is None:
                continue
            if progress.filled and progress.traded_at_level <= progress.initial_ahead:
                consumed = size
            else:
                consumed = min(
                    size, max(0.0, progress.traded_at_level - progress.initial_ahead)
                )
            filled = consumed > 0.0
            markout_cents = 0.0
            markout_measured = False
            entry_cents = 0.0
            entry_measured = False
            if filled_at[depth] is not None:
                mids = series.get(asset)
                m_fill = _mid_at(mids, filled_at[depth])
                m_later = _mid_at(mids, filled_at[depth] + horizon_s)
                if m_fill is not None and m_later is not None:
                    markout_cents = (m_fill - m_later) * 100.0
                    markout_measured = True
                if m_fill is not None:
                    entry_cents = (m_fill - prices[depth]) * 100.0
                    entry_measured = True
            for scope in (f"tick={tick:g}", f"tick={tick:g}|band={band:g}"):
                row = buckets.setdefault(scope, {}).setdefault(depth, _bucket())
                row["trials"] += 1.0
                row["fills"] += 1.0 if filled else 0.0
                if markout_measured:
                    row["markout_sum"] += markout_cents
                    row["markout_sq_sum"] += markout_cents * markout_cents
                    row["markout_n"] += 1.0
                if entry_measured:
                    row["entry_sum"] += entry_cents
                    row["entry_n"] += 1.0
            trials += 1

    return buckets, {
        "candidates": len(candidates),
        "sampled": len(sampled),
        "snapshots": snapshots,
        "trials": trials,
        "truncated": truncated,
        "elapsed_s": round(time.time() - started, 2),
    }


def _rows(bucket_by_depth: dict[int, dict[str, float]]) -> list[dict[str, Any]]:
    """Aggregate one scope's buckets into the depth table, with binomial error."""
    rows: list[dict[str, Any]] = []
    for depth in sorted(bucket_by_depth):
        bucket = bucket_by_depth[depth]
        trials = int(bucket["trials"])
        fills = int(bucket["fills"])
        markout_n = int(bucket["markout_n"])
        if markout_n > 1:
            mean_markout = bucket["markout_sum"] / markout_n
            variance = max(
                0.0,
                bucket["markout_sq_sum"] / markout_n - mean_markout * mean_markout,
            )
            markout_stderr = math.sqrt(variance / markout_n)
        elif markout_n == 1:
            # A single fill cannot estimate its own dispersion. Reporting 0.0
            # would claim certainty, so the observation's own magnitude is
            # carried as its error (100 % relative, the conservative reading).
            mean_markout = bucket["markout_sum"]
            markout_stderr = abs(mean_markout)
        else:
            mean_markout = 0.0
            markout_stderr = 0.0
        entry_n = int(bucket["entry_n"])
        low, high = _wilson(fills, trials)
        rows.append(
            {
                "ticks": depth,
                "trials": trials,
                "fills": fills,
                "p_fill": (fills / trials) if trials else 0.0,
                "p_fill_stderr": _binomial_stderr(fills, trials),
                "p_fill_ci_low": low,
                "p_fill_ci_high": high,
                "markout_fills": markout_n,
                "adverse_markout_cents": mean_markout,
                "adverse_markout_stderr_cents": markout_stderr,
                "entry_edge_cents": (bucket["entry_sum"] / entry_n) if entry_n else 0.0,
                "entry_edge_n": entry_n,
            }
        )
    return rows


def _quantiles(values: list[float], qs: tuple[float, ...]) -> dict[str, float]:
    if not values:
        return {f"p{int(q * 100)}": 0.0 for q in qs}
    ordered = sorted(values)
    out = {}
    for q in qs:
        index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
        out[f"p{int(q * 100)}"] = ordered[index]
    return out


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    started = time.time()
    budget = min(MAX_BUDGET_SECONDS, max(1.0, float(args.budget_seconds)))
    deadline = started + budget
    horizon = max(1.0, float(args.horizon_s))
    max_ticks = max(0, int(args.max_ticks))
    sample = max(1, int(args.snapshots))

    all_event_files = tape_files(TAPE, table="clob_events")
    all_trade_files = tape_files(TAPE, table="clob_trades")
    if not all_event_files or not all_trade_files:
        raise FileNotFoundError("no clob_events or clob_trades parts under data/tape")
    date = args.date or _latest_partition(all_event_files)
    if args.hours:
        hours = [hour.strip() for hour in str(args.hours).split(",") if hour.strip()]
    else:
        hours = [all_event_files[-1].parent.name.split("=", 1)[1]]
    wanted = {f"hour={hour}" for hour in hours}
    event_files = [
        path
        for path in all_event_files
        if path.parent.parent.name == f"date={date}" and path.parent.name in wanted
    ]
    trade_files = [
        path
        for path in all_trade_files
        if path.parent.parent.name == f"date={date}" and path.parent.name in wanted
    ]
    if not event_files or not trade_files:
        raise FileNotFoundError(
            f"no parts for date={date} hours={hours} "
            f"(events={len(event_files)}, trades={len(trade_files)})"
        )
    print(
        f"spread policy: date={date} hours={hours} events={len(event_files)} part(s) "
        f"trades={len(trade_files)} part(s) horizon={horizon:g}s max_ticks={max_ticks} "
        f"budget={budget:.0f}s"
    )

    universe = load_universe()
    prints = load_prints(trade_files)
    books = load_books(event_files)
    series = mid_series(books)

    windows: dict[str, list[float]] = {}
    buckets, sample_stats = replay_by_depth(
        universe=universe,
        books=books,
        prints=prints,
        series=series,
        windows=windows,
        horizon_s=horizon,
        max_ticks=max_ticks,
        sample_snapshots=sample,
        deadline=deadline,
        started=started,
    )

    payload_window_start = min(
        (snap[0] for snaps in books["book_rows"].values() for snap in snaps), default=None
    )
    payload_window_end = max(
        (snap[0] for snaps in books["book_rows"].values() for snap in snaps), default=None
    )
    scopes: list[dict[str, Any]] = []
    for scope in sorted(buckets):
        rows = _rows(buckets[scope])
        tick_value: float | None = None
        band_value: float | None = None
        for part in scope.split("|"):
            key, _, value = part.partition("=")
            if key == "tick":
                tick_value = float(value)
            elif key == "band":
                band_value = float(value)
        competing_samples = windows.get(scope) or []
        scopes.append(
            {
                "scope": scope,
                "tick": tick_value,
                "max_spread_cents": band_value,
                "competing_score": _quantiles(competing_samples, (0.25, 0.5, 0.75)),
                "competing_samples": len(competing_samples),
                "rows": rows,
            }
        )

    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "elapsed_s": round(time.time() - started, 2),
        "budget_seconds": budget,
        "partial": bool(sample_stats.get("truncated")),
        "window": {
            "date": date,
            "hours": hours,
            "start": payload_window_start,
            "end": payload_window_end,
        },
        "source": {
            "events": [str(path) for path in event_files],
            "trades": [str(path) for path in trade_files],
            "universe": str(DERIVED / "ctf_census" / "universe.jsonl"),
        },
        "definition": {
            "observation": (
                "a resting buy at the snapshot mid minus k ticks (floored onto the "
                "venue grid, never at or through the best ask), FIFO position "
                "replayed with QueuePositionTracker over the next fill horizon"
            ),
            "p_fill": "share of placements filled within horizon_s (positive FIFO consumption)",
            "p_fill_error": (
                "binomial: stderr = sqrt(p(1-p)/n); 95% Wilson interval, which "
                "stays informative at the depths where zero fills are measured"
            ),
            "markout_error": (
                "sd/sqrt(fills) with at least two fills; a single fill cannot "
                "estimate its own dispersion, so its error is its own magnitude"
            ),
            "adverse_markout": (
                "mid(at fill) - mid(at fill + horizon_s), cents per share, over "
                "placements that filled; stderr = sd/sqrt(fills)"
            ),
            "entry_edge": "mid(at fill) - quote, cents per share (telemetry only)",
            "hazard": (
                "the replay is the arrival/cancel/print process of the tape; "
                "queue_fit.json's pooled rates are provenance, not fit parameters"
            ),
        },
        "horizon_s": horizon,
        "max_offset_ticks": max_ticks,
        "hazard": _hazard_provenance(
            prints,
            (payload_window_end or 0.0) - (payload_window_start or 0.0),
        ),
        "sample": sample_stats,
        "scopes": scopes,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    rows = sum(len(scope["rows"]) for scope in scopes)
    log.info(
        f"wrote {OUTPUT_PATH} ({len(scopes)} scopes, {rows} depth rows, "
        f"{sample_stats.get('trials', 0):,} placements, partial={payload['partial']}, "
        f"{payload['elapsed_s']}s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
