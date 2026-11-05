#!/usr/bin/env python3
"""Measure our Binance-trigger -> CLOB cancellation path on this host.

Gate 3 asks whether our cancellation reaches the matching engine 100 ms faster
than the median taker snipe. The taker's latency is measured from the tape
(``scripts/measure_lead_time.py``); ours is measured here, without placing an
order:

* **Local leg** - the L2 cancellation auth: HMAC-SHA256 over
  ``timestamp + method + path + body``, base64 encoded. This is the only
  cryptographic work between the Binance tick and the socket write; the
  decision path itself is a book check, sub-millisecond.
* **Network leg** - the CLOB API round trip measured by ``netclock`` against
  ``/time`` with a warmed session. The forward (send) leg is at most one full
  RTT; using the RTT is therefore an upper bound on our one-way path.

``our_path_upper_ms = sign_p99_ms + min_rtt_ms``. Feed that number into

    scripts/measure_lead_time.py --our-path-ms <value>

and the study tests ``taker_p50 - our_path >= 100 ms`` with the taker's clock
bracket subtracted from the lower bound. The script never sends an order or a
cancel; it needs no key. A real cancel RTT can be measured separately with the
``DedicatedCancellationPool`` when a live order exists.

Usage:
    .venv/Scripts/python.exe scripts/bench_cancel_path.py
    .venv/Scripts/python.exe scripts/bench_cancel_path.py --count 20 --iterations 2000
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for extra in (REPO_ROOT, REPO_ROOT / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from polymarketbot.logs import get_logger
from polymarketbot.net import DEFAULT_HOSTS, install_doh_fallback, warm
from polymarketbot.netclock import best_sample, probe

log = get_logger("bench_cancel_path")

DEFAULT_ITERATIONS = 1000
DEFAULT_COUNT = 10


def sign_cancel(secret: bytes, timestamp: str, body: str = "") -> float:
    """Seconds for one cancel-auth HMAC, matching the CLOB L2 scheme."""
    message = f"{timestamp}DELETE/order{body}".encode()
    t0 = time.perf_counter_ns()
    hmac.new(secret, message, hashlib.sha256).digest()
    return (time.perf_counter_ns() - t0) / 1e9


def local_sign_ms(iterations: int, secret: bytes | None = None) -> dict[str, float]:
    """p50/p99 of the cancel-auth HMAC over ``iterations`` runs, in ms."""
    key = secret or os.urandom(32)
    samples = [sign_cancel(key, str(1793850000 + i)) * 1000.0 for i in range(iterations)]
    samples.sort()
    return {
        "p50_ms": round(statistics.median(samples), 4),
        "p99_ms": round(samples[int(0.99 * (len(samples) - 1))], 4),
        "max_ms": round(samples[-1], 4),
        "iterations": float(iterations),
    }


def path_upper_ms(sign_p99_ms: float, min_rtt_ms: float) -> float:
    """Our send-to-engine upper bound: local signing plus at most one RTT."""
    return round(float(sign_p99_ms) + float(min_rtt_ms), 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help="CLOB /time probes")
    parser.add_argument("--interval", type=float, default=0.2, help="seconds between probes")
    parser.add_argument("--out", default="data/derived/cancel_path.json")
    args = parser.parse_args(argv)

    install_doh_fallback()
    warm(DEFAULT_HOSTS)

    secret_text = os.environ.get("POLYMARKET_SECRET", "")
    secret = base64.b64decode(secret_text) if secret_text else None
    local = local_sign_ms(max(10, args.iterations), secret)

    samples = probe(venues=["clob"], count=max(3, args.count), interval_seconds=args.interval)
    best = best_sample(samples)
    if best is None:
        log.error("no CLOB /time sample; cannot measure the network leg")
        return 1
    min_rtt_ms = round(best.rtt_ns / 1e6, 1)
    upper = path_upper_ms(local["p99_ms"], min_rtt_ms)

    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "host": os.environ.get("COMPUTERNAME", ""),
        "local_sign_ms": local,
        "clob_probes": len(samples),
        "clob_min_rtt_ms": min_rtt_ms,
        "our_path_upper_ms": upper,
        "note": (
            "Local sign + one full RTT is an upper bound on the Binance-trigger to "
            "CLOB-send path; the decision is a book check. No order or cancel was sent. "
            "Pass this value to measure_lead_time.py --our-path-ms."
        ),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    log.info(
        f"sign p50={local['p50_ms']:.3f} ms p99={local['p99_ms']:.3f} ms; "
        f"CLOB min rtt={min_rtt_ms:.1f} ms; path upper={upper:.1f} ms -> {out}"
    )
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
