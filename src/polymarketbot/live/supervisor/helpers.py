"""Thread bridges shared by the supervision phases."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from polymarketbot.logs import get_logger

log = get_logger("real_money_trader")


async def _thread_optional(label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run blocking work in a thread; return None (logged) on failure."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:
        log.warning(f"{label} failed: {exc}")
        return None


async def _close_engine_quietly(engine: Any) -> None:
    """Close the engine off the event loop, never masking the caller's abort reason."""
    try:
        await asyncio.to_thread(engine.close)
    except Exception as exc:
        log.error(f"Engine close failed: {exc}")
