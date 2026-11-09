#!/usr/bin/env python3
"""A/B a predictive quote gate against the reactionary quote on the L3 tape.

The reactionary policy in ``backtest_quotes.py`` quotes one tick either side of
the current mid and holds the quote until the book top changes. This script
keeps that baseline intact and asks one question:

    If a maker cancels the side that order flow or Binance spot has just
    threatened, do the surviving fills earn more than the reactionary fills?

Two signals are tested, both computed with data available *before* the quote is
placed (no look-ahead):

* **OFI** -- Cont-style price-only order-flow imbalance from consecutive
  top-of-book states over a trailing window. Positive OFI (bid up / ask down)
  means buy pressure, so a resting *ask* is the threatened side; negative OFI
  threatens a resting *bid*. The gate skips the threatened side when the mean
  step is at least the configured threshold.
* **Binance velocity** -- for crypto markets only, the spot return over a
  trailing window. On a positively exposed token (Yes / above-strike / Up) an
  up surge threatens the ask and a down surge threatens the bid; the mapping is
  reversed on the complementary token.

Fills, shares, gross, markout and net come from the same L3-shadow simulator as
``backtest_quotes.py``. A fill the gate would have cancelled is reported under
``excluded``: the gate's value is the difference between the baseline and the
retained fills, and the ``excluded`` mean says whether the signal removed the
right fills. Both the gate and the placebo (signal sign flipped) are printed, so
a result that merely removes half the fills by chance is visible.

Caveat: the L3 shadow order joined the level before the hypothetical quote, so
the fill count is an optimistic upper bound. The sample here is small; the
report states fill counts and never converts a few fills into a strategy result.

Usage
-----
    # smoke: one asset-hour
    .venv/Scripts/python.exe scripts/backtest_predictive_gate.py --max-asset-hours 1

    # all derived asset-hours (political + crypto), OFI only
    .venv/Scripts/python.exe scripts/backtest_predictive_gate.py --binance

    # no output files, print the table
    .venv/Scripts/python.exe scripts/backtest_predictive_gate.py --max-asset-hours 10 --no-write
"""

from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import duckdb  # noqa: E402

import backtest_quotes as bq  # noqa: E402
from polymarketbot.logs import get_logger  # noqa: E402
from polymarketbot.rewards.microstructure import MicrostructureEngine  # noqa: E402

log = get_logger("backtest_predictive_gate")

NS = 1_000_000_000
DEFAULT_MANIFEST = Path("data/tape/manifest_current.json")
DEFAULT_BINANCE = Path("data/tape/binance_bbo")
DEFAULT_TAPE = Path("data/tape")
DEFAULT_L3_ROOT = Path("data/derived/l3_queue")
DEFAULT_OUT = Path("data/derived")

SYMBOL_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "BTCUSDT": ("bitcoin",),
    "ETHUSDT": ("ethereum", "ether"),
    "SOLUSDT": ("solana",),
    "XRPUSDT": ("xrp", "ripple"),
    "DOGEUSDT": ("dogecoin", "doge"),
}


