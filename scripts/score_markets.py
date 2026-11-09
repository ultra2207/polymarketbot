#!/usr/bin/env python3
"""Per-market scorecard: measured maker edge vs reward pool, for capital allocation.

The paper engine ranks markets by reward pool. That is one half of the
decision: a market with a big pool and a spread narrower than its adverse
selection loses money to better-informed takers. This script builds the other
half from the tape, one row per condition, so allocation can be driven by
measured numbers instead of assumptions.

What is measured (from the tape)
--------------------------------
* ``spread_median`` / ``half_spread_median`` -- reconstructed top of book.
  ``price_change`` frames are unnested (each nested change carries the token's
  ``best_bid``/``best_ask``) and ``best_bid_ask`` events are unioned in. The
  spread is quoted per asset; ``half_spread`` is spread / 2, the per-share
  income of a maker who earns the half-spread.
* ``trades`` / ``notional_usd`` / ``shares`` / ``trade_price_median`` -- the
  tape's ``clob_trades`` rows.
* ``markout_5m`` -- size-weighted signed mid move 300 s after each trade,
  matched by ASOF join to the *first* quote at or after t+300 s and only if
  that quote arrives within 60 s of the horizon. The sign is the taker's:
  **positive = taker profit = maker adverse selection** (the maker's per-share
  income is reduced by it). ``markout_5m_median`` is the unweighted median of
  the same per-trade values, so one huge print cannot carry a market.
* ``fee_per_share`` -- taker fee implied by the market's Gamma ``feeType``
  (``docs/fees.md``: C x rate x (p(1-p))^exponent), summed over the window's
  trades and divided by the shares that had a known schedule. Makers pay 0;
  this is the taker cost the maker's quote has to clear.
* ``merge_gas_pol`` -- one CTF ``mergePositions`` (78,000 gas) at the window's
  median Polygon ``base_fee_gwei``. USD only when ``--pol-usd`` is passed,
  because a POL/USD rate is an assumption this script refuses to invent.

What is a proxy (stated, not measured)
--------------------------------------
* ``daily_pool`` / ``min_size`` / ``max_spread`` -- from
  ``data/tape/manifest_current.json``. Polymarket publishes the pool; we do not
  measure what share of it a given quote actually earns.
* ``reward_per_capital = daily_pool / max(min_size * price, 1)`` -- reward USD
  per USD of capital per day **assuming the whole pool is earned by this one
  quote**. The pool is shared by every qualifying LP, so this is an upper
  bound and a ranking key, not an expected return.

Scoring
-------
    edge_per_share = half_spread - markout - fee_per_share
    gate1          = edge_per_share > 0        (None when any input is n/a)

Usage
    .venv/Scripts/python.exe scripts/score_markets.py
    .venv/Scripts/python.exe scripts/score_markets.py --date 2026-11-05 --hour-from 02 --hour-to 03
    .venv/Scripts/python.exe scripts/score_markets.py --top 20 --pol-usd 0.42
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import duckdb  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from polymarketbot.fees import FeeSchedule, schedule_for_fee_type  # noqa: E402
from polymarketbot.logs import get_logger  # noqa: E402

log = get_logger("score_markets")

NS = 1_000_000_000
DEFAULT_TAPE = Path("data/tape")
DEFAULT_SNAPSHOT = Path("data/raw/gamma/snapshot_2026-11-01.parquet")
DEFAULT_MANIFEST = Path("data/tape/manifest_current.json")
DEFAULT_OUT = Path("data/derived/market_scorecard.parquet")
DEFAULT_SUMMARY = Path("data/derived/market_scorecard_summary.txt")

MERGE_GAS_UNITS = 78_000  # CTF mergePositions, trading/ctf.py estimate_merge_cost_usd
MARKOUT_HORIZON_S = 300
MARKOUT_WINDOW_NS = 60 * NS  # a quote further than this past the horizon is not "at" it
MIN_MARKOUTS = 30  # below this a condition's markout is flagged low-sample
DEFAULT_WINDOW_HOURS = 2

MEASURED_COLUMNS = (
    "quote_points", "n_assets", "spread_median", "half_spread_median",
    "spread_p25", "spread_p75", "mid_median", "trades", "notional_usd",
    "shares", "trade_price_median", "markout_5m", "markout_5m_median",
    "markout_trades", "markout_matched", "fee_type", "fee_rate",
    "fee_exponent", "fee_per_share", "fee_usd", "fee_shares_covered",
    "merge_gas_pol",
)
DERIVED_COLUMNS = ("edge_per_share", "gate1", "rank_edge", "rank_pool")
PROXY_COLUMNS = (
    "daily_pool", "min_size", "max_spread", "reward_per_capital",
    "merge_gas_usd",
)


# ------------------------------------------------------------------ pure math
def half_spread(spread: Optional[float]) -> Optional[float]:
    """Half the quoted spread: a maker's per-share income if it is filled."""
    if spread is None:
        return None
    return spread / 2.0


def signed_markout(side: str, price: float, mid: Optional[float]) -> Optional[float]:
    """Per-share P&L of the taker who crossed, from the mid move.

    Positive means the taker made money, which is exactly the maker's adverse
    selection. An unknown side is missing data, not a zero.
    """
    if price is None or mid is None:
        return None
    s = str(side or "").upper()
    if s == "BUY":
        return mid - price
    if s == "SELL":
        return price - mid
    return None


