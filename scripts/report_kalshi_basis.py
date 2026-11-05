#!/usr/bin/env python3
"""Cross-venue basis and lead-lag between Kalshi and mapped Polymarket markets.

Usage
    .venv/Scripts/python.exe scripts/report_kalshi_basis.py
    .venv/Scripts/python.exe scripts/report_kalshi_basis.py \\
        --window-min 120 --bucket-sec 60 --max-lag 10 --top 20

What it measures
----------------
For every pair in ``data/derived/kalshi_mapping.parquet`` that is quoted on both
venues inside the window (default: the last 120 minutes of the tape):

* ``poly_mid``   - Polymarket mid of the market's YES token
  (``clob_tokens[0]`` in the universe manifest) rebuilt from the tape's quote
  events: ``book`` snapshots (best bid = highest bid, best ask = lowest ask) and
  ``best_bid_ask`` frames, with ``price_change`` nested frames as a fallback.
  The value per bucket is the last quote in that bucket, carried forward at most
  ``--max-stale-sec`` (default 180 s); past that the bucket is unknown.
* ``kalshi_mid`` - Kalshi ``(yes_bid + yes_ask) / 2`` from the ``kalshi_markets``
  poll stream; the value per bucket is the last poll in that bucket. Polls with
  an empty book (``yes_bid = 0`` and ``yes_ask = 1``) are not quotes and are
  dropped, so they never become a fabricated 50c mid.
* ``basis``     - ``poly_mid - kalshi_mid`` per bucket where *both* venues
  have a fresh mid, so a stale quote on one side cannot manufacture a basis.
* ``spread``    - ``ask - bid`` on each venue, over that venue's raw quotes.
* ``trades``    - ``clob_trades`` rows/shares and ``kalshi_trades`` trades/contracts.
* ``lead-lag``  - Pearson correlation of one-bucket changes: Polymarket's change
  over bucket ``b`` against Kalshi's change over bucket ``b + lag`` for
  ``lag`` in ``-max_lag .. +max_lag``. **Negative lag means Kalshi moves first
  (Polymarket leads only at positive lag).** Per-market changes are z-scored
  before pooling, so a volatile market cannot dominate the panel; the count of
  contributing markets is reported beside every lag.

A pair with fewer than ``MIN_BASIS_BUCKETS`` aligned buckets gets ``n/a`` for
basis, and a market with fewer than ``MIN_LAG_PAIRS`` usable changes is excluded
from the pooled lag panel. A pair whose other outcome token sits closer to
Kalshi's YES mid is reported but flagged (``direction_ok = false``) and kept out
of the panel: the sign of its changes is not trustworthy. ``n/a`` is printed; a
missing number is never a zero.

Outputs
    data/derived/kalshi_basis.parquet       one row per mapped market
    data/derived/kalshi_basis_lags.parquet  one row per lag (pooled panel)
    data/derived/kalshi_basis_summary.txt   the printed summary
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import duckdb  # noqa: E402
import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from polymarketbot.logs import get_logger  # noqa: E402

log = get_logger("report_kalshi_basis")

DEFAULT_TAPE = Path("data/tape")
DEFAULT_MAPPING = Path("data/derived/kalshi_mapping.parquet")
DEFAULT_MANIFEST = Path("data/tape/manifest_current.json")
DEFAULT_SNAPSHOT = Path("data/raw/gamma/snapshot_2026-11-01.parquet")
DEFAULT_OUT = Path("data/derived/kalshi_basis.parquet")
DEFAULT_LAGS_OUT = Path("data/derived/kalshi_basis_lags.parquet")
DEFAULT_SUMMARY_OUT = Path("data/derived/kalshi_basis_summary.txt")

DEFAULT_WINDOW_MIN = 120
DEFAULT_BUCKET_SEC = 60
DEFAULT_MAX_STALE_SEC = 180
DEFAULT_MAX_LAG = 10
MIN_BASIS_BUCKETS = 5  # aligned buckets (both venues fresh) below this: basis is n/a
MIN_LAG_PAIRS = 10  # one-bucket changes a market needs to enter the lag panel
MIN_LAG_PANEL = 30  # pooled pairs below this: lag correlation is n/a
STALE_SCAN_HOURS = 3  # hour partitions read to locate the tape's clock


# --------------------------------------------------------------------- pure
def bucket_last(
    ts_ns: Sequence[int], values: Sequence[float], bucket_ns: int
) -> Tuple[np.ndarray, np.ndarray]:
    """Last finite observation per bucket.

    ``bucket_ns`` is the bucket width in nanoseconds. Input need not be sorted.
    Returns ``(bucket_ids, values)`` sorted by bucket, one entry per bucket that
    had at least one finite value.
    """
    ts = np.asarray(ts_ns, dtype=np.int64).reshape(-1)
    v = np.asarray(values, dtype=np.float64).reshape(-1)
    if ts.size != v.size:
        raise ValueError("ts_ns and values must have the same length")
    keep = np.isfinite(v)
    ts, v = ts[keep], v[keep]
    if ts.size == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
    width = int(bucket_ns)
    if width <= 0:
        raise ValueError("bucket_ns must be positive")
    keys = ts // width
    order = np.lexsort((ts, keys))
    keys, v = keys[order], v[order]
    last = np.ones(keys.size, dtype=bool)
    last[:-1] = keys[1:] != keys[:-1]
    return keys[last], v[last]


def fill_grid(
    bucket_ids: Sequence[int],
    values: Sequence[float],
    grid_start: int,
    grid_end: int,
    max_stale_buckets: int,
) -> np.ndarray:
    """Forward-fill a quote series onto every bucket of a regular grid.

    A quote is the venue's mid until the next one arrives, so buckets between
    quotes carry the last value. Past ``max_stale_buckets`` buckets without a
    fresh quote the mid is unknown and the grid cell stays ``NaN`` - a stale
    quote is dropped, never carried to the end of the window.
    """
    b = np.asarray(bucket_ids, dtype=np.int64).reshape(-1)
    v = np.asarray(values, dtype=np.float64).reshape(-1)
    if b.size != v.size:
        raise ValueError("bucket_ids and values must have the same length")
    lo, hi = int(grid_start), int(grid_end)
    if hi < lo:
        return np.empty(0, dtype=np.float64)
    n = hi - lo + 1
    out = np.full(n, np.nan, dtype=np.float64)
    keep = (b >= lo) & (b <= hi) & np.isfinite(v)
    if not keep.any():
        return out
    out[b[keep] - lo] = v[keep]
    seen = np.isfinite(out)
    positions = np.where(seen, np.arange(n), -1)
    last = np.maximum.accumulate(positions)
    filled = np.where(last >= 0, out[np.maximum(last, 0)], np.nan)
    age = np.arange(n) - np.maximum(last, 0)
    filled[(last < 0) | (age > int(max_stale_buckets))] = np.nan
    return filled


def aligned_changes(
    poly_grid: Sequence[float], kalshi_grid: Sequence[float], grid_start: int
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One-bucket changes of two grid series, both defined at both endpoints.

    Returns ``(end_bucket_ids, poly_changes, kalshi_changes)``. Pairs where
    either venue is stale on either side of the bucket are dropped: a change
    spanning unknown territory is not a one-bucket change.
    """
    a = np.asarray(poly_grid, dtype=np.float64).reshape(-1)
    b = np.asarray(kalshi_grid, dtype=np.float64).reshape(-1)
    if a.size != b.size:
        raise ValueError("grid series must have the same length")
    if a.size < 2:
        empty = np.empty(0, dtype=np.float64)
        return np.empty(0, dtype=np.int64), empty, empty
    both = np.isfinite(a) & np.isfinite(b)
    ok = both[1:] & both[:-1]
    ids = np.arange(int(grid_start) + 1, int(grid_start) + a.size, dtype=np.int64)[ok]
    return ids, (a[1:] - a[:-1])[ok], (b[1:] - b[:-1])[ok]


