"""Test the EV-trader thesis: is the top decile's PnL entry edge or exit edge?

The decomposition is exact, not a model. For any wallet and token:

    profit     = (sell_cash - buy_cash) + payout * (buy_sh - sell_sh)

and, writing buy_cash = b * vwap_buy and sell_cash = s * vwap_sell, the same quantity is

    profit     = ENTRY_EDGE + EXIT_EDGE
    ENTRY_EDGE = b * (payout - vwap_buy)      -- bought below realised value
    EXIT_EDGE  = s * (vwap_sell - payout)     -- sold above realised value

A market maker quoting symmetrically around fair value produces ENTRY_EDGE ≈ EXIT_EDGE.
An EV trader who buys a mispricing and later flattens produces ENTRY_EDGE > EXIT_EDGE, and
their realised spread (exit price minus entry price) can even look negative.

Also measured:
  carry    = |net position| / volume      -- do they hold to resolution?
  dispersion = std of per-token profit     -- conviction bets vs many small wins
  win rate  = share of tokens with profit > 0

Reads the role-split cache produced by build_wallet_token.py.
"""

from __future__ import annotations

import duckdb

from polymarketbot.logs import get_logger
log = get_logger(__name__)

CACHE = "data/processed/wallet_token/*.parquet"
LABELS = "data/processed/market_labels.parquet"


def main() -> None:
    logger = get_logger("test_ev_thesis")
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='9GB'")
    con.execute("SET temp_directory='data/tmp_duck'")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE payout AS
        SELECT token1 AS asset_id, payout_0 AS payout FROM read_parquet('{LABELS}')
        WHERE resolved AND NOT coalesce(is_void,false) AND token1 IS NOT NULL
        UNION ALL
        SELECT token2, payout_1 FROM read_parquet('{LABELS}')
        WHERE resolved AND NOT coalesce(is_void,false) AND token2 IS NOT NULL
    """)

    logger.info("building (wallet, token) pairs with payout")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE q AS
        WITH p AS (
            SELECT wallet, asset_id,
                   sum(coalesce(m_buy_sh,0)   + coalesce(t_buy_sh,0))   AS bs,
                   sum(coalesce(m_buy_cash,0) + coalesce(t_buy_cash,0)) AS bc,
                   sum(coalesce(m_sell_sh,0)  + coalesce(t_sell_sh,0))  AS ss,
                   sum(coalesce(m_sell_cash,0)+ coalesce(t_sell_cash,0)) AS sc
            FROM read_parquet('{CACHE}')
            GROUP BY 1, 2
        )
        SELECT p.wallet, p.asset_id, p.bs, p.bc, p.ss, p.sc, po.payout,
               CASE WHEN p.bs>0 THEN p.bc/p.bs ELSE 0 END AS vbuy,
               CASE WHEN p.ss>0 THEN p.sc/p.ss ELSE 0 END AS vsell
        FROM p JOIN payout po ON po.asset_id = p.asset_id
    """)
    logger.info(f"pairs: {con.sql('SELECT count(*) FROM q').fetchall()[0][0]:,}")

    con.execute("""
        CREATE OR REPLACE TEMP TABLE pairp AS
        SELECT wallet, asset_id, bs, bc, ss, sc, payout,
               bs * (payout - vbuy)  AS entry_edge,
               ss * (vsell - payout) AS exit_edge,
               bs * (payout - vbuy) + ss * (vsell - payout) AS profit,
               abs(bs - ss) AS abs_net,
               bs + ss AS pair_volume
        FROM q
    """)

    con.execute("""
        CREATE OR REPLACE TEMP TABLE wallet AS
        SELECT wallet,
               sum(entry_edge) AS entry_edge,
               sum(exit_edge)  AS exit_edge,
               sum(profit)     AS profit,
               sum(bc + sc)    AS volume,
               sum(abs_net)    AS abs_net,
               sum(bs + ss)    AS shares,
               count(*)        AS tokens,
               stddev_samp(profit) AS sd_profit,
               sum(profit) FILTER (WHERE profit > 0) AS gross_win,
               -sum(profit) FILTER (WHERE profit <= 0) AS gross_loss,
               count(*) FILTER (WHERE profit > 0) AS win_tokens
        FROM pairp GROUP BY 1
    """)

    log.info("\n=== identity check: entry + exit must equal profit exactly ===")
    r = con.sql("""
        SELECT sum(entry_edge), sum(exit_edge), sum(profit),
               sum(entry_edge) + sum(exit_edge) - sum(profit)
        FROM wallet
    """).fetchall()[0]
    log.info(f"  entry {r[0]:+,.0f}  exit {r[1]:+,.0f}  profit {r[2]:+,.0f}  residual {r[3]:+.6f}")

    log.info("\n=== by volume decile ===")
    log.info("  dec  wallets   volume$m   entry/$   exit/$   entry share   carry   win%   pnl/token sd")
    for r in con.sql("""
        SELECT dec, count(*), sum(volume)/1e6,
               sum(entry_edge)/nullif(sum(volume),0),
               sum(exit_edge)/nullif(sum(volume),0),
               sum(entry_edge)/nullif(sum(entry_edge)+sum(exit_edge),0),
               sum(abs_net)/nullif(sum(shares),0),
               100.0*sum(win_tokens)/nullif(sum(tokens),0),
               sum(profit)/nullif(sum(tokens),0)
        FROM (
            SELECT *, ntile(10) OVER (ORDER BY volume DESC) AS dec
            FROM wallet WHERE volume >= 1000
        ) GROUP BY dec ORDER BY dec
    """).fetchall():
        log.info(f"  {r[0]:>3}  {r[1]:>7,}  {r[2]:>9,.0f}  {r[3]:>+8.5f}  {r[4]:>+8.5f}  "
              f"{r[5]:>11.3f}  {r[6]:>6.3f}  {r[7]:>5.1f}  {r[8]:>+10.4f}")

    log.info("\n=== dispersion: top decile vs the rest ===")
    for label, where in (("top decile", "vdec = 1"), ("deciles 2-10", "vdec > 1")):
        r = con.sql(f"""
            SELECT count(*), sum(profit), sum(volume),
                   avg(sd_profit) / nullif(avg(volume/tokens),0) AS sd_per_dollar_of_token,
                   sum(gross_win)/nullif(sum(gross_loss),0) AS win_loss_ratio,
                   100.0*sum(win_tokens)/sum(tokens) AS win_pct
            FROM (SELECT *, ntile(10) OVER (ORDER BY volume DESC) AS vdec FROM wallet WHERE volume >= 1000)
            WHERE {where}
        """).fetchall()[0]
        log.info(f"  {label:12} wallets {r[0]:>7,}  profit ${r[1]/1e6:>+8.2f} m  "
              f"win/loss {r[4]:>5.2f}  token win% {r[5]:>4.1f}")

    log.info("\n=== the 12 largest wallets: entry vs exit ===")
    log.info("  entry$m   exit$m   profit$m  entry share  carry   tokens")
    for r in con.sql("""
        SELECT entry_edge/1e6, exit_edge/1e6, profit/1e6,
               entry_edge/nullif(entry_edge+exit_edge,0),
               abs_net/nullif(shares,0), tokens
        FROM wallet WHERE volume >= 1000 ORDER BY volume DESC LIMIT 12
    """).fetchall():
        log.info(f"  {r[0]:>+7.2f}  {r[1]:>+7.2f}  {r[2]:>+8.2f}  {r[3]:>11.3f}  {r[4]:>6.3f}  {r[5]:>7,}")


if __name__ == "__main__":
    main()
