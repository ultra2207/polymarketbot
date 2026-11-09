#!/usr/bin/env python3
"""Refresh event/fee metadata for the live tape universe.

Fetches the manifest's condition ids from Gamma in 100-id batches (25 requests
for 2,496 markets), writes ``data/derived/universe_metadata.parquet``, and
backfills ``data/tape/manifest_current.json`` with:

* ``event_id`` / ``event_slug`` / ``event_title`` - risk grouping,
* ``fee_type`` / ``fee_rate`` / ``fee_exponent`` / ``fee_rebate`` - fee netting,
* ``resolution_source``, ``tick_size``, ``min_order_size``, ``tags``.

Run it after the universe refresh (or daily); it is idempotent. A smoke run:

    .venv/Scripts/python.exe scripts/refresh_universe_metadata.py --limit 20
    .venv/Scripts/python.exe scripts/refresh_universe_metadata.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from polymarketbot.data.universe_metadata import (
    MANIFEST_DEFAULT,
    META_PATH,
    refresh,
)
from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback

log = get_logger("refresh_universe_metadata")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(MANIFEST_DEFAULT))
    parser.add_argument("--out", default=str(META_PATH))
    parser.add_argument("--limit", type=int, default=0, help="smoke: first N ids")
    args = parser.parse_args()
    # Task Scheduler starts with C:\Windows\System32 as the working dir.
    os.chdir(REPO_ROOT)
    install_doh_fallback()
    stats = refresh(args.manifest, args.out, limit=args.limit or None)
    log.info(
        f"ids={stats['ids']} rows={stats['rows']} "
        f"written={stats['written']} manifest_updated={stats['applied']}"
    )
    return 0 if stats["rows"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
