"""Does the top decile's selection edge come from maker fills or taker fills?

Why this matters
----------------
At the same price, the top decile's buys resolve better than everyone else's, and neither
category, price bucket nor time-to-resolution explains it. The remaining split is role:

- MAKER fills: they posted a bid and were hit by a seller. The seller chose to sell, which is
  the classic adverse-selection channel — the maker earns only if the flow is uninformed.
- TAKER fills: they chose to lift an offer. That is a deliberate decision, i.e. a forecast.

If the edge sits entirely in maker fills, the "edge" is liquidity provision against noise, not
an EV model — and it is not replicable without standing quotes in thousands of markets.
If it sits in taker fills, there is a decision being made at a moment, which is modelable.

Reads the cube built by build_cube.py (33 MB), so it runs in well under a second.
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

    log.info("=== maker share of bought volume, by group (0 = pure taker, 9 = pure maker) ===")
    log.info("  mbucket   top %vol   rest %vol")
    for r in con.execute("""
        SELECT mbucket,
               100.0 * sum(bs) FILTER (WHERE dec = 1) / sum(sum(bs) FILTER (WHERE dec = 1)) OVER (),
               100.0 * sum(bs) FILTER (WHERE dec > 1) / sum(sum(bs) FILTER (WHERE dec > 1)) OVER ()
        FROM c WHERE mbucket >= 0 GROUP BY mbucket ORDER BY mbucket
    """).fetchall():
        log.info(f"  {r[0]:>7}   {r[1]:>7.1f}   {r[2]:>8.1f}")

    log.info("\n=== edge per share bought, by maker share (all prices) ===")
    log.info("  mbucket   top edge | rest edge |     gap     top shares $m")
    for r in con.execute("""
        SELECT mbucket,
               (sum(payout_bs) FILTER (WHERE dec = 1) - sum(bc) FILTER (WHERE dec = 1))
                 / nullif(sum(bs) FILTER (WHERE dec = 1), 0),
               (sum(payout_bs) FILTER (WHERE dec > 1) - sum(bc) FILTER (WHERE dec > 1))
                 / nullif(sum(bs) FILTER (WHERE dec > 1), 0),
               sum(bc) FILTER (WHERE dec = 1) / 1e6
        FROM c WHERE mbucket >= 0 AND outcome >= 0
        GROUP BY mbucket ORDER BY mbucket
    """).fetchall():
        if r[1] is None or r[2] is None:
            continue
        log.info(f"  {r[0]:>7}   {r[1]:>+8.4f} | {r[2]:>+9.4f} | {r[1]-r[2]:>+8.4f}   {r[3]:>10,.0f}")

    log.info("\n=== the same inside the 70-85 % band (where the gap is largest) ===")
    log.info("  mbucket   top edge | rest edge |     gap     top shares $m")
    for r in con.execute("""
        SELECT mbucket,
               (sum(payout_bs) FILTER (WHERE dec = 1) - sum(bc) FILTER (WHERE dec = 1))
                 / nullif(sum(bs) FILTER (WHERE dec = 1), 0),
               (sum(payout_bs) FILTER (WHERE dec > 1) - sum(bc) FILTER (WHERE dec > 1))
                 / nullif(sum(bs) FILTER (WHERE dec > 1), 0),
               sum(bc) FILTER (WHERE dec = 1) / 1e6
        FROM c WHERE mbucket >= 0 AND outcome >= 0 AND pbucket BETWEEN 14 AND 16
        GROUP BY mbucket ORDER BY mbucket
    """).fetchall():
        if r[1] is None or r[2] is None:
            continue
        log.info(f"  {r[0]:>7}   {r[1]:>+8.4f} | {r[2]:>+9.4f} | {r[1]-r[2]:>+8.4f}   {r[3]:>10,.0f}")

    log.info("\n=== decomposition of the top decile's total edge by role ===")
    for r in con.execute("""
        SELECT
          CASE WHEN mbucket >= 7 THEN 'maker-heavy (70-100%)'
               WHEN mbucket >= 4 THEN 'mixed (40-70%)'
               WHEN mbucket >= 0 THEN 'taker-heavy (0-40%)'
               ELSE 'unknown' END AS role,
          (sum(payout_bs) - sum(bc)) / 1e6 AS edge_m,
          sum(bc) / 1e6 AS shares_m
        FROM c WHERE dec = 1 AND outcome >= 0 AND mbucket >= 0
        GROUP BY role ORDER BY edge_m DESC
    """).fetchall():
        log.info(f"  {r[0]:<24} edge ${r[1]:>+8.2f} m   on ${r[2]:>9,.0f} m bought")


if __name__ == "__main__":
    main()
