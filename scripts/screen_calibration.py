"""Screen idea #1 (longshot / favourite bias) on the development window.

The question
------------
If you buy a token at price `p` and hold to resolution you receive `y` in
{0, 1, 0.5}. Across many markets, is `E[y | p]` equal to `p`? Betting markets
have a long-standing tendency to overprice longshots and underprice favourites.
If that holds here and exceeds fees, it is an edge that needs no information
advantage — only capacity.

What is measured
----------------
* Entry price = volume-weighted average of the first `--horizon-hours` of a
  market's life. A price you could plausibly have paid, unlike the last trade
  before resolution.
* Label = on-chain payout. `outcome_prices` is a final traded price, not a
  payout, and is not used.
* Fees via `schedule_at`, so a 2025 fill is priced at 2025 rates rather than
  the 2026 schedule.

Why it runs quarter by quarter
------------------------------
One query over the whole window reads too many row groups to finish inside a
300-second first run, and a job that cannot be observed cannot be trusted. Each
quarter is a separate bounded scan, written to a journal, so the run is
resumable and prints progress per quarter. It also produces the era split for
free, which is exactly what the fee question needs.

This is a **gross-effect screen**. It can delete the idea. It cannot graduate
it: there is no exit modelling and no fill modelling, so the answer is "the
effect exists / does not", not "this is tradable".

Usage
    uv run scripts/screen_calibration.py --since 2024-01-01 --until 2026-03-31
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "src")

import duckdb

from polymarketbot.fees import schedule_at
from polymarketbot.logs import get_logger

log = get_logger("screen_calibration")

OUT = Path("data/processed")
TRADES = Path("data/raw/sii/trades.parquet")
PIT = OUT / "markets_pit.parquet"
RESULT = OUT / "screen_calibration.parquet"
ROWS = OUT / "screen_calibration_rows.parquet"
# One file per quarter, written as soon as that quarter is scored. A run killed
# at a timeout therefore keeps its work, and the next run skips it.
PARTS = OUT / "screen_calibration_parts"

BUCKETS = [
    (0.00, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35),
    (0.35, 0.50), (0.50, 0.65), (0.65, 0.80), (0.80, 0.90), (0.90, 0.95),
    (0.95, 0.98), (0.98, 1.0001),
]
SCHEMA = """(
    era VARCHAR, condition_id VARCHAR, category VARCHAR, token_id VARCHAR,
    slot VARCHAR, vwap DOUBLE, usd_traded DOUBLE, fills BIGINT, payout DOUBLE,
    fee_frac DOUBLE, net_edge DOUBLE, is_void BOOLEAN
)"""


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 8")
    con.execute("SET memory_limit = '8GB'")
    con.execute("SET TimeZone = 'UTC'")
    return con


def quarters(since: datetime, until: datetime) -> list[tuple[datetime, datetime]]:
    out = []
    y, q = since.year, (since.month - 1) // 3
    while True:
        start = datetime(y, q * 3 + 1, 1, tzinfo=timezone.utc)
        end = datetime(y + (q * 3 + 3) // 12, (q * 3 + 3) % 12 + 1, 1,
                       tzinfo=timezone.utc)
        if start >= until:
            break
        out.append((max(start, since), min(end, until)))
        q += 1
        if q == 4:
            q, y = 0, y + 1
    return out


def build_entries(con: duckdb.DuckDBPyConnection, since: datetime, until: datetime,
                  from_end_h: int, to_end_h: int, min_fills: int,
                  min_usd: float) -> None:
    """VWAP of each token between `from_end_h` and `to_end_h` before resolution.

    The entry must be a price *before* the outcome is known. An earlier version
    averaged the first 48 h of the market's life, which for the median 24-hour
    market is the whole life: averaging a price path that terminates at 0 or 1
    pulls the average toward 0.5, so the realised outcome is mechanically more
    extreme than the measured entry. That produced an apparent 20 pp
    favourite-longshot bias that was pure artefact — the "gap" was exactly
    antisymmetric around 0.5 and cancelled to ~0 in the aggregate.
    """
    lo, hi = int(since.timestamp()), int(until.timestamp())
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE entries AS
        WITH m AS (
            SELECT condition_id, token1, token2, created_at, end_date, category,
                   payout_0, is_void, fee_type, fee_rate, fee_exponent, fee_rebate
            FROM read_parquet('{PIT.as_posix()}')
            WHERE NOT is_zero_volume AND resolved
              AND created_at >= to_timestamp({lo})
              AND created_at <  to_timestamp({hi})
              AND created_at <= end_date
              -- the entry window must sit inside the market's life
              AND epoch(end_date) - epoch(created_at) >= {from_end_h} * 3600
        )
        SELECT
            m.condition_id, m.category, m.created_at, m.is_void,
            m.fee_type, m.fee_rate, m.fee_exponent, m.fee_rebate,
            m.payout_0,
            t.asset_id AS token_id,
            CASE WHEN t.asset_id = m.token1 THEN 'token1' ELSE 'token2' END AS slot,
            sum(t.price * t.token_amount) / sum(t.token_amount) AS vwap,
            sum(t.usd_amount) AS usd_traded,
            count(*)          AS fills
        FROM m
        JOIN read_parquet('{TRADES.as_posix()}') t
          ON t.condition_id = m.condition_id
        WHERE t.usd_amount > 0 AND t.token_amount > 0
          -- numeric bounds keep Parquet row-group pruning; a function call on
          -- the column would read the whole 1.27 B-row file per quarter.
          AND t.timestamp >= epoch(m.end_date) - {from_end_h} * 3600
          AND t.timestamp <  epoch(m.end_date) - {to_end_h} * 3600
          AND (t.asset_id = m.token1 OR t.asset_id = m.token2)
        GROUP BY ALL
        HAVING count(*) >= {min_fills} AND sum(t.usd_amount) >= {min_usd}
    """)


