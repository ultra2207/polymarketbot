"""Stream discovery.

Every module in this package that defines ``NAME`` and ``async def run(ctx)``
is a stream. Import order is alphabetical and irrelevant; the orchestrator
filters by ``streams.enabled`` in the config.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from polymarketbot.data.context import CollectorContext

StreamRun = Callable[[CollectorContext], Awaitable[None]]


@dataclass(frozen=True)
class StreamModule:
    name: str
    run: StreamRun


def discover() -> dict[str, StreamModule]:
    found: dict[str, StreamModule] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as exc:  # noqa: BLE001
            # A half-written sibling module must not stop the streams that are
            # already valid. The orchestrator logs the missing name.
            from polymarketbot.logs import get_logger

            get_logger("data.streams").error(f"stream module {info.name} failed to import: {exc}")
            continue
        name = getattr(module, "NAME", info.name)
        run = getattr(module, "run", None)
        if run is None:
            continue
        found[name] = StreamModule(name=name, run=run)
    return found


__all__: list[str] = ["StreamModule", "discover"]
