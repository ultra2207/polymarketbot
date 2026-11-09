#!/usr/bin/env python3
"""H7 candle-only cross-check: does the v2 mapping point the right way?

For every mapped Kalshi ticker with 1-minute candles and every minute of the
Polymarket tape window, rebuild the PM YES mid from ``clob_events`` (last quote
at or before the minute mark, at most ``MAX_STALE_SEC`` old, else missing) and
compare its logit change with the Kalshi 1-minute close.

Per pair the report measures:

* Pearson r of 1-minute logit changes over aligned consecutive minutes,
* mean/median basis ``logit(kalshi) - logit(pm)`` on aligned levels,
* the count of aligned minutes and changes.

Pairs with fewer than ``MIN_CHANGES`` aligned changes are excluded from the
correlation panel (and counted). A pair whose change correlation is negative is
flagged as suspected inverted/incorrect mapping. The pooled panel z-scores each
pair's changes before pooling so no one market dominates; the market count is
reported beside the pooled r because the pairs are not independent.

H7 decision rule (docs/kalshi-hypotheses.md): |r| >= 0.9 on >= 20 pairs
validates direction; otherwise the failures are named.

Outputs ``data/derived/kalshi_pm_candle_check.parquet`` (one row per mapped
pair) and prints the report.
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

log = get_logger("report_kalshi_candle_check")

DEFAULT_MAPPING = Path("data/derived/kalshi_mapping_v2.parquet")
DEFAULT_TAPE = Path("data/tape")
DEFAULT_CANDLES = Path("data/raw/kalshi/candles")
DEFAULT_OUT = Path("data/derived/kalshi_pm_candle_check.parquet")

MAX_STALE_SEC = 180  # PM quote older than this at a minute mark is missing
MIN_CHANGES = 20  # aligned 1-minute changes needed for a pair correlation
H7_MIN_CORR = 0.9
H7_MIN_PAIRS = 20
LOGIT_EPS = 1e-3  # clamp before logit; documented in the output doc
TAIL_SCAN_HOURS = 2

OUTPUT_SCHEMA = pa.schema(
    [
        ("kalshi_ticker", pa.string()),
        ("kalshi_event", pa.string()),
        ("series_ticker", pa.string()),
        ("family", pa.string()),
        ("polymarket_condition_id", pa.string()),
        ("direction", pa.string()),
        ("confidence", pa.float64()),
        ("method", pa.string()),
        ("market_window", pa.string()),
        ("candle_minutes", pa.int64()),
        ("pm_minutes", pa.int64()),
        ("aligned_minutes", pa.int64()),
        ("n_changes", pa.int64()),
        ("corr_changes", pa.float64()),
        ("basis_mean", pa.float64()),
        ("basis_median", pa.float64()),
        ("basis_med_abs", pa.float64()),
        ("kalshi_mid_mean", pa.float64()),
        ("pm_mid_mean", pa.float64()),
        ("negative_corr", pa.bool_()),
        ("status", pa.string()),
        ("window_start_ns", pa.int64()),
        ("window_end_ns", pa.int64()),
        ("bucket_sec", pa.int32()),
        ("max_stale_sec", pa.int32()),
    ]
)


# --------------------------------------------------------------------- pure
def logit(values: np.ndarray) -> np.ndarray:
    """log(p/(1-p)) with the price clamped to ``LOGIT_EPS .. 1-LOGIT_EPS``."""
    p = np.clip(np.asarray(values, dtype=np.float64), LOGIT_EPS, 1.0 - LOGIT_EPS)
    return np.log(p / (1.0 - p))


def pearson(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    """Pearson r, or ``None`` when undefined (fewer than 3 pairs, zero variance)."""
    a = np.asarray(x, dtype=np.float64).reshape(-1)
    b = np.asarray(y, dtype=np.float64).reshape(-1)
    if a.size != b.size or a.size < 3:
        return None
    da = a - a.mean()
    db = b - b.mean()
    saa = float(np.dot(da, da))
    sbb = float(np.dot(db, db))
    if saa <= 0.0 or sbb <= 0.0:
        return None
    return float(np.dot(da, db) / math.sqrt(saa * sbb))


def zscore(values: np.ndarray) -> Optional[np.ndarray]:
    """Zero-mean unit-variance copy; ``None`` when the series has no variation."""
    x = np.asarray(values, dtype=np.float64).reshape(-1)
    if x.size < 3:
        return None
    sd = float(x.std())
    if not np.isfinite(sd) or sd <= 0.0:
        return None
    return (x - float(x.mean())) / sd


def mark_mids(
    quote_ts: np.ndarray, quote_mid: np.ndarray, marks: np.ndarray, max_stale_ns: int
) -> np.ndarray:
    """Last quote at or before every mark, at most ``max_stale_ns`` old.

    Returns ``nan`` where the tape has no usable quote; the caller keeps
    missing as null rather than carrying a stale price forward.
    """
    out = np.full(marks.size, np.nan, dtype=np.float64)
    if quote_ts.size == 0:
        return out
    order = np.argsort(quote_ts, kind="stable")
    ts = quote_ts[order]
    mid = quote_mid[order]
    pos = np.searchsorted(ts, marks, side="right") - 1
    ok = pos >= 0
    if not ok.any():
        return out
    idx = pos[ok]
    fresh = (marks[ok] - ts[idx]) <= max_stale_ns
    take = np.full(idx.size, np.nan, dtype=np.float64)
    take[fresh] = mid[idx[fresh]]
    out[ok] = take
    return out


def candle_at_marks(
    candle_ts: np.ndarray, candle_mid: np.ndarray, marks: np.ndarray
) -> np.ndarray:
    """Kalshi close exactly at each minute mark; ``nan`` where no bar exists."""
    out = np.full(marks.size, np.nan, dtype=np.float64)
    if candle_ts.size == 0:
        return out
    order = np.argsort(candle_ts, kind="stable")
    ts = candle_ts[order]
    mid = candle_mid[order]
    pos = np.searchsorted(ts, marks, side="left")
    pos_c = np.minimum(pos, ts.size - 1)
    ok = (pos < ts.size) & (ts[pos_c] == marks)
    out[ok] = mid[pos_c[ok]]
    return out


def aligned_stats(
    pm_level: np.ndarray, k_level: np.ndarray
) -> Dict[str, Any]:
    """Correlation of 1-minute logit changes plus basis on aligned minutes."""
    aligned = np.isfinite(pm_level) & np.isfinite(k_level)
    n_aligned = int(aligned.sum())
    n_changes = 0
    corr: Optional[float] = None
    basis_mean: Optional[float] = None
    basis_median: Optional[float] = None
    basis_med_abs: Optional[float] = None
    if n_aligned > 0:
        # logit is defined only on the aligned marks; missing stays missing
        pm_logit = np.full(pm_level.size, np.nan, dtype=np.float64)
        k_logit = np.full(k_level.size, np.nan, dtype=np.float64)
        pm_logit[aligned] = logit(pm_level[aligned])
        k_logit[aligned] = logit(k_level[aligned])
        basis = k_logit[aligned] - pm_logit[aligned]
        basis_mean = float(basis.mean())
        basis_median = float(np.median(basis))
        basis_med_abs = float(np.median(np.abs(basis)))
        # consecutive marks where both series are defined at both endpoints
        both = np.isfinite(pm_logit[:-1]) & np.isfinite(pm_logit[1:]) \
            & np.isfinite(k_logit[:-1]) & np.isfinite(k_logit[1:])
        d_pm = np.diff(pm_logit)[both]
        d_k = np.diff(k_logit)[both]
        n_changes = int(both.sum())
        if n_changes >= 3:
            corr = pearson(d_pm, d_k)
    return {
        "n_aligned": n_aligned,
        "n_changes": n_changes,
        "corr": corr,
        "basis_mean": basis_mean,
        "basis_median": basis_median,
        "basis_med_abs": basis_med_abs,
    }


def family_of(series: Optional[str], method: Optional[str]) -> str:
    """Coarse family label used for the pooled breakdown."""
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


# ------------------------------------------------------------------ tape i/o
def _hour_dirs(tape: Path, table: str) -> List[Tuple[str, str, Path]]:
    root = tape / table
    if not root.is_dir():
        return []
    out: List[Tuple[str, str, Path]] = []
    for d in root.glob("date=*/hour=*"):
        out.append((d.parent.name[len("date=") :], d.name[len("hour=") :], d))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def _window_files(tape: Path, table: str, start_ns: int, end_ns: int) -> List[str]:
    """Parquet files of ``table`` whose date/hour partition intersects the window."""
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
    for date, hour, d in _hour_dirs(tape, table):
        if (date, hour) in want:
            files.extend(str(p) for p in sorted(d.glob("*.parquet")))
    return files


def tape_window(con: duckdb.DuckDBPyConnection, tape: Path) -> Tuple[int, int]:
    """First and last ``clob_events`` receive timestamp (tail scan for the end)."""
    dirs = _hour_dirs(tape, "clob_events")
    if not dirs:
        raise SystemExit("no clob_events partitions; nothing to measure")
    first_files = [str(p) for p in sorted(dirs[0][2].glob("*.parquet"))]
    tail_files = [str(p) for d in dirs[-TAIL_SCAN_HOURS:] for p in sorted(d[2].glob("*.parquet"))]
    listing = "[" + ",".join(f"'{f}'" for f in first_files) + "]"
    start_ns = int(con.execute(f"SELECT MIN(recv_ts_ns) FROM read_parquet({listing})").fetchone()[0])
    listing = "[" + ",".join(f"'{f}'" for f in tail_files) + "]"
    end_ns = int(con.execute(f"SELECT MAX(recv_ts_ns) FROM read_parquet({listing})").fetchone()[0])
    return start_ns, end_ns


def _parquet_list(files: Sequence[str]) -> str:
    if not files:
        raise SystemExit("no parquet files in the window")
    return "[" + ",".join(f"'{f}'" for f in files) + "]"


# ------------------------------------------------------------------ loaders
def load_mapping(con: duckdb.DuckDBPyConnection, path: Path) -> List[Dict[str, Any]]:
    frame = con.execute(
        f"""
        SELECT kalshi_ticker, kalshi_event, series_ticker, polymarket_condition_id,
               polymarket_yes_token, direction, confidence, method, "window"
        FROM read_parquet('{path}')
        ORDER BY kalshi_ticker
        """
    ).fetch_df()
    rows: List[Dict[str, Any]] = []
    for r in frame.itertuples(index=False):
        rows.append(
            {
                "ticker": str(r.kalshi_ticker),
                "event": str(r.kalshi_event) if r.kalshi_event is not None else None,
                "series": str(r.series_ticker) if r.series_ticker is not None else None,
                "cid": str(r.polymarket_condition_id),
                "yes_token": str(r.polymarket_yes_token),
                "direction": str(r.direction) if r.direction is not None else None,
                "confidence": float(r.confidence) if r.confidence is not None else None,
                "method": str(r.method) if r.method is not None else None,
                "market_window": str(getattr(r, "window")) if getattr(r, "window") is not None else None,
            }
        )
    return rows


def candle_inventory(candles: Path) -> Tuple[List[str], Dict[str, Dict[str, int]]]:
    """File list plus ticker x month matrix from the on-disk partition names."""
    files = sorted(candles.glob("month=*/ticker=*.parquet"))
    matrix: Dict[str, Dict[str, int]] = {}
    for f in files:
        month = f.parent.name[len("month=") :]
        ticker = f.stem[len("ticker=") :]
        matrix.setdefault(ticker, {})[month] = int(f.stat().st_size)
    return [str(p) for p in files], matrix


def load_candles(
    con: duckdb.DuckDBPyConnection,
    files: Sequence[str],
    start_ns: int,
    end_ns: int,
    tickers: Sequence[str],
) -> Dict[str, Tuple[np.ndarray, np.ndarray]]:
    """ticker -> (end-of-minute ns, YES mid) inside the window.

    ``close`` is often null when a market did not trade, so the mark is the
    bid/ask midpoint of the bar (``yes_bid_close`` + ``yes_ask_close``) / 2,
    falling back to ``close`` when the quote is absent.
    """
    listing = _parquet_list(files)
    marks = ",".join("?" for _ in tickers)
    frame = con.execute(
        f"""
        SELECT ticker,
               COALESCE(end_period_ts, ts_seconds) AS ts_sec,
               CASE
                 WHEN yes_bid_close IS NOT NULL AND yes_ask_close IS NOT NULL
                      AND yes_ask_close >= yes_bid_close
                   THEN (yes_bid_close + yes_ask_close) / 2.0
                 ELSE close
               END AS mid
        FROM read_parquet({listing}, union_by_name=true)
        WHERE ticker IN ({marks})
          AND COALESCE(end_period_ts, ts_seconds) * 1000000000 BETWEEN ? AND ?
        """,
        [*tickers, start_ns, end_ns],
    ).fetch_df()
    out: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    if frame.empty:
        return out
    frame = frame[frame["mid"].notna() & (frame["mid"] > 0.0) & (frame["mid"] < 1.0)]
    for ticker, g in frame.groupby("ticker", sort=False):
        g = g.sort_values("ts_sec")
        out[str(ticker)] = (
            (g["ts_sec"].to_numpy(dtype=np.int64) * 1_000_000_000).astype(np.int64),
            g["mid"].to_numpy(dtype=np.float64),
        )
    return out


def load_pm_quotes(
    con: duckdb.DuckDBPyConnection,
    tape: Path,
    start_ns: int,
    end_ns: int,
    tokens: Sequence[str],
    cids: Sequence[str],
) -> Dict[str, Tuple[np.ndarray, np.ndarray]]:
    """token -> (recv ns, YES mid) rebuilt exactly as ``report_kalshi_basis`` does."""
    files = _window_files(tape, "clob_events", start_ns, end_ns)
    listing = _parquet_list(files)
    cid_marks = ",".join("?" for _ in cids)
    token_marks = ",".join("?" for _ in tokens)
    frame = con.execute(
        f"""
        WITH src AS (
            SELECT recv_ts_ns, condition_id, asset_id, event_type, bids, asks,
                   best_bid, best_ask, price_changes
            FROM read_parquet({listing})
            WHERE recv_ts_ns >= ? AND recv_ts_ns <= ?
              AND condition_id IN ({cid_marks})
        ),
        pc AS (
            SELECT recv_ts_ns, UNNEST(price_changes) AS c
            FROM src WHERE event_type = 'price_change'
        ),
        book AS (
            SELECT recv_ts_ns, asset_id,
                   (list_max(list_transform(bids, lambda x: x.price))
                    + list_min(list_transform(asks, lambda x: x.price))) / 2.0 AS mid
            FROM src
            WHERE event_type = 'book' AND len(bids) > 0 AND len(asks) > 0
        ),
        bbo AS (
            SELECT recv_ts_ns, asset_id, (best_bid + best_ask) / 2.0 AS mid
            FROM src
            WHERE event_type = 'best_bid_ask'
              AND best_bid IS NOT NULL AND best_ask IS NOT NULL
              AND best_ask >= best_bid
        ),
        nested AS (
            SELECT recv_ts_ns, c.asset_id AS asset_id,
                   (c.best_bid + c.best_ask) / 2.0 AS mid
            FROM pc
            WHERE c.asset_id IS NOT NULL AND c.best_bid IS NOT NULL
              AND c.best_ask IS NOT NULL AND c.best_ask >= c.best_bid
        )
        SELECT asset_id, recv_ts_ns, mid
        FROM (SELECT * FROM book UNION ALL SELECT * FROM bbo UNION ALL SELECT * FROM nested)
        WHERE mid > 0.0 AND mid < 1.0
          AND asset_id IN ({token_marks})
        ORDER BY asset_id, recv_ts_ns
        """,
        [start_ns, end_ns, *cids, *tokens],
    ).fetch_df()
    out: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    if frame.empty:
        return out
    for token, g in frame.groupby("asset_id", sort=False):
        out[str(token)] = (
            g["recv_ts_ns"].to_numpy(dtype=np.int64),
            g["mid"].to_numpy(dtype=np.float64),
        )
    return out


# ------------------------------------------------------------------- report
def _fmt(value: Optional[float], dp: int = 4) -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "n/a"
    return f"{float(value):.{dp}f}"


def build(
    mapping_path: Path,
    tape: Path,
    candles: Path,
    *,
    month: Optional[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    con = duckdb.connect()
    pairs = load_mapping(con, mapping_path)
    all_files, inventory = candle_inventory(candles)
    if month:
        file_list = [f for f in all_files if f"{Path(f).parent.name}" == f"month={month}"]
    else:
        file_list = all_files
    tickers = sorted({p["ticker"] for p in pairs})
    pair_tickers = set(tickers)

    start_ns, end_ns = tape_window(con, tape)
    first_mark = ((start_ns // 60_000_000_000) + 1) * 60_000_000_000
    last_mark = (end_ns // 60_000_000_000) * 60_000_000_000
    marks = np.arange(first_mark, last_mark + 1, 60_000_000_000, dtype=np.int64)

    candle_quotes = load_candles(con, file_list, start_ns, end_ns, tickers)
    tokens = sorted({p["yes_token"] for p in pairs})
    cids = sorted({p["cid"] for p in pairs})
    pm_quotes = load_pm_quotes(con, tape, start_ns, end_ns, tokens, cids)

    max_stale_ns = MAX_STALE_SEC * 1_000_000_000
    records: List[Dict[str, Any]] = []
    pooled_chunks: Dict[str, List[Tuple[np.ndarray, np.ndarray]]] = {}
    for p in pairs:
        cts, cmid = candle_quotes.get(p["ticker"], (np.empty(0, np.int64), np.empty(0, np.float64)))
        pts, pmid = pm_quotes.get(p["yes_token"], (np.empty(0, np.int64), np.empty(0, np.float64)))
        k_level = candle_at_marks(cts, cmid, marks)
        p_level = mark_mids(pts, pmid, marks, max_stale_ns)
        stats = aligned_stats(p_level, k_level)
        n_changes = stats["n_changes"]
        corr = stats["corr"]
        status = "ok" if n_changes >= MIN_CHANGES else "insufficient"
        if cts.size == 0:
            status = "no_candles"
        elif pts.size == 0:
            status = "no_pm"
        fam = family_of(p["series"], p["method"])
        k_mean = float(np.nanmean(k_level)) if np.isfinite(k_level).any() else None
        p_mean = float(np.nanmean(p_level)) if np.isfinite(p_level).any() else None
        records.append(
            {
                "kalshi_ticker": p["ticker"],
                "kalshi_event": p["event"],
                "series_ticker": p["series"],
                "family": fam,
                "polymarket_condition_id": p["cid"],
                "direction": p["direction"],
                "confidence": p["confidence"],
                "method": p["method"],
                "market_window": p["market_window"],
                "candle_minutes": int(cts.size),
                "pm_minutes": int(pts.size),
                "aligned_minutes": int(stats["n_aligned"]),
                "n_changes": int(n_changes),
                "corr_changes": corr,
                "basis_mean": stats["basis_mean"],
                "basis_median": stats["basis_median"],
                "basis_med_abs": stats["basis_med_abs"],
                "kalshi_mid_mean": k_mean,
                "pm_mid_mean": p_mean,
                "negative_corr": (corr < 0.0) if corr is not None and n_changes >= MIN_CHANGES else None,
                "status": status,
                "window_start_ns": int(start_ns),
                "window_end_ns": int(end_ns),
                "bucket_sec": 60,
                "max_stale_sec": MAX_STALE_SEC,
            }
        )
        # pooled panel: z-score this pair's logit changes before pooling
        l_p = np.full(p_level.size, np.nan, dtype=np.float64)
        l_k = np.full(k_level.size, np.nan, dtype=np.float64)
        ok = np.isfinite(p_level) & np.isfinite(k_level)
        l_p[ok] = logit(p_level[ok])
        l_k[ok] = logit(k_level[ok])
        if n_changes >= MIN_CHANGES:
            both = np.isfinite(l_p[:-1]) & np.isfinite(l_p[1:]) \
                & np.isfinite(l_k[:-1]) & np.isfinite(l_k[1:])
            zx, zy = zscore(np.diff(l_p)[both]), zscore(np.diff(l_k)[both])
            if zx is not None and zy is not None:
                pooled_chunks.setdefault(fam, []).append((zx, zy))

    # pooled r over the evaluated pairs, and again per family
    pooled: Dict[str, Any] = {}
    for fam in ("all", "crypto_ladder", "treasury", "fomc", "elections", "other"):
        chunks = (
            [c for v in pooled_chunks.values() for c in v]
            if fam == "all"
            else pooled_chunks.get(fam, [])
        )
        if not chunks:
            pooled[fam] = {"r": None, "n": 0, "markets": 0}
            continue
        x = np.concatenate([c[0] for c in chunks])
        y = np.concatenate([c[1] for c in chunks])
        pooled[fam] = {
            "r": pearson(x, y),
            "n": int(x.size),
            "markets": len(chunks),
        }
    summary = {
        "window_start_ns": int(start_ns),
        "window_end_ns": int(end_ns),
        "marks": int(marks.size),
        "minute_marks": int(marks.size),
        "mapped_pairs": len(pairs),
        "pairs_with_candles": sum(1 for r in records if r["candle_minutes"] > 0),
        "pairs_with_pm": sum(1 for r in records if r["pm_minutes"] > 0),
        "pairs_evaluated": sum(1 for r in records if r["n_changes"] >= MIN_CHANGES),
        "pairs_excluded": sum(1 for r in records if r["n_changes"] < MIN_CHANGES),
        "pairs_defined_corr": sum(
            1 for r in records if r["n_changes"] >= MIN_CHANGES and r["corr_changes"] is not None
        ),
        "pairs_zero_variance": sum(
            1 for r in records if r["n_changes"] >= MIN_CHANGES and r["corr_changes"] is None
        ),
        "negative_pairs": [
            r for r in records if r["negative_corr"] is True
        ],
        "inventory": {
            t: sorted(m) for t, m in inventory.items() if t in pair_tickers
        },
        "inventory_all_tickers": len(inventory),
        "inventory_months": sorted({m for v in inventory.values() for m in v}),
        "files_used": len(file_list),
        "pooled": pooled,
    }
    return records, summary


def h7_verdict(records: Sequence[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """|r| >= 0.9 on >= 20 pairs validates direction; list the failures otherwise."""
    evaluated = [r for r in records if r["n_changes"] >= MIN_CHANGES]
    defined = [r for r in evaluated if r["corr_changes"] is not None]
    strong = [r for r in defined if abs(float(r["corr_changes"])) >= H7_MIN_CORR]
    tail = (
        f"{len(strong)} of {len(evaluated)} evaluated pairs "
        f"({len(defined)} with a defined r) have |r| >= {H7_MIN_CORR}"
    )
    if len(strong) >= H7_MIN_PAIRS:
        return f"PASS: {tail} (rule needs >= {H7_MIN_PAIRS})", strong
    return f"FAIL: {tail} (rule needs >= {H7_MIN_PAIRS}); see weak/negative pairs", strong


def write_parquet(records: Sequence[Dict[str, Any]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(list(records), schema=OUTPUT_SCHEMA)
    pq.write_table(table, out, compression="zstd")


def format_report(records: Sequence[Dict[str, Any]], summary: Dict[str, Any], verdict: str) -> str:
    start = datetime.fromtimestamp(summary["window_start_ns"] / 1e9, UTC)
    end = datetime.fromtimestamp(summary["window_end_ns"] / 1e9, UTC)
    by_month: Dict[str, int] = {}
    for months in summary["inventory"].values():
        for month in months:
            by_month[month] = by_month.get(month, 0) + 1
    lines = [
        "Kalshi candle cross-check (H7)",
        f"tape window: {start:%Y-%m-%dT%H:%M:%SZ} .. {end:%Y-%m-%dT%H:%M:%SZ} "
        f"({summary['minute_marks']:,} minute marks)",
        f"candle files read: {summary['files_used']}; mapped tickers on disk: "
        f"{len(summary['inventory'])}; months: {', '.join(summary['inventory_months']) or 'none'}",
        "mapped tickers per month: "
        + (", ".join(f"{m} {by_month[m]}" for m in sorted(by_month)) or "none"),
        f"mapped pairs: {summary['mapped_pairs']} | with candles: {summary['pairs_with_candles']} "
        f"| with PM quotes: {summary['pairs_with_pm']} | evaluated (>= {MIN_CHANGES} changes): "
        f"{summary['pairs_evaluated']} | excluded: {summary['pairs_excluded']} "
        f"| r defined: {summary['pairs_defined_corr']} "
        f"| zero-variance changes: {summary['pairs_zero_variance']}",
        "",
        f"{'family':<15}{'pairs':>7}{'r':>10}{'n_changes':>12}{'markets':>9}",
        "-" * 54,
    ]
    for fam in ("all", "crypto_ladder", "treasury", "fomc", "elections", "other"):
        v = summary["pooled"][fam]
        n_pairs = sum(1 for r in records if r["family"] == fam and r["n_changes"] >= MIN_CHANGES)
        if fam == "all":
            n_pairs = summary["pairs_evaluated"]
        lines.append(
            f"{fam:<15}{n_pairs:>7}{_fmt(v['r']):>10}{v['n']:>12,}{v['markets']:>9}"
        )
    lines += ["", "negative-correlation pairs (suspected inverted mapping):"]
    neg = summary["negative_pairs"]
    if not neg:
        lines.append("  none")
    else:
        lines.append(f"  {'ticker':<38}{'r':>8}{'n':>6}  {'method':<18}{'conf':>6}  family")
        for r in neg:
            lines.append(
                f"  {r['kalshi_ticker']:<38}{float(r['corr_changes']):>8.3f}{r['n_changes']:>6}  "
                f"{(r['method'] or ''):<18}{(r['confidence'] if r['confidence'] is not None else float('nan')):>6.2f}  "
                f"{r['family']}"
            )
    lines += ["", f"H7: {verdict}"]
    weak = [r for r in records if r["n_changes"] >= MIN_CHANGES and r["corr_changes"] is not None
            and abs(float(r["corr_changes"])) < H7_MIN_CORR]
    if weak:
        lines.append(f"pairs below |r| {H7_MIN_CORR} (corr, n, family):")
        for r in sorted(weak, key=lambda x: abs(float(x["corr_changes"])))[:40]:
            lines.append(
                f"  {r['kalshi_ticker']:<38}{float(r['corr_changes']):>8.3f}{r['n_changes']:>6}  {r['family']}"
            )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    ap.add_argument("--tape", type=Path, default=DEFAULT_TAPE)
    ap.add_argument("--candles", type=Path, default=DEFAULT_CANDLES)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--month", default="", help="restrict candles to month=YYYY-MM ('' = all)")
    args = ap.parse_args(argv)

    records, summary = build(
        args.mapping, args.tape, args.candles, month=args.month or None
    )
    verdict, _strong = h7_verdict(records)
    write_parquet(records, args.out)
    text = format_report(records, summary, verdict)
    log.info(text)
    log.info(
        f"wrote {len(records)} pair rows to {args.out} "
        f"(evaluated={summary['pairs_evaluated']}, excluded={summary['pairs_excluded']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