# ------------------------------------------------------------------- signals
def load_bbo_states(tape: Path, date: str, hour: str, asset: str) -> List[Tuple[int, float, float]]:
    """Distinct ``(ts, best_bid, best_ask)`` states for ``asset`` in the hour."""
    glob = bq._sql(tape / "clob_events" / f"date={date}" / f"hour={hour}" / "*.parquet")
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            WITH pc AS (
                SELECT recv_ts_ns, UNNEST(price_changes) AS c
                FROM read_parquet('{glob}')
                WHERE event_type = 'price_change'
            )
            SELECT recv_ts_ns, c.best_bid, c.best_ask
            FROM pc
            WHERE c.asset_id = ? AND c.best_bid IS NOT NULL AND c.best_ask IS NOT NULL
            UNION ALL
            SELECT recv_ts_ns, best_bid, best_ask
            FROM read_parquet('{glob}')
            WHERE event_type = 'best_bid_ask' AND asset_id = ?
              AND best_bid IS NOT NULL AND best_ask IS NOT NULL
            ORDER BY 1
            """,
            [asset, asset],
        ).fetchall()
    finally:
        con.close()
    states: List[Tuple[int, float, float]] = []
    last: Optional[Tuple[int, float, float]] = None
    for ts, bid, ask in rows:
        state = (int(ts), float(bid), float(ask))
        if last is not None and state[1] == last[1] and state[2] == last[2]:
            continue
        states.append(state)
        last = state
    return states


def ofi_steps(states: Sequence[Tuple[int, float, float]]) -> List[Tuple[int, int]]:
    """Cont price-only OFI step at each state change: bid up/ask down = +1 each."""
    out: List[Tuple[int, int]] = []
    for (ts, bid, ask), (_, prev_bid, prev_ask) in zip(states[1:], states[:-1]):
        e_bid = 1 if bid > prev_bid else (-1 if bid < prev_bid else 0)
        e_ask = 1 if ask < prev_ask else (-1 if ask > prev_ask else 0)
        out.append((ts, e_bid + e_ask))
    return out


def ofi_series(steps: Sequence[Tuple[int, int]]) -> Tuple[List[int], List[int]]:
    """Sorted step timestamps and their prefix sums for O(log n) window queries."""
    stamps: List[int] = []
    prefix: List[int] = [0]
    for stamp, step in steps:
        stamps.append(int(stamp))
        prefix.append(prefix[-1] + int(step))
    return stamps, prefix


def ofi_signal(
    series: Tuple[Sequence[int], Sequence[int]],
    ts: int,
    window_s: float,
    min_changes: int,
) -> Optional[float]:
    """Mean OFI step over ``[ts - window, ts)``; None below ``min_changes``."""
    import bisect

    stamps, prefix = series
    lo_ts = int(ts - window_s * NS)
    i = bisect.bisect_left(stamps, lo_ts)
    j = bisect.bisect_left(stamps, ts)
    n = j - i
    if n < min_changes:
        return None
    return (prefix[j] - prefix[i]) / n


def signal_table(
    steps: Sequence[Tuple[int, int]],
    times: Sequence[int],
    window_s: float,
    min_changes: int,
) -> Dict[int, Optional[float]]:
    """Signal at each quote time, cached because ``times`` has duplicate stamps."""
    series = ofi_series(steps)
    out: Dict[int, Optional[float]] = {}
    for ts in times:
        if ts not in out:
            out[ts] = ofi_signal(series, ts, window_s, min_changes)
    return out


# ------------------------------------------------------------------- Binance
def load_binance_mids(path: Path, symbol: str) -> List[Tuple[int, float]]:
    """``(recv_ts_ns, mid)`` for one symbol in one Binance BBO partition."""
    glob = bq._sql(path / "*.parquet")
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            SELECT recv_ts_ns, (bid + ask) / 2.0 AS mid
            FROM read_parquet('{glob}')
            WHERE symbol = ? AND bid IS NOT NULL AND ask IS NOT NULL
            ORDER BY recv_ts_ns
            """,
            [symbol],
        ).fetchall()
    finally:
        con.close()
    return [(int(ts), float(mid)) for ts, mid in rows]


def binance_velocity(mids: Sequence[Tuple[int, float]], ts: int, lookback_s: float) -> Optional[float]:
    """Spot return in bps from the last mid at/before ``ts - lookback`` to ``ts``."""
    import bisect

    stamps = [m[0] for m in mids]
    i = bisect.bisect_right(stamps, ts) - 1
    if i < 1:
        return None
    j = bisect.bisect_right(stamps, ts - int(lookback_s * NS)) - 1
    if j < 0:
        return None
    ref = mids[j][1]
    if ref <= 0.0:
        return None
    return (mids[i][1] - ref) / ref * 10_000.0


