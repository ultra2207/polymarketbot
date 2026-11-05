#!/usr/bin/env python3
"""Continuously measure venue clock offsets into data/tape/clock_offset.

The collector streams venue timestamps; the analysis needs the local-vs-venue
offset to interpret cross-venue timing. `scripts/measure_lead_time.py` writes
samples only when it runs, so this lightweight service probes CLOB and Binance
every 15 minutes and appends one Parquet part per probe.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarketbot.logs import get_logger  # noqa: E402
from polymarketbot.net import install_doh_fallback  # noqa: E402
from polymarketbot.netclock import probe, record_samples  # noqa: E402

log = get_logger("clock_probe")
INTERVAL_SECONDS = 900.0
COUNT = 5


def main() -> int:
    install_doh_fallback()
    log.info(f"clock probe started: interval={INTERVAL_SECONDS:.0f}s count={COUNT}")
    while True:
        started = time.time()
        try:
            samples = probe(count=COUNT, interval_seconds=0.5)
            path = record_samples(samples, REPO_ROOT / "data" / "tape")
            offsets = ", ".join(
                f"{s.stream}={s.offset_ns / 1e6:+.1f}ms/rtt={s.rtt_ns / 1e6:.0f}ms"
                for s in samples
            )
            log.info(f"clock probe: {len(samples)} samples -> {path} [{offsets}]")
        except Exception as exc:  # noqa: BLE001 - the probe must survive outages
            log.warning(f"clock probe failed: {exc}")
        time.sleep(max(1.0, INTERVAL_SECONDS - (time.time() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
