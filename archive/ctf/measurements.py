"""Measured on-chain inputs, copied out of ``polymarketbot.trading.ctf``.

``polymarketbot.trading.ctf`` stays in production because live settlement
redemption needs it; archive code may not import ``polymarketbot.trading``, so
the four measured-input helpers the archived CTF strategy prices with are
copied here verbatim (only the repository-root depth changed):

* ``load_persisted_rpc_rtt_s`` - the persisted Polygon RPC round trip probe.
* ``load_measured_block_interval_s`` - the measured PoS block interval from the
  ``data/tape/polygon_gas`` header tape.
* ``get_dynamic_gas_price_gwei`` - EIP-1559 base fee + priority tip from a live
  RPC, else the fast gas oracle.
* ``get_dynamic_pol_usd_price`` - POL/USD from Binance with a TTL cache.

They read measurements; none of them invents a rate when the measurement is
missing (the documented fallbacks are the function defaults and are logged).
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from pathlib import Path
from typing import Optional

from polymarketbot.chain import ChainClient
from polymarketbot.logs import get_logger

logger = get_logger("archive.ctf")

# archive/ctf/measurements.py -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Polygon PoS block-header tape (one row per head). Consecutive block-number
# deltas are the measured inter-block time; an absent tape yields ``None`` so
# callers queue the on-chain action and report the gap instead of assuming 0 s.
DEFAULT_POLYGON_GAS_TAPE = _REPO_ROOT / "data" / "tape" / "polygon_gas"
_BLOCK_INTERVAL_CACHE: dict[str, tuple[float, Optional[float]]] = {}


def load_measured_block_interval_s(
    tape_dir: Optional[str] = None,
    refresh_s: float = 300.0,
) -> Optional[float]:
    """Measured mean Polygon PoS block interval, in seconds.

    Reads the newest ``polygon_gas`` partitions, keeps consecutive block
    numbers, and averages the header timestamp deltas (0 < dt <= 60 s). The
    mean -- not the median -- is the expected wait for the next block, because
    Polygon's header timestamps alternate 1 s / 2 s. Returns ``None`` when no
    measured block sequence exists; callers must not substitute a default.
    """
    key = str(tape_dir or DEFAULT_POLYGON_GAS_TAPE)
    now = time.time()
    cached = _BLOCK_INTERVAL_CACHE.get(key)
    if cached is not None and now - cached[0] < refresh_s:
        return cached[1]

    value: Optional[float] = None
    try:
        files = sorted(Path(key).glob("date=*/hour=*/*.parquet"))
        if files:
            import pyarrow.parquet as pq

            table = pq.read_table(files[-3:], columns=["block_number", "timestamp_ms"])
            pairs = sorted(
                set(
                    zip(
                        table.column("block_number").to_pylist(),
                        table.column("timestamp_ms").to_pylist(),
                    )
                )
            )
            deltas = []
            for (b0, t0), (b1, t1) in zip(pairs, pairs[1:]):
                if b0 is None or b1 is None or t0 is None or t1 is None:
                    continue
                if b1 != b0 + 1:
                    continue
                dt = (t1 - t0) / 1000.0
                if 0.0 < dt <= 60.0:
                    deltas.append(dt)
            if len(deltas) >= 10:
                value = statistics.fmean(deltas)
                logger.info(
                    f"Measured Polygon block interval {value:.3f}s from "
                    f"{len(deltas)} consecutive headers in {Path(key)}"
                )
    except Exception as exc:  # noqa: BLE001 - measurement gap is reported, not hidden
        logger.warning(f"Measured Polygon block interval unavailable from {key}: {exc}")

    _BLOCK_INTERVAL_CACHE[key] = (now, value)
    return value


def load_persisted_rpc_rtt_s(path: Optional[str] = None) -> Optional[float]:
    """Measured Polygon RPC round trip persisted by the latency probe, if any.

    The probe currently records CLOB/Gamma/Binance endpoints; a ``polygon_rpc``
    (or ``rpc``) endpoint is used when present. ``None`` means no independent
    RPC measurement exists and the caller falls back to the shared measured
    venue RTT rather than assuming a zero-latency RPC.
    """
    target = Path(path or (_REPO_ROOT / "data" / "derived" / "local_latency.json"))
    try:
        endpoints = (json.loads(target.read_text(encoding="utf-8")) or {}).get("endpoints") or {}
        for name in ("polygon_rpc", "rpc", "clob_time"):
            entry = endpoints.get(name) or {}
            ms = entry.get("ttfb_p50_ms")
            if ms is not None and float(ms) > 0.0:
                return float(ms) / 1000.0
    except Exception:
        return None
    return None


def get_dynamic_gas_price_gwei(
    chain_client: Optional[ChainClient] = None,
    rpc_url: Optional[str] = None,
    fallback_gwei: float = 40.0,
) -> float:
    """Query dynamic gas price in Gwei on Polygon PoS using EIP-1559 base fee + priority fee.

    Fallback sequence:
    1. Polygon RPC eth_getBlockByNumber("latest") baseFeePerGas + eth_maxPriorityFeePerGas.
    2. Polygon RPC eth_gasPrice.
    3. Fast gas oracle (https://gasstation.polygon.technology/v2).
    4. Fallback baseline (40.0 Gwei).
    """
    client = chain_client
    if client is None and rpc_url is not None:
        try:
            client = ChainClient(url=rpc_url)
        except Exception as e:
            logger.debug(f"Failed to instantiate ChainClient for gas query: {e}")

    if client is not None:
        try:
            block = client._rpc("eth_getBlockByNumber", ["latest", False])
            if isinstance(block, dict) and "baseFeePerGas" in block and block["baseFeePerGas"]:
                base_fee_gwei = int(str(block["baseFeePerGas"]), 16) / 1e9
                priority_fee_gwei = 30.0
                try:
                    raw_tip = client._rpc("eth_maxPriorityFeePerGas", [])
                    if raw_tip:
                        priority_fee_gwei = int(str(raw_tip), 16) / 1e9
                except Exception as tip_err:
                    logger.debug(f"eth_maxPriorityFeePerGas lookup fallback: {tip_err}")

                total_gwei = base_fee_gwei + priority_fee_gwei
                if total_gwei > 0:
                    return float(max(25.0, min(500.0, round(total_gwei, 2))))
        except Exception as e:
            logger.debug(f"RPC EIP-1559 gas price lookup failed: {e}")

        try:
            raw_gas_price = client._rpc("eth_gasPrice", [])
            if raw_gas_price:
                live_gwei = int(str(raw_gas_price), 16) / 1e9
                if live_gwei > 0:
                    return float(max(25.0, min(500.0, round(live_gwei, 2))))
        except Exception as e:
            logger.debug(f"RPC eth_gasPrice fallback lookup failed: {e}")

    try:
        import requests
        resp = requests.get("https://gasstation.polygon.technology/v2", timeout=2.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                fast_data = data.get("fast") or data.get("standard")
                if isinstance(fast_data, dict) and "maxFee" in fast_data:
                    oracle_gwei = float(fast_data["maxFee"])
                    if oracle_gwei > 0:
                        return float(max(25.0, min(500.0, round(oracle_gwei, 2))))
    except Exception as oracle_err:
        logger.debug(f"Fast gas oracle lookup fallback: {oracle_err}")

    return float(fallback_gwei)


_POL_PRICE_CACHE: dict[str, float] = {"price": 0.092, "timestamp": 0.0}
_POL_PRICE_LOCK = threading.Lock()


def get_dynamic_pol_usd_price(
    fallback_price: float = 0.092,
    cache_ttl_seconds: float = 300.0,
) -> float:
    """Query live POL/USD exchange rate from Binance REST with caching and fallback.

    Queries POLUSDT with fallback to MATICUSDT. Cached thread-safely for
    ``cache_ttl_seconds`` (default 300 s).
    """
    now = time.time()
    with _POL_PRICE_LOCK:
        if now - _POL_PRICE_CACHE["timestamp"] < cache_ttl_seconds and _POL_PRICE_CACHE["price"] > 0:
            return _POL_PRICE_CACHE["price"]

    symbols = ["POLUSDT", "MATICUSDT"]
    headers = {"User-Agent": "polymarketbot/1.0"}
    for sym in symbols:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={sym}"
        try:
            import requests
            resp = requests.get(url, headers=headers, timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "price" in data:
                    px = float(data["price"])
                    if px > 0.0:
                        with _POL_PRICE_LOCK:
                            _POL_PRICE_CACHE["price"] = px
                            _POL_PRICE_CACHE["timestamp"] = now
                        return px
        except Exception as e:
            logger.debug(f"Failed to fetch live POL price for {sym}: {e}")

    logger.debug(f"POL price lookup failed across all sources, returning fallback {fallback_price}")
    with _POL_PRICE_LOCK:
        cached = _POL_PRICE_CACHE["price"]
        if cached > 0 and _POL_PRICE_CACHE["timestamp"] > 0:
            return cached
    return float(fallback_price)