def weighted_markout(
    fills: Iterable[Tuple[str, float, Optional[float], float]],
) -> Tuple[Optional[float], int]:
    """Size-weighted mean signed markout plus the count of matched fills."""
    num = 0.0
    den = 0.0
    n = 0
    for side, price, mid, size in fills:
        m = signed_markout(side, price, mid)
        if m is None or size is None or size <= 0:
            continue
        num += m * size
        den += size
        n += 1
    return ((num / den) if den > 0 else None), n


def fee_per_share(price: Optional[float], schedule: Optional[FeeSchedule]) -> Optional[float]:
    """Taker fee per share: rate x (p(1-p))^exponent. Makers pay nothing."""
    if price is None or schedule is None:
        return None
    p = float(price)
    if not 0.0 <= p <= 1.0:
        return None
    return float(schedule.rate) * ((p * (1.0 - p)) ** float(schedule.exponent))


def edge_per_share(
    half_spread_value: Optional[float],
    markout_value: Optional[float],
    fee_value: Optional[float],
) -> Optional[float]:
    """Gate 1's left-hand side: what the maker keeps per share.

    ``half_spread - markout - fee_per_share``. Any missing input makes the
    edge ``None`` rather than a guess.
    """
    if half_spread_value is None or markout_value is None or fee_value is None:
        return None
    return float(half_spread_value) - float(markout_value) - float(fee_value)


def gate1_pass(edge_value: Optional[float]) -> Optional[bool]:
    """Gate 1: a maker quoting the half-spread clears its costs."""
    return None if edge_value is None else bool(edge_value > 0.0)


def reward_per_capital(
    daily_pool: Optional[float],
    min_size: Optional[float],
    price: Optional[float],
) -> Optional[float]:
    """Reward USD per USD of capital per day (proxy; see module docstring).

    ``daily_pool / max(min_size * price, 1)``. The floor of $1 keeps a
    near-zero-price market from producing an infinite ratio.
    """
    if daily_pool is None or min_size is None or price is None:
        return None
    capital = max(float(min_size) * float(price), 1.0)
    return float(daily_pool) / capital


def merge_gas_pol(gwei: Optional[float], gas_units: int = MERGE_GAS_UNITS) -> Optional[float]:
    """POL burned by one merge: gas_units x gwei x 1e-9."""
    if gwei is None:
        return None
    return float(gas_units) * float(gwei) * 1e-9


def merge_gas_usd(gwei: Optional[float], pol_usd: Optional[float]) -> Optional[float]:
    """Merge gas in USD; requires an explicit POL/USD rate (no network here)."""
    if gwei is None or pol_usd is None:
        return None
    return round(merge_gas_pol(gwei) * float(pol_usd), 6)


def horizon_label(seconds: int) -> str:
    """``300`` -> ``5m`` so the parquet column says what it measures."""
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


