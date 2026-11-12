"""Bounded background refits driven by the supervision clock.

The live supervisor must not stop quoting while a fitter reads the tape.  A
block is therefore a separate ``python -m polymarketbot.tools.refit`` process:
it is bounded by ``--max-partitions`` and by each fitter's timeout, it resumes
from ``data/derived/refit_state.json`` if a previous block was killed, and it
publishes artifacts with ``os.replace`` so the engine never reads a half-written
file.

One block runs at supervisor startup and then every ``--refit-every`` seconds
(default 3600).  Only one block may be in flight; a due block is skipped while
the previous one is still running.  ``--refit-every 0`` disables the cadence.
The loop reloads changed artifacts at its next tick boundary, never inside a
tick.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from polymarketbot.logs import get_logger

if TYPE_CHECKING:  # the run record only appears in annotations
    from polymarketbot.live.supervisor.context import RunContext

log = get_logger("real_money_trader")

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _block_command(ctx: RunContext) -> list[str]:
    return [
        sys.executable,
        "-m",
        "polymarketbot.tools.refit",
        "--max-partitions",
        str(max(1, int(ctx.args.refit_max_partitions))),
        "--partition-timeout",
        f"{float(ctx.args.refit_partition_timeout):.1f}",
    ]


async def _run_block(ctx: RunContext) -> int:
    command = _block_command(ctx)
    env = os.environ.copy()
    source = str(_REPO_ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source if not existing else source + os.pathsep + existing
    log.info("[REFIT] block start: " + " ".join(command))
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    ctx.refit_process = proc
    try:
        output, _ = await proc.communicate()
        returncode = int(proc.returncode or 0)
    except asyncio.CancelledError:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            proc.terminate()
        with suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        if proc.returncode is None:
            proc.kill()
            with suppress(Exception):
                await proc.wait()
        raise
    finally:
        ctx.refit_process = None
    text = (output or b"").decode("utf-8", "replace").strip()
    if text:
        log.info(f"[REFIT] block output: {text[-2000:]}")
    if returncode == 0:
        log.info("[REFIT] block finished; changed artifacts reload at the next tick")
    else:
        ctx.refit_failures += 1
        log.warning(
            f"[REFIT] block exited {returncode} "
            f"(failure {ctx.refit_failures}); the previous artifacts stay live"
        )
    return returncode


def _launch(ctx: RunContext, now: float) -> None:
    ctx.last_refit_at = now
    ctx.refit_task = asyncio.create_task(_run_block(ctx))


def start(ctx: RunContext) -> None:
    """Launch the startup block (the first supervision tick reloads its output)."""
    if float(getattr(ctx.args, "refit_every", 0.0) or 0.0) <= 0.0:
        return
    if ctx.refit_task is None:
        _launch(ctx, time.time())


async def supervise(ctx: RunContext, now: float) -> None:
    """Collect a finished block and start the next one when it is due."""
    every = float(getattr(ctx.args, "refit_every", 0.0) or 0.0)
    if every <= 0.0:
        return
    task = ctx.refit_task
    if task is not None:
        if not task.done():
            return
        ctx.refit_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            ctx.refit_failures += 1
            log.warning(f"[REFIT] block raised: {error}")
    if now - ctx.last_refit_at >= every:
        _launch(ctx, now)


async def shutdown(ctx: RunContext) -> None:
    """Cancel an in-flight block and wait for its process to be reaped.

    The supervisor loop calls this from its ``finally`` while it may itself be
    handling a cancellation.  ``asyncio.CancelledError`` is a ``BaseException``,
    so it must be swallowed explicitly: a refit cancellation is an expected
    shutdown event, never an error that escapes into the runner.
    """
    task = ctx.refit_task
    ctx.refit_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001 - shutdown must finish
        log.warning(f"[REFIT] block shutdown raised: {exc}")
