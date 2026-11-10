"""Minimal Polygon JSON-RPC client for log indexing and settlement broadcasts.

Why this exists
---------------
The wallet identity map (EOA ↔ proxy ↔ deposit wallet), UMA proposal/dispute
history and Chainlink feed updates all live in Polygon event logs, and none of
them is in any Polymarket dataset. Scraping them needs one thing the free
public endpoints do not all give: `eth_getLogs` over an arbitrary historical
range.

What was measured on 2026-11-01
-------------------------------
    polygon-rpc.com        HTTP 401 "API key disabled, tenant disabled"
                           (a HEAD request returns 200, so this needs a real
                            JSON-RPC probe to detect)
    publicnode.com         archive `eth_getLogs` -> "require a paid plan"
    1rpc.io/matic          `eth_getLogs` capped at ~5 blocks
    drpc.org               works, 10,000-block cap per call   <- default

So indexing is possible without a key, just chunked: Polygon is ~2.1 s/block, so
10 k blocks is ~5.8 h of chain time and the 2025-05 -> 2026-11 window is about
2,260 requests. Setting `POLYGON_RPC_URL` (Alchemy/Infura free tier) removes the
chunk pressure; it is read-only and cannot sign anything. `ETH_RPC_URL` is
accepted as an alias for the same Alchemy app key, and `POLYGON_RPC_URLS` may
list extra read-only failover endpoints. Settlement always broadcasts to the one
`ChainClient.write_url`, so a read failover can never redirect a signed
transaction.

What was measured on 2026-11-04
-------------------------------
Alchemy answered 510 WebSocket handshakes with HTTP 429 between 01:39:49Z and
05:56:51Z (4 h 17 min, still failing at the time of writing) while HTTPS on the
same key kept serving `eth_getLogs` and `eth_getBlockByNumber` without a single
429 (`data/logs/data.polygon.log`). The key was not over its compute budget: the
HTTP fallback ran about 50 requests per minute (~19 CU/s against a 300 CU/s
free-tier ceiling). The socket path had no rotation and no jitter, so every
failure retried the same Alchemy URL 2x/min for four hours. Provider rotation, per-provider cooldown on a 429 and jittered
retry now live in `ProviderPool`, which both this client and the polygon stream
use.

Usage
    from polymarketbot.chain import ChainClient
    chain = ChainClient()
    chain.block_number()
    chain.get_logs(address=FACTORY, from_block=50_000_000, topics=[topic])
    chain.send_raw_transaction(signed_tx_hex)   # already-signed type-2 payload
"""

from __future__ import annotations

import os
import random
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from polymarketbot.logs import get_logger

logger = get_logger("chain")

# Ordered fallbacks. The first is the only one verified to serve historical
# eth_getLogs on a free tier.
FREE_ENDPOINTS = (
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
)

# Polygon PoS mainnet. The chain id is the only proof that an endpoint is
# usable for settlement: a URL that answers for another chain silently returns
# nonces and receipts that belong to a different network.
POLYGON_CHAIN_ID = 137

ALCHEMY_POLYGON_HOST = "polygon-mainnet.g.alchemy.com"
ALCHEMY_POLYGON_URL = f"https://{ALCHEMY_POLYGON_HOST}/v2/{{key}}"
# An Alchemy key is a long opaque token; a URL always carries a scheme.
_ALCHEMY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")

# Error taxonomy. Only the transient kinds are retried, and only a transient
# kind means "the broadcast may or may not have landed".
RATE_LIMIT = "rate_limit"
SERVER_ERROR = "server_error"
TRANSPORT = "transport"
NONCE_TOO_LOW = "nonce_too_low"
REPLACEMENT_UNDERPRICED = "replacement_underpriced"
EXECUTION_REVERTED = "execution_reverted"
ALREADY_KNOWN = "already_known"
UNKNOWN = "unknown"

TRANSIENT_ERRORS = frozenset({RATE_LIMIT, SERVER_ERROR, TRANSPORT})

# Methods that must reach exactly one endpoint: a failover URL would return a
# different node's view of the mempool while the payload is signed for one chain.
WRITE_METHODS = frozenset({"eth_sendRawTransaction"})