# ------------------------------------------------------------------- windowing
def window_bounds(
    date: Optional[str],
    hour_from: Optional[int],
    hour_to: Optional[int],
) -> Optional[Tuple[int, int]]:
    """Half-open ``[start_ns, end_ns)`` for an explicit ``--date`` window.

    ``--date`` alone means the whole UTC day. One hour bound given means a
    two-hour window ending (or starting) there, matching the default length.
    """
    if date is None:
        return None
    base = datetime.strptime(str(date), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if hour_from is None and hour_to is None:
        hf, ht = 0, 23
    elif hour_to is None:
        hf, ht = int(hour_from), int(hour_from) + 1
    elif hour_from is None:
        ht = int(hour_to)
        hf = max(0, ht - 1)
    else:
        hf, ht = int(hour_from), int(hour_to)
    if ht < hf:
        raise ValueError(f"hour_to ({ht}) is before hour_from ({hf})")
    start = base + timedelta(hours=hf)
    end = base + timedelta(hours=ht + 1)
    return int(start.timestamp()) * NS, int(end.timestamp()) * NS


def last_window(con: duckdb.DuckDBPyConnection, tape: Path, hours: int = DEFAULT_WINDOW_HOURS) -> Tuple[int, int]:
    """The last ``hours`` whole UTC hours present in the tape.

    Anchored on the hour of the newest ``clob_events`` row, so a partial hour
    still counts as the last complete reporting hour. Returns ns bounds.
    """
    row = con.execute(
        f"SELECT MAX(recv_ts_ns) FROM read_parquet('{_glob(tape, 'clob_events')}')"
    ).fetchone()
    if not row or row[0] is None:
        raise RuntimeError(f"no clob_events rows under {tape}")
    stamp = datetime.fromtimestamp(int(row[0]) // NS, timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    end = stamp + timedelta(hours=1)
    start = end - timedelta(hours=int(hours))
    return int(start.timestamp()) * NS, int(end.timestamp()) * NS


def partition_predicate(start_ns: int, end_ns: int, alias: Optional[str] = None) -> str:
    """Hive predicate for the hours the window touches, so files are pruned.

    ``hour`` is a zero-padded VARCHAR in the tape. A window wider than three
    days skips the predicate and relies on the timestamp bound alone.
    """
    cur = datetime.fromtimestamp(start_ns // NS, timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    end = datetime.fromtimestamp(end_ns // NS, timezone.utc)
    pairs: List[Tuple[str, str]] = []
    while cur < end:
        pairs.append((cur.strftime("%Y-%m-%d"), f"{cur.hour:02d}"))
        cur += timedelta(hours=1)
    if not pairs or len(pairs) > 72:
        return ""
    p = f"{alias}." if alias else ""
    return "(" + " OR ".join(f"({p}date = '{d}' AND {p}hour = '{h}')" for d, h in pairs) + ")"


def window_predicate(start_ns: int, end_ns: int, alias: Optional[str] = None) -> str:
    """Timestamp bound plus the partition predicate, as one SQL expression."""
    p = f"{alias}." if alias else ""
    ts = f"{p}recv_ts_ns >= {int(start_ns)} AND {p}recv_ts_ns < {int(end_ns)}"
    part = partition_predicate(start_ns, end_ns, alias)
    return f"{ts} AND {part}" if part else ts


def _glob(tape: Path, table: str) -> str:
    return Path(tape, table).as_posix() + "/**/*.parquet"


# ------------------------------------------------------------------ SQL layers
def load_fee_table(
    con: duckdb.DuckDBPyConnection,
    snapshot: Optional[Path],
    when: datetime,
    metadata: Optional[Path] = Path("data/derived/universe_metadata.parquet"),
) -> Tuple[int, List[str]]:
    """TEMP TABLE ``fees``: condition -> taker rate/exponent from ``feeType``.

    The 2026-11-01 snapshot is the historical baseline; the live-universe
    metadata refresh overrides it for markets created later (the snapshot
    misses most of the current universe). Zero-rate schedules are kept:
    ``feeType = zero_fees`` means the fee is a measured 0, not an unknown.
    Returns the row count and the fee types this build does not recognise.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE fees ("
        "condition_id VARCHAR, fee_type VARCHAR, rate DOUBLE, exponent DOUBLE)"
    )
    sources: list[Path] = []
    if snapshot is not None and Path(snapshot).exists():
        sources.append(Path(snapshot))
    elif snapshot is not None:
        log.warning(f"fee snapshot missing ({snapshot}); using live metadata only")
    meta_path = Path(metadata).resolve() if metadata is not None and Path(metadata).exists() else None
    if meta_path is not None:
        sources.append(Path(metadata))
    if not sources:
        log.warning("no fee source available; fee_per_share will be n/a")
        return 0, []
    out: Dict[str, Tuple[str, float, float]] = {}
    unknown = set()
    for path in sources:
        if meta_path is not None and path.resolve() == meta_path:
            rows = con.execute(
                f"SELECT condition_id, fee_type FROM read_parquet('{path.as_posix()}') "
                "WHERE fee_type IS NOT NULL"
            ).fetchall()
        else:
            rows = con.execute(
                f"SELECT conditionId, feeType FROM read_parquet('{path.as_posix()}') "
                "WHERE feeType IS NOT NULL"
            ).fetchall()
        for condition_id, fee_type in rows:
            sched = schedule_for_fee_type(str(fee_type), when)
            if sched is None:
                unknown.add(str(fee_type))
                continue
            out[str(condition_id).lower()] = (
                str(fee_type), float(sched.rate), float(sched.exponent)
            )
    if out:
        con.executemany(
            "INSERT INTO fees VALUES (?, ?, ?, ?)",
            [(cid, ft, rate, exp) for cid, (ft, rate, exp) in out.items()],
        )
    return len(out), sorted(unknown)


def load_asset_map(
    con: duckdb.DuckDBPyConnection, tape: Path, start_ns: int, end_ns: int
) -> int:
    """TEMP TABLE ``amap``: asset_id -> condition_id for the window.

    A ``price_change`` frame that moves both tokens carries a NULL row-level
    ``asset_id``, so the mapping is also harvested from the nested changes of
    frames whose ``condition_id`` is known. Without that, half the quotes of
    two-token markets would be unattributable.
    """
    events = _glob(tape, "clob_events")
    pred = window_predicate(start_ns, end_ns)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE amap AS
        WITH row_pairs AS (
            SELECT asset_id, condition_id
            FROM read_parquet('{events}')
            WHERE {pred}
              AND asset_id IS NOT NULL AND condition_id IS NOT NULL
        ),
        nested AS (
            SELECT c.asset_id AS asset_id, condition_id
            FROM (
                SELECT condition_id, UNNEST(price_changes) AS c
                FROM read_parquet('{events}')
                WHERE {pred} AND event_type = 'price_change'
                  AND condition_id IS NOT NULL
            )
            WHERE c.asset_id IS NOT NULL
        )
        SELECT asset_id, ANY_VALUE(condition_id) AS condition_id
        FROM (SELECT * FROM row_pairs UNION ALL SELECT * FROM nested)
        GROUP BY 1
        """
    )
    return int(con.execute("SELECT COUNT(*) FROM amap").fetchone()[0])


def build_quotes(
    con: duckdb.DuckDBPyConnection, tape: Path, start_ns: int, end_ns: int
) -> int:
    """TEMP TABLE ``quotes``: one row per quoted top-of-book observation.

    ``price_change`` is the authoritative L2 stream; ``best_bid_ask`` is unioned
    in as the fallback for markets that only emit the BBO event. Only assets
    with a known condition are kept, which also drops the book-only frames.
    """
    events = _glob(tape, "clob_events")
    pred = window_predicate(start_ns, end_ns)
    pred_e = window_predicate(start_ns, end_ns, "e")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE quotes AS
        WITH pc AS (
            SELECT recv_ts_ns, UNNEST(price_changes) AS c
            FROM read_parquet('{events}')
            WHERE {pred} AND event_type = 'price_change'
        )
        SELECT a.condition_id AS condition_id,
               pc.c.asset_id AS asset_id,
               pc.recv_ts_ns AS recv_ts_ns,
               (pc.c.best_bid + pc.c.best_ask) / 2.0 AS mid,
               (pc.c.best_ask - pc.c.best_bid) AS spread
        FROM pc JOIN amap a ON a.asset_id = pc.c.asset_id
        WHERE pc.c.best_bid IS NOT NULL AND pc.c.best_ask IS NOT NULL
        UNION ALL
        SELECT a.condition_id, e.asset_id, e.recv_ts_ns,
               (e.best_bid + e.best_ask) / 2.0,
               (e.best_ask - e.best_bid)
        FROM read_parquet('{events}') e JOIN amap a ON a.asset_id = e.asset_id
        WHERE {pred_e} AND e.event_type = 'best_bid_ask'
          AND e.best_bid IS NOT NULL AND e.best_ask IS NOT NULL
        """
    )
    return int(con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0])


def spread_stats(con: duckdb.DuckDBPyConnection) -> Dict[str, Dict[str, Any]]:
    """Per-condition spread distribution from the reconstructed book."""
    sql = """
    SELECT condition_id, COUNT(*) AS n, COUNT(DISTINCT asset_id) AS assets,
           MEDIAN(spread) AS med, QUANTILE_CONT(spread, 0.25) AS p25,
           QUANTILE_CONT(spread, 0.75) AS p75, MEDIAN(mid) AS mid
    FROM quotes
    WHERE condition_id IS NOT NULL
    GROUP BY 1
    """
    out: Dict[str, Dict[str, Any]] = {}
    for cid, n, assets, med, p25, p75, mid in con.execute(sql).fetchall():
        out[str(cid).lower()] = {
            "quote_points": int(n),
            "n_assets": int(assets),
            "spread_median": float(med) if med is not None else None,
            "spread_p25": float(p25) if p25 is not None else None,
            "spread_p75": float(p75) if p75 is not None else None,
            "mid_median": float(mid) if mid is not None else None,
        }
    return out


def trade_aggregates(
    con: duckdb.DuckDBPyConnection, tape: Path, start_ns: int, end_ns: int
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    """Per-condition trade count, notional, shares and taker fee.

    The fee is summed from the window's own fills (``size * rate * curve^exp``)
    and divided later by the shares that had a known schedule, so a market with
    a missing ``feeType`` reports an n/a fee instead of a zero.
    """
    trades = _glob(tape, "clob_trades")
    pred = window_predicate(start_ns, end_ns, "t")
    base = (
        "t.condition_id IS NOT NULL AND t.asset_id IS NOT NULL "
        "AND t.price IS NOT NULL AND t.size IS NOT NULL"
    )
    scanned = int(
        con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{trades}') t WHERE {pred} AND {base}"
        ).fetchone()[0]
    )
    sql = f"""
    SELECT t.condition_id,
           COUNT(*) AS n,
           SUM(t.price * t.size) AS notional,
           SUM(t.size) AS shares,
           MEDIAN(t.price) AS med_price,
           SUM(CASE WHEN f.rate IS NULL THEN 0
                    ELSE t.size * f.rate * POW(t.price * (1 - t.price), f.exponent) END) AS fee_usd,
           SUM(CASE WHEN f.rate IS NOT NULL THEN t.size END) AS fee_shares,
           ANY_VALUE(f.fee_type) AS fee_type,
           ANY_VALUE(f.rate) AS fee_rate,
           ANY_VALUE(f.exponent) AS fee_exponent
    FROM read_parquet('{trades}') t
    LEFT JOIN fees f ON f.condition_id = t.condition_id
    WHERE {pred} AND {base}
    GROUP BY 1
    """
    out: Dict[str, Dict[str, Any]] = {}
    for (cid, n, notional, shares, med_price, fee_usd, fee_shares,
         fee_type, fee_rate, fee_exponent) in con.execute(sql).fetchall():
        covered = float(fee_shares or 0.0)
        out[str(cid).lower()] = {
            "trades": int(n),
            "notional_usd": float(notional or 0.0),
            "shares": float(shares or 0.0),
            "trade_price_median": float(med_price) if med_price is not None else None,
            "fee_usd": float(fee_usd or 0.0),
            "fee_shares_covered": covered,
            "fee_per_share": (float(fee_usd) / covered) if covered > 0 else None,
            "fee_type": str(fee_type) if fee_type is not None else None,
            "fee_rate": float(fee_rate) if fee_rate is not None else None,
            "fee_exponent": float(fee_exponent) if fee_exponent is not None else None,
        }
    return out, {"trade_rows": scanned}


def markout(
    con: duckdb.DuckDBPyConnection, tape: Path, start_ns: int, end_ns: int, horizon_s: int
) -> Dict[str, Dict[str, Any]]:
    """Per-condition markout at ``horizon_s`` (positive = taker profit).

    ASOF join to the first quote at or after t+h; a quote that arrives more
    than 60 s after the horizon is discarded so a quiet market cannot borrow a
    price from an hour later.
    """
    h_ns = int(horizon_s) * NS
    trades = _glob(tape, "clob_trades")
    pred = window_predicate(start_ns, end_ns)
    sql = f"""
    WITH t AS (
        SELECT condition_id, asset_id, recv_ts_ns, price, size, side
        FROM read_parquet('{trades}')
        WHERE {pred}
          AND condition_id IS NOT NULL AND asset_id IS NOT NULL
          AND price IS NOT NULL AND size IS NOT NULL AND side IS NOT NULL
    ),
    j AS (
        SELECT t.condition_id, t.price, t.size, t.recv_ts_ns AS tts,
               q.mid AS mid, q.recv_ts_ns AS qts,
               (q.mid IS NOT NULL
                AND q.recv_ts_ns < t.recv_ts_ns + {h_ns + MARKOUT_WINDOW_NS}) AS ok,
               CASE WHEN t.side = 'BUY' THEN q.mid - t.price
                    ELSE t.price - q.mid END AS sgn
        FROM t ASOF LEFT JOIN quotes q
          ON t.asset_id = q.asset_id
         AND q.recv_ts_ns >= t.recv_ts_ns + {h_ns}
    )
    SELECT condition_id,
           COUNT(*) AS n,
           SUM(CASE WHEN ok THEN 1 ELSE 0 END) AS matched,
           SUM(CASE WHEN ok THEN sgn * size END)
               / NULLIF(SUM(CASE WHEN ok THEN size END), 0) AS w_markout,
           MEDIAN(CASE WHEN ok THEN sgn END) AS med_markout
    FROM j
    GROUP BY 1
    """
    out: Dict[str, Dict[str, Any]] = {}
    for cid, n, matched, w_markout, med_markout in con.execute(sql).fetchall():
        out[str(cid).lower()] = {
            "markout_trades": int(n),
            "markout_matched": int(matched or 0),
            "markout": float(w_markout) if matched else None,
            "markout_median": float(med_markout) if matched else None,
        }
    return out


def gas_stats(
    con: duckdb.DuckDBPyConnection, tape: Path, start_ns: int, end_ns: int
) -> Dict[str, Any]:
    """Median Polygon base fee in the window, and the merge cost it implies."""
    pred = window_predicate(start_ns, end_ns)
    row = con.execute(
        f"""
        SELECT COUNT(*), AVG(base_fee_gwei), MEDIAN(base_fee_gwei),
               MIN(base_fee_gwei), MAX(base_fee_gwei), SUM(tx_count)
        FROM read_parquet('{_glob(tape, 'polygon_gas')}')
        WHERE {pred}
        """
    ).fetchone()
    blocks, avg_gwei, med_gwei, lo_gwei, hi_gwei, txs = row
    med = float(med_gwei) if med_gwei is not None else None
    return {
        "blocks": int(blocks or 0),
        "median_gwei": med,
        "avg_gwei": float(avg_gwei) if avg_gwei is not None else None,
        "min_gwei": float(lo_gwei) if lo_gwei is not None else None,
        "max_gwei": float(hi_gwei) if hi_gwei is not None else None,
        "txs": int(txs or 0),
        "gas_pol": merge_gas_pol(med),
    }


# ------------------------------------------------------------------ assembly
def load_manifest(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    """condition -> reward parameters from the manifest snapshot."""
    if path is None or not Path(path).exists():
        log.warning(f"manifest missing ({path}); reward columns will be n/a")
        return {}
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    out: Dict[str, Dict[str, Any]] = {}
    for m in doc.get("markets") or []:
        cid = str(m.get("condition_id") or "").lower()
        if not cid:
            continue
        out[cid] = {
            "question": m.get("question"),
            "daily_pool": _as_float(m.get("daily_pool")),
            "min_size": _as_float(m.get("min_size")),
            "max_spread": _as_float(m.get("max_spread")),
        }
    return out


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def score_rows(
    spreads: Dict[str, Dict[str, Any]],
    trades: Dict[str, Dict[str, Any]],
    markouts: Dict[str, Dict[str, Any]],
    manifest: Dict[str, Dict[str, Any]],
    *,
    gas_pol: Optional[float],
    gas_usd: Optional[float],
    min_markouts: int = MIN_MARKOUTS,
) -> List[Dict[str, Any]]:
    """One row per condition: measured metrics, then the Gate 1 verdict.

    The condition set is the union of quoted, traded and reward-eligible
    markets -- a market with a pool but no quotes in the window is still a
    row, with its measurements left n/a.
    """
    rows: List[Dict[str, Any]] = []
    for cid in sorted(set(spreads) | set(trades) | set(manifest)):
        s = spreads.get(cid, {})
        t = trades.get(cid, {})
        m = markouts.get(cid, {})
        man = manifest.get(cid, {})

        med_spread = s.get("spread_median")
        half = half_spread(med_spread)
        mk = m.get("markout")
        fee = t.get("fee_per_share")
        matched = m.get("markout_matched")

        price = t.get("trade_price_median")
        if price is None:
            price = s.get("mid_median")
        edge = edge_per_share(half, mk, fee)

        rows.append({
            "condition_id": cid,
            "question": man.get("question"),
            "in_manifest": bool(man),
            "daily_pool": man.get("daily_pool"),
            "min_size": man.get("min_size"),
            "max_spread": man.get("max_spread"),
            "quote_points": s.get("quote_points", 0),
            "n_assets": s.get("n_assets", 0),
            "spread_median": med_spread,
            "half_spread_median": half,
            "spread_p25": s.get("spread_p25"),
            "spread_p75": s.get("spread_p75"),
            "mid_median": s.get("mid_median"),
            "trades": t.get("trades", 0),
            "notional_usd": t.get("notional_usd"),
            "shares": t.get("shares"),
            "trade_price_median": t.get("trade_price_median"),
            "markout_5m": mk,
            "markout_5m_median": m.get("markout_median"),
            "markout_trades": m.get("markout_trades", 0),
            "markout_matched": matched,
            "markout_low_sample": (None if matched is None else bool(matched < min_markouts)),
            "fee_type": t.get("fee_type"),
            "fee_known": t.get("fee_type") is not None,
            "fee_rate": t.get("fee_rate"),
            "fee_exponent": t.get("fee_exponent"),
            "fee_per_share": fee,
            "fee_usd": t.get("fee_usd"),
            "fee_shares_covered": t.get("fee_shares_covered"),
            "edge_per_share": edge,
            "gate1": gate1_pass(edge),
            "merge_gas_pol": gas_pol,
            "merge_gas_usd": gas_usd,
            "reward_per_capital": reward_per_capital(
                man.get("daily_pool"), man.get("min_size"), price
            ),
            "rank_edge": None,
            "rank_pool": None,
        })
    _rank(rows, "edge_per_share", "rank_edge")
    _rank(rows, "daily_pool", "rank_pool")
    return rows


def _rank(rows: List[Dict[str, Any]], key: str, out_key: str) -> None:
    """Dense rank of the non-null rows by ``key`` descending."""
    scores = [r[key] for r in rows if r.get(key) is not None]
    if not scores:
        return
    ordered = sorted(set(scores), reverse=True)
    rank_of = {v: i + 1 for i, v in enumerate(ordered)}
    for r in rows:
        v = r.get(key)
        r[out_key] = rank_of.get(v) if v is not None else None


def collect(
    con: duckdb.DuckDBPyConnection,
    tape: Path,
    start_ns: int,
    end_ns: int,
    *,
    snapshot: Optional[Path],
    manifest: Dict[str, Dict[str, Any]],
    horizon_s: int = MARKOUT_HORIZON_S,
    pol_usd: Optional[float] = None,
    min_markouts: int = MIN_MARKOUTS,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run every window pass once and score the conditions."""
    when = datetime.fromtimestamp(start_ns // NS, timezone.utc)
    fee_rows, unknown_fee_types = load_fee_table(con, snapshot, when)
    assets = load_asset_map(con, tape, start_ns, end_ns)
    quote_points = build_quotes(con, tape, start_ns, end_ns)
    spreads = spread_stats(con)
    trades, tstats = trade_aggregates(con, tape, start_ns, end_ns)
    markouts = markout(con, tape, start_ns, end_ns, horizon_s)
    gas = gas_stats(con, tape, start_ns, end_ns)

    gas_pol = gas["gas_pol"]
    gas_usd = merge_gas_usd(gas["median_gwei"], pol_usd)
    gas["gas_usd"] = gas_usd
    rows = score_rows(
        spreads, trades, markouts, manifest,
        gas_pol=gas_pol, gas_usd=gas_usd, min_markouts=min_markouts,
    )
    stats = {
        "start_ns": start_ns,
        "end_ns": end_ns,
        "assets": assets,
        "quote_points": quote_points,
        "trade_rows": tstats["trade_rows"],
        "conditions": len(rows),
        "conditions_traded": len(trades),
        "conditions_quoted": len(spreads),
        "conditions_in_manifest": len(manifest),
        "conditions_scored": sum(1 for r in rows if r["edge_per_share"] is not None),
        "conditions_gate1": sum(1 for r in rows if r["gate1"] is True),
        "conditions_gate1_low_sample": sum(
            1 for r in rows if r["gate1"] is True and r["markout_low_sample"]
        ),
        "conditions_fee_known": sum(1 for r in trades.values() if r["fee_per_share"] is not None),
        "markout_matched": sum(m["markout_matched"] for m in markouts.values()),
        "markout_trades": sum(m["markout_trades"] for m in markouts.values()),
        "fee_rows": fee_rows,
        "unknown_fee_types": unknown_fee_types,
        "gas": gas,
        "min_markouts": min_markouts,
        "horizon_s": horizon_s,
        "pol_usd": pol_usd,
    }
    return rows, stats


# -------------------------------------------------------------------- output
def parquet_schema() -> pa.Schema:
    return pa.schema([
        ("condition_id", pa.string()),
        ("question", pa.string()),
        ("in_manifest", pa.bool_()),
        ("daily_pool", pa.float64()),
        ("min_size", pa.float64()),
        ("max_spread", pa.float64()),
        ("quote_points", pa.int64()),
        ("n_assets", pa.int64()),
        ("spread_median", pa.float64()),
        ("half_spread_median", pa.float64()),
        ("spread_p25", pa.float64()),
        ("spread_p75", pa.float64()),
        ("mid_median", pa.float64()),
        ("trades", pa.int64()),
        ("notional_usd", pa.float64()),
        ("shares", pa.float64()),
        ("trade_price_median", pa.float64()),
        ("markout_5m", pa.float64()),
        ("markout_5m_median", pa.float64()),
        ("markout_trades", pa.int64()),
        ("markout_matched", pa.int64()),
        ("markout_low_sample", pa.bool_()),
        ("fee_type", pa.string()),
        ("fee_known", pa.bool_()),
        ("fee_rate", pa.float64()),
        ("fee_exponent", pa.float64()),
        ("fee_per_share", pa.float64()),
        ("fee_usd", pa.float64()),
        ("fee_shares_covered", pa.float64()),
        ("edge_per_share", pa.float64()),
        ("gate1", pa.bool_()),
        ("merge_gas_pol", pa.float64()),
        ("merge_gas_usd", pa.float64()),
        ("reward_per_capital", pa.float64()),
        ("rank_edge", pa.int64()),
        ("rank_pool", pa.int64()),
    ])


def write_parquet(rows: Sequence[Dict[str, Any]], path: Path, header: str) -> int:
    schema = parquet_schema()
    table = pa.Table.from_arrays(
        [pa.array([r.get(f.name) for r in rows], type=f.type) for f in schema],
        schema=schema,
    )
    metadata = dict(table.schema.metadata or {})
    metadata[b"market_scorecard.header"] = header.encode("utf-8")
    metadata[b"market_scorecard.measured_columns"] = ",".join(MEASURED_COLUMNS).encode()
    metadata[b"market_scorecard.derived_columns"] = ",".join(DERIVED_COLUMNS).encode()
    metadata[b"market_scorecard.proxy_columns"] = ",".join(PROXY_COLUMNS).encode()
    table = table.replace_schema_metadata(metadata)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return table.num_rows


def write_csv(rows: Sequence[Dict[str, Any]], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_arrays(
        [pa.array([r.get(f.name) for r in rows], type=f.type) for f in parquet_schema()],
        schema=parquet_schema(),
    )
    table.to_pandas().to_csv(path, index=False)
    return len(rows)


def _fmt(value: Optional[float], width: int = 9, dp: int = 4) -> str:
    if value is None:
        return "n/a".rjust(width)
    return f"{value:.{dp}f}".rjust(width)


def _flag(value: Optional[bool]) -> str:
    return "n/a" if value is None else ("PASS" if value else "FAIL")


def _int(value: Optional[int], width: int = 5) -> str:
    if value is None:
        return "n/a".rjust(width)
    return f"{int(value):,}".rjust(width)


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns // NS, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render(rows: List[Dict[str, Any]], stats: Dict[str, Any], *, tape: Path, top: int) -> str:
    gas = stats["gas"]
    mk_cov = (
        100.0 * stats["markout_matched"] / stats["markout_trades"]
        if stats["markout_trades"] else 0.0
    )
    lines = [
        "market scorecard -- window {} .. {} (UTC, half-open, tape {})".format(
            _iso(stats["start_ns"]), _iso(stats["end_ns"]), tape
        ),
        "coverage: events->{} assets, {} quote points; trades {} rows; "
        "conditions {} traded / {} quoted / {} in manifest / {} total".format(
            stats["assets"], stats["quote_points"], stats["trade_rows"],
            stats["conditions_traded"], stats["conditions_quoted"],
            stats["conditions_in_manifest"], stats["conditions"],
        ),
        "markout({}s) coverage: {}/{} trades matched ({:.1f}%); "
        "feeType known for {}/{} traded conditions ({} fee schedules loaded{})".format(
            stats["horizon_s"], stats["markout_matched"], stats["markout_trades"], mk_cov,
            stats["conditions_fee_known"], stats["conditions_traded"], stats["fee_rows"],
            "" if not stats["unknown_fee_types"]
            else "; unknown types: " + ",".join(stats["unknown_fee_types"]),
        ),
        "gas: {} blocks / {} txs, base fee median {} gwei ({}..{}) -> merge {} gas = {} POL"
        "{}. merge gas is per merge, not per share".format(
            gas["blocks"], gas["txs"], _fmt(gas["median_gwei"], 8, 2),
            _fmt(gas["min_gwei"], 8, 2), _fmt(gas["max_gwei"], 8, 2),
            MERGE_GAS_UNITS, _fmt(gas["gas_pol"], 10, 8),
            "" if gas["gas_usd"] is None
            else " = ${:.6f}".format(gas["gas_usd"]) + " (--pol-usd given)",
        ),
        "edge = half_spread - markout - fee_per_share; gate1 = edge > 0 "
        "(n/a unless all three are measured and the market traded)",
        "measured columns: " + ", ".join(MEASURED_COLUMNS),
        "derived columns: " + ", ".join(DERIVED_COLUMNS)
        + "; keys: condition_id, question, in_manifest; flags: markout_low_sample",
        "proxy columns (manifest / assumed): " + ", ".join(PROXY_COLUMNS),
        "  reward_per_capital = daily_pool / max(min_size*price, 1), USD reward per USD "
        "capital per day assuming this quote earns the whole pool -- an upper bound, not an",
        "  expectation; the pool is split across every qualifying LP.",
        "gate1: {} pass of {} scored ({} pass with markout n<{} and are flagged low_sample)".format(
            stats["conditions_gate1"], stats["conditions_scored"],
            stats["conditions_gate1_low_sample"], stats["min_markouts"],
        ),
    ]

    by_edge = sorted(
        (r for r in rows if r["edge_per_share"] is not None),
        key=lambda r: (-r["edge_per_share"], -(r["daily_pool"] or 0.0)),
    )[:top]
    lines += ["", f"top {len(by_edge)} by measured edge (edge > 0 is gate1 PASS); "
             f"n_mk = matched trades behind markout -- below {stats['min_markouts']} "
             f"the edge rests on single prints and is not a measurement"]
    h1 = (
        f"{'condition':<12} {'n_mk':>5} {'trades':>6} {'notional':>10} "
        f"{'spread':>8} {'half':>8} {'mk5m':>8} {'fee/sh':>8} {'edge':>8} {'g1':>4} "
        f"{'pool':>7} {'rpc':>7}"
    )
    lines += [h1, "-" * len(h1)]
    for r in by_edge:
        lines.append(
            f"{r['condition_id'][:12]:<12} {_int(r['markout_matched'])} {_int(r['trades'], 6)} "
            f"{_fmt(r['notional_usd'], 10, 0)} {_fmt(r['spread_median'], 8)} "
            f"{_fmt(r['half_spread_median'], 8)} {_fmt(r['markout_5m'], 8)} "
            f"{_fmt(r['fee_per_share'], 8)} {_fmt(r['edge_per_share'], 8)} "
            f"{_flag(r['gate1']):>4} {_fmt(r['daily_pool'], 7, 1)} "
            f"{_fmt(r['reward_per_capital'], 7, 3)}"
        )

    by_pool = sorted(
        (r for r in rows if r["daily_pool"] is not None),
        key=lambda r: (-(r["daily_pool"] or 0.0), -(r["edge_per_share"] if r["edge_per_share"] is not None else -9e9)),
    )[:top]
    lines += ["", f"top {len(by_pool)} by daily_pool (manifest reward proxy; rpc is an upper "
             f"bound, edge is measured)"]
    h2 = (
        f"{'condition':<12} {'pool':>7} {'min_size':>8} {'max_spr':>7} "
        f"{'spread':>8} {'mk5m':>8} {'fee/sh':>8} {'edge':>8} {'g1':>4} {'n_mk':>5} {'rpc':>7}"
    )
    lines += [h2, "-" * len(h2)]
    for r in by_pool:
        lines.append(
            f"{r['condition_id'][:12]:<12} {_fmt(r['daily_pool'], 7, 1)} "
            f"{_fmt(r['min_size'], 8, 1)} {_fmt(r['max_spread'], 7, 2)} "
            f"{_fmt(r['spread_median'], 8)} {_fmt(r['markout_5m'], 8)} "
            f"{_fmt(r['fee_per_share'], 8)} {_fmt(r['edge_per_share'], 8)} "
            f"{_flag(r['gate1']):>4} {_int(r['markout_matched'])} "
            f"{_fmt(r['reward_per_capital'], 7, 3)}"
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tape", default=str(DEFAULT_TAPE))
    ap.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="parquet output")
    ap.add_argument("--csv", default="", help="CSV output (default: beside --out)")
    ap.add_argument("--summary", default=str(DEFAULT_SUMMARY), help="plain-text summary")
    ap.add_argument("--date", default=None, help="UTC day YYYY-MM-DD (default: last two tape hours)")
    ap.add_argument("--hour-from", type=int, default=None)
    ap.add_argument("--hour-to", type=int, default=None)
    ap.add_argument("--horizon", type=int, default=MARKOUT_HORIZON_S, help="markout seconds")
    ap.add_argument("--min-markouts", type=int, default=MIN_MARKOUTS,
                    help="matched trades below which a markout is flagged low_sample")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--pol-usd", type=float, default=None,
                    help="POL/USD rate; without it merge gas stays in POL")
    args = ap.parse_args(argv)

    tape = Path(args.tape)
    con = duckdb.connect()
    try:
        bounds = window_bounds(args.date, args.hour_from, args.hour_to)
        if bounds is None:
            if args.hour_from is not None or args.hour_to is not None:
                raise SystemExit("--hour-from/--hour-to need --date")
            start_ns, end_ns = last_window(con, tape)
            log.info(f"default window: {_iso(start_ns)} .. {_iso(end_ns)}")
        else:
            start_ns, end_ns = bounds

        rows, stats = collect(
            con, tape, start_ns, end_ns,
            snapshot=Path(args.snapshot),
            manifest=load_manifest(Path(args.manifest)),
            horizon_s=int(args.horizon),
            pol_usd=args.pol_usd,
            min_markouts=int(args.min_markouts),
        )
    finally:
        con.close()

    if not rows:
        raise SystemExit(f"no conditions in the window {_iso(start_ns)} .. {_iso(end_ns)}")

    text = render(rows, stats, tape=tape, top=int(args.top))
    out = Path(args.out)
    csv_path = Path(args.csv) if args.csv else out.with_suffix(".csv")
    summary = Path(args.summary)
    n = write_parquet(rows, out, text)
    write_csv(rows, csv_path)
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(text + "\n", encoding="utf-8")

    log.info(text)
    log.info(f"wrote {n:,} conditions to {out} and {csv_path}; summary {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
