#!/usr/bin/env python3
"""Materialise one idea's exogenous signals from the tape.

A module may only see its `DecisionContext`, so any idea that needs more than the
book (Binance depth, Kalshi basis, funding clock, UMA phase...) ships a
`prepare.sql` that produces a signal table. This runner executes it and writes
`data/derived/ideas/<slug>/signals.parquet`, which the replay harness joins
as-of by `(asset_id, ts_ms)` - never forward, so a strategy cannot see the
future.

    .venv/Scripts/python.exe scripts/build_idea_signals.py --slug cross-kalshi-basis-side-pull

Convention for the SQL: select `asset_id varchar`, `ts_ms bigint` (venue epoch
milliseconds) and one numeric column per signal the module reads from
`ctx.signals`. `${TAPE}` expands to the tape root.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarketbot.logs import get_logger

log = get_logger("build_idea_signals")

IDEAS = REPO_ROOT / "ideas"
OUT = REPO_ROOT / "data" / "derived" / "ideas"
TAPE = REPO_ROOT / "data" / "tape"
REQUIRED = ("asset_id", "ts_ms")


def validate(frame: pd.DataFrame) -> None:
    missing = [c for c in REQUIRED if c not in frame.columns]
    if missing:
        raise ValueError(f"signals must select {REQUIRED}; missing {missing}")
    signals = [c for c in frame.columns if c not in REQUIRED]
    if not signals:
        raise ValueError("signals table has no numeric columns beyond asset_id/ts_ms")
    non_numeric = [c for c in signals if not pd.api.types.is_numeric_dtype(frame[c])]
    if non_numeric:
        raise ValueError(f"signal columns must be numeric: {non_numeric}")
    if frame.duplicated(subset=list(REQUIRED)).any():
        raise ValueError("signals contain duplicate (asset_id, ts_ms) rows")
    if frame.empty:
        raise ValueError("signals table is empty")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--sql", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=0, help="optional row cap for a first pass")
    args = parser.parse_args(argv)

    sql_path = args.sql or (IDEAS / args.slug / "prepare.sql")
    if not sql_path.exists():
        log.error(f"{sql_path} does not exist; this idea needs no prepared signals")
        return 1
    sql = sql_path.read_text(encoding="utf-8").replace("${TAPE}", TAPE.as_posix())
    if args.limit:
        sql = f"select * from ({sql}) limit {int(args.limit)}"

    log.info(f"running {sql_path.name} for {args.slug}")
    con = duckdb.connect()
    frame = con.execute(sql).fetchdf()
    validate(frame)
    out = args.out or (OUT / args.slug / "signals.parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out, index=False)
    log.info(f"wrote {out} rows={len(frame)} signals={[c for c in frame.columns if c not in REQUIRED]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
