"""Phase 1 integrity gate for the downloaded core set.

Checks that matter before any analysis:

1. Files exist, sizes match the manifest, Parquet metadata is readable.
2. Row counts match the published totals.
3. `trades` profile: date range, contracts, price/amount sanity, nulls,
   self-trades, and whether taker-summary events leaked in.
4. `markets` profile: date range, nulls, resolution encoding.
5. Cross-indexer spot check against the independent moose-code archive.

Writes a JSON report to `data/raw/verify_report.json`.

Usage:
    uv run scripts/verify_core.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import duckdb

from polymarketbot.logs import IST

RAW = Path("data/raw")
TRADES = RAW / "sii/trades.parquet"
MARKETS = RAW / "sii/markets.parquet"
DOME = RAW / "dome/dome_polymarket_2025-10_2026-03.parquet"

MOOSE = (
    "https://huggingface.co/datasets/moose-code/polymarket-onchain-v1"
    "/resolve/main/order_filled/year=2026/month=03.parquet"
)

V1_CTF = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
V1_NEG = "0xc5d563a36ae78145c45a50134d48a1215220f80a"
V2_CTF = "0xe111180000d2663c0091e4f400237545b87b996b"
V2_NEG = "0xe2222d279d744050d28e00520010520000310f59"
EXCHANGES = (V1_CTF, V1_NEG, V2_CTF, V2_NEG)

REPORT: dict = {}


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def check_files() -> None:
    section("1. files and manifest")
    manifest_path = RAW / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    out = {}
    for path in (TRADES, MARKETS, DOME):
        name = path.relative_to(RAW).as_posix()
        exists = path.exists()
        size = path.stat().st_size if exists else None
        expected = (manifest.get(name) or {}).get("bytes")
        ok = exists and (expected is None or size == expected)
        print(f"  {'OK ' if ok else 'BAD'} {name:45} {size:>14,} bytes")
        out[name] = {"bytes": size, "manifest_bytes": expected, "ok": bool(ok)}
    REPORT["files"] = out


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 12")
    con.execute("SET memory_limit = '8GB'")
    tmp = RAW / ".." / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory = '{tmp.resolve().as_posix()}'")
    return con


def parquet_meta(con: duckdb.DuckDBPyConnection) -> None:
    section("2. parquet metadata and row counts")
    expected = {
        "sii/trades.parquet": 1_266_885_846,
        "sii/markets.parquet": 4_049_900,
        "dome/dome_polymarket_2025-10_2026-03.parquet": 339_183_655,
    }
    out = {}
    for rel, want in expected.items():
        path = (RAW / rel).as_posix()
        rows, groups = con.sql(
            f"SELECT num_rows, num_row_groups FROM parquet_file_metadata('{path}')"
        ).fetchall()[0]
        ok = rows == want
        print(f"  {'OK ' if ok else 'BAD'} {rel:45} {rows:>14,} rows  ({groups:,} groups)")
        out[rel] = {"rows": rows, "expected": want, "ok": bool(ok)}
    REPORT["row_counts"] = out


def profile_trades(con: duckdb.DuckDBPyConnection) -> None:
    section("3. trades profile (one pass over 1.27 B rows)")
    t0 = time.time()
    path = TRADES.as_posix()
    con.execute(f"CREATE OR REPLACE VIEW t AS SELECT * FROM read_parquet('{path}')")

    row = con.sql("""
        SELECT
            count(*)                                             AS n,
            min(timestamp)                                       AS first_epoch,
            max(timestamp)                                       AS last_epoch,
            sum(CASE WHEN price IS NULL OR usd_amount IS NULL THEN 1 ELSE 0 END) AS nulls,
            sum(CASE WHEN price < 0 OR price > 1 THEN 1 ELSE 0 END)              AS bad_price,
            sum(CASE WHEN usd_amount <= 0 THEN 1 ELSE 0 END)                     AS nonpos_usd,
            sum(CASE WHEN maker = taker THEN 1 ELSE 0 END)                       AS self_trades,
            sum(CASE WHEN lower(taker) IN (
                '0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e',
                '0xc5d563a36ae78145c45a50134d48a1215220f80a',
                '0xe111180000d2663c0091e4f400237545b87b996b',
                '0xe2222d279d744050d28e00520010520000310f59'
            ) THEN 1 ELSE 0 END)                                             AS summary_events
        FROM t
    """).fetchall()[0]

    n, first_epoch, last_epoch, nulls, bad_price, nonpos_usd, self_trades, summary = row
    first_ts = datetime.fromtimestamp(first_epoch, IST)
    last_ts = datetime.fromtimestamp(last_epoch, IST)
    print(f"  rows                 {n:,}")
    print(f"  first timestamp      {first_ts}")
    print(f"  last timestamp       {last_ts}")
    print(f"  null price/amount    {nulls:,}")
    print(f"  price outside [0,1]  {bad_price:,}")
    print(f"  usd_amount == 0      {nonpos_usd:,}  (zero-size fills; filter these out)")
    print(f"  maker == taker       {self_trades:,}")
    print(f"  taker-summary events {summary:,}  (should be 0)")
    REPORT["trades"] = {
        "rows": n,
        "first_ts": str(first_ts),
        "last_ts": str(last_ts),
        "nulls": nulls,
        "bad_price": bad_price,
        "nonpos_usd": nonpos_usd,
        "self_trades": self_trades,
        "summary_events": summary,
    }

    contracts = con.sql("""
        SELECT contract, count(*) AS n,
               sum(CASE WHEN lower(taker) = '0xe111180000d2663c0091e4f400237545b87b996b'
                          OR lower(taker) = '0xe2222d279d744050d28e00520010520000310f59'
                        THEN 1 ELSE 0 END) AS v2_hint
        FROM t GROUP BY 1 ORDER BY n DESC
    """).fetchall()
    print("\n  contracts:")
    REPORT["contracts"] = []
    for name, cnt, _ in contracts:
        print(f"    {name:28} {cnt:>14,}")
        REPORT["contracts"].append({"contract": name, "rows": cnt})

    print("\n  duplicate check on one week (2026-03-01 .. 2026-03-07):")
    dup = con.sql("""
        SELECT count(*) AS dup_groups, sum(c - 1) AS extra_rows
        FROM (
            SELECT transaction_hash, log_index, count(*) AS c
            FROM t
            WHERE timestamp BETWEEN epoch(timestamp '2026-03-01 00:00:00')
                                AND epoch(timestamp '2026-03-08 00:00:00')
            GROUP BY 1, 2
            HAVING count(*) > 1
        )
    """).fetchall()[0]
    print(f"    duplicate (tx,log_index) groups: {dup[0]:,}  extra rows: {dup[1] or 0:,}")
    REPORT["duplicates_week"] = {"groups": dup[0], "extra_rows": dup[1] or 0}

    print(f"\n  (profile took {time.time() - t0:.1f}s)")


def profile_markets(con: duckdb.DuckDBPyConnection) -> None:
    section("4. markets profile")
    path = MARKETS.as_posix()
    con.execute(f"CREATE OR REPLACE VIEW m AS SELECT * FROM read_parquet('{path}')")

    row = con.sql("""
        SELECT
            count(*) AS n,
            min(CAST(TRY_CAST(created_at AS TIMESTAMPTZ) AS VARCHAR)) AS first_created,
            max(CAST(TRY_CAST(created_at AS TIMESTAMPTZ) AS VARCHAR)) AS last_created,
            sum(CASE WHEN end_date IS NULL THEN 1 ELSE 0 END)      AS null_end,
            sum(CASE WHEN outcome_prices IS NULL OR outcome_prices = '' THEN 1 ELSE 0 END) AS null_outcome,
            sum(CASE WHEN volume = 0 THEN 1 ELSE 0 END)            AS zero_volume,
            sum(CASE WHEN neg_risk = 1 THEN 1 ELSE 0 END)          AS neg_risk
        FROM m
    """).fetchall()[0]

    print(f"  markets              {row[0]:,}")
    print(f"  created range        {row[1]}  ->  {row[2]}")
    print(f"  null end_date        {row[3]:,}")
    print(f"  null outcome_prices  {row[4]:,}")
    print(f"  zero volume          {row[5]:,}")
    print(f"  neg_risk             {row[6]:,}")
    REPORT["markets"] = {
        "rows": row[0],
        "first_created": str(row[1]),
        "last_created": str(row[2]),
        "null_end": row[3],
        "null_outcome": row[4],
        "zero_volume": row[5],
        "neg_risk": row[6],
    }

    print("\n  resolution encoding (top 6):")
    rows = con.sql("""
        SELECT outcome_prices, count(*) n FROM m GROUP BY 1 ORDER BY n DESC LIMIT 6
    """).fetchall()
    for val, n in rows:
        print(f"    {str(val)[:34]:36} {n:>10,}")


def cross_index(con: duckdb.DuckDBPyConnection) -> None:
    section("5. cross-indexer spot check (SII vs moose-code, 2026-03-02)")
    try:
        cols = [d[0] for d in con.sql(f"SELECT * FROM read_parquet('{MOOSE}') LIMIT 0").description]
        print(f"  moose columns: {', '.join(cols)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not read moose: {type(exc).__name__}: {exc}")
        REPORT["cross_index"] = {"error": str(exc)}
        return

    exchanges = (
        "'0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e',"
        "'0xc5d563a36ae78145c45a50134d48a1215220f80a'"
    )
    try:
        # IMPORTANT: sample the *filtered* set. `USING SAMPLE` on the base table
        # samples before WHERE, which returns nothing for a narrow date window.
        sampled = con.sql(f"""
            SELECT * FROM (
                SELECT transaction_hash FROM t
                WHERE timestamp >= epoch(timestamp '2026-03-02 00:00:00')
                  AND timestamp <  epoch(timestamp '2026-03-03 00:00:00')
                  AND lower(taker) NOT IN ({exchanges})
            ) USING SAMPLE 25 ROWS
        """).fetchall()
        hashes = [r[0] for r in sampled]
        hl = ", ".join(f"'{h}'" for h in hashes)

        sii = dict(con.sql(f"""
            SELECT transaction_hash, count(*) FROM t
            WHERE timestamp >= epoch(timestamp '2026-03-02 00:00:00')
              AND timestamp <  epoch(timestamp '2026-03-03 00:00:00')
              AND lower(taker) NOT IN ({exchanges})
              AND transaction_hash IN ({hl})
            GROUP BY 1
        """).fetchall())

        moose = dict(con.sql(f"""
            SELECT transactionHash, count(*) FROM read_parquet('{MOOSE}')
            WHERE transactionHash IN ({hl})
              AND lower(taker) NOT IN ({exchanges})
            GROUP BY 1
        """).fetchall())

        exact = sum(1 for h in hashes if sii.get(h, 0) == moose.get(h, 0))
        total_sii = sum(sii.get(h, 0) for h in hashes)
        total_moose = sum(moose.get(h, 0) for h in hashes)
        print(f"  sampled transactions: {len(hashes)}")
        print(f"  exact per-tx fill-count match: {exact}/{len(hashes)}")
        print(f"  total fills: sii={total_sii}  moose={total_moose}")
        REPORT["cross_index"] = {
            "sampled": len(hashes),
            "exact_matches": exact,
            "total_sii": total_sii,
            "total_moose": total_moose,
        }
    except Exception as exc:  # noqa: BLE001
        print(f"  ! cross-check failed: {type(exc).__name__}: {exc}")
        REPORT["cross_index"] = {"error": str(exc)}


def main() -> int:
    if not TRADES.exists():
        print(f"missing {TRADES} — run scripts/download.py first")
        return 1

    check_files()
    con = connect()
    parquet_meta(con)
    profile_trades(con)
    profile_markets(con)
    cross_index(con)

    out = RAW / "verify_report.json"
    out.write_text(json.dumps(REPORT, indent=2, default=str))
    print(f"\nreport written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