class RpcError(RuntimeError):
    """JSON-RPC / provider failure carrying a taxonomy kind."""

    def __init__(self, message: str, kind: str = UNKNOWN, code: object = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code


def classify_rpc_error(exc: object) -> str:
    """Map a provider exception or error string onto the taxonomy above.

    The classification drives two decisions that must not be guessed: whether a
    call is worth retrying, and whether a broadcast is definitive or ambiguous.
    """
    if isinstance(exc, RpcError):
        return exc.kind
    message = str(exc).lower()
    if not message:
        return UNKNOWN
    if "execution reverted" in message or "executionreverted" in message:
        return EXECUTION_REVERTED
    if "nonce too low" in message or "nonce is too low" in message:
        return NONCE_TOO_LOW
    if "replacement transaction underpriced" in message or "replacement underpriced" in message:
        return REPLACEMENT_UNDERPRICED
    if "already known" in message or "known transaction" in message or "already imported" in message:
        return ALREADY_KNOWN
    if "429" in message or "too many requests" in message or "rate limit" in message or "rate-limit" in message:
        return RATE_LIMIT
    if any(
        marker in message
        for marker in ("502", "503", "504", "server error", "internal error", "unavailable")
    ):
        return SERVER_ERROR
    if any(
        marker in message
        for marker in ("timeout", "timed out", "connection", "max retries", "reset by peer")
    ):
        return TRANSPORT
    return UNKNOWN


def is_transient(kind: str) -> bool:
    return kind in TRANSIENT_ERRORS


# The free endpoint rejects ranges wider than this.
FREE_CHUNK = 10_000
# A keyed provider is far more permissive; keep chunks civil anyway.
KEYED_CHUNK = 100_000

POLYGON_BLOCK_SECONDS = 2.1


def _env_file() -> dict[str, str]:
    """Read `.env` without adding a dependency. Absent file is fine."""
    path = Path(".env")
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _configured(name: str) -> str | None:
    """Environment first, then `.env` (the repo keeps secrets there)."""
    return os.environ.get(name) or _env_file().get(name)


def normalise_endpoint(raw: str | None) -> str | None:
    """One usable RPC URL, or None when `raw` is unusable.

    Accepts a bare Alchemy key (no scheme) and any Alchemy URL. An Alchemy URL
    for another chain is rewritten to polygon-mainnet with the same key: a
    settlement broadcast must never reach a non-Polygon endpoint, and the
    rewritten URL either works or fails `health_check`, which is a visible
    failure rather than a wrong-chain success.
    """
    value = (raw or "").strip().strip('"').strip("'").rstrip("/")
    if not value:
        return None
    if "://" not in value:
        if _ALCHEMY_KEY_RE.match(value):
            return ALCHEMY_POLYGON_URL.format(key=value)
        logger.warning("Ignoring RPC setting: no scheme and not an Alchemy key")
        return None

    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        logger.warning(f"Ignoring RPC URL with unsupported scheme: {parts.scheme}")
        return None

    host = parts.netloc.lower()
    segments = [s for s in parts.path.split("/") if s]
    if host.endswith("alchemy.com") and host != ALCHEMY_POLYGON_HOST:
        if len(segments) == 2 and segments[0] == "v2":
            logger.warning(
                f"RPC endpoint {host} is not Polygon; using {ALCHEMY_POLYGON_HOST} with the same key"
            )
            parts = parts._replace(netloc=ALCHEMY_POLYGON_HOST)
        else:
            logger.warning(f"RPC endpoint {host} is an Alchemy host without a /v2/<key> path")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, "")).rstrip("/")


def rpc_url() -> tuple[str, bool]:
    """Return (url, keyed) for the single write endpoint.

    Order: `POLYGON_RPC_URL`, then `ETH_RPC_URL` (the Alchemy app key lives
    there today), then the free fallbacks. Trailing slashes are stripped so the
    same endpoint always compares equal.
    """
    for name in ("POLYGON_RPC_URL", "ETH_RPC_URL"):
        endpoint = normalise_endpoint(_configured(name))
        if endpoint is not None:
            return endpoint, True
    return FREE_ENDPOINTS[0], False


