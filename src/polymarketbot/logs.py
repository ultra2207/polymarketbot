"""Logging that survives losing a terminal and is easy to audit per source file.

Why this exists
---------------
A long job that prints to stdout loses everything the moment its output is
piped through `tail`, killed at a timeout, or run detached. Rerunning to
recover an answer costs minutes to hours and teaches nothing new.

Every source file that logs gets its own rotating file under ``data/logs/``
*and* every record is also appended to ``data/logs/app.log``.  The per-file
record is keyed by the source file of the *caller* (not by the label passed to
``get_logger``), so two modules that both use the label ``paper`` still
write separate ``engine.log`` / ``main.log`` files.

One process can host several loggers (the collector imports every stream
module). Sinks are keyed by source file and filtered, not replaced: a later
``get_logger`` call must never steal the earlier module's file.

Environment
-----------
``LOG_DIR``        override output directory (default ``data/logs``)
``LOG_LEVEL``      per-file level (default ``INFO``)
``LOG_APP_LEVEL``  app.log level (defaults to ``LOG_LEVEL``)
``LOG_ROTATION``   rotation threshold (default ``10 MB``)
``LOG_RETENTION``  rotations to keep per file (default ``3``)
``LOG_COMPRESSION`` compression for rotated files (default ``gz``)

Usage
    from polymarketbot.logs import get_logger
    log = get_logger("screen_calibration")
    log.info("quarter {} done in {:.1f}s", era, secs)
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
import threading
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# Repository root: the nearest ancestor containing pyproject.toml.  Falling
# back to cwd keeps ad-hoc scripts outside the repo usable.
_ROOT = Path(__file__).resolve().parent
while not (_ROOT / "pyproject.toml").exists() and _ROOT != _ROOT.parent:
    _ROOT = _ROOT.parent
if not (_ROOT / "pyproject.toml").exists():
    _ROOT = Path.cwd().resolve()

LOG_DIR = Path(os.environ.get("LOG_DIR", "data/logs"))
# Human-facing timestamps are IST. Parquet partitions and epoch fields stay UTC.
IST = timezone(timedelta(hours=5, minutes=30), "IST")
_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss} IST | {level: <7} | {extra[name]} | "
    "{file.name}:{line} | {message}"
)

_ROTATION = os.environ.get("LOG_ROTATION", "10 MB")
_RETENTION = int(os.environ.get("LOG_RETENTION", "3"))
_COMPRESSION = os.environ.get("LOG_COMPRESSION", "gz")
_FILE_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
_APP_LEVEL = os.environ.get("LOG_APP_LEVEL", _FILE_LEVEL)

# Remove loguru's default stderr handler once; every sink below is explicit.
logger.remove()


def _stamp(record: Any) -> None:
    """Render timestamps in IST and guarantee ``extra[name]`` exists."""
    record["time"] = record["time"].astimezone(IST)
    record["extra"].setdefault(
        "name", Path(record["file"].name).stem or "unknown"
    )


logger.configure(patcher=_stamp)

_SINKS: dict[str, list[int]] = {}
_SINK_LOCK = threading.Lock()
_APP_SINK: Optional[int] = None


def _source_slug(frame: Any) -> tuple[str, str]:
    """Return ``(absolute_source_path, unique_slug)`` for a caller frame.

    The slug is a dotted module path relative to the repository root, so
    ``src/polymarketbot/paper/engine.py`` becomes
    ``polymarketbot.paper.engine`` and ``src/polymarketbot/paper/main.py``
    becomes ``polymarketbot.paper.main``.
    """
    raw = frame.f_code.co_filename
    if raw.startswith("<"):
        return raw, "interactive"
    path = Path(raw).resolve()
    try:
        rel = path.relative_to(_ROOT)
    except ValueError:
        return str(path), path.stem
    parts = list(rel.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    slug = ".".join(parts) or path.stem
    return str(path), slug


def _add_app_sink() -> None:
    """Create the aggregate sink exactly once per process."""
    global _APP_SINK
    if _APP_SINK is not None:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _APP_SINK = logger.add(
        LOG_DIR / "app.log",
        level=_APP_LEVEL,
        format=_FORMAT,
        rotation=_ROTATION,
        retention=_RETENTION,
        compression=_COMPRESSION,
        encoding="utf-8",
        enqueue=True,
    )


_add_app_sink()


class _InterceptHandler(logging.Handler):
    """Route stdlib ``logging`` records into loguru (loguru's documented recipe)."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - glue
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == __file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)


def get_logger(
    name: Optional[str] = None,
    *,
    level: Optional[str] = None,
    file: Optional[str] = None,
) -> Any:
    """A logger bound to one source file, writing to stderr, its file, and app.log.

    ``name`` is the label shown in the log line.  ``file`` overrides the
    per-file log name (rarely needed).  Repeated calls from the same source
    file reuse the same sinks, so importing a module twice does not duplicate
    output.
    """
    caller = inspect.currentframe()
    assert caller is not None
    frame = caller.f_back
    source, slug = _source_slug(frame) if frame is not None else ("<unknown>", "unknown")
    label = name or (frame.f_globals.get("__name__") if frame is not None else None) or slug
    log_name = file or slug
    effective_level = level or _FILE_LEVEL

    with _SINK_LOCK:
        if source not in _SINKS:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / f"{log_name}.log"

            def only(record: Any, _slug: str = log_name) -> bool:
                return record["extra"].get("_sink") == _slug

            stderr_id: Optional[int] = None
            if sys.stderr is not None:
                stderr_id = logger.add(
                    sys.stderr,
                    level=effective_level,
                    format=_FORMAT,
                    colorize=True,
                    filter=only,
                )
            file_id = logger.add(
                path,
                level=effective_level,
                format=_FORMAT,
                rotation=_ROTATION,
                retention=_RETENTION,
                compression=_COMPRESSION,
                encoding="utf-8",
                # One enqueue thread per source file is dozens of threads in the
                # soak. The per-file sinks are low-volume; write synchronously and
                # keep the aggregate app.log asynchronous.
                enqueue=False,
                filter=only,
            )
            _SINKS[source] = [sid for sid in (stderr_id, file_id) if sid is not None]

    return logger.bind(name=label, _sink=log_name)