def pair_at_lag(
    bucket_ids: Sequence[int],
    changes: Sequence[float],
    other_ids: Sequence[int],
    other_changes: Sequence[float],
    lag: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Pair each change at bucket ``b`` with the other series' change at ``b+lag``.

    ``lag > 0`` pairs a first-venue change with a *later* second-venue change
    (second venue follows); ``lag < 0`` pairs with an earlier one (second venue
    leads). Unmatched buckets are dropped.
    """
    b = np.asarray(bucket_ids, dtype=np.int64).reshape(-1)
    x = np.asarray(changes, dtype=np.float64).reshape(-1)
    ob = np.asarray(other_ids, dtype=np.int64).reshape(-1)
    y = np.asarray(other_changes, dtype=np.float64).reshape(-1)
    if b.size != x.size or ob.size != y.size:
        raise ValueError("bucket/changes length mismatch")
    if b.size == 0 or ob.size == 0:
        return np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64)
    target = b + int(lag)
    pos = np.searchsorted(ob, target)
    pos_c = np.minimum(pos, ob.size - 1)
    ok = (pos < ob.size) & (ob[pos_c] == target)
    return x[ok], y[pos_c[ok]]


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


def zscore(values: Sequence[float]) -> Optional[np.ndarray]:
    """Zero-mean unit-variance copy; ``None`` when the series has no variation."""
    x = np.asarray(values, dtype=np.float64).reshape(-1)
    if x.size < 3:
        return None
    sd = float(x.std())
    if not np.isfinite(sd) or sd <= 0.0:
        return None
    return (x - float(x.mean())) / sd


def lag_table(
    poly_ids: Sequence[int],
    poly_changes: Sequence[float],
    kalshi_ids: Sequence[int],
    kalshi_changes: Sequence[float],
    max_lag: int,
    min_pairs: int = MIN_LAG_PAIRS,
) -> List[Dict[str, Any]]:
    """Per-lag correlation of changes for **one** market.

    ``corr`` is ``None`` (printed ``n/a``) when fewer than ``min_pairs`` pairs
    survive at that lag.
    """
    rows: List[Dict[str, Any]] = []
    for lag in range(-int(max_lag), int(max_lag) + 1):
        px, kx = pair_at_lag(poly_ids, poly_changes, kalshi_ids, kalshi_changes, lag)
        r = pearson(px, kx) if px.size >= int(min_pairs) else None
        rows.append({"lag": lag, "n": int(px.size), "corr": r})
    return rows


def pooled_lag_table(
    series: Iterable[Dict[str, Any]],
    max_lag: int,
    min_pairs: int = MIN_LAG_PAIRS,
    min_panel: int = MIN_LAG_PANEL,
) -> List[Dict[str, Any]]:
    """Panel lag correlations: per-market z-scored changes, pooled per lag.

    ``series`` items carry ``poly_ids``, ``poly_changes``, ``kalshi_ids`` and
    ``kalshi_changes`` for one market. Markets contributing fewer than
    ``min_pairs`` pairs at a lag, or with no variation, drop out of that lag.
    """
    items = list(series)
    rows: List[Dict[str, Any]] = []
    for lag in range(-int(max_lag), int(max_lag) + 1):
        chunks_x: List[np.ndarray] = []
        chunks_y: List[np.ndarray] = []
        markets = 0
        for s in items:
            px, kx = pair_at_lag(
                s["poly_ids"], s["poly_changes"], s["kalshi_ids"], s["kalshi_changes"], lag
            )
            if px.size < int(min_pairs):
                continue
            zx, zy = zscore(px), zscore(kx)
            if zx is None or zy is None:
                continue
            chunks_x.append(zx)
            chunks_y.append(zy)
            markets += 1
        n = int(sum(c.size for c in chunks_x))
        r = None
        if n >= int(min_panel) and markets >= 2:
            r = pearson(np.concatenate(chunks_x), np.concatenate(chunks_y))
        rows.append({"lag": lag, "n_pairs": n, "n_markets": markets, "corr": r})
    return rows


def peak_lag(rows: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The lag with the largest ``|corr|``, or ``None`` when every lag is n/a."""
    valid = [r for r in rows if r.get("corr") is not None]
    if not valid:
        return None
    return max(valid, key=lambda r: abs(float(r["corr"])))


def basis_summary(
    poly_levels: Sequence[float],
    kalshi_levels: Sequence[float],
    min_buckets: int = MIN_BASIS_BUCKETS,
) -> Dict[str, Optional[float]]:
    """Mean/median-absolute basis on bucket-aligned levels.

    Fewer than ``min_buckets`` aligned buckets returns ``n`` plus ``None`` for
    every statistic - an insufficient sample is ``n/a``, not zero.
    """
    a = np.asarray(poly_levels, dtype=np.float64).reshape(-1)
    b = np.asarray(kalshi_levels, dtype=np.float64).reshape(-1)
    if a.size != b.size:
        raise ValueError("poly_levels and kalshi_levels must have the same length")
    both = np.isfinite(a) & np.isfinite(b)
    a, b = a[both], b[both]
    n = int(a.size)
    blank: Dict[str, Optional[float]] = {
        "n": n,
        "poly_mid_mean": None,
        "kalshi_mid_mean": None,
        "basis_mean": None,
        "basis_med_abs": None,
        "basis_std": None,
    }
    if n < int(min_buckets):
        return blank
    basis = a - b
    return {
        "n": n,
        "poly_mid_mean": float(a.mean()),
        "kalshi_mid_mean": float(b.mean()),
        "basis_mean": float(basis.mean()),
        "basis_med_abs": float(np.median(np.abs(basis))),
        "basis_std": float(basis.std()),
    }


def orientation_ok(
    mid_yes: Optional[float], mid_alt: Optional[float], kalshi_mid: Optional[float]
) -> Optional[bool]:
    """Does the manifest's YES token point the same way as Kalshi's YES?

    The manifest lists outcome tokens in ``outcomes`` order, so ``clob_tokens[0]``
    is the market's YES token. This check is evidence against that assumption:
    when the *other* token sits closer to Kalshi's YES mid, the pair is flagged
    (direction or market semantics differ) instead of being silently averaged
    into the panel. ``None`` when there is no second token to compare.
    """
    if mid_yes is None or mid_alt is None or kalshi_mid is None:
        return None
    return abs(float(mid_yes) - float(kalshi_mid)) <= abs(float(mid_alt) - float(kalshi_mid))


def fmt_opt(value: Optional[float], width: int = 9, dp: int = 4) -> str:
    """Right-aligned number or ``n/a``."""
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "n/a".rjust(width)
    return f"{float(value):.{dp}f}".rjust(width)


def format_lag_rows(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """Printable lag table with an explicit count beside every correlation."""
    out = [f"{'lag':>4} {'n_pairs':>9} {'markets':>8} {'corr':>9}", "-" * 32]
    for r in rows:
        n = r.get("n_pairs", r.get("n", 0))
        markets = r.get("n_markets")
        out.append(
            f"{int(r['lag']):>+4} {int(n):>9,} "
            f"{(f'{int(markets):,}' if markets is not None else 'n/a'):>8} "
            f"{fmt_opt(r.get('corr'), 9)}"
        )
    return out


# ------------------------------------------------------------------ tape i/o
def _hour_dirs(tape: Path, table: str) -> List[Tuple[str, str, Path]]:
    root = tape / table
    if not root.is_dir():
        return []
    out: List[Tuple[str, str, Path]] = []
    for d in root.glob("date=*/hour=*"):
        out.append((d.parent.name[len("date="):], d.name[len("hour="):], d))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def _files(tape: Path, table: str, start_ns: int, end_ns: int) -> List[str]:
    """Parquet files of ``table`` whose date/hour partition intersects the window."""
    start = datetime.fromtimestamp(start_ns / 1e9, timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(end_ns / 1e9, timezone.utc)
    want = set()
    t = start
    while t <= end:
        want.add((t.strftime("%Y-%m-%d"), t.strftime("%H")))
        t += timedelta(hours=1)
    files: List[str] = []
    for date, hour, d in _hour_dirs(tape, table):
        if (date, hour) in want:
            files.extend(sorted(str(p) for p in d.glob("*.parquet")))
    return files


def _tape_end_ns(con: duckdb.DuckDBPyConnection, tape: Path) -> int:
    """Newest receive timestamp across the tapes that carry the two venues.

    Only the last few hour partitions are read: the clock comes from the tail of
    the tape, and scanning every partition for a max is the expensive way to
    learn it.
    """
    latest: Optional[int] = None
    for table in ("kalshi_markets", "clob_events"):
        dirs = [d for _, _, d in _hour_dirs(tape, table)][-STALE_SCAN_HOURS:]
        if not dirs:
            continue
        files = [str(p) for d in dirs for p in sorted(d.glob("*.parquet"))]
        if not files:
            continue
        listing = "[" + ",".join(f"'{f}'" for f in files) + "]"
        row = con.execute(f"SELECT MAX(recv_ts_ns) FROM read_parquet({listing})").fetchone()
        if row and row[0] is not None:
            latest = int(row[0]) if latest is None else max(latest, int(row[0]))
    if latest is None:
        raise SystemExit("no tape rows found; nothing to measure")
    return latest


def _parquet_list(files: Sequence[str]) -> str:
    if not files:
        raise SystemExit("no parquet files in the window")
    return "[" + ",".join(f"'{f}'" for f in files) + "]"


def _load_tokens(con: duckdb.DuckDBPyConnection, mapping: Path, manifest: Path, snapshot: Path) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """condition_id -> (yes_token, other_token) from the manifest/snapshot."""
    cids = [str(r[0]) for r in con.execute(
        f"SELECT polymarket_condition_id FROM read_parquet('{mapping}')"
    ).fetchall()]
    tokens: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    try:
        payload = json.loads(Path(manifest).read_text(encoding="utf-8"))
        for m in payload.get("markets", []):
            cid = m.get("condition_id")
            toks = m.get("clob_tokens") or []
            if cid and toks:
                tokens[str(cid)] = (str(toks[0]), str(toks[1]) if len(toks) > 1 else None)
    except (OSError, ValueError) as exc:  # manifest is a cache, not a contract
        log.warning("manifest unreadable ({}); falling back to the gamma snapshot", exc)

    missing = [c for c in cids if c not in tokens]
    if missing and Path(snapshot).exists():
        marks = ",".join("?" for _ in missing)
        rows = con.execute(
            f"SELECT conditionId, clobTokenIds FROM read_parquet('{snapshot}') "
            f"WHERE conditionId IN ({marks})",
            missing,
        ).fetchall()
        for cid, raw in rows:
            try:
                toks = json.loads(raw) if raw else []
            except ValueError:
                continue
            if toks:
                tokens[str(cid)] = (str(toks[0]), str(toks[1]) if len(toks) > 1 else None)
        log.info("tokens: {} missing from the manifest, {} recovered from the snapshot", len(missing), len(rows))
    return tokens


def _mapped_table(
    con: duckdb.DuckDBPyConnection,
    mapping: Path,
    manifest: Path,
    snapshot: Path,
) -> int:
    tokens = _load_tokens(con, mapping, manifest, snapshot)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE mapped ("
        " cid VARCHAR, tkr VARCHAR, yes_asset VARCHAR, alt_asset VARCHAR,"
        " confidence DOUBLE, method VARCHAR)"
    )
    rows = []
    for cid, tkr, conf, method in con.execute(
        f"SELECT polymarket_condition_id, kalshi_ticker, confidence, method "
        f"FROM read_parquet('{mapping}')"
    ).fetchall():
        yes_asset, alt_asset = tokens.get(str(cid), (None, None))
        rows.append((str(cid), str(tkr), yes_asset, alt_asset,
                     float(conf) if conf is not None else None, method))
    if rows:
        con.executemany("INSERT INTO mapped VALUES (?,?,?,?,?,?)", rows)
    return len(rows)


def _poly_quotes(
    con: duckdb.DuckDBPyConnection,
    files: Sequence[str],
    start_ns: int,
    end_ns: int,
) -> Tuple[int, int]:
    """Rebuild Polymarket mids from the tape's quote events.

    ``price_change`` is the L2 stream (each nested change carries the token's
    ``best_bid``/``best_ask``); ``book`` snapshots are full books whose best bid
    is the highest bid price and best ask the lowest ask price; ``best_bid_ask``
    frames carry the BBO directly. All mid rows for both of a market's tokens are
    materialised once - the YES rows feed the basis, the pair feeds the
    direction check - so the tape is not scanned twice.
    """
    listing = _parquet_list(files)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE poly_quotes_all AS
        WITH src AS (
            SELECT recv_ts_ns, condition_id, asset_id, event_type,
                   bids, asks, best_bid, best_ask, price_changes
            FROM read_parquet({listing})
            WHERE event_type IN ('book', 'best_bid_ask', 'price_change')
              AND condition_id IN (SELECT cid FROM mapped)
              AND recv_ts_ns >= {start_ns} AND recv_ts_ns <= {end_ns}
        ),
        pc AS (
            SELECT recv_ts_ns, condition_id, UNNEST(price_changes) AS c
            FROM src WHERE event_type = 'price_change'
        ),
        book AS (
            SELECT recv_ts_ns, condition_id, asset_id,
                   (list_max(list_transform(bids, lambda x: x.price))
                    + list_min(list_transform(asks, lambda x: x.price))) / 2.0 AS mid,
                   list_min(list_transform(asks, lambda x: x.price))
                   - list_max(list_transform(bids, lambda x: x.price)) AS spread
            FROM src
            WHERE event_type = 'book' AND len(bids) > 0 AND len(asks) > 0
        ),
        bbo AS (
            SELECT recv_ts_ns, condition_id, asset_id,
                   (best_bid + best_ask) / 2.0 AS mid,
                   best_ask - best_bid AS spread
            FROM src
            WHERE event_type = 'best_bid_ask'
              AND best_bid IS NOT NULL AND best_ask IS NOT NULL
              AND best_ask >= best_bid
        ),
        nested AS (
            SELECT recv_ts_ns, condition_id, c.asset_id AS asset_id,
                   (c.best_bid + c.best_ask) / 2.0 AS mid,
                   c.best_ask - c.best_bid AS spread
            FROM pc
            WHERE c.asset_id IS NOT NULL AND c.best_bid IS NOT NULL
              AND c.best_ask IS NOT NULL AND c.best_ask >= c.best_bid
        ),
        allq AS (
            SELECT * FROM book UNION ALL SELECT * FROM bbo UNION ALL SELECT * FROM nested
        )
        SELECT q.condition_id AS cid, q.asset_id, q.recv_ts_ns AS ts, q.mid, q.spread
        FROM allq q
        WHERE q.mid IS NOT NULL AND q.mid > 0.0 AND q.mid < 1.0
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE poly_quotes AS
        SELECT m.cid, q.ts, q.mid, q.spread
        FROM poly_quotes_all q
        JOIN mapped m ON m.cid = q.cid AND m.yes_asset = q.asset_id
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE token_mid AS
        SELECT m.cid, q.asset_id, AVG(q.mid) AS mid
        FROM poly_quotes_all q
        JOIN mapped m ON m.cid = q.cid
         AND q.asset_id IN (m.yes_asset, m.alt_asset)
        GROUP BY 1, 2
        """
    )
    n_all = int(con.execute("SELECT COUNT(*) FROM poly_quotes_all").fetchone()[0])
    n_yes = int(con.execute("SELECT COUNT(*) FROM poly_quotes").fetchone()[0])
    return n_all, n_yes


def _kalshi_quotes(
    con: duckdb.DuckDBPyConnection, files: Sequence[str], start_ns: int, end_ns: int
) -> int:
    listing = _parquet_list(files)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE kalshi_quotes AS
        SELECT m.cid, k.ticker, k.recv_ts_ns AS ts,
               (k.yes_bid + k.yes_ask) / 2.0 AS mid,
               k.yes_ask - k.yes_bid AS spread
        FROM read_parquet({listing}) k
        JOIN mapped m ON m.tkr = k.ticker
        WHERE k.yes_bid IS NOT NULL AND k.yes_ask IS NOT NULL
          AND k.yes_bid <= k.yes_ask
          AND NOT (k.yes_bid <= 0.0 AND k.yes_ask >= 1.0)
          AND k.recv_ts_ns >= {start_ns} AND k.recv_ts_ns <= {end_ns}
        """
    )
    return int(con.execute("SELECT COUNT(*) FROM kalshi_quotes").fetchone()[0])


def _trade_counts(
    con: duckdb.DuckDBPyConnection,
    poly_files: Sequence[str],
    kalshi_files: Sequence[str],
    start_ns: int,
    end_ns: int,
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]]]:
    poly: Dict[str, Dict[str, float]] = {}
    for cid, n, shares in con.execute(
        f"""
        SELECT m.cid, COUNT(*), COALESCE(SUM(t.size), 0)
        FROM read_parquet({_parquet_list(poly_files)}) t
        JOIN mapped m ON m.cid = t.condition_id
        WHERE t.price IS NOT NULL
          AND t.recv_ts_ns >= {start_ns} AND t.recv_ts_ns <= {end_ns}
        GROUP BY 1
        """
    ).fetchall():
        poly[str(cid)] = {"trades": int(n), "shares": float(shares or 0.0)}

    kalshi: Dict[str, Dict[str, float]] = {}
    for cid, n, contracts in con.execute(
        f"""
        SELECT m.cid, COUNT(*), COALESCE(SUM(k.count), 0)
        FROM read_parquet({_parquet_list(kalshi_files)}) k
        JOIN mapped m ON m.tkr = k.ticker
        WHERE k.recv_ts_ns >= {start_ns} AND k.recv_ts_ns <= {end_ns}
        GROUP BY 1
        """
    ).fetchall():
        kalshi[str(cid)] = {"trades": int(n), "contracts": float(contracts or 0.0)}
    return poly, kalshi


