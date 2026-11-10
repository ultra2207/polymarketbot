"""Polygon PoS gas pricing and POL/USD, from measurements only.

Every number this module returns is a *measurement*: a live JSON-RPC call, the
Polygon gas oracle, a listed POL/USD market, or a previously recorded
measurement replayed together with its age. When none of those resolve, the
functions return ``None`` and the caller must refuse.

There is deliberately no fallback constant. A fee priced off a guess is a
guess, and a guessed fee does one of two wrong things: it merges when the merge
loses money, or it forgoes a merge that was profitable. The previous
implementation priced the EIP-1559 tip at an invented 30 Gwei, the all-in fee
at an invented 40 Gwei and POL at an invented $0.092. Measured on 2026-11-09
those were wrong by 20 % (the chain published a 25 Gwei tip), up to 33 % (the
oracle said 290 Gwei while three RPCs agreed on 217.54) and 5.8 % (POL traded
0.08686) respectively.

The estimator is a median over structurally independent sources. The spread
between them is logged on every call, so a divergence (an oracle that has
drifted, one RPC lagging a fork) is visible instead of silently averaged away.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from polymarketbot.logs import get_logger
from polymarketbot.strategy.onchain.core import ChainClient

log = get_logger("ctf_client")

GAS_ORACLE_URL = "https://gasstation.polygon.technology/v2"

# Public Polygon JSON-RPC endpoints, verified reachable and EIP-1559 capable
# from the operating host on 2026-11-09. Order is by measured response time.
# Override with POLYGON_RPC_URLS (comma-separated): a dedicated endpoint is
# preferred in production and is not a price, so it is configuration.
PUBLIC_RPC_URLS: Tuple[str, ...] = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
)

# Four listed POL/USD venues. Measured agreement on 2026-11-09 was 0.1 %
# (binance 0.086860, coinbase 0.086840, okx 0.086860, kraken 0.086880).
# MATICUSDT is deliberately absent: MATIC and POL are separate markets and a
# stale MATIC quote is a wrong measurement, not a fallback.
POL_PRICE_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("binance_polusdt", "https://api.binance.com/api/v3/ticker/price?symbol=POLUSDT"),
    ("coinbase_pol_usd", "https://api.coinbase.com/v2/prices/POL-USD/spot"),
    ("okx_pol_usdt", "https://www.okx.com/api/v5/market/ticker?instId=POL-USDT"),
    ("kraken_polusd", "https://api.kraken.com/0/public/Ticker?pair=POLUSD"),
)

# Overridable so a test (or a second stratum of the trader) keeps its recorded
# measurements out of the live ledger.
GAS_STATE_PATH = Path(os.environ.get("POLYMARKET_GAS_STATE_PATH", "data/derived/onchain_gas.json"))
POL_STATE_PATH = Path(os.environ.get("POLYMARKET_POL_STATE_PATH", "data/derived/onchain_pol.json"))

# How old a *recorded* measurement may be before it stops being evidence. This
# is a policy bound, not a price, and it is logged with the age it applied.
# 300 s matches the TTL already used for the POL quote cache.
DEFAULT_MAX_STALE_SECONDS = 300.0
# A measured gas price stays a measurement for a few blocks. The reward scanner
# calls this per candidate market, so without a cache the ladder would issue a
# dozen HTTP requests per market; without a *short* TTL it would price a 10 s
# trading loop off a minute-old number.
DEFAULT_CACHE_TTL_SECONDS = 5.0
_RPC_TIMEOUT_S = 6.0


class GasUnmeasured(RuntimeError):
    """Raised when a money-moving path cannot obtain a measured gas price."""


@dataclass(frozen=True)
class GasReading:
    gwei: float
    source: str
    samples: Dict[str, float]
    spread_pct: float
    measured_at: float
    stale_seconds: float = 0.0

    @property
    def is_stale(self) -> bool:
        return self.stale_seconds > 0.0


@dataclass(frozen=True)
class PolReading:
    usd: float
    source: str
    samples: Dict[str, float]
    spread_pct: float
    measured_at: float
    stale_seconds: float = 0.0

    @property
    def is_stale(self) -> bool:
        return self.stale_seconds > 0.0


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------


def _hex_to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            return int(value, 16) if value.startswith("0x") else int(float(value))
        return int(value)
    except (TypeError, ValueError):
        return None


def _hex_to_gwei(value: Any) -> Optional[float]:
    """Wei as Gwei, or None when the value is absent / zero / undecodable."""
    raw = _hex_to_int(value)
    if raw is None or raw <= 0:
        return None
    return raw / 1e9


def _spread_pct(samples: Dict[str, float]) -> float:
    values = [v for v in samples.values() if v and v > 0]
    if len(values) < 2:
        return 0.0
    centre = statistics.median(values)
    if centre <= 0:
        return 0.0
    return (max(values) - min(values)) / centre * 100.0


def _aggregate(samples: Dict[str, float]) -> Tuple[Optional[float], str]:
    """Median over independent sources; the spread is reported by the caller."""
    values = {k: v for k, v in samples.items() if v and v > 0}
    if not values:
        return None, "unmeasured"
    return statistics.median(values.values()), f"median_of_{len(values)}"


def _max_stale_seconds(explicit: Optional[float], env_key: str) -> float:
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get(env_key)
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            log.warning(f"{env_key}={raw!r} is not a positive number; using the default")
    return DEFAULT_MAX_STALE_SECONDS


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------


def _http_rpc(url: str, method: str, params: list) -> Any:
    import requests

    try:
        resp = requests.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={"Content-Type": "application/json", "User-Agent": "polymarketbot/1.0"},
            timeout=_RPC_TIMEOUT_S,
        )
        if resp.status_code != 200:
            log.debug(f"{method} at {url} -> HTTP {resp.status_code}")
            return None
        payload = resp.json()
        if not isinstance(payload, dict):
            return None
        if payload.get("error"):
            log.debug(f"{method} at {url} -> {payload['error']}")
            return None
        return payload.get("result")
    except Exception as exc:  # noqa: BLE001 - one dead endpoint is not a reason to give up
        log.debug(f"{method} at {url} failed: {exc}")
        return None


def _client_rpc(client: Any, method: str, params: list) -> Any:
    try:
        return client._rpc(method, params)
    except Exception as exc:  # noqa: BLE001 - try the next measurement source
        log.debug(f"{method} via chain client failed: {exc}")
        return None


def _fee_history_tip(call: Callable[[str, list], Any]) -> Optional[float]:
    """Median p50 priority fee actually paid over the last blocks."""
    history = call("eth_feeHistory", ["0x5", "latest", [50]])
    if not isinstance(history, dict):
        return None
    paid: list[int] = []
    for row in history.get("reward") or []:
        if isinstance(row, (list, tuple)):
            for value in row:
                raw = _hex_to_int(value)
                if raw is not None and raw > 0:
                    paid.append(raw)
    if not paid:
        return None
    return statistics.median(paid) / 1e9


def _samples_from_transport(
    call: Callable[[str, list], Any],
    label: str,
) -> Dict[str, float]:
    """All-in gas price samples from one RPC, each with its own provenance."""
    samples: Dict[str, float] = {}

    block = call("eth_getBlockByNumber", ["latest", False])
    base = _hex_to_gwei(block.get("baseFeePerGas")) if isinstance(block, dict) else None

    gas_price = _hex_to_gwei(call("eth_gasPrice", []))
    if gas_price:
        samples[f"{label}:eth_gasPrice"] = gas_price

    # The tip is a measurement from the first source that answers. Every one of
    # these is a real observation; none is a default.
    tip = _hex_to_gwei(call("eth_maxPriorityFeePerGas", []))
    if tip is None:
        tip = _fee_history_tip(call)
    if tip is None and gas_price and base and gas_price > base:
        tip = gas_price - base
    if base and tip:
        samples[f"{label}:base+tip"] = base + tip
    elif base and gas_price:
        samples[f"{label}:base(+gasPrice tip unknown)"] = base

    return samples


def _oracle_samples() -> Dict[str, float]:
    """Fast-tier total fee, and the priority fee it publishes separately."""
    import requests

    out: Dict[str, float] = {}
    try:
        resp = requests.get(GAS_ORACLE_URL, timeout=2.0)
        if resp.status_code != 200:
            return out
        data = resp.json()
        fast = (data.get("fast") or data.get("standard")) if isinstance(data, dict) else None
        if isinstance(fast, dict):
            max_fee = fast.get("maxFee")
            if isinstance(max_fee, (int, float)) and max_fee > 0:
                out["oracle:fast.maxFee"] = float(max_fee)
            max_tip = fast.get("maxPriorityFee")
            if isinstance(max_tip, (int, float)) and max_tip > 0:
                out["oracle:fast.maxPriorityFee"] = float(max_tip)
    except Exception as exc:  # noqa: BLE001 - the oracle is one source, not the only one
        log.debug(f"fast gas oracle unavailable: {exc}")
    return out


def _rpc_urls(explicit: Optional[Tuple[str, ...]]) -> Tuple[str, ...]:
    if explicit:
        return tuple(explicit)
    raw = os.environ.get("POLYGON_RPC_URLS")
    if raw:
        urls = tuple(part.strip() for part in raw.split(",") if part.strip())
        if urls:
            return urls
    return PUBLIC_RPC_URLS


# --------------------------------------------------------------------------
# persistence: a recorded measurement is still a measurement
# --------------------------------------------------------------------------


def _load_state(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001 - a corrupt cache is not fatal
        log.debug(f"could not read {path}: {exc}")
        return None


def _save_state(path: Path, payload: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 - failing to cache must not fail the read
        log.debug(f"could not write {path}: {exc}")


# --------------------------------------------------------------------------
# gas
# --------------------------------------------------------------------------

_GAS_CACHE: Dict[str, Any] = {"reading": None, "ts": 0.0}
_GAS_LOCK = threading.Lock()


def _cache_ttl(explicit: Optional[float], env_key: str) -> float:
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get(env_key)
    if raw:
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            log.warning(f"{env_key}={raw!r} is not a number; using the default")
    return DEFAULT_CACHE_TTL_SECONDS



def measure_gas(
    chain_client: Optional[ChainClient] = None,
    rpc_url: Optional[str] = None,
    rpc_urls: Optional[Tuple[str, ...]] = None,
    max_stale_seconds: Optional[float] = None,
    persist: bool = True,
    cache_ttl_seconds: Optional[float] = None,
) -> Optional[GasReading]:
    """Measure the all-in Polygon gas price, or return None. Never a default."""
    # A caller that supplies its own transport is asking for *that* view; it is
    # never served another transport's cached reading. The cache exists for the
    # shared public path, which the per-market scanner hammers.
    explicit_transport = chain_client is not None or rpc_url is not None
    ttl = 0.0 if explicit_transport else _cache_ttl(cache_ttl_seconds, "POLYGON_GAS_CACHE_TTL_S")
    now = time.time()
    if ttl > 0:
        with _GAS_LOCK:
            cached: Optional[GasReading] = _GAS_CACHE.get("reading")
            cached_ts = float(_GAS_CACHE.get("ts") or 0.0)
        if cached is not None and (now - cached_ts) < ttl:
            return cached

    transports: list[Tuple[str, Callable[[str, list], Any]]] = []

    client = chain_client
    if client is None and rpc_url is not None:
        try:
            client = ChainClient(url=rpc_url)
        except Exception as exc:  # noqa: BLE001 - fall through to the public list
            log.debug(f"could not build ChainClient for {rpc_url}: {exc}")

    if client is not None:
        transports.append(("client", lambda m, p: _client_rpc(client, m, p)))
    else:
        for url in _rpc_urls(rpc_urls):
            host = url.split("//", 1)[-1].split("/", 1)[0]
            transports.append((host, lambda m, p, u=url: _http_rpc(u, m, p)))

    samples: Dict[str, float] = {}
    for label, call in transports:
        samples.update(_samples_from_transport(call, label))

    # The RPC authorities all measure the same quantity: the price a
    # transaction pays now (base fee plus tip, or the node's equivalent
    # eth_gasPrice). They are the primary estimator because they are what the
    # settlement will actually be charged.
    rpc_totals = {
        k: v for k, v in samples.items() if not k.startswith("oracle:") and not k.endswith("maxPriorityFee")
    }
    gwei, source = _aggregate(rpc_totals)
    spread = _spread_pct(rpc_totals)

    # The gas-station ``maxFee`` is a *cap recommendation* with headroom, not
    # the current price: it belongs in a different estimator, never averaged
    # into the RPC measurements. Use it only when no RPC answered, and always
    # report the gap so a drifting oracle is visible.
    oracle = _oracle_samples()
    samples.update({k: v for k, v in oracle.items() if ":" in k})
    oracle_cap = oracle.get("oracle:fast.maxFee")
    if gwei is not None and oracle_cap:
        log.debug(
            f"oracle maxFee ${oracle_cap:.2f} Gwei vs measured {gwei:.2f} Gwei "
            f"({(oracle_cap - gwei) / gwei * 100.0:+.1f} % headroom)"
        )
    if gwei is None:
        gwei, source = _aggregate(
            {k: v for k, v in oracle.items() if not k.endswith("maxPriorityFee")}
        )
        spread = _spread_pct(
            {k: v for k, v in oracle.items() if not k.endswith("maxPriorityFee")}
        )
        if gwei is not None:
            source = "oracle_cap_no_rpc"

    if gwei is not None:
        if source != "unmeasured":
            log.info(
                f"gas measured {gwei:.2f} Gwei ({source}, spread {spread:.1f} %): "
                + ", ".join(f"{k}={v:.2f}" for k, v in sorted(samples.items()))
            )
        reading = GasReading(
            gwei=round(float(gwei), 4),
            source=source,
            samples=dict(samples),
            spread_pct=round(spread, 3),
            measured_at=time.time(),
        )
        if ttl > 0:
            with _GAS_LOCK:
                _GAS_CACHE["reading"] = reading
                _GAS_CACHE["ts"] = time.time()
        if persist:
            _save_state(
                GAS_STATE_PATH,
                {
                    "gwei": reading.gwei,
                    "source": reading.source,
                    "samples": reading.samples,
                    "spread_pct": reading.spread_pct,
                    "ts": reading.measured_at,
                },
            )
        return reading

    stale_limit = _max_stale_seconds(max_stale_seconds, "POLYGON_GAS_MAX_STALE_S")
    cached = _load_state(GAS_STATE_PATH)
    if cached and isinstance(cached.get("gwei"), (int, float)) and cached.get("gwei", 0) > 0:
        age = max(0.0, time.time() - float(cached.get("ts") or 0.0))
        if age <= stale_limit:
            log.warning(
                f"gas unmeasurable now; replaying the recorded measurement "
                f"{float(cached['gwei']):.2f} Gwei from {cached.get('source')} "
                f"({age:.0f}s old, limit {stale_limit:.0f}s)"
            )
            return GasReading(
                gwei=float(cached["gwei"]),
                source=f"recorded({cached.get('source')}, {age:.0f}s old)",
                samples=dict(cached.get("samples") or {}),
                spread_pct=float(cached.get("spread_pct") or 0.0),
                measured_at=float(cached.get("ts") or 0.0),
                stale_seconds=age,
            )
        log.error(
            f"gas unmeasurable and the recorded measurement is {age:.0f}s old "
            f"(limit {stale_limit:.0f}s); refusing to price gas"
        )
        return None

    log.error(
        "gas unmeasurable: no RPC, no oracle and no recorded measurement. "
        "Refusing to return a default - a guessed fee merges at a loss."
    )
    return None


def get_dynamic_gas_price_gwei(
    chain_client: Optional[ChainClient] = None,
    rpc_url: Optional[str] = None,
    rpc_urls: Optional[Tuple[str, ...]] = None,
    max_stale_seconds: Optional[float] = None,
    cache_ttl_seconds: Optional[float] = None,
) -> Optional[float]:
    """Measured all-in gas price in Gwei, or None when it cannot be measured."""
    reading = measure_gas(
        chain_client=chain_client,
        rpc_url=rpc_url,
        rpc_urls=rpc_urls,
        max_stale_seconds=max_stale_seconds,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    return None if reading is None else reading.gwei


# --------------------------------------------------------------------------
# POL/USD
# --------------------------------------------------------------------------

_POL_CACHE: Dict[str, Any] = {"reading": None, "ts": 0.0}
_POL_LOCK = threading.Lock()


def _pol_from_source(name: str, url: str) -> Optional[float]:
    import requests

    try:
        resp = requests.get(url, headers={"User-Agent": "polymarketbot/1.0"}, timeout=3.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        price: Optional[float] = None
        if isinstance(data, dict):
            if "price" in data:
                price = float(data["price"])
            elif isinstance(data.get("data"), dict) and "amount" in data["data"]:
                price = float(data["data"]["amount"])
            elif isinstance(data.get("data"), list) and data["data"]:
                entry = data["data"][0]
                price = float(entry.get("last") or entry.get("bidPx") or 0.0)
            elif isinstance(data.get("result"), dict):
                quote = next(iter(data["result"].values()), {})
                last = quote.get("c") if isinstance(quote, dict) else None
                if isinstance(last, list) and last:
                    price = float(last[0])
        if price and price > 0:
            return price
    except Exception as exc:  # noqa: BLE001 - try the next venue
        log.debug(f"POL price unavailable from {name}: {exc}")
    return None


def measure_pol_usd(
    cache_ttl_seconds: float = 300.0,
    max_stale_seconds: Optional[float] = None,
    persist: bool = True,
) -> Optional[PolReading]:
    """Median POL/USD across listed venues, or None. Never a default."""
    now = time.time()
    with _POL_LOCK:
        cached: Optional[PolReading] = _POL_CACHE.get("reading")
        cached_ts = float(_POL_CACHE.get("ts") or 0.0)
    if cached is not None and (now - cached_ts) < cache_ttl_seconds:
        return cached

    samples: Dict[str, float] = {}
    for name, url in POL_PRICE_SOURCES:
        price = _pol_from_source(name, url)
        if price is not None:
            samples[name] = price

    usd, source = _aggregate(samples)
    spread = _spread_pct(samples)

    if usd is not None:
        if spread > 1.0:
            # Log only: a wide spread is a data-quality signal, not a reason to
            # discard a measurement and substitute a number of our own.
            log.warning(
                f"POL/USD sources disagee by {spread:.2f} %: "
                + ", ".join(f"{k}={v:.6f}" for k, v in sorted(samples.items()))
            )
        else:
            log.info(
                f"POL/USD {usd:.6f} ({source}, spread {spread:.3f} %) from "
                + ", ".join(sorted(samples))
            )
        reading = PolReading(
            usd=float(usd),
            source=source,
            samples=dict(samples),
            spread_pct=round(spread, 3),
            measured_at=now,
        )
        with _POL_LOCK:
            _POL_CACHE["reading"] = reading
            _POL_CACHE["ts"] = now
        if persist:
            _save_state(
                POL_STATE_PATH,
                {"usd": reading.usd, "source": source, "samples": samples, "spread_pct": spread, "ts": now},
            )
        return reading

    stale_limit = _max_stale_seconds(max_stale_seconds, "POLYGON_POL_MAX_STALE_S")
    recorded = _load_state(POL_STATE_PATH)
    if recorded and isinstance(recorded.get("usd"), (int, float)) and recorded.get("usd", 0) > 0:
        age = max(0.0, now - float(recorded.get("ts") or 0.0))
        if age <= stale_limit:
            log.warning(
                f"POL/USD unavailable from every venue; replaying the recorded "
                f"{float(recorded['usd']):.6f} from {recorded.get('source')} "
                f"({age:.0f}s old, limit {stale_limit:.0f}s)"
            )
            return PolReading(
                usd=float(recorded["usd"]),
                source=f"recorded({recorded.get('source')}, {age:.0f}s old)",
                samples=dict(recorded.get("samples") or {}),
                spread_pct=float(recorded.get("spread_pct") or 0.0),
                measured_at=float(recorded.get("ts") or 0.0),
                stale_seconds=age,
            )
        log.error(
            f"POL/USD unavailable and the recorded value is {age:.0f}s old "
            f"(limit {stale_limit:.0f}s); refusing to price gas"
        )
        return None

    log.error("POL/USD unmeasurable from every venue and nothing recorded; refusing to guess")
    return None


def get_dynamic_pol_usd_price(
    cache_ttl_seconds: float = 300.0,
    max_stale_seconds: Optional[float] = None,
) -> Optional[float]:
    """Measured POL/USD, or None when it cannot be measured."""
    reading = measure_pol_usd(
        cache_ttl_seconds=cache_ttl_seconds,
        max_stale_seconds=max_stale_seconds,
    )
    return None if reading is None else reading.usd


# --------------------------------------------------------------------------
# merge cost
# --------------------------------------------------------------------------


def estimate_merge_cost_usd(
    pol_usd_price: Optional[float] = None,
    gas_units: int = 78_000,
    gas_price_gwei: Optional[float] = None,
    chain_client: Optional[ChainClient] = None,
) -> Optional[float]:
    """Dollar cost of one mergePositions: gas units x gas price x POL/USD.

    ``None`` means unmeasurable, never zero. ``gas_units`` is the documented
    budget for a direct merge and is overridden from measured receipts by the
    callers that have one.
    """
    gwei = float(gas_price_gwei) if gas_price_gwei is not None else get_dynamic_gas_price_gwei(
        chain_client=chain_client
    )
    px = float(pol_usd_price) if pol_usd_price is not None else get_dynamic_pol_usd_price()
    if gwei is None or px is None:
        log.warning(
            f"merge gas cost unmeasured (gwei={gwei}, pol_usd={px}); refusing to price it"
        )
        return None
    gas_fee_pol = gas_units * (gwei * 1e-9)
    return round(gas_fee_pol * px, 6)


def calculate_optimal_merge_threshold(
    gas_cost_usd: Optional[float] = None,
    fee_basis_points: float = 0.0,
    max_gas_friction_ratio: float = 0.0015,
    min_threshold_shares: float = 1.0,
    pol_usd_price: Optional[float] = None,
    chain_client: Optional[ChainClient] = None,
) -> float:
    """Minimum complete sets whose gas drag stays under ``max_gas_friction_ratio``.

    Raises :class:`GasUnmeasured` when no cost can be measured: sizing a merge
    threshold off a guess is the failure this guard exists to prevent.
    """
    if gas_cost_usd is None:
        cost = estimate_merge_cost_usd(pol_usd_price=pol_usd_price, chain_client=chain_client)
    else:
        cost = float(gas_cost_usd)

    if cost is None:
        raise GasUnmeasured(
            "cannot size the merge threshold: gas price or POL/USD is unmeasured"
        )

    if cost <= 0.0:
        return float(min_threshold_shares)

    hurdle = float(max_gas_friction_ratio) + (float(fee_basis_points) / 10_000.0)
    if hurdle <= 0.0:
        hurdle = 0.0015

    return max(float(min_threshold_shares), round(cost / hurdle, 2))


def main() -> int:
    """Print the current measurements and every sample behind them."""
    gas = measure_gas()
    pol = measure_pol_usd()

    if gas is None:
        print("gas: UNMEASURED (no RPC, no oracle, no recorded measurement)")
    else:
        age = f", {gas.stale_seconds:.0f}s old" if gas.is_stale else ""
        print(f"gas: {gas.gwei:.4f} Gwei via {gas.source}{age} (spread {gas.spread_pct:.2f} %)")
        for key, value in sorted(gas.samples.items()):
            print(f"    {key:<40s} {value:>12.4f}")

    if pol is None:
        print("POL/USD: UNMEASURED")
    else:
        age = f", {pol.stale_seconds:.0f}s old" if pol.is_stale else ""
        print(f"POL/USD: {pol.usd:.6f} via {pol.source}{age} (spread {pol.spread_pct:.3f} %)")
        for key, value in sorted(pol.samples.items()):
            print(f"    {key:<40s} {value:>12.6f}")

    cost = estimate_merge_cost_usd()
    print(f"merge cost: {'UNMEASURED' if cost is None else f'${cost:.6f}'}")
    return 0 if (gas is not None and pol is not None) else 1


if __name__ == "__main__":
    raise SystemExit(main())
