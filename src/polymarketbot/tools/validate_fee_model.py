"""Compare the era-based fee fallback against Gamma's per-market schedules.

`configs/fees.yaml` is a fallback for markets whose `feeSchedule` has not been
fetched. This script measures how good that fallback actually is:

* rate / exponent / rebate agreement by category and month
* the fee error at p=0.5 (peak) and p=0.1 (away from the peak), per 100 shares

The per-market schedule is authoritative; a disagreement is a fallback error,
not a data error.

Only the distinct (category, month) pairs are resolved in Python — one era
lookup each. The 1.9 M-row comparison is a DuckDB join, not a Python loop.

Usage
    uv run python -m polymarketbot.tools.validate_fee_model
"""

from __future__ import annotations

from pathlib import Path

import argparse

import duckdb

from polymarketbot.fees import era_schedule, taker_fee
from polymarketbot.logs import get_logger
log = get_logger(__name__)

OUT = Path("data/processed")
META = OUT / "market_metadata_gamma.parquet"
PIT = OUT / "markets_pit.parquet"

# Gamma metadata carries only fee fields; category and created_at live in PIT.
SOURCE = f"""
    read_parquet('{PIT.as_posix()}') p
    JOIN read_parquet('{META.as_posix()}') m USING (condition_id)
"""

# Fee per 100 shares: fee = 100 * rate * (p(1-p))^exponent
FEE_AT = "100 * {rate} * pow({curve}, {exp})"


def _curve(p: float) -> str:
    return f"({p} * (1 - {p}))"


