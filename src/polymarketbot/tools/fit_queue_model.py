"""Fit the FIFO queue model and measure the value of a queue position from tape.

The premise is that a FIFO queue has no free fill parameter: a resting order
sits behind whatever size was displayed at its price when it landed, every
later change to that level is observable, and every print at or through the
price is observable. Replaying
:class:`polymarketbot.risk.queue_position.QueuePositionTracker` over the L2
book and the public prints therefore *measures* fill probability and queue
value instead of assuming a pro-rata share.

The artifact written here is the exact input set of
:class:`polymarketbot.strategy.reward.queue.QueuePriorityValuationModel`, plus
the sample behind every field:

``trade_arrival_rate_shares_sec``
    Shares printed at the touch per second per active market, as an EWMA over
    60 s buckets. A print is "at the touch" when its price equals the last
    quoted best bid or best ask for that asset (the tape records the maker
    price a taker actually got).
``cancellation_rate_intensity``
    Size removed from a price level between consecutive full book snapshots
    that is not explained by prints in the same interval, per second per
    average share of displayed depth. This is the standard
    ``cancelled_shares / (depth x dt)`` intensity, measured from
    ``clob_events`` book diffs.
``kyle_lambda``
    Price impact per 1,000 shares of signed flow, fitted on one row per public
    print at +10 s and +60 s: event-study OLS, the same with per-market fixed
    effects, a Huber fit and a median fit, plus the depth-normalised level-1
    OFI of the same window as evidence. The artifact carries every
    specification with its interval; the value is 0 when none of the
    signed-size specifications identifies a positive impact.
``volatility_annualized``
    Realised variance of mid log returns per second, annualised over a 24/7
    calendar (``365.25 * 86_400`` seconds): ``sqrt(sum(r^2)/sum(dt) * YEAR)``.
    The estimator is zero-mean, which is the standard realised-variance
    convention over horizons this short.
``annual_capital_hurdle_rate``
    The opportunity cost of capital: the best net annualised yield of a
    rewarded pool in the measured universe, where the yield is the venue's own
    pool rate times the *measured* share at a minimum-lot quote on the cheaper
    leg, net of the measured adverse markout and fill rate. The minimum-lot
    notional is the venue's ``rewardsMinSize`` at the quoted price.

The second artifact, ``queue_value.json``, is the measured value of a queue
position: ``E[filled fraction] * (entry edge - adverse markout) + reward
accrual`` per share per second, replayed on a grid of quote prices and sizes.
A value near zero is a finding, not a failure: it says queue priority on this
tape is worth about nothing per unit time.

Usage::

    python -m polymarketbot.tools.fit_queue_model --hours 14 --budget-seconds 60
    python -m polymarketbot.tools.fit_queue_model --hours 14 --budget-seconds 600

Cost is dominated by parsing the book snapshots. The tool reads only the
columns each stage needs, logs rows in and out of every stage, and writes the
artifacts incrementally: a budget stop leaves a valid file with ``partial``
set and the stages that completed.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.compute as pc

from polymarketbot.logs import get_logger
from polymarketbot.risk.queue_position import QueuePositionTracker
from polymarketbot.strategy.reward.accrual import measured_accrual
from polymarketbot.tools.tape_reader import read_tape, tape_files

log = get_logger("fit_queue_model")

ROOT = Path(__file__).resolve().parents[3]
DERIVED = ROOT / "data" / "derived"
TAPE = ROOT / "data" / "tape"
CENSUS = DERIVED / "ctf_census" / "universe.jsonl"
QUEUE_FIT = DERIVED / "queue_fit.json"
QUEUE_VALUE = DERIVED / "queue_value.json"
MARKOUT = DERIVED / "ctf_maker_markout.json"
FILL_HORIZON = DERIVED / "ctf_quote_fill_horizon.json"

YEAR_SECONDS = 365.25 * 86_400.0
BUCKET_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Small numeric helpers (no dependency beyond the standard library)
# ---------------------------------------------------------------------------


def _ols(x: list[float], y: list[float]) -> dict[str, float | None]:
    """Ordinary least squares ``y = a + b*x`` with standard errors."""
    n = len(x)
    if n < 3:
        return {"n": n, "slope": None, "intercept": None, "r2": None, "slope_stderr": None}
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    sxx = sum((v - mean_x) ** 2 for v in x)
    sxy = sum((v - mean_x) * (w - mean_y) for v, w in zip(x, y, strict=True))
    if sxx <= 0.0:
        return {"n": n, "slope": None, "intercept": None, "r2": None, "slope_stderr": None}
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    residuals = [w - (intercept + slope * v) for v, w in zip(x, y, strict=True)]
    ss_res = sum(r * r for r in residuals)
    ss_tot = sum((w - mean_y) ** 2 for w in y)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0.0 else 0.0
    stderr = math.sqrt(ss_res / (n - 2) / sxx) if n > 2 and sxx > 0.0 else None
    return {
        "n": n,
        "slope": slope,
        "intercept": intercept,
        "r2": r2,
        "slope_stderr": stderr,
    }


def _quantiles(values: list[float], qs: tuple[float, ...]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    out: dict[str, float] = {}
    for q in qs:
        idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
        out[f"p{round(q * 100)}"] = ordered[idx]
    out["mean"] = sum(ordered) / len(ordered)
    out["min"] = ordered[0]
    out["max"] = ordered[-1]
    out["count"] = len(ordered)
    return out


def _iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return datetime.fromtimestamp(ns / 1e9, UTC).isoformat()


def _ewma(series: list[float]) -> float | None:
    """Recursive EWMA with ``alpha = 2/(n+1)`` (the fitter's own convention)."""
    value: float | None = None
    for i, sample in enumerate(series, start=1):
        alpha = 2.0 / (i + 1.0)
        value = sample if value is None else (1.0 - alpha) * value + alpha * sample
    return value


def _levels(raw: Any, *, side: str) -> list[tuple[float, float]]:
    """A CLOB ladder as ``[(price, size)]``, sorted best-first.

    The venue's ``book`` event publishes bids ascending and asks descending, so
    the raw order is the *worst* price first. Every consumer here wants the
    touch, so the side decides the sort.
    """
    if not isinstance(raw, list):
        return []
    out: list[tuple[float, float]] = []
    for level in raw:
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0.0 < price < 1.0 and size > 0.0:
            out.append((price, size))
    out.sort(key=lambda item: item[0], reverse=(side == "BID"))
    return out


def _with_own_order(
    levels: list[tuple[float, float]], price: float, size: float
) -> list[tuple[float, float]]:
    """The ladder including our virtual order, as the venue would publish it.

    ``QueuePositionTracker.on_book`` assumes the snapshot includes our size
    (``ahead = min(prev, observed - size)``). The tape obviously does not, so
    the replay adds it at the quote price; without it the first later snapshot
    would read the size ahead of us as the whole level and clear it.
    """
    if size <= 0.0:
        return levels
    merged: list[tuple[float, float]] = []
    placed = False
    for level_price, level_size in levels:
        if abs(level_price - price) <= 1e-12:
            merged.append((level_price, level_size + size))
            placed = True
        else:
            merged.append((level_price, level_size))
    if not placed:
        merged.append((price, size))
    return merged


def _as_book(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> dict:
    return {
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
    }


# ---------------------------------------------------------------------------
# Universe: the venue's own reward parameters
# ---------------------------------------------------------------------------


def load_universe() -> dict[str, dict[str, Any]]:
    """Rewarded, active markets from the census snapshot, keyed by condition."""
    universe: dict[str, dict[str, Any]] = {}
    malformed = 0
    if not CENSUS.exists():
        raise FileNotFoundError(f"reward universe census missing: {CENSUS}")
    with CENSUS.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            rewards = record.get("clobRewards") or []
            rate = sum(
                float(campaign.get("rewardsDailyRate") or 0.0)
                for campaign in rewards
                if isinstance(campaign, dict)
            )
            if rate <= 0.0 or not record.get("acceptingOrders", True):
                continue
            try:
                tokens = json.loads(record.get("clobTokenIds") or "[]")
                prices = json.loads(record.get("outcomePrices") or "[]")
            except ValueError:
                continue
            if len(tokens) != 2:
                continue
            try:
                tick = float(record.get("orderPriceMinTickSize") or 0.0)
                min_size = float(record.get("rewardsMinSize") or 0.0)
                max_spread = float(record.get("rewardsMaxSpread") or 0.0)
            except (TypeError, ValueError):
                continue
            if tick <= 0.0 or min_size <= 0.0 or max_spread <= 0.0:
                continue
            universe[str(record["conditionId"])] = {
                "rate": rate,
                "tokens": [str(t) for t in tokens],
                "prices": [float(p) for p in prices] if len(prices) == 2 else None,
                "tick": tick,
                "min_size": min_size,
                "max_spread": max_spread,
            }
    log.info(
        f"universe: {len(universe)} rewarded active markets "
        f"(${sum(v['rate'] for v in universe.values()):,.0f}/day), "
        f"{malformed} malformed census line(s) skipped"
    )
    return universe


# ---------------------------------------------------------------------------
# Prints
# ---------------------------------------------------------------------------


def load_prints(files: list[Path]) -> dict[str, Any]:
    """Prints indexed by asset and by (asset, price) for the replay."""
    table = read_tape(
        files,
        columns=["recv_ts_ns", "condition_id", "asset_id", "price", "size", "side"],
    )
    rows = table.to_pylist()
    by_asset: dict[str, list[tuple[float, float, float, str]]] = {}
    for row in rows:
        try:
            ts = int(row["recv_ts_ns"]) / 1e9
            price = float(row["price"])
            size = float(row["size"])
        except (TypeError, ValueError):
            continue
        asset = row.get("asset_id")
        if not asset or not (0.0 < price < 1.0) or size <= 0.0:
            continue
        by_asset.setdefault(str(asset), []).append(
            (ts, price, size, str(row.get("side") or ""))
        )
    for prints in by_asset.values():
        prints.sort(key=lambda item: item[0])
    log.info(
        f"prints: {len(rows):,} rows -> {len(by_asset):,} assets, "
        f"{sum(len(v) for v in by_asset.values()):,} usable prints"
    )
    return {"rows": len(rows), "by_asset": by_asset}


# ---------------------------------------------------------------------------
# Book snapshots and best quotes
# ---------------------------------------------------------------------------


def load_books(files: list[Path]) -> dict[str, Any]:
    """Full book snapshots and best-bid/ask updates, materialised as ladders."""
    table = read_tape(
        files,
        columns=[
            "recv_ts_ns",
            "asset_id",
            "event_type",
            "bids",
            "asks",
            "best_bid",
            "best_ask",
        ],
    )
    books = table.filter(pc.equal(table.column("event_type"), "book"))
    quotes = table.filter(pc.equal(table.column("event_type"), "best_bid_ask"))
    log.info(
        f"events: {table.num_rows:,} rows -> {books.num_rows:,} book snapshots, "
        f"{quotes.num_rows:,} best-bid/ask updates"
    )

    book_rows: dict[str, list[tuple[float, list[tuple[float, float]], list[tuple[float, float]]]]] = {}
    for row in books.to_pylist():
        asset = row.get("asset_id")
        if not asset:
            continue
        bids = _levels(row.get("bids"), side="BID")
        asks = _levels(row.get("asks"), side="ASK")
        if not bids and not asks:
            continue
        book_rows.setdefault(str(asset), []).append(
            (int(row["recv_ts_ns"]) / 1e9, bids, asks)
        )
    for snaps in book_rows.values():
        snaps.sort(key=lambda item: item[0])

    quote_rows: dict[str, list[tuple[float, float, float]]] = {}
    for row in quotes.to_pylist():
        asset = row.get("asset_id")
        if not asset:
            continue
        try:
            bid = float(row["best_bid"])
            ask = float(row["best_ask"])
        except (TypeError, ValueError):
            continue
        if not (0.0 < bid < ask < 1.0):
            continue
        quote_rows.setdefault(str(asset), []).append(
            (int(row["recv_ts_ns"]) / 1e9, bid, ask)
        )
    for rows_ in quote_rows.values():
        rows_.sort(key=lambda item: item[0])

    log.info(
        f"books: {len(book_rows):,} assets with snapshots, "
        f"{len(quote_rows):,} assets with best quotes"
    )
    return {
        "rows": table.num_rows,
        "book_rows": book_rows,
        "quote_rows": quote_rows,
        "book_count": books.num_rows,
        "quote_count": quotes.num_rows,
    }


def mid_series(books: dict[str, Any]) -> dict[str, list[tuple[float, float]]]:
    """Per-asset sorted ``(ts, mid)`` from best quotes, falling back to books."""
    series: dict[str, list[tuple[float, float]]] = {}
    for asset, rows_ in books["quote_rows"].items():
        points = [(ts, (bid + ask) * 0.5) for ts, bid, ask in rows_]
        if points:
            series[asset] = points
    for asset, snaps in books["book_rows"].items():
        if asset in series:
            continue
        points = []
        for ts, bids, asks in snaps:
            if bids and asks:
                bid, ask = bids[0][0], asks[0][0]
                if 0.0 < bid < ask < 1.0:
                    points.append((ts, (bid + ask) * 0.5))
        if points:
            series[asset] = points
    return series


def _mid_at(series: list[tuple[float, float]] | None, ts: float) -> float | None:
    """Last mid at or before ``ts`` (``None`` when the series has no such point)."""
    if not series:
        return None
    lo, hi = 0, len(series)
    while lo < hi:
        middle = (lo + hi) // 2
        if series[middle][0] <= ts:
            lo = middle + 1
        else:
            hi = middle
    return series[lo - 1][1] if lo else None


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------


def measure_volatility(series: dict[str, list[tuple[float, float]]]) -> dict[str, Any]:
    """Pooled realised variance of mid log returns per second, annualised 24/7."""
    weighted = 0.0
    elapsed = 0.0
    returns = 0
    assets = 0
    for points in series.values():
        if len(points) < 2:
            continue
        assets += 1
        for (t0, m0), (t1, m1) in itertools.pairwise(points):
            dt = t1 - t0
            if dt <= 0.0 or m0 <= 0.0 or m1 <= 0.0:
                continue
            r = math.log(m1 / m0)
            weighted += r * r
            elapsed += dt
            returns += 1
    if elapsed <= 0.0 or returns < 2:
        return {"value": None, "returns": returns, "assets": assets, "seconds": elapsed}
    var_per_second = weighted / elapsed
    return {
        "value": math.sqrt(var_per_second * YEAR_SECONDS),
        "variance_per_second": var_per_second,
        "returns": returns,
        "assets": assets,
        "seconds": elapsed,
    }


# ---------------------------------------------------------------------------
# Kyle lambda: print-level price impact across every tape partition
# ---------------------------------------------------------------------------
#
# The earlier estimator bucketed signed volume into 60 s windows per asset and
# regressed the window mid change on it. That has two defects: a window's sum
# and its mid change are taken over the same interval regardless of when inside
# it the prints landed, and the pooled slope picks up the cross-sectional
# difference between markets (a busy 50c market and a quiet 3c market have
# different volatility, not a different lambda). The fit below is event-level
# (one row per public print, the object impact is defined on), reports four
# specifications with intervals, and adds market fixed effects so that lambda is
# identified from *within-market* variation only.

KYLE_HORIZONS = (10.0, 60.0)
# An edge whose newest top-of-book state is older than this is unmeasured: a
# stale mid reads as no move, which is a measurement of nothing.
KYLE_EDGE_STALE_S = 60.0
KYLE_MIN_EVENTS = 1000
KYLE_BOOTSTRAP_DRAWS = 200
KYLE_CACHE = DERIVED / "kyle_fit"
KYLE_WORKERS = 4

#: The columns of one part's cached state rows.  A part is the resumption unit
#: of the Kyle fit: a bounded invocation that runs out of time stops between
#: parts and the next one continues from the cached rows instead of re-reading
#: the part.
_KYLE_STATE_COLUMNS = ("code", "ts_ns", "mid", "ofi", "depth")


def _kyle_part_cache_path(date: str, hour: str, part: Path) -> Path:
    return KYLE_CACHE / "parts" / f"date={date}" / f"hour={hour}" / f"{part.stem}.parquet"


def _kyle_part_states(path: Path, index: dict[str, int]) -> dict[str, np.ndarray]:
    """One part's top-of-book states of the tokens that printed in the hour.

    ``_tob_states``/``_ofi_events`` are the fitter of record for the exact
    arithmetic of :meth:`MicrostructureEngine.update`; importing them here
    keeps one definition of OFI on the tape. The tape carries a top-of-book
    state for far more tokens than it prints, so the states are filtered to the
    hour's printed tokens before they are carried: an impact regression needs
    the mid of a token that traded, not the book of one that did not.

    Tokens are carried as ``index`` codes rather than object strings: an hour's
    five million states as objects is what made the read fail on a loaded host,
    and the rows are stored as float32 exactly as the fit consumes them.
    """
    from polymarketbot.tools.fit_risk_calibration import _ofi_events, _tob_states

    states = _ofi_events(_tob_states(path))
    empty = {
        "code": np.empty(0, dtype=np.int32),
        "ts_ns": np.empty(0, dtype=np.int64),
        "mid": np.empty(0, dtype=np.float32),
        "ofi": np.empty(0, dtype=np.float32),
        "depth": np.empty(0, dtype=np.float32),
    }
    if states["ts"].size == 0:
        return empty
    asset = np.asarray(states["asset"], dtype=object)
    code = np.fromiter(
        (index.get(str(token), -1) for token in asset),
        dtype=np.int32,
        count=asset.size,
    )
    keep = code >= 0
    if not keep.any():
        return empty
    qb = states["qb"][keep]
    qa = states["qa"][keep]
    total = qb + qa
    depth = np.where(
        np.isfinite(total) & (qb > 0.0) & (qa > 0.0),
        2.0 * qb * qa / np.where(total > 0.0, total, 1.0),
        np.nan,
    )
    return {
        "code": code[keep],
        "ts_ns": states["ts"][keep].astype(np.int64),
        "mid": np.asarray(states["mid"][keep], dtype=np.float32),
        "ofi": np.asarray(states["ofi"][keep], dtype=np.float32),
        "depth": np.asarray(depth, dtype=np.float32),
    }


def _kyle_hour_events(
    date: str,
    hour: str,
    prints: dict[str, np.ndarray] | None = None,
    *,
    deadline: float | None = None,
    rebuild: bool = False,
) -> dict[str, np.ndarray] | None:
    """Event-study rows of one hour: signed print size against its mid moves.

    One row per public print whose mid before the print and whose mid at
    ``+10 s``/``+60 s`` are measured on the same hour of book states. The
    covariate is the depth-normalised net order flow over the same window,
    summed from the level-1 OFI of every touch change in it.

    Returns ``None`` when ``deadline`` passed before every part was read: the
    finished parts are cached, so the caller can resume rather than publish a
    fit over half an hour.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    empty = {
        "asset": np.empty(0, dtype=object),
        **{name: np.empty(0, dtype=np.float32) for name in _KYLE_COLUMNS},
    }
    folder = TAPE / "clob_events" / f"date={date}" / f"hour={hour}"
    if not folder.is_dir():
        return empty
    if prints is None:
        trade_folder = TAPE / "clob_trades" / f"date={date}" / f"hour={hour}"
        trade_files = sorted(trade_folder.glob("*.parquet")) if trade_folder.is_dir() else []
        prints = _load_print_rows(trade_files)
    if prints["ts"].size == 0:
        return empty
    tokens = sorted({str(token) for token in prints["asset"]})
    index = {token: position for position, token in enumerate(tokens)}
    print_code = np.fromiter(
        (index.get(str(asset), -1) for asset in prints["asset"]),
        dtype=np.int32,
        count=prints["asset"].size,
    )

    pieces: list[dict[str, np.ndarray]] = []
    for path in sorted(folder.glob("*.parquet")):
        cache = _kyle_part_cache_path(date, hour, path)
        if cache.exists() and not rebuild:
            table = pq.read_table(cache, columns=list(_KYLE_STATE_COLUMNS))
            pieces.append(
                {name: table[name].to_numpy() for name in _KYLE_STATE_COLUMNS}
            )
            continue
        if deadline is not None and time.time() > deadline:
            log.info(
                f"kyle hour {date} {hour}: budget exhausted after "
                f"{len(pieces)} part(s); the hour resumes from the cache"
            )
            return None
        piece = _kyle_part_states(path, index)
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix(".tmp.parquet")
        pq.write_table(
            pa.table({name: pa.array(piece[name]) for name in _KYLE_STATE_COLUMNS}),
            temporary,
        )
        temporary.replace(cache)
        pieces.append(piece)

    pieces = [piece for piece in pieces if piece["ts_ns"].size]
    if not pieces:
        return empty
    code = np.concatenate([piece["code"] for piece in pieces])
    ts = np.concatenate([piece["ts_ns"] for piece in pieces]).astype(np.float64) * 1e-9
    mid = np.concatenate([piece["mid"] for piece in pieces]).astype(np.float64)
    ofi = np.concatenate([piece["ofi"] for piece in pieces]).astype(np.float64)
    depth = np.concatenate([piece["depth"] for piece in pieces]).astype(np.float64)
    order = np.lexsort((ts, code))
    code, ts, mid, ofi, depth = code[order], ts[order], mid[order], ofi[order], depth[order]
    bounds = np.searchsorted(code, np.arange(len(tokens) + 1))
    rows: dict[str, list[np.ndarray]] = {name: [] for name in _KYLE_COLUMNS}
    rows["asset"] = []
    for code_value in np.unique(print_code[print_code >= 0]):
        lo, hi = int(bounds[code_value]), int(bounds[code_value + 1])
        if hi - lo < 2:
            continue
        t = ts[lo:hi]
        token_mid = mid[lo:hi]
        token_depth = depth[lo:hi]
        token_ofi = ofi[lo:hi]
        valid = np.isfinite(token_ofi)
        flow = np.concatenate(([0.0], np.cumsum(np.where(valid, token_ofi, 0.0))))
        events = np.concatenate(([0], np.cumsum(valid)))
        selection = print_code == code_value
        pts = prints["ts"][selection]
        order = np.argsort(pts, kind="stable")
        pts = pts[order]
        psigned = prints["signed"][selection][order]
        idx0 = np.searchsorted(t, pts, side="right") - 1
        safe0 = np.maximum(idx0, 0)
        fresh0 = (idx0 >= 0) & ((pts - t[safe0]) <= KYLE_EDGE_STALE_S)
        mid0 = np.where(fresh0, token_mid[safe0], np.nan)
        out: dict[str, np.ndarray] = {"signed_size": psigned, "mid0": mid0}
        for horizon in KYLE_HORIZONS:
            edge = pts + horizon
            idx = np.searchsorted(t, edge, side="right") - 1
            safe = np.maximum(idx, 0)
            fresh = (idx >= 0) & ((edge - t[safe]) <= KYLE_EDGE_STALE_S)
            out[f"dmid_{horizon:g}"] = np.where(fresh, token_mid[safe] - mid0, np.nan)
            counted = events[safe + 1] - events[safe0 + 1]
            summed = flow[safe + 1] - flow[safe0 + 1]
            out[f"flow_{horizon:g}"] = np.where(
                (counted >= 1)
                & np.isfinite(token_depth[safe0])
                & (token_depth[safe0] > 0.0),
                summed / token_depth[safe0],
                np.nan,
            )
        out["depth"] = np.where(fresh0, token_depth[safe0], np.nan)
        for name in _KYLE_COLUMNS:
            rows[name].append(out[name])
        rows["asset"].append(np.full(pts.size, str(tokens[code_value]), dtype=object))

    if not rows["signed_size"]:
        return empty
    combined: dict[str, np.ndarray] = {}
    for name in _KYLE_COLUMNS:
        combined[name] = np.concatenate(rows[name]).astype(np.float32)
    combined["asset"] = np.concatenate(rows["asset"])
    # The hour is measured: the part rows were only the resumption state of an
    # unfinished read, so they are dropped instead of accumulating per part.
    for path in sorted(folder.glob("*.parquet")):
        _kyle_part_cache_path(date, hour, path).unlink(missing_ok=True)
    return combined


_KYLE_COLUMNS = (
    "signed_size",
    "mid0",
    "depth",
    "dmid_10",
    "dmid_60",
    "flow_10",
    "flow_60",
)


def _load_print_rows(files: list[Path]) -> dict[str, np.ndarray]:
    """Every public print of these parts: asset, ``ts`` in seconds, signed size."""
    if not files:
        return {
            "asset": np.empty(0, dtype=object),
            "ts": np.empty(0),
            "signed": np.empty(0),
        }
    table = read_tape(
        files, columns=["recv_ts_ns", "asset_id", "price", "size", "side"]
    )
    size = table["size"].to_numpy().astype(np.float64)
    keep = np.isfinite(size) & (size > 0.0)
    side = np.char.upper(np.asarray(table["side"].to_pylist(), dtype=np.str_))
    return {
        "asset": np.asarray(table["asset_id"].to_pylist(), dtype=object)[keep],
        "ts": table["recv_ts_ns"].to_numpy().astype(np.float64)[keep] * 1e-9,
        "signed": np.where(side[keep] == "BUY", size[keep], -size[keep]),
    }


def _kyle_cache_path(date: str, hour: str) -> Path:
    return KYLE_CACHE / f"date={date}" / f"hour={hour}.parquet"


def _kyle_hour_job(
    job: tuple[str, str, float | None, bool],
) -> tuple[str, str, int | None]:
    """Worker: measure one hour partition and checkpoint its event rows.

    Returns ``None`` rows when the deadline ended the read between parts: the
    part rows stay cached, so the hour is resumed, not published half-read.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    date, hour, deadline, rebuild = job
    events = _kyle_hour_events(date, hour, deadline=deadline, rebuild=rebuild)
    if events is None:
        return date, hour, None
    path = _kyle_cache_path(date, hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table({name: pa.array(events[name]) for name in _KYLE_COLUMNS + ("asset",)})
    temporary = path.with_suffix(".tmp.parquet")
    pq.write_table(table, temporary)
    temporary.replace(path)
    return date, hour, int(table.num_rows)


def collect_kyle_events(
    dates: list[str],
    *,
    hours: list[tuple[str, str]] | None = None,
    workers: int,
    rebuild: bool,
    budget_s: float,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """Event rows of every hour partition under ``dates``, from cache or tape.

    ``hours`` (date, hour pairs) restricts the read to those partitions, which
    is what a single-partition refit needs; without it every hour of every
    given date is read.
    """
    import pyarrow.parquet as pq

    jobs: list[tuple[str, str]] = []
    state: dict[str, Any] = {
        "dates": dates,
        "hours": 0,
        "computed_hours": 0,
        "events": 0,
        "partial": False,
    }
    partitions = (
        sorted(hours)
        if hours is not None
        else [
            (date, hour_folder.name.split("=", 1)[1])
            for date in dates
            for hour_folder in sorted((TAPE / "clob_events" / f"date={date}").glob("hour=*"))
            if (TAPE / "clob_events" / f"date={date}").is_dir()
        ]
    )
    for date, hour in partitions:
        folder = TAPE / "clob_events" / f"date={date}" / f"hour={hour}"
        if not folder.is_dir():
            continue
        state["hours"] += 1
        if rebuild or not _kyle_cache_path(date, hour).exists():
            jobs.append((date, hour))
    started = time.time()
    if jobs:
        workers = max(1, min(int(workers), len(jobs)))
        if workers == 1:
            for job in jobs:
                remaining = budget_s - (time.time() - started)
                if remaining <= 0.0:
                    state["partial"] = True
                    break
                try:
                    _d, _h, rows = _kyle_hour_job(
                        (job[0], job[1], time.time() + remaining, rebuild)
                    )
                    if rows is None:
                        state["partial"] = True
                        break
                    state["computed_hours"] += 1
                    log.info(f"kyle hour {_d} {_h}: {rows:,} events")
                except Exception as exc:  # noqa: BLE001 - one bad hour must not hide the others
                    # A failed hour is a missing measurement, not a quiet skip:
                    # the fit is partial and the combiner must not publish it.
                    state["partial"] = True
                    state.setdefault("failed_hours", []).append(f"{job[0]}T{job[1]}: {exc}")
                    log.warning(f"kyle hour {job}: {exc}")
        else:
            from concurrent.futures import ProcessPoolExecutor, as_completed

            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        _kyle_hour_job,
                        (job[0], job[1], started + budget_s, rebuild),
                    ): job
                    for job in jobs
                }
                for future in as_completed(futures):
                    date, hour = futures[future]
                    if time.time() - started > budget_s:
                        state["partial"] = True
                        for pending in futures:
                            pending.cancel()
                        break
                    try:
                        _d, _h, rows = future.result()
                        if rows is None:
                            state["partial"] = True
                            continue
                        state["computed_hours"] += 1
                        log.info(f"kyle hour {date} {hour}: {rows:,} events")
                    except Exception as exc:  # noqa: BLE001 - one bad hour must not hide the others
                        state["partial"] = True
                        state.setdefault("failed_hours", []).append(
                            f"{date}T{hour}: {exc}"
                        )
                        log.warning(f"kyle hour {date} {hour}: {exc}")

    pieces: dict[str, list[np.ndarray]] = {name: [] for name in _KYLE_COLUMNS + ("asset",)}
    for date, hour in partitions:
        path = _kyle_cache_path(date, hour)
        if not path.exists():
            state["partial"] = True
            continue
        table = pq.read_table(path, columns=list(_KYLE_COLUMNS + ("asset",)))
        for name, arrays in pieces.items():
            arrays.append(table[name].to_numpy(zero_copy_only=False))
    events: dict[str, np.ndarray] = {}
    for name, arrays in pieces.items():
        events[name] = (
            np.concatenate(arrays)
            if arrays
            else np.empty(0, dtype=object if name == "asset" else np.float64)
        )
    state["events"] = int(events["signed_size"].size)
    state["elapsed_s"] = round(time.time() - started, 1)
    return events, state


def _centered(x: np.ndarray, y: np.ndarray, groups: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    """Within-market deviations when ``groups`` is given, else the raw pair."""
    if groups is None:
        return x - x.mean(), y - y.mean()
    order = np.argsort(groups, kind="stable")
    sorted_groups = groups[order]
    starts = np.flatnonzero(np.r_[True, sorted_groups[1:] != sorted_groups[:-1]])
    dx = np.empty_like(x)
    dy = np.empty_like(y)
    xs, ys = x[order], y[order]
    for lo, hi in zip(starts, np.r_[starts[1:], sorted_groups.size], strict=True):
        dx[order[lo:hi]] = xs[lo:hi] - xs[lo:hi].mean()
        dy[order[lo:hi]] = ys[lo:hi] - ys[lo:hi].mean()
    return dx, dy


def _interval(estimate: float, se: float) -> list[float] | None:
    if not (math.isfinite(estimate) and math.isfinite(se)) or se <= 0.0:
        return None
    return [estimate - 1.96 * se, estimate + 1.96 * se]


def _ols_evidence(
    x: np.ndarray, y: np.ndarray, groups: np.ndarray | None = None
) -> dict[str, Any]:
    """Slope with a cluster-robust interval (clustered by institution when given)."""
    n = int(x.size)
    if n < 50:
        return {"n": n, "coefficient": None, "ci95": None, "se": None, "r2": None}
    dx, dy = _centered(x, y, groups)
    sxx = float(dx @ dx)
    ss_tot = float(dy @ dy)
    if sxx <= 0.0 or ss_tot <= 0.0:
        return {"n": n, "coefficient": None, "ci95": None, "se": None, "r2": None}
    slope = float(dx @ dy) / sxx
    resid = dy - slope * dx
    r2 = 1.0 - float(resid @ resid) / ss_tot
    if groups is None:
        meat = float((resid * resid) @ (dx * dx))
        se = math.sqrt(meat / sxx**2 * n / max(n - 2, 1))
        clusters = n
    else:
        order = np.argsort(groups, kind="stable")
        sorted_groups = groups[order]
        starts = np.flatnonzero(np.r_[True, sorted_groups[1:] != sorted_groups[:-1]])
        scores = resid[order] * dx[order]
        sums = np.add.reduceat(scores, starts)
        clusters = int(starts.size)
        meat = float(sums @ sums)
        se = math.sqrt(meat / sxx**2 * clusters / max(clusters - 1, 1))
    return {
        "n": n,
        "clusters": clusters,
        "coefficient": slope,
        "se": se,
        "ci95": _interval(slope, se),
        "r2": r2,
    }


def _huber_evidence(x: np.ndarray, y: np.ndarray, *, tuning: float = 1.345) -> dict[str, Any]:
    """Huber M-estimate of the slope with its sandwich interval.

    A single large print must not set lambda: the print-size distribution has a
    heavy tail, and OLS prices it as if it were the typical trade.
    """
    n = int(x.size)
    if n < 50:
        return {"n": n, "coefficient": None, "ci95": None, "se": None}
    dx = x - x.mean()
    dy = y - y.mean()
    sxx = float(dx @ dx)
    if sxx <= 0.0:
        return {"n": n, "coefficient": None, "ci95": None, "se": None}
    slope = float(dx @ dy) / sxx
    intercept = y.mean() - slope * x.mean()
    scale = 1.4826 * float(np.median(np.abs(y - intercept - slope * x)))
    if not math.isfinite(scale) or scale <= 0.0:
        scale = float(np.std(y - intercept - slope * x)) or 1.0
    for _ in range(60):
        u = (y - intercept - slope * x) / scale
        weight = np.where(np.abs(u) <= tuning, 1.0, tuning / np.maximum(np.abs(u), 1e-12))
        wx = weight * dx
        sxx = float(wx @ dx)
        if sxx <= 0.0:
            break
        slope = float(wx @ (y - y.mean())) / sxx
        intercept = y.mean() - slope * x.mean()
        new_scale = 1.4826 * float(np.median(np.abs(y - intercept - slope * x)))
        if new_scale > 0.0 and math.isfinite(new_scale):
            scale = new_scale
    u = (y - intercept - slope * x) / scale
    inside = np.abs(u) <= tuning
    psi = np.clip(u, -tuning, tuning)
    curvature = float((dx * dx)[inside].sum())
    spread = float(((psi * dx) ** 2).sum())
    if curvature <= 0.0:
        return {"n": n, "coefficient": slope, "ci95": None, "se": None, "scale": scale}
    se = math.sqrt(spread) / curvature
    return {
        "n": n,
        "coefficient": slope,
        "se": se,
        "ci95": _interval(slope, se),
        "scale": scale,
        "share_inside": float(inside.mean()),
    }


def _lad_slope(x: np.ndarray, y: np.ndarray, *, iterations: int = 40) -> float | None:
    """Median (L1) regression slope by iteratively reweighted least squares."""
    dx = x - x.mean()
    dy = y - y.mean()
    sxx = float(dx @ dx)
    if sxx <= 0.0:
        return None
    slope = float(dx @ dy) / sxx
    intercept = y.mean() - slope * x.mean()
    for _ in range(iterations):
        resid = y - intercept - slope * x
        weight = 1.0 / np.maximum(np.abs(resid), 1e-9)
        wx = weight * dx
        sxx = float(wx @ dx)
        if sxx <= 0.0:
            return None
        slope = float(wx @ (y - y.mean())) / sxx
        intercept = y.mean() - slope * x.mean()
    return slope


def _lad_evidence(
    x: np.ndarray, y: np.ndarray, groups: np.ndarray, *, draws: int = KYLE_BOOTSTRAP_DRAWS
) -> dict[str, Any]:
    """Median regression with a cluster bootstrap interval over markets."""
    n = int(x.size)
    if n < 50:
        return {"n": n, "coefficient": None, "ci95": None}
    slope = _lad_slope(x, y)
    if slope is None:
        return {"n": n, "coefficient": None, "ci95": None}
    order = np.argsort(groups, kind="stable")
    sorted_groups = groups[order]
    starts = np.flatnonzero(np.r_[True, sorted_groups[1:] != sorted_groups[:-1]])
    stops = np.r_[starts[1:], sorted_groups.size]
    blocks = list(zip(starts.tolist(), stops.tolist(), strict=True))
    rng = np.random.default_rng(11)
    estimates = []
    for _ in range(draws):
        picked = rng.integers(0, len(blocks), len(blocks))
        index = np.concatenate([order[blocks[i][0] : blocks[i][1]] for i in picked])
        value = _lad_slope(x[index], y[index])
        if value is not None and math.isfinite(value):
            estimates.append(value)
    if len(estimates) < draws // 2:
        return {"n": n, "coefficient": slope, "ci95": None, "draws": len(estimates)}
    low, high = np.quantile(estimates, (0.025, 0.975))
    return {
        "n": n,
        "coefficient": slope,
        "ci95": [float(low), float(high)],
        "draws": len(estimates),
        "se": float(np.std(estimates, ddof=1)),
    }


def _positive(evidence: dict[str, Any]) -> bool:
    interval = evidence.get("ci95")
    return bool(
        interval
        and evidence.get("coefficient") is not None
        and interval[0] > 0.0
        and evidence["coefficient"] > 0.0
    )


def summarize_kyle(events: dict[str, np.ndarray], state: dict[str, Any]) -> dict[str, Any]:
    """Four independent measurements of Kyle lambda, and the identification call.

    ``kyle_lambda`` is price change per 1,000 shares of signed flow, the unit
    :meth:`QueuePriorityValuationModel.evaluate_queue_value` prices a fill in, so
    only the specifications whose regressor is signed print size can carry the
    value. The depth-normalised flow regression measures the same impact in a
    different unit and is reported as evidence; a print's size and the depth it
    trades through move together by construction, so agreement between the two
    is corroboration, not an independent test.
    """
    y_columns = {horizon: f"dmid_{horizon:g}" for horizon in KYLE_HORIZONS}
    x = events["signed_size"].astype(np.float64) / 1000.0
    asset = events["asset"]
    keep_asset = x.size > 0
    if not keep_asset:
        return {
            "identified": False,
            "value": 0.0,
            "sample_size": 0,
            "reason": "no event rows",
            "state": state,
        }
    tokens, groups = np.unique(asset, return_inverse=True)
    result: dict[str, Any] = {
        "sample_size": int(x.size),
        "markets": int(tokens.size),
        "horizons_s": list(KYLE_HORIZONS),
        "specifications": {},
        "roles": {
            "event_study_ols": "event study: pooled slope of the print's mid move on its signed size",
            "event_study_fixed_effects": "event study, per-market panel: the same slope from within-market variation",
            "flow_depth_ols": "OFI regression: pooled slope on the depth-normalised level-1 OFI of the same window",
            "flow_depth_fixed_effects": "OFI regression, per-market panel: the same slope from within-market variation",
            "event_study_huber": "event study, Huber M-estimate: the slope with tail prints down-weighted",
            "event_study_lad": "event study, median regression: the slope of the median print",
        },
        "state": state,
    }
    candidates: list[tuple[str, dict[str, Any]]] = []
    for horizon in KYLE_HORIZONS:
        y = events[y_columns[horizon]].astype(np.float64)
        usable = np.isfinite(y) & np.isfinite(x)
        flow = events[f"flow_{horizon:g}"].astype(np.float64)
        flow_ok = usable & np.isfinite(flow)
        spec: dict[str, Any] = {}
        spec["event_study_ols"] = _ols_evidence(x[usable], y[usable])
        spec["event_study_fixed_effects"] = _ols_evidence(
            x[usable], y[usable], groups[usable]
        )
        if np.count_nonzero(flow_ok) >= KYLE_MIN_EVENTS:
            spec["flow_depth_ols"] = _ols_evidence(flow[flow_ok], y[flow_ok])
            spec["flow_depth_fixed_effects"] = _ols_evidence(
                flow[flow_ok], y[flow_ok], groups[flow_ok]
            )
        if int(np.count_nonzero(usable)) >= KYLE_MIN_EVENTS:
            spec["event_study_huber"] = _huber_evidence(x[usable], y[usable])
            spec["event_study_lad"] = _lad_evidence(
                x[usable], y[usable], groups[usable]
            )
        result["specifications"][f"{horizon:g}s"] = spec
        if horizon == 60.0:
            candidates = [
                ("event_study_fixed_effects", spec["event_study_fixed_effects"]),
                ("event_study_ols", spec["event_study_ols"]),
                ("event_study_huber", spec.get("event_study_huber", {})),
                ("event_study_lad", spec.get("event_study_lad", {})),
            ]

    identified = [(name, evidence) for name, evidence in candidates if _positive(evidence)]
    if identified:
        name, evidence = identified[0]
        result.update(
            {
                "identified": True,
                "value": float(evidence["coefficient"]),
                "estimator": name,
                "ci95": evidence.get("ci95"),
                "se": evidence.get("se"),
                "r2": evidence.get("r2"),
                "n": evidence.get("n"),
                "market_effect": name.endswith("fixed_effects"),
            }
        )
    else:
        signed_size = sorted(
            {
                name
                for horizon, spec in result["specifications"].items()
                for name, evidence in spec.items()
                if name.startswith("event_study")
            }
        )
        flow_positive = [
            f"{horizon}:{name}"
            for horizon, spec in result["specifications"].items()
            for name, evidence in spec.items()
            if name.startswith("flow_depth") and _positive(evidence)
        ]
        result.update(
            {
                "identified": False,
                "value": 0.0,
                "estimator": None,
                "rejection": {
                    "rule": (
                        "the Kyle term is dropped (lambda = 0) unless a signed-size "
                        "specification puts a positive 95 % interval on the slope; "
                        "a negative slope is not an impact and a null slope prices "
                        "no impact"
                    ),
                    "units": (
                        "the signed-size specifications report price change per "
                        "1,000 shares, the unit the queue valuation prices a fill "
                        "in; the flow-depth specifications report price change per "
                        "unit of depth-normalised OFI and cannot be substituted for "
                        "lambda"
                    ),
                    "signed_size_specifications": signed_size,
                    "ofi_regression_positive_in_own_unit": flow_positive,
                    "specifications": {name: evidence for name, evidence in candidates},
                },
            }
        )
    return result


def measure_cancellations(
    books: dict[str, Any], prints: dict[str, Any]
) -> dict[str, Any]:
    """Cancelled shares between book snapshots, net of prints, per depth per s."""
    total_cancelled = 0.0
    total_depth_seconds = 0.0
    intervals = 0
    for asset, snaps in books["book_rows"].items():
        asset_prints = prints["by_asset"].get(asset)
        for (t0, bids0, asks0), (t1, bids1, asks1) in itertools.pairwise(snaps):
            dt = t1 - t0
            if dt <= 0.0:
                continue
            prev = {("B", p): s for p, s in bids0}
            prev.update({("A", p): s for p, s in asks0})
            cur = {("B", p): s for p, s in bids1}
            cur.update({("A", p): s for p, s in asks1})
            depth = sum(prev.values()) / len(prev) if prev else 0.0
            if depth <= 0.0:
                continue
            removed = 0.0
            for key, size in prev.items():
                after = cur.get(key, 0.0)
                if after < size:
                    removed += size - after
            if removed > 0.0 and asset_prints:
                printed = sum(
                    size
                    for ts, price, size, _side in asset_prints
                    if t0 < ts <= t1
                    and any(
                        abs(price - key[1]) <= 1e-9
                        for key in prev
                    )
                )
                removed = max(0.0, removed - printed)
            total_cancelled += removed
            total_depth_seconds += depth * dt
            intervals += 1
    intensity = (
        total_cancelled / total_depth_seconds if total_depth_seconds > 0.0 else None
    )
    return {
        "value": intensity,
        "cancelled_shares": total_cancelled,
        "depth_seconds": total_depth_seconds,
        "intervals": intervals,
        "assets": len(books["book_rows"]),
    }


def measure_touch_rate(
    books: dict[str, Any], prints: dict[str, Any]
) -> dict[str, Any]:
    """Shares printed at the touch per second per active asset, EWMA over buckets."""
    touch_volume: dict[int, float] = {}
    touch_prints = 0
    used_prints = 0
    for asset, rows_ in prints["by_asset"].items():
        quotes = books["quote_rows"].get(asset)
        if not quotes:
            continue
        for ts, price, size, side in rows_:
            quote = None
            lo, hi = 0, len(quotes)
            while lo < hi:
                middle = (lo + hi) // 2
                if quotes[middle][0] <= ts:
                    lo = middle + 1
                else:
                    hi = middle
            if lo:
                quote = quotes[lo - 1]
            if quote is None:
                continue
            used_prints += 1
            _, bid, ask = quote
            at_touch = (
                abs(price - bid) <= 1e-9
                or abs(price - ask) <= 1e-9
            )
            if not at_touch:
                continue
            touch_prints += 1
            bucket = int(ts // BUCKET_SECONDS)
            touch_volume[bucket] = touch_volume.get(bucket, 0.0) + size
            del side
    active: dict[int, set[str]] = {}
    for asset, rows_ in books["quote_rows"].items():
        for ts, _bid, _ask in rows_:
            active.setdefault(int(ts // BUCKET_SECONDS), set()).add(asset)
    buckets = sorted(set(touch_volume) | set(active))
    series: list[float] = []
    touch_shares = 0.0
    active_asset_seconds = 0.0
    for bucket in buckets:
        assets_active = len(active.get(bucket, ()))
        if assets_active <= 0:
            continue
        touch_shares += touch_volume.get(bucket, 0.0)
        active_asset_seconds += assets_active * BUCKET_SECONDS
        series.append(touch_volume.get(bucket, 0.0) / (assets_active * BUCKET_SECONDS))
    value = _ewma(series)
    return {
        "value": value,
        "buckets": len(series),
        "touch_prints": touch_prints,
        "prints_with_a_quote": used_prints,
        "prints": sum(len(v) for v in prints["by_asset"].values()),
        # The two totals behind the EWMA: a pooled rate is
        # sum(touch_shares) / sum(active_asset_seconds), never a mean of the
        # per-partition EWMAs.
        "touch_shares": touch_shares,
        "active_asset_seconds": active_asset_seconds,
    }


# ---------------------------------------------------------------------------
# Capital hurdle and queue value
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Replace ``path`` in one rename: a reader never sees a half-written fit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def _nearest_cell(cells: list[dict], tick: float, horizon: float, min_flow: float | None = None) -> dict | None:
    candidates = [
        cell
        for cell in cells
        if cell.get("horizon_s") is not None
        and (min_flow is None or cell.get("min_flow_shares") is not None)
    ]
    if not candidates:
        return None

    def distance(cell: dict) -> tuple[float, float, float]:
        d_tick = abs(math.log(max(1e-9, float(cell.get("tick") or 1e-9)) / max(1e-9, tick)))
        d_h = abs(float(cell["horizon_s"]) - horizon)
        d_flow = (
            0.0
            if min_flow is None
            else abs(math.log(max(1e-9, float(cell.get("min_flow_shares") or 1e-9)) / max(1e-9, min_flow)))
        )
        return (d_h, d_tick, d_flow)

    return min(candidates, key=distance)


def measure_capital_hurdle(
    universe: dict[str, dict[str, Any]],
    books: dict[str, Any],
    max_markets: int,
    trial_horizon_s: float,
) -> dict[str, Any]:
    """Best alternative reward yield per dollar, net of measured fill costs.

    Every pool is measured at one common capital so the pools are comparable:
    the largest venue-minimum notional in the sample. That is the amount needed
    to qualify in the most demanding pool, so every sampled pool is deployable
    at that capital. Measuring each pool at *its own* minimum instead made a
    $6.40 minimum lot earning $2.27/day report a 12,700 %/yr opportunity cost,
    which no larger allocation can reproduce: the pool pays at most its daily
    rate whatever the capital, so the per-dollar yield must be computed at one
    deployable scale.
    """
    markout_payload = _load_json(MARKOUT) or {}
    fill_payload = _load_json(FILL_HORIZON) or {}
    markout_cells = markout_payload.get("cells") or []
    fill_cells = fill_payload.get("cells") or []

    priced: list[dict[str, Any]] = []
    stride = max(1, len(universe) // max(1, max_markets))
    for index, (condition_id, params) in enumerate(universe.items()):
        if index % stride:
            continue
        legs = [books["book_rows"].get(token) for token in params["tokens"]]
        if not all(legs):
            continue
        # Align the two legs: take the latest snapshot of one leg and the last
        # snapshot of the other at or before it. Snapshot lists are sorted.
        anchor_ts = legs[0][-1][0]
        prior_other = [snap for snap in legs[1] if snap[0] <= anchor_ts]
        if not prior_other:
            anchor_ts = legs[1][-1][0]
            prior_self = [snap for snap in legs[0] if snap[0] <= anchor_ts]
            if not prior_self:
                continue
            last = [prior_self[-1], legs[1][-1]]
        else:
            last = [legs[0][-1], prior_other[-1]]
        bids = [row[1] for row in last]
        asks = [row[2] for row in last]
        mids = []
        for bid_levels, ask_levels in zip(bids, asks, strict=True):
            if bid_levels and ask_levels and bid_levels[0][0] < ask_levels[0][0]:
                mids.append((bid_levels[0][0] + ask_levels[0][0]) * 0.5)
            else:
                mids.append(None)
        if any(m is None for m in mids):
            continue
        leg = 0 if mids[0] <= mids[1] else 1
        quote = (bids[leg][0][0] if bids[leg] else 0.0) + params["tick"]
        ask = asks[leg][0][0] if asks[leg] else 0.0
        if not (0.0 < quote < ask):
            quote = bids[leg][0][0] if bids[leg] else 0.0
        if not (0.0 < quote < 1.0):
            continue
        priced.append(
            {
                "condition_id": condition_id,
                "quote": quote,
                "params": params,
                "leg_book": _as_book(bids[leg], asks[leg]),
                "other_book": _as_book(bids[1 - leg], asks[1 - leg]),
                "minimum_notional": params["min_size"] * quote,
            }
        )
    if not priced:
        return {
            "value": None,
            "markets": 0,
            "common_capital_usd": 0.0,
            "distribution": {},
            "net_basis": None,
            "best_market": None,
            "market_yields": [],
        }
    common_capital = max(row["minimum_notional"] for row in priced)
    markets: list[dict[str, Any]] = []
    for row in priced:
        params = row["params"]
        quote = row["quote"]
        # Every pool is quoted at the same capital; shares are therefore the
        # participant's score-driving quantity and are >= the venue minimum.
        shares = common_capital / quote
        share = measured_accrual(
            pool_usd_per_day=params["rate"],
            price=quote,
            shares=shares,
            min_size=params["min_size"],
            max_spread_cents=params["max_spread"],
            leg_book=row["leg_book"],
            other_book=row["other_book"],
            competing=None,
            books_include_our_order=False,
        )
        if share is None:
            continue
        gross = share.usd_per_day
        gross_annual = gross / common_capital * 365.25
        net_annual = gross_annual
        cost_per_day = 0.0
        basis = "gross (no measured markout/fill cell for this market)"
        markout_cell = _nearest_cell(markout_cells, params["tick"], trial_horizon_s)
        fill_cell = _nearest_cell(
            fill_cells, params["tick"], trial_horizon_s, min_flow=params["min_size"]
        )
        if markout_cell is not None and fill_cell is not None:
            p_fill = float(fill_cell.get("p_fill") or 0.0)
            horizon = float(fill_cell["horizon_s"])
            fills_per_day = p_fill * 86_400.0 / horizon if horizon > 0.0 else 0.0
            cost_per_day = (
                float(markout_cell.get("mean_markout_usd_per_share") or 0.0)
                * shares
                * fills_per_day
            )
            net_annual = (gross - cost_per_day) / common_capital * 365.25
            basis = (
                f"gross - markout({markout_cell['horizon_s']:.0f}s, "
                f"tick {markout_cell['tick']}) x p_fill({p_fill:.4g} @ {horizon:.0f}s)"
            )
        markets.append(
            {
                "condition_id": row["condition_id"],
                "quote": quote,
                "capital": common_capital,
                "gross_usd_per_day": gross,
                "cost_usd_per_day": cost_per_day,
                "share": share.share,
                "net_annualized": net_annual,
                "basis": basis,
                "competing_score": share.competing_score,
            }
        )
    best = max(markets, key=lambda m: m["net_annualized"], default=None)
    values = [m["net_annualized"] for m in markets]
    distribution = _quantiles(values, (0.1, 0.5, 0.9))
    # The opportunity cost is the 90th percentile of measured pool yields, the
    # explicit target stated once in the artifact. The maximum is a single
    # empty pool and is an extreme-value statistic over 1,300 noisy estimates;
    # the median is negative. The p90 is the measured yield a dollar can
    # actually expect to find among the best pools and is reported with the
    # full distribution so the choice is auditable.
    value = distribution.get("p90")
    if value is not None and value < 0.0:
        # A negative opportunity cost is not a valid hurdle. Refuse rather
        # than clamp it to zero: the pool sample priced no positive
        # alternative, which is missing evidence, not a zero.
        value = None
    return {
        "value": value,
        "target_quantile": 0.9,
        "best_market": best,
        "markets": len(markets),
        "common_capital_usd": common_capital,
        "distribution": distribution,
        "net_basis": best["basis"] if best else None,
        "market_yields": markets,
        "sample_markets": markets[:20],
        "top_markets": sorted(
            markets, key=lambda m: m["net_annualized"], reverse=True
        )[:10],
    }


def replay_queue_value(
    universe: dict[str, dict[str, Any]],
    books: dict[str, Any],
    prints: dict[str, Any],
    series: dict[str, list[tuple[float, float]]],
    trial_horizon_s: float,
    max_trials: int,
) -> dict[str, Any]:
    """Replay the FIFO tracker on a grid of quote prices and sizes."""
    token_to_condition = {
        token: condition_id
        for condition_id, params in universe.items()
        for token in params["tokens"]
    }
    trials: list[dict[str, Any]] = []
    candidates = [
        (asset, snap_index)
        for asset, snaps in books["book_rows"].items()
        if asset in token_to_condition
        for snap_index in range(len(snaps))
    ]
    # Each sampled snapshot produces 3 prices x 3 sizes = 9 trials; the cap is
    # on trials, so the snapshot stride is chosen accordingly.
    target_snapshots = max(1, int(max_trials) // 9)
    stride = max(1, math.ceil(len(candidates) / target_snapshots))

    for asset, snap_index in candidates[::stride]:
        params = universe[token_to_condition[asset]]
        snaps = books["book_rows"][asset]
        ts, bids, asks = snaps[snap_index]
        if not bids or not asks:
            continue
        best_bid_price = bids[0][0]
        best_ask_price = asks[0][0]
        entry_mid = (best_bid_price + best_ask_price) * 0.5
        bid_quote = best_bid_price + params["tick"]
        if bid_quote >= best_ask_price:
            bid_quote = best_bid_price
        price_grid = [
            bid_quote,
            max(params["tick"], bid_quote - params["tick"]),
            max(params["tick"], bid_quote - 2 * params["tick"]),
        ]
        size_grid = [
            params["min_size"],
            params["min_size"] * 2.0,
            params["min_size"] * 5.0,
        ]
        end = ts + trial_horizon_s
        timeline: list[tuple[float, int, Any]] = [
            (snap[0], 0, snap) for snap in snaps if ts < snap[0] <= end
        ]
        timeline += [
            (p[0], 1, p) for p in prints["by_asset"].get(asset, []) if ts < p[0] <= end
        ]
        timeline.sort(key=lambda item: (item[0], item[1]))
        other_leg = [
            token for token in params["tokens"] if token != asset
        ]
        other_snaps = books["book_rows"].get(other_leg[0]) if other_leg else None
        other_book = None
        if other_snaps:
            prior = [snap for snap in other_snaps if snap[0] <= ts]
            if prior:
                other_book = _as_book(prior[-1][1], prior[-1][2])
        for offset_index, price in enumerate(price_grid):
            if not (0.0 < price < 1.0):
                continue
            for size_index, size in enumerate(size_grid):
                tracker = QueuePositionTracker()
                tracker.on_entry(
                    "probe", asset, "BUY", price, size, ts, bids, asks
                )
                filled_at: float | None = None
                for event_ts, kind, payload in timeline:
                    progress = tracker.progress("probe")
                    if kind == 0:
                        tracker.on_book(
                            asset,
                            _with_own_order(payload[1], price, size),
                            payload[2],
                            event_ts,
                        )
                    else:
                        _ts, print_price, print_size, side = payload
                        tracker.on_trade(
                            asset, print_price, print_size, side, event_ts
                        )
                    after = tracker.progress("probe")
                    if (
                        filled_at is None
                        and after is not None
                        and after.filled
                        and (progress is None or not progress.filled)
                    ):
                        filled_at = event_ts
                progress = tracker.progress("probe")
                if progress is None:
                    continue
                if progress.filled and progress.traded_at_level <= progress.initial_ahead:
                    consumed = size
                else:
                    consumed = min(
                        size, max(0.0, progress.traded_at_level - progress.initial_ahead)
                    )
                filled_fraction = consumed / size if size > 0.0 else 0.0
                rest_seconds = (
                    (filled_at - ts) if filled_at is not None else trial_horizon_s
                )
                accrual_usd = 0.0
                accrual_measured = False
                if other_book is not None:
                    share = measured_accrual(
                        pool_usd_per_day=params["rate"],
                        price=price,
                        shares=size,
                        min_size=params["min_size"],
                        max_spread_cents=params["max_spread"],
                        leg_book=_as_book(bids, asks),
                        other_book=other_book,
                        competing=None,
                        books_include_our_order=False,
                    )
                    if share is not None:
                        accrual_usd = share.usd_per_day * max(0.0, rest_seconds) / 86_400.0
                        accrual_measured = True
                markout = 0.0
                markout_measured = False
                if filled_at is not None:
                    midpoint = series.get(asset)
                    m_fill = _mid_at(midpoint, filled_at)
                    m_later = _mid_at(midpoint, filled_at + trial_horizon_s)
                    if m_fill is not None and m_later is not None:
                        # Positive markout is the adverse drift a buy pays.
                        markout = m_fill - m_later
                        markout_measured = True
                entry_edge = entry_mid - price
                per_share = accrual_usd / size + filled_fraction * (entry_edge - markout)
                trials.append(
                    {
                        "asset": asset,
                        "condition_id": token_to_condition[asset],
                        "ts": ts,
                        "price_offset_ticks": offset_index,
                        "size_multiple": size_index + 1,
                        "p_fill": 1.0 if consumed > 0.0 else 0.0,
                        "filled_fraction": filled_fraction,
                        "entry_edge_usd": entry_edge,
                        "accrual_usd_per_share": accrual_usd / size,
                        "rest_seconds": rest_seconds,
                        "markout_usd_per_share": markout,
                        "markout_measured": markout_measured,
                        "accrual_measured": accrual_measured,
                        "value_per_share_usd": per_share,
                        "value_per_share_per_sec": per_share / trial_horizon_s
                        if trial_horizon_s > 0.0
                        else 0.0,
                    }
                )

    complete = [t for t in trials if t["accrual_measured"]]
    values = [t["value_per_share_per_sec"] for t in complete]
    p_fill = sum(t["p_fill"] for t in trials) / len(trials) if trials else 0.0
    by_offset: dict[str, Any] = {}
    for offset in sorted({t["price_offset_ticks"] for t in trials}):
        subset = [t for t in complete if t["price_offset_ticks"] == offset]
        by_offset[str(offset)] = {
            "trials": len(subset),
            "p_fill": (
                sum(t["p_fill"] for t in subset) / len(subset) if subset else 0.0
            ),
            "value_per_share_per_sec": _quantiles(
                [t["value_per_share_per_sec"] for t in subset], (0.5,)
            ),
        }
    by_size: dict[str, Any] = {}
    for multiple in sorted({t["size_multiple"] for t in trials}):
        subset = [t for t in complete if t["size_multiple"] == multiple]
        by_size[str(multiple)] = {
            "trials": len(subset),
            "p_fill": sum(t["p_fill"] for t in subset) / len(subset),
            "value_per_share_per_sec": _quantiles(
                [t["value_per_share_per_sec"] for t in subset], (0.5,)
            ),
        }
    accrual_values = [
        t["accrual_usd_per_share"] / t["rest_seconds"]
        for t in complete
        if t["rest_seconds"] > 0.0
    ]
    return {
        "trials": len(trials),
        "trials_with_accrual": len(complete),
        "trials_filled": sum(1 for t in trials if t["p_fill"] > 0.0),
        "p_fill": p_fill,
        "distribution": _quantiles(values, (0.1, 0.5, 0.9)),
        "reward_accrual_usd_per_share_sec": _quantiles(
            accrual_values, (0.1, 0.5, 0.9)
        ),
        # The raw per-trial accrual rates are what a pooled median needs; the
        # quantile summary above cannot be medianed across partitions.
        "reward_accrual_values": accrual_values,
        "by_price_offset_ticks": by_offset,
        "by_size_multiple": by_size,
        "finding": (
            "queue value is approximately zero per unit time on the complete "
            "value trials"
            if values and abs(sum(values) / len(values)) < 1e-9
            else "queue value is non-zero per unit time; see distribution"
        ),
        "sample": trials[:500],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fit the FIFO queue model from tape")
    parser.add_argument("--date", default=None, help="date=YYYY-MM-DD partition (default: latest)")
    parser.add_argument("--hours", nargs="*", default=None, help="hour partitions, e.g. 14")
    parser.add_argument("--budget-seconds", type=float, default=600.0)
    parser.add_argument("--max-trials", type=int, default=20000)
    parser.add_argument("--max-markets", type=int, default=2000)
    parser.add_argument("--out", default=str(QUEUE_FIT))
    parser.add_argument("--value-out", default=str(QUEUE_VALUE))
    parser.add_argument(
        "--kyle-dates",
        nargs="*",
        default=None,
        help="date partitions for the print-level Kyle fit (default: every date under the tape)",
    )
    parser.add_argument(
        "--kyle-hours",
        nargs="*",
        default=None,
        help=(
            "hour partitions of the Kyle fit as YYYY-MM-DDTHH (default: every "
            "hour of every selected date)"
        ),
    )
    parser.add_argument("--kyle-workers", type=int, default=KYLE_WORKERS)
    parser.add_argument(
        "--kyle-budget-seconds",
        type=float,
        default=3600.0,
        help="wall-clock budget for the print-level Kyle fit alone",
    )
    parser.add_argument("--rebuild-kyle", action="store_true")
    return parser.parse_args(argv)


def _latest_partition(files: list[Path]) -> str:
    names = [path.parent.parent.name for path in files]
    with_date = [name for name in names if name.startswith("date=")]
    return max(with_date).split("=", 1)[1]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    started = time.time()
    deadline = started + max(1.0, float(args.budget_seconds))

    def remaining() -> float:
        return deadline - time.time()

    def log_stage(stage: str, before: int, after: int, extra: str = "") -> None:
        log.info(
            f"stage {stage}: rows_in={before:,} rows_out={after:,} "
            f"elapsed={time.time() - started:.1f}s remaining={remaining():.0f}s {extra}"
        )

    all_event_files = tape_files(TAPE, table="clob_events")
    all_trade_files = tape_files(TAPE, table="clob_trades")
    if not all_event_files or not all_trade_files:
        raise FileNotFoundError("no clob_events or clob_trades parts under data/tape")
    date = args.date or _latest_partition(all_event_files)
    hours = args.hours or [all_event_files[-1].parent.name.split("=", 1)[1]]
    event_files = [
        path
        for path in all_event_files
        if path.parent.parent.name == f"date={date}" and path.parent.name in {f"hour={h}" for h in hours}
    ]
    trade_files = [
        path
        for path in all_trade_files
        if path.parent.parent.name == f"date={date}" and path.parent.name in {f"hour={h}" for h in hours}
    ]
    if not event_files or not trade_files:
        raise FileNotFoundError(
            f"no parts for date={date} hours={hours} "
            f"(events={len(event_files)}, trades={len(trade_files)})"
        )
    print(
        f"queue fit: date={date} hours={hours} events={len(event_files)} part(s) "
        f"trades={len(trade_files)} part(s) budget={args.budget_seconds:.0f}s"
    )

    output: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "window": {"start": None, "end": None},
        "sample_size": {},
        "source": {
            "events": [str(path) for path in event_files],
            "trades": [str(path) for path in trade_files],
            "census": str(CENSUS),
            "markout": str(MARKOUT),
            "fill_horizon": str(FILL_HORIZON),
        },
        "definition": {
            "trade_arrival_rate_shares_sec": (
                "shares printed at the touch per second per active market; EWMA "
                "(alpha=2/(n+1)) over 60 s buckets"
            ),
            "cancellation_rate_intensity": (
                "shares removed from a level between consecutive full book "
                "snapshots, net of prints in the same interval, divided by "
                "mean displayed depth and elapsed seconds"
            ),
            "kyle_lambda": (
                "event-study slope of the mid change at +10 s / +60 s on the "
                "signed print size per 1000 shares (taker side signs it), with "
                "the depth-normalised level-1 OFI of the same window as a "
                "second regressor, a Huber and a median fit for tail prints, "
                "and per-market fixed effects; the value is the first positive "
                "specification (fixed effects first), else 0"
            ),
            "volatility_annualized": (
                "sqrt(sum(log(mid_t/mid_t-1)^2)/sum(dt) * 365.25*86400); "
                "zero-mean realised variance, 24/7 calendar"
            ),
            "annual_capital_hurdle_rate": (
                "90th percentile (explicit target) of the net annualised yield "
                "of a rewarded pool at one common deployable capital (the "
                "largest venue-minimum notional in the sample): pool rate x "
                "measured share / capital, net of the measured markout x "
                "measured fill rate"
            ),
        },
        "partial": False,
    }
    value_output: dict[str, Any] = {
        "generated_at": output["generated_at"],
        "window": output["window"],
        "source": output["source"],
        "definition": {
            "observation": (
                "a resting buy at a grid of prices/sizes on a full book "
                "snapshot; FIFO position replayed with QueuePositionTracker "
                "over the next measured fill horizon"
            ),
            "value": (
                "reward accrual per share while resting + filled fraction x "
                "(entry edge - adverse markout); divided by the trial horizon"
            ),
            "adverse_markout": "mid(at fill) - mid(at fill + trial horizon)",
        },
        "partial": False,
    }

    learners = ["trade_arrival_rate_shares_sec", "cancellation_rate_intensity",
                "kyle_lambda", "volatility_annualized", "annual_capital_hurdle_rate"]
    for key in learners:
        output[key] = None
    output["fields"] = {key: {"value": None} for key in learners}

    # Stage 1: universe
    universe = load_universe()
    output["sample_size"]["rewarded_markets"] = len(universe)

    # Stage 2: prints
    before = sum(1 for _ in trade_files)
    prints = load_prints(trade_files)
    output["sample_size"]["prints"] = prints["rows"]
    log_stage("prints", before, prints["rows"])

    # Stage 3: books and quotes
    books = load_books(event_files)
    output["sample_size"]["book_snapshots"] = books["book_count"]
    output["sample_size"]["quote_updates"] = books["quote_count"]
    timestamps = [
        row[0] for snaps in books["book_rows"].values() for row in snaps
    ] + [row[0] for rows_ in books["quote_rows"].values() for row in rows_]
    timestamps += [p[0] for rows_ in prints["by_asset"].values() for p in rows_]
    if timestamps:
        output["window"] = {
            "start": _iso(int(min(timestamps) * 1e9)),
            "end": _iso(int(max(timestamps) * 1e9)),
        }
        value_output["window"] = output["window"]
    log_stage("books", books["rows"], books["book_count"] + books["quote_count"])

    book_assets = set(books["book_rows"])
    quote_assets = set(books["quote_rows"])
    output["sample_size"]["assets_with_books"] = len(book_assets)
    output["sample_size"]["assets_with_quotes"] = len(quote_assets)

    if remaining() <= 0:
        output["partial"] = True
    else:
        series = mid_series(books)
        output["sample_size"]["assets_with_mid_series"] = len(series)

        volatility = measure_volatility(series)
        output["volatility_annualized"] = volatility["value"]
        output["fields"]["volatility_annualized"] = {
            "value": volatility["value"],
            "sample_size": volatility.get("returns"),
            "window": output["window"],
            "definition": output["definition"]["volatility_annualized"],
            "source": "clob_events best_bid_ask / book mids",
            "estimator": "pooled zero-mean realised variance per second",
            "assets": volatility.get("assets"),
            "seconds": volatility.get("seconds"),
        }
        print(f"volatility: {volatility}")
        log_stage("volatility", len(series), volatility.get("returns") or 0)

    if remaining() <= 0:
        output["partial"] = True
    else:
        cancels = measure_cancellations(books, prints)
        output["cancellation_rate_intensity"] = cancels["value"]
        output["fields"]["cancellation_rate_intensity"] = {
            "value": cancels["value"],
            "sample_size": cancels["intervals"],
            "window": output["window"],
            "definition": output["definition"]["cancellation_rate_intensity"],
            "source": "clob_events book snapshots net of clob_trades prints",
            "estimator": "cancelled shares / (mean depth x elapsed seconds)",
            "cancelled_shares": cancels["cancelled_shares"],
            "depth_seconds": cancels["depth_seconds"],
            "assets": cancels["assets"],
        }
        print(f"cancels: {cancels}")
        log_stage("cancels", len(books["book_rows"]), cancels["intervals"])

    if remaining() <= 0:
        output["partial"] = True
    else:
        touch = measure_touch_rate(books, prints)
        output["trade_arrival_rate_shares_sec"] = touch["value"]
        output["fields"]["trade_arrival_rate_shares_sec"] = {
            "value": touch["value"],
            "sample_size": touch["buckets"],
            "window": output["window"],
            "definition": output["definition"]["trade_arrival_rate_shares_sec"],
            "source": "clob_trades prints at clob_events best bid/ask",
            "estimator": "EWMA over 60 s buckets",
            "touch_prints": touch["touch_prints"],
            "prints_with_a_quote": touch["prints_with_a_quote"],
            "prints": touch["prints"],
            "touch_shares": touch["touch_shares"],
            "active_asset_seconds": touch["active_asset_seconds"],
        }
        print(f"touch: {touch}")
        log_stage("touch", touch["prints"], touch["touch_prints"])

    # The fill/markout horizons are read from the measured artifacts, never set
    # here: the trial horizon is the longest measured fill horizon and the
    # markout horizon is the measured cell nearest it.
    fill_payload = _load_json(FILL_HORIZON) or {}
    fill_horizons = [
        float(cell["horizon_s"])
        for cell in fill_payload.get("cells") or []
        if cell.get("horizon_s") is not None
    ]
    trial_horizon = max(fill_horizons) if fill_horizons else 300.0
    value_output["trial_horizon_s"] = trial_horizon
    value_output["markout_horizon_s"] = trial_horizon

    if remaining() <= 0:
        output["partial"] = True
        value_output["partial"] = True
    else:
        hurdle = measure_capital_hurdle(universe, books, args.max_markets, trial_horizon)
        output["annual_capital_hurdle_rate"] = hurdle["value"]
        output["fields"]["annual_capital_hurdle_rate"] = {
            "value": hurdle["value"],
            "sample_size": hurdle["markets"],
            "window": output["window"],
            "definition": output["definition"]["annual_capital_hurdle_rate"],
            "source": "ctf_census reward parameters x clob_events books x measured markout/fill cells",
            "estimator": "90th percentile of net annualised pool yields at common capital",
            "target_quantile": hurdle["target_quantile"],
            "common_capital_usd": hurdle["common_capital_usd"],
            "distribution": hurdle["distribution"],
            "market_yields": hurdle["market_yields"],
            "net_basis": hurdle["net_basis"],
            "best_market": hurdle["best_market"],
        }
        print(f"capital hurdle: {hurdle['value']} over {hurdle['markets']} markets")
        log_stage("capital_hurdle", len(universe), hurdle["markets"])

    if remaining() <= 0:
        value_output["partial"] = True
    else:
        queue_value = replay_queue_value(
            universe, books, prints, series, trial_horizon, args.max_trials
        )
        value_output.update(
            {
                "sample_size": queue_value["trials"],
                "trials_with_accrual": queue_value["trials_with_accrual"],
                "trials_filled": queue_value["trials_filled"],
                "p_fill": queue_value["p_fill"],
                "distribution": queue_value["distribution"],
                "by_price_offset_ticks": queue_value["by_price_offset_ticks"],
                "by_size_multiple": queue_value["by_size_multiple"],
                "finding": queue_value["finding"],
                "sample": queue_value["sample"],
            }
        )
        print(
            f"queue value: {queue_value['trials']} trials, p_fill={queue_value['p_fill']:.4f}, "
            f"distribution={queue_value['distribution']}"
        )
        log_stage("queue_value", queue_value["trials"], queue_value["trials"])
        accrual = queue_value.get("reward_accrual_usd_per_share_sec") or {}
        output["reward_accrual_usd_per_share_sec"] = accrual.get("p50")
        output["fields"]["reward_accrual_usd_per_share_sec"] = {
            "value": accrual.get("p50"),
            "sample_size": queue_value.get("trials_with_accrual"),
            "window": output["window"],
            "definition": (
                "reward payout a resting bid at the venue minimum size earns per "
                "share per second, from the measured pool rate x measured share "
                "over the trial's resting seconds"
            ),
            "source": "ctf_census reward parameters x clob_events books (both legs)",
            "estimator": "median over accrual-measured queue-value trials",
            "distribution": accrual,
            "values": queue_value.get("reward_accrual_values"),
        }
        value_output["reward_accrual_usd_per_share_sec"] = accrual

    # The print-level Kyle fit runs last on its own budget: it reads every event
    # partition of every date (the state cost of the whole tool), while the
    # stages above read one day of books.
    dates = args.kyle_dates or sorted(
        folder.name.split("=", 1)[1]
        for folder in (TAPE / "clob_events").glob("date=*")
        if folder.is_dir()
    )
    kyle_hours: list[tuple[str, str]] | None = None
    if args.kyle_hours:
        kyle_hours = []
        for value in args.kyle_hours:
            if "T" not in value:
                raise ValueError(f"--kyle-hours expects YYYY-MM-DDTHH, got {value!r}")
            date_part, hour_part = value.split("T", 1)
            kyle_hours.append((date_part, hour_part))
        dates = sorted({date_part for date_part, _hour in kyle_hours})
    events, kyle_state = collect_kyle_events(
        dates,
        hours=kyle_hours,
        workers=args.kyle_workers,
        rebuild=args.rebuild_kyle,
        budget_s=float(args.kyle_budget_seconds),
    )
    kyle = summarize_kyle(events, kyle_state)
    output["kyle_lambda"] = float(kyle["value"])
    output["fields"]["kyle_lambda"] = {
        "value": float(kyle["value"]),
        # Not `sample_size`: the refit combiner sums every key with that name,
        # and this sample is the same pooled pool in each partial, so summing
        # it would report five copies of the events.
        "events": kyle.get("sample_size"),
        "markets": kyle.get("markets"),
        "window": output["window"],
        "definition": output["definition"]["kyle_lambda"],
        "source": (
            "clob_events price_change top-of-book states + clob_trades public "
            f"prints, date partitions {','.join(dates)}"
        ),
        "estimator": kyle.get("estimator") or "rejected (lambda = 0)",
        "identified": bool(kyle.get("identified")),
        "valuation_term": (
            f"priced: lambda * quote_size / 1000 per share ({kyle.get('estimator')})"
            if kyle.get("identified")
            else "removed: lambda = 0, so the Kyle adverse-selection term is "
            "exactly zero and the queue valuation prices no impact"
        ),
        "partitions": [f"{date}T{hour}" for date, hour in kyle_hours]
        if kyle_hours
        else dates,
        "horizons_s": kyle.get("horizons_s"),
        "specifications": kyle.get("specifications"),
        "rejection": kyle.get("rejection"),
        "ci95": kyle.get("ci95"),
        "r2": kyle.get("r2"),
        "state": kyle_state,
    }
    if kyle_state.get("partial"):
        output["partial"] = True
        value_output["partial"] = True
    print(
        f"kyle: identified={kyle.get('identified')} value={kyle.get('value')!r} "
        f"estimator={kyle.get('estimator')!r} over {kyle.get('sample_size')} events"
    )
    log_stage("kyle", len(dates), kyle.get("sample_size") or 0)

    elapsed = time.time() - started
    for payload in (output, value_output):
        payload["elapsed_s"] = round(elapsed, 2)
        payload["budget_seconds"] = float(args.budget_seconds)
    out_path = Path(args.out)
    value_path = Path(args.value_out)
    _write_json_atomic(out_path, output)
    _write_json_atomic(value_path, value_output)
    print(f"wrote {out_path} and {value_path} in {elapsed:.1f}s (partial={output['partial']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
