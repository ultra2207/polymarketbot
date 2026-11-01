"""Take a dated snapshot of Gamma market metadata.

Phase 2 requires snapshotting metadata daily from now on: SII's
`markets.parquet` is a *latest-state* dump, so everything that changes after
creation (end dates, tags, resolution status, fees) is unrecoverable unless it
is recorded going forward.

Each run writes one immutable file, `data/raw/gamma/snapshot_YYYY-MM-DD.parquet`.
Re-running on the same day overwrites that day's file, which is intended.

Notes
-----
* Gamma keyset pagination caps at 100 rows/page, so this is intentionally
  bounded by `--max-pages`. An open-market snapshot is small; a full closed
  history crawl is not and is out of scope here.
* `category` is null for most markets; `tags` carry the signal, so
  `include_tag=true` is set.

Usage
    uv run scripts/snapshot_gamma.py
    uv run scripts/snapshot_gamma.py --max-pages 50 --closed false
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

import duckdb
import requests

from polymarketbot.net import DEFAULT_HOSTS, install_doh_fallback, warm

GAMMA = "https://gamma-api.polymarket.com/markets/keyset"
OUT = Path("data/raw/gamma")

KEEP = [
    "id", "conditionId", "slug", "question", "category", "closed", "active",
    "archived", "endDate", "startDate", "negRisk", "clobTokenIds",
    "outcomePrices", "volume", "liquidity", "feesEnabled", "feeType",
    "umaResolutionStatuses", "tags",
    # Liquidity-reward eligibility. Without these the D1/E5 questions cannot be
    # answered for any day that was not captured, and Gamma overwrites them.
    "rewardsMinSize", "rewardsMaxSpread", "competitive",
]


def fetch_page(session: requests.Session, params: dict, attempts: int = 6):
    for i in range(attempts):
        try:
            r = session.get(GAMMA, params=params, timeout=45)
            if r.status_code != 200:
                time.sleep(1.5 * (i + 1))
                continue
            return r.json()
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (i + 1))
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=400)
    ap.add_argument("--closed", default="false", help="false | true | omit for both")
    args = ap.parse_args()

    install_doh_fallback()
    print("resolved:", warm(DEFAULT_HOSTS))

    session = requests.Session()
    session.headers["User-Agent"] = "polymarketbot-research/0.1"

    rows: list[dict] = []
    cursor = None
    for page in range(args.max_pages):
        params = {"limit": 100, "include_tag": "true"}
        if args.closed != "omit":
            params["closed"] = args.closed
        if cursor:
            params["after_cursor"] = cursor
        payload = fetch_page(session, params)
        if payload is None:
            print(f"  page {page}: failed, stopping")
            break
        markets = payload.get("markets") or []
        if not markets:
            break
        for m in markets:
            rec = {k: m.get(k) for k in KEEP}
            tags = m.get("tags") or []
            rec["tag_labels"] = [t.get("label") for t in tags]
            rec["event_ids"] = [e.get("id") for e in (m.get("events") or [])]

            # `clobRewards` is a list of reward campaigns, each with its own daily
            # rate and validity window. Flatten the money fields so a daily snapshot
            # is queryable without unnesting JSON.
            rewards = m.get("clobRewards") or []
            rec["reward_campaigns"] = len(rewards)
            rec["rewards_daily_rate"] = sum(
                float(r.get("rewardsDailyRate") or 0) for r in rewards)
            rec["rewards_amount"] = sum(
                float(r.get("rewardsAmount") or 0) for r in rewards)
            rec["rewards_start"] = min(
                (r.get("startDate") for r in rewards if r.get("startDate")),
                default=None)
            rec["rewards_end"] = max(
                (r.get("endDate") for r in rewards if r.get("endDate")),
                default=None)
            rows.append(rec)
        cursor = payload.get("next_cursor")
        if page % 10 == 0:
            print(f"  page {page}: {len(rows):,} markets", flush=True)
        if not cursor:
            break

    if not rows:
        print("no rows fetched")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = dt.date.today().isoformat()
    dest = OUT / f"snapshot_{stamp}.parquet"
    jsonl = OUT / f"_snapshot_{stamp}.jsonl"
    with jsonl.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")

    con = duckdb.connect()
    con.execute(f"""
        COPY (SELECT * FROM read_json_auto('{jsonl.as_posix()}', format='newline_delimited'))
        TO '{dest.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    jsonl.unlink(missing_ok=True)

    n = con.sql(f"SELECT count(*) FROM read_parquet('{dest.as_posix()}')").fetchall()[0][0]
    print(f"\nwrote {dest}  ({n:,} markets, closed={args.closed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