# ------------------------------------------------------------------- report
ARROW_SCHEMA = pa.schema([
    ("condition_id", pa.string()),
    ("kalshi_ticker", pa.string()),
    ("confidence", pa.float64()),
    ("method", pa.string()),
    ("window_start_ns", pa.int64()),
    ("window_end_ns", pa.int64()),
    ("bucket_sec", pa.int32()),
    ("poly_quotes", pa.int64()),
    ("poly_buckets", pa.int64()),
    ("kalshi_quotes", pa.int64()),
    ("kalshi_buckets", pa.int64()),
    ("aligned_buckets", pa.int64()),
    ("coverage", pa.float64()),
    ("poly_mid_mean", pa.float64()),
    ("kalshi_mid_mean", pa.float64()),
    ("basis_mean", pa.float64()),
    ("basis_med_abs", pa.float64()),
    ("basis_std", pa.float64()),
    ("poly_spread_mean", pa.float64()),
    ("poly_spread_med", pa.float64()),
    ("kalshi_spread_mean", pa.float64()),
    ("kalshi_spread_med", pa.float64()),
    ("poly_trades", pa.int64()),
    ("poly_trade_shares", pa.float64()),
    ("kalshi_trades", pa.int64()),
    ("kalshi_contracts", pa.float64()),
    ("poly_change_std", pa.float64()),
    ("kalshi_change_std", pa.float64()),
    ("corr_lag0", pa.float64()),
    ("best_lag", pa.int64()),
    ("best_corr", pa.float64()),
    ("best_pairs", pa.int64()),
    ("direction_ok", pa.bool_()),
    ("status", pa.string()),
])

