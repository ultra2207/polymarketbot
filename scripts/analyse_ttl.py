"""Does the top decile's selection gap survive conditioning on entry timing?

The gap
-------
At the same price bucket the top decile's buys resolve better than everyone else's:
+0.117 in crypto, +0.078 in sports, +0.187 in economics, all in the 70-85 % band. Payout is
a property of the token, so at an equal price this can only mean they buy *different tokens*.
Category does not explain it.

The timing hypothesis
---------------------
If they buy closer to resolution, the same quoted price is a more determined outcome: a
crypto market at 0.77 with two hours left is nearly settled, while the same 0.77 with two
weeks left is a genuine forecast. If the top decile buys late and small wallets buy early,
the gap is a timing artefact, not skill.

Test: `ttl_h` (hours from first fill to the market's end date) is now on every position.
Compare the edge within the same price bucket AND the same time-to-resolution bucket. If the
gap collapses, timing explains it. If it persists, something else does.

Smoke test: `--limit 3000000`.
"""

from __future__ import annotations

import argparse

import duckdb

from polymarketbot.logs import get_logger

POSITIONS = "data/processed/positions/*.parquet"
TTL_LABELS = ["<1h", "1-6h", "6-24h", "1-3d", "3-7d", "7-30d", ">30d", "unknown"]


def ttl_bucket_expr(col: str = "ttl_h") -> str:
    return f"""CASE
        WHEN {col} IS NULL THEN 7
        WHEN {col} < 1    THEN 0
        WHEN {col} < 6    THEN 1
        WHEN {col} < 24   THEN 2
        WHEN {col} < 72   THEN 3
        WHEN {col} < 168  THEN 4
        WHEN {col} < 720  THEN 5
        ELSE 6 END"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    logger = get_logger("analyse_ttl")
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='10GB'")
    con.execute("SET temp_directory='data/tmp_duck'")

    src = (
        f"(SELECT * FROM read_parquet('{POSITIONS}') LIMIT {args.limit})"
        if args.limit
        else f"read_parquet('{POSITIONS}')"
    )

    logger.info("aggregating by ttl bucket, price bucket, group")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE agg AS
        SELECT {ttl_bucket_expr()} AS tb,
               least(19, greatest(0, floor(20 * bc / nullif(bs,0))))::INT AS pbucket,
               CASE WHEN dec = 1 THEN 1 ELSE 0 END AS top,
               count(*) AS n,
               sum(bs) AS bs, sum(bc) AS bc, sum(bs * payout) AS payout_bs
        FROM {src}
        WHERE payout IS NOT NULL AND bs > 0 AND dec IS NOT NULL
        GROUP BY 1, 2, 3
    """)

    print("\n=== where do the two groups sit in time-to-resolution? (share of bought shares) ===")
    print("  ttl bucket      top share%   rest share%")
    for r in con.execute("""
        SELECT tb,
               100.0 * sum(bs) FILTER (WHERE top=1) / sum(sum(bs) FILTER (WHERE top=1)) OVER (),
               100.0 * sum(bs) FILTER (WHERE top=0) / sum(sum(bs) FILTER (WHERE top=0)) OVER ()
        FROM agg GROUP BY tb ORDER BY tb
    """).fetchall():
        print(f"  {TTL_LABELS[r[0]]:<12} {r[1]:>11.1f}   {r[2]:>11.1f}")

    print("\n=== edge within each ttl bucket (all price buckets) ===")
    print("  ttl bucket    top edge | rest edge |     gap")
    for r in con.execute("""
        SELECT tb,
               (sum(payout_bs) FILTER (WHERE top=1) - sum(bc) FILTER (WHERE top=1))
                 / nullif(sum(bs) FILTER (WHERE top=1),0),
               (sum(payout_bs) FILTER (WHERE top=0) - sum(bc) FILTER (WHERE top=0))
                 / nullif(sum(bs) FILTER (WHERE top=0),0)
        FROM agg GROUP BY tb ORDER BY tb
    """).fetchall():
        if r[1] is None or r[2] is None:
            continue
        print(f"  {TTL_LABELS[r[0]]:<12} {r[1]:>+8.4f} | {r[2]:>+9.4f} | {r[1]-r[2]:>+8.4f}")

    print("\n=== the 70-85 % price band only, by ttl bucket ===")
    print("  ttl bucket    top edge | rest edge |     gap   top pos")
    for r in con.execute("""
        SELECT tb,
               (sum(payout_bs) FILTER (WHERE top=1) - sum(bc) FILTER (WHERE top=1))
                 / nullif(sum(bs) FILTER (WHERE top=1),0),
               (sum(payout_bs) FILTER (WHERE top=0) - sum(bc) FILTER (WHERE top=0))
                 / nullif(sum(bs) FILTER (WHERE top=0),0),
               sum(n) FILTER (WHERE top=1)
        FROM agg WHERE pbucket BETWEEN 14 AND 16 GROUP BY tb ORDER BY tb
    """).fetchall():
        if r[1] is None or r[2] is None:
            continue
        print(f"  {TTL_LABELS[r[0]]:<12} {r[1]:>+8.4f} | {r[2]:>+9.4f} | {r[1]-r[2]:>+8.4f}  {r[3]:>9,}")


if __name__ == "__main__":
    main()
