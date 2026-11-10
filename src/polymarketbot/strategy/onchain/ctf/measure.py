"""Measured Polygon PoS timing probes: block cadence and RPC round trip."""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Optional, Tuple

from polymarketbot.logs import get_logger

logger = get_logger("ctf_client")

# Polygon PoS block-header tape (one row per head). Consecutive block-number
# deltas are the measured inter-block time; an absent tape yields ``None`` so
# callers queue the on-chain action and report the gap instead of assuming 0 s.
DEFAULT_POLYGON_GAS_TAPE = Path(__file__).resolve().parents[5] / "data" / "tape" / "polygon_gas"
_BLOCK_INTERVAL_CACHE: dict[str, Tuple[float, Optional[float]]] = {}


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
    import json

    target = Path(path or (Path(__file__).resolve().parents[5] / "data" / "derived" / "local_latency.json"))
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