LAG_SCHEMA = pa.schema([
    ("lag", pa.int32()),
    ("n_pairs", pa.int64()),
    ("n_markets", pa.int64()),
    ("corr", pa.float64()),
    ("window_start_ns", pa.int64()),
    ("window_end_ns", pa.int64()),
    ("bucket_sec", pa.int32()),
])


def _median(values: Sequence[float]) -> Optional[float]:
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=np.float64)
    return float(np.median(arr)) if arr.size else None


def _mean(values: Sequence[float]) -> Optional[float]:
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=np.float64)
    return float(arr.mean()) if arr.size else None


def build_report(
    tape: Path,
    mapping: Path,
    manifest: Path,
    snapshot: Path,
    *,
    window_min: int,
    bucket_sec: int,
    max_stale_sec: int,
    max_lag: int,
    top: int,
    out: Path,
    lags_out: Path,
    summary_out: Path,
) -> str:
    con = duckdb.connect()
    con.execute("SET threads = 2")
    end_ns = _tape_end_ns(con, tape)
    start_ns = end_ns - int(window_min) * 60 * 1_000_000_000
    window = (
        f"{datetime.fromtimestamp(start_ns / 1e9, timezone.utc):%Y-%m-%d %H:%M:%SZ}"
        f" - {datetime.fromtimestamp(end_ns / 1e9, timezone.utc):%Y-%m-%d %H:%M:%SZ}"
    )
    log.info("window {} ({} min, bucket {} s)", window, window_min, bucket_sec)

    files = {
        "clob_events": _files(tape, "clob_events", start_ns, end_ns),
        "clob_trades": _files(tape, "clob_trades", start_ns, end_ns),
        "kalshi_markets": _files(tape, "kalshi_markets", start_ns, end_ns),
        "kalshi_trades": _files(tape, "kalshi_trades", start_ns, end_ns),
    }
    for name, paths in files.items():
        log.info("{}: {} files", name, len(paths))

    n_mapped = _mapped_table(con, mapping, manifest, snapshot)
    log.info("mapped pairs: {}", n_mapped)
    n_poly_all, n_poly = _poly_quotes(con, files["clob_events"], start_ns, end_ns)
    log.info("poly quotes: {} ({} on YES tokens)", n_poly_all, n_poly)
    n_kalshi = _kalshi_quotes(con, files["kalshi_markets"], start_ns, end_ns)
    log.info("kalshi quotes: {}", n_kalshi)
    poly_trades, kalshi_trades = _trade_counts(
        con, files["clob_trades"], files["kalshi_trades"], start_ns, end_ns
    )
    log.info("trades: poly {} markets, kalshi {} markets", len(poly_trades), len(kalshi_trades))

    mapped_rows = con.execute(
        "SELECT cid, tkr, yes_asset, alt_asset, confidence, method FROM mapped ORDER BY cid"
    ).fetchall()

    poly_by_cid: Dict[str, Dict[str, List[float]]] = {}
    for cid, ts, mid, spread in con.execute(
        "SELECT cid, ts, mid, spread FROM poly_quotes ORDER BY cid, ts"
    ).fetchall():
        d = poly_by_cid.setdefault(str(cid), {"ts": [], "mid": [], "spread": []})
        d["ts"].append(int(ts))
        d["mid"].append(float(mid))
        if spread is not None:
            d["spread"].append(float(spread))

    kalshi_by_cid: Dict[str, Dict[str, List[float]]] = {}
    for cid, ts, mid, spread in con.execute(
        "SELECT cid, ts, mid, spread FROM kalshi_quotes ORDER BY cid, ts"
    ).fetchall():
        d = kalshi_by_cid.setdefault(str(cid), {"ts": [], "mid": [], "spread": []})
        d["ts"].append(int(ts))
        d["mid"].append(float(mid))
        if spread is not None:
            d["spread"].append(float(spread))

    token_mid: Dict[str, Dict[str, float]] = {}
    for cid, asset, mid in con.execute("SELECT cid, asset_id, mid FROM token_mid").fetchall():
        token_mid.setdefault(str(cid), {})[str(asset)] = float(mid)

    width_ns = int(bucket_sec) * 1_000_000_000
    grid_start = start_ns // width_ns
    grid_end = end_ns // width_ns
    stale_buckets = max(0, int(max_stale_sec) // int(bucket_sec))
    records: List[Dict[str, Any]] = []
    panel: List[Dict[str, Any]] = []
    both_pairs = 0

    for cid, tkr, yes_asset, alt_asset, confidence, method in mapped_rows:
        cid = str(cid)
        p = poly_by_cid.get(cid)
        k = kalshi_by_cid.get(cid)
        pt = poly_trades.get(cid, {})
        kt = kalshi_trades.get(cid, {})

        pb = pv = kb = kv = np.empty(0)
        if p:
            pb, pv = bucket_last(p["ts"], p["mid"], width_ns)
        if k:
            kb, kv = bucket_last(k["ts"], k["mid"], width_ns)

        status = "ok"
        if not p and not k:
            status = "n/a: no quotes on either venue"
        elif not p:
            status = "n/a: no polymarket quotes"
        elif not k:
            status = "n/a: no kalshi quotes"
        elif pb.size == 0 or kb.size == 0:
            status = "n/a: empty bucket series"

        basis: Dict[str, Optional[float]] = {
            "n": 0, "poly_mid_mean": None, "kalshi_mid_mean": None,
            "basis_mean": None, "basis_med_abs": None, "basis_std": None,
        }
        coverage: Optional[float] = None
        corr_lag0: Optional[float] = None
        best: Optional[Dict[str, Any]] = None
        poly_changes = kalshi_changes = np.empty(0)
        change_ids = np.empty(0, dtype=np.int64)
        aligned = 0
        dir_ok: Optional[bool] = None

        if pb.size and kb.size:
            both_pairs += 1
            poly_grid = fill_grid(pb, pv, grid_start, grid_end, stale_buckets)
            kal_grid = fill_grid(kb, kv, grid_start, grid_end, stale_buckets)
            both_fresh = np.isfinite(poly_grid) & np.isfinite(kal_grid)
            aligned = int(np.count_nonzero(both_fresh))
            coverage = float(aligned / poly_grid.size) if poly_grid.size else None
            if aligned:
                basis = basis_summary(poly_grid, kal_grid)
                tm = token_mid.get(cid, {})
                dir_ok = orientation_ok(
                    tm.get(str(yes_asset)) if yes_asset else None,
                    tm.get(str(alt_asset)) if alt_asset else None,
                    float(kal_grid[both_fresh].mean()),
                )
                change_ids, poly_changes, kalshi_changes = aligned_changes(
                    poly_grid, kal_grid, grid_start
                )
                if change_ids.size:
                    rows = lag_table(
                        change_ids, poly_changes, change_ids, kalshi_changes, max_lag
                    )
                    corr_lag0 = next((r["corr"] for r in rows if r["lag"] == 0), None)
                    best = peak_lag(rows)
                    # A pair whose other token sits closer to Kalshi's YES mid is
                    # pointed the wrong way (or mapped to different semantics);
                    # its change correlation would enter the panel with the wrong
                    # sign, so it is reported but kept out of the pooled table.
                    if change_ids.size >= MIN_LAG_PAIRS and dir_ok is not False:
                        panel.append({
                            "poly_ids": change_ids,
                            "poly_changes": poly_changes,
                            "kalshi_ids": change_ids,
                            "kalshi_changes": kalshi_changes,
                        })
            if basis["basis_mean"] is None and status == "ok":
                status = f"n/a: fewer than {MIN_BASIS_BUCKETS} aligned buckets"

        if dir_ok is False:
            status = "ok (direction flagged)"

        records.append({
            "condition_id": cid,
            "kalshi_ticker": str(tkr),
            "confidence": float(confidence) if confidence is not None else None,
            "method": method,
            "window_start_ns": start_ns,
            "window_end_ns": end_ns,
            "bucket_sec": int(bucket_sec),
            "poly_quotes": len(p["mid"]) if p else 0,
            "poly_buckets": int(pb.size),
            "kalshi_quotes": len(k["mid"]) if k else 0,
            "kalshi_buckets": int(kb.size),
            "aligned_buckets": int(aligned),
            "coverage": coverage,
            "poly_mid_mean": basis["poly_mid_mean"],
            "kalshi_mid_mean": basis["kalshi_mid_mean"],
            "basis_mean": basis["basis_mean"],
            "basis_med_abs": basis["basis_med_abs"],
            "basis_std": basis["basis_std"],
            "poly_spread_mean": _mean(p["spread"]) if p else None,
            "poly_spread_med": _median(p["spread"]) if p else None,
            "kalshi_spread_mean": _mean(k["spread"]) if k else None,
            "kalshi_spread_med": _median(k["spread"]) if k else None,
            "poly_trades": int(pt.get("trades", 0)),
            "poly_trade_shares": float(pt.get("shares", 0.0)),
            "kalshi_trades": int(kt.get("trades", 0)),
            "kalshi_contracts": float(kt.get("contracts", 0.0)),
            "poly_change_std": float(np.std(poly_changes)) if poly_changes.size else None,
            "kalshi_change_std": float(np.std(kalshi_changes)) if kalshi_changes.size else None,
            "corr_lag0": corr_lag0,
            "best_lag": int(best["lag"]) if best else None,
            "best_corr": float(best["corr"]) if best else None,
            "best_pairs": int(best["n"]) if best else None,
            "direction_ok": dir_ok,
            "status": status,
        })
        log.debug("{} {} aligned={} basis={}", cid[:12], tkr, aligned, basis["basis_mean"])

    lag_rows = pooled_lag_table(panel, max_lag)
    for r in lag_rows:
        r["window_start_ns"] = start_ns
        r["window_end_ns"] = end_ns
        r["bucket_sec"] = int(bucket_sec)
    log.info("panel: {} markets, lags {}", len(panel), len(lag_rows))

    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(records, schema=ARROW_SCHEMA), out)
    pq.write_table(pa.Table.from_pylist(lag_rows, schema=LAG_SCHEMA), lags_out)
    log.info("wrote {} rows to {}", len(records), out)

    text = _summary(
        records, lag_rows, window, window_min, bucket_sec, max_stale_sec, max_lag,
        n_mapped, both_pairs, n_poly, n_kalshi, top, out,
    )
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.write_text(text + "\n", encoding="utf-8")
    return text


