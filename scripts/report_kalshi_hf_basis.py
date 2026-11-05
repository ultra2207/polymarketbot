#!/usr/bin/env python3
"""E1: does the Kalshi high-frequency basis predict the Polymarket reprice?

The hypothesis register (``docs/kalshi-hypotheses.md``, H1) asks whether
``r_t(PM, next few seconds)`` is explained by ``basis_t = logit(kalshi) -
logit(pm)``. This script builds that panel from the live book tape:

1. **Snapshot grid.** Every row of ``data/tape/kalshi_books`` for a mapped
   ticker is a snapshot time. The Kalshi mid is ``(yes_best_bid + yes_best_ask)
   / 2``; rows with a missing side stay missing and are dropped, never filled.
2. **Polymarket mid at that time.** The last PM YES quote at or before the
   snapshot (ASOF join), at most ``MAX_STALE_SEC`` old, else null. The quote is
   rebuilt from ``clob_events`` exactly as ``report_kalshi_basis`` does: book
   snapshot mid, ``best_bid_ask`` mid, or the nested
   ``price_changes.best_bid``/``best_ask`` mid.
3. **Basis and forward return.** ``basis = logit(kalshi) - logit(pm)`` and
   ``ret(h) = logit(pm at t+h) - logit(pm at t)`` for h in {5 s, 15 s, 60 s},
   each side null-safe (a missing or stale quote gives a null, never a zero).
4. **Output.** One parquet per ``kalshi_books`` hour partition under
   ``data/derived/kalshi_hf_basis/hour=YYYYMMDDHH/part-0000.parquet``, one row
   per aligned (pair, snapshot). A partition already on disk is skipped unless
   ``--force`` is passed.
5. **Report.** Per-pair regression slope of the forward return on the basis,
   the cross-pair mean slope with the pair count and a clustered standard error
   (rows are not independent - a condition is one cluster), rank deciles of the
   forward return by basis (pooled, with the pair count in every bucket), and
   the number of pairs with at least ``MIN_ALIGNED`` aligned snapshots.

A partition is built only once its hour has closed: an hour the collector is
still appending to would otherwise be frozen at a partial state, because an
existing output is skipped. ``--force`` overrides both rules.

Every hour partition is processed and persisted on its own, so no single run
straddles the 600 s budget. The tape today is one hour long: the numbers this
prints are a smoke test of the harness, not a test of H1.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from polymarketbot.logs import get_logger  # noqa: E402

log = get_logger("report_kalshi_hf_basis")

DEFAULT_MAPPING = Path("data/derived/kalshi_mapping_v2.parquet")
DEFAULT_BOOKS = Path("data/tape/kalshi_books")
DEFAULT_TAPE = Path("data/tape")
DEFAULT_OUT = Path("data/derived/kalshi_hf_basis")

SECOND_NS = 1_000_000_000
MAX_STALE_SEC = 60  # a PM quote older than this at the snapshot is missing
HORIZONS_SEC = (5, 15, 60)  # forward PM horizons from H1
MIN_ALIGNED = 50  # aligned snapshots a pair needs to enter the slope panel
N_DECILES = 10
LOGIT_EPS = 1e-3
MIN_SLOPE_ROWS = 10  # aligned snapshots needed for one pair's regression


# --------------------------------------------------------------------- pure
def logit(values: Sequence[float]) -> np.ndarray:
    """``log(p/(1-p))`` with the price clamped to ``LOGIT_EPS .. 1-LOGIT_EPS``."""
    p = np.clip(np.asarray(values, dtype=np.float64), LOGIT_EPS, 1.0 - LOGIT_EPS)
    return np.log(p / (1.0 - p))


def basis(kalshi_mid: Sequence[float], pm_mid: Sequence[float]) -> np.ndarray:
    """``logit(kalshi) - logit(pm)``; null wherever either price is unusable."""
    k = np.asarray(kalshi_mid, dtype=np.float64).reshape(-1)
    p = np.asarray(pm_mid, dtype=np.float64).reshape(-1)
    if k.size != p.size:
        raise ValueError("kalshi and polymarket mids must be the same length")
    ok = (
        np.isfinite(k)
        & np.isfinite(p)
        & (k > 0.0)
        & (k < 1.0)
        & (p > 0.0)
        & (p < 1.0)
    )
    out = np.full(k.shape, np.nan, dtype=np.float64)
    out[ok] = logit(k[ok]) - logit(p[ok])
    return out


def asof_pos(
    src_ts: Sequence[int], targets: Sequence[int], max_stale_ns: int
) -> np.ndarray:
    """Index of the last source row at or before each target.

    ``-1`` when the source has no row at or before the target, or when that row
    is older than ``max_stale_ns``. ``src_ts`` must be sorted ascending.
    """
    s = np.asarray(src_ts, dtype=np.int64).reshape(-1)
    t = np.asarray(targets, dtype=np.int64).reshape(-1)
    out = np.full(t.shape, -1, dtype=np.int64)
    if s.size == 0 or t.size == 0:
        return out
    if s.size > 1 and np.any(np.diff(s) < 0):
        raise ValueError("src_ts must be sorted ascending")
    pos = np.searchsorted(s, t, side="right") - 1
    ok = pos >= 0
    idx = np.where(ok, pos, 0)
    keep = ok & ((t - s[idx]) <= np.int64(max_stale_ns))
    out[keep] = idx[keep]
    return out


def forward_returns(
    pm_ts: Sequence[int],
    pm_mid: Sequence[float],
    snapshot_ts: Sequence[int],
    horizons_ns: Sequence[int],
    max_stale_ns: int,
) -> Dict[int, np.ndarray]:
    """``logit(pm at t+h) - logit(pm at t)`` per horizon, nan-safe on both sides.

    Both endpoints use the same ASOF rule: the last quote at or before the
    target, at most ``max_stale_ns`` old. A quote that did not update between
    ``t`` and ``t+h`` therefore gives a return of exactly ``0.0`` - that is a
    measured absence of reprice, not a missing value - while a venue that
    stopped quoting gives nan.
    """
    ts = np.asarray(pm_ts, dtype=np.int64).reshape(-1)
    mid = np.asarray(pm_mid, dtype=np.float64).reshape(-1)
    if ts.size != mid.size:
        raise ValueError("pm timestamps and mids must be the same length")
    targets = np.asarray(snapshot_ts, dtype=np.int64).reshape(-1)
    at_t = asof_pos(ts, targets, max_stale_ns)
    out: Dict[int, np.ndarray] = {}
    for h in horizons_ns:
        at_h = asof_pos(ts, targets + np.int64(h), max_stale_ns)
        fwd = np.full(targets.shape, np.nan, dtype=np.float64)
        ok = (at_t >= 0) & (at_h >= 0)
        if ok.any():
            fwd[ok] = logit(mid[at_h[ok]]) - logit(mid[at_t[ok]])
        out[int(h)] = fwd
    return out


def ols_slope(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    """Slope of ``y`` on ``x`` with an intercept; ``None`` when undefined."""
    a = np.asarray(x, dtype=np.float64).reshape(-1)
    b = np.asarray(y, dtype=np.float64).reshape(-1)
    if a.size != b.size or a.size < 3:
        return None
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if a.size < 3:
        return None
    da = a - a.mean()
    saa = float(np.dot(da, da))
    if saa <= 0.0:
        return None
    return float(np.dot(da, b - b.mean()) / saa)


def pearson(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    """Pearson r of the finite pairs; ``None`` when fewer than 3 or no variance."""
    a = np.asarray(x, dtype=np.float64).reshape(-1)
    b = np.asarray(y, dtype=np.float64).reshape(-1)
    if a.size != b.size or a.size < 3:
        return None
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if a.size < 3:
        return None
    da = a - a.mean()
    db = b - b.mean()
    saa = float(np.dot(da, da))
    sbb = float(np.dot(db, db))
    if saa <= 0.0 or sbb <= 0.0:
        return None
    return float(np.dot(da, db) / math.sqrt(saa * sbb))


def pair_regression(x: Sequence[float], y: Sequence[float]) -> Optional[Dict[str, Any]]:
    """One pair's regression: slope, intercept, n, r. ``None`` when undefined."""
    a = np.asarray(x, dtype=np.float64).reshape(-1)
    b = np.asarray(y, dtype=np.float64).reshape(-1)
    if a.size != b.size:
        return None
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    slope = ols_slope(a, b)
    if slope is None:
        return None
    return {
        "slope": slope,
        "intercept": float(b.mean() - slope * a.mean()),
        "n": int(a.size),
        "r": pearson(a, b),
    }