def _source(meta: Path, pit: Path) -> str:
    return f"""
    read_parquet('{pit.as_posix()}') p
    JOIN read_parquet('{meta.as_posix()}') m USING (condition_id)
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare the era fee fallback with Gamma per-market schedules"
    )
    parser.add_argument("--meta", default=str(META), help="Gamma metadata parquet")
    parser.add_argument("--pit", default=str(PIT), help="point-in-time markets parquet")
    return parser


def main(argv: list[str] | None = None) -> int:
    global META, PIT, SOURCE
    args = build_parser().parse_args(argv)
    META = Path(args.meta)
    PIT = Path(args.pit)
    SOURCE = _source(META, PIT)
    if not META.exists():
        log.info(f"missing {META}; run python -m polymarketbot.data.builders.enrich_market_metadata first")
        return 1

    con = duckdb.connect()
    con.execute("SET threads = 8")
    con.execute("SET TimeZone = 'UTC'")

    pairs = con.sql(f"""
        SELECT DISTINCT coalesce(p.category, 'other') AS category,
                        strftime(p.created_at, '%Y-%m') AS month,
                        date_trunc('month', p.created_at) AS month_ts
        FROM {SOURCE}
        WHERE m.fee_rate IS NOT NULL
    """).fetchall()
    if not pairs:
        log.info("no per-market fee schedules yet")
        return 1

    # era fallback for each distinct (category, month)
    era_rows = []
    for category, month, month_ts in pairs:
        era = era_schedule(category, month_ts)
        era_rows.append((category, month, era.rate, era.exponent, era.rebate_rate))

    con.execute("""
        CREATE TEMP TABLE era (
            category VARCHAR, month VARCHAR,
            rate DOUBLE, exponent DOUBLE, rebate DOUBLE
        )
    """)
    con.executemany("INSERT INTO era VALUES (?, ?, ?, ?, ?)", era_rows)

    con.execute(f"""
        CREATE TEMP TABLE j AS
        SELECT coalesce(p.category, 'other') AS category,
               strftime(p.created_at, '%Y-%m') AS month,
               m.fee_type,
               CAST(m.fee_rate AS DOUBLE)       AS g_rate,
               CAST(coalesce(m.fee_exponent, 1.0) AS DOUBLE) AS g_exp,
               CAST(coalesce(m.fee_rebate, 0.0) AS DOUBLE)   AS g_rebate,
               e.rate AS e_rate, e.exponent AS e_exp, e.rebate AS e_rebate
        FROM {SOURCE}
        JOIN era e
          ON e.category = coalesce(p.category, 'other')
         AND e.month    = strftime(p.created_at, '%Y-%m')
        WHERE m.fee_rate IS NOT NULL
    """)

    total, rate_ok, exp_ok, reb_ok = con.sql("""
        SELECT count(*),
               count(*) FILTER (WHERE abs(g_rate - e_rate) < 1e-9),
               count(*) FILTER (WHERE abs(g_exp - e_exp) < 1e-9),
               count(*) FILTER (WHERE abs(g_rebate - e_rebate) < 1e-9)
        FROM j
    """).fetchall()[0]

    log.info(f"markets with a per-market schedule: {total:,}")
    log.info(f"fallback agreement:  rate {100*rate_ok/total:6.2f}%   "
          f"exponent {100*exp_ok/total:6.2f}%   rebate {100*reb_ok/total:6.2f}%\n")

    log.info("worst 15 category/month cells by mean peak error (fee per 100 shares):")
    rows = con.sql(f"""
        SELECT category, month, count(*) n,
               100.0 * count(*) FILTER (WHERE abs(g_rate - e_rate) < 1e-9) / count(*) rate_pct,
               100.0 * count(*) FILTER (WHERE abs(g_exp  - e_exp)  < 1e-9) / count(*) exp_pct,
               100.0 * count(*) FILTER (WHERE abs(g_rebate - e_rebate) < 1e-9) / count(*) reb_pct,
               avg(abs({FEE_AT.format(rate='g_rate', curve=_curve(0.5), exp='g_exp')}
                     - {FEE_AT.format(rate='e_rate', curve=_curve(0.5), exp='e_exp')})) mean_err,
               max(abs({FEE_AT.format(rate='g_rate', curve=_curve(0.5), exp='g_exp')}
                     - {FEE_AT.format(rate='e_rate', curve=_curve(0.5), exp='e_exp')})) max_err
        FROM j GROUP BY 1, 2 ORDER BY mean_err DESC LIMIT 15
    """).fetchall()
    log.info(f"  {'category':12} {'month':8} {'n':>7} {'rate':>6} {'exp':>6} {'rebate':>7} "
          f"{'mean_err':>9} {'max_err':>8}")
    for cat, month, n, rp, ep, rbp, mean_err, max_err in rows:
        log.info(f"  {cat:12} {month:8} {n:>7,} {rp:>5.0f}% {ep:>5.0f}% {rbp:>6.0f}% "
              f"{mean_err:>9.4f} {max_err:>8.4f}")

    log.info("\nlargest single-market fallback errors (fee per 100 shares at p=0.5):")
    for cat, month, ftype, err, g, e in con.sql(f"""
        SELECT category, month, fee_type,
               abs({FEE_AT.format(rate='g_rate', curve=_curve(0.5), exp='g_exp')}
                 - {FEE_AT.format(rate='e_rate', curve=_curve(0.5), exp='e_exp')}) AS err,
               (g_rate, g_exp, g_rebate) AS gamma, (e_rate, e_exp, e_rebate) AS era
        FROM j ORDER BY err DESC LIMIT 10
    """).fetchall():
        log.info(f"  {err:8.4f}  {cat:11} {month} {str(ftype):22} gamma={g} era={e}")

    # Away from the peak the exponent matters less; the rate still dominates.
    log.info("\nby fee_type, mean absolute fallback error at p=0.5 and p=0.1:")
    for ftype, n, e50, e10 in con.sql(f"""
        SELECT fee_type, count(*) n,
               avg(abs({FEE_AT.format(rate='g_rate', curve=_curve(0.5), exp='g_exp')}
                     - {FEE_AT.format(rate='e_rate', curve=_curve(0.5), exp='e_exp')})),
               avg(abs({FEE_AT.format(rate='g_rate', curve=_curve(0.1), exp='g_exp')}
                     - {FEE_AT.format(rate='e_rate', curve=_curve(0.1), exp='e_exp')}))
        FROM j GROUP BY 1 ORDER BY 2 DESC
    """).fetchall():
        log.info(f"  {str(ftype):24} n={n:>8,}  p=0.5 {e50:7.4f}  p=0.1 {e10:7.4f}")

    # Sanity: the published peak for a crypto 15-min market is 1.56% of notional.
    peak = taker_fee(100, 0.5, era_schedule("crypto", "2026-02-01"))
    log.info(f"\ncrypto era fallback at 2026-02, 100 shares at p=0.5: {peak:.4f} USDC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
