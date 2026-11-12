"""Fit the de-skew inventory model from the recorded CLOB tape.

Every coefficient in ``data/derived/deskew_calibration.json`` is computed here
from ``data/tape/{clob_trades,clob_events}``; the de-skew optimizer refuses to
act without it because each coefficient decides when inventory is moved at a
measured execution cost.

What is measured (and how)
--------------------------
*Book.* For every ``price_change`` event the tape carries the post-change best
bid / best ask. After dropping rows with a zero side or a crossed book, the
per-asset top-of-book series is ``(recv_ts_ns, best_bid, best_ask)``.

*Print replay.* Each print is a fill opportunity for a resting quote at the
touch. A ``SELL`` print at or below the prevailing bid fills a bid, a ``BUY``
print at or above the ask fills an ask. The entry capture per filled share is
``mid - price`` for a bid and ``price - mid`` for an ask (the half-spread the
quote earns at the mid). The adverse loss is the same mark-to-mid move after a
holding horizon ``H``: ``max(0, mid - mid(t+H))`` for a bid fill and
``max(0, mid(t+H) - mid)`` for an ask fill. Net capture is capture minus
adverse loss, i.e. ``mid(t+H) - price`` for a bid.

*Horizon.* ``H`` is the measured round-trip holding time: prints are matched
FIFO, a bid fill against the next ask fill and vice versa, and ``H`` is the
median matched holding. The same fill record gives the inventory path ``q``
(signed share count, each fill capped at one measured quote size), which is
bucketed to measure the loss distribution *conditional on inventory*.

Fitted fields
-------------
* ``q_soft`` - the first inventory bucket whose mean adverse loss per share
  exceeds the measured half-spread capture (the "expected adverse cost exceeds
  capture" boundary).
* ``q_hard`` - the first bucket whose adverse loss at the stated loss
  probability ``alpha`` (``--loss-probability``, the single stated preference)
  exceeds the capture. Both are measured; neither is a preference.
* ``gamma`` - solved from the Guéant-Tapia-Manziadi / Avellaneda-Stoikov
  objective so that the risk skew at ``q_hard`` equals the measured execution
  friction: ``gamma = friction / (q_hard * sigma^2_ref)`` with
  ``sigma^2_ref = p_ref (1 - p_ref) / tau_ref_days`` the bounded binary
  variance at the measured median mid and measured median remaining life. The
  measured probability that the horizon loss exceeds capture at ``q_hard`` is
  reported alongside; ``alpha`` is the one number stated rather than measured.
* ``skew_aggressiveness`` - replaying the quote skew ``phi * spread * level``
  over a grid of ``phi`` at each inventory level; the fitted value maximises
  measured net capture per quote placement. When the maximiser is the grid's
  zero edge the measured policy is never to skew the quote (rest at the touch):
  the artifact carries ``corner: true`` and the zero is a measured corner, not
  a missing value. A sample in which no placement fills at any gain is refused
  as unmeasured.
* ``q_soft_bankroll_fraction`` / ``q_hard_risk_fraction`` /
  ``merge_bankroll_fraction`` - the fitted inventory (shares x mid) divided by
  the measured capital committed to the measured universe (two-sided notional
  at the measured quote size). The runtime bankroll is deployment capital, so
  the fit expresses the threshold as a share of it.
* ``q_hard_absorption_quotes`` - ``q_hard`` in units of the measured quote
  size (median print size); ``q_hard_floor_quotes`` - ``q_hard / q_soft``.
* ``reserve_usable_fraction`` - ``1 - gas_per_merge / (q_hard * mid)``, the
  share of the crossing cost the measured gas does not consume.
* ``holding_*`` - from the measured round-trip holding time: the median in
  units of mean inter-print time (turnover cycles), the 10th/90th percentiles
  as the min/max clamp, and the 90th percentile of holding / remaining life as
  the expiry fraction.
* ``merge_*`` - the batch below which one merge's gas exceeds the measured
  crossing saving is ``gas / (spread/2)``; the fitted batch is that break-even
  raised to the measured median fill and the venue minimum order size.
  ``merge_gas_fraction`` is gas per share at the fitted batch as a fraction of
  the spread, ``merge_quote_multiples`` the batch in quote-size units.
* ``ofi_toxicity_threshold`` / ``drift_toxicity_cents`` - the ``1 - alpha``
  quantile of the Cont-Kukanov-Stoikov rolling 10-event OFI and of the Stoikov
  micro-price drift, measured on the reconstructed L1 sizes. If
  ``data/derived/risk_calibration.json`` already carries these two fields they
  are reused so the risk gate and the de-skew gate agree.

Usage
-----
    python -m polymarketbot.tools.fit_deskew_calibration \
        --dates 2026-11-04,2026-11-05,2026-11-06 --hours 07 \
        --out data/derived/deskew_calibration.json

A missing sample is an error: the tool never writes an artifact without the
sample sizes that produced it.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from polymarketbot.logs import get_logger
from polymarketbot.strategy.reward.deskew import load_deskew_calibration
from polymarketbot.tools.pooling_stats import SCHEMA, log_histogram

log = get_logger("fit_deskew")

DEFAULT_OUT = Path("data/derived/deskew_calibration.json")
DEFAULT_RISK = Path("data/derived/risk_calibration.json")
UNIVERSE_DIR = Path("data/tape/universe")

#: The venue's published minimum order size (``orderMinSize``), shares. A
#: fetched protocol constant, not a fitted coefficient.
VENUE_MIN_ORDER_SHARES = 5.0

#: Interior inventory levels (fraction of the passive-skew range) the skew
#: replay is evaluated at. A design grid, not a fitted constant.
_SKEW_LEVELS = (0.25, 0.5, 0.75, 1.0)

#: Grid the skew gain is solved on: the quote offset is a fraction of the
#: half-spread, so the domain is (0, 1].
_SKEW_GRID = tuple(round(0.005 * i, 3) for i in range(201))

#: Fixed grids of the distributions the artifact's quantiles are read from.
#: Every partition uses them, so the partial histograms add exactly and the
#: pooled quantile is the merged CDF's quantile.  ``per_decade`` bins a decade
#: apart gives a ~2.3 % relative bin width, which is the resolution the
#: combined artifact reports next to each quantile.
HOLDING_HISTOGRAM = {"lo": 0.01, "hi": 1e7, "per_decade": 100}
EXPIRY_HISTOGRAM = {"lo": 1e-9, "hi": 1.0, "per_decade": 100}
PRINT_RATE_HISTOGRAM = {"lo": 1e-4, "hi": 1e4, "per_decade": 50}
OFI_TOXICITY_HISTOGRAM = {"lo": 1e-3, "hi": 1e6, "per_decade": 50}
DRIFT_TOXICITY_HISTOGRAM = {"lo": 1e-3, "hi": 100.0, "per_decade": 50}


@dataclass
class HourTape:
    """One hour of top-of-book series and prints, per asset."""

    books: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]
    trades: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]]
    condition_of: dict[str, str]
    n_book_events: int


@dataclass
class FillSample:
    """One filled quote: inventory before the fill, capture and loss."""

    asset_id: str
    abs_q: float
    capture: float
    loss: float
    ts_ns: int


@dataclass
class RoundTrip:
    """A FIFO-matched bid/ask pair: entry time and holding time."""

    asset_id: str
    entry_ts_ns: int
    holding_ns: int


def _hour_globs(
    dates: Sequence[str], hours: Sequence[str]
) -> list[tuple[str, str, list[str], list[str]]]:
    """Available (date, hour, event parts, trade parts) partitions, sorted."""
    out = []
    for date in dates:
        for hour in hours:
            trades = sorted(
                Path(f"data/tape/clob_trades/date={date}/hour={hour}").glob("*.parquet")
            )
            events = sorted(
                Path(f"data/tape/clob_events/date={date}/hour={hour}").glob("*.parquet")
            )
            if trades and events:
                out.append((date, hour, [str(p) for p in events], [str(p) for p in trades]))
    return out


def _load_hour(
    con: duckdb.DuckDBPyConnection, events: list[str], trades: list[str]
) -> HourTape | None:
    """Read one hour and rebuild the per-asset top-of-book and print series."""
    book_rows = con.execute(
        f"""
        with traded as (select distinct asset_id from read_parquet({trades!r}))
        select asset_id, ts, bid, ask from (
            select (unnest(price_changes)).asset_id asset_id,
                   recv_ts_ns ts,
                   (unnest(price_changes)).best_bid bid,
                   (unnest(price_changes)).best_ask ask
            from read_parquet({events!r}) where event_type = 'price_change'
        )
        where bid > 0 and ask > 0 and ask > bid
          and asset_id in (select asset_id from traded)
        order by asset_id, ts
        """
    ).fetchall()
    trade_rows = con.execute(
        f"""
        select asset_id, condition_id, recv_ts_ns, price, size, side
        from read_parquet({trades!r})
        where price > 0 and size > 0
        order by asset_id, recv_ts_ns
        """
    ).fetchall()
    if not book_rows or not trade_rows:
        return None

    books_raw: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for asset_id, ts, bid, ask in book_rows:
        books_raw[str(asset_id)].append((int(ts), float(bid), float(ask)))
    trades_raw: dict[str, list[tuple[int, float, float, str]]] = defaultdict(list)
    condition_of: dict[str, str] = {}
    for asset_id, condition_id, ts, price, size, side in trade_rows:
        key = str(asset_id)
        trades_raw[key].append((int(ts), float(price), float(size), str(side)))
        condition_of[key] = str(condition_id)

    books = {
        asset_id: (
            np.array([r[0] for r in rows], dtype=np.int64),
            np.array([r[1] for r in rows], dtype=float),
            np.array([r[2] for r in rows], dtype=float),
        )
        for asset_id, rows in books_raw.items()
        if len(rows) >= 2
    }
    trades_series = {
        asset_id: (
            np.array([r[0] for r in rows], dtype=np.int64),
            np.array([r[1] for r in rows], dtype=float),
            np.array([r[2] for r in rows], dtype=float),
            [r[3] for r in rows],
        )
        for asset_id, rows in trades_raw.items()
        if rows
    }
    return HourTape(
        books=books,
        trades=trades_series,
        condition_of=condition_of,
        n_book_events=len(book_rows),
    )


def _load_trades(
    con: duckdb.DuckDBPyConnection, trades: list[str]
) -> tuple[dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]], dict[str, str]]:
    """Read one hour of prints only (the book pass is the expensive one)."""
    rows = con.execute(
        f"""
        select asset_id, condition_id, recv_ts_ns, price, size, side
        from read_parquet({trades!r})
        where price > 0 and size > 0
        order by asset_id, recv_ts_ns
        """
    ).fetchall()
    grouped: dict[str, list[tuple[int, float, float, str]]] = defaultdict(list)
    condition_of: dict[str, str] = {}
    for asset_id, condition_id, ts, price, size, side in rows:
        key = str(asset_id)
        grouped[key].append((int(ts), float(price), float(size), str(side)))
        condition_of[key] = str(condition_id)
    series = {
        asset_id: (
            np.array([r[0] for r in items], dtype=np.int64),
            np.array([r[1] for r in items], dtype=float),
            np.array([r[2] for r in items], dtype=float),
            [r[3] for r in items],
        )
        for asset_id, items in grouped.items()
        if items
    }
    return series, condition_of


def measure_round_trips(
    trades: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]],
) -> list[RoundTrip]:
    """FIFO match bid fills against ask fills; holding time in nanoseconds.

    A print is a fill opportunity at the touch, so a SELL print is matched
    against a later BUY print (and vice versa) exactly as it would clear the
    two-sided inventory a quote builds. Unmatched prints are coverage, not a
    holding time.
    """
    out: list[RoundTrip] = []
    for asset_id, (ts, _price, _size, sides) in trades.items():
        open_bids: list[int] = []
        open_asks: list[int] = []
        for i, side in enumerate(sides):
            if side == "SELL":
                open_bids.append(int(ts[i]))
                if open_asks:
                    entry = open_asks.pop(0)
                    out.append(RoundTrip(asset_id, entry, int(ts[i]) - entry))
            elif side == "BUY":
                open_asks.append(int(ts[i]))
                if open_bids:
                    entry = open_bids.pop(0)
                    out.append(RoundTrip(asset_id, entry, int(ts[i]) - entry))
    return out


def replay_fills(
    books: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
    trades: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]],
    *,
    horizon_ns: int,
    quote_size: float,
) -> tuple[list[FillSample], list[tuple[float, float, float, float, str]]]:
    """Replay touch quotes against the prints.

    Returns the inventory-conditional fill samples and the raw skew-replay
    records ``(spread, mid, mid_at_h, price, side)`` for every print.
    """
    fills: list[FillSample] = []
    opportunities: list[tuple[float, float, float, float, str]] = []
    for asset_id, (ts, px, sz, sides) in trades.items():
        book = books.get(asset_id)
        if book is None:
            continue
        book_ts, bid, ask = book
        idx = np.searchsorted(book_ts, ts, side="right") - 1
        idx_h = np.searchsorted(book_ts, ts + horizon_ns, side="right") - 1
        valid = (idx >= 0) & (idx_h >= 0)
        if not np.any(valid):
            continue
        q = 0.0
        for i in range(len(ts)):
            if not valid[i]:
                continue
            j, k = int(idx[i]), int(idx_h[i])
            mid = (bid[j] + ask[j]) / 2.0
            mid_h = (bid[k] + ask[k]) / 2.0
            spread = float(ask[j] - bid[j])
            price = float(px[i])
            side = sides[i]
            opportunities.append((spread, mid, mid_h, price, side))
            if side == "SELL" and price <= bid[j] + 1e-9:
                capture, loss = mid - bid[j], max(0.0, mid - mid_h)
                q += min(sz[i], quote_size)
            elif side == "BUY" and price >= ask[j] - 1e-9:
                capture, loss = ask[j] - mid, max(0.0, mid_h - mid)
                q -= min(sz[i], quote_size)
            else:
                continue
            fills.append(FillSample(asset_id, abs(q), float(capture), float(loss), int(ts[i])))
    return fills, opportunities


def solve_bucket_thresholds(
    fills: Iterable[FillSample],
    *,
    quote_size: float,
    alpha: float,
    min_bucket: int,
) -> dict[str, Any]:
    """Inventory at which measured adverse loss overtakes spread capture.

    Buckets are geometric in the measured quote size. ``q_soft`` is the first
    bucket whose mean loss exceeds the median capture; ``q_hard`` the first
    whose ``1 - alpha`` loss quantile does. When even the smallest measured
    bucket already exceeds the bound, the threshold is that bucket and the
    measured exceedance probability is reported - the honest answer when the
    tape shows no safe inventory band above one quote size.
    """
    rows = list(fills)
    if not rows:
        return {"measurable": False, "reason": "no fills in the sample"}
    abs_q = np.array([r.abs_q for r in rows], dtype=float)
    loss = np.array([r.loss for r in rows], dtype=float)
    capture = np.array([r.capture for r in rows], dtype=float)
    capture_ref = float(np.median(capture))
    if capture_ref <= 0.0:
        return {"measurable": False, "reason": "measured capture is not positive"}

    edges = quote_size * (2.0 ** np.arange(0, 14))
    buckets: list[dict[str, Any]] = []
    q_soft = q_hard = None
    exceed_at_hard = None
    for lo, hi in pairwise(edges):
        mask = (abs_q >= lo) & (abs_q < hi)
        n = int(mask.sum())
        if n < min_bucket:
            continue
        mean_loss = float(loss[mask].mean())
        tail_loss = float(np.quantile(loss[mask], 1.0 - alpha))
        exceed = float(np.mean(loss[mask] > capture[mask]))
        buckets.append(
            {
                "lower_shares": round(float(lo), 2),
                "upper_shares": round(float(hi), 2),
                "n": n,
                "mean_loss_per_share": round(mean_loss, 6),
                "loss_quantile_per_share": round(tail_loss, 6),
                "measured_exceedance_probability": round(exceed, 6),
            }
        )
        if q_soft is None and mean_loss > capture_ref:
            q_soft = float(hi)
        if q_hard is None and tail_loss > capture_ref:
            q_hard = float(hi)
            exceed_at_hard = exceed
    if q_soft is None:
        q_soft = float(edges[0])
    if q_hard is None:
        q_hard = float(edges[0])
        mask = abs_q < edges[1]
        exceed_at_hard = float(np.mean(loss[mask] > capture[mask])) if mask.any() else None
    return {
        "measurable": True,
        "capture_per_share": round(capture_ref, 6),
        "q_soft_shares": round(max(q_soft, quote_size), 2),
        "q_hard_shares": round(max(q_hard, quote_size), 2),
        "measured_exceedance_probability_at_q_hard": exceed_at_hard,
        "buckets": buckets,
        "bucket_edges": [round(float(e), 2) for e in edges],
    }


def solve_skew_aggressiveness(
    opportunities: Sequence[tuple[float, float, float, float, str]],
) -> dict[str, Any]:
    """Gain maximising measured net capture per placement, per inventory level.

    ``net = mid(t+H) - quote`` for a bid and ``quote - mid(t+H)`` for an ask,
    with ``quote = bid + phi*spread*level`` (bid side) or
    ``ask - phi*spread*level`` (ask side). The objective is the sum over every
    quote placement divided by the number of placements, so a tighter quote
    must earn its extra fills.
    """
    if not opportunities:
        return {"measurable": False, "reason": "no quote placements in the sample"}
    spreads = np.array([o[0] for o in opportunities])
    mids_h = np.array([o[2] for o in opportunities])
    prices = np.array([o[3] for o in opportunities])
    sides = np.array([o[4] for o in opportunities])
    bid = np.array([o[1] - o[0] / 2.0 for o in opportunities])
    ask = np.array([o[1] + o[0] / 2.0 for o in opportunities])
    placements = len(opportunities)
    is_sell = sides == "SELL"
    per_level: dict[str, Any] = {}
    totals = np.zeros(len(_SKEW_GRID))
    fill_totals = np.zeros(len(_SKEW_GRID))
    for level in _SKEW_LEVELS:
        rows = []
        for g, phi in enumerate(_SKEW_GRID):
            buy_quote = bid + phi * spreads * level
            sell_quote = ask - phi * spreads * level
            filled_buy = is_sell & (prices <= buy_quote + 1e-9)
            filled_sell = (~is_sell) & (prices >= sell_quote - 1e-9)
            net = np.where(filled_buy, mids_h - buy_quote, 0.0) + np.where(
                filled_sell, sell_quote - mids_h, 0.0
            )
            total = float(net.sum())
            rows.append(
                {
                    "phi": phi,
                    "net_capture_per_placement": total / placements,
                    "fills": int(filled_buy.sum() + filled_sell.sum()),
                }
            )
            totals[g] += total
            fill_totals[g] += int(filled_buy.sum() + filled_sell.sum())
        best = max(rows, key=lambda r: (r["net_capture_per_placement"], -r["phi"]))
        per_level[f"{level:g}"] = best
    best_index = int(np.argmax(totals))
    if not fill_totals.any():
        return {
            "measurable": False,
            "reason": "no quote placement fills at any gain; the skew gain is unmeasured",
        }
    best_phi = _SKEW_GRID[best_index]
    corner = best_index == 0
    return {
        "measurable": True,
        "gain": float(best_phi),
        "corner": bool(corner),
        "policy": (
            "never skew the quote: rest at the touch"
            if corner
            else "interior optimum"
        ),
        "fills_at_gain": int(fill_totals[best_index]),
        "objective": "net capture per quote placement, summed over inventory levels",
        "placements": placements,
        "grid": [0.0, float(_SKEW_GRID[-1])],
        # Additive sufficient statistics: the pooled objective at one grid
        # point is sum(net_total) / sum(placements), and the pooled argmax is
        # taken on those sums rather than on a mean of per-partition optima.
        "grid_totals": [
            {
                "phi": float(phi),
                "net_total": float(totals[index]),
                "fills": int(fill_totals[index]),
            }
            for index, phi in enumerate(_SKEW_GRID)
        ],
        "per_level": per_level,
    }


def measure_toxicity_tails(
    events_parts: Sequence[str],
    con: duckdb.DuckDBPyConnection,
    *,
    alpha: float,
    traded_parts: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Tail of the Cont-Kukanov-Stoikov OFI and Stoikov drift on the tape.

    L1 sizes are reconstructed from the price-change stream exactly as the
    engine consumes them: a change at the current best updates that side's
    size, a change that improves the best replaces it. When ``traded_parts`` is
    given the measurement is restricted to markets the tape shows trading,
    because those are the only markets a resting quote can be gated in.
    """
    if not events_parts:
        return {"measurable": False, "reason": "no event partitions"}
    traded_filter = (
        f"and asset_id in (select distinct asset_id from read_parquet({list(traded_parts)!r}))"
        if traded_parts
        else ""
    )
    table = con.execute(
        f"""
        select * from (
            select (unnest(price_changes)).asset_id asset_id,
                   recv_ts_ns ts,
                   (unnest(price_changes)).price price,
                   (unnest(price_changes)).size size,
                   (unnest(price_changes)).side side,
                   (unnest(price_changes)).best_bid bid,
                   (unnest(price_changes)).best_ask ask
            from read_parquet({list(events_parts)!r}) where event_type = 'price_change'
        )
        where asset_id is not null {traded_filter}
        order by asset_id, ts
        """
    ).fetchnumpy()
    assets = table["asset_id"]
    if len(assets) == 0:
        return {"measurable": False, "reason": "no price-change rows"}
    price = np.asarray(table["price"], dtype=float)
    size = np.asarray(table["size"], dtype=float)
    side = table["side"]
    best_bid = np.array(
        [np.nan if v is None else float(v) for v in table["bid"]], dtype=float
    )
    best_ask = np.array(
        [np.nan if v is None else float(v) for v in table["ask"]], dtype=float
    )

    valid_spread = (best_ask - best_bid)[(best_bid > 0.0) & (best_ask > best_bid)]
    if valid_spread.size == 0:
        return {"measurable": False, "reason": "no two-sided book rows"}
    # A static cents threshold is only meaningful for the spread regime it is
    # measured in: the engine's own drift cap is a fraction of the half-spread,
    # so the tail is measured on the interquartile spread band of the tape.
    spread_lo, spread_hi = (float(v) for v in np.quantile(valid_spread, [0.25, 0.75]))
    ofi_values: list[float] = []
    drift_values: list[float] = []
    window: list[float] = []
    current_asset: Any = None
    bid = ask = 0.0
    bid_size = ask_size = 0.0
    prev_bid = prev_ask = 0.0
    prev_bs = prev_as = 0.0
    for i in range(len(assets)):
        b, a = best_bid[i], best_ask[i]
        if not (b > 0.0 and a > b):
            continue
        key = assets[i]
        if key != current_asset:
            current_asset = key
            window.clear()
            bid, ask = b, a
            bid_size = ask_size = 0.0
            prev_bid = prev_ask = 0.0
            prev_bs = prev_as = 0.0
        p, s = price[i], size[i]
        if math.isnan(p) or math.isnan(s):
            continue
        if side[i] == "BUY":
            if p >= b:
                bid_size = s
        elif side[i] == "SELL":
            if p <= a:
                ask_size = s
        else:
            continue
        bid, ask = b, a
        if prev_bid > 0.0 and prev_ask > 0.0:
            delta_b = (
                bid_size
                if bid > prev_bid
                else (bid_size - prev_bs if bid == prev_bid else -prev_bs)
            )
            delta_a = (
                ask_size if ask < prev_ask else (ask_size - prev_as if ask == prev_ask else -prev_as)
            )
            window.append(delta_b - delta_a)
            if len(window) > 10:
                window.pop(0)
            spread = ask - bid
            if spread_lo <= spread <= spread_hi:
                ofi_values.append(abs(sum(window)))
                mid = (bid + ask) / 2.0
                depth = bid_size + ask_size
                if depth > 0.0:
                    micro = (bid_size * ask + ask_size * bid) / depth
                    drift_values.append(abs(micro - mid) * 100.0)
        prev_bid, prev_ask = bid, ask
        prev_bs, prev_as = bid_size, ask_size
    if len(ofi_values) < 100:
        return {"measurable": False, "reason": f"only {len(ofi_values)} OFI events"}
    return {
        "measurable": True,
        "ofi_toxicity_threshold": float(np.quantile(ofi_values, 1.0 - alpha)),
        "drift_toxicity_cents": float(np.quantile(drift_values, 1.0 - alpha)),
        "samples": len(ofi_values),
        "ofi_median": float(np.median(ofi_values)),
        "drift_median_cents": float(np.median(drift_values)),
        # Raw distributions behind the two tail quantiles.  Both have a mass at
        # exactly zero (a window with balanced flow or a micro price on the
        # mid), which the log grids carry as ``zeros``.
        "ofi_histogram": log_histogram(ofi_values, **OFI_TOXICITY_HISTOGRAM),
        "drift_histogram": log_histogram(
            drift_values, **DRIFT_TOXICITY_HISTOGRAM
        ),
        "spread_band_dollars": [spread_lo, spread_hi],
        "window_events": 10,
    }


