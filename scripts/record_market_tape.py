#!/usr/bin/env python3
"""Record an hourly Parquet market tape for one Polymarket condition.

Example:
    .venv/Scripts/python.exe scripts/record_market_tape.py \
        --condition-id 0x... --token-yes 123 --token-no 456 \
        --duration 60 --out data/tape

The recorder is observational only: it subscribes to the public feed, samples
the book at 1 Hz and flushes one Parquet file per (condition, UTC hour).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarketbot.logs import get_logger  # noqa: E402
from polymarketbot.simulator.tape import MarketTapeRecorder  # noqa: E402

log = get_logger("record_market_tape")

FIRST_RUN_CAP_SECONDS = 300.0


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition-id", required=True)
    parser.add_argument("--token-yes", required=True)
    parser.add_argument("--token-no", required=True)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--out", default="data/tape")
    parser.add_argument("--flush-interval", type=float, default=60.0)
    parser.add_argument(
        "--allow-long",
        action="store_true",
        help="Bypass the 300 s first-run cap",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    duration = max(1.0, float(args.duration))
    if duration > FIRST_RUN_CAP_SECONDS and not args.allow_long:
        log.warning(
            f"New recorder first run capped at {FIRST_RUN_CAP_SECONDS:.0f}s "
            f"(requested {duration:.0f}s); pass --allow-long for a longer run"
        )
        duration = FIRST_RUN_CAP_SECONDS

    recorder = MarketTapeRecorder(
        condition_id=args.condition_id,
        token_yes=args.token_yes,
        token_no=args.token_no,
        out_dir=args.out,
        flush_interval=args.flush_interval,
    )
    existing = recorder.existing_hours()
    if existing:
        log.info(f"Skipping {len(existing)} hour file(s) already present: {sorted(existing)}")

    log.info(
        f"Recording tape for {args.condition_id[:16]}... for {duration:.0f}s -> {recorder.out_dir}"
    )
    try:
        asyncio.run(recorder.run(duration=duration))
    except KeyboardInterrupt:
        log.warning("Interrupted; flushing buffered tape rows")
        recorder.flush()

    files = [p for p in recorder.output_files() if p.exists()]
    for path in files:
        log.info(path)
    if not files:
        log.warning("No tape file was written (no book samples captured)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