def cluster_stats(values: Sequence[float]) -> Dict[str, Optional[float]]:
    """Mean of per-pair statistics with a standard error across pairs.

    Rows of two correlated tokens are not independent observations, so the
    dispersion comes from the pair-level distribution (one condition = one
    cluster), never from the row count.
    """
    v = np.asarray([x for x in values if x is not None and np.isfinite(x)], dtype=np.float64)
    k = int(v.size)
    out: Dict[str, Optional[float]] = {
        "pairs": k,
        "mean": None,
        "sd": None,
        "se": None,
        "t": None,
        "positive": None,
    }
    if k == 0:
        return out
    mean = float(v.mean())
    out["mean"] = mean
    out["positive"] = int((v > 0.0).sum())
    if k >= 2:
        sd = float(v.std(ddof=1))
        se = sd / math.sqrt(k)
        out["sd"] = sd
        out["se"] = se
        out["t"] = float(mean / se) if se > 0.0 else None
    return out


def decile_table(
    basis_values: Sequence[float],
    returns: Sequence[float],
    pair_ids: Sequence[str],
    n_deciles: int = N_DECILES,
) -> List[Dict[str, Any]]:
    """Rank-based deciles of ``returns`` by ``basis_values``, pooled.

    Rank cuts (not value cuts) keep every bucket the same size where the basis
    has ties, which these 1-cent tick ladders do. ``pairs`` is the number of
    distinct clusters contributing to the bucket - the count that matters, since
    the rows inside a bucket are not independent.
    """
    b = np.asarray(basis_values, dtype=np.float64).reshape(-1)
    r = np.asarray(returns, dtype=np.float64).reshape(-1)
    p = np.asarray(list(pair_ids), dtype=object).reshape(-1)
    if b.size != r.size or b.size != p.size:
        raise ValueError("basis, return and pair arrays must be the same length")
    ok = np.isfinite(b) & np.isfinite(r)
    b, r, p = b[ok], r[ok], p[ok]
    if b.size == 0:
        return []
    order = np.argsort(b, kind="mergesort")
    ranks = np.empty(order.size, dtype=np.int64)
    ranks[order] = np.arange(order.size, dtype=np.int64)
    bucket = np.minimum((ranks * n_deciles) // order.size, n_deciles - 1)
    rows: List[Dict[str, Any]] = []
    for d in range(n_deciles):
        sel = bucket == d
        if not sel.any():
            continue
        rows.append(
            {
                "decile": d + 1,
                "n": int(sel.sum()),
                "pairs": int(np.unique(p[sel]).size),
                "basis_lo": float(b[sel].min()),
                "basis_hi": float(b[sel].max()),
                "mean_ret": float(r[sel].mean()),
                "median_ret": float(np.median(r[sel])),
            }
        )
    return rows


def family_of(series: Optional[str], method: Optional[str]) -> str:
    """Coarse family label used for the breakdown (same rule as the H7 check)."""
    s = (series or "").upper()
    m = (method or "").lower()
    if m == "crypto_bound" or s.startswith(("KXBTC", "KXETH", "KXSOL")):
        return "crypto_ladder"
    if m == "treasury_bound" or s.startswith(("KX2YR", "KX5YR", "KX10YR", "KX30YR")):
        return "treasury"
    if s in ("KXFEDDECISION", "KXRATECUTCOUNT") or m.startswith("fed_"):
        return "fomc"
    if s in (
        "KXHOUSERACE",
        "KXHOUSEWINSTATE",
        "SENATEMS",
        "HOUSEAZ2",
        "KXAKSENATE",
        "KXDPRESPRIMARY",
    ):
        return "elections"
    return "other"


def fmt(value: Optional[float], width: int = 9, dp: int = 5) -> str:
    """Right-aligned number or ``n/a``."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a".rjust(width)
    return f"{float(value):.{dp}f}".rjust(width)


# ------------------------------------------------------------------ tape i/o
def hour_partitions(root: Path) -> List[Tuple[str, str, Path]]:
    """``(date, hour, dir)`` for every ``date=/hour=`` child, sorted."""
    if not root.is_dir():
        return []
    out: List[Tuple[str, str, Path]] = []
    for d in root.glob("date=*/hour=*"):
        out.append((d.parent.name[len("date=") :], d.name[len("hour=") :], d))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def partition_label(date: str, hour: str) -> str:
    """``date=2026-11-05`` + ``hour=19`` -> ``2026110519``."""
    return f"{date.replace('-', '')}{hour}"


def hour_bounds(date: str, hour: str) -> Tuple[int, int]:
    """``[start, end)`` nanoseconds of a ``date=/hour=`` UTC partition."""
    start = datetime.strptime(f"{date} {hour}", "%Y-%m-%d %H").replace(tzinfo=UTC)
    end = start + timedelta(hours=1)
    return int(start.timestamp() * 1e9), int(end.timestamp() * 1e9)


def partition_sealed(date: str, hour: str, now_ns: int) -> bool:
    """Is the hour over, i.e. will the collector stop appending to it?

    A partition that is still being written must not be turned into an output:
    the output is skipped once it exists, so a half-hour built today would stay
    frozen at whatever the tape held at that moment.
    """
    return now_ns >= hour_bounds(date, hour)[1]


def _window_files(root: Path, start_ns: int, end_ns: int) -> List[str]:
    """Parquet files whose ``date=/hour=`` partition intersects the window."""
    start = datetime.fromtimestamp(start_ns / 1e9, UTC).replace(
        minute=0, second=0, microsecond=0
    )
    end = datetime.fromtimestamp(end_ns / 1e9, UTC)
    want = set()
    t = start
    while t <= end:
        want.add((t.strftime("%Y-%m-%d"), t.strftime("%H")))
        t += timedelta(hours=1)
    files: List[str] = []
    for date, hour, d in hour_partitions(root):
        if (date, hour) in want:
            files.extend(str(p) for p in sorted(d.glob("*.parquet")))
    return files


def _parquet_list(files: Sequence[str]) -> str:
    if not files:
        raise SystemExit("no parquet files in the window")
    return "[" + ",".join(f"'{f}'" for f in files) + "]"


def load_mapping(con: duckdb.DuckDBPyConnection, path: Path) -> List[Dict[str, Any]]:
    """Mapped pairs with their family label."""
    frame = con.execute(
        f"""
        SELECT kalshi_ticker, series_ticker, polymarket_condition_id,
               polymarket_yes_token, direction, confidence, method
        FROM read_parquet('{path}')
        ORDER BY kalshi_ticker
        """
    ).fetch_df()
    rows: List[Dict[str, Any]] = []
    for r in frame.itertuples(index=False):
        rows.append(
            {
                "ticker": str(r.kalshi_ticker),
                "series": str(r.series_ticker) if r.series_ticker is not None else None,
                "cid": str(r.polymarket_condition_id),
                "yes_token": str(r.polymarket_yes_token),
                "direction": str(r.direction) if r.direction is not None else None,
                "confidence": float(r.confidence) if r.confidence is not None else None,
                "method": str(r.method) if r.method is not None else None,
                "family": family_of(
                    str(r.series_ticker) if r.series_ticker is not None else None,
                    str(r.method) if r.method is not None else None,
                ),
            }
        )
    return rows


def load_kalshi_snapshots(
    con: duckdb.DuckDBPyConnection,
    partition: Path,
    tickers: Sequence[str],
) -> Any:
    """One ``kalshi_books`` partition -> (ticker, ts, kalshi mid) frame.

    The mid is the YES bid/ask midpoint; a row with an empty side is dropped
    (the tape writes ``mid = null`` there and the basis needs both sides).
    """
    files = [str(p) for p in sorted(partition.glob("*.parquet"))]
    listing = _parquet_list(files)
    marks = ",".join("?" for _ in tickers)
    return con.execute(
        f"""
        SELECT ticker, recv_ts_ns,
               CASE
                 WHEN yes_best_bid IS NOT NULL AND yes_best_ask IS NOT NULL
                      AND yes_best_ask >= yes_best_bid
                   THEN (yes_best_bid + yes_best_ask) / 2.0
                 ELSE mid
               END AS k_mid,
               spread
        FROM read_parquet({listing})
        WHERE ticker IN ({marks})
        ORDER BY ticker, recv_ts_ns
        """,
        [*tickers],
    ).fetch_df()


def load_pm_mids(
    con: duckdb.DuckDBPyConnection,
    tape: Path,
    start_ns: int,
    end_ns: int,
    cids: Sequence[str],
    tokens: Sequence[str],
) -> Any:
    """Mapped tokens -> (cid, token, ts, PM YES mid) inside the window.

    Same reconstruction as ``scripts/report_kalshi_basis.py``: a full ``book``
    snapshot's best bid/ask midpoint, a ``best_bid_ask`` frame's midpoint, or a
    nested ``price_changes`` entry's midpoint.
    """
    files = _window_files(tape / "clob_events", start_ns, end_ns)
    listing = _parquet_list(files)
    cid_marks = ",".join("?" for _ in cids)
    token_marks = ",".join("?" for _ in tokens)
    return con.execute(
        f"""
        WITH src AS (
            SELECT recv_ts_ns, condition_id, asset_id, event_type, bids, asks,
                   best_bid, best_ask, price_changes
            FROM read_parquet({listing})
            WHERE recv_ts_ns >= ? AND recv_ts_ns <= ?
              AND condition_id IN ({cid_marks})
        ),
        pc AS (
            SELECT recv_ts_ns, condition_id, UNNEST(price_changes) AS c
            FROM src WHERE event_type = 'price_change'
        ),
        book AS (
            SELECT recv_ts_ns, condition_id, asset_id,
                   (list_max(list_transform(bids, lambda x: x.price))
                    + list_min(list_transform(asks, lambda x: x.price))) / 2.0 AS mid
            FROM src
            WHERE event_type = 'book' AND len(bids) > 0 AND len(asks) > 0
        ),
        bbo AS (
            SELECT recv_ts_ns, condition_id, asset_id,
                   (best_bid + best_ask) / 2.0 AS mid
            FROM src
            WHERE event_type = 'best_bid_ask'
              AND best_bid IS NOT NULL AND best_ask IS NOT NULL
              AND best_ask >= best_bid
        ),
        nested AS (
            SELECT recv_ts_ns, condition_id, c.asset_id AS asset_id,
                   (c.best_bid + c.best_ask) / 2.0 AS mid
            FROM pc
            WHERE c.asset_id IS NOT NULL AND c.best_bid IS NOT NULL
              AND c.best_ask IS NOT NULL AND c.best_ask >= c.best_bid
        )
        SELECT condition_id AS cid, asset_id AS token, recv_ts_ns AS ts, mid
        FROM (SELECT * FROM book UNION ALL SELECT * FROM bbo UNION ALL SELECT * FROM nested)
        WHERE mid > 0.0 AND mid < 1.0
          AND asset_id IN ({token_marks})
        ORDER BY asset_id, recv_ts_ns
        """,
        [start_ns, end_ns, *cids, *tokens],
    ).fetch_df()


# ---------------------------------------------------------------- panel build
def build_pm_series(frame: Any) -> Dict[str, Tuple[np.ndarray, np.ndarray]]:
    """token -> (ts, mid) sorted, last row wins on a duplicated timestamp."""
    out: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    if frame.empty:
        return out
    dedup = frame.drop_duplicates(subset=["token", "ts"], keep="last")
    for token, g in dedup.groupby("token", sort=False):
        ts = g["ts"].to_numpy(dtype=np.int64)
        mid = g["mid"].to_numpy(dtype=np.float64)
        order = np.argsort(ts, kind="mergesort")
        out[str(token)] = (ts[order], mid[order])
    return out


def build_hour_rows(
    pairs: Sequence[Dict[str, Any]],
    kalshi_frame: Any,
    pm_series: Dict[str, Tuple[np.ndarray, np.ndarray]],
    max_stale_ns: int,
    horizons_ns: Sequence[int],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """One row per aligned (pair, Kalshi snapshot) for one hour partition."""
    counters = {
        "kalshi_rows": int(len(kalshi_frame)),
        "kalshi_usable": 0,
        "snapshots_without_pm": 0,
        "aligned_rows": 0,
    }
    by_ticker = {p["ticker"]: p for p in pairs}
    records: List[Dict[str, Any]] = []
    if kalshi_frame.empty:
        return records, counters
    usable = kalshi_frame[
        kalshi_frame["k_mid"].notna()
        & (kalshi_frame["k_mid"] > 0.0)
        & (kalshi_frame["k_mid"] < 1.0)
    ]
    counters["kalshi_usable"] = int(len(usable))
    for ticker, group in usable.groupby("ticker", sort=False):
        pair = by_ticker.get(ticker)
        if pair is None:
            continue
        series = pm_series.get(pair["yes_token"])
        if series is None or series[0].size == 0:
            counters["snapshots_without_pm"] += int(len(group))
            continue
        pm_ts, pm_mid = series
        snaps = group["recv_ts_ns"].to_numpy(dtype=np.int64)
        k_mid = group["k_mid"].to_numpy(dtype=np.float64)
        spread = group["spread"].to_numpy(dtype=np.float64)
        at = asof_pos(pm_ts, snaps, max_stale_ns)
        pm_at = np.where(at >= 0, pm_mid[np.clip(at, 0, None)], np.nan)
        b = basis(k_mid, pm_at)
        fwd = forward_returns(pm_ts, pm_mid, snaps, horizons_ns, max_stale_ns)
        keep = np.isfinite(b)
        counters["snapshots_without_pm"] += int((~keep).sum())
        for i in np.flatnonzero(keep):
            row: Dict[str, Any] = {
                "kalshi_ticker": pair["ticker"],
                "polymarket_condition_id": pair["cid"],
                "series_ticker": pair["series"],
                "family": pair["family"],
                "snapshot_ts_ns": int(snaps[i]),
                "kalshi_mid": float(k_mid[i]),
                "kalshi_spread": float(spread[i]) if np.isfinite(spread[i]) else None,
                "pm_ts_ns": int(pm_ts[at[i]]),
                "pm_mid": float(pm_at[i]),
                "pm_stale_ms": float(snaps[i] - pm_ts[at[i]]) / 1e6,
                "basis": float(b[i]),
            }
            for h in horizons_ns:
                val = fwd[int(h)][i]
                row[f"ret_{int(h) // SECOND_NS}s"] = (
                    float(val) if np.isfinite(val) else None
                )
            records.append(row)
    counters["aligned_rows"] = len(records)
    return records, counters


PANEL_SCHEMA = pa.schema(
    [
        ("kalshi_ticker", pa.string()),
        ("polymarket_condition_id", pa.string()),
        ("series_ticker", pa.string()),
        ("family", pa.string()),
        ("snapshot_ts_ns", pa.int64()),
        ("kalshi_mid", pa.float64()),
        ("kalshi_spread", pa.float64()),
        ("pm_ts_ns", pa.int64()),
        ("pm_mid", pa.float64()),
        ("pm_stale_ms", pa.float64()),
        ("basis", pa.float64()),
        ("ret_5s", pa.float64()),
        ("ret_15s", pa.float64()),
        ("ret_60s", pa.float64()),
    ]
)


def write_partition(records: Sequence[Dict[str, Any]], out_dir: Path) -> Path:
    """Write one hour partition atomically (tmp file, then rename)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "part-0000.parquet"
    tmp = out_dir / "part-0000.parquet.tmp"
    table = pa.Table.from_pylist(list(records), schema=PANEL_SCHEMA)
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(target)
    return target


def read_panel(root: Path) -> Any:
    """Every derived partition that exists, concatenated."""
    files = sorted(str(p) for p in root.glob("hour=*/part-*.parquet"))
    if not files:
        return None
    con = duckdb.connect()
    return con.execute(
        f"SELECT * FROM read_parquet({_parquet_list(files)}) ORDER BY snapshot_ts_ns"
    ).fetch_df()


# ------------------------------------------------------------------- report
def analyse(panel: Any, horizons: Sequence[int] = HORIZONS_SEC) -> Dict[str, Any]:
    """Per-pair slopes, clustered summary, deciles and the data inventory."""
    pairs = panel["polymarket_condition_id"].to_numpy(dtype=object)
    basis_arr = panel["basis"].to_numpy(dtype=np.float64)
    n_rows = int(len(panel))
    pair_ids = sorted(set(pairs.tolist()))
    out: Dict[str, Any] = {
        "rows": n_rows,
        "pairs_with_data": len(pair_ids),
        "horizons": {},
        "pair_rows": {p: int((pairs == p).sum()) for p in pair_ids},
    }
    out["pairs_at_least_min"] = int(
        sum(1 for v in out["pair_rows"].values() if v >= MIN_ALIGNED)
    )
    for h in horizons:
        col = f"ret_{h}s"
        ret = panel[col].to_numpy(dtype=np.float64)
        per_pair: List[Dict[str, Any]] = []
        for pid in pair_ids:
            sel = pairs == pid
            reg = pair_regression(basis_arr[sel], ret[sel])
            if reg is None or reg["n"] < MIN_SLOPE_ROWS:
                continue
            ticker = str(panel.loc[sel, "kalshi_ticker"].iloc[0])
            family = str(panel.loc[sel, "family"].iloc[0])
            per_pair.append({"cid": pid, "ticker": ticker, "family": family, **reg})
        per_pair.sort(key=lambda r: r["slope"], reverse=True)
        stats = cluster_stats([r["slope"] for r in per_pair])
        strict = cluster_stats(
            [r["slope"] for r in per_pair if r["n"] >= MIN_ALIGNED]
        )
        by_family = {}
        for fam in sorted({r["family"] for r in per_pair}):
            by_family[fam] = cluster_stats(
                [r["slope"] for r in per_pair if r["family"] == fam]
            )
        out["horizons"][h] = {
            "rows_defined": int(np.isfinite(ret).sum()),
            "per_pair": per_pair,
            "stats": stats,
            "stats_strict": strict,
            "by_family": by_family,
            "deciles": decile_table(basis_arr, ret, pairs),
        }
    return out


def format_report(summary: Dict[str, Any], window: Tuple[str, str]) -> str:
    lines = [
        "Kalshi high-frequency basis (E1) - harness smoke test on the live book tape",
        f"snapshot window: {window[0]} .. {window[1]}",
        f"aligned rows: {summary['rows']:,} | pairs with data: {summary['pairs_with_data']} "
        f"| pairs with >= {MIN_ALIGNED} snapshots: {summary['pairs_at_least_min']}",
        "basis = logit(kalshi mid) - logit(pm mid); ret(h) = logit(pm at t+h) - logit(pm at t)",
        "effective sample size = the pair (cluster) count, not the row count",
        "",
    ]
    for h, block in summary["horizons"].items():
        st = block["stats"]
        lines.append(f"--- h = {h} s: {block['rows_defined']:,} rows with a defined return")
        lines.append(
            f"cross-pair mean slope {fmt(st['mean'])}  pairs {st['pairs']}  "
            f"se {fmt(st['se'])}  t {fmt(st['t'], dp=2)}  "
            f"positive {st['positive']}"
        )
        strict = block["stats_strict"]
        if strict["pairs"] and strict["pairs"] != st["pairs"]:
            lines.append(
                f"  with >= {MIN_ALIGNED} aligned snapshots: mean slope "
                f"{fmt(strict['mean'])}  pairs {strict['pairs']}  "
                f"se {fmt(strict['se'])}  t {fmt(strict['t'], dp=2)}"
            )
        if block["per_pair"]:
            lines.append(f"{'family':<15}{'pairs':>7}{'mean_slope':>12}{'se':>10}{'t':>8}")
            for fam, fst in block["by_family"].items():
                lines.append(
                    f"{fam:<15}{fst['pairs']:>7}{fmt(fst['mean'], width=12)}"
                    f"{fmt(fst['se'], width=10)}{fmt(fst['t'], width=8, dp=2)}"
                )
            lines.append(f"{'ticker':<34}{'slope':>10}{'n':>6}{'r':>8}")
            top = block["per_pair"][:5] + block["per_pair"][-5:]
            for r in top:
                lines.append(
                    f"{r['ticker']:<34}{fmt(r['slope']):>10}{r['n']:>6}{fmt(r['r'], dp=3):>8}"
                )
        lines.append(f"{'decile':>7}{'n':>7}{'pairs':>7}{'basis_lo':>10}{'basis_hi':>10}"
                     f"{'mean_ret':>11}")
        for d in block["deciles"]:
            lines.append(
                f"{d['decile']:>7}{d['n']:>7}{d['pairs']:>7}"
                f"{fmt(d['basis_lo'], width=10)}{fmt(d['basis_hi'], width=10)}"
                f"{fmt(d['mean_ret'], width=11)}"
            )
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------- main
def run_hour(
    con: duckdb.DuckDBPyConnection,
    pairs: Sequence[Dict[str, Any]],
    tape: Path,
    partition: Path,
    out_dir: Path,
    force: bool,
) -> Dict[str, int]:
    """Build (or skip) one hour partition; return the stage counters."""
    if not force and any(out_dir.glob("part-*.parquet")):
        log.info("skip {}: {} already written", out_dir.name, out_dir)
        return {"skipped": 1}
    files = sorted(partition.glob("*.parquet"))
    if not files:
        return {"skipped": 1}
    hour_start, hour_end = con.execute(
        f"SELECT MIN(recv_ts_ns), MAX(recv_ts_ns) FROM read_parquet({_parquet_list(files)})"
    ).fetchone()
    hour_start, hour_end = int(hour_start), int(hour_end)
    horizons_ns = tuple(h * SECOND_NS for h in HORIZONS_SEC)
    kalshi_frame = load_kalshi_snapshots(
        con, partition, [p["ticker"] for p in pairs]
    )
    pm_frame = load_pm_mids(
        con,
        tape,
        hour_start - MAX_STALE_SEC * SECOND_NS,
        hour_end + max(horizons_ns),
        [p["cid"] for p in pairs],
        [p["yes_token"] for p in pairs],
    )
    records, counters = build_hour_rows(
        pairs, kalshi_frame, build_pm_series(pm_frame),
        MAX_STALE_SEC * SECOND_NS, horizons_ns,
    )
    counters["pm_quote_rows"] = int(len(pm_frame))
    if records:
        target = write_partition(records, out_dir)
        log.info("wrote {} rows to {}", len(records), target)
    else:
        log.warning("no aligned rows for {}; nothing written", out_dir.name)
    return counters


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    ap.add_argument("--books", type=Path, default=DEFAULT_BOOKS)
    ap.add_argument("--tape", type=Path, default=DEFAULT_TAPE)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--hour", default="", help="only this partition, e.g. 2026110519")
    ap.add_argument("--force", action="store_true", help="rewrite existing partitions")
    args = ap.parse_args(argv)

    now_ns = int(datetime.now(UTC).timestamp() * 1e9)
    con = duckdb.connect()
    pairs = load_mapping(con, args.mapping)
    log.info("mapping: {} pairs from {}", len(pairs), args.mapping)
    parts = hour_partitions(args.books)
    if args.hour:
        parts = [p for p in parts if partition_label(p[0], p[1]) == args.hour]
    if not parts:
        raise SystemExit(f"no kalshi_books partitions under {args.books}")
    for date, hour, part in parts:
        label = partition_label(date, hour)
        if not args.force and not partition_sealed(date, hour, now_ns):
            log.info("hour={} still being written; skipped (use --force to build it)", label)
            continue
        counters = run_hour(
            con, pairs, args.tape, part,
            args.out / f"hour={label}", args.force,
        )
        if counters.get("skipped"):
            log.info("hour={} skipped (output present or no books)", label)
            continue
        log.info(
            "hour={} kalshi_rows={} usable={} pm_quote_rows={} aligned={} "
            "snapshots_without_pm={}",
            label, counters["kalshi_rows"], counters["kalshi_usable"],
            counters.get("pm_quote_rows", 0), counters["aligned_rows"],
            counters["snapshots_without_pm"],
        )
    panel = read_panel(args.out)
    if panel is None:
        print("no aligned rows written; nothing to report")
        return 0
    summary = analyse(panel)
    first = datetime.fromtimestamp(
        int(panel["snapshot_ts_ns"].min()) / 1e9, UTC
    )
    last = datetime.fromtimestamp(
        int(panel["snapshot_ts_ns"].max()) / 1e9, UTC
    )
    window = (
        f"{first:%Y-%m-%dT%H:%M:%SZ}",
        f"{last:%Y-%m-%dT%H:%M:%SZ}",
    )
    text = format_report(summary, window)
    print(text)
    log.info(
        "panel rows={} pairs={}", summary["rows"], summary["pairs_with_data"]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
