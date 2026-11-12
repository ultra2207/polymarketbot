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

import gzip
import inspect
import logging
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

# Repository root: the nearest ancestor containing pyproject.toml.  Falling
# back to cwd keeps ad-hoc scripts outside the repo usable.
_ROOT = Path(__file__).resolve().parent
while not (_ROOT / "pyproject.toml").exists() and _ROOT != _ROOT.parent:
    _ROOT = _ROOT.parent
if not (_ROOT / "pyproject.toml").exists():
    _ROOT = Path.cwd().resolve()

def _resolve_log_dir(raw: str) -> Path:
    """Anchor a relative ``LOG_DIR`` to the repository root, never the caller's cwd.

    A tool or a test launched from another directory must still write into the
    checkout's ``data/logs``; resolving against cwd scattered log files into
    whatever directory the process happened to start in.
    """
    path = Path(raw)
    return path if path.is_absolute() else (_ROOT / path).resolve()


LOG_DIR = _resolve_log_dir(os.environ.get("LOG_DIR", "data/logs"))
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

#: How many times a rotation rename is retried before it is deferred. Bounded so
#: a second process holding the file cannot stall a logging call.
ROTATION_ATTEMPTS = 5
ROTATION_RETRY_SLEEP_S = 0.05

_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}

# Remove loguru's default stderr handler once; every sink below is explicit.
logger.remove()


