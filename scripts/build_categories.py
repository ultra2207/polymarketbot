"""Build a per-market category table from slug rules.

Gamma's `category` is null for most markets and keyset pagination caps at 100
rows/page, so a 4 M-market crawl is impractical. The roadmap explicitly allows
deriving history from slugs; this script does that and *measures* the residual
`other` bucket rather than hiding it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "src")

from polymarketbot.db import connect
from polymarketbot.universe.categories import sql_case
from polymarketbot.logs import get_logger
log = get_logger(__name__)

OUT = Path("data/processed")
SRC = OUT / "market_labels.parquet"
DEST = OUT / "market_categories.parquet"
OVERRIDES = OUT / "gamma_category_overrides.parquet"


def main() -> int:
    con = connect(threads=12, memory_limit="8GB")

    txt = "lower(coalesce(event_slug,'') || ' ' || coalesce(slug,''))"
    case = sql_case(txt)

    # Gamma tag enrichment is an override, applied only where it produced a
    # real category. Where it didn't, the slug rule stands.
    if OVERRIDES.exists():
        override_join = f"""
            LEFT JOIN read_parquet('{OVERRIDES.as_posix()}') o
                   ON o.condition_id = r.condition_id
        """
        final_cat = """CASE
                WHEN o.derived_category IS NOT NULL AND o.derived_category <> 'other'
                     THEN o.derived_category
                ELSE r.rule_category
            END"""
        source = """CASE
                WHEN o.derived_category IS NOT NULL AND o.derived_category <> 'other'
                     THEN 'gamma_tags'
                ELSE 'rule'
            END"""
    else:
        override_join = ""
        final_cat = "r.rule_category"
        source = "'rule'"

    con.execute(f"""
        COPY (
            WITH r AS (
                SELECT
                    condition_id,
                    market_id,
                    created_at,
                    volume,
                    closed,
                    event_slug,
                    slug,
                    {case} AS rule_category
                FROM read_parquet('{SRC.as_posix()}')
            )
            SELECT
                r.condition_id,
                r.market_id,
                r.created_at,
                r.volume,
                r.closed,
                r.event_slug,
                r.slug,
                {final_cat} AS category,
                {source} AS category_source
            FROM r
            {override_join}
        ) TO '{DEST.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    n = con.sql(f"SELECT count(*) FROM read_parquet('{DEST.as_posix()}')").fetchall()[0][0]
    log.info(f"wrote {DEST} ({n:,} markets)\n")

    log.info("all markets, by count and volume:")
    log.info(f"  {'category':12} {'markets':>10} {'share':>7} {'volume $B':>10} {'vol share':>10}")
    rows = con.sql(f"""
        WITH t AS (SELECT count(*) n, sum(volume) v FROM read_parquet('{DEST.as_posix()}'))
        SELECT category, count(*) n, sum(volume) v,
               100.0*count(*)/(SELECT n FROM t) pc,
               100.0*sum(volume)/(SELECT v FROM t) pv
        FROM read_parquet('{DEST.as_posix()}')
        GROUP BY 1 ORDER BY n DESC
    """).fetchall()
    for cat, cnt, vol, pc, pv in rows:
        log.info(f"  {cat:12} {cnt:>10,} {pc:>6.1f}% {vol/1e9:>10.2f} {pv:>9.1f}%")

    log.info("\ncategory source:")
    for r in con.sql(f"""
        SELECT category_source, count(*) n,
               round(100.0*count(*)/(SELECT count(*) FROM read_parquet('{DEST.as_posix()}')),1) pct
        FROM read_parquet('{DEST.as_posix()}') GROUP BY 1 ORDER BY n DESC
    """).fetchall():
        log.info(f"  {r[0]:12} {r[1]:>10,}  {r[2]:>5.1f}%")

    log.info("\ntradable universe proxy (closed, volume > 0):")
    log.info(f"  {'category':12} {'markets':>10} {'share':>7} {'volume $B':>10} {'vol share':>10}")
    rows = con.sql(f"""
        WITH u AS (
            SELECT category, count(*) n, sum(volume) v
            FROM read_parquet('{DEST.as_posix()}')
            WHERE closed = 1 AND volume > 0 GROUP BY 1
        ), t AS (SELECT sum(n) tn, sum(v) tv FROM u)
        SELECT category, n, v, 100.0*n/(SELECT tn FROM t), 100.0*v/(SELECT tv FROM t)
        FROM u ORDER BY n DESC
    """).fetchall()
    for cat, cnt, vol, pc, pv in rows:
        log.info(f"  {cat:12} {cnt:>10,} {pc:>6.1f}% {vol/1e9:>10.2f} {pv:>9.1f}%")

    log.info("\n'other' share by year (tradable proxy):")
    for r in con.sql(f"""
        SELECT date_part('year', created_at) yr, count(*) n,
               round(100.0*count(*) FILTER (WHERE category='other')/count(*),1) other_pct
        FROM read_parquet('{DEST.as_posix()}')
        WHERE closed=1 AND volume>0
        GROUP BY 1 ORDER BY 1
    """).fetchall():
        log.info(f"  {int(r[0])}  markets={r[1]:>9,}  other={r[2]:>5.1f}%")

    log.info("\nlargest 'other' families (event_slug prefix):")
    for r in con.sql(f"""
        SELECT split_part(event_slug,'-',1) pref, count(*) n, round(sum(volume)/1e9,2) v
        FROM read_parquet('{DEST.as_posix()}')
        WHERE category='other' AND closed=1 AND volume>0 AND event_slug IS NOT NULL
        GROUP BY 1 ORDER BY n DESC LIMIT 15
    """).fetchall():
        log.info(f"  {r[0]:18} {r[1]:>8,}  ${r[2]:>6.2f}B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