def _snapshot_time(path: Path) -> datetime:
    """``20261104T101010`` -> aware UTC datetime (the snapshot's own label)."""
    try:
        return datetime.strptime(path.stem[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=UTC)


def _snapshot_time(path: Path) -> datetime:
    """``20261104T101010`` -> aware UTC datetime (the snapshot's own label)."""
    try:
        return datetime.strptime(path.stem[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=UTC)


def load_end_epoch_seconds(
    condition_ids: Iterable[str], *, near: datetime, max_snapshots: int = 3
) -> dict[str, float]:
    """Market end time per condition from the universe snapshot nearest the tape.

    Snapshots are large JSON documents, so only the closest labels are read and
    the first one carrying a condition wins; end dates are static per market.
    """
    wanted = set(condition_ids)
    snaps = [p for p in UNIVERSE_DIR.glob("*.json") if p.name != "universe_state.json"]
    if not snaps or not wanted:
        return {}
    ranked = sorted(snaps, key=lambda p: abs((_snapshot_time(p) - near).total_seconds()))
    out: dict[str, float] = {}
    for path in ranked[:max_snapshots]:
        if len(out) == len(wanted):
            break
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for market in payload.get("markets", []):
            cid = market.get("condition_id")
            end = market.get("end_date")
            if cid not in wanted or not end or str(cid) in out:
                continue
            try:
                end_dt = datetime.fromisoformat(str(end))
            except ValueError:
                continue
            out[str(cid)] = end_dt.timestamp()
    return out


def _expiry_ratios(
    trips: Sequence[RoundTrip],
    condition_of: dict[str, str],
    end_epochs: dict[str, float],
) -> list[float]:
    """Measured holding time relative to remaining life at entry, per trip."""
    ratios = []
    for trip in trips:
        cond = condition_of.get(trip.asset_id)
        end_epoch = end_epochs.get(cond) if cond is not None else None
        if end_epoch is None:
            continue
        remaining_seconds = end_epoch - trip.entry_ts_ns / 1e9
        if remaining_seconds <= 0.0:
            continue
        ratios.append((trip.holding_ns / 1e9) / remaining_seconds)
    return ratios


def _expiry_fraction(
    trips: Sequence[RoundTrip],
    condition_of: dict[str, str],
    end_epochs: dict[str, float],
) -> float | None:
    """90th percentile of holding time relative to remaining life at entry."""
    ratios = _expiry_ratios(trips, condition_of, end_epochs)
    if not ratios:
        return None
    return float(min(1.0, max(1e-9, np.quantile(ratios, 0.90))))


def solve_gamma(
    *, friction: float, q_hard_shares: float, p_ref: float, tau_days: float
) -> float:
    """A-S/GTM risk aversion whose inventory skew equals friction at ``q_hard``.

    The reservation-price skew is ``gamma * sigma^2 * q``; requiring it to equal
    the measured execution friction at the measured hard limit gives
    ``gamma = friction / (q_hard * sigma^2)`` with ``sigma^2 = p(1-p)/tau`` the
    bounded binary variance at the measured reference mid and remaining life.
    """
    if friction <= 0.0 or q_hard_shares <= 0.0 or tau_days <= 0.0:
        raise ValueError("gamma needs positive friction, q_hard and remaining life")
    sigma_sq = p_ref * (1.0 - p_ref) / tau_days
    return friction / (q_hard_shares * sigma_sq)


def solve_merge_batch(
    *, gas_cost_usd: float, half_spread: float, quote_size: float
) -> float:
    """Smallest batch whose per-share gas does not exceed the crossing saving.

    ``gas / batch <= spread/2`` is the break-even; the fitted batch is that
    break-even raised to the measured quote size and the venue minimum order.
    """
    if gas_cost_usd <= 0.0 or half_spread <= 0.0:
        raise ValueError("merge batch needs a positive measured gas cost and spread")
    return max(gas_cost_usd / half_spread, quote_size, VENUE_MIN_ORDER_SHARES)


def _merge_gas_cost() -> tuple[float, dict[str, Any]]:
    """Measured one-merge dollar cost from the on-chain gas and POL prices."""
    from polymarketbot.strategy.onchain.ctf.gas import estimate_merge_cost_usd

    gas_path = Path("data/derived/onchain_gas.json")
    pol_path = Path("data/derived/onchain_pol.json")
    try:
        gwei = float(json.loads(gas_path.read_text(encoding="utf-8"))["gwei"])
    except (OSError, ValueError, KeyError, TypeError):
        gwei = None
    try:
        pol_usd = float(json.loads(pol_path.read_text(encoding="utf-8"))["usd"])
    except (OSError, ValueError, KeyError, TypeError):
        pol_usd = None
    cost = estimate_merge_cost_usd(gas_price_gwei=gwei, pol_usd_price=pol_usd)
    if cost is None:
        raise SystemExit("merge gas is unmeasured; refusing to price the merge batch")
    return float(cost), {
        "gas_file": str(gas_path),
        "pol_file": str(pol_path),
        "gwei": gwei,
        "pol_usd": pol_usd,
    }


def _validate(payload: dict[str, Any]) -> None:
    """Reject a fit whose own definition is not satisfied by its sample."""
    for name in (
        "gamma",
        "q_soft_bankroll_fraction",
        "q_hard_risk_fraction",
        "q_hard_absorption_quotes",
        "q_hard_floor_quotes",
        "reserve_usable_fraction",
        "holding_turnover_cycles",
        "holding_expiry_fraction",
        "holding_min_seconds",
        "holding_max_seconds",
        "merge_gas_fraction",
        "merge_quote_multiples",
        "merge_bankroll_fraction",
        "merge_min_shares",
        "ofi_toxicity_threshold",
        "drift_toxicity_cents",
    ):
        value = payload.get(name)
        if value is None or not math.isfinite(float(value)) or float(value) <= 0.0:
            raise SystemExit(f"fitted field {name}={value!r} is not a positive measured value")
    if payload["holding_max_seconds"] < payload["holding_min_seconds"]:
        raise SystemExit("measured holding bounds are inconsistent")
    # The skew gain's grid starts at zero, so a measured zero is the "never
    # skew the quote" corner rather than a missing value. It is only valid when
    # the fit says so: a zero that was not measured as the argmax, or a
    # negative gain, is still a broken fit.
    gain = payload.get("skew_aggressiveness")
    if gain is None or not math.isfinite(float(gain)):
        raise SystemExit(f"fitted field skew_aggressiveness={gain!r} is not a measured value")
    if not 0.0 <= float(gain) < 1.0:
        raise SystemExit("skew gain must be inside the quote's [0, 1) half-spread domain")
    if float(gain) == 0.0 and payload.get("corner") is not True:
        raise SystemExit(
            "a zero skew gain is only valid as the measured no-skew corner "
            "(corner=true); an unmarked zero is not a measurement"
        )
    if float(gain) > 0.0 and payload.get("corner") is True:
        raise SystemExit("a positive skew gain cannot be marked as the zero corner")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", required=True, help="comma-separated date= partitions")
    parser.add_argument("--hours", required=True, help="comma-separated hour partitions")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--risk-calibration", default=str(DEFAULT_RISK))
    parser.add_argument(
        "--loss-probability",
        type=float,
        default=0.01,
        help="the one stated preference: probability the horizon loss exceeds capture at q_hard",
    )
    parser.add_argument("--min-bucket", type=int, default=50)
    parser.add_argument("--max-seconds", type=float, default=600.0)
    parser.add_argument("--max-hours", type=int, default=0, help="cap the number of hour partitions")
    args = parser.parse_args(argv)
    if not 0.0 < args.loss_probability < 0.5:
        parser.error("--loss-probability must be in (0, 0.5)")

    started = datetime.now(UTC)
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    hours = [h.strip().zfill(2) for h in args.hours.split(",") if h.strip()]
    partitions = _hour_globs(dates, hours)
    if not partitions:
        raise SystemExit("no tape partition matches the requested dates/hours")
    if args.max_hours > 0:
        partitions = partitions[: args.max_hours]
    log.info(f"fitting de-skew calibration over {len(partitions)} hour partitions")

    con = duckdb.connect()
    con.execute("SET threads = 4")
    round_trips: list[RoundTrip] = []
    all_sizes: list[np.ndarray] = []
    condition_ids: set[str] = set()
    prints_by_asset: dict[str, int] = defaultdict(int)
    mid_medians: dict[str, float] = {}
    condition_of: dict[str, str] = {}
    book_events = 0
    prints = 0
    t_min = t_max = None

    for index, (date, hour, events, trades) in enumerate(partitions, start=1):
        elapsed = (datetime.now(UTC) - started).total_seconds()
        if elapsed > args.max_seconds:
            raise SystemExit(
                f"stopped after {elapsed:.0f}s at {date} {hour}: the run exceeds "
                f"--max-seconds {args.max_seconds:.0f} with {index - 1} partitions measured"
            )
        hour_trades, hour_conditions = _load_trades(con, trades)
        if not hour_trades:
            log.warning(f"{date} {hour}: no prints; skipped")
            continue
        hour_prints = sum(len(v[0]) for v in hour_trades.values())
        log.info(f"{date} {hour}: {hour_prints} prints over {len(hour_trades)} assets")
        condition_ids.update(hour_conditions.values())
        condition_of.update(hour_conditions)
        all_sizes.append(np.concatenate([v[2] for v in hour_trades.values()]))
        for asset_id, series in hour_trades.items():
            prints_by_asset[asset_id] += len(series[0])
            lo, hi = int(series[0].min()), int(series[0].max())
            t_min = lo if t_min is None else min(t_min, lo)
            t_max = hi if t_max is None else max(t_max, hi)
        round_trips.extend(measure_round_trips(hour_trades))
        prints += hour_prints

    if not all_sizes or not prints:
        raise SystemExit("no prints measured; refusing to write a calibration")
    sizes_all = np.concatenate(all_sizes)
    quote_size = float(np.median(sizes_all))
    if not round_trips:
        raise SystemExit("no matched round trips; the holding horizon is not measurable")
    holdings = np.array([r.holding_ns for r in round_trips], dtype=np.int64)
    horizon_ns = int(np.median(holdings))
    log.info(
        f"measured quote size {quote_size:.2f} sh; "
        f"round-trip horizon {horizon_ns / 1e9:.0f}s over {len(round_trips)} trips"
    )

    fills: list[FillSample] = []
    opportunities: list[tuple[float, float, float, float, str]] = []
    for index, (date, hour, events, trades) in enumerate(partitions, start=1):
        elapsed = (datetime.now(UTC) - started).total_seconds()
        if elapsed > args.max_seconds:
            log.warning(f"replay stopped at {date} {hour} after {elapsed:.0f}s")
            break
        tape = _load_hour(con, events, trades)
        if tape is None:
            continue
        book_events += tape.n_book_events
        for asset_id, (ts, bid, ask) in tape.books.items():
            mid_medians[asset_id] = float(np.median((bid + ask) / 2.0))
        hour_fills, hour_opps = replay_fills(
            tape.books, tape.trades, horizon_ns=horizon_ns, quote_size=quote_size
        )
        fills.extend(hour_fills)
        opportunities.extend(hour_opps)

    thresholds = solve_bucket_thresholds(
        fills,
        quote_size=quote_size,
        alpha=args.loss_probability,
        min_bucket=args.min_bucket,
    )
    if not thresholds.get("measurable"):
        raise SystemExit(f"de-skew thresholds not measurable: {thresholds.get('reason')}")
    skew = solve_skew_aggressiveness(opportunities)
    if not skew.get("measurable"):
        raise SystemExit(f"skew gain not measurable: {skew.get('reason')}")

    event_parts = [p for _, _, events, _ in partitions for p in events]
    trade_parts = [p for _, _, _, trades in partitions for p in trades]
    toxicity = measure_toxicity_tails(
        event_parts, con, alpha=args.loss_probability, traded_parts=trade_parts
    )

    risk_reused = False
    risk_path = Path(args.risk_calibration)
    if risk_path.exists():
        try:
            risk = json.loads(risk_path.read_text(encoding="utf-8"))
            ofi = risk.get("ofi", risk) if isinstance(risk, dict) else {}
            if ofi.get("ofi_toxicity_threshold") is not None and ofi.get("drift_toxicity_cents") is not None:
                toxicity = {
                    "measurable": True,
                    "ofi_toxicity_threshold": float(ofi["ofi_toxicity_threshold"]),
                    "drift_toxicity_cents": float(ofi["drift_toxicity_cents"]),
                    "samples": ofi.get("samples"),
                    "source": f"reused from {risk_path}",
                }
                risk_reused = True
        except (OSError, ValueError, TypeError):
            pass
    if not toxicity.get("measurable"):
        raise SystemExit(f"toxicity tails not measurable: {toxicity.get('reason')}")

    window_start = datetime.fromtimestamp((t_min or 0) / 1e9, tz=UTC)
    window_end = datetime.fromtimestamp((t_max or 0) / 1e9, tz=UTC)
    p_ref = float(np.median(list(mid_medians.values()))) if mid_medians else 0.5
    p_ref = min(max(p_ref, 0.01), 0.99)
    end_epochs = load_end_epoch_seconds(condition_ids, near=window_end)
    remaining_days = [
        (end - window_end.timestamp()) / 86_400.0
        for end in end_epochs.values()
        if end > window_end.timestamp()
    ]
    if not remaining_days:
        raise SystemExit("remaining life is unmeasurable; gamma needs the measured reference life")
    tau_days = float(np.median(remaining_days))

    rates_per_min = [count / 60.0 for count in prints_by_asset.values() if count >= 2]
    trades_per_min = float(np.median(rates_per_min)) if rates_per_min else 0.0
    if trades_per_min <= 0.0:
        raise SystemExit("measured print rate is zero; holding time is not measurable")

    capture = float(thresholds["capture_per_share"])
    gas_cost_usd, gas_meta = _merge_gas_cost()
    half_spread = capture
    friction = half_spread
    q_soft = float(thresholds["q_soft_shares"])
    q_hard = float(thresholds["q_hard_shares"])
    gamma = solve_gamma(
        friction=friction, q_hard_shares=q_hard, p_ref=p_ref, tau_days=tau_days
    )

    bankroll_ref = float(sum(2.0 * mid * quote_size for mid in mid_medians.values()))
    bankroll_ref = max(bankroll_ref, quote_size * max(len(mid_medians), 1))

    break_even_batch = gas_cost_usd / max(half_spread, 1e-9)
    merge_batch = solve_merge_batch(
        gas_cost_usd=gas_cost_usd, half_spread=half_spread, quote_size=quote_size
    )

    expiry_ratios = _expiry_ratios(round_trips, condition_of, end_epochs)
    expiry = (
        float(min(1.0, max(1e-9, np.quantile(expiry_ratios, 0.90))))
        if expiry_ratios
        else None
    )
    if expiry is None:
        raise SystemExit(
            "holding/remaining-life ratio is unmeasurable for every matched trip "
            f"({len(end_epochs)} markets carry an end date)"
        )

    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "window": {
            "start_utc": window_start.isoformat(),
            "end_utc": window_end.isoformat(),
            "partitions": [f"{d} {h}" for d, h, _, _ in partitions],
        },
        "sample_size": {
            "hour_partitions": len(partitions),
            "assets": len(mid_medians),
            "assets_with_prints": len(prints_by_asset),
            "book_events": book_events,
            "prints": prints,
            "fills": len(fills),
            "round_trips": int(holdings.size),
            "quote_placements": len(opportunities),
            "toxicity_events": toxicity.get("samples"),
            "markets_with_end_date": len(end_epochs),
        },
        "preference": {
            "loss_probability": args.loss_probability,
            "statement": (
                "the probability that the adverse markout over the measured holding "
                "horizon exceeds the measured spread capture at q_hard; the only "
                "stated number in this fit"
            ),
            "skew_levels": list(_SKEW_LEVELS),
            "holding_bounds_quantiles": [0.10, 0.90],
            "holding_expiry_quantile": 0.90,
            "min_bucket_observations": args.min_bucket,
            "skew_grid_resolution": 0.005,
            "quantile_basis": (
                "the loss probability, the holding clamp/expiry quantiles and the "
                "skew design grid are the only stated choices; every coefficient "
                "is otherwise measured or solved from them"
            ),
        },
        "definition": {
            "window": "recv_ts_ns of the measured prints (UTC)",
            "book": "price_change events with bid>0, ask>0 and ask>bid",
            "fill": "SELL print at or below the prevailing best bid, or BUY print at or above the best ask",
            "capture": "mid - fill price (bid fill) / fill price - mid (ask fill)",
            "adverse_loss": "max(0, mid-mid(t+H)) for a bid, max(0, mid(t+H)-mid) for an ask",
            "holding_horizon": "median FIFO-matched bid/ask round-trip holding time",
            "gamma": "GTM/A-S risk skew at q_hard equals measured friction: gamma = friction/(q_hard*sigma^2_ref)",
            "sigma_sq_ref": "bounded binary variance p(1-p)/tau at the measured median mid and remaining life",
            "q_soft": "first inventory bucket whose mean adverse loss exceeds the median capture",
            "q_hard": "first inventory bucket whose (1-alpha) loss quantile exceeds the median capture",
            "skew_aggressiveness": "phi maximising net capture (mid(t+H)-quote) per quote placement; a zero is the measured no-skew corner (rest at the touch)",
            "corner": "true when skew_aggressiveness sits on the grid's zero edge: the measured policy is never to skew the quote",
            "holding_turnover_cycles": "median round-trip holding / mean inter-print interval",
            "holding_min_seconds": "10th percentile of matched round-trip holding",
            "holding_max_seconds": "90th percentile of matched round-trip holding",
            "holding_expiry_fraction": "90th percentile of round-trip holding / remaining life at entry",
            "merge_min_shares": "max(gas/(spread/2), measured median fill size, venue minimum order size)",
            "merge_gas_fraction": "one merge's gas per share at the fitted batch, in units of the spread",
            "merge_quote_multiples": "fitted merge batch / measured quote size",
            "merge_bankroll_fraction": "fitted merge batch notional / measured universe capital",
            "ofi_toxicity_threshold": "1-alpha quantile of |rolling 10-event CKS OFI| on traded markets",
            "drift_toxicity_cents": "1-alpha quantile of |Stoikov micro-price drift| in cents, measured on traded markets inside the interquartile spread band",
        },
        "source": {
            "gamma": "solved from measured q_hard, spread and bounded-variance reference",
            "skew_aggressiveness": "print replay grid",
            "q_soft": "inventory-conditional loss replay",
            "q_hard": "inventory-conditional loss replay tail",
            "q_soft_bankroll_fraction": "q_soft notional / measured universe capital",
            "q_hard_risk_fraction": "q_hard notional / measured universe capital",
            "q_hard_absorption_quotes": "q_hard / measured quote size",
            "q_hard_floor_quotes": "q_hard / q_soft",
            "reserve_usable_fraction": "1 - gas per merge / (q_hard * mid)",
            "holding_turnover_cycles": "measured round-trip holding and print rate",
            "holding_expiry_fraction": "measured round-trip holding and remaining life",
            "holding_min_seconds": "measured round-trip holding P10",
            "holding_max_seconds": "measured round-trip holding P90",
            "merge_gas_fraction": "measured gas / (fitted batch * measured spread)",
            "merge_quote_multiples": "fitted batch / measured quote size",
            "merge_bankroll_fraction": "batch notional / measured universe capital",
            "merge_min_shares": "measured gas/spread break-even raised to the measured fill and venue minimum",
            "ofi_toxicity_threshold": "measured event tail" if not risk_reused else "reused from risk_calibration.json",
            "drift_toxicity_cents": "measured event tail" if not risk_reused else "reused from risk_calibration.json",
        },
        "diagnostics": {
            "quote_size_shares": round(quote_size, 4),
            "median_spread_dollars": round(half_spread * 2.0, 6),
            "mid_price_reference": round(p_ref, 4),
            "remaining_life_days_reference": round(tau_days, 4),
            "capture_per_share": round(capture, 6),
            "holding_horizon_seconds": round(horizon_ns / 1e9, 3),
            "trades_per_min": round(trades_per_min, 6),
            "gas_cost_usd_per_merge": round(gas_cost_usd, 8),
            "gas_source": gas_meta,
            "bankroll_reference_usd": round(bankroll_ref, 2),
            "merge_break_even_batch_shares": round(break_even_batch, 4),
            "inventory_buckets": thresholds["buckets"],
            "skew_grid": skew.get("per_level"),
            "toxicity": toxicity,
            "measured_exceedance_probability_at_q_hard": thresholds.get(
                "measured_exceedance_probability_at_q_hard"
            ),
        },
        # --- the artifact fields -------------------------------------------------
        "gamma": float(gamma),
        "skew_aggressiveness": float(skew["gain"]),
        "corner": bool(skew["corner"]),
        "q_soft_bankroll_fraction": float(min(1.0, max(1e-9, q_soft * p_ref / bankroll_ref))),
        "q_hard_risk_fraction": float(min(1.0, max(1e-9, q_hard * p_ref / bankroll_ref))),
        "q_hard_absorption_quotes": float(max(1e-9, q_hard / quote_size)),
        "q_hard_floor_quotes": float(max(1e-9, q_hard / max(q_soft, 1e-9))),
        "reserve_usable_fraction": float(
            min(1.0, max(1e-9, 1.0 - gas_cost_usd / max(q_hard * p_ref, 1e-9)))
        ),
        "holding_turnover_cycles": float(
            max(1e-9, horizon_ns / 1e9 / (60.0 / max(trades_per_min, 1e-9)))
        ),
        "holding_expiry_fraction": float(expiry),
        "holding_min_seconds": float(max(0.1, np.quantile(holdings, 0.10) / 1e9)),
        "holding_max_seconds": float(max(1.0, np.quantile(holdings, 0.90) / 1e9)),
        "merge_gas_fraction": float(
            max(1e-9, gas_cost_usd / max(merge_batch * half_spread * 2.0, 1e-9))
        ),
        "merge_quote_multiples": float(max(1e-9, merge_batch / quote_size)),
        "merge_bankroll_fraction": float(min(1.0, max(1e-9, merge_batch * p_ref / bankroll_ref))),
        "merge_min_shares": float(
            max(break_even_batch, quote_size, VENUE_MIN_ORDER_SHARES)
        ),
        "ofi_toxicity_threshold": float(toxicity["ofi_toxicity_threshold"]),
        "drift_toxicity_cents": float(toxicity["drift_toxicity_cents"]),
        # Raw sufficient statistics for every quantile / argmax above.  The
        # grids are fixed by the module constants, so the combiner can merge
        # the counts and read the pooled quantile off the merged CDF instead of
        # averaging per-partition quantiles.
        "sufficient_statistics": {
            "schema": SCHEMA,
            "holding_seconds": log_histogram(
                holdings.astype(np.float64) / 1e9, **HOLDING_HISTOGRAM
            ),
            "expiry_fraction": log_histogram(
                expiry_ratios, **EXPIRY_HISTOGRAM
            ),
            "print_rates_per_min": log_histogram(
                rates_per_min, **PRINT_RATE_HISTOGRAM
            ),
            "abs_ofi": toxicity.get("ofi_histogram"),
            "abs_drift_cents": toxicity.get("drift_histogram"),
            "skew": {
                "placements": int(skew["placements"]),
                "grid": skew["grid_totals"],
            },
            "merge_gas": {
                "gas_usd": float(gas_cost_usd),
                "denominator": float(merge_batch * half_spread * 2.0),
            },
            "holding_quantiles": [0.10, 0.90],
            "expiry_quantile": 0.90,
            "loss_probability": float(args.loss_probability),
        },
    }
    _validate(payload)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if load_deskew_calibration(out) is None:
        raise SystemExit(f"wrote {out} but the de-skew loader cannot read it")
    log.info(f"wrote {out} ({len(fills)} fills, {prints} prints, {len(mid_medians)} assets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
