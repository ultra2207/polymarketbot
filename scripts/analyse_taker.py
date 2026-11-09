"""Where exactly does the taker edge live? Category x price x time-to-resolution.

Established so far: the top decile earns on taking (+6.9 % of money deployed in maker-share
buckets 1-4) and loses on making (-1.0 %). This script localises the taker edge so the next
step can be a supervised model rather than a search.

Reads the cube (81 MB) so it runs in about a second.
"""

from __future__ import annotations

import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

CUBE = "data/processed/cube.parquet"
CATS = {0: "other", 1: "crypto", 2: "sports", 3: "weather", 4: "politics",
        5: "geopolitics", 6: "tech", 7: "economics", 8: "culture", 9: "finance",
        10: "health"}
TTLS = ["<1h", "1-6h", "6-24h", "1-3d", "3-7d", "7-30d", ">30d", "unknown"]


def main() -> None:
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute(f"CREATE OR REPLACE VIEW c AS SELECT * FROM read_parquet('{CUBE}')")

    # taker = maker share 0-4; maker = 5-9
    con.execute("""
        CREATE OR REPLACE VIEW t AS
        SELECT *, CASE WHEN mbucket BETWEEN 0 AND 4 THEN 'taker'
                       WHEN mbucket BETWEEN 5 AND 9 THEN 'maker'
                       ELSE 'unknown' END AS role
        FROM c WHERE dec = 1 AND outcome >= 0 AND mbucket >= 0
    """)

    log.info("=== taker vs maker edge, by category ===")
    log.info("  category       taker edge   volume $m |  maker edge   volume $m")
    for r in con.execute("""
        SELECT coalesce(cat,0),
               (sum(payout_bs) FILTER (WHERE role='taker') - sum(bc) FILTER (WHERE role='taker'))
                 / nullif(sum(bs) FILTER (WHERE role='taker'),0),
               sum(bc) FILTER (WHERE role='taker')/1e6,
               (sum(payout_bs) FILTER (WHERE role='maker') - sum(bc) FILTER (WHERE role='maker'))
                 / nullif(sum(bs) FILTER (WHERE role='maker'),0),
               sum(bc) FILTER (WHERE role='maker')/1e6
        FROM t GROUP BY 1 ORDER BY sum(bc) DESC
    """).fetchall():
        if r[1] is None:
            continue
        log.info(f"  {CATS.get(r[0], '?'):<12}  {r[1]:>+9.4f}  {r[2]:>9,.0f} | "
              f"{r[3]:>+10.4f}  {r[4]:>9,.0f}" if r[3] is not None else "")

    log.info("\n=== taker calibration by price band (does payout exceed price?) ===")
    log.info("  price band   volume $m   payout   entry    edge")
    for r in con.execute("""
        SELECT pbucket, sum(bc)/1e6,
               sum(payout_bs)/nullif(sum(bs),0),
               sum(bc)/nullif(sum(bs),0),
               (sum(payout_bs)-sum(bc))/nullif(sum(bs),0)
        FROM t WHERE role='taker' GROUP BY pbucket ORDER BY pbucket
    """).fetchall():
        if r[3] is None:
            continue
        log.info(f"  {r[0]*5:>3}-{r[0]*5+5:<3}%  {r[1]:>9,.0f}  {r[2]:>6.3f}  {r[3]:>6.3f}  {r[4]:>+7.4f}")

    log.info("\n=== taker edge by time-to-resolution ===")
    log.info("  ttl          volume $m    edge")
    for r in con.execute("""
        SELECT tbucket, sum(bc)/1e6, (sum(payout_bs)-sum(bc))/nullif(sum(bs),0)
        FROM t WHERE role='taker' GROUP BY tbucket ORDER BY tbucket
    """).fetchall():
        log.info(f"  {TTLS[r[0]]:<10}  {r[1]:>9,.0f}  {r[2]:>+8.4f}")

    log.info("\n=== the largest positive cells (cat x price band), taker only ===")
    log.info("  category     band       volume $m    payout   entry     edge")
    for r in con.execute("""
        SELECT cat, pbucket, sum(bc)/1e6,
               sum(payout_bs)/nullif(sum(bs),0),
               sum(bc)/nullif(sum(bs),0),
               (sum(payout_bs)-sum(bc))/nullif(sum(bs),0)
        FROM t WHERE role='taker'
        GROUP BY 1, 2 HAVING sum(bc) > 5e7
        ORDER BY (sum(payout_bs)-sum(bc))/nullif(sum(bs),0) DESC LIMIT 12
    """).fetchall():
        log.info(f"  {CATS.get(r[0],'?'):<12} {r[1]*5:>3}-{r[1]*5+5:<3}%  {r[2]:>9,.0f}   "
              f"{r[3]:>6.3f}  {r[4]:>6.3f}  {r[5]:>+8.4f}")

    log.info("\n=== the largest negative cells (cat x price band), maker only ===")
    log.info("  category     band       volume $m    payout   entry     edge")
    for r in con.execute("""
        SELECT cat, pbucket, sum(bc)/1e6,
               sum(payout_bs)/nullif(sum(bs),0),
               sum(bc)/nullif(sum(bs),0),
               (sum(payout_bs)-sum(bc))/nullif(sum(bs),0)
        FROM t WHERE role='maker'
        GROUP BY 1, 2 HAVING sum(bc) > 5e7
        ORDER BY (sum(payout_bs)-sum(bc))/nullif(sum(bs),0) ASC LIMIT 12
    """).fetchall():
        log.info(f"  {CATS.get(r[0],'?'):<12} {r[1]*5:>3}-{r[1]*5+5:<3}%  {r[2]:>9,.0f}   "
              f"{r[3]:>6.3f}  {r[4]:>6.3f}  {r[5]:>+8.4f}")


if __name__ == "__main__":
    main()
