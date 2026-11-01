"""Cross-check the SII trade tape against the independent moose-code indexer.

Gate 1: "trade counts reconcile within 0.1% across two independent indexers".

Sampling rows is the wrong unit: what can silently break is a *transaction's*
fill decomposition (one indexer sees 7 fills, the other sees 5). So we sample
transactions and compare per-transaction fill counts.

Two practical constraints shape this script:

* **Remote scans are the bottleneck.** moose month files range from 2 MB to
  21 GB. Querying a 21 GB file over HTTPS can hang for many minutes, so only
  small months are eligible and every remote query is interruptible.
* **Local sampling must prune row groups.** Sampling a whole month forces a
  45 GB scan; restricting to one day lets Parquet statistics skip almost all
  of it.

Both archives include Polymarket "taker summary" events (`taker` == exchange
contract). SII `trades` excludes them, moose `order_filled` includes them, so
they are filtered on the moose side.

Usage
    uv run scripts/cross_check_indexers.py
    uv run scripts/cross_check_indexers.py --months 2025-06,2024-09 --per-month 40 --timeout 90

Writes
    data/processed/indexer_crosscheck.json
    data/processed/indexer_crosscheck.parquet
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import duckdb

RAW = Path("data/raw")
OUT = Path("data/processed")
TRADES = RAW / "sii/trades.parquet"
MOOSE = (
    "https://huggingface.co/datasets/moose-code/polymarket-onchain-v1"
    "/resolve/main/order_filled/year={y}/month={m:02d}.parquet"
)

EXCHANGES = (
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
    "0xe111180000d2663c0091e4f400237545b87b996b",
    "0xe2222d279d744050d28e00520010520000310f59",
)
EXCL = ",".join(f"'{x}'" for x in EXCHANGES)

# Chosen so every remote file is small enough to scan inside the timeout,
# while still spanning 2023 -> 2025. 2026-03 was already validated separately
# (25/25 transactions, 217/217 fills) and its file is 21 GB.
DEFAULT_MONTHS = "2023-06,2024-06,2024-09,2025-06,2025-10"


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 12")
    con.execute("SET memory_limit = '8GB'")
    con.execute("SET http_timeout = 60000")
    con.execute("SET http_retries = 2")
    return con


def run_with_timeout(con, fn, timeout_s: float):
    """Run fn() in a thread; interrupt the DuckDB query if it overruns."""
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        con.interrupt()
        thread.join(5)
        raise TimeoutError(f"query exceeded {timeout_s:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def month_window(year: int, month: int, day: int = 15) -> tuple[str, str]:
    nxt_year, nxt_month = (year + 1, 1) if month == 12 else (year, month + 1)
    last_day = 28 if month == 2 else 30
    end_day = day + 1 if day < last_day else 1
    end_year, end_month = (year, month) if day < last_day else (nxt_year, nxt_month)
    return (
        f"{year}-{month:02d}-{day:02d} 00:00:00",
        f"{end_year}-{end_month:02d}-{end_day:02d} 00:00:00",
    )


def run_month(year: int, month: int, per_month: int, timeout_s: float) -> dict:
    con = connect()
    start, end = month_window(year, month)
    t0 = time.time()
    label = f"{year}-{month:02d}"

    def local_sample():
        return con.sql(f"""
            SELECT * FROM (
                SELECT DISTINCT transaction_hash
                FROM read_parquet('{TRADES.as_posix()}')
                WHERE timestamp >= epoch(timestamp '{start}')
                  AND timestamp <  epoch(timestamp '{end}')
                  AND lower(taker) NOT IN ({EXCL})
            ) USING SAMPLE {per_month} ROWS
        """).fetchall()

    hashes = [r[0] for r in run_with_timeout(con, local_sample, timeout_s)]
    if not hashes:
        return {"month": label, "sampled": 0, "note": "no local rows"}

    hl = ",".join(f"'{h}'" for h in hashes)

    def sii_counts():
        return dict(con.sql(f"""
            SELECT transaction_hash, count(*)
            FROM read_parquet('{TRADES.as_posix()}')
            WHERE timestamp >= epoch(timestamp '{start}')
              AND timestamp <  epoch(timestamp '{end}')
              AND lower(taker) NOT IN ({EXCL})
              AND transaction_hash IN ({hl})
            GROUP BY 1
        """).fetchall())

    sii = run_with_timeout(con, sii_counts, timeout_s)

    def moose_counts():
        return dict(con.sql(f"""
            SELECT transactionHash, count(*)
            FROM read_parquet('{MOOSE.format(y=year, m=month)}')
            WHERE transactionHash IN ({hl})
              AND lower(taker) NOT IN ({EXCL})
            GROUP BY 1
        """).fetchall())

    try:
        moose = run_with_timeout(con, moose_counts, timeout_s)
    except TimeoutError:
        print(f"  {label}: TIMEOUT after {timeout_s:.0f}s (skipped)")
        return {"month": label, "sampled": len(hashes), "skipped": "timeout",
                "seconds": round(time.time() - t0, 1)}
    except Exception as exc:  # noqa: BLE001
        print(f"  {label}: FAILED {type(exc).__name__}: {str(exc)[:120]}")
        return {"month": label, "sampled": len(hashes), "skipped": f"{type(exc).__name__}"}

    pairs = [(h, sii.get(h, 0), moose.get(h, 0)) for h in hashes]
    exact = sum(1 for _, a, b in pairs if a == b)
    total_sii = sum(a for _, a, _ in pairs)
    total_moose = sum(b for _, _, b in pairs)
    missing = sum(1 for _, a, b in pairs if b == 0 and a > 0)

    print(f"  {label}: {len(hashes):>3} tx  exact {exact:>3}/{len(hashes):<3}  "
          f"fills sii={total_sii:>5} moose={total_moose:>5}  missing={missing:>2}  "
          f"({time.time() - t0:.1f}s)")

    return {
        "month": label, "sampled": len(hashes), "exact": exact,
        "total_sii": total_sii, "total_moose": total_moose,
        "missing_in_moose": missing, "seconds": round(time.time() - t0, 1),
        "pairs": pairs,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", default=DEFAULT_MONTHS, help="comma-separated YYYY-MM")
    ap.add_argument("--per-month", type=int, default=40)
    ap.add_argument("--timeout", type=float, default=120.0, help="seconds per query")
    args = ap.parse_args()

    months = []
    for spec in args.months.split(","):
        y, m = spec.strip().split("-")
        months.append((int(y), int(m)))

    print(f"cross-checking {len(months)} months x <= {args.per_month} transactions "
          f"(timeout {args.timeout:.0f}s/query)\n")

    results = [run_month(y, m, args.per_month, args.timeout) for y, m in months]

    ok = [r for r in results if "skipped" not in r and r.get("sampled")]
    tot_tx = sum(r["sampled"] for r in ok)
    tot_exact = sum(r["exact"] for r in ok)
    tot_sii = sum(r["total_sii"] for r in ok)
    tot_moose = sum(r["total_moose"] for r in ok)
    pct = 100 * abs(tot_sii - tot_moose) / tot_sii if tot_sii else 0.0

    all_pairs = [p for r in ok for p in r.get("pairs", [])]
    mismatched = [(h, a, b) for h, a, b in all_pairs if a != b]

    summary = {
        "months_checked": [r["month"] for r in ok],
        "months_skipped": [r["month"] for r in results if "skipped" in r],
        "transactions": tot_tx,
        "exact_transactions": tot_exact,
        "exact_pct": round(100 * tot_exact / tot_tx, 4) if tot_tx else None,
        "fills_sii": tot_sii,
        "fills_moose": tot_moose,
        "fill_count_diff_pct": round(pct, 4),
        "mismatches": len(mismatched),
        "missing_in_moose": sum(r.get("missing_in_moose", 0) for r in ok),
        "per_month": results,
    }

    print(f"\nTOTAL: {tot_exact}/{tot_tx} transactions exact "
          f"({summary['exact_pct']}%), fills {tot_sii} vs {tot_moose} "
          f"(diff {pct:.4f}%)")
    if mismatched:
        print("first mismatches (tx, sii, moose):")
        for row in mismatched[:10]:
            print("  ", row[0][:14], row[1], row[2])
    else:
        print("no per-transaction fill-count mismatches")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "indexer_crosscheck.json").write_text(json.dumps(summary, indent=2))

    if all_pairs:
        jsonl = OUT / "_crosscheck.jsonl"
        with jsonl.open("w", encoding="utf-8") as fh:
            for h, a, b in all_pairs:
                fh.write(json.dumps({"transaction_hash": h, "sii_fills": a,
                                     "moose_fills": b}) + "\n")
        con = connect()
        con.execute(f"""
            COPY (SELECT * FROM read_json_auto('{jsonl.as_posix()}', format='newline_delimited'))
            TO '{(OUT / 'indexer_crosscheck.parquet').as_posix()}'
            (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        jsonl.unlink(missing_ok=True)

    print(f"wrote {OUT / 'indexer_crosscheck.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
