"""Mirror the Polymarket documentation.

Two artefacts, because they serve different purposes:

* `llms-full.txt` - the entire documentation in one 1.8 MB file. Best for
  grepping and for feeding to an agent.
* `site/**.md` - the individual pages, so a specific page can be cited.

Resumable: pages already on disk are skipped. Bounded by `--max-seconds` so a
flaky network cannot turn this into an unbounded job.

Usage
    uv run scripts/download_docs.py
    uv run scripts/download_docs.py --max-seconds 300
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, "src")

import requests

from polymarketbot.net import install_doh_fallback

OUT = Path("data/raw/docs")
SITE = OUT / "site"
INDEX = "https://docs.polymarket.com/llms.txt"
FULL = "https://docs.polymarket.com/llms-full.txt"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (polymarketbot research)"
    return s


def _get(session: requests.Session, url: str, attempts: int = 5) -> requests.Response | None:
    for i in range(attempts):
        try:
            r = session.get(url, timeout=40)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (i + 1))
                continue
            return r
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (i + 1))
    return None


def local_path(url: str) -> Path:
    path = urlparse(url).path.lstrip("/")
    if path.endswith("/") or not path:
        path += "index"
    return SITE / path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-seconds", type=float, default=600.0)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    install_doh_fallback()
    OUT.mkdir(parents=True, exist_ok=True)
    session = _session()
    started = time.time()

    # 1. the single-file bundle
    full = OUT / "llms-full.txt"
    if not full.exists() or full.stat().st_size < 100_000:
        r = _get(session, FULL)
        if r is None or r.status_code != 200:
            print("could not fetch llms-full.txt", flush=True)
        else:
            full.write_text(r.text, encoding="utf-8")
            print(f"saved llms-full.txt ({len(r.content):,} bytes)", flush=True)
    else:
        print(f"llms-full.txt already present ({full.stat().st_size:,} bytes)", flush=True)

    # 2. the index and individual pages
    r = _get(session, INDEX)
    if r is None or r.status_code != 200:
        print("could not fetch llms.txt", flush=True)
        return 1
    (OUT / "llms.txt").write_text(r.text, encoding="utf-8")

    links = re.findall(r"\]\((https://docs\.polymarket\.com/[^)]+)\)", r.text)
    pages = sorted({u for u in links if u.endswith((".md", ".txt"))})
    print(f"index lists {len(pages)} documents", flush=True)

    todo = [u for u in pages if not local_path(u).exists()]
    print(f"on disk already: {len(pages) - len(todo)}, to fetch: {len(todo)}", flush=True)

    # Project from measured throughput before committing (AGENTS.md rule).
    if todo:
        t0 = time.time()
        probe = todo[:3]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(lambda u: _get(_session(), u), probe))
        per_page = (time.time() - t0) / max(1, len(probe))
        print(f"projection: ~{per_page:.2f}s/page -> "
              f"~{len(todo) * per_page / 60:.1f} min for the rest", flush=True)

    manifest: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_get, _session(), u): u for u in todo}
        for fut in as_completed(futures):
            url = futures[fut]
            if time.time() - started > args.max_seconds:
                for f in futures:
                    f.cancel()
                print("  wall-clock budget reached; stopping", flush=True)
                break
            resp = fut.result()
            dest = local_path(url)
            if resp is None or resp.status_code != 200:
                manifest.append({"url": url, "status": getattr(resp, "status_code", None)})
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(resp.text, encoding="utf-8")
            manifest.append({"url": url, "path": str(dest), "bytes": len(resp.content),
                             "status": 200})
            done += 1
            if done % 20 == 0:
                print(f"  {done}/{len(todo)} pages ({time.time() - started:.0f}s)", flush=True)

    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    files = list(SITE.rglob("*")) if SITE.exists() else []
    total = sum(f.stat().st_size for f in files if f.is_file())
    print(f"\nmirrored {done} new pages; site/ holds {sum(1 for f in files if f.is_file())} files "
          f"({total/1e6:.1f} MB); full bundle {full.stat().st_size/1e6:.2f} MB"
          if full.exists() else f"\nmirrored {done} new pages", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