def _summary(
    records: Sequence[Dict[str, Any]],
    lag_rows: Sequence[Dict[str, Any]],
    window: str,
    window_min: int,
    bucket_sec: int,
    max_stale_sec: int,
    max_lag: int,
    n_mapped: int,
    both_pairs: int,
    n_poly: int,
    n_kalshi: int,
    top: int,
    out: Path,
) -> str:
    with_basis = [r for r in records if r["basis_mean"] is not None]
    wide = [
        r for r in with_basis
        if r["poly_spread_med"] is not None and r["poly_spread_med"] > 0.10
    ]
    flagged = [r for r in records if r["direction_ok"] is False]
    lag0 = [r for r in records if r["corr_lag0"] is not None]
    best = peak_lag(lag_rows)

    lines = [
        f"window: {window}  ({window_min} min, bucket {bucket_sec} s, "
        f"max stale {max_stale_sec} s, lags ±{max_lag})",
        f"mapping: {n_mapped:,} pairs; {both_pairs:,} quoted on both venues; "
        f"{len(with_basis):,} with >= {MIN_BASIS_BUCKETS} aligned buckets; "
        f"{n_mapped - len(with_basis):,} n/a",
        f"quotes: polymarket {n_poly:,} (YES token) · kalshi {n_kalshi:,}",
        f"direction-flagged pairs (other token closer to Kalshi YES): {len(flagged):,}"
        " - reported, kept out of the lag panel",
        "",
        "basis = poly_mid - kalshi_mid on buckets where both venues have a fresh mid",
        f"  per-market mean of basis: median {fmt_opt(_median([r['basis_mean'] for r in with_basis]), 8)}, "
        f"min {fmt_opt(min((r['basis_mean'] for r in with_basis), default=None), 8)}, "
        f"max {fmt_opt(max((r['basis_mean'] for r in with_basis), default=None), 8)}",
        f"  per-market median |basis|: median {fmt_opt(_median([r['basis_med_abs'] for r in with_basis]), 8)}",
        f"  polymarket spread (median of per-market medians): "
        f"{fmt_opt(_median([r['poly_spread_med'] for r in with_basis]), 8)}",
        f"  kalshi spread (median of per-market medians): "
        f"{fmt_opt(_median([r['kalshi_spread_med'] for r in with_basis]), 8)}",
        f"  per-market corr of levels at lag 0: median {fmt_opt(_median([r['corr_lag0'] for r in lag0]), 8)} "
        f"({len(lag0)} markets)",
        f"  wide polymarket books (median spread > 10c): {len(wide):,} markets - "
        "their mid sits between far-apart quotes, so the basis is a book artefact, "
        "not a tradeable gap",
        "",
        f"lag table: corr(Δpolymarket at b, Δkalshi at b+lag), per-market z-scored, "
        f"pooled; negative lag = Kalshi moves first",
        *format_lag_rows(lag_rows),
        (
            f"peak |corr|: lag {best['lag']:+d} r={best['corr']:.4f} "
            f"(n={best['n_pairs']:,} pairs, {best['n_markets']} markets)"
            if best else "peak |corr|: n/a (no lag reached the sample floor)"
        ),
        "",
        f"per-market (top {min(top, len(records))} by aligned buckets; "
        f"'n/a' = below sample floor):",
    ]

    header = (
        f"{'condition':<14} {'kalshi_ticker':<26} {'algn':>4} {'basis':>9} "
        f"{'|basis|':>9} {'pol_spr':>9} {'kal_spr':>9} {'p_trd':>6} {'k_trd':>6} "
        f"{'r(lag0)':>8} {'best':>5} {'dir':>4}"
    )
    lines += [header, "-" * len(header)]
    ranked = sorted(records, key=lambda r: (-int(r["aligned_buckets"]), r["condition_id"]))
    for r in ranked[:top]:
        best_lag_txt = f"{r['best_lag']:+d}" if r["best_lag"] is not None else "n/a"
        dir_txt = {True: "ok", False: "flag", None: "-"}[r["direction_ok"]]
        lines.append(
            f"{r['condition_id'][:14]:<14} {r['kalshi_ticker'][:26]:<26} "
            f"{r['aligned_buckets']:>4} {fmt_opt(r['basis_mean'], 9)} "
            f"{fmt_opt(r['basis_med_abs'], 9)} {fmt_opt(r['poly_spread_med'], 9)} "
            f"{fmt_opt(r['kalshi_spread_med'], 9)} {r['poly_trades']:>6} "
            f"{r['kalshi_trades']:>6} {fmt_opt(r['corr_lag0'], 8)} {best_lag_txt:>5} "
            f"{dir_txt:>4}"
        )
    lines += [
        "",
        f"trades in window: polymarket {sum(r['poly_trades'] for r in records):,} "
        f"({sum(r['poly_trade_shares'] for r in records):,.0f} shares), "
        f"kalshi {sum(r['kalshi_trades'] for r in records):,} "
        f"({sum(r['kalshi_contracts'] for r in records):,.0f} contracts)",
        f"output: {out} (+ data/derived/kalshi_basis_lags.parquet)",
        "",
        "caveats",
        f"  buckets: last quote per {bucket_sec}s bucket, carried forward at most "
        f"{max_stale_sec} s; after that the mid is unknown (NaN) and the bucket is "
        "dropped from basis and changes.",
        f"  samples: per-market lag pairs < {MIN_LAG_PAIRS} are excluded; the pooled "
        f"panel needs >= {MIN_LAG_PANEL} pairs and >= 2 markets, else n/a.",
        "  dependence: markets share a common election factor and each market's "
        "changes are autocorrelated, so the printed n is not the effective sample "
        "size; these correlations are descriptive, not a significance test.",
        f"  coverage: {both_pairs}/{n_mapped} mapped pairs are quoted on both venues "
        "in this window - the rest are reported as n/a, not as a zero basis.",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tape", type=Path, default=DEFAULT_TAPE)
    ap.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    ap.add_argument("--window-min", type=int, default=DEFAULT_WINDOW_MIN)
    ap.add_argument("--bucket-sec", type=int, default=DEFAULT_BUCKET_SEC)
    ap.add_argument(
        "--max-stale-sec", type=int, default=DEFAULT_MAX_STALE_SEC,
        help="how long a quote may be carried forward before the mid is unknown",
    )
    ap.add_argument("--max-lag", type=int, default=DEFAULT_MAX_LAG)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--lags-out", type=Path, default=DEFAULT_LAGS_OUT)
    ap.add_argument("--summary-out", type=Path, default=DEFAULT_SUMMARY_OUT)
    args = ap.parse_args(argv)

    text = build_report(
        args.tape,
        args.mapping,
        args.manifest,
        args.snapshot,
        window_min=args.window_min,
        bucket_sec=args.bucket_sec,
        max_stale_sec=args.max_stale_sec,
        max_lag=args.max_lag,
        top=args.top,
        out=args.out,
        lags_out=args.lags_out,
        summary_out=args.summary_out,
    )
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