def accumulate(con: duckdb.DuckDBPyConnection) -> int:
    rows = con.sql("""
        SELECT condition_id, category, created_at, is_void, token_id, slot,
               vwap, usd_traded, fills, fee_type, fee_rate, fee_exponent,
               fee_rebate, payout_0
        FROM entries
    """).fetchall()

    batch: list[tuple] = []
    for (cid, cat, created, is_void, token, slot, vwap, usd, fills,
         fee_type, fee_rate, fee_exp, fee_reb, p0) in rows:
        if vwap is None or not (0.0 < vwap < 1.0) or p0 is None:
            continue
        payout = p0 if slot == "token1" else 1.0 - p0
        sched = schedule_at({"category": cat, "fee_type": fee_type,
                             "fee_rate": fee_rate, "fee_exponent": fee_exp,
                             "fee_rebate": fee_reb}, created)
        curve = vwap * (1.0 - vwap)
        fee_per_share = sched.rate * (curve ** sched.exponent) if sched.rate > 0 else 0.0
        fee_frac = fee_per_share / vwap
        net_edge = (payout - vwap) - fee_per_share
        era = f"{created.year}-Q{(created.month - 1) // 3 + 1}"
        batch.append((era, cid, cat, token, slot, vwap, usd, fills, payout,
                      fee_frac, net_edge, bool(is_void)))

    if batch:
        con.executemany(f"INSERT INTO scored VALUES ({','.join(['?'] * 12)})", batch)
    return len(batch)


