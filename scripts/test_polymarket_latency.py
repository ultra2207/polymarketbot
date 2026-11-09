#!/usr/bin/env python3
"""Comprehensive Polymarket venue latency benchmark.

Measures:
1. ICMP ping (raw network layer)
2. DNS resolution time
3. TCP & TLS handshake latency
4. Cold vs Warm Keep-Alive HTTP REST latency (CLOB /time, /version, Gamma /events)
5. WebSocket connection & Ping/Pong frame RTT (ws-subscriptions-clob)
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import statistics
import time
from typing import Dict, List, Tuple
from urllib.parse import urlparse

import requests
import websockets
from polymarketbot.logs import get_logger
log = get_logger(__name__)

TARGET_HOSTS = {
    "CLOB REST": "https://clob.polymarket.com",
    "CLOB Time": "https://clob.polymarket.com/time",
    "CLOB Version": "https://clob.polymarket.com/version",
    "CLOB Sampling Markets": "https://clob.polymarket.com/sampling-markets",
    "Gamma API Events": "https://gamma-api.polymarket.com/events?limit=1",
}

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def measure_dns(host: str, samples: int = 5) -> List[float]:
    rtts = []
    for _ in range(samples):
        t0 = time.perf_counter()
        socket.getaddrinfo(host, 443, family=socket.AF_INET)
        t1 = time.perf_counter()
        rtts.append((t1 - t0) * 1000.0)
    return rtts


def measure_tcp_tls(host: str, samples: int = 5) -> Tuple[List[float], List[float]]:
    tcp_times = []
    tls_times = []
    context = ssl.create_default_context()

    for _ in range(samples):
        # 1. TCP
        t0 = time.perf_counter()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((host, 443))
        t1 = time.perf_counter()
        tcp_times.append((t1 - t0) * 1000.0)

        # 2. TLS
        t2 = time.perf_counter()
        ssock = context.wrap_socket(sock, server_hostname=host)
        t3 = time.perf_counter()
        tls_times.append((t3 - t2) * 1000.0)

        ssock.close()

    return tcp_times, tls_times


def measure_http_rest(url: str, warm_samples: int = 15) -> Tuple[float, List[float]]:
    # Cold request (creates new TCP connection)
    with requests.Session() as cold_session:
        t0 = time.perf_counter()
        r_cold = cold_session.get(url, timeout=5.0)
        t1 = time.perf_counter()
        cold_rtt = (t1 - t0) * 1000.0

    # Warm requests (uses persistent HTTP/1.1 keep-alive connection)
    warm_rtts = []
    with requests.Session() as warm_session:
        # Initial warmup connection
        warm_session.get(url, timeout=5.0)
        for _ in range(warm_samples):
            t0 = time.perf_counter()
            r = warm_session.get(url, timeout=5.0)
            t1 = time.perf_counter()
            if r.status_code == 200:
                warm_rtts.append((t1 - t0) * 1000.0)
            time.sleep(0.05)

    return cold_rtt, warm_rtts


async def measure_websocket(ws_url: str, ping_samples: int = 15) -> Tuple[float, List[float]]:
    t0 = time.perf_counter()
    async with websockets.connect(ws_url, open_timeout=5.0) as ws:
        t1 = time.perf_counter()
        connect_time_ms = (t1 - t0) * 1000.0

        ping_rtts = []
        for _ in range(ping_samples):
            t_ping = time.perf_counter()
            pong_waiter = await ws.ping()
            await pong_waiter
            t_pong = time.perf_counter()
            ping_rtts.append((t_pong - t_ping) * 1000.0)
            await asyncio.sleep(0.05)

    return connect_time_ms, ping_rtts


def print_stats(name: str, values: List[float], unit: str = "ms"):
    if not values:
        log.info(f"  {name:<30} : No successful samples")
        return
    min_v = min(values)
    med_v = statistics.median(values)
    avg_v = statistics.mean(values)
    max_v = max(values)
    p95_v = sorted(values)[int(len(values) * 0.95)] if len(values) >= 5 else max_v
    log.info(
        f"  {name:<30} : min={min_v:6.2f}{unit} | med={med_v:6.2f}{unit} | "
        f"avg={avg_v:6.2f}{unit} | p95={p95_v:6.2f}{unit} | max={max_v:6.2f}{unit}"
    )


def main():
    log.info("=" * 80)
    log.info(" POLYMARKET AWS DUBLIN LATENCY BENCHMARK ")
    log.info("=" * 80)

    # 1. DNS Resolution
    log.info("\n[1] DNS Resolution Latency (5 iterations):")
    for host in ["clob.polymarket.com", "ws-subscriptions-clob.polymarket.com", "gamma-api.polymarket.com"]:
        rtts = measure_dns(host)
        print_stats(host, rtts)

    # 2. TCP & TLS Handshake
    log.info("\n[2] TCP Connect & TLS 1.3 Handshake Latency (5 connections):")
    for host in ["clob.polymarket.com", "ws-subscriptions-clob.polymarket.com"]:
        tcp, tls = measure_tcp_tls(host)
        print_stats(f"{host} (TCP)", tcp)
        print_stats(f"{host} (TLS)", tls)

    # 3. HTTP REST Latency (Cold vs Warm Keep-Alive)
    log.info("\n[3] HTTP REST Round-Trip Latency (15 samples each over Keep-Alive):")
    for label, url in TARGET_HOSTS.items():
        try:
            cold, warm = measure_http_rest(url, warm_samples=15)
            log.info(f"\n  -- {label} ({url}) --")
            log.info(f"  {'Cold Start (Connect + TLS + RTT)':<30} : {cold:6.2f}ms")
            print_stats("Warm Keep-Alive RTT", warm)
        except Exception as e:
            log.info(f"  {label:<30} : Failed ({e})")

    # 4. WebSocket Handshake & Frame Ping/Pong
    log.info("\n[4] WebSocket Streaming Latency (ws-subscriptions-clob):")
    try:
        ws_conn, ws_pings = asyncio.run(measure_websocket(WS_URL, ping_samples=15))
        log.info(f"  {'WS Handshake & Upgrade':<30} : {ws_conn:6.2f}ms")
        print_stats("WS Protocol Ping/Pong RTT", ws_pings)
    except Exception as e:
        log.info(f"  WebSocket Test Failed: {e}")

    log.info("\n" + "=" * 80)


if __name__ == "__main__":
    main()
