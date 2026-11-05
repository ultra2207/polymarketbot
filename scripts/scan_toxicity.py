#!/usr/bin/env python3
"""Toxicity scan: which conditions are being picked off, and in which 5-minute bucket.

The question this answers is narrow on purpose: *before* a maker quotes, can a
cheap tape signal say "this market is being picked off right now"? If yes, the
quoting layer can widen or cancel instead of paying for the information.

Window
    The last ``--hours`` UTC hours (default 2), ending at ``--end`` (default
    now). Only tape partitions covering that window are read, and only assets
    that actually traded in it -- the book of 6,800 assets is not carried.

Per condition and per ``--bucket-seconds`` (default 300 s) bucket:

* **signed markout** at 30 s and 5 m. Each tape trade is matched to the first
  reconstructed top-of-book at or after ``trade + h`` via an ASOF join, and only
  if that quote lands within ``--match-window-seconds`` (default 60 s) of the
  horizon -- a quiet market must not silently borrow a quote from an hour later.
  The move is signed by the taker side: ``mid - price`` for BUY, ``price - mid``
  for SELL, so positive = taker profit = maker adverse selection. Aggregation is
  size weighted. Both tokens of a condition stay coherent under this sign (an
  informed YES-up trade is a BUY of YES and a SELL of NO, both positive), so a
  condition's two tokens are pooled rather than netted against each other.
  A matched quote wider than ``MAX_QUOTE_SPREAD_MULTIPLE`` (10x) the condition's
  median spread is a transient one-sided book, not a price, and is dropped: the
  count of those drops is printed so the filter is never silent.

* **trade imbalance** ``(buy_volume - sell_volume) / (buy_volume + sell_volume)``.

* **OFI** from consecutive top-of-book changes, Cont-style but price-only (the
  tape has no size at the top of book): ``+1`` per tick the bid rises, ``-1`` per
  tick it falls, ``+1`` per tick the ask falls, ``-1`` per tick it rises, summed
  over the bucket and divided by the number of top-of-book changes, so the value
  is a mean step in ``[-2, 2]``. Size-only events are not changes and do not
  enter the denominator.

Flags (thresholds are written to the output and the summary, never left implicit)
    * ``markout``: ``markout(h) > 0.5 * median_spread(condition)`` at 30 s or
      5 m, where the median spread is the window median of the reconstructed
      top-of-book. Positive markout only: a losing taker is a maker's profit, not
      a pick-off. Needs at least ``--min-matched`` (default 3) matched trades.
    * ``ofi``: ``|mean OFI step|`` beyond the pooled p95 over buckets with at
      least ``--min-ofi-changes`` (default 10) top-of-book changes. If fewer than
      ``MIN_OFI_BUCKETS`` buckets are eligible the threshold is unavailable and
      no bucket is OFI-flagged.

Missing is never zero
    A bucket with no trades is status ``no_trades`` with NULL markout and NULL
    imbalance -- not 0, which would read as "flow was balanced". A bucket whose
    trades never matched a quote inside the window is ``unmatched``; too few
    matches for a markout is ``thin``. Only ``ok`` buckets can carry a markout
    flag. The OFI flag reads the book rather than the tape, so it can fire on a
    bucket that had no trades -- that is book pressure, and the row says so in
    ``status`` and ``flag_reason``.

Output
    ``data/derived/toxicity_flags.parquet`` (one row per condition-bucket, all
    buckets observed in the window, flagged or not) and a plain-text summary at
    ``data/derived/toxicity_summary.txt`` that states the flagged/total counts
    and the coverage.

Usage
    .venv/Scripts/python.exe scripts/scan_toxicity.py
    .venv/Scripts/python.exe scripts/scan_toxicity.py --hours 6 --top 20
    .venv/Scripts/python.exe scripts/scan_toxicity.py --end 2026-11-05T04:00
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import duckdb  # noqa: E402

from polymarketbot.logs import get_logger  # noqa: E402

log = get_logger("scan_toxicity")

DEFAULT_TAPE = "data/tape"
DEFAULT_OUT = "data/derived/toxicity_flags.parquet"
DEFAULT_SUMMARY = "data/derived/toxicity_summary.txt"
DEFAULT_MANIFEST = "data/tape/manifest_current.json"

BUCKET_S = 300
HORIZONS: Tuple[int, ...] = (30, 300)
MATCH_WINDOW_S = 60
MIN_MATCHED = 3
MIN_OFI_CHANGES = 10
OFI_TAIL_QUANTILE = 0.95
MIN_OFI_BUCKETS = 20  # below this the pooled p95 is not a distribution, it is noise
QUOTE_LOOKBACK_S = 300  # so the first quote of the window has a predecessor for OFI
# A matched quote wider than this multiple of the condition's window-median
# spread is a transient one-sided book, not a price: it is dropped (unmatched),
# because its mid would manufacture a markout that never happened.
MAX_QUOTE_SPREAD_MULTIPLE = 10.0

NS = 1_000_000_000


# --------------------------------------------------------------------- helpers
def bucket_start_ns(ts_ns: int, bucket_s: int = BUCKET_S) -> int:
    """Start of the bucket containing ``ts_ns`` (UTC epoch nanoseconds)."""
    size = int(bucket_s) * NS
    return (int(ts_ns) // size) * size


def signed_move(side: Optional[str], price: Optional[float], mid: Optional[float]) -> Optional[float]:
    """Per-share taker markout: ``mid - price`` for BUY, ``price - mid`` for SELL.

    Positive = the taker's fill moved the mid their way = the maker was picked
    off. An unknown side is ``None`` (unmatched), never a zero.
    """
    if price is None or mid is None:
        return None
    tag = (side or "").strip().upper()
    if tag == "BUY":
        return float(mid) - float(price)
    if tag == "SELL":
        return float(price) - float(mid)
    return None


def trade_imbalance(buy_volume: float, sell_volume: float) -> Optional[float]:
    """``(buy - sell) / (buy + sell)``; ``None`` when no volume was observed."""
    buy = float(buy_volume or 0.0)
    sell = float(sell_volume or 0.0)
    total = buy + sell
    if total <= 0.0:
        return None
    return (buy - sell) / total


def ofi_step(
    prev_bid: Optional[float],
    prev_ask: Optional[float],
    bid: Optional[float],
    ask: Optional[float],
) -> Optional[int]:
    """One top-of-book change as an order-flow-imbalance step in ``[-2, 2]``.

    ``+1`` per tick the bid rises, ``-1`` per tick it falls, ``+1`` per tick the
    ask falls (buyers lifting), ``-1`` per tick it rises. ``None`` when either
    book is incomplete -- a partial top of book is not a zero step.
    """
    if prev_bid is None or prev_ask is None or bid is None or ask is None:
        return None
    step = 0
    if bid > prev_bid:
        step += 1
    elif bid < prev_bid:
        step -= 1
    if ask < prev_ask:
        step += 1
    elif ask > prev_ask:
        step -= 1
    return step


def weighted_markout(moves: Iterable[Tuple[Optional[float], float]]) -> Tuple[Optional[float], float, int]:
    """Size-weighted mean of ``(move, size)`` pairs.

    Returns ``(markout, matched_size, matched_count)``; ``markout`` is ``None``
    when nothing matched, so an empty cell stays empty instead of collapsing to
    a zero that would read as "flow was balanced".
    """
    moved = 0.0
    size = 0.0
    n = 0
    for move, shares in moves:
        if move is None or shares is None or shares <= 0.0:
            continue
        moved += float(move) * float(shares)
        size += float(shares)
        n += 1
    if size <= 0.0:
        return None, 0.0, 0
    return moved / size, size, n


def quantile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolation quantile (numpy's default) without numpy."""
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * float(q)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def ofi_tail_threshold(
    values: Iterable[Optional[float]],
    *,
    q: float = OFI_TAIL_QUANTILE,
    min_buckets: int = MIN_OFI_BUCKETS,
) -> Tuple[Optional[float], int]:
    """Pooled ``|value|`` quantile, plus the sample size it was computed on."""
    magnitudes = [abs(float(v)) for v in values if v is not None]
    if len(magnitudes) < int(min_buckets):
        return None, len(magnitudes)
    return quantile(magnitudes, q), len(magnitudes)


