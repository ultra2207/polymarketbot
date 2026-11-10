#!/usr/bin/env python3
"""Resumable downloader for the Polymarket research stack.

Design goals, in order:

1. Never re-download bytes we already have (``.part`` files + HTTP Range).
2. Never silently mix dataset versions (ETag pinning in the manifest).
3. Never blow the disk (a size check runs before anything starts).
4. Be safe to kill and restart at any time.

Usage
-----
    uv run python -m polymarketbot.data.builders.download --dry-run           # plan + sizes, no bytes
    uv run python -m polymarketbot.data.builders.download                     # core: 56.5 GB
    uv run python -m polymarketbot.data.builders.download --pmxt --pmxt-start 2026-06-01 --pmxt-end 2026-06-07
    uv run python -m polymarketbot.data.builders.download --all --yes         # core + PMXT (large!)
    uv run python -m polymarketbot.data.builders.download --verify            # check existing files vs manifest

Core (default) is `sii/markets.parquet`, `sii/trades.parquet`, and the Dome
snapshot archive. PMXT is opt-in because the full 2026-04-13 -> 2026-08-10
window is roughly 1.07 TB.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from tqdm import tqdm
from polymarketbot.logs import get_logger
log = get_logger(__name__)

SII = "https://huggingface.co/datasets/SII-WANGZJ/Polymarket_data/resolve/main"
DOME = (
    "https://huggingface.co/datasets/vpcapitano/polymarket-genesis-liquidity"
    "/resolve/main/data"
)
PMXT = "https://r2v2.pmxt.dev/polymarket_orderbook_{stamp}.parquet"

UA = {"User-Agent": "polymarketbot-research/0.1"}
CHUNK = 1 << 20
RETRIES = 6
DISK_HEADROOM_GB = 5.0
MANIFEST_NAME = "manifest.json"

REPO_ROOT = Path(__file__).resolve().parents[4]


@dataclass
class Item:
    name: str
    url: str
    dest: Path
    size: int | None = None
    etag: str | None = None
    status: str = "pending"  # pending | done | skipped | missing | failed

    @property
    def part(self) -> Path:
        return self.dest.with_name(self.dest.name + ".part")


# --------------------------------------------------------------------------- #
# probing
# --------------------------------------------------------------------------- #


def head(session: requests.Session, url: str) -> tuple[int | None, str | None] | None:
    """Return (size, etag). None means 404 (file does not exist)."""
    for attempt in range(RETRIES):
        try:
            r = session.head(url, allow_redirects=True, timeout=(30, 60))
            if r.status_code == 404:
                return None
            r.raise_for_status()
            size = r.headers.get("Content-Length") or r.headers.get("x-linked-size")
            etag = r.headers.get("ETag") or r.headers.get("x-linked-etag")
            return (int(size) if size and size.isdigit() else None, etag)
        except Exception:  # noqa: BLE001 - transient network
            time.sleep(min(2**attempt, 30))
    return None


def hourly_stamps(start: datetime, end: datetime) -> list[str]:
    stamps: list[str] = []
    cur = start.replace(minute=0, second=0, microsecond=0)
    while cur <= end:
        stamps.append(cur.strftime("%Y-%m-%dT%H"))
        cur += timedelta(hours=1)
    return stamps


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #


def plan_core(dest: Path, session: requests.Session) -> list[Item]:
    specs = [
        ("sii/markets.parquet", f"{SII}/markets.parquet"),
        ("sii/trades.parquet", f"{SII}/trades.parquet"),
        ("dome/dome_polymarket_2025-10_2026-03.parquet",
         f"{DOME}/dome_polymarket_2025-10_2026-03.parquet"),
    ]
    items: list[Item] = []
    for name, url in specs:
        probe = head(session, url)
        if probe is None:
            log.info(f"  ! could not probe {name} (network or missing)")
            continue
        size, etag = probe
        items.append(Item(name=name, url=url, dest=dest / name, size=size, etag=etag))
    return items


def plan_pmxt(
    dest: Path,
    session: requests.Session,
    start: datetime,
    end: datetime,
    workers: int,
) -> list[Item]:
    stamps = hourly_stamps(start, end)
    log.info(f"  probing {len(stamps):,} PMXT hourly files ...")
    items: list[Item] = []

    def probe(stamp: str) -> Item | None:
        url = PMXT.format(stamp=stamp)
        result = head(session, url)
        if result is None:
            return None
        size, etag = result
        return Item(
            name=f"pmxt/polymarket_orderbook_{stamp}.parquet",
            url=url,
            dest=dest / "pmxt" / f"polymarket_orderbook_{stamp}.parquet",
            size=size,
            etag=etag,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe, s): s for s in stamps}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="probing", unit="file"):
            item = fut.result()
            if item is not None:
                items.append(item)

    items.sort(key=lambda i: i.name)
    missing = len(stamps) - len(items)
    if missing:
        log.info(f"  ! {missing:,} hours returned 404 (recorded as absent)")
    return items


# --------------------------------------------------------------------------- #
# downloading
# --------------------------------------------------------------------------- #


def already_complete(item: Item) -> bool:
    return (
        item.dest.exists()
        and item.size is not None
        and item.dest.stat().st_size == item.size
    )


def download(
    item: Item,
    session: requests.Session,
    bar: tqdm,
    manifest: dict,
    lock: threading.Lock,
) -> str:
    item.dest.parent.mkdir(parents=True, exist_ok=True)

    previous = manifest.get(item.name, {})
    if already_complete(item):
        if previous.get("etag") and item.etag and previous["etag"] != item.etag:
            log.info(
                f"\n  ~ {item.name}: upstream ETag changed since last download "
                f"({previous['etag']} -> {item.etag}). Keeping local copy; use --force to refresh."
            )
        item.status = "skipped"
    else:
        resume = item.part.stat().st_size if item.part.exists() else 0
        if previous.get("etag") and item.etag and previous["etag"] != item.etag:
            # upstream changed under a partial file: never stitch two versions together
            log.info(f"\n  ~ {item.name}: ETag changed, discarding partial download")
            item.part.unlink(missing_ok=True)
            resume = 0
        if item.size is not None and resume > item.size:
            item.part.unlink()
            resume = 0

        for attempt in range(RETRIES):
            try:
                headers = dict(UA)
                if resume:
                    headers["Range"] = f"bytes={resume}-"
                with session.get(item.url, headers=headers, stream=True, timeout=(30, 120)) as r:
                    if r.status_code == 416:  # already have everything
                        break
                    if resume and r.status_code != 206:
                        # server ignored Range; start over
                        resume = 0
                    r.raise_for_status()

                    mode = "ab" if resume else "wb"
                    with open(item.part, mode) as fh:
                        for chunk in r.iter_content(CHUNK):
                            if not chunk:
                                continue
                            fh.write(chunk)
                            bar.update(len(chunk))

                if item.size is not None and item.part.stat().st_size < item.size:
                    raise IOError(
                        f"short read: {item.part.stat().st_size:,} / {item.size:,}"
                    )
                os.replace(item.part, item.dest)
                item.status = "done"
                break
            except Exception as exc:  # noqa: BLE001
                resume = item.part.stat().st_size if item.part.exists() else 0
                if attempt == RETRIES - 1:
                    item.status = "failed"
                    log.info(f"\n  ! {item.name}: {type(exc).__name__}: {exc}")
                else:
                    time.sleep(min(2**attempt, 60))

    with lock:
        manifest[item.name] = {
            "url": item.url,
            "bytes": item.size,
            "etag": item.etag,
            "status": item.status,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    return item.status


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 * CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


def human(n: float | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


def print_plan(items: list[Item], dest: Path) -> tuple[int, int]:
    log.info(f"\n{len(items)} files -> {dest}\n")
    total = 0
    present = 0
    for item in items:
        total += item.size or 0
        if already_complete(item):
            present += item.size or 0
            flag = "have"
        elif item.part.exists():
            flag = "part"
        else:
            flag = "new "
        log.info(f"  [{flag}] {human(item.size):>10}  {item.name}")
    log.info(f"\n  total {human(total)}   present {human(present)}   to fetch {human(total - present)}")
    free = shutil.disk_usage(dest).free
    log.info(f"  free on destination: {human(free)}")
    return total, present


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Resumable downloader for the Polymarket research stack.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--dest", type=Path, default=REPO_ROOT / "data" / "raw")
    ap.add_argument("--dry-run", action="store_true", help="plan and size up only")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--force", action="store_true", help="re-download even if complete")
    ap.add_argument("--hash", action="store_true", help="compute sha256 after download")
    ap.add_argument("--verify", action="store_true", help="check existing files vs manifest")
    ap.add_argument("--all", action="store_true", help="core + PMXT")
    ap.add_argument("--pmxt", action="store_true", help="include PMXT (large)")
    ap.add_argument(
        "--only",
        choices=["core", "pmxt"],
        help="download one group only (implied by --pmxt when combined)",
    )
    ap.add_argument("--pmxt-start", default="2026-04-13T19")
    ap.add_argument("--pmxt-end", default="2026-08-10T23")
    ap.add_argument("--parallel", type=int, default=4, help="parallel downloads (PMXT)")
    args = ap.parse_args()

    dest: Path = args.dest
    dest.mkdir(parents=True, exist_ok=True)
    manifest_path = dest / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    if args.verify:
        bad = 0
        for name, meta in manifest.items():
            path = dest / name
            expected = meta.get("bytes")
            if not path.exists():
                log.info(f"  missing  {name}")
                bad += 1
            elif expected is not None and path.stat().st_size != expected:
                log.info(f"  sizemis  {name}: {path.stat().st_size:,} != {expected:,}")
                bad += 1
        log.info(f"\n{'OK' if bad == 0 else f'{bad} problems'}")
        return 0 if bad == 0 else 1

    session = requests.Session()
    want_pmxt = args.pmxt or args.all or args.only == "pmxt"
    want_core = not args.only or args.only == "core"

    log.info("planning ...")
    items: list[Item] = []
    if want_core:
        items += plan_core(dest, session)
    if want_pmxt:
        start = datetime.fromisoformat(args.pmxt_start).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(args.pmxt_end).replace(tzinfo=timezone.utc)
        if end < start:
            log.info("  ! --pmxt-end is before --pmxt-start")
            return 2
        items += plan_pmxt(dest, session, start, end, min(args.parallel * 4, 16))

    if not items:
        log.info("nothing to do")
        return 1

    total, present = print_plan(items, dest)
    free = shutil.disk_usage(dest).free
    needed = total - present
    if free < needed + DISK_HEADROOM_GB * 1024**3:
        log.info(
            f"\n  ! not enough disk: need {human(needed + DISK_HEADROOM_GB * 1024**3)}, "
            f"have {human(free)}. Aborting."
        )
        return 2

    if args.dry_run:
        log.info("\n  dry run - nothing downloaded")
        return 0

    if not args.yes:
        log.info(f"\n  about to fetch {human(needed)}. Ctrl-C to abort.")
        try:
            input("  press Enter to continue ... ")
        except (EOFError, KeyboardInterrupt):
            log.info("\n  aborted")
            return 130

    lock = threading.Lock()
    completed_bytes = sum(i.size or 0 for i in items if already_complete(i))
    completed_bytes += sum(
        i.part.stat().st_size for i in items if not already_complete(i) and i.part.exists()
    )

    bar = tqdm(
        total=total,
        initial=completed_bytes,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc="downloading",
        smoothing=0.05,
    )

    started = time.time()
    try:
        if args.parallel > 1 and len(items) > 1:
            with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                futures = [
                    pool.submit(download, item, session, bar, manifest, lock)
                    for item in items
                ]
                for fut in as_completed(futures):
                    fut.result()
        else:
            for item in items:
                download(item, session, bar, manifest, lock)
    except KeyboardInterrupt:
        log.info("\n\n  interrupted - partial files kept as .part, re-run to resume")
    finally:
        bar.close()
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

    elapsed = time.time() - started
    done = sum(1 for i in items if i.status in ("done", "skipped"))
    failed = [i for i in items if i.status == "failed"]
    log.info(f"\n  {done}/{len(items)} files complete in {elapsed / 60:.1f} min")
    if failed:
        log.info(f"  {len(failed)} failed - re-run the same command to resume:")
        for item in failed[:10]:
            log.info(f"    {item.name}")

    if args.hash:
        log.info("\n  hashing ...")
        for item in items:
            if item.status in ("done", "skipped"):
                digest = sha256_of(item.dest)
                with lock:
                    manifest[item.name]["sha256"] = digest
                log.info(f"    {digest[:16]}...  {item.name}")
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
