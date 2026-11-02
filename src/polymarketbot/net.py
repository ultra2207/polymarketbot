"""Network helpers that survive a flaky local resolver.

The local resolver on this machine intermittently fails `getaddrinfo` for
Polymarket hosts (and returns IPv6-only answers that then fail to connect).
Both failure modes look like random connection errors, which is worse than a
clean outage because it corrupts long-running crawls halfway through.

`install_doh_fallback()` monkey-patches `socket.getaddrinfo` so that:

* known Polymarket hosts are resolved over DNS-over-HTTPS to 1.1.1.1 by IP
  (no DNS needed to reach the resolver itself),
* results are cached, and
* IPv4 is preferred, because the IPv6 path is what usually breaks here.

It is deliberately explicit: call it from scripts, not on import.
"""

from __future__ import annotations

import socket
import threading
import time
from urllib.parse import urlparse

import requests

DOH_URL = "https://1.1.1.1/dns-query"

DEFAULT_HOSTS = {
    "gamma-api.polymarket.com",
    "data-api.polymarket.com",
    "clob.polymarket.com",
    "polymarket.com",
    "ws-subscriptions-clob.polymarket.com",
}

_CACHE: dict[str, tuple[float, str]] = {}
_LOCK = threading.Lock()
_ORIG = socket.getaddrinfo
_INSTALLED = False


def _doh_query(name: str, rtype: str = "A") -> list[dict]:
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(
                DOH_URL,
                params={"name": name, "type": rtype},
                headers={"Accept": "application/dns-json"},
                timeout=10,
            )
            r.raise_for_status()
            return r.json().get("Answer", []) or []
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"DoH lookup failed for {name}: {last}")


def resolve_ipv4(host: str, depth: int = 0) -> str:
    """Resolve a host to an IPv4 address, following CNAME chains."""
    if host in _CACHE:
        expires, ip = _CACHE[host]
        if expires > time.time():
            return ip

    if depth > 5:
        raise RuntimeError(f"CNAME loop resolving {host}")

    # Blocked by regional ISP injunction (causes WinError 10054 on TLS handshake)
    BLOCKED_IPS = {"104.18.34.205", "172.64.153.51"}
    RESILIENT_CF_FALLBACK = "104.18.35.205"

    answers = _doh_query(host, "A")
    for a in answers:
        if a.get("type") == 1:
            ip = a["data"]
            if ip in BLOCKED_IPS:
                ip = RESILIENT_CF_FALLBACK
            _CACHE[host] = (time.time() + 300, ip)
            return ip

    for a in answers:
        if a.get("type") == 5:
            target = a["data"].rstrip(".")
            ip = resolve_ipv4(target, depth + 1)
            if ip in BLOCKED_IPS:
                ip = RESILIENT_CF_FALLBACK
            _CACHE[host] = (time.time() + 300, ip)
            return ip

    raise RuntimeError(f"DoH returned no A record for {host}: {answers}")


def install_doh_fallback(hosts: set[str] | None = None) -> None:
    """Patch socket.getaddrinfo for the given hosts (default: Polymarket)."""
    global _INSTALLED
    watched = hosts or DEFAULT_HOSTS
    if _INSTALLED:
        return

    def patched(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        name = host.decode() if isinstance(host, bytes) else host
        if name in watched:
            try:
                ip = resolve_ipv4(name)
                # AF_INET only: avoids the broken IPv6 path entirely.
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                         "", (ip, port))]
            except Exception:  # noqa: BLE001
                pass  # fall through to the system resolver
        return _ORIG(host, port, family, type, proto, flags)

    socket.getaddrinfo = patched  # type: ignore[assignment]
    _INSTALLED = True


def warm(hosts: set[str] | None = None) -> dict[str, str]:
    """Resolve everything up front and report what worked."""
    out: dict[str, str] = {}
    for h in hosts or DEFAULT_HOSTS:
        try:
            out[h] = resolve_ipv4(h)
        except Exception as exc:  # noqa: BLE001
            out[h] = f"FAIL: {exc}"
    return out