def crypto_symbol(question: str) -> Optional[str]:
    q = (question or "").lower()
    for symbol, patterns in SYMBOL_PATTERNS.items():
        if any(p in q for p in patterns):
            return symbol
    return None


def load_l3_top_states(path: Path, asset: str) -> List[Tuple[int, float, float, float, float]]:
    """``(ts, bid, bid_size, ask, ask_size)`` when the L3 top of book changes."""
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            SELECT recv_ts_ns, side, price, size_at_level
            FROM read_parquet('{bq._sql(path)}')
            WHERE asset_id = ?
            ORDER BY recv_ts_ns
            """,
            [asset],
        ).fetchall()
    finally:
        con.close()
    bids: Dict[float, float] = {}
    asks: Dict[float, float] = {}
    last: Optional[Tuple[float, float, float, float]] = None
    out: List[Tuple[int, float, float, float, float]] = []
    for ts, side, price, size in rows:
        book = bids if str(side).lower() in ("bid", "buy") else asks
        key = round(float(price), 10)
        if float(size) > 0.0:
            book[key] = float(size)
        else:
            book.pop(key, None)
        bid = max(bids) if bids else 0.0
        ask = min(asks) if asks else 1.0
        top = (bid, bids.get(bid, 0.0), ask, asks.get(ask, 0.0))
        if top != last:
            out.append((int(ts), top[0], top[1], top[2], top[3]))
            last = top
    return out


def live_actions(states: Sequence[Tuple[int, float, float, float, float]]) -> List[Tuple[int, str, str, bool]]:
    """Run the live ``MicrostructureEngine`` over the top states.

    Returns ``(ts, side, action, pull)`` for both quote sides at each state.
    """
    engine = MicrostructureEngine(ofi_threshold=850.0, drift_threshold_cents=1.5)
    out: List[Tuple[int, str, str, bool]] = []
    for ts, bid, bid_size, ask, ask_size in states:
        engine.update(bid, bid_size, ask, ask_size, timestamp=ts / 1e9)
        for side in ("bid", "ask"):
            action = engine.evaluate_quote_action("BUY" if side == "bid" else "SELL")
            out.append((ts, side, action.action, bool(action.should_pull or action.should_widen)))
    return out


# ------------------------------------------------------------------- gating
@dataclass(frozen=True)
class GateConfig:
    side_signal: str = "ofi"  # ofi | binance
    threshold: float = 0.0  # mean OFI step, or Binance bps
    binance_lookback_s: float = 5.0
    sign: float = 1.0  # +1: positively exposed token; -1: complementary


def adverse(side: str, signed_signal: Optional[float], sign: float) -> bool:
    """Is ``side`` threatened by a signed signal on a token with exposure ``sign``?"""
    if signed_signal is None:
        return False
    s = signed_signal * sign
    return (s > 0.0 and side == "ask") or (s < 0.0 and side == "bid")


def agg_fills(fills: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    shares = sum(float(r["shares"]) for r in fills)
    net = sum(float(r["net_usd"]) for r in fills if r.get("net_usd") is not None)
    marked = [r for r in fills if r.get("net_per_share") is not None]
    mean = statistics.fmean(float(r["net_per_share"]) for r in marked) if marked else None
    weighted = net / shares if shares else None
    markout = (
        statistics.fmean(float(r["markout"]) for r in marked)
        if marked and all(r.get("markout") is not None for r in marked)
        else None
    )
    return {
        "fills": len(fills),
        "shares": shares,
        "net_usd": net,
        "net_per_share": mean,
        "net_per_share_weighted": weighted,
        "markout": markout,
        "losers": sum(1 for r in marked if float(r["net_per_share"]) < 0.0),
    }


def ab_rows(
    rows: Sequence[Dict[str, Any]],
    signals: Dict[int, Optional[float]],
    cfg: GateConfig,
) -> Dict[str, Any]:
    """Split marked fills into retained vs excluded by the gate; return the A/B."""
    base = [r for r in rows if r.get("net_per_share") is not None]

    retained: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    for r in base:
        s = signals.get(int(r["quote_ts_ns"]))
        # The OFI signal lives on the asset's own book, so token exposure is
        # irrelevant; the Binance signal is on the underlying, so the token's
        # Yes/No exposure (and the placebo flip) decide the threatened side.
        exposure = float(r.get("exposure_sign", 1.0)) if cfg.side_signal == "binance" else 1.0
        if s is not None and abs(s) >= cfg.threshold and adverse(
            str(r["side"]), s, exposure * cfg.sign
        ):
            excluded.append(r)
        else:
            retained.append(r)
    return {"base": agg_fills(base), "retained": agg_fills(retained), "excluded": agg_fills(excluded),
            "retained_rows": retained, "excluded_rows": excluded}


def ab_live(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """A/B the live MicrostructureEngine's PULL/WIDEN gate on the same fills."""
    base = [r for r in rows if r.get("net_per_share") is not None]
    retained = [r for r in base if not r.get("live_pull")]
    excluded = [r for r in base if r.get("live_pull")]
    return {"base": agg_fills(base), "retained": agg_fills(retained),
            "excluded": agg_fills(excluded),
            "retained_rows": retained, "excluded_rows": excluded}


