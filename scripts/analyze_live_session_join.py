#!/usr/bin/env python3
"""Join a LiveSessionRecorder session to the public CLOB tape.

Reads the session's quote/order/fill rows, selects the public tape rows for the
session's condition, and reports:

  * condition and asset key overlap (the session's YES/NO vs the tape's assets);
  * tape coverage vs the session window;
  * nearest-event and nearest-trade time deltas for every session observation;
  * feed latency (tape recv_ts_ns - venue_ts_ms) for context.

The output is a Markdown report plus a JSON artifact. The join is deliberately
observational: the public tape is the market context, not an execution truth.

Usage:
    .venv/Scripts/python.exe scripts/analyze_live_session_join.py \
        --session data/live_tape/20261105T084909Z-abbb5f \
        --report docs/live-session-join-report.md
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Optional

import duckdb
import numpy as np
import pyarrow.parquet as pq
from datetime import datetime, timezone

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SESSION_ROOT = REPO_ROOT / "data" / "live_tape"
DEFAULT_TAPE_ROOT = REPO_ROOT / "data" / "tape"
TOLERANCE_NS = 60 * 1_000_000_000


def _session_rows(root: Path, table: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    directory = root / table
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("*.parquet")):
        rows.extend(pq.read_table(path).to_pylist())
    return rows


def _select_session(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit)
    candidates = []
    for path in DEFAULT_SESSION_ROOT.iterdir():
        if not path.is_dir() or path.name.startswith("paper-"):
            continue
        manifest = path / "session.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if len(str(data.get("condition_id") or "")) >= 40:
            candidates.append(path)
    if not candidates:
        raise SystemExit("no recorded session found under data/live_tape")
    return sorted(candidates)[-1]


def _nearest_delta_ns(times: np.ndarray, obs: int) -> Optional[int]:
    if times.size == 0:
        return None
    idx = int(np.searchsorted(times, obs))
    best: Optional[int] = None
    for j in (idx - 1, idx):
        if 0 <= j < times.size:
            delta = int(times[j]) - obs
            if best is None or abs(delta) < abs(best):
                best = delta
    return best


def _stats(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    abs_ms = sorted(abs(v) / 1e6 for v in values)
    return {
        "n": len(abs_ms),
        "p50_ms": round(statistics.median(abs_ms), 1),
        "p95_ms": round(abs_ms[min(len(abs_ms) - 1, int(0.95 * len(abs_ms)))], 1),
        "max_ms": round(abs_ms[-1], 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default=None, help="session directory (default: latest real session)")
    parser.add_argument("--report", default="docs/live-session-join-report.md")
    parser.add_argument("--json-output", default=None)
    args = parser.parse_args()

    root = _select_session(args.session)
    manifest = json.loads((root / "session.json").read_text(encoding="utf-8"))
    condition_id = str(manifest.get("condition_id") or "")
    tokens = [str(t) for t in (manifest.get("tokens") or []) if t]
    candidates = [t for t in tokens if len(t) >= 40]
    started_at = manifest.get("started_at")
    ended_at = (manifest.get("end") or {}).get("stopped_at") or started_at
    start_dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00")) if started_at else None
    end_dt = datetime.fromisoformat(str(ended_at).replace("Z", "+00:00")) if ended_at else start_dt
    start_ns = int(start_dt.timestamp() * 1e9) if start_dt else 0
    end_ns = int(end_dt.timestamp() * 1e9) if end_dt else start_ns
    date = start_dt.strftime("%Y-%m-%d") if start_dt else None

    conv = duckdb.connect()
    tape_events: list[dict[str, Any]] = []
    tape_trades: list[dict[str, Any]] = []
    if date and condition_id:
        for table, target in (("clob_events", tape_events), ("clob_trades", tape_trades)):
            glob = (DEFAULT_TAPE_ROOT / table / f"date={date}" / "hour=*" / "*.parquet").as_posix()
            query = f"""
                SELECT * FROM read_parquet(?)
                WHERE condition_id = ?
                  AND recv_ts_ns BETWEEN ? AND ?
            """
            cursor = conv.execute(query, [glob, condition_id, start_ns - 3600 * 1_000_000_000, end_ns + 3600 * 1_000_000_000])
            columns = [d[0] for d in cursor.description]
            target.extend(dict(zip(columns, row)) for row in cursor.fetchall())

    observations: list[dict[str, Any]] = []
    for row in _session_rows(root, "quotes"):
        if row.get("asset_id") and row.get("recv_ts_ns"):
            observations.append({"kind": str(row.get("kind")), "asset_id": str(row["asset_id"]), "ts_ns": int(row["recv_ts_ns"])})
    for row in _session_rows(root, "orders"):
        asset = row.get("token_id") or row.get("asset_id")
        if asset:
            ts = row.get("timestamp")
            observations.append({
                "kind": "venue_order",
                "asset_id": str(asset),
                "ts_ns": int(float(ts) * 1e9) if ts else int(row.get("recv_ts_ns") or 0),
            })
    for row in _session_rows(root, "active_quotes"):
        if str(row.get("condition_id") or "") != condition_id:
            continue
        token_side = str(row.get("token_side") or "").upper()
        asset = tokens[0] if token_side == "YES" and len(tokens) > 0 else tokens[1] if len(tokens) > 1 else None
        ts = row.get("timestamp")
        if asset and ts:
            observations.append({"kind": "engine_quote", "asset_id": str(asset), "ts_ns": int(float(ts) * 1e9)})
    for row in _session_rows(root, "fills"):
        asset = row.get("asset_id")
        if asset:
            ts = row.get("timestamp")
            observations.append({
                "kind": "fill",
                "asset_id": str(asset),
                "ts_ns": int(float(ts) * 1e9) if ts else int(row.get("recv_ts_ns") or 0),
            })

    tape_by_asset: dict[str, list[int]] = {}
    for event in tape_events:
        if event.get("asset_id") and event.get("recv_ts_ns") is not None:
            tape_by_asset.setdefault(str(event["asset_id"]), []).append(int(event["recv_ts_ns"]))
    trade_by_asset: dict[str, list[int]] = {}
    for trade in tape_trades:
        if trade.get("asset_id") and trade.get("recv_ts_ns") is not None:
            trade_by_asset.setdefault(str(trade["asset_id"]), []).append(int(trade["recv_ts_ns"]))
    for times in list(tape_by_asset.values()) + list(trade_by_asset.values()):
        times.sort()

    event_deltas: list[int] = []
    trade_deltas: list[int] = []
    matched_assets: set[str] = set()
    unmatched_assets: set[str] = set()
    within_tolerance = 0
    for obs in observations:
        asset = obs["asset_id"]
        times = tape_by_asset.get(asset, [])
        if times:
            matched_assets.add(asset)
        else:
            unmatched_assets.add(asset)
        delta = _nearest_delta_ns(np.asarray(times, dtype=np.int64), obs["ts_ns"])
        if delta is not None:
            event_deltas.append(delta)
            if abs(delta) <= TOLERANCE_NS:
                within_tolerance += 1
        trade_delta = _nearest_delta_ns(np.asarray(trade_by_asset.get(asset, []), dtype=np.int64), obs["ts_ns"])
        if trade_delta is not None:
            trade_deltas.append(trade_delta)

    tape_asset_ids = sorted(tape_by_asset)
    event_times = [int(e["recv_ts_ns"]) for e in tape_events if e.get("recv_ts_ns") is not None]
    tape_lo = min(event_times) if event_times else None
    tape_hi = max(event_times) if event_times else None
    overlap_ns = 0
    if tape_lo is not None:
        overlap_ns = max(0, min(end_ns, tape_hi) - max(start_ns, tape_lo))
    feed_latency_ms = sorted(
        (int(e["recv_ts_ns"]) - int(e["venue_ts_ms"]) * 1_000_000) / 1e6
        for e in tape_events
        if e.get("venue_ts_ms") and int(e["venue_ts_ms"]) > 0
    )
    event_types: dict[str, int] = {}
    for event in tape_events:
        event_types[str(event.get("event_type"))] = event_types.get(str(event.get("event_type")), 0) + 1

    report = {
        "session": root.name,
        "condition_id": condition_id,
        "session_started_at": started_at,
        "session_ended_at": ended_at,
        "dry_run": manifest.get("dry_run"),
        "public_tape_run": manifest.get("public_tape_run"),
        "session_tokens": tokens,
        "tape_assets": tape_asset_ids,
        "tape_events": len(tape_events),
        "tape_trades": len(tape_trades),
        "tape_event_types": event_types,
        "tape_coverage": {
            "min": datetime.fromtimestamp(tape_lo / 1e9, timezone.utc).isoformat() if tape_lo else None,
            "max": datetime.fromtimestamp(tape_hi / 1e9, timezone.utc).isoformat() if tape_hi else None,
        },
        "session_window_overlap_s": round(overlap_ns / 1e9, 3),
        "observations": len(observations),
        "observation_kinds": {kind: sum(1 for o in observations if o["kind"] == kind) for kind in {o["kind"] for o in observations}},
        "asset_keys_matched": sorted(matched_assets),
        "asset_keys_unmatched": sorted(unmatched_assets),
        "nearest_event": _stats(event_deltas),
        "nearest_trade": _stats(trade_deltas),
        "within_60s": within_tolerance,
        "feed_latency_ms": {
            "n": len(feed_latency_ms),
            "p50": round(statistics.median(feed_latency_ms), 1) if feed_latency_ms else None,
            "p95": round(feed_latency_ms[min(len(feed_latency_ms) - 1, int(0.95 * len(feed_latency_ms)))], 1) if feed_latency_ms else None,
        },
    }

    lines = [
        "# Live session ↔ public tape join",
        "",
        f"- Session: `{root.name}` ({'dry-run' if report['dry_run'] else 'live'}), window "
        f"`{started_at}` → `{ended_at}`; public tape run `{report['public_tape_run']}`.",
        f"- Condition: `{condition_id}`.",
        f"- Observations: **{len(observations)}** "
        + ", ".join(f"{k}={v}" for k, v in sorted(report["observation_kinds"].items()))
        + ".",
        "",
        "## Tape coverage",
        "",
        f"- `clob_events` rows for the condition: **{len(tape_events)}** "
        f"({', '.join(f'{k}={v}' for k, v in sorted(event_types.items()))}).",
        f"- `clob_trades` rows: **{len(tape_trades)}**.",
        f"- Coverage: `{report['tape_coverage']['min']}` → `{report['tape_coverage']['max']}`; "
        f"overlap with the session window: **{report['session_window_overlap_s']:.1f} s**.",
        "",
        "## Key overlap",
        "",
        f"- Session tokens: {['`' + t + '`' for t in tokens]}",
        f"- Tape assets: {['`' + t + '`' for t in tape_asset_ids]}",
        f"- Matched asset keys: {['`' + t + '`' for t in sorted(matched_assets)] or '—'}",
        f"- Unmatched session assets: {['`' + t + '`' for t in sorted(unmatched_assets)] or '—'}",
        "",
        "## Nearest-event join",
        "",
        f"- Nearest event |Δ|: n={report['nearest_event'].get('n', 0)}, "
        f"p50={report['nearest_event'].get('p50_ms')} ms, "
        f"p95={report['nearest_event'].get('p95_ms')} ms, "
        f"max={report['nearest_event'].get('max_ms')} ms.",
        f"- Observations within 60 s of a tape event: **{within_tolerance}/{len(observations)}**.",
        f"- Nearest trade |Δ|: n={report['nearest_trade'].get('n', 0)}, "
        f"p50={report['nearest_trade'].get('p50_ms')} ms, "
        f"p95={report['nearest_trade'].get('p95_ms')} ms.",
        "",
        "## Context",
        "",
        f"- Tape feed latency (recv_ts_ns − venue_ts_ms): p50={report['feed_latency_ms']['p50']} ms, "
        f"p95={report['feed_latency_ms']['p95']} ms (n={report['feed_latency_ms']['n']}).",
        "",
        "## Findings",
        "",
    ]
    findings: list[str] = []
    if tape_events:
        findings.append(
            f"The condition is present in the public tape; {len(observations)} session observations "
            "carry joinable condition/asset keys."
        )
    else:
        findings.append("No tape rows for the condition in the queried window; the session cannot be joined for this window.")
    if unmatched_assets:
        findings.append(
            "Session token(s) absent from the tape: "
            + ", ".join(f"`{a}`" for a in sorted(unmatched_assets))
            + ". The CLOB market detail is authoritative; the runner now refuses a "
            "`--token-*` override that disagrees with it. Re-run with the CLOB token "
            "or omit the flag."
        )
    if tape_lo is not None and overlap_ns == 0:
        findings.append("The session window does not overlap the tape's condition coverage; nearest-event deltas are context only.")
    if not any(o["kind"] == "fill" for o in observations):
        findings.append("No fills in this session (dry-run); the fill join path is exercised once a live session records executions.")
    if report["nearest_event"].get("n", 0) and report["nearest_event"].get("max_ms", 0) > 60_000:
        findings.append(
            "The condition's public book/trade stream is change-driven; the nearest event is "
            f"{report['nearest_event']['p50_ms']:.0f} ms away. In a quiet book the last snapshot remains "
            "the state until the next change, so event proximity is context, not a validity test. "
            "This dry-run window had no public update within 60 s."
        )
    lines.extend(f"- {f}" for f in findings)
    lines.extend([
        "",
        "The public tape is market context, not an execution record. The join is by",
        "`condition_id`/`asset_id` and UTC `recv_ts_ns`. The collector writes book",
        "snapshots on change, so a long nearest-event delta can mean an unchanged",
        "book (the last snapshot is still the state), not a missing join. The",
        "nearest-event delta is not a clock correction.",
        "",
    ])

    report_path = REPO_ROOT / args.report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    json_path = Path(args.json_output) if args.json_output else REPO_ROOT / "data" / "derived" / "live_join" / f"{root.name}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps(report, indent=1, default=str))
    print(f"report -> {report_path}")
    print(f"json   -> {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
