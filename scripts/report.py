"""All cross-sections, read from a 1.7 MB cube instead of the 13.6 GB table.

Every table here is a roll-up of `data/processed/cube.parquet`; this whole script runs in
seconds because the expensive scan already happened once in `build_cube.py`.

Sections
--------
1. Selection - at the same price, do the top decile pick better outcomes?
2. Closure   - do they close winners and hold losers?
3. Lifecycle - where the PnL comes from (round trip vs held to resolution)
4. Size      - the notional distribution of positions held to resolution
"""

from __future__ import annotations

import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

CUBE = "data/processed/cube.parquet"


def main() -> None:
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute(f"CREATE OR REPLACE VIEW c AS SELECT * FROM read_parquet('{CUBE}')")

    log.info("=== 1. selection: at the same price, does the top decile pick better outcomes? ===")
    log.info("  bucket     top: payout  entry    edge  |  rest: payout  entry    edge  |    gap")
    for r in con.sql("""
        SELECT pbucket,
               sum(payout_bs) FILTER (WHERE dec = 1) / nullif(sum(bs) FILTER (WHERE dec = 1),0),
               sum(bc)        FILTER (WHERE dec = 1) / nullif(sum(bs) FILTER (WHERE dec = 1),0),
               sum(payout_bs) FILTER (WHERE dec > 1) / nullif(sum(bs) FILTER (WHERE dec > 1),0),
               sum(bc)        FILTER (WHERE dec > 1) / nullif(sum(bs) FILTER (WHERE dec > 1),0)
        FROM c WHERE outcome >= 0 AND pbucket >= 0
        GROUP BY pbucket ORDER BY pbucket
    """).fetchall():
        if r[1] is None or r[3] is None:
            continue
        te = r[1] - r[2]
        re = r[3] - r[4]
        log.info(f"  {r[0]*5:>3}-{r[0]*5+5:<3}%  {r[1]:>7.3f}  {r[2]:>6.3f}  {te:>+6.3f}  |"
              f" {r[3]:>7.3f}  {r[4]:>6.3f}  {re:>+6.3f}  | {te-re:>+6.3f}")

    r = con.sql("""
        SELECT (sum(payout_bs) FILTER (WHERE dec = 1) - sum(bc) FILTER (WHERE dec = 1))
                 / nullif(sum(bs) FILTER (WHERE dec = 1),0),
               (sum(payout_bs) FILTER (WHERE dec > 1) - sum(bc) FILTER (WHERE dec > 1))
                 / nullif(sum(bs) FILTER (WHERE dec > 1),0)
        FROM c WHERE outcome >= 0
    """).fetchall()[0]
    log.info(f"\n  total edge per share bought:  top {r[0]:+.4f}   rest {r[1]:+.4f}")

    log.info("\n\n=== 2. closure: do winners get closed and losers held? (top decile) ===")
    log.info("  outcome      positions     closed %")
    for r in con.sql("""
        SELECT CASE outcome WHEN 1 THEN 'would win' WHEN 0 THEN 'would lose' ELSE 'unresolved' END o,
               sum(n), 100.0*sum(n_closed)/sum(n)
        FROM c WHERE dec = 1 GROUP BY outcome ORDER BY outcome DESC
    """).fetchall():
        log.info(f"  {r[0]:<12} {r[1]:>12,}   {r[2]:>8.1f}")

    log.info("\n\n=== 3. lifecycle: round trip vs held, by wallet decile ===")
    log.info("  dec   volume $m    closed $m    held $m    exit $m     net $m")
    for r in con.sql("""
        SELECT dec, (sum(bc) + sum(sc))/1e6,
               sum(closed_pnl)/1e6, sum(held_pnl)/1e6, sum(exit_pnl)/1e6,
               (sum(closed_pnl) + sum(held_pnl) + sum(exit_pnl))/1e6
        FROM c WHERE dec > 0 GROUP BY dec ORDER BY dec
    """).fetchall():
        log.info(f"  {r[0]:>3}  {r[1]:>10,.0f}  {r[2]:>+11.2f}  {r[3]:>+9.2f}  "
              f"{r[4]:>+9.2f}  {r[5]:>+9.2f}")

    log.info("\n\n=== 4. top decile: never-closed positions by size ===")
    log.info("  notional      never sold    held PnL $m    per held $")
    labels = ["<$1", "$1-10", "$10-100", "$100-1k", "$1k-10k",
              "$10k-100k", "$100k-1M", "$1M-10M", "$10M+"]
    for r in con.sql("""
        SELECT sbucket, sum(n_buy_only),
               sum(held_pnl)/1e6,
               sum(held_pnl)/nullif(sum(n_held),0)
        FROM c WHERE dec = 1 GROUP BY sbucket ORDER BY sbucket
    """).fetchall():
        log.info(f"  {labels[r[0]]:<12} {r[1]:>12,}   {r[2]:>+9.2f}   {r[3]:>+14.2f}")


if __name__ == "__main__":
    main()
