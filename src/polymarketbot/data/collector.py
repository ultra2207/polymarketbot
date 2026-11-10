#!/usr/bin/env python3
"""Unified data collector.

Runs every enabled stream in one asyncio process. Each stream owns its own
reconnecting socket or polling loop and writes through the shared WriterHub
(JSONL WAL -> hourly Parquet). The universe is refreshed from Gamma on a
schedule; the CLOB stream applies the add/remove diff to its live sockets.

Usage
-----
    .venv/Scripts/python.exe -m polymarketbot.data.collector --duration 300
    .venv/Scripts/python.exe -m polymarketbot.data.collector --allow-long --duration 0

``--duration 0`` means run until interrupted. The first run is capped at 300 s
unless ``--allow-long`` is passed, per AGENTS.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import signal
import time
from datetime import datetime
from pathlib import Path

from polymarketbot.data.config import load_config
from polymarketbot.data.context import CollectorContext, HealthRegistry
from polymarketbot.data.streams import discover
from polymarketbot.data.universe import UniverseState
from polymarketbot.data.writer import WriterHub
from polymarketbot.logs import IST, get_logger
from polymarketbot.net import DEFAULT_HOSTS, install_doh_fallback, warm

log = get_logger("data.collector", file="data.collector")

FIRST_RUN_CAP_SECONDS = 300.0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/data_collection.yaml")
    ap.add_argument("--duration", type=float, default=300.0, help="seconds; 0 = until interrupted")
    ap.add_argument("--allow-long", action="store_true", help="bypass the 300 s first-run cap")
    ap.add_argument("--streams", default="", help="comma-separated override of streams.enabled")
    ap.add_argument("--root", default="", help="override the tape root (smoke runs use a separate dir)")
    return ap.parse_args(argv)


def _write_run_meta(root: Path, run_id: str, payload: dict) -> Path:
    path = root / "runs" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return path


async def _run(args: argparse.Namespace) -> int:
    install_doh_fallback()
    resolved = warm(DEFAULT_HOSTS)
    log.info(f"resolved Polymarket hosts: {resolved}")

    config = load_config(args.config)
    if args.root:
        config["root"] = args.root
    root = Path(config["root"])
    root.mkdir(parents=True, exist_ok=True)

    hub = WriterHub(
        root,
        flush_seconds=float(config["flush_seconds"]),
        queue_max_rows=int(config["queue_max_rows"]),
        compaction_workers=int(config["compaction_workers"]),
        part_seconds=float(config.get("part_seconds", 600)),
    )
    health = HealthRegistry()
    clob_config = config["streams"]["clob"]
    universe = UniverseState(
        clob_config["manifest_path"],
        cutoff=float(clob_config["cutoff_daily_reward"]),
        refresh_seconds=float(clob_config["universe_refresh_seconds"]),
        max_pages=int(clob_config.get("universe_max_pages", 800)),
        poll_seconds=float(clob_config.get("universe_poll_seconds", 60.0)),
        incremental=bool(clob_config.get("universe_incremental", True)),
        mode=str(clob_config.get("universe_mode", "rolling")),
        locked_manifest=str(
            clob_config.get("locked_manifest", "data/tape/manifest_locked_1028.json")
        ),
        sentinel_enabled=bool(clob_config.get("sentinel_enabled", True)),
        sentinel_question_patterns=clob_config.get("sentinel_question_patterns"),
        sentinel_max_markets=int(clob_config.get("sentinel_max_markets", 200)),
    )
    ctx = CollectorContext(config=config, hub=hub, health=health, stop=asyncio.Event(), universe=universe)

    try:
        await asyncio.wait_for(universe.start(), timeout=540.0)
    except asyncio.TimeoutError:
        log.error("universe refresh did not finish within 540s; aborting")
        hub.close()
        return 1
    if not universe.assets():
        log.error("universe is empty after refresh; refusing to start")
        hub.close()
        return 1

    modules = discover()
    requested = {s.strip() for s in args.streams.split(",") if s.strip()} or ctx.enabled()
    selected = [m for name, m in modules.items() if name in requested]
    missing = requested - set(modules)
    if missing:
        log.warning(f"requested streams not found: {sorted(missing)}")
    if not selected:
        log.error(f"no streams selected (available: {sorted(modules)})")
        hub.close()
        return 1

    duration = float(args.duration)
    if duration <= 0:
        duration = 0.0
    if not args.allow_long and (duration == 0.0 or duration > FIRST_RUN_CAP_SECONDS):
        log.warning(
            f"first run capped at {FIRST_RUN_CAP_SECONDS:.0f}s "
            f"(requested {'forever' if duration == 0 else f'{duration:.0f}s'}); pass --allow-long"
        )
        duration = FIRST_RUN_CAP_SECONDS

    meta = {
        "run_id": hub.run_id,
        "started_at": datetime.now(IST).isoformat(),
        "duration_s": duration,
        "streams": [m.name for m in selected],
        "universe_markets": len(universe.markets),
        "universe_sentinel_markets": universe.sentinel_count,
        "universe_assets": len(universe.assets()),
        "config": config,
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    meta_path = _write_run_meta(root, hub.run_id, meta)
    log.info(
        f"run {hub.run_id}: {len(selected)} streams, {len(universe.markets)} markets, "
        f"{len(universe.assets())} assets, duration={duration or 'until stop'}, meta={meta_path}"
    )

    loop = asyncio.get_running_loop()

    def _stop() -> None:
        log.warning("stop requested")
        loop.call_soon_threadsafe(ctx.stop.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: _stop())
        except (ValueError, OSError):
            pass

    async def _supervise(name: str, run) -> None:
        """A crashed stream restarts; only ctx.stop ends the run."""
        backoff = 1.0
        while not ctx.stop.is_set():
            try:
                await run(ctx)
                if ctx.stop.is_set():
                    return
                log.warning(f"stream {name} exited; restarting in {backoff:.0f}s")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error(f"stream {name} crashed: {exc}; restarting in {backoff:.0f}s")
            try:
                await asyncio.wait_for(ctx.stop.wait(), timeout=backoff)
                return
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2.0, 60.0)

    tasks = [
        asyncio.create_task(_supervise(m.name, m.run), name=m.name) for m in selected
    ]
    stop_task = asyncio.create_task(ctx.stop.wait(), name="stop-wait")
    try:
        if duration:
            done, _ = await asyncio.wait(
                {stop_task, *tasks}, timeout=duration, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                log.info(f"duration {duration:.0f}s elapsed")
        else:
            await asyncio.wait({stop_task, *tasks}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        ctx.stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        await universe.stop()
        hub.close()

    meta["universe_final_markets"] = len(universe.markets)
    meta["universe_final_sentinel_markets"] = universe.sentinel_count
    meta["universe_final_assets"] = len(universe.assets())
    meta["ended_at"] = datetime.now(IST).isoformat()
    meta["elapsed_s"] = round(time.time() - datetime.fromisoformat(meta["started_at"]).timestamp(), 1)
    meta["writer"] = hub.stats()
    meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    log.info(f"run {hub.run_id} finished: {meta['writer']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
