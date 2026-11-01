"""Logging that survives losing a terminal.

Why this exists
---------------
A long job that prints to stdout loses everything the moment its output is
piped through `tail`, killed at a timeout, or run detached. Rerunning to
recover an answer costs minutes to hours and teaches nothing new.

Every script logs to `data/logs/<name>.log` as well as stderr, so the record of
what a killed run actually did is always on disk and readable afterwards.

Usage
    from polymarketbot.logs import get_logger
    log = get_logger("screen_calibration")
    log.info("quarter {} done in {:.1f}s", era, secs)
"""

from __future__ import annotations

import sys
from pathlib import Path

from typing import Any

from loguru import logger

LOG_DIR = Path("data/logs")
_FORMAT = ("{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {extra[name]} | "
           "{message} | {elapsed}")


def get_logger(name: str, *, level: str = "INFO") -> Any:
    """A logger bound to one script name, writing to stderr and to disk.

    Each call replaces the sinks so a script gets exactly one stderr and one
    file sink, at the level it asked for.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{name}.log"

    logger.remove()
    logger.add(sys.stderr, level=level, format=_FORMAT, colorize=True)
    # rotation by size keeps the newest evidence; retention keeps history
    logger.add(path, level=level, format=_FORMAT, rotation="10 MB",
               retention=5, encoding="utf-8", enqueue=True)
    return logger.bind(name=name)