def rpc_urls() -> list[str]:
    """Read endpoints in preference order: primary, `POLYGON_RPC_URLS`, free.

    Reads may fail over; writes never do (see `ChainClient.write_url`).
    """
    out: list[str] = []
    primary, _ = rpc_url()
    out.append(primary)
    for extra in (_configured("POLYGON_RPC_URLS") or "").split(","):
        endpoint = normalise_endpoint(extra)
        if endpoint is not None and endpoint not in out:
            out.append(endpoint)
    for free in FREE_ENDPOINTS:
        if free not in out:
            out.append(free)
    return out


def mask_url(url: str) -> str:
    """URL with any Alchemy key replaced, for logs and reports."""
    match = re.match(r"^(https?://[^/]+/v2/)(.+)$", url)
    if match:
        return f"{match.group(1)}<key:{match.group(2)[:4]}...>"
    return url


# --------------------------------------------------------------------------- #
# Provider registry, cooldown and retry jitter
# --------------------------------------------------------------------------- #
# A 429 is a statement about the *credential*, not about the request: the same
# call usually succeeds immediately somewhere else. Rotating on a 429 and
# resting the provider is therefore the only retry that helps; retrying the
# same URL (what this client did until 2026-11-04) is what kept that incident
# alive for four hours.
#
# Hosted nodes throttle the key, not the hostname: one Alchemy key serves both
# `eth-mainnet.g.alchemy.com/v2/<key>` and `polygon-mainnet.g.alchemy.com/v2/<key>`.
# Resting only the URL that answered rotates to its sibling on the same
# throttled key, which re-earns the 429 and climbs a second, independent
# backoff ladder. Measured 2026-11-10 in one 30-minute soak: cooldown
# 56 s -> 142 s -> 921 s on a single configured key. Cooldown is therefore
# applied to every endpoint sharing a credential.
MIN_COOLDOWN_S = 60.0
MAX_COOLDOWN_S = 900.0
RETRY_BASE_S = 0.5
RETRY_CAP_S = 30.0

# WebSocket endpoint per provider host, where it is not simply https -> wss.
_WSS_BY_HOST = {
    "polygon.drpc.org": "wss://polygon.drpc.org",
    "polygon-bor-rpc.publicnode.com": "wss://polygon-bor-rpc.publicnode.com",
    "rpc.ankr.com": "wss://rpc.ankr.com/polygon/ws",
}
# Provider name per host, for log lines and health metrics.
_NAME_BY_HOST = {
    "polygon.drpc.org": "drpc",
    "polygon-bor-rpc.publicnode.com": "publicnode",
    "rpc.ankr.com": "ankr",
    "polygon-rpc.com": "polygon_rpc",
    "1rpc.io": "1rpc",
}

# Read order used when nothing is configured. Alchemy is not in the list
# because it needs a key; it is prepended by `rpc_urls()` when one exists.
PUBLIC_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("drpc", "https://polygon.drpc.org"),
    ("publicnode", "https://polygon-bor-rpc.publicnode.com"),
    ("ankr", "https://rpc.ankr.com/polygon"),
)


def provider_name(url: str) -> str:
    """Stable short name for a provider URL, safe as a health metric component."""
    host = urlsplit(url or "").netloc.lower()
    if host in _NAME_BY_HOST:
        return _NAME_BY_HOST[host]
    if "alchemy.com" in host:
        return "alchemy"
    if "infura.io" in host:
        return "infura"
    if "quiknode.pro" in host or "quicknode" in host:
        return "quicknode"
    cleaned = re.sub(r"[^a-z0-9]+", "_", host).strip("_")
    return cleaned or "unknown"


def rate_limit_key(url: str) -> str:
    """Identity whose quota a 429 on ``url`` actually exhausted.

    Hosted nodes throttle the credential: one Alchemy key serves every chain's
    endpoint, so `eth-mainnet.../v2/<key>` and `polygon-mainnet.../v2/<key>`
    share one bucket and must share one cooldown. Public providers throttle
    per host, so their identity is the provider name alone.
    """
    host = urlsplit(url or "").netloc.lower()
    name = provider_name(url)
    if "alchemy.com" in host or "infura.io" in host:
        parts = [p for p in (urlsplit(url or "").path or "").split("/") if p]
        credential = parts[-1] if parts else ""
        return f"{name}:{credential}" if credential else name
    if "quiknode.pro" in host or "quicknode" in host:
        return f"{name}:{host.split('.')[0]}"
    return name


