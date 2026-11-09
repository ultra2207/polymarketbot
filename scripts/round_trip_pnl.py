"""Round-trip maker PnL - does the biggest maker actually earn, or is it paid?

Why this exists
---------------
`analyse_fill_quality.py` marked each maker fill at the token's realised payout. That
metric can only see the inventory a maker carries to resolution: a maker who buys and
sells the same token flattens to zero exposure whatever it earned on the round trip. It
therefore cannot separate "skilled" from "paid" - it establishes only that the largest
makers are not left holding the wrong side.

This script measures the round trip instead. For every (maker, token) pair it computes
the matched quantity and the excess:

    vwap_buy  = buy_notional  / buy_shares
    vwap_sell = sell_notional / sell_shares
    matched   = min(buy_shares, sell_shares)

    realised  = matched * (vwap_sell - vwap_buy)
    residual  = buy_shares - sell_shares          (+ left long, - left short)
    settled   = residual > 0  ->  residual  * (payout - vwap_buy)
                residual < 0  -> (-residual) * (vwap_sell - payout)

    pnl       = realised + settled

This is an average-cost (not FIFO) decomposition. Within a (maker, token) pair average
cost is defensible because a market maker trades many times at similar prices; FIFO would
reorder fills that are economically simultaneous. It is stated in the output so the
result is not over-read.

Output: PnL by volume decile, and the share of makers who are positive. The comparison
that matters is PnL per dollar against the rebate rate - a maker whose trading PnL is
negative but small is being paid; one whose PnL covers its own costs is earning.

Usage
-----
    uv run scripts/round_trip_pnl.py --start 2026-06-01 --end 2026-07-01
    uv run scripts/round_trip_pnl.py --start 2026-04-01 --end 2026-10-01 --min-volume 10000
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from polymarketbot.logs import get_logger
log = get_logger(__name__)

TRADES = Path("data/raw/sii/trades.parquet")
LABELS = Path("data/processed/market_labels.parquet")


def month_starts(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    out, cur = [], start
    while cur < end:
        nxt = (
            datetime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1, tzinfo=timezone.utc)
            if cur.month == 12
            else datetime(cur.year, cur.month + 1, 1, tzinfo=timezone.utc)
        )
        out.append((cur, min(nxt, end)))
        cur = nxt
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--min-volume", type=float, default=1000.0,
                    help="keep makers with at least this much maker volume ($)")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--cache", default="data/processed/wallet_token",
                    help="pre-aggregated wallet/token/month parquet dir (build_wallet_token.py)")
    args = ap.parse_args()

    logger = get_logger("round_trip_pnl")
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    con = duckdb.connect()
    con.execute(f"SET threads={args.threads}")
    con.execute("SET memory_limit='6GB'")
    con.execute("SET temp_directory='data/tmp_duck'")
    con.execute("SET preserve_insertion_order=false")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE payout AS
        SELECT token1 AS asset_id, payout_0 AS payout FROM read_parquet('{LABELS}')
        WHERE resolved AND NOT coalesce(is_void,false) AND token1 IS NOT NULL AND payout_0 IS NOT NULL
        UNION ALL
        SELECT token2, payout_1 FROM read_parquet('{LABELS}')
        WHERE resolved AND NOT coalesce(is_void,false) AND token2 IS NOT NULL AND payout_1 IS NOT NULL
    """)
    logger.info(f"resolved tokens: {con.sql('SELECT count(*) FROM payout').fetchall()[0][0]:,}")

    rows = []
    if args.cache and Path(args.cache).exists() and any(Path(args.cache).glob("*.parquet")):
        f = str(Path(args.cache) / "*.parquet")
        logger.info(f"reading cache {f}")
        con.execute(f"""
            CREATE OR REPLACE TABLE accum AS
            WITH p AS (
                SELECT wallet AS maker, asset_id,
                       sum(coalesce(buy_sh,0))    AS bs,
                       sum(coalesce(buy_cash,0))  AS bc,
                       sum(coalesce(sell_sh,0))   AS ss,
                       sum(coalesce(sell_cash,0)) AS sc,
                       sum(coalesce(n_buy,0))     AS n_buy,
                       sum(coalesce(n_sell,0))    AS n_sell
                FROM read_parquet('{f}') GROUP BY 1, 2
            )
            SELECT p.maker,
                   sum(least(bs,ss) * ((CASE WHEN ss>0 THEN sc/ss ELSE 0 END)
                                     - (CASE WHEN bs>0 THEN bc/bs ELSE 0 END))) AS realised,
                   sum(CASE WHEN bs >= ss
                            THEN (bs-ss) * (po.payout - (CASE WHEN bs>0 THEN bc/bs ELSE 0 END))
                            ELSE (ss-bs) * ((CASE WHEN ss>0 THEN sc/ss ELSE 0 END) - po.payout)
                       END) AS settled,
                   sum(bc + sc) AS volume,
                   sum(n_buy + n_sell) AS fills,
                   count(*) AS tokens
            FROM p JOIN payout po ON po.asset_id = p.asset_id
            GROUP BY 1
        """)
        months = []
    else:
        months = month_starts(start, end)
    for i, (lo, hi) in enumerate(months, 1):
        t0, t1 = int(lo.timestamp()), int(hi.timestamp())
        con.execute(f"""
            -- A wallet's round trip spans BOTH roles: the largest makers buy as
            -- maker and exit as taker, so a maker-only view sees one leg and reports
            -- zero. Union both roles, keyed by wallet.
            CREATE OR REPLACE TEMP TABLE pairs AS
            WITH fills AS (
                SELECT t.maker AS wallet, t.asset_id, t.maker_direction AS direction,
                       t.price, t.token_amount
                FROM read_parquet('{TRADES}') t
                WHERE t.timestamp >= {t0} AND t.timestamp < {t1}
                  AND t.maker IS NOT NULL AND t.taker IS NOT NULL
                  AND t.maker <> t.taker
                  AND t.usd_amount > 0 AND t.price > 0 AND t.price < 1
                  AND t.maker_direction IN ('BUY','SELL')
                UNION ALL
                SELECT t.taker, t.asset_id, t.taker_direction, t.price, t.token_amount
                FROM read_parquet('{TRADES}') t
                WHERE t.timestamp >= {t0} AND t.timestamp < {t1}
                  AND t.maker IS NOT NULL AND t.taker IS NOT NULL
                  AND t.maker <> t.taker
                  AND t.usd_amount > 0 AND t.price > 0 AND t.price < 1
                  AND t.taker_direction IN ('BUY','SELL')
            )
            SELECT f.wallet AS maker, f.asset_id,
                   sum(CASE WHEN f.direction = 'BUY'  THEN f.price * f.token_amount END) AS buy_usd,
                   sum(CASE WHEN f.direction = 'SELL' THEN f.price * f.token_amount END) AS sell_usd,
                   sum(CASE WHEN f.direction = 'BUY'  THEN f.token_amount END) AS buy_sh,
                   sum(CASE WHEN f.direction = 'SELL' THEN f.token_amount END) AS sell_sh,
                   count(*) AS fills
            FROM fills f
            JOIN payout p ON p.asset_id = f.asset_id
            GROUP BY 1, 2
        """)
        con.execute("""
            -- coalesce BEFORE the comparison: `buy_sh > sell_sh` is NULL when one side
            -- is missing, which silently dropped every one-sided wallet's settled term
            -- while still counting two-sided wallets' realised PnL. That asymmetry made
            -- aggregate PnL positive, which conservation forbids.
            CREATE OR REPLACE TEMP TABLE wallet AS
            SELECT maker,
                   sum(least(bs, ss) * (vss - vbs)) AS realised,
                   sum(CASE WHEN bs >= ss THEN (bs - ss) * (p.payout - vbs)
                            ELSE (ss - bs) * (vss - p.payout) END) AS settled,
                   sum(buy_usd + sell_usd) AS volume,
                   sum(fills) AS fills,
                   count(*) AS tokens
            FROM (
                SELECT maker, asset_id,
                       coalesce(buy_usd,0) AS buy_usd, coalesce(sell_usd,0) AS sell_usd,
                       coalesce(buy_sh,0)  AS bs,      coalesce(sell_sh,0)  AS ss,
                       coalesce(buy_usd / nullif(buy_sh,0), 0)  AS vbs,
                       coalesce(sell_usd / nullif(sell_sh,0), 0) AS vss,
                       fills
                FROM pairs
            ) q
            JOIN payout p ON p.asset_id = q.asset_id
            GROUP BY 1
        """)
        v, p, _pnl = con.sql("""
            SELECT count(*), sum(volume), sum(coalesce(realised,0) + coalesce(settled,0))
            FROM wallet
        """).fetchall()[0]
        rows.append((lo.date(), hi.date(), v, p or 0.0, 0.0, len(months)))
        logger.info(f"  {lo:%Y-%m}: {v:,} makers with a round trip")

        if i == 1:
            con.execute("CREATE OR REPLACE TABLE accum AS SELECT * FROM wallet")
        else:
            con.execute("""
                CREATE OR REPLACE TABLE accum AS
                SELECT maker,
                       sum(realised) AS realised, sum(settled) AS settled,
                       sum(volume) AS volume, sum(fills) AS fills, sum(tokens) AS tokens
                FROM (SELECT * FROM accum UNION ALL SELECT * FROM wallet)
                GROUP BY 1
            """)

    tot = con.sql("SELECT count(*), sum(volume) FROM accum").fetchall()[0]
    logger.info(f"total: {tot[0]:,} makers, ${tot[1]/1e9:.2f} B volume")

    log.info("\nby volume decile (round-trip PnL, average cost):")
    log.info("  decile    makers   volume $m    pnl $m     pnl/$   positive")
    for r in con.sql(f"""
        SELECT dec, count(*) makers, sum(volume)/1e6 vol_m,
               sum(coalesce(realised,0) + coalesce(settled,0))/1e6 pnl_m,
               sum(coalesce(realised,0) + coalesce(settled,0)) / nullif(sum(volume),0) pnl_per_dollar,
               100.0 * count(*) FILTER (WHERE coalesce(realised,0) + coalesce(settled,0) > 0) / count(*) positive_pct
        FROM (
            SELECT *, ntile(10) OVER (ORDER BY volume DESC) AS dec
            FROM accum WHERE volume >= {args.min_volume}
        )
        GROUP BY dec ORDER BY dec
    """).fetchall():
        log.info(f"  {r[0]:>6}  {r[1]:>8,}  {r[2]:>10,.0f}  {r[3]:>+9.2f}  {r[4]:>+8.5f}  {r[5]:>7.1f}%")

    allr = con.sql(f"""
        SELECT count(*), sum(volume), sum(coalesce(realised,0) + coalesce(settled,0)),
               sum(coalesce(realised,0) + coalesce(settled,0)) / nullif(sum(volume),0),
               100.0 * count(*) FILTER (WHERE coalesce(realised,0) + coalesce(settled,0) > 0) / count(*)
        FROM accum WHERE volume >= {args.min_volume}
    """).fetchall()[0]
    log.info(f"\n  all      {allr[0]:>8,}  {allr[1]/1e6:>10,.0f}  {allr[2]/1e6:>+9.2f}  {allr[3]:>+8.5f}  {allr[4]:>7.1f}%")

    log.info(f"\nmakers with >= ${args.min_volume:,.0f} maker volume: {allr[0]:,}")
    log.info(f"round-trip pnl: ${allr[2]/1e6:+.2f} M on ${allr[1]/1e9:.2f} B "
          f"({allr[3]*100:+.4f} cents per $1)")
    log.info(f"positive-PnL makers: {allr[4]:.1f} %")

    top = con.sql(f"""
        SELECT maker, volume, coalesce(realised,0)+coalesce(settled,0) pnl,
               (coalesce(realised,0)+coalesce(settled,0))/nullif(volume,0) per_dollar, tokens
        FROM accum WHERE volume >= {args.min_volume}
        ORDER BY volume DESC LIMIT 15
    """).fetchall()
    log.info("\nthe 15 largest makers by volume:")
    log.info("  pnl $m    volume $m    pnl/$      tokens  maker")
    for m, vol, pnl, pd, tk in top:
        log.info(f"  {pnl/1e6:>+7.2f}  {vol/1e6:>10,.1f}  {pd:>+8.5f}  {tk:>8,}  {m[:20]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
