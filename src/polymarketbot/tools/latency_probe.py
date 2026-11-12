"""Measure the latency profile that the paper/live strategies actually see.

Run this on the India PC before trusting a local soak:

    uv run python -m polymarketbot.tools.latency_probe --samples 100

It measures DNS, TCP, TLS and HTTP time-to-first-byte for:
  * Polymarket CLOB   GET  /time
  * Polymarket CLOB   POST /books   (one live token)
  * Polymarket Gamma  GET  /markets?limit=1
  * Binance Futures   GET  /fapi/v1/time
  * Polygon RPC       POST eth_blockNumber (the on-chain leg's own clock)

The report is written to data/derived/local_latency.json and logged as a
Markdown table. No credentials are used. Run it at peak and off-peak hours:
residential India -> London routes vary by ISP, time of day and jitter.

Name resolution goes through polymarketbot.net.install_doh_fallback() by
default, because the machine's resolver fails getaddrinfo in bursts and that
failure otherwise shows up as request loss. Pass --raw-dns to measure the
unfiltered path instead; that is the run in which failures are DNS, not wire.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import socket
import ssl
import statistics
import time
from pathlib import Path
from typing import Any

import requests

from polymarketbot.logs import get_logger
from polymarketbot.net import install_doh_fallback

log = get_logger("measure_local_latency")

DEFAULT_CLOB = "https://clob.polymarket.com"
DEFAULT_GAMMA = "https://gamma-api.polymarket.com"
DEFAULT_BINANCE = "https://fapi.binance.com"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def _jitter(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.fmean(abs(b - a) for a, b in itertools.pairwise(values))


def _socket_timings(host: str, port: int = 443, timeout: float = 8.0) -> dict[str, float | None]:
    """DNS -> TCP -> TLS, in milliseconds; an unfinished stage is ``None``.

    A failed lookup used to report ``0.0``, which then entered the sample and
    pulled the reported DNS/TCP/TLS medians toward zero exactly in the runs with
    the most loss - the opposite of what the probe exists to show.
    """
    result: dict[str, float | None] = {"dns_ms": None, "tcp_ms": None, "tls_ms": None}
    raw: socket.socket | None = None
    tls: Any = None
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
        raw = None
        result["tls_ms"] = (time.perf_counter() - t2) * 1000.0
    except Exception as exc:  # noqa: BLE001 - report the failed sample
        log.debug("socket probe {}:{} failed: {}", host, port, exc)  # noqa: PLE1205 - loguru str.format
    finally:
        for sock in (tls, raw):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    return result


def _http_probe(
    session: requests.Session,
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
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


def _pick_token(session: requests.Session) -> str | None:
    """One live CLOB token for the /books probe; no hardcoded market id."""
    try:
        r = session.get(
            f"{DEFAULT_GAMMA}/markets",
            params={"limit": 1, "active": "true", "closed": "false"},
            timeout=10.0,
        )
        if r.status_code != 200:
            log.warning(  # noqa: PLE1205 - loguru str.format
                "Gamma token discovery HTTP {}; /books probe skipped",
                r.status_code,
            )
            return None
        markets = r.json()
        if not isinstance(markets, list) or not markets:
            return None
        raw_tokens = markets[0].get("clobTokenIds")
        tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else (raw_tokens or [])
        return str(tokens[0]) if tokens else None
    except Exception as exc:  # noqa: BLE001
        log.warning("Gamma token discovery failed: {}", exc)  # noqa: PLE1205 - loguru str.format
        return None


def _summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [s for s in samples if s.get("ok")]
    ttfb = [float(s["ttfb_ms"]) for s in ok]

    def stage(key: str) -> list[float]:
        """Only completed stages: an unmeasured one is absent, never zero."""
        return [float(s[key]) for s in samples if s.get(key) is not None]

    def p50(key: str) -> float | None:
        values = stage(key)
        return round(_percentile(values, 50), 2) if values else None

    return {
        "samples": len(samples),
        "ok": len(ok),
        "loss_pct": round(100.0 * (1.0 - len(ok) / max(1, len(samples))), 2),
        "ttfb_p50_ms": round(_percentile(ttfb, 50), 2),
        "ttfb_p95_ms": round(_percentile(ttfb, 95), 2),
        "ttfb_p99_ms": round(_percentile(ttfb, 99), 2),
        "ttfb_jitter_ms": round(_jitter(ttfb), 2),
        "dns_p50_ms": p50("dns_ms"),
        "tcp_p50_ms": p50("tcp_ms"),
        "tls_p50_ms": p50("tls_ms"),
        "dns_samples": len(stage("dns_ms")),
        "tcp_samples": len(stage("tcp_ms")),
        "tls_samples": len(stage("tls_ms")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure India -> Polymarket/Binance latency")
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--out", type=Path, default=Path("data/derived/local_latency.json"))
    parser.add_argument("--sleep", type=float, default=0.20)
    parser.add_argument(
        "--raw-dns",
        action="store_true",
        help="Use the system resolver instead of the DoH/IPv4-pinned fallback (diagnoses ISP DNS)",
    )
    args = parser.parse_args()

    if not args.raw_dns:
        install_doh_fallback()
    log.info(  # noqa: PLE1205 - loguru str.format
        "name resolution: {}",
        "system resolver (--raw-dns)" if args.raw_dns else "DoH 1.1.1.1, IPv4 pinned",
    )

    session = requests.Session()
    session.headers.update({"User-Agent": "polymarketbot-latency-probe/1.0"})
    token = _pick_token(session)
    endpoints = [
        {"name": "clob_time", "host": "clob.polymarket.com", "port": 443, "method": "GET", "url": f"{DEFAULT_CLOB}/time"},
        {"name": "gamma_markets", "host": "gamma-api.polymarket.com", "port": 443, "method": "GET", "url": f"{DEFAULT_GAMMA}/markets?limit=1"},
        {"name": "binance_time", "host": "fapi.binance.com", "port": 443, "method": "GET", "url": f"{DEFAULT_BINANCE}/fapi/v1/time"},
    ]
    if token:
        endpoints.append(
            {
                "name": "clob_books",
                "host": "clob.polymarket.com",
                "port": 443,
                "method": "POST",
                "url": f"{DEFAULT_CLOB}/books",
                "body": [{"token_id": token}],
            }
        )

    # Polygon RPC: the clock the on-chain leg (merge/split/redeem) actually
    # pays. ``polymarketbot.strategy.onchain.ctf.load_persisted_rpc_rtt_s`` looks for
    # this exact name; without it the on-chain leg silently falls back to the
    # CLOB HTTP round trip, which is a different network path.
    try:
        from urllib.parse import urlsplit

        from polymarketbot.strategy.onchain.core import rpc_url

        _rpc_endpoint, _ = rpc_url()
        parsed = urlsplit(_rpc_endpoint)
        endpoints.append(
            {
                "name": "polygon_rpc",
                # ``netloc`` can carry ``:port``, and getaddrinfo("host:8545")
                # fails, which silently zeroed this endpoint's socket stages.
                "host": parsed.hostname or parsed.netloc,
                "port": parsed.port or 443,
                "method": "POST",
                "url": _rpc_endpoint,
                "body": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_blockNumber",
                    "params": [],
                },
            }
        )
    except Exception as exc:  # noqa: BLE001 - probe is additive, never fatal
        log.warning(  # noqa: PLE1205 - loguru str.format
            "Polygon RPC endpoint unresolved, RPC latency not measured: {}",
            exc,
        )

    report: dict[str, Any] = {"ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "endpoints": {}}
    log.info(  # noqa: PLE1205 - loguru str.format
        "Latency probe: {} samples/endpoint, token={}...",
        args.samples,
        token or "none",
    )
    for ep in endpoints:
        samples: list[dict[str, Any]] = []
        for _ in range(max(1, args.samples)):
            timing = _socket_timings(ep["host"], ep.get("port", 443))
            http = _http_probe(session, ep["method"], ep["url"], ep.get("body"))
            http.update(timing)
            samples.append(http)
            time.sleep(max(0.0, args.sleep))
        summary = _summarize(samples)
        report["endpoints"][ep["name"]] = summary

        def _ms(value: float | None) -> str:
            return "   n/a" if value is None else f"{value:>7.2f}"

        log.info(  # noqa: PLE1205 - loguru str.format
            "{:<14} p50={}ms p95={}ms p99={}ms "
            "jitter={:>6.2f}ms loss={:>5.2f}% dns={} tcp={} tls={}",
            ep["name"],
            _ms(summary["ttfb_p50_ms"]),
            _ms(summary["ttfb_p95_ms"]),
            _ms(summary["ttfb_p99_ms"]),
            summary["ttfb_jitter_ms"],
            summary["loss_pct"],
            _ms(summary["dns_p50_ms"]),
            _ms(summary["tcp_p50_ms"]),
            _ms(summary["tls_p50_ms"]),
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: a kill mid-write must not leave the report truncated, because
    # `load_persisted_rpc_rtt_s` reads it for the on-chain flight time.
    tmp = args.out.with_name(f".{args.out.name}.tmp")
    tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
    os.replace(tmp, args.out)
    log.info("Wrote {}", args.out)  # noqa: PLE1205 - loguru str.format
    return 0 if all(v["ok"] > 0 for v in report["endpoints"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
