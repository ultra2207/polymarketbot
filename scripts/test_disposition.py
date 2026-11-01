"""Do they exit winners and hold losers?  (disposition effect vs fair-value exit)

The question
------------
The top decile earns +$192 m on round trips and loses -$141 m on what it carries to
resolution. Two explanations fit that aggregate:

  (a) DISPOSITION: they exit a position when it is in profit and hold it when it is in
      loss, so every exit books a winner and every unresolved position is a loser.
  (b) FAIR VALUE: their exits are timed to the price reaching their fair value; the
      residue that reaches resolution is the part where they were wrong.

Aggregates cannot separate them. Within-position data can: for each position that is
partially exited, compare the PnL of the sold shares against the PnL of the held shares.

    closed_pnl = sells        * (sell_vwap - buy_vwap)     money actually booked
    open_pnl   = (buys-sells) * (payout     - buy_vwap)    money the residue made

If (a), `closed_pnl > 0` and `open_pnl < 0` should dominate. If (b), the two should be
independent.

Also measured: P(exit in profit) = share of exits where sell_vwap > buy_vwap, against
P(held position finishes in profit) = share of held positions where payout > buy_vwap.

Optimisation (AGENTS.md): deciles are computed on wallets (~470 k rows) before joining;
the top decile is filtered into its own table; only integer buckets are grouped on.
`--cache` lets this run against a single month for the 60-second smoke test.
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

    logger = get_logger("test_disposition")
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='10GB'")
    con.execute("SET temp_directory='data/tmp_duck'")

    logger.info("stage 1: positions")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pr AS
        SELECT wallet, asset_id,
               sum(coalesce(m_buy_sh,0)  + coalesce(t_buy_sh,0))  AS bs,
               sum(coalesce(m_sell_sh,0) + coalesce(t_sell_sh,0)) AS ss,
               sum(coalesce(m_buy_cash,0)  + coalesce(t_buy_cash,0))  AS bc,
               sum(coalesce(m_sell_cash,0) + coalesce(t_sell_cash,0)) AS sc,
               sum(coalesce(m_buy_cash,0)  + coalesce(t_buy_cash,0)
                 + coalesce(m_sell_cash,0) + coalesce(t_sell_cash,0)) AS vol
        FROM read_parquet('{args.cache}')
        GROUP BY 1, 2
    """)
    logger.info(f"  positions: {con.sql('SELECT count(*) FROM pr').fetchall()[0][0]:,}")

    logger.info("stage 2: deciles on wallets (small table), then top decile only")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE rw AS
        SELECT wallet, ntile(10) OVER (ORDER BY vol DESC) AS dec
        FROM (SELECT wallet, sum(vol) AS vol FROM pr GROUP BY 1)
        WHERE vol >= 1000
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE top_w AS SELECT wallet FROM rw WHERE dec = 1
    """)
    logger.info(f"  top-decile wallets: {con.sql('SELECT count(*) FROM top_w').fetchall()[0][0]:,}")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE payout AS
        SELECT token1 AS asset_id, payout_0 AS payout FROM read_parquet('{LABELS}')
        WHERE resolved AND NOT coalesce(is_void,false) AND token1 IS NOT NULL
        UNION ALL
        SELECT token2, payout_1 FROM read_parquet('{LABELS}')
        WHERE resolved AND NOT coalesce(is_void,false) AND token2 IS NOT NULL
    """)

    logger.info("stage 3: within-position split, top decile")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE pos AS
        SELECT p.asset_id, pr.bs, pr.ss, pr.bc / nullif(pr.bs,0) AS vbuy,
               pr.sc / nullif(pr.ss,0) AS vsell, p.payout
        FROM pr JOIN top_w ON top_w.wallet = pr.wallet
        JOIN payout p ON p.asset_id = pr.asset_id
        WHERE pr.bs > pr.ss AND pr.ss > 0          -- net long, partially exited
    """)
    n = con.sql("SELECT count(*) FROM pos").fetchall()[0][0]
    logger.info(f"  partially-exited net-long positions: {n:,}")
    if n == 0:
        logger.warning("no qualifying positions")
        return

    print("\n=== within a partially exited position: is the sold part the winner? ===")
    print("  closed_pnl / open_pnl      positions     closed $m     open $m")
    for r in con.sql("""
        SELECT CASE WHEN closed > 0 AND open <= 0 THEN 'sold win, held loss'
                    WHEN closed > 0 AND open > 0  THEN 'sold win, held win'
                    WHEN closed <= 0 AND open <= 0 THEN 'sold loss, held loss'
                    ELSE 'sold loss, held win' END AS kind,
               count(*), sum(closed)/1e6, sum(open)/1e6
        FROM (
            SELECT ss * (vsell - vbuy)             AS closed,
                   (bs - ss) * (payout - vbuy)     AS open
            FROM pos
        ) GROUP BY kind ORDER BY count(*) DESC
    """).fetchall():
        print(f"  {r[0]:<24} {r[1]:>9,}   {r[2]:>+10.2f}   {r[3]:>+10.2f}")

    print("\n=== exit timing: do they sell into profit? ===")
    r = con.sql("""
        SELECT 100.0 * count(*) FILTER (WHERE vsell > vbuy) / count(*) AS exit_in_profit_pct,
               100.0 * count(*) FILTER (WHERE payout > vbuy)  / count(*) AS would_hold_be_profit_pct,
               avg(vsell - vbuy) AS mean_exit_gain,
               avg(payout - vbuy) AS mean_hold_gain
        FROM pos
    """).fetchall()[0]
    print(f"  exits taken at sell>buy:        {r[0]:>6.1f} %")
    print(f"  same positions would have won:  {r[1]:>6.1f} % (if held to resolution)")
    print(f"  mean gain at exit:              {r[2]:>+8.4f} per share")
    print(f"  mean gain if held:              {r[3]:>+8.4f} per share")

    print("\n=== the pure holding set: bought, never sold ===")
    r = con.sql("""
        SELECT count(*),
               100.0 * count(*) FILTER (WHERE p.payout > pr.bc/nullif(pr.bs,0)) / count(*),
               sum(pr.bs * (p.payout - pr.bc/nullif(pr.bs,0)))/1e6
        FROM pr JOIN top_w ON top_w.wallet = pr.wallet
        JOIN payout p ON p.asset_id = pr.asset_id
        WHERE pr.ss = 0 AND pr.bs > 0
    """).fetchall()[0]
    print(f"  positions held to resolution: {r[0]:,}")
    print(f"  of those, profitable:         {r[1]:.1f} %")
    print(f"  total PnL on them:            ${r[2]:+,.2f} m")

    print("\n=== never-sold positions: size distribution (dust or conviction?) ===")
    print("  notional          positions    avg $      total PnL $m")
    labels = ["<$1", "$1-10", "$10-100", "$100-1k", "$1k-10k",
              "$10k-100k", "$100k-1M", "$1M-10M", "$10M+"]
    for r in con.sql("""
        SELECT b, count(*), avg(n), sum(pnl)/1e6
        FROM (
            SELECT pr.bs * (pr.bc / nullif(pr.bs,0)) AS n,
                   pr.bs * (p.payout - pr.bc / nullif(pr.bs,0)) AS pnl,
                   CASE WHEN n < 1 THEN 0 WHEN n < 10 THEN 1 WHEN n < 100 THEN 2
                        WHEN n < 1000 THEN 3 WHEN n < 10000 THEN 4 WHEN n < 100000 THEN 5
                        WHEN n < 1000000 THEN 6 WHEN n < 10000000 THEN 7 ELSE 8 END AS b
            FROM pr JOIN top_w ON top_w.wallet = pr.wallet
            JOIN payout p ON p.asset_id = pr.asset_id
            WHERE pr.ss = 0 AND pr.bs > 0
        ) GROUP BY b ORDER BY b
    """).fetchall():
        print(f"  {labels[r[0]]:<12} {r[1]:>12,}   {r[2]:>8.2f}   {r[3]:>+12.2f}")


if __name__ == "__main__":
    main()
