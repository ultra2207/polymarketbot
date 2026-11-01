"""The decisive test: at the same price, do the top decile pick better outcomes?

Why this is the right question
------------------------------
Everything so far says the top decile wins the same fraction of tokens as everyone else
(54.0 % vs 53.9 %) and does not hold losers. So their advantage is either

  (a) EXECUTION - they buy at better prices (maker side, spread capture), or
  (b) SELECTION - at the same price they pick tokens that resolve better.

Aggregate PnL cannot separate these because price and selection are confounded: buying
cheaper automatically earns more. Bucketing by the price actually paid removes the price
difference, leaving only selection.

    within a price bucket, compare E[payout] for the top decile against deciles 2-10.

If the top decile's realised payout rate exceeds the others' inside the same bucket, they
are selecting mispriced tokens - a model exists to be found. If it is flat, their edge is
entirely execution and there is no EV model to reverse-engineer.

The payout is the realised outcome (0 or 1), so this is a calibration test, not a mark.

Optimisation (AGENTS.md): deciles on wallets (~470 k rows) before joining positions;
integer buckets; projection to the four columns used; `--cache` for the 60-second smoke test.
"""

from __future__ import annotations

import argparse

import duckdb

from polymarketbot.logs import get_logger

CACHE = "data/processed/wallet_token/*.parquet"
LABELS = "data/processed/market_labels.parquet"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=CACHE, help="parquet glob; one month for a smoke test")
    args = ap.parse_args()

    logger = get_logger("test_selection")
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='10GB'")
    con.execute("SET temp_directory='data/tmp_duck'")

    logger.info("stage 1: positions with average buy price")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pr AS
        SELECT wallet, asset_id,
               sum(coalesce(m_buy_sh,0)  + coalesce(t_buy_sh,0))  AS bs,
               sum(coalesce(m_buy_cash,0) + coalesce(t_buy_cash,0)) AS bc,
               sum(coalesce(m_buy_cash,0) + coalesce(t_buy_cash,0)
                 + coalesce(m_sell_cash,0) + coalesce(t_sell_cash,0)) AS vol
        FROM read_parquet('{args.cache}')
        GROUP BY 1, 2
    """)
    logger.info(f"  positions: {con.sql('SELECT count(*) FROM pr').fetchall()[0][0]:,}")

    con.execute("""
        CREATE OR REPLACE TEMP TABLE rw AS
        SELECT wallet, ntile(10) OVER (ORDER BY vol DESC) AS dec
        FROM (SELECT wallet, sum(vol) AS vol FROM pr GROUP BY 1)
        WHERE vol >= 1000
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE payout AS
        SELECT token1 AS asset_id, payout_0 AS payout FROM read_parquet('{LABELS}')
        WHERE resolved AND NOT coalesce(is_void,false) AND token1 IS NOT NULL
        UNION ALL
        SELECT token2, payout_1 FROM read_parquet('{LABELS}')
        WHERE resolved AND NOT coalesce(is_void,false) AND token2 IS NOT NULL
    """)

    logger.info("stage 2: buys with a known payout, bucketed by price paid")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE b AS
        SELECT rw.dec,
               least(19, greatest(0, floor(20 * pr.bc / nullif(pr.bs,0))))::INT AS bucket,
               pr.bs, pr.bc, pr.bc / nullif(pr.bs,0) AS vbuy, p.payout
        FROM pr
        JOIN rw   ON rw.wallet = pr.wallet
        JOIN payout p ON p.asset_id = pr.asset_id
        WHERE pr.bs > 0
    """)
    logger.info(f"  buys in scope: {con.sql('SELECT count(*) FROM b').fetchall()[0][0]:,}")

    print("\n=== at the same price, does the top decile pick better outcomes? ===")
    print("  price bucket   top: payout  entry   edge  |  rest: payout  entry   edge  |  gap")
    for r in con.sql("""
        SELECT bucket,
               sum(payout * bs) FILTER (WHERE dec = 1)  / nullif(sum(bs) FILTER (WHERE dec = 1),0),
               sum(bc)          FILTER (WHERE dec = 1)  / nullif(sum(bs) FILTER (WHERE dec = 1),0),
               (sum(payout * bs) FILTER (WHERE dec = 1) - sum(bc) FILTER (WHERE dec = 1))
                 / nullif(sum(bs) FILTER (WHERE dec = 1),0),
               sum(payout * bs) FILTER (WHERE dec > 1)  / nullif(sum(bs) FILTER (WHERE dec > 1),0),
               sum(bc)          FILTER (WHERE dec > 1)  / nullif(sum(bs) FILTER (WHERE dec > 1),0),
               (sum(payout * bs) FILTER (WHERE dec > 1) - sum(bc) FILTER (WHERE dec > 1))
                 / nullif(sum(bs) FILTER (WHERE dec > 1),0)
        FROM b GROUP BY bucket ORDER BY bucket
    """).fetchall():
        gap = (r[1] - r[4]) if r[1] is not None and r[4] is not None else None
        if gap is None:
            print(f"  {r[0]*5:>3}-{r[0]*5+5:<3}%   insufficient")
            continue
        print(f"  {r[0]*5:>3}-{r[0]*5+5:<3}%   "
              f"{r[1]:>6.3f}  {r[2]:>6.3f}  {r[3]:>+6.3f}  |  "
              f"{r[4]:>6.3f}  {r[5]:>6.3f}  {r[6]:>+6.3f}  |  {gap:>+6.3f}")

    print("\n=== summary: edge per share, decomposed ===")
    r = con.sql("""
        SELECT
          (sum(payout * bs) FILTER (WHERE dec = 1) - sum(bc) FILTER (WHERE dec = 1))
            / nullif(sum(bs) FILTER (WHERE dec = 1),0) AS top_total,
          (sum(payout * bs) FILTER (WHERE dec > 1) - sum(bc) FILTER (WHERE dec > 1))
            / nullif(sum(bs) FILTER (WHERE dec > 1),0) AS rest_total,
          count(*) FILTER (WHERE dec = 1) AS top_positions,
          count(*) FILTER (WHERE dec > 1) AS rest_positions
        FROM b
    """).fetchall()[0]
    print(f"  total edge per share bought: top {r[0]:+.4f}, rest {r[1]:+.4f}")
    print(f"  positions: top {r[2]:,}, rest {r[3]:,}")


if __name__ == "__main__":
    main()
