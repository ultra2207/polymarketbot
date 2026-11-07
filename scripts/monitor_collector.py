#!/usr/bin/env python3
"""Collector storage and rate-limit monitor.

Checks every 5 minutes:
- disk free and projected CLOB tape rate (last hour of Parquet parts)
- writer queue depth, spills, compaction errors
- CLOB disconnects, Polygon unbackfilled blocks, Kalshi failures

Writes a status line to data/logs/monitor_collector.log and appends any
WARN/CRIT to data/logs/collector_alerts.log.
"""
from __future__ import annotations

import glob
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from polymarketbot.db import connect as db_connect
from polymarketbot.logs import IST, get_logger

log = get_logger("monitor_collector")
ALERT = Path("data/logs/collector_alerts.log")


def count_since(path: str, patterns: list[str], minutes: int = 60) -> int:
    if not os.path.exists(path):
        return 0
    cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(minutes=minutes)
    n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                dt = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if dt < cutoff:
                continue
            if any(p in line for p in patterns):
                n += 1
    return n


def writer_health() -> dict[str, float]:
    g = "data/tape/health_metrics/**/*.parquet"
    cutoff = int((time.time() - 600) * 1e9)
    try:
        con = db_connect(threads=2, memory_limit="1GB")
        try:
            rows = con.sql(
                f"SELECT metric, max(value) FROM read_parquet('{g}') "
                f"WHERE recv_ts_ns >= {cutoff} "
                "AND metric IN ('queue_depth','spills','compaction_errors') GROUP BY 1"
            ).fetchall()
            return {r[0]: float(r[1]) for r in rows}
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - monitor must not die
        log.warning(f"health query failed: {exc}")
        return {}


def clob_rate_gb_day() -> float:
    cutoff = time.time() - 3600
    files = glob.glob("data/tape/clob_events/**/*.parquet", recursive=True)
    b = sum(os.path.getsize(f) for f in files if os.path.getmtime(f) >= cutoff)
    return b / 1e9 * 24


def main() -> None:
    log.info(
        "monitor started; WARN rate>15GB/d disk<60GB queue>400k poly_nb>0 "
        "kalshi>3 clob_disc>3; CRIT rate>20GB/d disk<30GB queue>480k "
        "spills/compaction poly_nb>5 kalshi>10 clob_disc>10"
    )
    while True:
        free = shutil.disk_usage("C:/").free / 1e9
        rate = clob_rate_gb_day()
        h = writer_health()
        q = h.get("queue_depth", 0.0)
        spills = h.get("spills", 0.0)
        comp = h.get("compaction_errors", 0.0)
        clob_disc = count_since("data/logs/data.clob.log", ["disconnected"])
        poly_nb = count_since("data/logs/data.polygon.log", ["not backfilled"])
        kalshi = count_since("data/logs/data.kalshi.log", ["poll failed", "429"])

        alerts: list[str] = []
        if free < 30:
            alerts.append(f"CRIT disk free {free:.1f} GB < 30")
        elif free < 60:
            alerts.append(f"WARN disk free {free:.1f} GB < 60")
        if rate > 20:
            alerts.append(f"CRIT CLOB rate {rate:.1f} GB/day > 20")
        elif rate > 15:
            alerts.append(f"WARN CLOB rate {rate:.1f} GB/day > 15")
        if spills > 0:
            alerts.append(f"CRIT writer spills {spills:.0f}")
        if comp > 0:
            alerts.append(f"CRIT compaction errors {comp:.0f}")
        if q > 480000:
            alerts.append(f"CRIT writer queue {q:.0f}")
        elif q > 400000:
            alerts.append(f"WARN writer queue {q:.0f}")
        if poly_nb > 5:
            alerts.append(f"CRIT Polygon not-backfilled {poly_nb} in 1h")
        elif poly_nb > 0:
            alerts.append(f"WARN Polygon not-backfilled {poly_nb} in 1h")
        if clob_disc > 10:
            alerts.append(f"CRIT CLOB disconnects {clob_disc} in 1h")
        elif clob_disc > 3:
            alerts.append(f"WARN CLOB disconnects {clob_disc} in 1h")
        if kalshi > 10:
            alerts.append(f"CRIT Kalshi failures {kalshi} in 1h")
        elif kalshi > 3:
            alerts.append(f"WARN Kalshi failures {kalshi} in 1h")

        status = (
            f"STATUS disk={free:.1f}GB rate={rate:.1f}GB/day queue={q:.0f} "
            f"spills={spills:.0f} comp_err={comp:.0f} clob_disc={clob_disc} "
            f"poly_nb={poly_nb} kalshi_fail={kalshi}"
        )
        if alerts:
            msg = "; ".join(alerts)
            log.warning(f"{status} -> {msg}")
            with open(ALERT, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST {msg}\n")
        else:
            log.info(f"{status} -> OK")
        time.sleep(300)


if __name__ == "__main__":
    main()