def quantile_threshold(signals: Sequence[Optional[float]], q: float) -> float:
    vals = sorted(abs(float(s)) for s in signals if s is not None)
    if not vals:
        return 0.0
    idx = max(0, min(len(vals) - 1, int(round(q * (len(vals) - 1)))))
    return vals[idx]


# ----------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="A/B a predictive quote gate against the reactionary quote.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--l3-root", default=str(DEFAULT_L3_ROOT))
    p.add_argument("--tape", default=str(DEFAULT_TAPE))
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p.add_argument("--binance-root", default=str(DEFAULT_BINANCE))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--max-asset-hours", type=int, default=0, help="cap for a staged run")
    p.add_argument("--date", default=None, help="tape date filter, YYYY-MM-DD")
    p.add_argument("--hour", default=None, help="tape hour filter, HH")
    p.add_argument("--assets", default="", help="comma-separated asset-id prefixes")
    p.add_argument("--sides", choices=("both", "bid", "ask"), default="both")
    p.add_argument("--distance-ticks", type=float, default=1.0)
    p.add_argument("--size", type=float, default=None, help="order size (default: market min_size)")
    p.add_argument("--ofi-window-s", type=float, default=60.0)
    p.add_argument("--ofi-min-changes", type=int, default=10)
    p.add_argument("--threshold-quantile", type=float, default=0.9)
    p.add_argument("--binance", action="store_true", help="also test the Binance velocity gate")
    p.add_argument("--microstructure", action="store_true", help="also test the live MicrostructureEngine gate")
    p.add_argument("--binance-bps", type=float, default=1.0)
    p.add_argument("--binance-lookback-s", type=float, default=5.0)
    p.add_argument("--min-fills", type=int, default=5)
    p.add_argument("--no-write", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    l3_root = Path(args.l3_root)
    tape = Path(args.tape)
    manifest_path = Path(args.manifest)
    binance_root = Path(args.binance_root)
    out_dir = Path(args.out)
    prefixes = [p.strip() for p in args.assets.split(",") if p.strip()]

    hours = bq.available_asset_hours(l3_root)
    if args.date:
        hours = [h for h in hours if h["date"] == args.date]
    if args.hour:
        hours = [h for h in hours if h["hour"] == f"{int(args.hour):02d}"]
    if prefixes:
        hours = [h for h in hours if any(h["asset"].startswith(p) for p in prefixes)]
    if args.max_asset_hours:
        hours = hours[: args.max_asset_hours]
    if not hours:
        raise SystemExit(f"no l3_queue asset-hours under {l3_root}")

    all_rows: List[Dict[str, Any]] = []
    per_hour: List[Dict[str, Any]] = []
    ofi_signals: List[Optional[float]] = []
    binance_signals: List[Optional[float]] = []
    gate_cfg = GateConfig(side_signal="ofi", threshold=0.0)
    bin_cfg = GateConfig(side_signal="binance", threshold=args.binance_bps,
                         binance_lookback_s=args.binance_lookback_s)

    for idx, hour in enumerate(hours, 1):
        date, hh, asset = hour["date"], hour["hour"], hour["asset"]
        try:
            episodes, condition_id = bq.load_episodes(tape, date, hh, asset)
        except SystemExit as exc:
            log.warning(f"{date} {hh} {asset[:16]}.. skipped: {exc}")
            continue
        if not episodes:
            continue
        path = Path(hour["path"])
        levels = bq.load_levels(path)
        prices = bq.load_prices(path)
        tick = bq.load_tick_size(tape, date, hh, asset) or bq.infer_tick(prices) or 0.01
        market = bq.load_market(manifest_path, condition_id, asset)
        size = float(args.size) if args.size else float((market or {}).get("min_size") or 100.0)
        question = str((market or {}).get("question") or "")
        cfg = bq.QuoteConfig(
            sides=args.sides,
            distance=args.distance_ticks * tick,
            tick=tick,
            order_size=size,
        )
        mid_ts = [e.start_ns for e in episodes]
        mid_val = [e.mid for e in episodes]
        res = bq.simulate(episodes, levels, mid_ts, mid_val, cfg)
        rows = res["rows"]

        states = load_bbo_states(tape, date, hh, asset)
        steps = ofi_steps(states)
        quote_times = sorted({int(e.start_ns) for e in episodes})
        sig = signal_table(steps, quote_times, args.ofi_window_s, args.ofi_min_changes)
        for r in rows:
            r["ofi_signal"] = sig.get(int(r["quote_ts_ns"]))

        if args.microstructure:
            tops = load_l3_top_states(path, asset)
            acts = live_actions(tops)
            live_at: Dict[int, Dict[str, Tuple[str, bool]]] = {}
            for ts, side, action, pull in acts:
                live_at.setdefault(int(ts), {})[side] = (action, pull)
            top_ts = [int(t[0]) for t in tops]
            for r in rows:
                i = bisect.bisect_right(top_ts, int(r["quote_ts_ns"])) - 1
                if i >= 0:
                    entry = live_at.get(top_ts[i], {}).get(str(r["side"]))
                    if entry:
                        r["live_action"], r["live_pull"] = entry[0], entry[1]

        # Binance signal for crypto markets: signed by the token's exposure.
        sign = 1.0
        bin_sig: Dict[int, Optional[float]] = {}
        symbol = crypto_symbol(question)
        if symbol:
            tokens = [str(t) for t in ((market or {}).get("clob_tokens") or [])]
            if len(tokens) == 2 and asset == tokens[1]:
                sign = -1.0
            bpath = binance_root / f"date={date}" / f"hour={hh}"
            mids = load_binance_mids(bpath, symbol) if bpath.exists() else []
            if mids:
                for ts in quote_times:
                    bin_sig[ts] = binance_velocity(mids, ts, args.binance_lookback_s)
                for r in rows:
                    r["binance_signal"] = bin_sig.get(int(r["quote_ts_ns"]))
        bin_cfg = GateConfig(side_signal="binance", threshold=args.binance_bps,
                             binance_lookback_s=args.binance_lookback_s, sign=sign)
        for r in rows:
            r["exposure_sign"] = sign
            r["symbol"] = symbol

        all_rows.extend(rows)
        ofi_signals.extend(sig.get(int(e.start_ns)) for e in episodes)
        if bin_sig:
            binance_signals.extend(bin_sig.get(int(e.start_ns)) for e in episodes)
        per_hour.append({
            "date": date, "hour": hh, "asset": asset, "question": question,
            "symbol": symbol, "sign": sign, "episodes": len(episodes),
            "rows": rows, "sig": sig, "bin_sig": bin_sig,
            "ofi_cfg": gate_cfg, "bin_cfg": bin_cfg,
        })
        log.info(
            f"[{idx}/{len(hours)}] {date} {hh} {asset[:12]}.. eps={len(episodes)} "
            f"fills={len(rows)} tick={tick:g} symbol={symbol or '-'}"
        )

    if not per_hour:
        log.warning("no asset-hours produced fill rows")
        return 1

    # Thresholds are fit on all eligible quote signals (not on the fills), so the
    # gate's trigger rate is a property of the market, not of the realised fills.
    ofi_q = quantile_threshold(ofi_signals, args.threshold_quantile)
    bin_q = quantile_threshold(binance_signals, args.threshold_quantile) if binance_signals else 0.0

    def row_summary(label: str, rows: Sequence[Dict[str, Any]], sig_key: str,
                    threshold: float, sign: float = 1.0) -> Dict[str, Any]:
        cfg = GateConfig(side_signal=sig_key, threshold=threshold, sign=sign)
        signals = {int(r["quote_ts_ns"]): r.get(sig_key) for r in rows}
        # Only the quoted fills carry a signal key; the A/B needs the fill rows.
        res = ab_rows(rows, signals, cfg)
        return {"label": label, "threshold": threshold, **res}

    def fmt(x: Optional[float], dp: int = 5) -> str:
        return "n/a" if x is None else f"{x:+.{dp}f}"

    def agg_line(label: str, a: Dict[str, Any], which: str = "retained") -> str:
        r = a["base"] if which == "base" else a["retained"]
        e = a["excluded"]
        excluded = (
            ""
            if which == "base"
            else (
                f"  | excluded {e['fills']:3d} fills, net/fill {fmt(e['net_per_share'])},"
                f" net/share {fmt(e['net_per_share_weighted'])}, net ${fmt(e['net_usd'], 3)},"
                f" losers {e['losers']:3d}"
            )
        )
        return (
            f"{label:22s} fills {r['fills']:4d}  shares {r['shares']:8.1f}  "
            f"net/fill {fmt(r['net_per_share'])}  net/share {fmt(r['net_per_share_weighted'])}  "
            f"net ${fmt(r['net_usd'], 3)}{excluded}"
        )

    lines: List[str] = []
    lines.append("predictive quote gate A/B -- L3-shadow fills, no look-ahead signal")
    lines.append(
        f"asset-hours: {len(per_hour)}  quote sides: {args.sides}  "
        f"distance: {args.distance_ticks:g} tick  size: market min_size"
    )
    lines.append(
        f"OFI: trailing {args.ofi_window_s:.0f}s, >= {args.ofi_min_changes} changes; "
        f"p{args.threshold_quantile:.0%} threshold {ofi_q:.4f}"
    )
    if args.binance:
        lines.append(
            f"Binance: trailing {args.binance_lookback_s:.0f}s, p{args.threshold_quantile:.0%} "
            f"threshold {bin_q:.3f} bps (configured {args.binance_bps:g} bps)"
        )
    lines.append("")

    all_ofi = row_summary("all / OFI", all_rows, "ofi_signal", ofi_q)
    base = all_ofi["base"]
    lines.append(agg_line("baseline", all_ofi, "base"))
    lines.append(agg_line("gate OFI (same sign)", all_ofi))
    if args.microstructure:
        all_live = ab_live(all_rows)
        lines.append(agg_line("baseline live", all_live, "base"))
        lines.append(agg_line("gate live (PULL/WIDEN)", all_live))
    placebo = row_summary("all / OFI placebo", all_rows, "ofi_signal", ofi_q, sign=-1.0)
    lines.append(agg_line("gate OFI (placebo)", placebo))
    for thr in (0.0, 0.5, 1.0):
        if thr >= ofi_q:
            continue
        a = row_summary(f"all / OFI >={thr:g}", all_rows, "ofi_signal", thr)
        lines.append(agg_line(f"gate OFI >={thr:g}", a))

    if args.binance and binance_signals:
        all_bin = row_summary("all / Binance", all_rows, "binance_signal", bin_q)
        lines.append(agg_line("gate Binance", all_bin))
        bplacebo = row_summary("all / Binance placebo", all_rows, "binance_signal", bin_q, sign=-1.0)
        lines.append(agg_line("gate Binance placebo", bplacebo))
        for thr in (1.0, 2.0, 5.0):
            if thr >= bin_q:
                continue
            a = row_summary(f"all / Binance >={thr:g}", all_rows, "binance_signal", thr)
            lines.append(agg_line(f"gate Binance >={thr:g}", a))

    # Split by market type: the OFI gate is the only one available on politics.
    for kind, keep in (("crypto", lambda r: r.get("symbol") is not None),
                       ("political", lambda r: r.get("symbol") is None)):
        rows = [r for r in all_rows if keep(r)]
        if not rows:
            continue
        a = row_summary(f"{kind} / OFI", rows, "ofi_signal", ofi_q)
        lines.append("")
        lines.append(agg_line(f"{kind} baseline", a, "base"))
        lines.append(agg_line(f"{kind} gate OFI", a))
        p = row_summary(f"{kind} / OFI placebo", rows, "ofi_signal", ofi_q, sign=-1.0)
        lines.append(agg_line(f"{kind} gate placebo", p))
        if args.microstructure:
            live = ab_live(rows)
            lines.append(agg_line(f"{kind} live base", live, "base"))
            lines.append(agg_line(f"{kind} gate live", live))

    lines.append("")
    lines.append(f"baseline marked fills: {base['fills']}, losers {base['losers']}")
    lines.append("signal coverage: "
                 f"OFI {sum(1 for r in all_rows if r.get('ofi_signal') is not None)}/{len(all_rows)} fills; "
                 f"Binance {sum(1 for r in all_rows if r.get('binance_signal') is not None)}/{len(all_rows)} fills; "
                 f"live {sum(1 for r in all_rows if r.get('live_pull') is not None)}/{len(all_rows)} fills")
    lines.append("")
    lines.append("caveats:")
    lines.append("  * L3-shadow fills are optimistic; sample sizes are printed, not inferred.")
    lines.append("  * the threshold is fit on the same tape that is evaluated (in-sample);")
    lines.append("    the placebo column is the control for a gate that simply removes fills.")
    lines.append("  * a gate improves a maker only if it removes negative-markout fills; the")
    lines.append("    excluded mean says whether it did or merely reduced sample size.")

    text = "\n".join(lines)
    log.info(text)

    if not args.no_write:
        import pyarrow as pa
        import pyarrow.parquet as pq

        out_dir.mkdir(parents=True, exist_ok=True)
        flat: List[Dict[str, Any]] = []
        for ph in per_hour:
            for r in ph["rows"]:
                flat.append({
                    "date": ph["date"], "hour": ph["hour"], "asset": ph["asset"],
                    "question": ph["question"], "symbol": ph["symbol"],
                    "sign": ph["sign"],
                    **{k: r.get(k) for k in (
                        "quote_ts_ns", "side", "quote_price", "mid_at_quote", "fill_ts_ns",
                        "shares", "mid_at_fill", "gross_spread", "mid_after_horizon",
                        "markout", "fee_per_share", "gas_per_share", "net_per_share",
                        "net_usd", "ofi_signal", "binance_signal",
                    )},
                })
        pq.write_table(pa.Table.from_pylist(flat), out_dir / "predictive_gate_fills.parquet")
        (out_dir / "predictive_gate_summary.txt").write_text(text + "\n", encoding="utf-8")
        log.info(f"wrote {out_dir / 'predictive_gate_fills.parquet'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
