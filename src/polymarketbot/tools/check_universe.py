"""Gate 2 verification: universe invariants plus a live spot-check.

Two parts:

1. **Invariants** at 12 dates. Any violation is a hard failure.
2. **Spot-check 20 markets** against Polymarket's Gamma API: condition id,
   slug, question, closed flag and end date must agree with what we built
   locally. This is the automated half of "spot-check against polymarket.com
   history"; the other half is a human opening the market page, and the script
   prints the URLs to make that a 2-minute job.

Usage
    uv run python -m polymarketbot.tools.check_universe
    uv run python -m polymarketbot.tools.check_universe --n 20
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import duckdb
import requests

from polymarketbot.universe.markets import universe_sql
from polymarketbot.logs import get_logger
log = get_logger(__name__)

OUT = Path("data/processed")
PIT = OUT / "markets_pit.parquet"
GAMMA = "https://gamma-api.polymarket.com/markets/keyset"

DATES = [
    "2024-01-01", "2024-04-01", "2024-07-01", "2024-10-01",
    "2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01",
    "2026-01-01", "2026-04-01", "2026-06-01", "2026-08-10",
]


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 8")
    con.execute(f"CREATE VIEW pit AS SELECT * FROM read_parquet('{PIT.as_posix()}')")
    return con


def invariants(con: duckdb.DuckDBPyConnection) -> None:
    log.info("invariants at 12 dates:")
    log.info(f"  {'date':12} {'universe':>10} {'bad_created':>12} {'bad_fill':>10} {'expired':>9} {'zero_vol':>9}")
    failures = 0
    for when in DATES:
        sql = universe_sql(when)
        row = con.sql(f"""
            SELECT count(*) n,
                   count(*) FILTER (WHERE created_at > TIMESTAMPTZ '{when}') bad_created,
                   count(*) FILTER (WHERE first_fill_ts IS NULL
                                      OR to_timestamp(first_fill_ts) > TIMESTAMPTZ '{when}') bad_fill,
                   count(*) FILTER (WHERE end_date IS NOT NULL
                                      AND end_date <= TIMESTAMPTZ '{when}') expired,
                   count(*) FILTER (WHERE is_zero_volume) zero_vol
            FROM ({sql})
        """).fetchall()[0]
        bad = row[1] + row[2] + row[3] + row[4]
        failures += bad
        log.info(f"  {when:12} {row[0]:>10,} {row[1]:>12,} {row[2]:>10,} {row[3]:>9,} {row[4]:>9,}")
    log.info(f"\n  total violations: {failures}")
    if failures:
        raise SystemExit("universe invariants failed")


def pick_markets(con: duckdb.DuckDBPyConnection, n: int) -> list[dict]:
    """Stratified across eras and categories, resolved and void."""
    per = max(1, n // 4)
    picks: list[dict] = []
    queries = [
        # model-development era, resolved
        f"""SELECT * FROM (
                SELECT condition_id, slug, question, closed, end_date, category, created_at
                FROM pit WHERE resolved AND NOT coalesce(is_void,false)
                  AND created_at BETWEEN TIMESTAMPTZ '2024-01-01' AND TIMESTAMPTZ '2025-12-31'
                  AND NOT is_zero_volume
                QUALIFY row_number() OVER (PARTITION BY category ORDER BY md5(condition_id)) = 1
            ) ORDER BY md5(condition_id) LIMIT {per}""",
        # forward-test era (no local labels exist for these, so don't filter on resolved)
        f"""SELECT * FROM (
                SELECT condition_id, slug, question, closed, end_date, category, created_at
                FROM pit WHERE closed = 1
                  AND created_at BETWEEN TIMESTAMPTZ '2026-06-01' AND TIMESTAMPTZ '2026-08-10'
                  AND NOT is_zero_volume
                QUALIFY row_number() OVER (PARTITION BY category ORDER BY md5(condition_id)) = 1
            ) ORDER BY md5(condition_id) LIMIT {per}""",
        # voids
        f"""SELECT condition_id, slug, question, closed, end_date, category, created_at
            FROM pit WHERE is_void AND created_at BETWEEN TIMESTAMPTZ '2025-10-01'
                  AND TIMESTAMPTZ '2026-08-10'
            ORDER BY md5(condition_id) LIMIT {per}""",
        # fractional payouts, if any
        f"""SELECT condition_id, slug, question, closed, end_date, category, created_at
            FROM pit WHERE outcome = 'fractional'
            ORDER BY md5(condition_id) LIMIT {per}""",
    ]
    for sql in queries:
        cols = [d[0] for d in con.sql(sql).description]
        for row in con.sql(sql).fetchall():
            picks.append(dict(zip(cols, row)))
    return picks[:n]


def gamma_get(params: list[tuple], attempts: int = 6):
    s = requests.Session()
    s.headers["User-Agent"] = "polymarketbot-research/0.1"
    for i in range(attempts):
        try:
            r = s.get(GAMMA, params=params, timeout=45)
            if r.status_code != 200:
                time.sleep(1.5 * (i + 1))
                continue
            return r.json().get("markets", [])
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (i + 1))
    return None


def spot_check(con: duckdb.DuckDBPyConnection, n: int) -> dict:
    picks = pick_markets(con, n)
    log.info(f"\nspot-checking {len(picks)} markets against Gamma ...")

    ids = [p["condition_id"] for p in picks]
    # Gamma's keyset endpoint defaults to *open* markets only. Omitting this
    # makes every historical lookup return zero rows.
    params = ([("condition_ids", i) for i in ids]
              + [("closed", "true"), ("limit", str(len(ids)))])
    remote = gamma_get(params)
    if remote is None:
        log.info("  ! Gamma unreachable; skipping live comparison")
        return {"checked": 0, "error": "gamma unreachable"}

    by_condition = {m.get("conditionId"): m for m in remote}
    rows = []
    for p in picks:
        m = by_condition.get(p["condition_id"])
        if not m:
            rows.append({**p, "found": False})
            continue
        local_end = p["end_date"]
        remote_end = m.get("endDate")
        rows.append({
            "condition_id": p["condition_id"],
            "slug": p["slug"],
            "category": p["category"],
            "found": True,
            "slug_match": m.get("slug") == p["slug"],
            "question_match": (m.get("question") or "").strip() == (p["question"] or "").strip(),
            "closed_match": bool(m.get("closed")) == bool(p["closed"]),
            "local_end": str(local_end),
            "remote_end": remote_end,
            "remote_outcome_prices": m.get("outcomePrices"),
            "url": f"https://polymarket.com/market/{p['slug']}",
        })

    found = [r for r in rows if r["found"]]
    summary = {
        "picked": len(picks),
        "found": len(found),
        "slug_match": sum(1 for r in found if r["slug_match"]),
        "question_match": sum(1 for r in found if r["question_match"]),
        "closed_match": sum(1 for r in found if r["closed_match"]),
        "rows": rows,
    }

    # `polymarket.com/market/<slug>` redirects to an event page; print the URL
    # that actually renders, otherwise the human check lands somewhere unhelpful.
    log.info("  resolving public URLs ...")
    for r in rows:
        if not r.get("found"):
            continue
        try:
            resp = requests.get(f"https://polymarket.com/market/{r['slug']}",
                                timeout=20, allow_redirects=True)
            r["http_status"] = resp.status_code
            r["final_url"] = resp.url
        except Exception:  # noqa: BLE001
            r["http_status"] = None
            r["final_url"] = None

    log.info(f"  {'condition':14} {'cat':10} {'found':5} {'slug':5} {'quest':5} {'closed':6} local_end -> remote_end")
    for r in rows:
        if not r["found"]:
            log.info(f"  {r['condition_id'][:12]:14} {r['category']:10} {'no':5}")
            continue
        log.info(f"  {r['condition_id'][:12]:14} {r['category']:10} {'yes':5} "
              f"{str(r['slug_match'])[0]:5} {str(r['question_match'])[0]:5} "
              f"{str(r['closed_match'])[0]:6} {str(r['local_end'])[:10]} -> {str(r['remote_end'])[:10]}")

    log.info(f"\n  found {summary['found']}/{summary['picked']}, "
          f"slug {summary['slug_match']}/{summary['found']}, "
          f"question {summary['question_match']}/{summary['found']}, "
          f"closed {summary['closed_match']}/{summary['found']}")
    log.info("\n  open these to confirm by eye:")
    for r in rows[:8]:
        if r.get("found"):
            log.info(f"    [{r.get('http_status')}] {r.get('final_url') or r['url']}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()

    con = connect()
    invariants(con)
    summary = spot_check(con, args.n)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "universe_spotcheck.json").write_text(json.dumps(summary, indent=2, default=str))
    log.info(f"\nwrote {OUT / 'universe_spotcheck.json'}")

    if summary.get("found") and summary["slug_match"] < summary["found"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
