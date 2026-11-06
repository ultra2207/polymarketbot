#!/usr/bin/env python3
"""Run the implemented ideas serially and record what each one actually did.

The scheduler is the only consumer of the CPU: a replay of 500 markets over ten
minutes takes seconds, but two replays at once on one core take longer than the
same two in order. It reads ``ideas/registry.json``, runs every idea whose
status is ``implemented`` through ``scripts/replay_all_markets.py`` on one
shared slice (a shared slice is what makes the comparison paired), and writes
``ideas/<slug>/result.json`` plus the registry status.

    .venv/Scripts/python.exe scripts/run_idea_queue.py --minutes 10 --baseline queue_aware_ofi

An idea is compared with the baseline on the same slice and the same markets.
Promotion is a statistic, not an opinion: the gates live in ``plan.md`` §6 and
are enforced here, not by whoever wrote the module.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarketbot.logs import get_logger

log = get_logger("run_idea_queue")
IDEAS = REPO_ROOT / "ideas"
REGISTRY = IDEAS / "registry.json"
REPLAY = REPO_ROOT / "scripts" / "replay_all_markets.py"
DERIVED = REPO_ROOT / "data" / "derived" / "replay"

MIN_FILLS = 200
MIN_CONDITIONS = 50
MIN_T = 2.0
MAX_CONCENTRATION = 0.25


def load_registry(path: Path = REGISTRY) -> dict[str, Any]:
    if not path.exists():
        return {"schema": 1, "ideas": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_registry(data: dict[str, Any], path: Path = REGISTRY) -> None:
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def pending(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for i in data.get("ideas", []) if i.get("status") == "implemented"]


def slice_start(args: argparse.Namespace, offset: int) -> str:
    """ISO start a given number of seconds before the newest tape timestamp."""
    import duckdb

    newest = duckdb.connect().execute(
        "select max(venue_ts_ms) from read_parquet('data/tape/clob_events/**/*.parquet')"
    ).fetchone()[0]
    import math

    end = math.floor(float(newest) / 1000.0 / 300.0) * 300.0 - float(offset)
    from datetime import datetime, UTC

    return datetime.fromtimestamp(end - args.minutes * 60.0, UTC).strftime("%Y-%m-%dT%H:%M")


def run_replay(strategy: str, args: argparse.Namespace, tag: str, out: Path) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(REPLAY),
        "--minutes", str(args.minutes),
        "--strategies", strategy,
        "--order-by", args.order_by,
        "--max-markets", str(args.max_markets),
        "--tag", tag,
        "--out", str(out),
    ]
    if args.start_utc:
        cmd += ["--start-utc", args.start_utc]
    if args.flat:
        cmd += ["--flat", str(args.flat)]
    log.info(f"replay {strategy} -> {out}")
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=args.timeout, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"replay failed for {strategy}: {result.stderr[-800:]}")
    return json.loads((out / "summary.json").read_text(encoding="utf-8"))


def paired(idea: Path, base: Path) -> dict[str, Any]:
    """Paired per-condition difference against the baseline on the same slice."""
    a = pd.read_parquet(idea / "metrics.parquet").set_index("condition_id")
    b = pd.read_parquet(base / "metrics.parquet").set_index("condition_id")
    common = a.index.intersection(b.index)
    diff = (a.loc[common, "net"] - b.loc[common, "net"]).astype(float)
    n = len(diff)
    mean = float(diff.mean()) if n else 0.0
    sd = float(diff.std(ddof=1)) if n > 1 else 0.0
    t = mean / (sd / n**0.5) if sd > 0 and n > 1 else 0.0
    total = float(diff.sum())
    top = float(diff.abs().max()) if n else 0.0
    concentration = top / abs(total) if total else 1.0
    return {
        "conditions": n,
        "mean_diff": round(mean, 5),
        "t": round(t, 3),
        "total_diff": round(total, 4),
        "concentration": round(concentration, 3),
    }


def evaluate(idea_dir: Path, idea_metrics: dict[str, Any], base_metrics: dict[str, Any], pair: dict[str, Any]) -> dict[str, Any]:
    name = next(iter(idea_metrics["strategies"]))
    stats = idea_metrics["strategies"][name]
    gates = {
        "fills_and_conditions": stats["fills"] >= MIN_FILLS and pair["conditions"] >= MIN_CONDITIONS,
        "beats_baseline": pair["mean_diff"] > 0 and pair["t"] >= MIN_T,
        "not_concentrated": pair["concentration"] <= MAX_CONCENTRATION,
    }
    # An idea that changed nothing was not tested, it was inert. Calling that a
    # "fail" hides a broken trigger behind a statistic.
    inert = abs(pair["total_diff"]) < 1e-12 and pair["t"] == 0.0
    verdict = "pass" if all(gates.values()) else ("inert" if inert else "fail")
    return {
        "strategy": name,
        "stats": stats,
        "paired": pair,
        "gates": gates,
        "verdict": verdict,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=10)
    parser.add_argument("--start-utc", default=None)
    parser.add_argument("--order-by", default="liquidity", choices=("pool", "liquidity", "volume"))
    parser.add_argument("--max-markets", type=int, default=500)
    parser.add_argument("--baseline", default="queue_aware_ofi")
    parser.add_argument("--flat", type=float, default=0.0)
    parser.add_argument("--timeout", type=int, default=280, help="per-replay cap, below the 300 s rule")
    parser.add_argument("--limit", type=int, default=0, help="run at most N pending ideas")
    parser.add_argument("--only", default="", help="comma-separated slugs; overrides status filtering")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--slice-offsets",
        default="0",
        help="comma-separated seconds before the newest data; promotion requires every slice",
    )
    args = parser.parse_args(argv)

    registry = load_registry()
    queue = pending(registry)
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        queue = [i for i in registry.get("ideas", []) if i.get("slug") in wanted]
    if args.limit:
        queue = queue[: args.limit]
    if not queue:
        log.info("no implemented ideas to run")
        return 0
    log.info(f"{len(queue)} implemented idea(s); baseline={args.baseline}")

    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M")
    base_dir = DERIVED / f"{stamp}_{args.minutes}m_{args.baseline}"
    frozen = bool(args.start_utc)
    offsets = [0] if frozen else [int(o) for o in str(args.slice_offsets).split(",") if o.strip()]
    frozen_start = args.start_utc
    base_dirs: dict[int, Path] = {}
    base_summaries: dict[int, dict[str, Any]] = {}
    for offset in offsets:
        args.start_utc = frozen_start if frozen else slice_start(args, offset)
        base_dir = DERIVED / f"{stamp}_{args.minutes}m_o{offset}_{args.baseline}"
        base_summaries[offset] = run_replay(
            args.baseline, args, f"{stamp}_{args.minutes}m_o{offset}_{args.baseline}", base_dir
        )
        base_dirs[offset] = base_dir
        log.info(
            f"baseline[{offset}] start={args.start_utc} net="
            f"{base_summaries[offset]['strategies'][args.baseline]['net']}"
        )

    for idea in queue:
        slug = str(idea["slug"])
        per_slice: list[dict[str, Any]] = []
        verdict = "pass"
        try:
            for offset in offsets:
                args.start_utc = frozen_start if frozen else slice_start(args, offset)
                idea_dir = DERIVED / f"{stamp}_{args.minutes}m_o{offset}_{slug}"
                summary = run_replay(
                    slug, args, f"{stamp}_{args.minutes}m_o{offset}_{slug}", idea_dir
                )
                pair = paired(idea_dir, base_dirs[offset])
                result = evaluate(idea_dir, summary, base_summaries[offset], pair)
                result["slice"] = summary["slice"]
                result["offset"] = offset
                per_slice.append(result)
                if result["verdict"] == "inert":
                    verdict = "inert"
                elif result["verdict"] != "pass":
                    verdict = "fail"
                log.info(
                    f"{slug}[{offset}] net_diff={pair['mean_diff']} t={pair['t']} "
                    f"({result['verdict']})"
                )
        except Exception as exc:  # noqa: BLE001 - one broken idea must not stop the queue
            log.warning(f"{slug}: {exc}")
            idea["status"] = "error"
            idea["error"] = str(exc)[:400]
            save_registry(registry)
            continue
        combined = {
            "strategy": slug,
            "slices": per_slice,
            "verdict": verdict,
            "finished_utc": datetime.now(tz=UTC).isoformat(),
        }
        (IDEAS / slug / "result.json").write_text(json.dumps(combined, indent=1), encoding="utf-8")
        idea["status"] = "done"
        idea["verdict"] = verdict
        idea["result"] = f"data/derived/replay/{stamp}_{args.minutes}m_o{offsets[0]}_{slug}"
        save_registry(registry)
        log.info(f"{slug}: verdict={verdict} over {len(per_slice)} slice(s)")
        if args.dry_run:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
