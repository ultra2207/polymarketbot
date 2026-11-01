"""Three questions about who earns on Polymarket, answered from the materialised cache.

T1  Does PnL come from providing liquidity (maker) or consuming it (taker)?
T2  Which tokens have volume but no top-maker presence - i.e. where is the whitespace?
T3  Do winners earn in the price tails or the middle?

Everything reads `data/processed/wallet_token/*.parquet` (build_wallet_token.py), so it runs
in minutes rather than rescanning 45 GB.

The PnL decomposition is the one verified in `round_trip_pnl.py`: realised spread captured on
the matched quantity plus the settlement of whatever is left, marked at the realised payout.
It satisfies conservation (aggregate PnL is zero), which is the only check that matters.
"""

from __future__ import annotations

import duckdb

from polymarketbot.logs import get_logger

CACHE = "data/processed/wallet_token/*.parquet"
LABELS = "data/processed/market_labels.parquet"


def main() -> None:
    logger = get_logger("analyse_roles")
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

    logger.info("collapsing months to (wallet, token) pairs")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE pairs AS
        SELECT wallet, asset_id,
               sum(coalesce(m_buy_sh,0)  + coalesce(t_buy_sh,0))  AS bs,
               sum(coalesce(m_buy_cash,0)+ coalesce(t_buy_cash,0)) AS bc,
               sum(coalesce(m_sell_sh,0) + coalesce(t_sell_sh,0)) AS ss,
               sum(coalesce(m_sell_cash,0)+coalesce(t_sell_cash,0)) AS sc,
               sum(coalesce(m_buy_cash,0) + coalesce(m_sell_cash,0)) AS maker_cash,
               sum(coalesce(t_buy_cash,0) + coalesce(t_sell_cash,0)) AS taker_cash,
               sum(coalesce(n_maker,0)) AS n_maker,
               sum(coalesce(n_taker,0)) AS n_taker
        FROM read_parquet('{CACHE}')
        GROUP BY 1, 2
    """)
    n_pairs = con.sql("SELECT count(*) FROM pairs").fetchall()[0][0]
    logger.info(f"{n_pairs:,} (wallet, token) pairs")

    con.execute("""
        CREATE OR REPLACE TEMP TABLE wallet AS
        SELECT wallet,
               sum(least(bs,ss) * ((CASE WHEN ss>0 THEN sc/ss ELSE 0 END)
                                 - (CASE WHEN bs>0 THEN bc/bs ELSE 0 END))) AS realised,
               sum(CASE WHEN bs >= ss
                        THEN (bs-ss) * (p.payout - (CASE WHEN bs>0 THEN bc/bs ELSE 0 END))
                        ELSE (ss-bs) * ((CASE WHEN ss>0 THEN sc/ss ELSE 0 END) - p.payout)
                   END) AS settled,
               sum(bc+sc) AS volume,
               sum(maker_cash) AS maker_cash,
               sum(taker_cash) AS taker_cash,
               sum(n_maker) AS n_maker,
               sum(n_taker) AS n_taker,
               count(*) AS tokens,
               sum(bc) / nullif(sum(bs),0) AS avg_buy_px
        FROM pairs JOIN payout p ON p.asset_id = pairs.asset_id
        GROUP BY 1
    """)

    # ---------------------------------------------------------------- T1
    print("\n=== T1  maker share of volume vs PnL ===")
    print("  Does the money come from supplying liquidity or consuming it?")
    print("\n  maker-share   wallets   volume $m   pnl $m     pnl/$    pos%    med_tokens")
    for r in con.sql("""
        SELECT maker_share_band, count(*), sum(volume)/1e6,
               sum(coalesce(realised,0)+coalesce(settled,0))/1e6,
               sum(coalesce(realised,0)+coalesce(settled,0))/nullif(sum(volume),0),
               100.0*count(*) FILTER (WHERE coalesce(realised,0)+coalesce(settled,0)>0)/count(*),
               median(tokens)
        FROM (
            SELECT *, width_bucket(coalesce(maker_cash/nullif(maker_cash+taker_cash,0),0), 0, 1, 10) AS b,
                   b*10||'-'||(b+1)*10||'%' AS maker_share_band
            FROM wallet WHERE maker_cash+taker_cash >= 1000
        ) GROUP BY maker_share_band
        ORDER BY min(b)
    """).fetchall():
        print(f"  {r[0]:>11}  {r[1]:>8,}  {r[2]:>10,.0f}  {r[3]:>+9.2f}  "
              f"{r[4]:>+8.5f}  {r[5]:>5.1f}  {r[6]:>10,.0f}")

    print("\n  the same, only wallets in the top volume decile:")
    print("\n  maker-share   wallets   volume $m   pnl $m     pnl/$    pos%")
    for r in con.sql("""
        SELECT maker_share_band, count(*), sum(volume)/1e6,
               sum(coalesce(realised,0)+coalesce(settled,0))/1e6,
               sum(coalesce(realised,0)+coalesce(settled,0))/nullif(sum(volume),0),
               100.0*count(*) FILTER (WHERE coalesce(realised,0)+coalesce(settled,0)>0)/count(*)
        FROM (
            SELECT *, width_bucket(coalesce(maker_cash/nullif(maker_cash+taker_cash,0),0), 0, 1, 10) AS b,
                   b*10||'-'||(b+1)*10||'%' AS maker_share_band,
                   ntile(10) OVER (ORDER BY volume DESC) AS vdec
            FROM wallet WHERE maker_cash+taker_cash >= 1000
        ) WHERE vdec = 1 GROUP BY maker_share_band ORDER BY min(b)
    """).fetchall():
        print(f"  {r[0]:>11}  {r[1]:>8,}  {r[2]:>10,.0f}  {r[3]:>+9.2f}  {r[4]:>+8.5f}  {r[5]:>5.1f}")

    # ---------------------------------------------------------------- T3
    print("\n=== T3  price band vs PnL ===")
    print("  avg buy price of the wallet, bucketed; does the edge live in the tails?")
    print("\n  price band   wallets   volume $m   pnl $m     pnl/$")
    for r in con.sql("""
        SELECT band, count(*), sum(volume)/1e6,
               sum(coalesce(realised,0)+coalesce(settled,0))/1e6,
               sum(coalesce(realised,0)+coalesce(settled,0))/nullif(sum(volume),0)
        FROM (
            SELECT *, width_bucket(least(greatest(coalesce(avg_buy_px,0.5),0),1), 0, 1, 10) AS b,
                   b*10||'-'||(b+1)*10||'c' AS band
            FROM wallet WHERE maker_cash+taker_cash >= 10000
        ) GROUP BY band ORDER BY min(b)
    """).fetchall():
        print(f"  {r[0]:>10}  {r[1]:>8,}  {r[2]:>10,.0f}  {r[3]:>+9.2f}  {r[4]:>+8.5f}")

    # ---------------------------------------------------------------- T2
    print("\n=== T2  coverage: tokens with volume but no top-decile maker ===")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE top_wallets AS
        SELECT wallet FROM (
            SELECT wallet, sum(maker_cash+taker_cash) v FROM wallet
            WHERE maker_cash+taker_cash >= 1000 ORDER BY v DESC LIMIT 500
        )
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE token_cov AS
        SELECT p.asset_id,
               sum(coalesce(maker_cash,0)) AS token_maker_cash,
               sum(coalesce(maker_cash,0)+coalesce(taker_cash,0)) AS token_volume,
               count(*) AS wallets,
               count(*) FILTER (WHERE tw.wallet IS NOT NULL) AS top_wallets,
               max(coalesce(n_maker,0)) AS max_maker_fills
        FROM pairs p
        LEFT JOIN top_wallets tw ON tw.wallet = p.wallet
        GROUP BY 1
    """)
    r = con.sql("""
        SELECT count(*) n,
               count(*) FILTER (WHERE top_wallets = 0) untouchable,
               100.0*count(*) FILTER (WHERE top_wallets = 0)/count(*) pct_n,
               sum(token_volume) FILTER (WHERE top_wallets = 0)/1e6 vol_untouchable,
               sum(token_volume)/1e6 vol_all,
               median(token_volume) med_token_vol
        FROM token_cov
    """).fetchall()[0]
    print(f"  tokens traded: {r[0]:,}")
    print(f"  with NO top-500 wallet present: {r[1]:,} ({r[2]:.1f} %)")
    print(f"  volume in those tokens: ${r[3]:,.0f} m of ${r[4]:,.0f} m "
          f"({100*r[3]/max(r[4],1e-9):.2f} %)")
    print(f"  median token volume: ${r[5]:,.0f}")

    print("\n  volume distribution of tokens by number of top-500 wallets present:")
    print("\n  top-500 present   tokens     volume $m    median $")
    for r in con.sql("""
        SELECT least(top_wallets,5) tw, count(*), sum(token_volume)/1e6, median(token_volume)
        FROM token_cov GROUP BY 1 ORDER BY 1
    """).fetchall():
        print(f"  {r[0]:>15}   {r[1]:>7,}   {r[2]:>9,.0f}   {r[3]:>9,.0f}")


if __name__ == "__main__":
    main()
