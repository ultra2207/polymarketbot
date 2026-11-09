"""Reconcile market labels against the live Polymarket Data API.

Gate 1 requires: "payouts match /v2/tokens for >= 99% of 400 sampled markets
including void (0.5/0.5) cases".

We check three things on each market:

1. `chain`   - on-chain payoutNumerators (pre-May-2026 universe) vs the API's
               `final_price`. This is the genuinely independent check.
2. `sii`     - SII `outcome_prices` vs the API's `final_price`. Validates the
               archive against the live service.
3. `api`     - /v2/resolutions `payouts` vs /v2/tokens `final_price`
               (post-May universe, where no on-chain label exists locally).

Sampling is stratified: resolved and void-ish markets are both represented,
because a label pipeline that gets winners right and voids wrong is useless.

Usage
    uv run scripts/reconcile_resolutions.py [--n 100]

Writes
    data/processed/resolution_reconciliation.parquet
    data/processed/resolution_reconciliation.json
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import duckdb
import requests
from polymarketbot.logs import get_logger
log = get_logger(__name__)

OUT = Path("data/processed")
LABELS = OUT / "market_labels.parquet"
API = "https://data-api.polymarket.com/v2"

OP_PARSE = """list_transform(
    str_split(replace(replace(replace(replace(outcome_prices,'[',''),']',''),'''',''),' ',''), ','),
    lambda x: TRY_CAST(x AS DOUBLE))"""


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 8")
    return con


def sample(con: duckdb.DuckDBPyConnection, n: int) -> list[dict]:
    """Stratified sample: resolved + void-ish, before and after the moose cutoff."""
    groups = {
        "pre_may_resolved": f"""
            SELECT condition_id, 'pre_may_resolved' AS grp, payout_0 AS chain_p0,
                   {OP_PARSE}[1] AS sii_p0
            FROM read_parquet('{LABELS.as_posix()}')
            WHERE resolved AND NOT is_void AND closed = 1
              AND created_at >= TIMESTAMPTZ '2025-10-01'
              AND created_at <  TIMESTAMPTZ '2026-04-01'
            ORDER BY md5(condition_id) LIMIT {n}
        """,
        "pre_may_void": f"""
            SELECT condition_id, 'pre_may_void' AS grp, payout_0 AS chain_p0,
                   {OP_PARSE}[1] AS sii_p0
            FROM read_parquet('{LABELS.as_posix()}')
            WHERE is_void AND closed = 1
              AND created_at >= TIMESTAMPTZ '2025-10-01'
              AND created_at <  TIMESTAMPTZ '2026-04-01'
            ORDER BY md5(condition_id) LIMIT {max(1, n // 2)}
        """,
        "post_may_resolved": f"""
            SELECT condition_id, 'post_may_resolved' AS grp, NULL::DOUBLE AS chain_p0,
                   {OP_PARSE}[1] AS sii_p0
            FROM read_parquet('{LABELS.as_posix()}')
            WHERE label_source = 'missing' AND closed = 1 AND volume > 0
              AND created_at >= TIMESTAMPTZ '2026-06-01'
              AND created_at <  TIMESTAMPTZ '2026-08-11'
              AND outcome_prices NOT LIKE '%0.5%'
            ORDER BY md5(condition_id) LIMIT {n}
        """,
        "post_may_voidish": f"""
            SELECT condition_id, 'post_may_voidish' AS grp, NULL::DOUBLE AS chain_p0,
                   {OP_PARSE}[1] AS sii_p0
            FROM read_parquet('{LABELS.as_posix()}')
            WHERE label_source = 'missing' AND closed = 1
              AND created_at >= TIMESTAMPTZ '2026-06-01'
              AND created_at <  TIMESTAMPTZ '2026-08-11'
              AND outcome_prices LIKE '%0.5%'
            ORDER BY md5(condition_id) LIMIT {max(1, n // 2)}
        """,
    }
    rows: list[dict] = []
    for name, sql in groups.items():
        got = con.sql(sql).fetchall()
        log.info(f"  sampled {len(got):>3}  {name}")
        for cid, grp, chain_p0, sii_p0 in got:
            rows.append({"condition_id": cid, "group": grp,
                         "chain_p0": chain_p0, "sii_p0": sii_p0})
    return rows


SESSION_LOCAL = None


def _session() -> requests.Session:
    global SESSION_LOCAL
    if SESSION_LOCAL is None:
        s = requests.Session()
        s.headers["User-Agent"] = "polymarketbot-research/0.1"
        SESSION_LOCAL = s
    return SESSION_LOCAL


def get(path: str, params: dict, attempts: int = 6) -> dict | None:
    """GET with retries. DNS for data-api is intermittently flaky on this host."""
    last = None
    for i in range(attempts):
        try:
            r = _session().get(f"{API}/{path}", params=params, timeout=25)
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            if r.status_code >= 500:
                time.sleep(1 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.7 * (i + 1))
    log.info(f"    ! {path} {params} failed: {type(last).__name__}: {last}")
    return None


def fetch_one(row: dict) -> dict:
    cid = row["condition_id"]
    out = dict(row)

    tok = get("tokens", {"condition": cid})
    api_p0 = api_resolved = None
    if tok:
        for t in tok.get("data", []):
            if t.get("outcome_index") == 0:
                api_p0 = t.get("final_price")
                api_resolved = t.get("resolved")
                break
    out["api_final_p0"] = api_p0
    out["api_resolved"] = api_resolved

    # /v2/resolutions is only needed where no on-chain label exists.
    res_p0 = None
    if row["group"].startswith("post_may"):
        res = get("resolutions", {"condition": cid})
        if res and res.get("data"):
            r0 = res["data"][0]
            payouts = r0.get("payouts")
            if payouts:
                total = sum(payouts)
                if total:
                    res_p0 = payouts[0] / total
            out["resolution_status"] = r0.get("status")
    out["api_res_p0"] = res_p0

    return out


def close(a, b, tol=0.01) -> bool | None:
    if a is None or b is None:
        return None
    return abs(float(a) - float(b)) <= tol


def summarise(rows: list[dict]) -> dict:
    summary: dict = {}
    for grp in sorted({r["group"] for r in rows}):
        sub = [r for r in rows if r["group"] == grp]
        got = [r for r in sub if r["api_final_p0"] is not None]
        s = {
            "sampled": len(sub),
            "api_answered": len(got),
            "chain_vs_api_match": sum(1 for r in got if close(r["chain_p0"], r["api_final_p0"]) is True),
            "chain_vs_api_checked": sum(1 for r in got if close(r["chain_p0"], r["api_final_p0"]) is not None),
            "sii_vs_api_match": sum(1 for r in got if close(r["sii_p0"], r["api_final_p0"]) is True),
            "sii_vs_api_checked": sum(1 for r in got if close(r["sii_p0"], r["api_final_p0"]) is not None),
            "res_vs_api_match": sum(1 for r in got if close(r["api_res_p0"], r["api_final_p0"]) is True),
            "res_vs_api_checked": sum(1 for r in got if close(r["api_res_p0"], r["api_final_p0"]) is not None),
        }
        summary[grp] = s
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="per non-void stratum")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    con = connect()
    log.info("sampling:")
    rows = sample(con, args.n)

    log.info(f"\nfetching {len(rows)} markets from {API} ...")
    done = 0
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_one, r): r for r in rows}
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if done % 25 == 0 or done == len(rows):
                log.info(f"  {done}/{len(rows)}")

    summary = summarise(results)

    log.info("\nreconciliation:")
    log.info(f"  {'group':22} {'sampled':>8} {'answered':>9} {'chain':>12} {'sii':>12} {'resolutions':>14}")
    for grp, s in summary.items():
        def fmt(m, c):
            return f"{m}/{c}" if c else "-"
        log.info(f"  {grp:22} {s['sampled']:>8} {s['api_answered']:>9} "
              f"{fmt(s['chain_vs_api_match'], s['chain_vs_api_checked']):>12} "
              f"{fmt(s['sii_vs_api_match'], s['sii_vs_api_checked']):>12} "
              f"{fmt(s['res_vs_api_match'], s['res_vs_api_checked']):>14}")

    log.info("\nworst disagreements:")
    bad = [r for r in results
           if close(r["chain_p0"], r["api_final_p0"]) is False
           or close(r["sii_p0"], r["api_final_p0"]) is False
           or close(r["api_res_p0"], r["api_final_p0"]) is False]
    for r in bad[:12]:
        log.info(f"  {r['condition_id'][:12]} {r['group']:18} "
              f"chain={r['chain_p0']} sii={r['sii_p0']} res={r['api_res_p0']} api={r['api_final_p0']}")
    if not bad:
        log.info("  none")

    OUT.mkdir(parents=True, exist_ok=True)
    jsonl = OUT / "resolution_reconciliation.jsonl"
    with jsonl.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, default=str) + "\n")
    con.execute(f"""
        COPY (SELECT * FROM read_json_auto('{jsonl.as_posix()}', format='newline_delimited'))
        TO '{(OUT / "resolution_reconciliation.parquet").as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    jsonl.unlink(missing_ok=True)
    (OUT / "resolution_reconciliation.json").write_text(json.dumps(summary, indent=2))
    log.info(f"\nwrote {OUT / 'resolution_reconciliation.parquet'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