def persist_part(con: duckdb.DuckDBPyConnection, label: str) -> None:
    """Write this quarter's rows to disk immediately, then clear memory."""
    PARTS.mkdir(parents=True, exist_ok=True)
    dest = PARTS / f"quarter={label}.parquet"
    con.execute(f"""
        COPY (SELECT * FROM scored) TO '{dest.as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    con.execute("DELETE FROM scored")


def aggregate(con: duckdb.DuckDBPyConnection) -> None:
    # Persist the raw scored rows once: the aggregate can then be re-cut without
    # rescanning the tape.
    con.execute(f"""
        COPY (SELECT * FROM scored) TO '{ROWS.as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    bounds = ", ".join(f"({lo}, {hi})" for lo, hi in BUCKETS)
    con.execute(f"""
        COPY (
            WITH b AS (SELECT * FROM (VALUES {bounds}) AS t(lo, hi))
            SELECT b.lo, b.hi, s.era, s.category,
                   count(*)                                AS n,
                   avg(s.vwap)                             AS mean_price,
                   avg(s.payout)                           AS mean_payout,
                   avg(s.payout) - avg(s.vwap)             AS calibration_gap,
                   avg(s.net_edge)                         AS mean_net_edge,
                   avg(s.fee_frac)                         AS mean_fee_frac,
                   sum(s.usd_traded)                       AS usd_traded,
                   stddev_samp(s.net_edge) / sqrt(count(*)) AS net_edge_se
            FROM scored s
            JOIN b ON s.vwap >= b.lo AND s.vwap < b.hi
            GROUP BY 1, 2, 3, 4
            ORDER BY 1, 3, 4
        ) TO '{RESULT.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)


def report(con: duckdb.DuckDBPyConnection) -> None:
    src = f"read_parquet('{RESULT.as_posix()}')"
    log.info("\ncalibration by entry price, whole window")
    log.info(f"  {'bucket':>12} {'n':>9} {'price':>7} {'payout':>7} {'gap':>8} "
          f"{'fee%':>6} {'net edge':>9} {'±se':>7} {'vol $':>13}")
    for lo, hi, n, mp, my, gap, fee, ne, se, vol in con.sql(f"""
        SELECT lo, hi, sum(n), sum(mean_price*n)/sum(n), sum(mean_payout*n)/sum(n),
               sum(calibration_gap*n)/sum(n), sum(mean_fee_frac*n)/sum(n),
               sum(mean_net_edge*n)/sum(n), avg(net_edge_se), sum(usd_traded)
        FROM {src} GROUP BY 1,2 ORDER BY 1
    """).fetchall():
        sig = "  <-" if (ne and se and abs(ne) > 2 * se) else ""
        log.info(f"  {lo:.2f}-{hi:.2f} {int(n):>9,} {mp:>7.3f} {my:>7.3f} "
              f"{gap:>+8.3f} {100*(fee or 0):>5.1f}% {ne:>+9.4f} "
              f"{(se or 0):>7.4f} {vol:>13,.0f}{sig}")

    log.info("\nfavourite / longshot gap by quarter (avg over buckets, n-weighted)")
    log.info(f"  {'era':>8} {'n':>9} {'mean |gap|':>11} {'fee%':>6} "
          f"{'force<-0.02':>12} {'force>0.02':>11}")
    for era, n, gap, fee, neg, pos in con.sql(f"""
        SELECT era, sum(n), sum(calibration_gap*n)/sum(n),
               sum(mean_fee_frac*n)/sum(n),
               sum(mean_net_edge*n) FILTER (WHERE lo < 0.5)/sum(n) FILTER (WHERE lo < 0.5),
               sum(mean_net_edge*n) FILTER (WHERE lo >= 0.5)/sum(n) FILTER (WHERE lo >= 0.5)
        FROM {src} GROUP BY 1 ORDER BY 1
    """).fetchall():
        log.info(f"  {era:>8} {int(n):>9,} {gap:>+11.3f} {100*(fee or 0):>5.2f}% "
              f"{(neg or 0):>+12.4f} {(pos or 0):>+11.4f}")

    log.info("\nnet edge by category, n-weighted (n >= 200)")
    for cat, n, gap, ne, se in con.sql(f"""
        SELECT category, sum(n), sum(calibration_gap*n)/sum(n),
               sum(mean_net_edge*n)/sum(n), avg(net_edge_se)
        FROM {src} GROUP BY 1 HAVING sum(n) >= 200
        ORDER BY abs(sum(mean_net_edge*n)/sum(n)) DESC LIMIT 12
    """).fetchall():
        log.info(f"  {cat:12} n={int(n):>8,}  gap={gap:+.4f}  net={ne:+.4f} ±{se:.4f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2024-01-01")
    ap.add_argument("--until", default="2026-03-31")
    ap.add_argument("--from-end-hours", type=int, default=24,
                    help="entry window starts this many hours before resolution")
    ap.add_argument("--to-end-hours", type=int, default=6,
                    help="entry window ends this many hours before resolution")
    ap.add_argument("--min-fills", type=int, default=5)
    ap.add_argument("--min-usd", type=float, default=100.0)
    ap.add_argument("--fresh", action="store_true",
                    help="delete completed quarter parts and rescan (default: resume)")
    args = ap.parse_args()

    for p in (TRADES, PIT):
        if not p.exists():
            log.info(f"missing {p}")
            return 1

    con = connect()
    con.execute(f"CREATE TABLE scored {SCHEMA}")

    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    until = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)

    global PARTS
    PARTS = OUT / f"screen_calibration_parts/entry=T-{args.from_end_hours}h..T-{args.to_end_hours}h"
    qs = quarters(since, until)
    # Resuming is the DEFAULT. Wiping completed work must be asked for: an
    # `if not args.resume` guard here once deleted five finished quarters on a
    # routine second run.
    if args.fresh and PARTS.exists():
        for f in PARTS.glob("quarter=*.parquet"):
            f.unlink()

    done = {f.stem.replace("quarter=", "")
            for f in PARTS.glob("quarter=*.parquet")} if PARTS.exists() else set()
    todo = [(a, b) for a, b in qs if f"{a.year}-Q{(a.month - 1) // 3 + 1}" not in done]
    log.info("{} quarter(s) total, {} already done, {} to run",
             len(qs), len(done), len(todo))

    t_all = time.time()
    for i, (a, b) in enumerate(todo, 1):
        t0 = time.time()
        label = f"{a.year}-Q{(a.month - 1) // 3 + 1}"
        build_entries(con, a, b, args.from_end_hours, args.to_end_hours,
                      args.min_fills, args.min_usd)
        n = accumulate(con)
        persist_part(con, label)
        log.info("[{}/{}] {}  {} tokens  {:.1f}s", i, len(todo), label, f"{n:,}",
                 time.time() - t0)
    if todo:
        log.info("scan total {:.1f}s", time.time() - t_all)

    parts = sorted(PARTS.glob("quarter=*.parquet"))
    if not parts:
        log.error("no quarter parts found under {}", PARTS)
        return 1
    # hive_partitioning=false: the directory name carries `entry=...`, which
    # DuckDB would otherwise materialise as an extra column and break the load.
    con.execute(f"INSERT INTO scored SELECT * FROM read_parquet('"
                f"{PARTS.as_posix()}/quarter=*.parquet', hive_partitioning=false)")
    n = con.sql("SELECT count(*) FROM scored").fetchall()[0][0]
    log.info("loaded {:,} scored rows from {} quarter file(s)", n, len(parts))

    aggregate(con)
    report(con)
    log.info(f"\nwrote {RESULT} and {ROWS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
