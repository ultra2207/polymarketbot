#!/usr/bin/env python3
"""Probe which Kalshi environment accepts the configured API key.

Loads ``.env`` (keys read through :mod:`os.environ` only), signs one request
with :mod:`polymarketbot.kalshi_auth` and reports the outcome:

    status=200 accepted=demo error_code=none
    status=401 accepted=none error_code=authentication_error

The probe never logs the key id, the PEM path's contents or the signature.
It reports the key *type* (``ed25519`` / ``rsa``) and the edge IP it pinned.

Why it pins an edge
-------------------
Direct TLS to Kalshi resets on this host (``WinError 10054``) on some of the
four A records the resolver returns. The probe therefore installs the DoH
fallback, probes candidates with the same :class:`EdgePool` the collector
stream uses and pins one session to a working IP via ``_new_session``.

Usage
    .venv/Scripts/python.exe scripts/kalshi_probe.py --host demo
    .venv/Scripts/python.exe scripts/kalshi_probe.py --host prod

Exit codes: 0 the tested host accepted the key, 1 it rejected the key
(401/403), 2 transport or configuration failure.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from polymarketbot.config import _parse_env_file_direct
from polymarketbot.data.streams.kalshi import (
    DEFAULT_TIMEOUT_SECONDS,
    EdgePool,
    _new_session,
    _resolve_ipv4,
    probe_tls,
)
from polymarketbot.kalshi_auth import (
    KEY_PATH_ENV,
    KalshiAuthError,
    KalshiSigner,
    error_code,
    signed_get,
)
from polymarketbot.logs import get_logger
from polymarketbot.net import DEFAULT_HOSTS, install_doh_fallback

log = get_logger("kalshi_probe")

ROOT = Path(__file__).resolve().parents[1]

#: Unauthenticated portfolio endpoint: 200 means the key was accepted.
DEFAULT_PATH = "/trade-api/v2/portfolio/balance"

#: Hosts from the API Environments table: the root each environment's
#: ``/trade-api/v2`` prefix hangs off. The demo key is registered in demo only.
HOSTS = {
    "demo": "https://demo-api.kalshi.co",
    "prod": "https://api.elections.kalshi.com",
}


def _load_env(path: Path) -> None:
    """Expose ``.env`` values through ``os.environ`` without overriding it."""
    if not path.is_file():
        log.warning("no .env at {}; using the process environment", path)
        return
    for key, value in _parse_env_file_direct(path).items():
        os.environ.setdefault(key, value)


def _normalise_key_path() -> None:
    """Resolve a relative ``KALSHI_PRIVATE_KEY_PATH`` against the repo root."""
    raw = os.environ.get(KEY_PATH_ENV, "").strip()
    if raw and not Path(raw).expanduser().is_absolute():
        os.environ[KEY_PATH_ENV] = str(ROOT / raw)


def _pin_edge(host: str) -> str | None:
    """A reachable IP for ``host``, preferring the DoH answer.

    The DoH patch makes ``getaddrinfo`` return a single address, which is
    enough when it works but leaves no sibling to rotate to when an edge
    resets mid-handshake. The unpatched resolver's full A-record list is
    captured first and used as the fallback set.
    """
    system = _resolve_ipv4(host)
    install_doh_fallback(DEFAULT_HOSTS | {host})
    doh = _resolve_ipv4(host)
    candidates = doh + [ip for ip in system if ip not in doh]
    log.info("host={} candidates={} ({})", host, len(candidates), ", ".join(candidates))

    pool = EdgePool(host)
    ip = pool.first_reachable()
    if ip is None:
        for extra in candidates:
            if extra in doh:
                continue
            if probe_tls(host, extra):
                ip = extra
                break
    if ip is not None:
        log.info("pinned edge {} for {}", ip, host)
    return ip


def _summarise(status: int, accepted: str, code: str | None) -> str:
    return f"status={status} accepted={accepted} error_code={code or 'none'}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report whether the configured Kalshi API key is accepted."
    )
    parser.add_argument(
        "--host",
        choices=sorted(HOSTS),
        default="demo",
        help="environment to test (default: demo)",
    )
    parser.add_argument(
        "--path",
        default=DEFAULT_PATH,
        help=f"request path including the API prefix (default: {DEFAULT_PATH})",
    )
    parser.add_argument(
        "--key-path",
        default=None,
        help="PEM private key (default: KALSHI_PRIVATE_KEY_PATH)",
    )
    parser.add_argument(
        "--key-id",
        default=None,
        help="override the key id header (default: KALSHI_KEY_ID)",
    )
    parser.add_argument(
        "--env-file",
        default=str(ROOT / ".env"),
        help="dotenv file to load (default: <repo>/.env)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-request timeout seconds (default: {DEFAULT_TIMEOUT_SECONDS:.0f})",
    )
    args = parser.parse_args(argv)

    _load_env(Path(args.env_file))

    base = HOSTS[args.host]
    host = base.split("://", 1)[1]
    url = base + args.path if args.path.startswith("/") else args.path

    ip = _pin_edge(host)
    if ip is None:
        log.error("no edge of {} completed a TLS handshake; not sending", host)
        return 2

    try:
        if args.key_path:
            signer = KalshiSigner.from_path(args.key_path, key_id=args.key_id)
        else:
            _normalise_key_path()
            signer = KalshiSigner.from_env()
    except KalshiAuthError as exc:
        log.error("cannot load the Kalshi key: {}", exc)
        return 2

    # The key id and PEM never reach the log; the type is not secret.
    log.info(
        "host={} url={} key_type={} action=GET",
        args.host,
        url,
        signer.kind,
    )

    session = _new_session(ip, timeout=args.timeout)
    try:
        response = signed_get(session, url, signer=signer, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001 - transport failure is the answer
        log.error(
            "request to {} failed at the transport layer: {}",
            host,
            type(exc).__name__,
        )
        return 2
    finally:
        session.close()

    status = int(response.status_code)
    code = error_code(response)
    accepted = args.host if response.ok else "none"
    log.info(_summarise(status, accepted, code))

    if response.ok:
        return 0
    if status in (401, 403):
        log.info(
            "the key was rejected by {} (the demo key is demo-only)", args.host
        )
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