def classify_bucket(
    *,
    n_trades: int,
    matched: Dict[int, int],
    markouts: Dict[int, Optional[float]],
    median_spread: Optional[float],
    ofi_per_change: Optional[float],
    ofi_changes: int,
    ofi_threshold: Optional[float],
    min_matched: int = MIN_MATCHED,
    min_ofi_changes: int = MIN_OFI_CHANGES,
) -> Dict[str, Any]:
    """Status and flags for one condition-bucket.

    ``status`` is one of ``no_trades`` (nothing traded), ``unmatched`` (trades
    but no quote inside the match window), ``thin`` (matched below
    ``min_matched``), ``ok``. A markout flag is only raised on an ``ok`` bucket
    and only when the threshold that would justify it is available. The OFI flag
    reads the book rather than the tape, so it can fire on a bucket with no
    trades -- that is a pick-off warning, not a fabricated markout.
    """
    if n_trades <= 0:
        status = "no_trades"
    elif not any(int(v or 0) > 0 for v in matched.values()):
        status = "unmatched"
    elif max(int(v or 0) for v in matched.values()) < int(min_matched):
        status = "thin"
    else:
        status = "ok"

    half_spread = None if median_spread is None else float(median_spread) / 2.0
    markout_flags: Dict[str, bool] = {}
    for horizon in sorted(markouts):
        markout = markouts.get(horizon)
        markout_flags[f"markout_{horizon}s"] = bool(
            status == "ok"
            and half_spread is not None
            and markout is not None
            and int(matched.get(horizon) or 0) >= int(min_matched)
            and float(markout) > half_spread
        )
    ofi_flag = bool(
        ofi_threshold is not None
        and ofi_per_change is not None
        and int(ofi_changes) >= int(min_ofi_changes)
        and abs(float(ofi_per_change)) > float(ofi_threshold)
    )
    reasons = [name for name, on in markout_flags.items() if on]
    if ofi_flag:
        reasons.append("ofi")
    return {
        "status": status,
        "half_spread": half_spread,
        "markout_flags": markout_flags,
        "flag_ofi": ofi_flag,
        "flagged": bool(reasons),
        "flag_reason": "+".join(reasons),
    }


