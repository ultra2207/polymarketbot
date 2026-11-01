"""Minimal Polygon JSON-RPC client for historical log indexing.

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
chunk pressure; it is read-only and cannot sign anything.

Usage
    from polymarketbot.chain import ChainClient
    chain = ChainClient()
    chain.block_number()
    chain.get_logs(address=FACTORY, from_block=50_000_000, topics=[topic])
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import requests

# Ordered fallbacks. The first is the only one verified to serve historical
# eth_getLogs on a free tier.
FREE_ENDPOINTS = (
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
)

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


def rpc_url() -> tuple[str, bool]:
    """Return (url, keyed). `POLYGON_RPC_URL` wins over the free fallbacks."""
    url = os.environ.get("POLYGON_RPC_URL") or _env_file().get("POLYGON_RPC_URL")
    if url:
        return url.rstrip("/"), True
    return FREE_ENDPOINTS[0], False


@dataclass
class ChainClient:
    url: str | None = None
    timeout_s: float = 30.0
    max_retries: int = 4
    min_interval_s: float = 0.05
    session: requests.Session = field(default_factory=requests.Session)
    _last: float = 0.0

    def __post_init__(self) -> None:
        if self.url is None:
            self.url, self.keyed = rpc_url()
        else:
            self.keyed = True
        self.chunk = KEYED_CHUNK if self.keyed else FREE_CHUNK
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "polymarketbot-research/0.1",
        })

    def _rpc(self, method: str, params: list) -> object:
        wait = self.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                r = self.session.post(
                    self.url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=self.timeout_s,
                )
                if r.status_code == 429:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                payload = r.json()
                if "error" in payload:
                    err = payload["error"]
                    # A range rejection is the caller's problem; anything else
                    # (rate limit, archive restriction) is worth a retry.
                    raise RuntimeError(f"{method}: {err}")
                return payload["result"]
            except Exception as exc:  # noqa: BLE001 - network + provider errors
                last = exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{method} failed after {self.max_retries} attempts: {last}")

    def block_number(self) -> int:
        return int(str(self._rpc("eth_blockNumber", [])), 16)

    def eth_call(self, to: str, data: str, *, block: str | int = "latest") -> str:
        tag = block if isinstance(block, str) else hex(block)
        return str(self._rpc("eth_call", [{"to": to, "data": data}, tag]))

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
                print(f"  blocks {start:,}-{end:,}  logs so far {len(out):,}")
            start = end + 1
        return out
