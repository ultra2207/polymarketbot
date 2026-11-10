#!/usr/bin/env python3
"""Continuously measure venue clock offsets into data/tape/clock_offset.

The collector streams venue timestamps; the analysis needs the local-vs-venue
offset to interpret cross-venue timing. The latency measurements write samples
only when they run, so this lightweight service probes CLOB and Binance every
15 minutes and appends one Parquet part per probe.

Usage:
    .venv/Scripts/python.exe -m polymarketbot.ops.clock_probe
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback
from polymarketbot.netclock import probe, record_samples

log = get_logger("clock_probe", file="clock_probe")
REPO_ROOT = Path(__file__).resolve().parents[3]
INTERVAL_SECONDS = 900.0
COUNT = 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

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