# ---------------------------------------------------------------- aggregation
def aggregate_trades(
    rows: Iterable[Dict[str, Any]],
    *,
    horizons: Sequence[int] = HORIZONS,
    match_window_s: int = MATCH_WINDOW_S,
    bucket_s: int = BUCKET_S,
    median_spreads: Optional[Dict[str, Optional[float]]] = None,
    max_spread_multiple: float = MAX_QUOTE_SPREAD_MULTIPLE,
) -> Dict[Tuple[str, int], Dict[str, Any]]:
    """Per ``(condition_id, bucket_start_ns)`` trade aggregates.

    ``rows`` carry ``condition_id, recv_ts_ns, price, size, side`` and ``mids``:
    a mapping ``horizon_s -> (quote_ts_ns, quote_mid, quote_spread)`` from the
    ASOF join. Two rules are applied here, so both are testable without tape: a
    quote more than ``match_window_s`` past the horizon does not count, and a
    quote wider than ``max_spread_multiple`` times the condition's median spread
    does not count either (a transient one-sided book is not a price).
    """
    out: Dict[Tuple[str, int], Dict[str, Any]] = {}
    win_ns = int(match_window_s) * NS
    for row in rows:
        condition_id = row["condition_id"]
        if not condition_id:
            continue
        ts = int(row["recv_ts_ns"])
        key = (str(condition_id), bucket_start_ns(ts, bucket_s))
        cell = out.get(key)
        if cell is None:
            cell = out[key] = {
                "trades": 0,
                "volume": 0.0,
                "buy_volume": 0.0,
                "sell_volume": 0.0,
                "matched": {int(h): 0 for h in horizons},
                "moves": {int(h): [] for h in horizons},
                "spreads": {int(h): [] for h in horizons},
                "wide_quotes": 0,
            }
        cell["trades"] += 1
        size = float(row.get("size") or 0.0)
        cell["volume"] += size
        side = (row.get("side") or "").strip().upper()
        if side == "BUY":
            cell["buy_volume"] += size
        elif side == "SELL":
            cell["sell_volume"] += size
        for horizon in horizons:
            h = int(horizon)
            quote = (row.get("mids") or {}).get(h)
            if not quote:
                continue
            quote_ts, quote_mid = quote[0], quote[1]
            quote_spread = quote[2] if len(quote) > 2 else None
            if quote_ts is None or quote_mid is None:
                continue
            if int(quote_ts) >= ts + h * NS + win_ns:
                continue  # stale quote: beyond the match window, use nothing
            reference = (median_spreads or {}).get(str(condition_id))
            if (
                reference
                and quote_spread is not None
                and float(quote_spread) > float(max_spread_multiple) * float(reference)
            ):
                cell["wide_quotes"] += 1
                continue
            move = signed_move(side, row.get("price"), quote_mid)
            if move is None:
                continue
            cell["matched"][h] += 1
            cell["moves"][h].append((move, size))
            if quote_spread is not None:
                cell["spreads"][h].append(float(quote_spread))
    return out


def aggregate_ofi(
    rows: Iterable[Dict[str, Any]],
    *,
    bucket_s: int = BUCKET_S,
    start_ns: int = 0,
) -> Dict[Tuple[str, int], Dict[str, Any]]:
    """Per ``(condition_id, bucket)`` OFI from consecutive top-of-book changes.

    ``rows`` are ``(condition_id, asset_id, recv_ts_ns, best_bid, best_ask)`` in
    arrival order per asset; the caller drops unchanged books. Rows older than
    ``start_ns`` are skipped but still supply the predecessor of the window's
    first change.
    """
    out: Dict[Tuple[str, int], Dict[str, Any]] = {}
    prev: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
    for row in rows:
        asset_id = row["asset_id"]
        bid = row.get("best_bid")
        ask = row.get("best_ask")
        before = prev.get(str(asset_id))
        prev[str(asset_id)] = (bid, ask)
        if before is None:
            continue
        step = ofi_step(before[0], before[1], bid, ask)
        if step is None:
            continue
        ts = int(row["recv_ts_ns"])
        if ts < int(start_ns):
            continue
        condition_id = row.get("condition_id")
        if not condition_id:
            continue
        key = (str(condition_id), bucket_start_ns(ts, bucket_s))
        cell = out.get(key)
        if cell is None:
            cell = out[key] = {"ofi_sum": 0, "ofi_changes": 0}
        cell["ofi_sum"] += int(step)
        cell["ofi_changes"] += 1
    return out


