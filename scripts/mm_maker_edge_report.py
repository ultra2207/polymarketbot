"""Report register #11 by family x month from scripts/mm_maker_edge_family.py.

Reads data/processed/mm/agg_*.parquet and prints:
  1. family x month: fills, shares, edge c/fill, $ PnL per 1M shares
  2. pooled monthly series and crypto-15m monthly series
  3. flags: families >2x pooled, families with sign flips
"""
from __future__ import annotations

import duckdb

AGG = "data/processed/mm/agg_*.parquet"


def table(con, sql: str, title: str) -> None:
    rows = con.execute(sql).fetchall()
    cols = [d[0] for d in con.execute(sql).description]
    print(f"\n== {title}")
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join("" if v is None else
                         (f"{v:,.2f}" if isinstance(v, float) else str(v))
                         for v in r))


def main() -> None:
    con = duckdb.connect()
    con.execute("SET threads=6")
    con.execute("SET memory_limit='6GB'")
    con.execute("SET temp_directory='data/tmp_duck'")

    base = f"""
        SELECT category, month,
               sum(fills) AS fills,
               sum(shares) AS shares,
               sum(pnl_cs) / sum(shares) AS edge_c,
               sum(pnl_cs) / sum(shares) * 1e4 AS pnl_per_1m
        FROM '{AGG}'
        GROUP BY 1, 2
    """

    table(con, f"SELECT * FROM ({base}) ORDER BY category, month",
          "family x month")
    table(con, f"""
        SELECT month, sum(fills) AS fills, sum(shares) AS shares,
               sum(pnl_cs)/sum(shares) AS edge_c,
               sum(pnl_cs)/sum(shares)*1e4 AS pnl_per_1m
        FROM '{AGG}' GROUP BY 1 ORDER BY 1
    """, "pooled by month")
    table(con, f"""
        SELECT side, month, sum(fills) AS fills, sum(shares) AS shares,
               sum(pnl_cs)/sum(shares) AS edge_c
        FROM '{AGG}' GROUP BY 1,2 ORDER BY 1,2
    """, "by side")
    table(con, f"""
        SELECT price_bucket, sum(fills) AS fills, sum(shares) AS shares,
               sum(pnl_cs)/sum(shares) AS edge_c
        FROM '{AGG}' GROUP BY 1 ORDER BY 1
    """, "by price bucket (pooled months)")


if __name__ == "__main__":
    main()
