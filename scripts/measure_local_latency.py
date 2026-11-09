#!/usr/bin/env python3
"""Measure the latency profile that the paper/live strategies actually see.

Run this on the India PC before trusting a local soak:

    uv run python scripts/measure_local_latency.py --samples 100

It measures DNS, TCP, TLS and HTTP time-to-first-byte for:
  * Polymarket CLOB   GET  /time
  * Polymarket CLOB   POST /books   (one live token)
  * Polymarket Gamma  GET  /markets?limit=1
  * Binance Futures   GET  /fapi/v1/time

The report is written to data/derived/local_latency.json and logged as a
Markdown table. No credentials are used. Run it at peak and off-peak hours:
residential India -> London routes vary by ISP, time of day and jitter.
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from polymarketbot.logs import get_logger

log = get_logger("measure_local_latency")

DEFAULT_CLOB = "https://clob.polymarket.com"
DEFAULT_GAMMA = "https://gamma-api.polymarket.com"
DEFAULT_BINANCE = "https://fapi.binance.com"


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def _jitter(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.fmean(abs(b - a) for a, b in zip(values, values[1:]))


def _socket_timings(host: str, port: int = 443, timeout: float = 8.0) -> Dict[str, float]:
    """DNS -> TCP -> TLS, in milliseconds; failures return NaN-like 0.0."""
    result = {"dns_ms": 0.0, "tcp_ms": 0.0, "tls_ms": 0.0}
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        result["dns_ms"] = (time.perf_counter() - t0) * 1000.0
        family, socktype, proto, _, sockaddr = infos[0]
        t1 = time.perf_counter()
        raw = socket.socket(family, socktype, proto)
        raw.settimeout(timeout)
        raw.connect(sockaddr)
        result["tcp_ms"] = (time.perf_counter() - t1) * 1000.0
        t2 = time.perf_counter()
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(raw, server_hostname=host)
        result["tls_ms"] = (time.perf_counter() - t2) * 1000.0
        tls.close()
    except Exception as exc:  # noqa: BLE001 - report the failed sample
        log.debug("socket probe {} failed: {}", host, exc)
    return result


def _http_probe(
    session: requests.Session,
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """HTTP TTFB and total time; stream so the body is not downloaded."""
    t0 = time.perf_counter()
    try:
        resp = session.request(method, url, json=body, stream=True, timeout=10.0)
        ttfb = (time.perf_counter() - t0) * 1000.0
        status = resp.status_code
        # Force headers only; close without reading a large /books body.
        resp.close()
        return {"ok": True, "status": status, "ttfb_ms": ttfb, "total_ms": ttfb}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": 0,
            "ttfb_ms": (time.perf_counter() - t0) * 1000.0,
            "total_ms": (time.perf_counter() - t0) * 1000.0,
            "error": str(exc),
        }


def _pick_token(session: requests.Session) -> Optional[str]:
    """One live CLOB token for the /books probe; no hardcoded market id."""
    try:
        r = session.get(
            f"{DEFAULT_GAMMA}/markets",
            params={"limit": 1, "active": "true", "closed": "false"},
            timeout=10.0,
        )
        if r.status_code != 200:
            log.warning("Gamma token discovery HTTP {}; /books probe skipped", r.status_code)
            return None
        markets = r.json()
        if not isinstance(markets, list) or not markets:
            return None
        raw_tokens = markets[0].get("clobTokenIds")
        tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else (raw_tokens or [])
        return str(tokens[0]) if tokens else None
    except Exception as exc:  # noqa: BLE001
        log.warning("Gamma token discovery failed: {}", exc)
        return None


def _summarize(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    ok = [s for s in samples if s.get("ok")]
    ttfb = [float(s["ttfb_ms"]) for s in ok]
    dns = [float(s.get("dns_ms", 0.0)) for s in samples]
    tcp = [float(s.get("tcp_ms", 0.0)) for s in samples]
    tls = [float(s.get("tls_ms", 0.0)) for s in samples]
    return {
        "samples": len(samples),
        "ok": len(ok),
        "loss_pct": round(100.0 * (1.0 - len(ok) / max(1, len(samples))), 2),
        "ttfb_p50_ms": round(_percentile(ttfb, 50), 2),
        "ttfb_p95_ms": round(_percentile(ttfb, 95), 2),
        "ttfb_p99_ms": round(_percentile(ttfb, 99), 2),
        "ttfb_jitter_ms": round(_jitter(ttfb), 2),
        "dns_p50_ms": round(_percentile(dns, 50), 2),
        "tcp_p50_ms": round(_percentile(tcp, 50), 2),
        "tls_p50_ms": round(_percentile(tls, 50), 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure India -> Polymarket/Binance latency")
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--out", type=Path, default=Path("data/derived/local_latency.json"))
    parser.add_argument("--sleep", type=float, default=0.20)
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": "polymarketbot-latency-probe/1.0"})
    token = _pick_token(session)
    endpoints = [
        {"name": "clob_time", "host": "clob.polymarket.com", "method": "GET", "url": f"{DEFAULT_CLOB}/time"},
        {"name": "gamma_markets", "host": "gamma-api.polymarket.com", "method": "GET", "url": f"{DEFAULT_GAMMA}/markets?limit=1"},
        {"name": "binance_time", "host": "fapi.binance.com", "method": "GET", "url": f"{DEFAULT_BINANCE}/fapi/v1/time"},
    ]
    if token:
        endpoints.append(
            {
                "name": "clob_books",
                "host": "clob.polymarket.com",
                "method": "POST",
                "url": f"{DEFAULT_CLOB}/books",
                "body": [{"token_id": token}],
            }
        )

    report: Dict[str, Any] = {"ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "endpoints": {}}
    log.info("Latency probe: {} samples/endpoint, token={}...", args.samples, token or "none")
    for ep in endpoints:
        samples: List[Dict[str, Any]] = []
        for _ in range(max(1, args.samples)):
            timing = _socket_timings(ep["host"])
            http = _http_probe(session, ep["method"], ep["url"], ep.get("body"))
            http.update(timing)
            samples.append(http)
            time.sleep(max(0.0, args.sleep))
        summary = _summarize(samples)
        report["endpoints"][ep["name"]] = summary
        log.info(
            "{:<14} p50={:>7.2f}ms p95={:>7.2f}ms p99={:>7.2f}ms "
            "jitter={:>6.2f}ms loss={:>5.2f}% dns={:>5.2f} tcp={:>5.2f} tls={:>5.2f}",
            ep["name"],
            summary["ttfb_p50_ms"],
            summary["ttfb_p95_ms"],
            summary["ttfb_p99_ms"],
            summary["ttfb_jitter_ms"],
            summary["loss_pct"],
            summary["dns_p50_ms"],
            summary["tcp_p50_ms"],
            summary["tls_p50_ms"],
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info("Wrote {}", args.out)
    return 0 if all(v["ok"] > 0 for v in report["endpoints"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
