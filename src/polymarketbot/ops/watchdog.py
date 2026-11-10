#!/usr/bin/env python3
"""Restart the data collector if it is not running.

The collector is a long-lived process. The monitor (``polymarketbot.ops.monitor``)
alerts on disk, queue and disconnect anomalies, but it does not restart a dead
collector, and nothing currently starts the collector after a reboot. This
watchdog is the missing supervisor:

* every run (Task Scheduler, 2 h) it looks for a live
  ``python -m polymarketbot.data.collector`` process;
* if none is found it starts one detached and logs the decision;
* if one is found but the health heartbeat is older than ``--max-age`` it logs
  CRIT and leaves the process alone (killing a live but slow collector risks
  losing WAL data; the monitor already flags the cause).

Install once, as the logged-in user (no admin):

    schtasks /create /tn PolymarketCollector /tr "\"C:\\path\\to\\.venv\\Scripts\\python.exe\" -m polymarketbot.ops.watchdog --once" /sc minute /mo 120 /f

Then ``schtasks /query /tn PolymarketCollector`` proves it registered. The
collector itself runs detached, so it survives the watchdog's exit. Disable
Windows sleep for the two-week window (``powercfg /change standby-timeout-ac 0``);
a sleeping host stops every stream regardless of the watchdog.

Usage:
    .venv/Scripts/python.exe -m polymarketbot.ops.watchdog --once --dry-run
    .venv/Scripts/python.exe -m polymarketbot.ops.watchdog --once
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import time
from pathlib import Path

from polymarketbot.logs import get_logger

_logger = get_logger("watchdog_collector")

REPO_ROOT = Path(__file__).resolve().parents[3]
PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
COLLECTOR_MODULE = "polymarketbot.data.collector"
HEALTH_GLOB = str(REPO_ROOT / "data" / "tape" / "health_metrics" / "**" / "*.parquet")
LOG = REPO_ROOT / "data" / "logs" / "watchdog_collector.log"
LOCK = REPO_ROOT / "data" / "tape" / ".watchdog.pid"
DEFAULT_MAX_AGE_S = 600.0


def log(message: str) -> None:
    line = f"{datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')} {message}"
    _logger.info(line)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def collector_pids() -> list[int]:
    """PIDs whose command line runs the collector, via PowerShell CIM."""
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*"
        f"{COLLECTOR_MODULE}*' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - a broken probe must not start a duplicate
        log(f"process probe failed: {exc}")
        return []
    return sorted({int(match) for match in re.findall(r"\d+", result.stdout)})


def health_age_s() -> float | None:
    """Age of the newest health row; None when there is no health table."""
    try:
        from polymarketbot.db import connect as db_connect

        con = db_connect(threads=2, memory_limit="1GB")
        try:
            value = con.sql(
                f"SELECT max(recv_ts_ns) FROM read_parquet('{HEALTH_GLOB}')"
            ).fetchall()[0][0]
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - no table yet is not an error
        return None
    if not value:
        return None
    return (time.time() * 1e9 - int(value)) / 1e9


def _lock_held() -> bool:
    """True when another watchdog run is still alive."""
    try:
        pid = int(LOCK.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return str(pid) in result.stdout


def start_collector() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stdout = (REPO_ROOT / "data" / "logs" / "watchdog_collector.out").open("a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        [str(PYTHON), "-m", COLLECTOR_MODULE, "--duration", "0", "--allow-long"],
        cwd=str(REPO_ROOT),
        stdout=stdout,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    try:
        LOCK.write_text(str(process.pid), encoding="utf-8")
    except OSError:
        pass
    return process.pid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="one check, then exit (default)")
    parser.add_argument("--dry-run", action="store_true", help="report, never start")
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_S)
    args = parser.parse_args(argv)

    if _lock_held():
        log("another watchdog run is alive; nothing to do")
        return 0
    pids = collector_pids()
    age = health_age_s()
    age_text = "none" if age is None else f"{age:.0f}s"
    if pids:
        if age is not None and age > args.max_age:
            log(
                f"CRIT collector alive pid={pids} but health age {age_text} "
                f"> {args.max_age:.0f}s; not killing it"
            )
        else:
            log(f"collector alive pid={pids}; health age {age_text}; ok")
        return 0
    log(f"collector not running; health age {age_text}")
    if args.dry_run:
        log("dry run: would start the collector")
        return 0
    pid = start_collector()
    log(f"started collector pid={pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
