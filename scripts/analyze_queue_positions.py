#!/usr/bin/env python3
"""Join the predictive-gate fills to the L3 queue at the quote price.

The OFI side gate was measured on 29 political asset-hours: reactionary
-$309.69, side gate +$17.52 over 780 marked fills. This script asks the
queue-position question the research raises: was the gate's benefit concentrated
in fills where the threatened side had a *low* fill probability (deep queue,
liquid book), or where the queue was thin and the adverse fill likely?

For every fill produced by ``scripts/backtest_predictive_gate.py`` it reads the
L3 row for the fill side at the quote price, before the quote timestamp:
``q_ahead``, ``size_at_level``, ``queue_ratio``, ``survival``,
``replenishment_latency``, ``queue_age_ns``. It computes the measured fill
probability from ``data/derived/fill_model.json`` and reports:

* baseline: all marked fills;
* side gate: drop every fill on the side the trailing OFI threatens (threshold 0);
* queue-aware: drop the threatened fill only when the measured fill probability
  is at least ``--min-fill-prob`` and the queue ratio is below
  ``--max-queue-ratio``. In deep/low-probability queues it keeps the fill
  (the pull would mostly forfeit reward without avoiding an adverse fill).

Outputs ``data/derived/queue_gate_summary.txt`` and
``data/derived/queue_gate_fills.parquet`` (the fill rows plus queue columns).
No look-ahead: the queue row is the last one at or before the quote timestamp.
"""

from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import duckdb  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from polymarketbot.logs import get_logger  # noqa: E402
from polymarketbot.paper_trading.orchestrator import fill_probability  # noqa: E402

log = get_logger("analyze_queue_positions")

DEFAULT_FILLS = Path("data/derived/predictive_gate_fills.parquet")
DEFAULT_L3 = Path("data/derived/l3_queue")
DEFAULT_OUT = Path("data/derived")
MIN_ADVERSE_QUEUE_RATIO = 0.60
PROB_EDGES = (0.0, 0.05, 0.10, 0.20, 0.40, 0.70, 1.0)
RATIO_EDGES = (0.0, 0.30, 0.60, 0.85, 0.95, 1.0)
SIZE_EDGES = (0.0, 50.0, 250.0, 1000.0, 5000.0, float("inf"))


def _bucket(value: float, edges: Sequence[float]) -> str:
    for lo, hi in zip(edges, edges[1:]):
        if lo <= value < hi:
            return f"{lo:g}-{hi:g}"
    return f"{edges[-2]:g}+"


def load_fills(path: Path) -> List[Dict[str, Any]]:
    table = pq.read_table(path)
    return [dict(r) for r in table.to_pylist()]


