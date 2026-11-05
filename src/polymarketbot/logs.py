"""Logging that survives losing a terminal.

Why this exists
---------------
A long job that prints to stdout loses everything the moment its output is
piped through `tail`, killed at a timeout, or run detached. Rerunning to
recover an answer costs minutes to hours and teaches nothing new.

Every script logs to `data/logs/<name>.log` as well as stderr, so the record of
what a killed run actually did is always on disk and readable afterwards.

One process can host several loggers (the collector imports every stream
module). Sinks are therefore keyed by name and filtered, not replaced: a later
``get_logger`` call must never steal the earlier module's file.

Usage
    from polymarketbot.logs import get_logger
    log = get_logger("screen_calibration")
    log.info("quarter {} done in {:.1f}s", era, secs)
"""

from __future__ import annotations

import sys
import threading
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

LOG_DIR = Path("data/logs")
# Human-facing timestamps are IST. Parquet partitions and epoch fields stay UTC.
IST = timezone(timedelta(hours=5, minutes=30), "IST")
_FORMAT = ("{time:YYYY-MM-DD HH:mm:ss} IST | {level: <7} | {extra[name]} | "
           "{message} | {elapsed}")

# Remove loguru's default stderr handler once; every sink below is explicit.
logger.remove()
logger.patch(lambda record: record.update(time=record["time"].astimezone(IST)))
_SINKS: dict[str, list[int]] = {}
_LOCK = threading.Lock()


def get_logger(name: str, *, level: str = "INFO") -> Any:
    """A logger bound to one script name, writing to stderr and to disk.

    Repeated calls for the same name reuse the sinks. Calls for different names
    add their own filtered sinks, so modules sharing a process keep separate
    files and neither loses the other's output.
    """
    with _LOCK:
        if name not in _SINKS:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / f"{name}.log"

            def only(record: Any, _name: str = name) -> bool:
                return record["extra"].get("name") == _name

            stderr_id = None
            if sys.stderr is not None:
                stderr_id = logger.add(
                    sys.stderr, level=level, format=_FORMAT, colorize=True,
                    filter=only,
                )
            file_id = logger.add(
                path, level=level, format=_FORMAT, rotation="10 MB",
                retention=5, encoding="utf-8", enqueue=True, filter=only,
            )
            _SINKS[name] = [sid for sid in (stderr_id, file_id) if sid is not None]
    return logger.bind(name=name)