def websocket_url(url: str) -> str:
    """WebSocket endpoint for an HTTP JSON-RPC URL.

    Most providers accept the same host over `wss`; Ankr does not (it needs
    `/ws`), so the known hosts are looked up first.
    """
    text = (url or "").strip().rstrip("/")
    if text.startswith(("wss://", "ws://")):
        return text
    known = _WSS_BY_HOST.get(urlsplit(text).netloc.lower())
    if known:
        return known
    if text.startswith("https://"):
        return "wss://" + text[len("https://") :]
    if text.startswith("http://"):
        return "ws://" + text[len("http://") :]
    raise ValueError("cannot derive a WebSocket URL from a URL without a scheme")


def jittered_delay(
    attempt: int,
    *,
    base: float = RETRY_BASE_S,
    cap: float = RETRY_CAP_S,
    rng: Callable[[], float] = random.random,
) -> float:
    """Capped exponential backoff with equal jitter.

    Equal jitter keeps the lower half of the interval, so a retry always waits
    at least half the exponential step while peers that failed together do not
    come back in lockstep. The 2026-11-04 incident retried on a fixed ladder
    with no jitter against a single endpoint for 492 attempts.
    """
    step = min(cap, float(base) * (2 ** max(0, int(attempt))))
    return step * (0.5 + 0.5 * max(0.0, min(1.0, float(rng()))))


@dataclass
class ProviderHealth:
    """What one provider has done and how long it must rest."""

    url: str
    name: str
    wss: str | None = None
    calls: int = 0
    ok: int = 0
    errors: int = 0
    rate_limits: int = 0
    streak: int = 0  # consecutive rate limits, drives the cooldown ladder
    cooldown_until: float = 0.0
    last_kind: str | None = None


