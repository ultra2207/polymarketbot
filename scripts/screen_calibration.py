"""Screen idea #1 (longshot / favourite bias) on the development window.

The question this answers
-------------------------
If you buy a token at price `p` and hold to resolution you receive `y` in
{0, 1, 0.5}. Across many markets, is `E[y | p]` equal to `p`? Betting markets
have a long-standing tendency to overprice longshots (low `p`, `E[y] < p`) and
underprice favourites (high `p`, `E[y] > p`). If that holds here and is bigger
than fees, it is an edge that needs no information advantage — only capacity.

What is measured, and what is deliberately not
---------------------------------------------
* Entry price is the **volume-weighted average of the first `--horizon-hours`**
  of a market's life. That is a price you could plausibly have paid, unlike the
  last trade before resolution.
* The label is the on-chain payout. `outcome_prices` is not used: it is a final
  traded price, not a payout.
* Fees come from `schedule_at`, so a 2025 fill is priced at 2025 rates rather
  than the 2026 schedule.
* This is a **gross-effect screen**. It can delete the idea. It cannot graduate
  it: there is no exit modelling and no fill modelling, so the result is "the
  effect exists / does not exist", not "this is tradable".

Usage
    uv run scripts/screen_calibration.py --since 2024-01 --until 2026-03
    uv run scripts/screen_calibration.py --since 2025-01 --horizon-hours 24
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, "src")

import duckdb

from polymarketbot.fees import schedule_at

OUT = Path("data/processed")
TRADES = Path("data/raw/sii/trades.parquet")
PIT = OUT / "markets_pit.parquet"
RESULT = OUT / "screen_calibration.parquet"

BUCKETS = [
    (0.00, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35),
    (0.35, 0.50), (0.50, 0.65), (0.65, 0.80), (0.80, 0.90), (0.90, 0.95),
    (0.95, 0.98), (0.98, 1.0001),
]


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 12")
    con.execute("SET memory_limit = '8GB'")
    con.execute("SET TimeZone = 'UTC'")
    return con


def build_entries(con: duckdb.DuckDBPyConnection, since: str, until: str,
                  horizon_h: int, min_fills: int, min_usd: float) -> None:
    """VWAP of each token over the first `horizon_h` hours of its market's life."""
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE entries AS
        WITH m AS (
            SELECT condition_id, market_id, token1, token2, created_at, end_date,
                   category, resolved, payout_0, payout_1, is_void,
                   label_source, fee_type, fee_rate, fee_exponent, fee_rebate,
                   is_zero_volume
            FROM read_parquet('{PIT.as_posix()}')
            WHERE NOT is_zero_volume
              AND resolved
              AND created_at >= TIMESTAMPTZ '{since}'
              AND created_at <  TIMESTAMPTZ '{until}'
              AND created_at <= end_date
        ),
        t AS (
            SELECT
                x.condition_id,
                x.asset_id AS token_id,
                x.timestamp AS ts,
                x.price, x.token_amount, x.usd_amount
            FROM read_parquet('{TRADES.as_posix()}') x
            WHERE x.usd_amount > 0 AND x.token_amount > 0
              AND x.timestamp >= epoch(TIMESTAMPTZ '{since}')
              AND x.timestamp <  epoch(TIMESTAMPTZ '{until}'::TIMESTAMPTZ
                                      + INTERVAL 7 DAY)
        )
        SELECT
            m.condition_id, m.category, m.created_at, m.end_date,
            m.label_source, m.is_void,
            m.fee_type, m.fee_rate, m.fee_exponent, m.fee_rebate,
            m.payout_0, m.payout_1,
            t.token_id,
            CASE WHEN t.token_id = m.token1 THEN 'token1'
                 WHEN t.token_id = m.token2 THEN 'token2' END AS slot,
            sum(t.price * t.token_amount) / sum(t.token_amount) AS vwap,
            sum(t.usd_amount)  AS usd_traded,
            count(*)           AS fills,
            min(t.ts)          AS first_fill_ts
        FROM m
        JOIN t ON t.condition_id = m.condition_id
        WHERE t.ts >= epoch(m.created_at)
          AND t.ts <  epoch(m.created_at) + {horizon_h} * 3600
          AND (t.token_id = m.token1 OR t.token_id = m.token2)
        GROUP BY ALL
        HAVING count(*) >= {min_fills} AND sum(t.usd_amount) >= {min_usd}
    """)


def score(con: duckdb.DuckDBPyConnection) -> None:
    """Attach payout and fees, then aggregate into price buckets."""
    rows = con.sql("""
        SELECT condition_id, category, created_at, is_void, token_id, slot,
               vwap, usd_traded, fills,
               fee_type, fee_rate, fee_exponent, fee_rebate,
               payout_0, payout_1
        FROM entries
    """).fetchall()
    print(f"  tokens with a usable entry: {len(rows):,}")

    con.execute("""
        CREATE OR REPLACE TEMP TABLE scored (
            condition_id VARCHAR, category VARCHAR, created_at TIMESTAMPTZ,
            token_id VARCHAR, slot VARCHAR, vwap DOUBLE, usd_traded DOUBLE,
            fills BIGINT, payout DOUBLE, p DOUBLE, fee_frac DOUBLE,
            net_edge DOUBLE, is_void BOOLEAN
        )
    """)

    batch: list[tuple] = []
    for (cid, cat, created, is_void, _token, slot, vwap, usd, fills,
         fee_type, fee_rate, fee_exp, fee_reb, p0, p1) in rows:
        if vwap is None or not (0.0 < vwap < 1.0) or p0 is None:
            continue
        payout = p0 if slot == "token1" else 1.0 - p0
        row = {"category": cat, "fee_type": fee_type, "fee_rate": fee_rate,
               "fee_exponent": fee_exp, "fee_rebate": fee_reb}
        sched = schedule_at(row, created)
        curve = vwap * (1.0 - vwap)
        fee_per_share = sched.rate * (curve ** sched.exponent) if sched.rate > 0 else 0.0
        fee_frac = fee_per_share / vwap          # as a fraction of notional
        net_edge = (payout - vwap) - fee_per_share
        batch.append((cid, cat, created, _token, slot, vwap, usd, fills,
                      payout, vwap, fee_frac, net_edge, bool(is_void)))
        if len(batch) >= 50_000:
            con.executemany("INSERT INTO scored VALUES (" + ",".join(["?"] * 13) + ")", batch)
            batch = []
    if batch:
        con.executemany("INSERT INTO scored VALUES (" + ",".join(["?"] * 13) + ")", batch)

    bounds = ", ".join(f"({lo}, {hi})" for lo, hi in BUCKETS)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE buckets AS
        SELECT * FROM (VALUES {bounds}) AS b(lo, hi)
    """)
    con.execute(f"""
        COPY (
            SELECT
                b.lo, b.hi,
                category,
                count(*)                                          AS n,
                avg(s.p)                                          AS mean_price,
                avg(s.payout)                                     AS mean_payout,
                avg(s.payout) - avg(s.p)                          AS calibration_gap,
                avg(s.net_edge)                                   AS mean_net_edge,
                avg(s.fee_frac)                                   AS mean_fee_frac,
                sum(s.net_edge * s.p)                             AS pnl_per_share,
                sum(s.usd_traded)                                 AS usd_traded,
                stddev_samp(s.net_edge) / sqrt(count(*))           AS net_edge_se
            FROM scored s
            JOIN buckets b ON s.p >= b.lo AND s.p < b.hi
            GROUP BY 1, 2, 3
            ORDER BY 1, 3
        ) TO '{RESULT.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)


def report(con: duckdb.DuckDBPyConnection) -> None:
    print("\ncalibration by entry price (all categories)")
    print(f"  {'bucket':>12} {'n':>8} {'price':>7} {'payout':>7} {'gap':>8} "
          f"{'fee%':>6} {'net edge':>9} {'±se':>7} {'vol $':>12}")
    for lo, hi, n, mp, my, gap, fee, ne, se, vol in con.sql(f"""
        SELECT lo, hi, sum(n), avg(mean_price), avg(mean_payout),
               avg(calibration_gap), avg(mean_fee_frac), avg(mean_net_edge),
               avg(net_edge_se), sum(usd_traded)
        FROM read_parquet('{RESULT.as_posix()}')
        GROUP BY 1,2 ORDER BY 1
    """).fetchall():
        flag = "  <-" if (ne is not None and se is not None and abs(ne) > 2 * se) else ""
        print(f"  {lo:.2f}-{hi:.2f} {int(n):>8,} {mp:>7.3f} {my:>7.3f} "
              f"{gap:>+8.3f} {100*(fee or 0):>5.1f}% {ne:>+9.4f} "
              f"{(se or 0):>7.4f} {vol:>12,.0f}{flag}")

    # Weight by n: an unweighted mean across buckets always lands near 0.50
    # because every market contributes both a favourite and a longshot token.
    print("\nnet edge by category, volume-weighted (n >= 200)")
    for cat, n, mp, my, gap, ne, se in con.sql(f"""
        SELECT category, sum(n),
               sum(mean_price  * n) / sum(n),
               sum(mean_payout * n) / sum(n),
               sum(calibration_gap * n) / sum(n),
               sum(mean_net_edge * n) / sum(n),
               avg(net_edge_se)
        FROM read_parquet('{RESULT.as_posix()}')
        GROUP BY 1 HAVING sum(n) >= 200
        ORDER BY abs(sum(mean_net_edge * n) / sum(n)) DESC LIMIT 12
    """).fetchall():
        print(f"  {cat:12} n={int(n):>7,}  price={mp:.3f} payout={my:.3f} "
              f"gap={gap:+.3f} net={ne:+.4f} ±{se:.4f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2024-01-01")
    ap.add_argument("--until", default="2026-03-31")
    ap.add_argument("--horizon-hours", type=int, default=48,
                    help="entry window at the start of a market's life")
    ap.add_argument("--min-fills", type=int, default=5)
    ap.add_argument("--min-usd", type=float, default=100.0)
    args = ap.parse_args()

    for p in (TRADES, PIT):
        if not p.exists():
            print(f"missing {p}")
            return 1

    con = connect()
    print(f"window {args.since} -> {args.until}, entry = first {args.horizon_hours}h")
    build_entries(con, args.since, args.until, args.horizon_hours,
                  args.min_fills, args.min_usd)
    score(con)
    report(con)
    print(f"\nwrote {RESULT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