def _parse_size(text: str) -> float | None:
    """``"10 MB"`` / ``"512 KB"`` -> bytes; ``None`` when it is not a size."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGB]{0,2})\s*", text.upper())
    if not match:
        return None
    value, unit = float(match.group(1)), match.group(2) or "B"
    if unit not in _SIZE_UNITS:
        return None
    return value * _SIZE_UNITS[unit]


class _RotatingFileSink:
    """Append to one log file and rotate it without ever dropping a record.

    loguru's own file sink renames the live file. On Windows a rename raises
    ``PermissionError`` whenever another process holds that file open, and two
    bot processes always hold ``app.log`` open, so the rotation the sink asked
    for could not happen and the handler discarded the record.

    This sink owns its handle. On rotation it closes, retries the rename a
    bounded number of times, and if the rename is still refused it keeps
    appending to the same file and says so once on stderr. The file then grows
    past the threshold until the other process lets go - which is visible - but
    no line is lost.
    """

    def __init__(
        self,
        path: Path,
        *,
        rotation: str = _ROTATION,
        retention: int = _RETENTION,
        compression: str | None = _COMPRESSION,
        encoding: str = "utf-8",
    ) -> None:
        self.path = Path(path)
        self.encoding = encoding
        self.retention = max(0, int(retention))
        self.compression = compression or None
        self._lock = threading.Lock()
        self._fh: Any = None
        self._size = 0
        self._rotation_spec = rotation
        self._rotation_bytes: float | None = _parse_size(rotation)
        self._next_rotation_ts: float | None = None
        if self._rotation_bytes is None:
            self._next_rotation_ts = self._daily_boundary(rotation)
        self._warned = False
        if self.compression not in (None, "gz", "none", "false", "False"):
            sys.stderr.write(
                f"loguru: LOG_COMPRESSION={self.compression!r} unsupported; "
                "rotated logs stay uncompressed\n"
            )
            self.compression = None
        if self.compression in ("none", "false", "False"):
            self.compression = None

    @staticmethod
    def _daily_boundary(rotation: str) -> float | None:
        """Epoch of the next ``HH:MM`` boundary, or ``None`` for an unusable spec."""
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", rotation)
        if not match:
            sys.stderr.write(
                f"loguru: LOG_ROTATION={rotation!r} is neither a size (10 MB) nor "
                "a daily time (00:00); file logging will not rotate\n"
            )
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        now = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        boundary = now.replace(hour=min(hour, 23), minute=min(minute, 59))
        if boundary <= datetime.now(IST):
            boundary += timedelta(days=1)
        return boundary.timestamp()

    # -- sink protocol ---------------------------------------------------
    def _ensure_open(self) -> None:
        if self._fh is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "ab")  # noqa: SIM115 - the handle lives with the sink
        self._size = self.path.stat().st_size

    def _should_rotate(self, incoming: int) -> bool:
        if self._rotation_bytes is not None:
            return self._size > 0 and self._size + incoming > self._rotation_bytes
        if self._next_rotation_ts is not None:
            return time.time() >= self._next_rotation_ts and self._size > 0
        return False

    def _rotate(self) -> None:
        self._close()
        stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.stem}.{stamp}.{os.getpid()}{self.path.suffix}")
        rotated = False
        for attempt in range(ROTATION_ATTEMPTS):
            try:
                os.replace(self.path, target)
                rotated = True
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if attempt < ROTATION_ATTEMPTS - 1:
                    time.sleep(ROTATION_RETRY_SLEEP_S)
        if not rotated:
            if not self._warned:
                self._warned = True
                sys.stderr.write(
                    f"loguru: {self.path.name} cannot be rotated (another process "
                    "holds it); appending past the threshold and keeping every "
                    "record until it can\n"
                )
        else:
            self._warned = False
            self._compress(target)
            self._prune()
        self._ensure_open()
        if self._next_rotation_ts is not None:
            self._next_rotation_ts = self._daily_boundary(self._rotation_spec)

    def _compress(self, target: Path) -> None:
        if self.compression != "gz":
            return
        try:
            with open(target, "rb") as src, gzip.open(f"{target}.gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            target.unlink(missing_ok=True)
        except OSError as exc:  # keep the plain rotated file; nothing is lost
            sys.stderr.write(f"loguru: could not compress {target.name}: {exc}\n")

    def _prune(self) -> None:
        if self.retention <= 0:
            return
        pattern = f"{self.path.stem}.*{self.path.suffix}*"
        rotated = sorted(
            (p for p in self.path.parent.glob(pattern) if p != self.path),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in rotated[self.retention:]:
            try:
                stale.unlink()
            except OSError:
                pass

    def write(self, message: Any) -> None:
        text = message if isinstance(message, str) else str(message)
        data = text.encode(self.encoding)
        with self._lock:
            self._ensure_open()
            if self._should_rotate(len(data)):
                self._rotate()
            self._fh.write(data)
            self._fh.flush()
            self._size += len(data)

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def stop(self) -> None:
        """Called by loguru when the sink is removed."""
        with self._lock:
            self._close()


def _stamp(record: Any) -> None:
    """Render timestamps in IST and guarantee ``extra[name]`` exists."""
    record["time"] = record["time"].astimezone(IST)
    record["extra"].setdefault(
        "name", Path(record["file"].name).stem or "unknown"
    )


logger.configure(patcher=_stamp)

_SINKS: dict[str, list[int]] = {}
_SINK_LOCK = threading.Lock()
_APP_SINK: int | None = None


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
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        sink = _RotatingFileSink(LOG_DIR / "app.log")
        sink._ensure_open()  # surface a read-only directory here, not on first log
        _APP_SINK = logger.add(
            sink,
            level=_APP_LEVEL,
            format=_FORMAT,
            enqueue=True,
        )
    except OSError as exc:
        # A read-only checkout or a container without a writable data/ must not
        # make importing any module fatal; stderr still carries every record.
        sys.stderr.write(
            f"loguru: aggregate sink unavailable ({exc}); file logging disabled\n"
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
    name: str | None = None,
    *,
    level: str | None = None,
    file: str | None = None,
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
            def only(record: Any, _slug: str = log_name) -> bool:
                return record["extra"].get("_sink") == _slug

            stderr_id: int | None = None
            if sys.stderr is not None:
                stderr_id = logger.add(
                    sys.stderr,
                    level=effective_level,
                    format=_FORMAT,
                    colorize=True,
                    filter=only,
                )
            file_id: int | None = None
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                sink = _RotatingFileSink(LOG_DIR / f"{log_name}.log")
                sink._ensure_open()
                file_id = logger.add(
                    sink,
                    level=effective_level,
                    format=_FORMAT,
                    # One enqueue thread per source file is dozens of threads in
                    # the paper trader. The per-file sinks are low-volume; write
                    # synchronously and keep the aggregate app.log asynchronous.
                    # The sink itself is locked, so synchronous writes from
                    # several threads are still safe.
                    enqueue=False,
                    filter=only,
                )
            except OSError as exc:
                # Same contract as the aggregate sink: a read-only checkout
                # degrades to stderr instead of raising on first log call.
                sys.stderr.write(
                    f"loguru: per-file sink for {log_name!r} unavailable ({exc})\n"
                )
            _SINKS[source] = [sid for sid in (stderr_id, file_id) if sid is not None]

    return logger.bind(name=label, _sink=log_name)