class ProviderPool:
    """Ordered providers with per-provider cooldown after a 429.

    `clock` and `rng` are injected so cooldown behaviour is unit-tested without
    sleeping or relying on wall time.
    """

    def __init__(
        self,
        urls: Iterable[str],
        *,
        cooldown_base_s: float = MIN_COOLDOWN_S,
        cooldown_max_s: float = MAX_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.cooldown_base_s = max(0.0, float(cooldown_base_s))
        self.cooldown_max_s = max(self.cooldown_base_s, float(cooldown_max_s))
        self._clock = clock
        self._rng = rng
        self._providers: dict[str, ProviderHealth] = {}
        self.set_urls(urls)

    # -- ordering ---------------------------------------------------------- #
    def set_urls(self, urls: Iterable[str]) -> None:
        """Replace the order, keeping the recorded health of surviving URLs."""
        seen: dict[str, ProviderHealth] = {}
        used_names: set[str] = set()
        for raw in urls:
            url = (raw or "").strip().rstrip("/")
            if not url or url in seen:
                continue
            health = self._providers.get(url)
            if health is None:
                health = ProviderHealth(url=url, name=provider_name(url))
                try:
                    health.wss = websocket_url(url)
                except ValueError:
                    health.wss = None
            base = health.name
            name, n = base, 1
            while name in used_names:
                n += 1
                name = f"{base}_{n}"
            health.name = name
            used_names.add(name)
            seen[url] = health
        self._providers = seen

    def set_wss(self, url: str, wss: str) -> None:
        """Pin the WebSocket endpoint for one provider URL."""
        health = self.get(url)
        if health is not None:
            health.wss = (wss or "").strip().rstrip("/") or None

    def get(self, url: str) -> ProviderHealth | None:
        return self._providers.get((url or "").strip().rstrip("/"))

    def health(self) -> list[ProviderHealth]:
        return list(self._providers.values())

    def available(self, now: float | None = None) -> list[ProviderHealth]:
        """Providers not cooling down, in preference order."""
        moment = self._clock() if now is None else now
        return [h for h in self._providers.values() if h.cooldown_until <= moment]

    def cooling(self, now: float | None = None) -> list[ProviderHealth]:
        moment = self._clock() if now is None else now
        return [h for h in self._providers.values() if h.cooldown_until > moment]

    def next_ready(self, now: float | None = None) -> ProviderHealth | None:
        """Provider whose cooldown ends first; None when the pool is empty."""
        moment = self._clock() if now is None else now
        if not self._providers:
            return None
        return min(
            self._providers.values(),
            key=lambda h: (max(0.0, h.cooldown_until - moment),),
        )

    def next_available_in(self, now: float | None = None) -> float:
        moment = self._clock() if now is None else now
        ready = self.available(now=moment)
        if ready or not self._providers:
            return 0.0
        candidate = self.next_ready(now=moment)
        return max(0.0, (candidate.cooldown_until - moment) if candidate else 0.0)

    def ws_candidates(self, now: float | None = None) -> list[ProviderHealth]:
        """Providers with a WebSocket endpoint that are not cooling down."""
        return [h for h in self.available(now=now) if h.wss]

    def cooldown_remaining(self, url: str, now: float | None = None) -> float:
        health = self.get(url)
        if health is None:
            return 0.0
        moment = self._clock() if now is None else now
        return max(0.0, health.cooldown_until - moment)

    # -- outcome recording ------------------------------------------------- #
    def mark_ok(self, url: str) -> None:
        health = self.get(url)
        if health is None:
            return
        health.calls += 1
        health.ok += 1
        health.last_kind = None
        # A success proves the credential is not throttled, so the ladder its
        # siblings inherited from the same 429 no longer applies. Their
        # remaining cooldown is left to expire so the pool does not thrash.
        key = rate_limit_key(health.url)
        for member in self._providers.values():
            if member is health:
                continue
            if rate_limit_key(member.url) == key:
                member.streak = 0
        health.streak = 0
        health.cooldown_until = 0.0

    def mark_error(self, url: str, kind: str = UNKNOWN) -> None:
        """A failure that is not a rate limit: failover, but no cooldown."""
        health = self.get(url)
        if health is None:
            return
        health.calls += 1
        health.errors += 1
        health.last_kind = kind

    def mark_rate_limited(self, url: str) -> float:
        """Rest the whole credential behind ``url`` after a 429.

        Every endpoint sharing that credential is cooled together and shares
        the backoff ladder, because the quota is the credential's. Returns the
        remaining cooldown for the URL that was called.
        """
        health = self.get(url)
        if health is None:
            return 0.0
        health.calls += 1
        health.errors += 1
        health.rate_limits += 1
        health.last_kind = RATE_LIMIT
        key = rate_limit_key(health.url)
        group = [h for h in self._providers.values() if rate_limit_key(h.url) == key] or [health]
        streak = max(h.streak for h in group) + 1
        ceiling = min(
            self.cooldown_max_s,
            self.cooldown_base_s * (2 ** max(0, streak - 1)),
        )
        delay = ceiling * (0.75 + 0.5 * max(0.0, min(1.0, float(self._rng()))))
        until = self._clock() + delay
        for member in group:
            member.streak = streak
            member.cooldown_until = max(member.cooldown_until, until)
        return self.cooldown_remaining(url)

    def metrics(self, now: float | None = None) -> dict[str, float]:
        """Flatten every provider's counters and cooldown for the health table."""
        moment = self._clock() if now is None else now
        out: dict[str, float] = {}
        for health in self._providers.values():
            out[f"provider_{health.name}_errors"] = float(health.errors)
            out[f"provider_{health.name}_rate_limits"] = float(health.rate_limits)
            out[f"provider_{health.name}_cooldown_s"] = max(
                0.0, health.cooldown_until - moment
            )
        return out


@dataclass
class ChainClient:
    url: str | None = None
    timeout_s: float = 30.0
    max_retries: int = 4
    min_interval_s: float = 0.05
    session: requests.Session = field(default_factory=requests.Session)
    _last: float = 0.0

    def __post_init__(self) -> None:
        if self.url is not None and not str(self.url).strip():
            self.url = None
        if self.url is None:
            self.url, self.keyed = rpc_url()
            self.read_urls = rpc_urls()
        else:
            # An explicitly supplied endpoint is used for reads and writes alike;
            # a caller that pinned a URL did not ask for failover. Normalise it
            # anyway: an Alchemy URL for another chain must be rewritten to
            # polygon-mainnet, never silently read Ethereum.
            normalized = normalise_endpoint(self.url) or self.url.rstrip("/")
            self.url = normalized
            self.keyed = "alchemy.com" in normalized
            self.read_urls = [normalized]
        # The one endpoint a signed transaction is ever sent to. Settlement
        # reads `write_url`, so a read failover can never redirect a broadcast.
        self.write_url = self.url.rstrip("/")
        self.chunk = KEYED_CHUNK if self.keyed else FREE_CHUNK
        self.pool = ProviderPool(self.read_urls)
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "polymarketbot-research/0.1",
        })

    def _backoff(self, attempt: int, kind: str) -> float:
        """Jittered, capped retry delay; a 429 waits longer than a 503."""
        base = 1.0 if kind == RATE_LIMIT else RETRY_BASE_S
        return jittered_delay(attempt, base=base, cap=RETRY_CAP_S)

    def _read_order(self) -> list[str]:
        """Read candidates in preference order, skipping cooling providers.

        `read_urls` is re-read on every call so a caller that reassigns it
        (tests, or a config reload) is honoured; when every provider is cooling,
        the one whose cooldown ends first is still tried, because a 429 lifts.
        """
        self.pool.set_urls(self.read_urls)
        ready = [h.url for h in self.pool.available()]
        if ready:
            return ready
        candidate = self.pool.next_ready()
        return [candidate.url] if candidate is not None else list(self.read_urls[:1])

    def _post(self, url: str, method: str, params: list) -> object:
        response = self.session.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=self.timeout_s,
        )
        if response.status_code == 429:
            raise RpcError(f"{method}: HTTP 429 rate limited", RATE_LIMIT, 429)
        if response.status_code >= 500:
            raise RpcError(
                f"{method}: HTTP {response.status_code}", SERVER_ERROR, response.status_code
            )
        if response.status_code >= 400:
            raise RpcError(
                f"{method}: HTTP {response.status_code} {str(response.text)[:200]}",
                UNKNOWN,
                response.status_code,
            )
        payload = response.json()
        if "error" in payload:
            err = payload.get("error") or {}
            code = err.get("code") if isinstance(err, dict) else None
            message = f"{method}: {err}"
            # A range rejection is the caller's problem; a nonce/revert error is
            # definitive. Both are raised without retrying.
            raise RpcError(message, classify_rpc_error(message), code)
        return payload["result"]

    def _rpc(self, method: str, params: list) -> object:
        """One JSON-RPC call, retrying only transient failures.

        A 429 rests that provider for a cooldown and moves to the next one in
        the same round: the provider is what is limited, so retrying the same
        URL is the one thing that cannot help. `eth_sendRawTransaction` always
        goes to `write_url` and is retried there only, because a second node
        would answer about a different mempool than the one that received the
        signed payload.
        """
        write = method in WRITE_METHODS
        attempts = max(1, self.max_retries)
        last: Exception | None = None

        for attempt in range(attempts):
            urls = [self.write_url] if write else self._read_order()
            if not urls:
                raise RuntimeError(f"{method}: no read endpoint configured")
            for url in urls:
                wait = self.min_interval_s - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
                try:
                    result = self._post(url, method, params)
                except Exception as exc:  # noqa: BLE001 - network + provider errors
                    kind = classify_rpc_error(exc)
                    last = exc
                    if not is_transient(kind):
                        raise
                    if kind == RATE_LIMIT:
                        cooled = self.pool.mark_rate_limited(url)
                        logger.warning(
                            f"{method} rate limited on {mask_url(url)}; "
                            f"cooldown {cooled:.0f}s, trying {len(urls) - urls.index(url) - 1} "
                            f"other provider(s)"
                        )
                    else:
                        self.pool.mark_error(url, kind)
                    logger.debug(
                        f"{method} on {mask_url(url)} failed transiently ({kind}), "
                        f"attempt {attempt + 1}/{attempts}: {exc}"
                    )
                    continue
                self.pool.mark_ok(url)
                return result
            time.sleep(self._backoff(attempt, classify_rpc_error(last)))
        raise RuntimeError(f"{method} failed after {attempts} attempts: {last}")

    def health_check(
        self,
        *,
        max_block_age_s: float = 60.0,
        max_latency_s: float = 1.5,
    ) -> tuple[bool, str]:
        """Prove the write endpoint is Polygon and fresh. (ok, reason)."""
        started = time.monotonic()
        try:
            chain_id = int(str(self._rpc("eth_chainId", [])), 16)
        except Exception as exc:  # noqa: BLE001 - any failure is a health failure
            return False, f"eth_chainId failed: {exc}"
        latency = time.monotonic() - started
        if chain_id != POLYGON_CHAIN_ID:
            return False, f"wrong chain: eth_chainId={chain_id}, expected {POLYGON_CHAIN_ID}"
        if latency > max_latency_s:
            return False, f"latency {latency * 1000:.0f}ms exceeds {max_latency_s * 1000:.0f}ms"

        try:
            block = self._rpc("eth_getBlockByNumber", ["latest", False])
        except Exception as exc:  # noqa: BLE001 - head read failure is a health failure
            return False, f"eth_getBlockByNumber failed: {exc}"
        if not isinstance(block, dict) or "timestamp" not in block:
            return False, "latest block is malformed"
        age = time.time() - int(str(block["timestamp"]), 16)
        if age < -60.0:
            return False, f"local clock is {-age:.0f}s behind the chain head"
        if age > max_block_age_s:
            return False, f"stale head: {age:.0f}s old (limit {max_block_age_s:.0f}s)"
        return True, (
            f"chain={POLYGON_CHAIN_ID} latency={latency * 1000:.0f}ms head_age={age:.0f}s"
        )

    def block_number(self) -> int:
        return int(str(self._rpc("eth_blockNumber", [])), 16)

    def eth_call(self, to: str, data: str, *, block: str | int = "latest") -> str:
        tag = block if isinstance(block, str) else hex(block)
        return str(self._rpc("eth_call", [{"to": to, "data": data}, tag]))

    def get_transaction_count(self, address: str, block: str = "pending") -> int:
        """Nonce for `address`, including pending transactions by default."""
        return int(str(self._rpc("eth_getTransactionCount", [address, block])), 16)

    def estimate_gas(self, tx: dict) -> int:
        """eth_estimateGas for a transaction object (from/to/data/value)."""
        return int(str(self._rpc("eth_estimateGas", [tx])), 16)

    def send_raw_transaction(self, raw_hex: str) -> str:
        """Broadcast a signed transaction; returns the node's transaction hash.

        The payload is already signed, so a retried send is idempotent: the same
        bytes always produce the same hash and the nonce cannot execute twice.
        """
        payload = raw_hex.strip()
        if not payload.startswith("0x"):
            payload = "0x" + payload
        result = self._rpc("eth_sendRawTransaction", [payload])
        if not result:
            raise RuntimeError("eth_sendRawTransaction returned no transaction hash")
        return str(result)

    def get_transaction_receipt(self, tx_hash: str) -> dict | None:
        """Mined receipt for `tx_hash`, or None while it is still pending."""
        result = self._rpc("eth_getTransactionReceipt", [tx_hash])
        return result if isinstance(result, dict) else None

    def get_logs(self, *, address: str | Iterable[str], from_block: int,
                 to_block: int | None = None, topics: list | None = None,
                 progress: bool = False) -> list[dict]:
        """Every log in the range, chunked to the provider's limit.

        Chunking is a property of the endpoint, not of the chain, so it is done
        here rather than at each call site. A chunk that fails is raised, not
        skipped: a silently missing log range would corrupt the wallet map.
        """
        if to_block is None:
            to_block = self.block_number()
        addr = ([address] if isinstance(address, str)
                else list(address))

        out: list[dict] = []
        start = from_block
        while start <= to_block:
            end = min(start + self.chunk - 1, to_block)
            flt: dict = {"fromBlock": hex(start), "toBlock": hex(end), "address": addr}
            if topics:
                flt["topics"] = topics
            try:
                out.extend(self._rpc("eth_getLogs", [flt]) or [])
            except RuntimeError:
                # The provider may cap below our chunk; halve once and retry.
                if self.chunk <= 1_000:
                    raise
                self.chunk //= 2
                continue
            if progress:
                logger.info(f"  blocks {start:,}-{end:,}  logs so far {len(out):,}")
            start = end + 1
        return out
