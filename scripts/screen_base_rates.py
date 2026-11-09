"""Base-rate screen: do recurring market families beat the price as a forecast?

Idea C2 in `docs/alpha-pipeline.md` - "the cheapest idea in the backlog", because it
needs no external data at all. Polymarket repeats market *families*: the same question
shape recurs with a different date in the slug
(`will-btc-be-above-...-on-october-24`, `highest-temperature-in-nyc-on-...`). The
historical base rate of a family is therefore a GROUP BY over labels already held.

Two questions are asked, and only the second is an edge.

1. **Is the base rate informative?** Nearly free, and true for some family by accident.
2. **Does the market price already know it?** A base rate is tradable only if the entry
   price deviates from it systematically. Measured as a Brier score: if a constant
   family base rate predicts outcomes *better* than the market's own price, the market is
   not incorporating its own history, and that gap is the candidate edge.

The comparison is deliberately harsh: a base rate discards everything instance-specific,
so it should lose. It only wins where the market ignores history.

Entry prices come from `screen_calibration_rows.parquet`, which holds a per-market VWAP
for the T-24 h -> T-6 h slot. That is a mid, not an ask, so any surviving edge must still
clear half the ~2 c spread.

Usage
    uv run scripts/screen_base_rates.py
    uv run scripts/screen_base_rates.py --min-family 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, "src")

import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

OUT = Path("data/processed")

# What differs between members of a family is the date suffix in the slug.
SUFFIXES = [
    r"-on-[a-z]+-[0-9]{1,2}$",
    r"-on-[0-9]{4}-[0-9]{2}-[0-9]{2}$",
    r"-in-[0-9]{4}$",
    r"-[0-9]{4}$",
]


def family_expr(col: str = "slug") -> str:
    e = col
    for pat in SUFFIXES:
        e = f"regexp_replace({e}, '{pat}', '')"
    return e


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 8")
    con.execute("SET memory_limit = '6GB'")
    con.execute("SET preserve_insertion_order = false")
    return con


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-family", type=int, default=20,
                    help="families with fewer resolved instances are dropped")
    ap.add_argument("--dest", default=str(OUT))
    args = ap.parse_args()

    con = connect()

    # The base rate MUST be computed over markets, not tokens: every binary market
    # resolves one YES and one NO, so averaging over both tokens yields exactly 0.5
    # by construction and measures nothing.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE mk AS
        SELECT condition_id, token1, token2, payout_0, payout_1,
               {family_expr()} AS family
        FROM read_parquet('data/processed/market_labels.parquet')
        WHERE resolved AND NOT coalesce(is_void, FALSE)
          AND slug IS NOT NULL AND token1 IS NOT NULL AND token2 IS NOT NULL
          AND payout_0 IS NOT NULL AND payout_1 IS NOT NULL
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE fam AS
        SELECT family, count(*) AS n, avg(payout_0) AS base_rate
        FROM mk GROUP BY 1
    """)

    # A token's prior is the family's YES rate, or its complement on the NO side.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE tok AS
        SELECT m.condition_id, m.token1 AS token_id, m.payout_0 AS payout,
               m.family, f.n AS n_family, f.base_rate AS prior
        FROM mk m JOIN fam f USING (family)
        UNION ALL
        SELECT m.condition_id, m.token2, m.payout_1,
               m.family, f.n, 1.0 - f.base_rate
        FROM mk m JOIN fam f USING (family)
    """)

    n_fam = con.sql(f"SELECT count(*) FROM fam WHERE n >= {args.min_family}").fetchall()[0][0]
    n_tok = con.sql(
        f"SELECT count(*) FROM tok t JOIN fam f USING (family) "
        f"WHERE f.n >= {args.min_family}").fetchall()[0][0]
    log.info(f"families with >= {args.min_family} resolved instances: {n_fam:,}")
    log.info(f"token-observations inside them: {n_tok:,}")
    if n_fam == 0:
        log.info("no recurring families found; the slug suffix rules need widening")
        return 1

    con.execute("""
        CREATE OR REPLACE TEMP TABLE cmp AS
        SELECT t.condition_id, t.token_id, t.family, t.payout,
               t.n_family, t.prior AS base_rate, r.vwap AS price
        FROM tok t
        JOIN read_parquet('data/processed/screen_calibration_rows.parquet') r
          ON r.token_id = t.token_id
        WHERE t.n_family >= ?
    """, [args.min_family])

    n_cmp = con.sql("SELECT count(*) FROM cmp").fetchall()[0][0]
    if n_cmp == 0:
        log.info("no overlap between family tokens and entry-priced tokens")
        return 1
    log.info(f"compared against an observed entry price: {n_cmp:,}\n")

    n, bp, bb, bl, dev = con.sql("""
        SELECT count(*),
               avg((price - payout) * (price - payout)),
               avg((base_rate - payout) * (base_rate - payout)),
               avg(((price + base_rate) / 2 - payout)
                   * ((price + base_rate) / 2 - payout)),
               avg(abs(base_rate - price))
        FROM cmp
    """).fetchall()[0]
    log.info(f"{'forecaster':22} {'Brier':>9}")
    log.info(f"{'market price':22} {bp:>9.5f}")
    log.info(f"{'family base rate':22} {bb:>9.5f}")
    log.info(f"{'50/50 blend':22} {bl:>9.5f}")
    log.info(f"\nmean |base_rate - price| = {dev:.4f} "
          f"({dev*100:.1f} c/share, against a ~2 c round trip)")
    log.info(f"=> {'base rate WINS' if bb < bp else 'market price wins'}")

    con.execute("""
        CREATE OR REPLACE TEMP TABLE fam_stats AS
        SELECT family, n_family, base_rate,
               count(*) AS n_obs,
               avg(price) AS mean_price,
               avg(payout) AS realised,
               avg(base_rate - price) AS mean_gap,
               stddev_samp(base_rate - price) / sqrt(count(*)) AS gap_se
        FROM cmp GROUP BY 1, 2, 3
        HAVING count(*) >= 20
    """)

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (SELECT * FROM fam_stats ORDER BY abs(mean_gap) DESC)
        TO '{(dest / 'base_rate_families.parquet').as_posix()}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """)

    log.info("\nlargest base-rate deviations (families with >= 20 priced instances):")
    log.info(f"{'family':50} {'base':>6} {'price':>6} {'gap':>7} {'t':>6}")
    for famname, br, mp, gap, se in con.sql("""
        SELECT family, base_rate, mean_price, mean_gap, gap_se
        FROM fam_stats ORDER BY abs(mean_gap) DESC LIMIT 20
    """).fetchall():
        t = gap / se if se else float("nan")
        log.info(f"  {str(famname)[:48]:48} {br:>6.3f} {mp:>6.3f} {gap:>+7.4f} {t:>6.1f}")
    log.info(f"\nwrote {dest / 'base_rate_families.parquet'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
