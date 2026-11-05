"""Configuration loading for the data collector."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path("configs/data_collection.yaml")


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """Load the YAML config and apply the defaults the streams rely on."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"collector config not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"collector config must be a mapping: {p}")
    data.setdefault("root", "data/tape")
    data.setdefault("flush_seconds", 60)
    data.setdefault("part_seconds", 600)
    data.setdefault("health_seconds", 30)
    data.setdefault("queue_max_rows", 500_000)
    data.setdefault("compaction_workers", 2)
    streams = data.setdefault("streams", {})
    streams.setdefault("enabled", [])
    return data
