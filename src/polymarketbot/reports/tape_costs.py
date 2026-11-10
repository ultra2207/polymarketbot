#!/usr/bin/env python3
"""Tape-side cost stack: spread, markout, fees, gas, Gate 1.

Answers the questions the 14-day plan gates on, using the raw tape rather than
the paper engine:

* spread           -- best_bid_ask per condition (median, p25, p75)
* markout          -- signed mid move h seconds after each tape trade.
                      Positive = taker profit = maker adverse selection.
* fees             -- taker fee implied by the market's Gamma ``feeType``
                      (``docs/fees.md``: C x rate x (p(1-p))^exponent).
* gas              -- Polygon base fee observed in the tape, and the cost of
                      one CTF ``mergePositions`` at that fee (78,000 gas).
* Gate 1           -- half_spread > markout(h) + fee_per_share.

A cell with too few observations prints ``n/a``; it never prints a fabricated
zero. Markout is matched by an ASOF join to the first ``best_bid_ask`` at or
after t+h and *within 60 s of it*, so a market that goes quiet does not
silently borrow a quote from hours later.

Usage
    python -m polymarketbot.reports.tape_costs --date 2026-11-04
    python -m polymarketbot.reports.tape_costs --hour-from 13 --top 20
    python -m polymarketbot.reports.tape_costs --horizons 30,300,1800
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb

from polymarketbot.fees import schedule_for_fee_type
from polymarketbot.logs import get_logger

log = get_logger("report_tape_costs")

DEFAULT_TAPE = "data/tape"
DEFAULT_SNAPSHOT = "data/raw/gamma/snapshot_2026-11-01.parquet"
MERGE_GAS_UNITS = 78_000  # CTF mergePositions, trading/ctf.py
MATCH_WINDOW_NS = 60_000_000_000  # accept a quote within 60 s of the horizon
MIN_MARKOUTS = 30  # below this a condition's markout is n/a
TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"


def _where(date: Optional[str], hour_from: Optional[int], hour_to: Optional[int]) -> str:
    clauses: List[str] = []
    if date:
        clauses.append(f"date = '{date}'")
    if hour_from is not None:
        clauses.append(f"hour >= '{int(hour_from):02d}'")
    if hour_to is not None:
        clauses.append(f"hour <= '{int(hour_to):02d}'")
    return " WHERE 1=1" + (" AND " + " AND ".join(clauses) if clauses else "")


def _fmt(value: Optional[float], width: int = 10, dp: int = 4) -> str:
    if value is None:
        return "n/a".rjust(width)
    return f"{value:.{dp}f}".rjust(width)


def _load_fee_table(con: duckdb.DuckDBPyConnection, snapshot: Path, when: datetime) -> int:
    """Register ``fees``: condition_id -> rate, exponent from the Gamma feeType."""
    if not snapshot.exists():
        log.warning(f"snapshot missing ({snapshot}); fees will be n/a")
        con.execute("CREATE TEMP TABLE fees (condition_id VARCHAR, rate DOUBLE, exponent DOUBLE)")
        return 0
    rows = con.execute(
        f"SELECT conditionId, feeType FROM read_parquet('{snapshot.as_posix()}') "
        "WHERE feeType IS NOT NULL"
    ).fetchall()
    fees: Dict[str, Tuple[float, float]] = {}
    for condition_id, fee_type in rows:
        sched = schedule_for_fee_type(str(fee_type), when)
        if sched is None or sched.rate <= 0.0:
            continue
        fees[str(condition_id).lower()] = (sched.rate, sched.exponent)
    con.execute("CREATE TEMP TABLE fees (condition_id VARCHAR, rate DOUBLE, exponent DOUBLE)")
    if fees:
        con.executemany(
            "INSERT INTO fees VALUES (?, ?, ?)",
            [(cid, rate, exp) for cid, (rate, exp) in fees.items()],
        )
    return len(fees)


def _build_quotes(con: duckdb.DuckDBPyConnection, where: str) -> int:
    """Materialise mid quotes for the window once, for all markout horizons.

    ``price_change`` is the authoritative L2 stream (148 M frames); each nested
    change carries the token's ``best_bid``/``best_ask``. ``best_bid_ask`` is
    unioned in as a fallback for markets that only emit the BBO event. Only
    assets that actually traded in the window are kept, so the ASOF join does
    not carry the full book of 6,800 assets.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE quotes AS
        WITH pc AS (
            SELECT recv_ts_ns, UNNEST(price_changes) AS c
            FROM read_parquet('{DEFAULT_TAPE}/clob_events/**/*.parquet')
            {where} AND event_type = 'price_change'
        ),
        traded AS (
            SELECT asset_id, ANY_VALUE(condition_id) AS condition_id
            FROM read_parquet('{DEFAULT_TAPE}/clob_trades/**/*.parquet')
            {where} AND asset_id IS NOT NULL AND condition_id IS NOT NULL
            GROUP BY 1
        )
        SELECT c.asset_id AS asset_id, traded.condition_id AS condition_id,
               pc.recv_ts_ns AS recv_ts_ns,
               (c.best_bid + c.best_ask) / 2.0 AS mid,
               (c.best_ask - c.best_bid) AS spread
        FROM pc JOIN traded ON traded.asset_id = c.asset_id
        WHERE c.asset_id IS NOT NULL
          AND c.best_bid IS NOT NULL AND c.best_ask IS NOT NULL
        UNION ALL
        SELECT asset_id, condition_id, recv_ts_ns,
               (best_bid + best_ask) / 2.0,
               (best_ask - best_bid)
        FROM read_parquet('{DEFAULT_TAPE}/clob_events/**/*.parquet')
        {where} AND event_type = 'best_bid_ask'
          AND best_bid IS NOT NULL AND best_ask IS NOT NULL
        """
    )
    return int(con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0])


def _markout(
    con: duckdb.DuckDBPyConnection,
    where: str,
    horizon_s: int,
) -> Dict[str, Dict[str, Any]]:
    """Per-condition markout at ``horizon_s`` (positive = taker profit)."""
    h_ns = int(horizon_s) * 1_000_000_000
    sql = f"""
    WITH t AS (
        SELECT condition_id, asset_id, recv_ts_ns, price, size, side
        FROM read_parquet('{DEFAULT_TAPE}/clob_trades/**/*.parquet')
        {where}
          AND asset_id IS NOT NULL AND condition_id IS NOT NULL
          AND price IS NOT NULL AND size IS NOT NULL
    ),
    j AS (
        SELECT t.condition_id, t.price, t.size, t.side, t.recv_ts_ns AS tts,
               q.mid, q.recv_ts_ns AS qts
        FROM t ASOF LEFT JOIN quotes q
          ON t.asset_id = q.asset_id
         AND q.recv_ts_ns >= t.recv_ts_ns + {h_ns}
    )
    SELECT condition_id,
           COUNT(*) AS n,
           COUNT(mid) AS matched,
           SUM(price * size) AS notional,
           SUM(CASE WHEN side = 'BUY' THEN mid - price ELSE price - mid END * size)
               / NULLIF(SUM(CASE WHEN mid IS NOT NULL THEN size END), 0) AS w_markout
    FROM j
    WHERE qts IS NULL OR qts < tts + {h_ns + MATCH_WINDOW_NS}
    GROUP BY 1
    """
    out: Dict[str, Dict[str, Any]] = {}
    for cid, n, matched, notional, w_markout in con.execute(sql).fetchall():
        out[str(cid)] = {
            "trades": int(n),
            "matched": int(matched or 0),
            "notional": float(notional or 0.0),
            "markout": float(w_markout) if matched else None,
        }
    return out


def _spreads(con: duckdb.DuckDBPyConnection) -> Dict[str, Dict[str, Any]]:
    sql = """
    SELECT condition_id, COUNT(*) AS n, COUNT(DISTINCT asset_id) AS assets,
           MEDIAN(spread) AS med, QUANTILE_CONT(spread, 0.25) AS p25,
           QUANTILE_CONT(spread, 0.75) AS p75
    FROM quotes
    WHERE condition_id IS NOT NULL
    GROUP BY 1
    """
    out: Dict[str, Dict[str, Any]] = {}
    for cid, n, assets, med, p25, p75 in con.execute(sql).fetchall():
        out[str(cid)] = {
            "quotes": int(n),
            "assets": int(assets),
            "med_spread": float(med) if med is not None else None,
            "p25": float(p25) if p25 is not None else None,
            "p75": float(p75) if p75 is not None else None,
        }
    return out


def _trade_fees(con: duckdb.DuckDBPyConnection, where: str) -> Dict[str, Dict[str, Any]]:
    sql = f"""
    SELECT t.condition_id, COUNT(*) AS n, SUM(t.price * t.size) AS notional,
           SUM(t.size) AS shares,
           SUM(CASE WHEN f.rate IS NULL THEN 0
                    ELSE t.size * f.rate * POW(t.price * (1 - t.price), f.exponent) END) AS fee
    FROM read_parquet('{DEFAULT_TAPE}/clob_trades/**/*.parquet') t
    LEFT JOIN fees f ON t.condition_id = f.condition_id
    {where}
      AND t.condition_id IS NOT NULL AND t.price IS NOT NULL AND t.size IS NOT NULL
    GROUP BY 1
    """
    out: Dict[str, Dict[str, Any]] = {}
    for cid, n, notional, shares, fee in con.execute(sql).fetchall():
        out[str(cid)] = {
            "trades": int(n),
            "notional": float(notional or 0.0),
            "shares": float(shares or 0.0),
            "fee_usd": float(fee or 0.0),
        }
    return out


def _gas(con: duckdb.DuckDBPyConnection, where: str, pol_usd: Optional[float]) -> Dict[str, Any]:
    row = con.execute(
        f"""
        SELECT COUNT(*), AVG(base_fee_gwei), MEDIAN(base_fee_gwei),
               MIN(base_fee_gwei), MAX(base_fee_gwei), SUM(tx_count)
        FROM read_parquet('{DEFAULT_TAPE}/polygon_gas/**/*.parquet') {where}
        """
    ).fetchone()
    blocks, avg_gwei, med_gwei, lo_gwei, hi_gwei, txs = row
    gas_pol = None
    gas_usd = None
    if med_gwei is not None:
        gas_pol = MERGE_GAS_UNITS * float(med_gwei) * 1e-9
        if pol_usd is not None:
            gas_usd = gas_pol * float(pol_usd)
    ctf = con.execute(
        f"""
        SELECT
          SUM(CASE WHEN topic0 = '{TRANSFER_BATCH}' THEN 1 ELSE 0 END),
          SUM(CASE WHEN topic0 = '{TRANSFER_SINGLE}' THEN 1 ELSE 0 END)
        FROM read_parquet('{DEFAULT_TAPE}/polygon_logs/**/*.parquet') {where}
        """
    ).fetchone()
    return {
        "blocks": int(blocks or 0),
        "avg_gwei": float(avg_gwei) if avg_gwei is not None else None,
        "med_gwei": float(med_gwei) if med_gwei is not None else None,
        "lo_gwei": float(lo_gwei) if lo_gwei is not None else None,
        "hi_gwei": float(hi_gwei) if hi_gwei is not None else None,
        "txs": int(txs or 0),
        "gas_pol": gas_pol,
        "gas_usd": gas_usd,
        "transfer_batch": int(ctf[0] or 0),
        "transfer_single": int(ctf[1] or 0),
    }


def _last_window(con: duckdb.DuckDBPyConnection) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """The last two UTC hours present in the tape (default report window)."""
    row = con.execute(
        f"SELECT MAX(recv_ts_ns) FROM read_parquet('{DEFAULT_TAPE}/clob_events/**/*.parquet')"
    ).fetchone()
    if not row or row[0] is None:
        return None, None, None
    stamp = datetime.fromtimestamp(int(row[0]) / 1e9, timezone.utc)
    return stamp.strftime("%Y-%m-%d"), max(0, stamp.hour - 1), stamp.hour


def build_report(
    tape: Path,
    snapshot: Path,
    *,
    date: Optional[str],
    hour_from: Optional[int],
    hour_to: Optional[int],
    horizons: List[int],
    top: int,
    pol_usd: Optional[float],
) -> str:
    con = duckdb.connect()
    when = datetime.now(timezone.utc)
    if date is None and hour_from is None and hour_to is None:
        date, hour_from, hour_to = _last_window(con)
    fee_rows = _load_fee_table(con, snapshot, when)
    where = _where(date, hour_from, hour_to)

    trades = _trade_fees(con, where)
    quote_rows = _build_quotes(con, where)
    spreads = _spreads(con)
    markouts = {h: _markout(con, where, h) for h in horizons}
    gas = _gas(con, where, pol_usd)

    lines = [
        f"window: {date or 'all dates'} hours {hour_from if hour_from is not None else '00'}"
        f"-{hour_to if hour_to is not None else '23'}  (tape {tape})",
        f"fee schedules loaded: {fee_rows:,} conditions; quote points: {quote_rows:,}",
        f"gas: blocks={gas['blocks']:,} txs={gas['txs']:,} "
        f"base_fee avg={_fmt(gas['avg_gwei'], 8, 2)} gwei med={_fmt(gas['med_gwei'], 8, 2)} "
        f"range={_fmt(gas['lo_gwei'], 7, 2)}-{_fmt(gas['hi_gwei'], 7, 2)}",
        f"     merge {MERGE_GAS_UNITS:,} gas = {_fmt(gas['gas_pol'], 10, 6)} POL"
        + (f" = ${gas['gas_usd']:.6f}" if gas["gas_usd"] is not None else " (pass --pol-usd for USD)"),
        f"     CTF transfer logs: batch={gas['transfer_batch']:,} single={gas['transfer_single']:,}",
        "",
        "markout: signed mid move at t+h; positive = taker profit = maker adverse selection",
        "gate1: half_spread > markout(5m) + fee_per_share",
        "",
    ]

    h5 = 300 if 300 in horizons else horizons[0]
    rows: List[Tuple[float, str, Dict[str, Any], Dict[str, Any], Dict[str, Any]]] = []
    for cid, t in trades.items():
        if t["notional"] <= 0:
            continue
        rows.append((t["notional"], cid, t, spreads.get(cid, {}), markouts[h5].get(cid, {})))
    rows.sort(reverse=True)

    header = (
        f"{'condition':<14} {'trades':>7} {'notional':>12} {'assets':>6} "
        f"{'spread_med':>10} {'markout':>9} {'fee/sh':>8} {'gate1':>7}"
    )
    lines += [header, "-" * len(header)]

    pass_n = 0
    eval_n = 0
    for notional, cid, t, s, m in rows[:top]:
        med = s.get("med_spread")
        mk = m.get("markout")
        fee_sh = (t["fee_usd"] / t["shares"]) if t["shares"] else None
        gate = "n/a"
        if med is not None and mk is not None and m.get("matched", 0) >= MIN_MARKOUTS and fee_sh is not None:
            eval_n += 1
            ok = (med / 2.0) > (mk + fee_sh)
            pass_n += int(ok)
            gate = "PASS" if ok else "FAIL"
        lines.append(
            f"{cid[:14]:<14} {t['trades']:>7,} {notional:>12,.0f} {s.get('assets', 0):>6,} "
            f"{_fmt(med, 10)} {_fmt(mk, 9)} {_fmt(fee_sh, 8)} {gate:>7}"
        )

    total_notional = sum(t["notional"] for t in trades.values())
    total_fee = sum(t["fee_usd"] for t in trades.values())
    total_shares = sum(t["shares"] for t in trades.values())
    all_matched = sum(m.get("matched", 0) for m in markouts[h5].values())
    all_trades = sum(m.get("trades", 0) for m in markouts[h5].values())
    lines += [
        "",
        f"conditions: {len(trades):,} traded, {len(spreads):,} quoted; "
        f"notional=${total_notional:,.0f}; taker fee=${total_fee:,.2f} "
        f"({(total_fee / total_shares * 100 if total_shares else 0):.3f} c/share)",
        f"markout(5m) coverage: {all_matched:,}/{all_trades:,} trades matched "
        f"({(100 * all_matched / all_trades if all_trades else 0):.1f}%)",
        f"gate1 evaluated: {eval_n} conditions, {pass_n} pass "
        f"({(100 * pass_n / eval_n if eval_n else 0):.1f}%)",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tape", default=DEFAULT_TAPE)
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--hour-from", type=int, default=None)
    ap.add_argument("--hour-to", type=int, default=None)
    ap.add_argument("--horizons", default="30,300,1800", help="comma seconds")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--pol-usd", type=float, default=None)
    args = ap.parse_args(argv)

    horizons = [int(x) for x in str(args.horizons).split(",") if x.strip()]
    log.info(
        build_report(
            Path(args.tape),
            Path(args.snapshot),
            date=args.date,
            hour_from=args.hour_from,
            hour_to=args.hour_to,
            horizons=horizons,
            top=args.top,
            pol_usd=args.pol_usd,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
