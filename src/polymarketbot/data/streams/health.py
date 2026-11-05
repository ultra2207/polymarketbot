"""Health stream: periodic counters, gauges and writer statistics."""

from __future__ import annotations

import asyncio
import shutil
import time

from polymarketbot.data.context import CollectorContext
from polymarketbot.data.schema import HEALTH_METRICS

NAME = "health"


async def run(ctx: CollectorContext) -> None:
    interval = float(ctx.config.get("health_seconds", 30))
    log = ctx.log(NAME)
    ctx.register_table("health_metrics", HEALTH_METRICS)
    while not ctx.stop.is_set():
        try:
            usage = shutil.disk_usage(ctx.config["root"])
            free_gb = usage.free / 1e9
            ctx.health.gauge("host", "disk_free_gb", free_gb)
            ctx.health.gauge("host", "disk_used_pct", 100.0 * usage.used / usage.total)
            if free_gb < 15.0:
                # Stop before the tape fills the volume. The WAL is durable;
                # free space, then restart and it compacts what was written.
                log.critical(f"disk free {free_gb:.1f} GB < 15 GB; stopping collector")
                ctx.stop.set()
        except OSError as exc:
            log.warning(f"disk usage probe failed: {exc}")
        rows = ctx.health.snapshot(reset_counters=True)
        now = time.time_ns()
        for table, value in ctx.hub.stats().items():
            rows.append(
                {
                    "recv_ts_ns": now,
                    "stream": "writer",
                    "metric": table,
                    "value": value,
                    "labels_json": None,
                }
            )
        for row in rows:
            ctx.emit("health_metrics", row)
        log.debug(f"health snapshot: {len(rows)} metrics")
        try:
            await asyncio.wait_for(ctx.stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