# ------------------------------------------------------------------------ tape
def _partitions(start_ns: int, end_ns: int) -> List[Tuple[str, str]]:
    """``(date, hour)`` partitions touched by ``[start_ns, end_ns]``, UTC."""
    start = datetime.fromtimestamp(start_ns / NS, timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(end_ns / NS, timezone.utc)
    out: List[Tuple[str, str]] = []
    stamp = start
    while stamp <= end:
        out.append((stamp.strftime("%Y-%m-%d"), f"{stamp.hour:02d}"))
        stamp += timedelta(hours=1)
    return out


def _partition_predicate(parts: Sequence[Tuple[str, str]], prefix: str = "") -> str:
    by_date: Dict[str, List[str]] = {}
    for date, hour in parts:
        by_date.setdefault(date, []).append(hour)
    clauses = []
    for date, hours in sorted(by_date.items()):
        joined = ", ".join(f"'{h}'" for h in sorted(set(hours)))
        clauses.append(f"({prefix}date = '{date}' AND {prefix}hour IN ({joined}))")
    return "(" + " OR ".join(clauses) + ")" if clauses else "(FALSE)"


def _load_quotes(
    con: duckdb.DuckDBPyConnection,
    tape: Path,
    where: str,
    start_ns: int,
    end_ns: int,
    quote_end_ns: int,
) -> int:
    """Materialise the reconstructed top of book for assets that traded."""
    events = (tape / "clob_events").as_posix()
    trades = (tape / "clob_trades").as_posix()
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE quotes AS
        WITH pc AS (
            SELECT recv_ts_ns, UNNEST(price_changes) AS c
            FROM read_parquet('{events}/**/*.parquet')
            WHERE {where} AND event_type = 'price_change' AND recv_ts_ns <= {quote_end_ns}
        ),
        traded AS (
            SELECT asset_id, ANY_VALUE(condition_id) AS condition_id
            FROM read_parquet('{trades}/**/*.parquet')
            WHERE {where} AND asset_id IS NOT NULL AND condition_id IS NOT NULL
              AND recv_ts_ns >= {start_ns} AND recv_ts_ns <= {end_ns}
            GROUP BY 1
        )
        SELECT c.asset_id AS asset_id, traded.condition_id AS condition_id,
               pc.recv_ts_ns AS recv_ts_ns,
               c.best_bid AS best_bid, c.best_ask AS best_ask,
               (c.best_bid + c.best_ask) / 2.0 AS mid,
               (c.best_ask - c.best_bid) AS spread
        FROM pc JOIN traded ON traded.asset_id = c.asset_id
        WHERE c.asset_id IS NOT NULL
          AND c.best_bid IS NOT NULL AND c.best_ask IS NOT NULL
        UNION ALL
        SELECT b.asset_id AS asset_id, traded.condition_id AS condition_id,
               b.recv_ts_ns AS recv_ts_ns, b.best_bid AS best_bid, b.best_ask AS best_ask,
               (b.best_bid + b.best_ask) / 2.0,
               (b.best_ask - b.best_bid)
        FROM read_parquet('{events}/**/*.parquet') b
        JOIN traded ON traded.asset_id = b.asset_id
        WHERE {where} AND event_type = 'best_bid_ask' AND b.recv_ts_ns <= {quote_end_ns}
          AND b.best_bid IS NOT NULL AND b.best_ask IS NOT NULL
        """
    )
    return int(con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0])


def _trade_mids(
    con: duckdb.DuckDBPyConnection,
    tape: Path,
    where: str,
    start_ns: int,
    end_ns: int,
    horizons: Sequence[int],
) -> List[Dict[str, Any]]:
    """Tape trades in the window with the ASOF-matched mid at each horizon."""
    trades = (tape / "clob_trades").as_posix()
    join = "\n        ".join(
        f"ASOF LEFT JOIN quotes q{h} ON t.asset_id = q{h}.asset_id "
        f"AND q{h}.recv_ts_ns >= t.recv_ts_ns + {int(h) * NS}"
        for h in horizons
    )
    select = ",\n               ".join(
        f"q{h}.recv_ts_ns AS q{h}_ts, q{h}.mid AS q{h}_mid, q{h}.spread AS q{h}_spread"
        for h in horizons
    )
    rows = con.execute(
        f"""
        SELECT t.condition_id, t.asset_id, t.recv_ts_ns, t.price, t.size, t.side,
               {select}
        FROM read_parquet('{trades}/**/*.parquet') t
        {join}
        WHERE {where.replace('date', 't.date').replace('hour', 't.hour')}
          AND t.recv_ts_ns >= {start_ns} AND t.recv_ts_ns <= {end_ns}
          AND t.asset_id IS NOT NULL AND t.condition_id IS NOT NULL
          AND t.price IS NOT NULL AND t.size IS NOT NULL
        """
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        condition_id, asset_id, ts, price, size, side = row[:6]
        rest = row[6:]
        mids = {}
        for i, horizon in enumerate(horizons):
            mids[int(horizon)] = (rest[3 * i], rest[3 * i + 1], rest[3 * i + 2])
        out.append(
            {
                "condition_id": condition_id,
                "asset_id": asset_id,
                "recv_ts_ns": int(ts),
                "price": price,
                "size": size,
                "side": side,
                "mids": mids,
            }
        )
    return out


def _ofi_rows(con: duckdb.DuckDBPyConnection) -> List[Dict[str, Any]]:
    """Per-asset top-of-book changes in arrival order (unchanged books dropped)."""
    rows = con.execute(
        """
        WITH q AS (
            SELECT DISTINCT asset_id, condition_id, recv_ts_ns, best_bid, best_ask
            FROM quotes
            WHERE asset_id IS NOT NULL AND condition_id IS NOT NULL
        ),
        d AS (
            SELECT asset_id, condition_id, recv_ts_ns, best_bid, best_ask,
                   LAG(best_bid) OVER (PARTITION BY asset_id ORDER BY recv_ts_ns) AS prev_bid,
                   LAG(best_ask) OVER (PARTITION BY asset_id ORDER BY recv_ts_ns) AS prev_ask
            FROM q
        )
        SELECT condition_id, asset_id, recv_ts_ns, best_bid, best_ask, prev_bid, prev_ask
        FROM d
        WHERE prev_bid IS NOT NULL AND prev_ask IS NOT NULL
          AND (best_bid IS DISTINCT FROM prev_bid OR best_ask IS DISTINCT FROM prev_ask)
        ORDER BY asset_id, recv_ts_ns
        """
    ).fetchall()
    return [
        {
            "condition_id": cid,
            "asset_id": aid,
            "recv_ts_ns": int(ts),
            "best_bid": bid,
            "best_ask": ask,
            "prev_bid": pbid,
            "prev_ask": pask,
        }
        for cid, aid, ts, bid, ask, pbid, pask in rows
    ]


def _median_spreads(con: duckdb.DuckDBPyConnection) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    rows = con.execute(
        """
        SELECT condition_id, COUNT(*) AS n, COUNT(DISTINCT asset_id) AS assets,
               MEDIAN(spread) AS med
        FROM quotes
        WHERE condition_id IS NOT NULL AND spread > 0
        GROUP BY 1
        """
    ).fetchall()
    for cid, n, assets, med in rows:
        out[str(cid)] = {
            "quotes": int(n),
            "assets": int(assets),
            "median_spread": float(med) if med is not None else None,
        }
    return out


def _questions(con: duckdb.DuckDBPyConnection, manifest: Path) -> Dict[str, str]:
    """``condition_id -> question`` from the universe manifest, best effort."""
    if not manifest.exists():
        log.warning(f"manifest missing ({manifest}); questions will be blank")
        return {}
    try:
        payload = con.execute(
            f"SELECT unnest(markets) AS market FROM read_json_auto('{manifest.as_posix()}')"
        ).fetchall()
    except Exception as exc:  # pragma: no cover - manifest shape drift must not kill the scan
        log.warning(f"manifest unreadable ({exc}); questions will be blank")
        return {}
    out: Dict[str, str] = {}
    for (market,) in payload:
        if not isinstance(market, dict):
            continue
        cid = market.get("condition_id") or market.get("conditionId")
        question = market.get("question")
        if cid and question:
            out[str(cid)] = str(question)
    return out


# ------------------------------------------------------------------------ scan
def scan(
    tape: Path = Path(DEFAULT_TAPE),
    *,
    end: Optional[datetime] = None,
    hours: float = 2.0,
    bucket_s: int = BUCKET_S,
    horizons: Sequence[int] = HORIZONS,
    match_window_s: int = MATCH_WINDOW_S,
    min_matched: int = MIN_MATCHED,
    min_ofi_changes: int = MIN_OFI_CHANGES,
    ofi_quantile: float = OFI_TAIL_QUANTILE,
    out: Path = Path(DEFAULT_OUT),
    summary: Path = Path(DEFAULT_SUMMARY),
    manifest: Path = Path(DEFAULT_MANIFEST),
    top: int = 15,
) -> Dict[str, Any]:
    end_dt = (end or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end_ns = int(end_dt.timestamp()) * NS
    start_ns = end_ns - int(float(hours) * 3600 * NS)
    parts = _partitions(start_ns, end_ns)
    where = _partition_predicate(parts)
    log.info(
        f"window {datetime.fromtimestamp(start_ns / NS, timezone.utc):%Y-%m-%d %H:%M} "
        f"-> {end_dt:%Y-%m-%d %H:%M} UTC; partitions {parts}"
    )

    con = duckdb.connect()
    horizon_list = [int(h) for h in horizons]
    quote_end_ns = end_ns + (max(horizon_list) + int(match_window_s)) * NS
    quote_where = _partition_predicate(
        _partitions(start_ns - QUOTE_LOOKBACK_S * NS, quote_end_ns)
    )
    quote_rows = _load_quotes(con, tape, quote_where, start_ns, end_ns, quote_end_ns)
    log.info(f"reconstructed quote points: {quote_rows:,}")

    spreads = _median_spreads(con)
    trade_rows = _trade_mids(con, tape, where, start_ns, end_ns, horizon_list)
    log.info(f"trades in window: {len(trade_rows):,}")
    bucket_trades = aggregate_trades(
        trade_rows,
        horizons=horizon_list,
        match_window_s=match_window_s,
        bucket_s=bucket_s,
        median_spreads={cid: meta.get("median_spread") for cid, meta in spreads.items()},
    )

    ofi_rows = _ofi_rows(con)
    log.info(f"top-of-book changes: {len(ofi_rows):,}")
    bucket_ofi = aggregate_ofi(ofi_rows, bucket_s=bucket_s, start_ns=start_ns)

    questions = _questions(con, manifest)
    con.close()

    # ---- per-bucket markout / imbalance
    cells: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for key, trade in bucket_trades.items():
        markouts: Dict[int, Optional[float]] = {}
        matched: Dict[int, int] = {}
        for horizon in horizon_list:
            value, _size, n = weighted_markout(trade["moves"][horizon])
            markouts[horizon] = value
            matched[horizon] = n
        cells[key] = {
            "condition_id": key[0],
            "bucket_ts_ns": key[1],
            "n_trades": trade["trades"],
            "volume": trade["volume"],
            "buy_volume": trade["buy_volume"],
            "sell_volume": trade["sell_volume"],
            "trade_imbalance": trade_imbalance(trade["buy_volume"], trade["sell_volume"]),
            "matched": matched,
            "markouts": markouts,
            "matched_spreads": {
                h: quantile(trade["spreads"][h], 0.5) for h in horizon_list
            },
            "wide_quotes": int(trade["wide_quotes"]),
            "ofi_sum": 0,
            "ofi_changes": 0,
        }
    for key, ofi in bucket_ofi.items():
        cell = cells.get(key)
        if cell is None:
            cells[key] = {
                "condition_id": key[0],
                "bucket_ts_ns": key[1],
                "n_trades": 0,
                "volume": 0.0,
                "buy_volume": 0.0,
                "sell_volume": 0.0,
                "trade_imbalance": None,
                "matched": {h: 0 for h in horizon_list},
                "markouts": {h: None for h in horizon_list},
                "matched_spreads": {h: None for h in horizon_list},
                "wide_quotes": 0,
                "ofi_sum": 0,
                "ofi_changes": 0,
            }
            cell = cells[key]
        cell["ofi_sum"] = ofi["ofi_sum"]
        cell["ofi_changes"] = ofi["ofi_changes"]

    for cell in cells.values():
        changes = int(cell["ofi_changes"])
        cell["ofi_per_change"] = (
            float(cell["ofi_sum"]) / changes if changes >= int(min_ofi_changes) else None
        )

    ofi_threshold, ofi_sample = ofi_tail_threshold(
        (c["ofi_per_change"] for c in cells.values()),
        q=ofi_quantile,
        min_buckets=MIN_OFI_BUCKETS,
    )

    records: List[Dict[str, Any]] = []
    for cell in cells.values():
        condition_id = cell["condition_id"]
        meta = spreads.get(condition_id, {})
        median_spread = meta.get("median_spread")
        verdict = classify_bucket(
            n_trades=cell["n_trades"],
            matched=cell["matched"],
            markouts=cell["markouts"],
            median_spread=median_spread,
            ofi_per_change=cell["ofi_per_change"],
            ofi_changes=cell["ofi_changes"],
            ofi_threshold=ofi_threshold,
            min_matched=min_matched,
            min_ofi_changes=min_ofi_changes,
        )
        record = {
            "condition_id": condition_id,
            "question": questions.get(condition_id),
            "bucket_start": datetime.fromtimestamp(cell["bucket_ts_ns"] / NS, timezone.utc),
            "bucket_ts_ns": int(cell["bucket_ts_ns"]),
            "n_trades": int(cell["n_trades"]),
            "volume": float(cell["volume"]),
            "buy_volume": float(cell["buy_volume"]),
            "sell_volume": float(cell["sell_volume"]),
            "trade_imbalance": cell["trade_imbalance"],
            "median_spread": median_spread,
            "half_spread": verdict["half_spread"],
            "ofi_sum": int(cell["ofi_sum"]),
            "ofi_changes": int(cell["ofi_changes"]),
            "ofi_per_change": cell["ofi_per_change"],
            "ofi_threshold": ofi_threshold,
            "status": verdict["status"],
            "flag_ofi": verdict["flag_ofi"],
            "flagged": verdict["flagged"],
            "flag_reason": verdict["flag_reason"],
            "wide_quotes": int(cell["wide_quotes"]),
        }
        for horizon in horizon_list:
            record[f"markout_{horizon}s"] = cell["markouts"][horizon]
            record[f"matched_{horizon}s"] = int(cell["matched"][horizon])
            record[f"flag_markout_{horizon}s"] = verdict["markout_flags"][f"markout_{horizon}s"]
            record[f"matched_spread_{horizon}s"] = cell["matched_spreads"][horizon]
        records.append(record)

    records.sort(key=lambda r: (r["bucket_start"], r["condition_id"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_parquet(records, out, horizon_list)

    text = _summary_text(
        records=records,
        start_ns=start_ns,
        end_ns=end_ns,
        parts=parts,
        quote_rows=quote_rows,
        trade_rows=trade_rows,
        ofi_rows=ofi_rows,
        ofi_threshold=ofi_threshold,
        ofi_sample=ofi_sample,
        spreads=spreads,
        bucket_s=bucket_s,
        horizons=horizon_list,
        match_window_s=match_window_s,
        min_matched=min_matched,
        min_ofi_changes=min_ofi_changes,
        ofi_quantile=ofi_quantile,
        out=out,
        top=top,
    )
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(text, encoding="utf-8")
    log.info(f"wrote {len(records):,} buckets to {out} and summary to {summary}")
    print(text)
    return {
        "records": len(records),
        "flagged": sum(1 for r in records if r["flagged"]),
        "ofi_threshold": ofi_threshold,
        "out": str(out),
        "summary": str(summary),
    }


def _write_parquet(
    records: Sequence[Dict[str, Any]], out: Path, horizons: Sequence[int] = HORIZONS
) -> None:
    if not records:
        # An empty result is still a result: write the schema so downstream
        # readers fail loudly on column names, not on a missing file.
        columns = {
            "condition_id": "string",
            "question": "string",
            "bucket_start": "datetime64[ns, UTC]",
            "bucket_ts_ns": "int64",
            "n_trades": "int64",
            "volume": "float64",
            "buy_volume": "float64",
            "sell_volume": "float64",
            "trade_imbalance": "float64",
            "median_spread": "float64",
            "half_spread": "float64",
            "ofi_sum": "int64",
            "ofi_changes": "int64",
            "ofi_per_change": "float64",
            "ofi_threshold": "float64",
            "status": "string",
            "flag_ofi": "bool",
            "flagged": "bool",
            "flag_reason": "string",
            "wide_quotes": "int64",
        }
        for horizon in horizons:
            columns[f"markout_{horizon}s"] = "float64"
            columns[f"matched_spread_{horizon}s"] = "float64"
            columns[f"matched_{horizon}s"] = "int64"
            columns[f"flag_markout_{horizon}s"] = "bool"
        import pandas as pd  # local import: only needed on the empty path

        pd.DataFrame({name: pd.Series(dtype=dtype) for name, dtype in columns.items()}).to_parquet(
            out, index=False
        )
        return
    import pandas as pd

    pd.DataFrame(list(records)).to_parquet(out, index=False)


def _fmt(value: Optional[float], width: int = 9, dp: int = 4) -> str:
    if value is None:
        return "n/a".rjust(width)
    return f"{value:.{dp}f}".rjust(width)


def _summary_text(
    *,
    records: Sequence[Dict[str, Any]],
    start_ns: int,
    end_ns: int,
    parts: Sequence[Tuple[str, str]],
    quote_rows: int,
    trade_rows: Sequence[Dict[str, Any]],
    ofi_rows: Sequence[Dict[str, Any]],
    ofi_threshold: Optional[float],
    ofi_sample: int,
    spreads: Dict[str, Dict[str, Any]],
    bucket_s: int,
    horizons: Sequence[int],
    match_window_s: int,
    min_matched: int,
    min_ofi_changes: int,
    ofi_quantile: float,
    out: Path,
    top: int,
) -> str:
    total = len(records)
    flagged = [r for r in records if r["flagged"]]
    status_counts: Dict[str, int] = {}
    for record in records:
        status_counts[record["status"]] = status_counts.get(record["status"], 0) + 1
    with_markout = sum(
        1 for r in records if any(r.get(f"markout_{h}s") is not None for h in horizons)
    )
    marked_ok = status_counts.get("ok", 0)
    conditions = {r["condition_id"] for r in records}
    traded_conditions = {r["condition_id"] for r in records if r["n_trades"] > 0}
    matched = {h: sum(int(r.get(f"matched_{h}s") or 0) for r in records) for h in horizons}
    n_trades = sum(int(r["n_trades"]) for r in records)
    wide_quotes = sum(int(r.get("wide_quotes") or 0) for r in records)
    trade_ts = [int(r["recv_ts_ns"]) for r in trade_rows if r.get("recv_ts_ns") is not None]
    span = (
        f"{datetime.fromtimestamp(min(trade_ts) / NS, timezone.utc):%Y-%m-%d %H:%M:%S}"
        f" -> {datetime.fromtimestamp(max(trade_ts) / NS, timezone.utc):%H:%M:%S} UTC"
        if trade_ts
        else "no trades in window"
    )
    start_dt = datetime.fromtimestamp(start_ns / NS, timezone.utc)
    end_dt = datetime.fromtimestamp(end_ns / NS, timezone.utc)
    window_s = max(1.0, (end_ns - start_ns) / NS)
    buckets_in_window = int(window_s // bucket_s)

    lines = [
        "toxicity scan",
        "=============",
        f"window            : {start_dt:%Y-%m-%d %H:%M} -> {end_dt:%Y-%m-%d %H:%M} UTC "
        f"({window_s / 3600:.2f} h, buckets of {bucket_s}s)",
        f"tape partitions   : {', '.join(f'{d}T{h}' for d, h in parts)}",
        f"trade coverage    : {len(trade_rows):,} trades from "
        f"{len(traded_conditions):,} conditions; {span}",
        f"quote coverage    : {quote_rows:,} reconstructed top-of-book points; "
        f"{len(spreads):,} conditions with a positive spread; "
        f"{len(ofi_rows):,} top-of-book changes",
        f"conditions in out : {len(conditions):,}",
        f"buckets           : {total:,} condition-buckets emitted out of "
        f"{len(conditions) * buckets_in_window:,} possible "
        f"({100.0 * total / (len(conditions) * buckets_in_window) if conditions else 0:.1f}% of "
        f"condition x bucket slots)",
        f"status            : "
        + (", ".join(f"{k}={v:,}" for k, v in sorted(status_counts.items())) or "none"),
        f"wide-quote drops  : {wide_quotes:,} horizon matches dropped by the "
        f"{MAX_QUOTE_SPREAD_MULTIPLE:g}x guard",
        f"matched markouts  : "
        + ", ".join(
            f"{h}s {matched[h]:,}/{n_trades:,} ({100.0 * matched[h] / n_trades if n_trades else 0:.1f}%)"
            for h in horizons
        ),
        "",
        "thresholds",
        "----------",
        f"markout           : markout(h) > 0.5 * median_spread(condition) in the adverse "
        f"(taker-profit) direction, at h in {sorted(horizons)}; "
        f"min {min_matched} matched trades per bucket. "
        f"Median spread is the window median of the reconstructed top of book.",
        f"ofi               : |mean OFI step per top-of-book change| > pooled "
        f"p{int(round(ofi_quantile * 100))} of |mean step| "
        + (
            f"= {ofi_threshold:.4f} (computed on {ofi_sample:,} buckets with >= "
            f"{min_ofi_changes} changes)"
            if ofi_threshold is not None
            else f"= n/a (only {ofi_sample:,} buckets with >= {min_ofi_changes} changes; "
            f"need >= {MIN_OFI_BUCKETS}) -> no bucket can be OFI-flagged"
        ),
        f"match window      : a quote must land within {match_window_s}s of t+h; "
        f"otherwise the trade is unmatched, not zero",
        f"markout spread    : mk over a market whose matched book was itself wide "
        f"is a wide-book artefact, not drift -- compare mk against mk_spr",
        f"wide-quote guard  : a matched quote wider than {MAX_QUOTE_SPREAD_MULTIPLE:g}x the "
        f"condition's median spread is dropped as a one-sided book "
        f"({wide_quotes:,} horizon matches dropped)",
        "",
        "result",
        "------",
        f"flagged           : {len(flagged):,}/{total:,} buckets "
        f"({100.0 * len(flagged) / total if total else 0:.1f}%), "
        f"{marked_ok:,} buckets cleared the min-matched bar; "
        f"{with_markout:,} buckets carry at least one markout value",
    ]
    lines += [
        f"{f'  markout {horizon}s':<18}: {sum(1 for r in records if r.get(f'flag_markout_{horizon}s')):,}"
        for horizon in horizons
    ]
    lines += [
        f"  ofi tail        : {sum(1 for r in records if r['flag_ofi']):,}",
        f"output            : {out}",
        "",
        f"top {top} flagged buckets (markout flags first, then {horizons[0]}s markout)",
        "-" * 44,
        f"{'bucket':<17} {'condition':<14} {'trades':>6} {'volume':>10} "
        f"{'mk' + str(horizons[0]) + 's':>9} {'mk' + str(horizons[-1]) + 's':>9} "
        f"{'half_spr':>9} {'mk_spr':>9} {'ofi':>8} {'imb':>7} reason",
    ]

    def _rank(record: Dict[str, Any]) -> Tuple[int, float, Any]:
        has_markout = any(record.get(f"flag_markout_{h}s") for h in horizons)
        best = record.get(f"markout_{horizons[0]}s")
        # Markout-flagged buckets first: an OFI-only flag on 1 trade is book
        # pressure, not evidence about where the price went.
        return (0 if has_markout else 1, -(best if best is not None else -9.0), record["bucket_start"])

    ranked = sorted(flagged, key=_rank)
    for record in ranked[:top]:
        lines.append(
            f"{record['bucket_start']:%Y-%m-%d %H:%M}   "
            f"{record['condition_id'][:14]:<14} {record['n_trades']:>6,} "
            f"{record['volume']:>10,.0f} "
            f"{_fmt(record.get(f'markout_{horizons[0]}s'))} "
            f"{_fmt(record.get(f'markout_{horizons[-1]}s'))} "
            f"{_fmt(record.get('half_spread'))} "
            f"{_fmt(record.get(f'matched_spread_{horizons[0]}s'))} "
            f"{_fmt(record.get('ofi_per_change'), 8, 3)} "
            f"{_fmt(record.get('trade_imbalance'), 7, 3)} {record['flag_reason']}"
        )
    if not flagged:
        lines.append("(none)")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=2.0, help="window length in hours (default 2)")
    ap.add_argument("--end", default=None, help="window end, ISO UTC (default: now)")
    ap.add_argument("--bucket-seconds", type=int, default=BUCKET_S)
    ap.add_argument("--horizons", default=",".join(str(h) for h in HORIZONS), help="comma seconds")
    ap.add_argument("--match-window-seconds", type=int, default=MATCH_WINDOW_S)
    ap.add_argument("--min-matched", type=int, default=MIN_MATCHED)
    ap.add_argument("--min-ofi-changes", type=int, default=MIN_OFI_CHANGES)
    ap.add_argument("--ofi-quantile", type=float, default=OFI_TAIL_QUANTILE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--summary", default=DEFAULT_SUMMARY)
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args(argv)

    end = None
    if args.end:
        end = datetime.fromisoformat(args.end)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
    horizons = tuple(int(x) for x in str(args.horizons).split(",") if x.strip())
    scan(
        Path(DEFAULT_TAPE),
        end=end,
        hours=args.hours,
        bucket_s=args.bucket_seconds,
        horizons=horizons,
        match_window_s=args.match_window_seconds,
        min_matched=args.min_matched,
        min_ofi_changes=args.min_ofi_changes,
        ofi_quantile=args.ofi_quantile,
        out=Path(args.out),
        summary=Path(args.summary),
        manifest=Path(args.manifest),
        top=args.top,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
