"""Polygon PoS gas pricing, POL/USD and merge-cost sizing."""

from __future__ import annotations

import threading
import time
from typing import Optional

from polymarketbot.logs import get_logger
from polymarketbot.strategy.onchain.core import ChainClient

logger = get_logger("ctf_client")

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

    Returns:
        Gas price in Gwei (float).
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
                # 30 Gwei here is an invented tip, not a measurement: the venue
                # publishes no default. It is kept because the pinned fallback
                # sequence in test_real_money_engine expects it, but it must not
                # be silent - a fee priced off a guess is a guess.
                priority_fee_gwei = 30.0
                try:
                    raw_tip = client._rpc("eth_maxPriorityFeePerGas", [])
                    if raw_tip:
                        priority_fee_gwei = int(str(raw_tip), 16) / 1e9
                except Exception as tip_err:
                    logger.warning(
                        "eth_maxPriorityFeePerGas unavailable "
                        f"({tip_err}); pricing the tip at the invented 30 Gwei "
                        "default instead of a measurement"
                    )

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

    logger.warning(
        "No gas price could be measured (RPC base fee, eth_gasPrice and the fast "
        f"oracle all failed); returning the {fallback_gwei:g} Gwei fallback. Any "
        "quote or settlement sized against this number is sized against a guess."
    )
    return float(fallback_gwei)


_POL_PRICE_CACHE: dict[str, float] = {"price": 0.092, "timestamp": 0.0}
_POL_PRICE_LOCK = threading.Lock()


def get_dynamic_pol_usd_price(
    fallback_price: float = 0.092,
    cache_ttl_seconds: float = 300.0,
) -> float:
    """Query live POL/USD exchange rate from Binance REST with caching and fallback.

    Queries POLUSDT with fallback to MATICUSDT.
    Cached thread-safely for cache_ttl_seconds (default: 300.0s).

    Args:
        fallback_price: Value returned if all queries fail (default: 0.092).
        cache_ttl_seconds: TTL cache window in seconds (default: 300.0).

    Returns:
        Live or cached POL price in USD (float).
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


def estimate_merge_cost_usd(
    pol_usd_price: Optional[float] = None,
    gas_units: int = 78_000,
    gas_price_gwei: Optional[float] = None,
    chain_client: Optional[ChainClient] = None,
) -> float:
    """Calculate dynamic dollar gas cost for CTF complete-set merge: Gas Units * Gas Price * POL/USD.

    Args:
        pol_usd_price: Current market exchange rate for POL in USD (default: dynamic lookup).
        gas_units: Number of Polygon PoS gas units consumed (default: 78,000 for mergePositions).
        gas_price_gwei: Optional explicit gas price in Gwei. If None, queries live dynamic gas price.
        chain_client: Optional ChainClient instance for RPC queries.

    Returns:
        Estimated gas cost in USD (float).
    """
    gwei = (
        float(gas_price_gwei)
        if gas_price_gwei is not None
        else get_dynamic_gas_price_gwei(chain_client=chain_client)
    )
    px = float(pol_usd_price) if pol_usd_price is not None else get_dynamic_pol_usd_price()
    gas_fee_pol = gas_units * (gwei * 1e-9)
    gas_cost_usd = gas_fee_pol * px
    return round(gas_cost_usd, 6)


def calculate_optimal_merge_threshold(
    gas_cost_usd: Optional[float] = None,
    fee_basis_points: float = 0.0,
    max_gas_friction_ratio: float = 0.0015,
    min_threshold_shares: float = 1.0,
    pol_usd_price: Optional[float] = None,
    chain_client: Optional[ChainClient] = None,
) -> float:
    """Calculate the optimal complete-set merge threshold in shares.

    Ensures complete sets are merged only when capital liberated exceeds
    the gas friction hurdle. At baseline $0.015 gas and 15 bps hurdle, returns 10.0 complete sets.
    Dynamically scales with live on-chain gas costs and live POL price.

    Formula:
        hurdle_ratio = max_gas_friction_ratio + (fee_basis_points / 10_000.0)
        threshold = gas_cost_usd / hurdle_ratio

    Args:
        gas_cost_usd: Dynamic USD cost of mergePositions transaction. If None, estimates dynamically.
        fee_basis_points: Optional exchange/protocol fee or hurdle in basis points (1 bp = 0.0001).
        max_gas_friction_ratio: Maximum tolerable gas drag as fraction of capital (default: 0.0015 = 15 bps).
        min_threshold_shares: Minimum threshold floor in complete sets (default: 1.0).
        pol_usd_price: Optional POL exchange rate in USD (default: dynamic lookup).
        chain_client: Optional ChainClient instance for RPC queries.

    Returns:
        Optimal threshold in complete sets (float).
    """
    if gas_cost_usd is None:
        cost = estimate_merge_cost_usd(pol_usd_price=pol_usd_price, chain_client=chain_client)
    else:
        cost = float(gas_cost_usd)

    if cost <= 0.0:
        return float(min_threshold_shares)

    hurdle = float(max_gas_friction_ratio) + (float(fee_basis_points) / 10_000.0)
    if hurdle <= 0.0:
        hurdle = 0.0015

    optimal_shares = cost / hurdle
    return max(float(min_threshold_shares), round(optimal_shares, 2))