def load_l3_rows(path: Path) -> Dict[Tuple[str, float], List[Tuple[int, float, float, float, float, int, int]]]:
    """``(side, price) -> sorted[(ts, q_ahead, size_at_level, ratio, fill_size, survival, queue_age)]``."""
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""
            SELECT recv_ts_ns, side, price, q_ahead, size_at_level, queue_ratio,
                   fill_size, survival, queue_age_ns
            FROM read_parquet('{path.as_posix()}')
            ORDER BY recv_ts_ns
            """
        ).fetchall()
    finally:
        con.close()
    out: Dict[Tuple[str, float], List[Tuple[int, float, float, float, float, int, int]]] = {}
    for ts, side, price, q_ahead, size, ratio, fill, survival, age in rows:
        key = (str(side), round(float(price), 10))
        out.setdefault(key, []).append(
            (int(ts), float(q_ahead), float(size), float(ratio), float(fill),
             int(survival), int(age))
        )
    return out


def queue_before(
    rows: Dict[Tuple[str, float], List[Tuple[int, float, float, float, float, int, int]]],
    side: str,
    price: float,
    ts: int,
) -> Optional[Tuple[float, float, float, float, int, int]]:
    arr = rows.get((side, round(float(price), 10)))
    if not arr:
        return None
    stamps = [r[0] for r in arr]
    idx = bisect.bisect_right(stamps, ts) - 1
    if idx < 0:
        return None
    _, q_ahead, size, ratio, _fill, survival, age = arr[idx]
    return q_ahead, size, ratio, survival, age, idx


def agg(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    marked = [r for r in rows if r.get("net_per_share") is not None]
    shares = sum(float(r["shares"]) for r in marked)
    net = sum(float(r["net_usd"]) for r in marked if r.get("net_usd") is not None)
    mean = statistics.fmean(float(r["net_per_share"]) for r in marked) if marked else None
    weighted = net / shares if shares else None
    return {
        "fills": len(marked),
        "shares": shares,
        "net_usd": net,
        "net_per_share": mean,
        "net_per_share_weighted": weighted,
        "losers": sum(1 for r in marked if float(r["net_per_share"]) < 0.0),
    }


def fmt(x: Optional[float], dp: int = 5) -> str:
    return "n/a" if x is None else f"{x:+.{dp}f}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fills", default=str(DEFAULT_FILLS))
    ap.add_argument("--l3-root", default=str(DEFAULT_L3))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--min-adverse-queue-ratio", type=float, default=MIN_ADVERSE_QUEUE_RATIO)
    args = ap.parse_args(argv)

    fills_path = Path(args.fills)
    l3_root = Path(args.l3_root)
    out_dir = Path(args.out)
    if not fills_path.exists():
        raise SystemExit(f"missing {fills_path}; run scripts/backtest_predictive_gate.py --write first")

    fills = load_fills(fills_path)
    log.info(f"loaded {len(fills)} fill rows from {fills_path}")

    # Group by asset-hour so each L3 file is read once.
    by_asset: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for r in fills:
        by_asset.setdefault((str(r["date"]), str(r["hour"]), str(r["asset"])), []).append(r)

    enriched: List[Dict[str, Any]] = []
    for (date, hour, asset), rows in by_asset.items():
        path = l3_root / f"date={date}" / f"hour={hour}" / f"asset={asset}.parquet"
        if not path.exists():
            log.warning(f"missing L3 {path}; skipping {len(rows)} fills")
            continue
        l3 = load_l3_rows(path)
        for r in rows:
            side = str(r["side"])
            price = float(r["quote_price"])
            ts = int(r["quote_ts_ns"])
            q = queue_before(l3, side, price, ts)
            row = dict(r)
            if q is None:
                row.update({"queue_ratio": None, "size_at_level": None,
                            "q_ahead": None, "fill_prob": None,
                            "survival": None, "queue_age_ns": None})
            else:
                q_ahead, size, ratio, survival, age, _ = q
                row.update({
                    "queue_ratio": ratio,
                    "size_at_level": size,
                    "q_ahead": q_ahead,
                    "fill_prob": fill_probability(ratio, size),
                    "survival": survival,
                    "queue_age_ns": age,
                })
            enriched.append(row)

    marked = [r for r in enriched if r.get("net_per_share") is not None and r.get("fill_prob") is not None]
    base = agg(marked)
    for r in marked:
        ofi = r.get("ofi_signal")
        side = str(r["side"])
        threatened = (
            ofi is not None
            and ((float(ofi) > 0.0 and side == "ask") or (float(ofi) < 0.0 and side == "bid"))
        )
        r["side_gate_excluded"] = bool(threatened)
        r["queue_aware_excluded"] = bool(
            threatened and float(r["queue_ratio"]) >= args.min_adverse_queue_ratio
        )
        r["threatened_side"] = ("ask" if float(ofi) > 0.0 else "bid") if ofi is not None else None

    side_gate = [r for r in marked if not r["side_gate_excluded"]]
    queue_aware = [r for r in marked if not r["queue_aware_excluded"]]
    side_excluded = [r for r in marked if r["side_gate_excluded"]]
    qa_excluded = [r for r in marked if r["queue_aware_excluded"]]
    qa_kept = [r for r in side_excluded if not r["queue_aware_excluded"]]

    lines: List[str] = []
    lines.append("queue-position join -- L3 shadow fills, no look-ahead")
    lines.append(
        f"fills: {len(enriched)} rows, {len(marked)} marked with a queue row; "
        f"min adverse queue ratio {args.min_adverse_queue_ratio:g}"
    )
    lines.append(
        "queue model: logit P = 3.2769 - 1.1421*ratio - 0.5733*log1p(size); "
        "6,817 observations / 2,184 fills, 2026-11-04 01:00-05:59 UTC"
    )
    lines.append("")
    for label, a in (("baseline", base), ("side gate", agg(side_gate)),
                     ("queue-aware", agg(queue_aware)),
                     ("side-gate excluded", agg(side_excluded)),
                     ("queue-aware excluded", agg(qa_excluded)),
                     ("queue-aware kept (was excluded)", agg(qa_kept))):
        lines.append(
            f"{label:32s} fills {a['fills']:4d}  shares {a['shares']:8.1f}  "
            f"net/fill {fmt(a['net_per_share'])}  net/share {fmt(a['net_per_share_weighted'])}  "
            f"net ${fmt(a['net_usd'], 3)}  losers {a['losers']:3d}"
        )

    lines.append("")
    lines.append("side-gate excluded fills by threatened-side fill probability:")
    for lo, hi in zip(PROB_EDGES, PROB_EDGES[1:]):
        bucket = [r for r in side_excluded if lo <= float(r["fill_prob"]) < hi]
        if bucket:
            a = agg(bucket)
            lines.append(
                f"  p {lo:.2f}-{hi:.2f}: fills {a['fills']:3d}  net/share "
                f"{fmt(a['net_per_share_weighted'])}  net ${fmt(a['net_usd'], 3)}"
            )
    lines.append("")
    lines.append("side-gate excluded fills by queue ratio:")
    for lo, hi in zip(RATIO_EDGES, RATIO_EDGES[1:]):
        bucket = [r for r in side_excluded if lo <= float(r["queue_ratio"]) < hi]
        if bucket:
            a = agg(bucket)
            lines.append(
                f"  ratio {lo:.2f}-{hi:.2f}: fills {a['fills']:3d}  net/share "
                f"{fmt(a['net_per_share_weighted'])}  net ${fmt(a['net_usd'], 3)}"
            )
    lines.append("")
    lines.append("side-gate excluded fills by level depth (shares):")
    for lo, hi in zip(SIZE_EDGES, SIZE_EDGES[1:]):
        bucket = [r for r in side_excluded if lo <= float(r["size_at_level"]) < hi]
        if bucket:
            a = agg(bucket)
            lines.append(
                f"  size {lo:g}-{hi:g}: fills {a['fills']:3d}  net/share "
                f"{fmt(a['net_per_share_weighted'])}  net ${fmt(a['net_usd'], 3)}"
            )

    lines.append("")
    lines.append(f"side-gate net improvement over baseline: ${base['net_usd'] - agg(side_gate)['net_usd']:+.3f}"
                 if False else
                 f"side-gate removal: baseline ${base['net_usd']:+.3f} -> ${agg(side_gate)['net_usd']:+.3f}")
    lines.append(f"queue-aware removal: baseline ${base['net_usd']:+.3f} -> ${agg(queue_aware)['net_usd']:+.3f}")
    lines.append(f"queue-aware kept (low-prob) fills: ${agg(qa_kept)['net_usd']:+.3f} over {agg(qa_kept)['fills']} fills")
    lines.append("")
    lines.append("caveats:")
    lines.append("  * queue row is the last L3 observation at/before the quote; a fresh quote")
    lines.append("    joins behind the displayed queue, so the measured ratio is the existing")
    lines.append("    queue's, not the new order's. The probability is relative, not absolute.")
    lines.append("  * the fill model is print-conditional; the backtester's per-episode fill")
    lines.append("    rate is much lower (0.98 % in the one-hour quote backtest).")
    lines.append("  * the reward is not in the net; a single-sided quote is divided by 3")
    lines.append("    inside 0.10-0.90 and scores zero outside it (Polymarket docs).")
    text = "\n".join(lines)
    log.info(text)

    out_dir.mkdir(parents=True, exist_ok=True)
    keys = [
        "date", "hour", "asset", "side", "quote_ts_ns", "quote_price", "mid_at_quote",
        "shares", "net_per_share", "net_usd", "markout", "ofi_signal",
        "queue_ratio", "size_at_level", "q_ahead", "fill_prob", "survival",
        "queue_age_ns", "side_gate_excluded", "queue_aware_excluded",
    ]
    flat = [{k: r.get(k) for k in keys} for r in enriched]
    pq.write_table(pa.Table.from_pylist(flat), out_dir / "queue_gate_fills.parquet")
    (out_dir / "queue_gate_summary.txt").write_text(text + "\n", encoding="utf-8")
    log.info(f"wrote {out_dir / 'queue_gate_summary.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
