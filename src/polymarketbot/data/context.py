"""Shared context passed to every collector stream."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import orjson
import pyarrow as pa

from polymarketbot.logs import get_logger

logger = get_logger("data.context")


class HealthRegistry:
    """Thread-safe counters and gauges, flushed by the health stream."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, str], float] = {}
        self._gauges: dict[tuple[str, str], float] = {}
        self._labels: dict[tuple[str, str], str] = {}

    def incr(self, stream: str, metric: str, n: float = 1.0) -> None:
        with self._lock:
            key = (stream, metric)
            self._counters[key] = self._counters.get(key, 0.0) + float(n)

    def gauge(self, stream: str, metric: str, value: float, labels: dict[str, Any] | None = None) -> None:
        with self._lock:
            key = (stream, metric)
            self._gauges[key] = float(value)
            if labels:
                self._labels[key] = orjson.dumps(labels).decode()

    def snapshot(self, *, reset_counters: bool = False) -> list[dict[str, Any]]:
        now = time.time_ns()
        with self._lock:
            rows = [
                {
                    "recv_ts_ns": now,
                    "stream": stream,
                    "metric": metric,
                    "value": value,
                    "labels_json": self._labels.get((stream, metric)),
                }
                for (stream, metric), value in self._gauges.items()
            ]
            rows.extend(
                {
                    "recv_ts_ns": now,
                    "stream": stream,
                    "metric": metric,
                    "value": value,
                    "labels_json": self._labels.get((stream, metric)),
                }
                for (stream, metric), value in self._counters.items()
            )
            if reset_counters:
                self._counters.clear()
        return rows


@dataclass
class CollectorContext:
    config: dict[str, Any]
    hub: Any  # WriterHub; typed loosely to avoid a circular import
    health: HealthRegistry
    stop: asyncio.Event
    universe: Any = None  # UniverseState
    run_id: str = field(default="")

    def __post_init__(self) -> None:
        self.run_id = self.hub.run_id

    def register_table(self, table: str, schema: pa.Schema) -> None:
        self.hub.register(table, schema)

    def emit(self, table: str, row: dict[str, Any]) -> None:
        self.hub.emit(table, row)

    @property
    def stream_config(self) -> dict[str, Any]:
        return self.config.get("streams", {})

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.config.get("streams", {}).get(name, {}))

    def enabled(self) -> set[str]:
        return set(self.config.get("streams", {}).get("enabled", []))

    def log(self, stream: str):
        return get_logger(f"data.{stream}")
