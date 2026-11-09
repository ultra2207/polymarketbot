"""Measure the distribution of Polymarket fill sizes (USD per fill).

Reads a slice of the moose-code on-chain archive directly over HTTPS with DuckDB,
so nothing large is downloaded. Used to ground the "target notional per trade"
decision in real data rather than a guess.

Usage:
    uv run scripts/measure_fill_sizes.py [year] [month] [limit]
"""

from __future__ import annotations

import sys

import duckdb
from polymarketbot.logs import get_logger
log = get_logger(__name__)

BASE = (
    "https://huggingface.co/datasets/moose-code/polymarket-onchain-v1"
    "/resolve/main/order_filled/year={year}/month={month:02d}.parquet"
)

# The CLOB cash leg has assetId '0'. One side of every fill is cash, the other is
# the outcome token. USDC and outcome tokens both use 6 decimals.
USD_EXPR = """
CAST(
  CASE WHEN makerAssetId = '0' THEN makerAmountFilled ELSE takerAmountFilled END
  AS HUGEINT
) / 1e6
"""


def main() -> None:
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    month = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 1_000_000

    url = BASE.format(year=year, month=month)
    con = duckdb.connect()
    con.execute("SET threads = 4")

    sample = f"""
        SELECT {USD_EXPR} AS usd
        FROM read_parquet('{url}')
        WHERE (makerAssetId = '0' OR takerAssetId = '0')
        LIMIT {limit}
    """

    stats = con.sql(f"""
        WITH s AS ({sample})
        SELECT
            count(*)                                        AS n,
            min(usd)                                        AS min_usd,
            quantile_cont(usd, 0.10)                        AS p10,
            quantile_cont(usd, 0.25)                        AS p25,
            quantile_cont(usd, 0.50)                        AS p50,
            quantile_cont(usd, 0.75)                        AS p75,
            quantile_cont(usd, 0.90)                        AS p90,
            quantile_cont(usd, 0.99)                        AS p99,
            quantile_cont(usd, 0.999)                       AS p999,
            max(usd)                                        AS max_usd,
            avg(usd)                                        AS mean_usd
        FROM s
    """).fetchall()[0]

    cols = [
        "n", "min", "p10", "p25", "p50", "p75", "p90", "p99", "p999", "max", "mean",
    ]

    log.info(f"\nfill size distribution - {year}-{month:02d}, first {limit:,} cash fills\n")
    log.info(f"{'':6}{'USD':>14}")
    for name, value in zip(cols, stats):
        if name == "n":
            log.info(f"{'n':6}{int(value):>14,}")
        else:
            log.info(f"{name:6}{value:>14,.2f}")

    log.info("\nshare of fills at or below:")
    shares = con.sql(f"""
        WITH s AS ({sample})
        SELECT
            count(*) FILTER (WHERE usd <= 10)   * 100.0 / count(*) AS le_10,
            count(*) FILTER (WHERE usd <= 50)   * 100.0 / count(*) AS le_50,
            count(*) FILTER (WHERE usd <= 100)  * 100.0 / count(*) AS le_100,
            count(*) FILTER (WHERE usd <= 500)  * 100.0 / count(*) AS le_500,
            count(*) FILTER (WHERE usd <= 1000) * 100.0 / count(*) AS le_1000,
            count(*) FILTER (WHERE usd <= 5000) * 100.0 / count(*) AS le_5000
        FROM s
    """).fetchall()[0]

    for threshold, pct in zip([10, 50, 100, 500, 1000, 5000], shares):
        log.info(f"  ${threshold:>6,}  {pct:6.2f}%")


if __name__ == "__main__":
    main()
