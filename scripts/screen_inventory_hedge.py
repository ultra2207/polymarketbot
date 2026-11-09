"""Does a mechanical hedge rescue the inventory-capped maker (#13)?

#13 measured the maker at the touch on one week of BTC 15 m and found +$9/week at a
2,000-share inventory cap, because the flow is ~73 % one-sided and the maker ends up
long the side that is about to lose. This asks whether hedging every fill immediately
recovers the spread capture.

Fill set is identical to `sim_maker_queue.py`: favourite side only (mid >= 0.7), a fill
only when a tape print executes at our touch, our size = 1/109 of the printed size
(HHI-implied effective maker count), carried PnL marked at the book mid 60 s later.

Variants
--------
no-hedge    inventory carried, marked at mid_60.
complement  each fill locked into a complete set at the same instant: long A at `price`
            is matched by buying the complement B at B's ask -> redeems 1, so
            1 - price - ask_B.  Short A at `price` is matched by selling B at B's bid
            -> owes 1, so price + bid_B - 1.
perp        inventory delta-hedged with Binance BTCUSDT 1 m. The hedge ratio is the
            *measured* d(mid)/d(ln S) slope on the same week, held 60 s, charged a taker
            round trip per side.

PnL sign convention matches `sim_maker_queue.py`: for a taker BUY (sgn=+1) the maker
sold, so the maker's position is -sgn*shares and carried PnL is sgn*(price - mid60).

Cap policies are ambiguous in #13, so all three are reported: per token, per market
(up+down netted), and portfolio-wide net signed shares.

Usage: uv run scripts/screen_inventory_hedge.py [--cap 2000]
"""

from __future__ import annotations

import argparse

import duckdb

from polymarketbot.fees import era_schedule
from polymarketbot.logs import get_logger

log = get_logger("inventory_hedge")

WEEK_START = 1772064000  # 2026-02-26T00:00Z  (the window the #13 sim used)
WEEK_END = 1772668800    # 2026-03-05T00:00Z
QUEUE_SHARE = 1.0 / 109.0
MARKOUT_S = 60
PERP_FEE_BPS_PER_SIDE = 4.5  # Binance USDT-M taker; 0/1.8 for maker


def build(con: duckdb.DuckDBPyConnection, queue_share: float) -> None:
    # ---- universe: BTC 15 m tokens with their complement -----------------------
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE mk AS
        SELECT market_id, condition_id, end_ts, 1 AS dir,
               up_token_id AS token_id, down_token_id AS comp_id
        FROM 'data/raw/hf/aliplayer1/markets.parquet'
        WHERE crypto='BTC' AND timeframe='15-minute'
          AND up_token_id IS NOT NULL AND down_token_id IS NOT NULL
        UNION ALL
        SELECT market_id, condition_id, end_ts, -1 AS dir,
               down_token_id, up_token_id
        FROM 'data/raw/hf/aliplayer1/markets.parquet'
        WHERE crypto='BTC' AND timeframe='15-minute'
          AND up_token_id IS NOT NULL AND down_token_id IS NOT NULL
        """
    )
    con.execute("CREATE OR REPLACE TEMP TABLE toks AS SELECT token_id FROM mk")

    # ---- book, week only ------------------------------------------------------
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE book AS
        SELECT d.timestamp_ms, d.token_id, d.mid, d.best_bid, d.best_ask
        FROM 'data/raw/dome/dome_polymarket_2025-10_2026-03.parquet' d
        JOIN toks USING(token_id)
        WHERE d.timestamp_ms BETWEEN {WEEK_START * 1000} AND {WEEK_END * 1000}
          AND d.best_ask > d.best_bid AND d.mid BETWEEN 0.02 AND 0.98
        """
    )
    log.info("book rows in week: {}", f"{con.sql('SELECT count(*) FROM book').fetchall()[0][0]:,}")

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE tr AS
        SELECT t.timestamp, t.asset_id AS token_id, t.price, t.token_amount,
               CASE WHEN t.taker_direction='BUY' THEN 1 ELSE -1 END AS sgn
        FROM read_parquet('data/raw/sii/trades.parquet') t
        JOIN toks ON toks.token_id = t.asset_id
        WHERE t.timestamp BETWEEN {WEEK_START} AND {WEEK_END}
          AND t.price > 0.02 AND t.price < 0.98
        """
    )
    log.info("trades on BTC 15m tokens: {}", f"{con.sql('SELECT count(*) FROM tr').fetchall()[0][0]:,}")

    # ---- at-touch fills, mid at +60 s, complement's touch ---------------------
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE own AS
        SELECT tr.timestamp, tr.token_id, tr.price, tr.sgn, tr.token_amount,
               b.mid AS mid_t, b.best_bid AS bid_t, b.best_ask AS ask_t,
               b60.mid AS mid60
        FROM tr
        ASOF LEFT JOIN book b
          ON b.token_id = tr.token_id AND b.timestamp_ms <= tr.timestamp * 1000
        ASOF LEFT JOIN book b60
          ON b60.token_id = tr.token_id
          AND b60.timestamp_ms <= tr.timestamp * 1000 + {MARKOUT_S * 1000}
        WHERE b.mid IS NOT NULL
          AND ((tr.sgn = 1 AND abs(tr.price - b.best_ask) < 0.0005)
            OR (tr.sgn = -1 AND abs(tr.price - b.best_bid) < 0.0005))
          AND b.mid >= 0.7
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE f AS
        SELECT own.*, mk.market_id, mk.condition_id, mk.end_ts, mk.dir, mk.comp_id,
               cb.mid AS comp_mid, cb.best_bid AS comp_bid, cb.best_ask AS comp_ask
        FROM own
        JOIN mk ON mk.token_id = own.token_id
        ASOF LEFT JOIN book cb
          ON cb.token_id = mk.comp_id AND cb.timestamp_ms <= own.timestamp * 1000
        """
    )
    log.info("at-touch fills, favourite side: {}", f"{con.sql('SELECT count(*) FROM f').fetchall()[0][0]:,}")

    # ---- spot at fill and at +60 s -------------------------------------------
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE spot AS
        SELECT open_time_s AS t, close AS px
        FROM read_parquet('data/raw/binance/klines/symbol=BTCUSDT/*.parquet')
        WHERE open_time_s BETWEEN {WEEK_START} - 120 AND {WEEK_END} + 240
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE fills AS
        SELECT f.*, f.token_amount * {queue_share} AS our_shares,
               ln(s1.px / s0.px) AS r60, s0.px AS spot0
        FROM f
        ASOF LEFT JOIN spot s0 ON s0.t <= f.timestamp
        ASOF LEFT JOIN spot s1 ON s1.t <= f.timestamp + {MARKOUT_S}
        WHERE s0.px IS NOT NULL AND s1.px IS NOT NULL
          AND f.mid60 IS NOT NULL AND f.comp_bid IS NOT NULL AND f.comp_ask IS NOT NULL
        """
    )
    log.info("fills with spot + complement both ends: {}",
             f"{con.sql('SELECT count(*) FROM fills').fetchall()[0][0]:,}")

    # ---- hold-to-settlement view --------------------------------------------
    # payout of the token we traded, and the spot move from the fill to the
    # market's end_ts: this is the inventory risk that actually matters.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE settled AS
        SELECT fills.*,
               CASE WHEN fills.token_id = pit.up_token_id
                    THEN pit.payout_0 ELSE pit.payout_1 END AS payout,
               ln(send.px / fills.spot0) AS r_end
        FROM fills
        JOIN (SELECT condition_id, token1 AS up_token_id, payout_0, payout_1
              FROM 'data/processed/markets_pit.parquet'
              WHERE resolved) pit USING(condition_id)
        ASOF LEFT JOIN spot send ON send.t <= fills.end_ts
        WHERE send.px IS NOT NULL
        """
    )
    log.info("fills with a settlement payout and end-of-life spot: {}",
             f"{con.sql('SELECT count(*) FROM settled').fetchall()[0][0]:,}")


def cap_policy(con, name: str, part: str, cap: float) -> str:
    """Materialise the subset of fills admissible under an inventory cap.

    `part` is the PARTITION BY clause (empty = portfolio-wide) and the frame is
    cumulative. Pass part="RANGE" for the trailing-15-minute live-inventory cap.
    """
    tbl = f"capped_{name}"
    if part == "RANGE":
        window = ("sum(-sgn * our_shares) OVER (ORDER BY timestamp "
                  "RANGE BETWEEN 900 PRECEDING AND 1 PRECEDING)")
    else:
        window = (f"sum(-sgn * our_shares) OVER ({part} ORDER BY timestamp, token_id, price "
                  "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE {tbl} AS
        SELECT * FROM (
          SELECT settled.*,
                 coalesce({window}, 0) AS pos_before
          FROM settled
        ) WHERE abs(pos_before) < {cap}
        """
    )
    if part == "RANGE":
        window_after = ("sum(-sgn * our_shares) OVER (ORDER BY timestamp "
                        "RANGE BETWEEN 900 PRECEDING AND CURRENT ROW)")
    else:
        window_after = (f"sum(-sgn * our_shares) OVER ({part} ORDER BY timestamp, token_id, price "
                        "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE path_{name} AS
        SELECT *, coalesce({window_after}, 0) AS pos_after
        FROM {tbl}
        """
    )
    return f"path_{name}"


def report(con, path: str, gamma: float, fee_rate: float, fee_exp: float,
           rebate_rate: float) -> dict:
    """Aggregate one admissible fill set into the four PnL variants."""
    row = con.sql(
        f"""
        WITH x AS (
          SELECT *,
            our_shares * sgn * (price - mid60)                        AS pnl_none,
            our_shares * sgn * (price - mid_t)                        AS edge_touch,
            CASE WHEN sgn = -1
                 THEN our_shares * (1 - price - comp_ask)
                 ELSE our_shares * (price + comp_bid - 1) END         AS pnl_comp,
            our_shares * sgn * dir * {gamma} * r60                    AS pnl_perp_raw,
            abs(our_shares * {gamma})                                 AS perp_notional,
            -- we rest, so we pay no taker fee and collect the rebate on the taker's fee
            our_shares * {rebate_rate} * {fee_rate}
              * pow(price * (1 - price), {fee_exp})                   AS rebate,
            -- the hedge leg crosses the complement's touch: we are the taker there
            our_shares * {fee_rate}
              * pow((CASE WHEN sgn = -1 THEN comp_ask ELSE comp_bid END)
                    * (1 - CASE WHEN sgn = -1 THEN comp_ask ELSE comp_bid END),
                    {fee_exp})                                        AS comp_fee
          FROM {path}
        )
        SELECT count(*), sum(our_shares),
               sum(pnl_none), sum(pnl_comp), sum(edge_touch),
               sum(pnl_none + pnl_perp_raw),
               sum(2 * {PERP_FEE_BPS_PER_SIDE} / 10000.0 * perp_notional),
               avg(abs(pos_after)), max(abs(pos_after)), max(abs(pos_before)),
               sum(rebate), sum(comp_fee),
               sum(pnl_none + pnl_perp_raw + rebate),
               sum(pnl_comp + rebate - comp_fee),
               sum(pnl_comp), sum(rebate)
        FROM x
        """
    ).fetchall()[0]
    return dict(fills=row[0], shares=row[1], pnl_none=row[2], pnl_comp=row[3],
                edge=row[4], perp_gross=row[5], perp_fee=row[6],
                inv_mean=row[7], inv_peak=row[8], inv_peak_before=row[9],
                rebate=row[10], comp_fee=row[11],
                perp_net=row[12] - row[6], comp_net=row[13],
                comp_lock=row[14])



def report_settle(con, path: str, gamma: float, fee_rate: float, fee_exp: float,
                  rebate_rate: float) -> dict:
    """Same four variants, but inventory is carried to settlement instead of 60 s."""
    row = con.sql(
        f"""
        WITH x AS (
          SELECT *,
            our_shares * sgn * (price - payout)                       AS pnl_none,
            CASE WHEN sgn = -1
                 THEN our_shares * (1 - price - comp_ask)
                 ELSE our_shares * (price + comp_bid - 1) END         AS pnl_comp,
            our_shares * sgn * dir * {gamma} * r_end                  AS pnl_perp_raw,
            abs(our_shares * {gamma})                                 AS perp_notional,
            our_shares * {rebate_rate} * {fee_rate}
              * pow(price * (1 - price), {fee_exp})                   AS rebate,
            our_shares * {fee_rate}
              * pow((CASE WHEN sgn = -1 THEN comp_ask ELSE comp_bid END)
                    * (1 - CASE WHEN sgn = -1 THEN comp_ask ELSE comp_bid END),
                    {fee_exp})                                        AS comp_fee
          FROM {path}
        )
        SELECT count(*), sum(our_shares), sum(pnl_none), sum(pnl_comp),
               sum(pnl_none + pnl_perp_raw),
               sum(2 * {PERP_FEE_BPS_PER_SIDE} / 10000.0 * perp_notional),
               max(abs(pos_after)), avg(abs(pos_after)),
               sum(rebate), sum(comp_fee), sum(perp_notional),
               sum(pnl_none + pnl_perp_raw + rebate) - sum(2 * {PERP_FEE_BPS_PER_SIDE} / 10000.0 * perp_notional),
               sum(pnl_comp + rebate - comp_fee),
               avg(payout), avg(abs(price - payout))
        FROM x
        """
    ).fetchall()[0]
    return dict(fills=row[0], shares=row[1], pnl_none=row[2], comp_lock=row[3],
                perp_gross=row[4], perp_fee=row[5], inv_peak=row[6], inv_mean=row[7],
                rebate=row[8], comp_fee=row[9], perp_notional=row[10],
                perp_net=row[11], comp_net=row[12],
                payout_mean=row[13], price_payout_gap=row[14])


def main() -> int:
    global PERP_FEE_BPS_PER_SIDE
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=float, default=2000.0)
    ap.add_argument("--queue-share", type=float, default=QUEUE_SHARE)
    ap.add_argument("--fee-bps", type=float, default=PERP_FEE_BPS_PER_SIDE)
    args = ap.parse_args()
    PERP_FEE_BPS_PER_SIDE = args.fee_bps

    con = duckdb.connect()
    con.execute("SET threads=6")
    con.execute("SET memory_limit='6GB'")
    con.execute("SET temp_directory='data/tmp_duck'")

    build(con, args.queue_share)

    r = con.sql(
        "SELECT count(*) FILTER (sgn=1), count(*) FILTER (sgn=-1) FROM fills"
    ).fetchall()[0]
    log.info("taker BUY {} / taker SELL {} -> {:.1f}% one-sided",
             f"{r[0]:,}", f"{r[1]:,}", 100 * max(r[0], r[1]) / (r[0] + r[1]))

    # ---- measured delta over the same 60 s -----------------------------------
    d = con.sql(
        """
        SELECT count(*), regr_slope(mid60 - mid_t, dir * r60),
               corr(mid60 - mid_t, dir * r60),
               stddev_samp(r60), stddev_samp(mid60 - mid_t) * 100
        FROM fills WHERE abs(r60) > 1e-9
        """
    ).fetchall()[0]
    gamma = d[1]
    hs = con.sql("SELECT avg(sgn*(price-mid_t)), avg(price) FROM fills").fetchall()[0]
    log.info("d(mid)/d(ln S) over 60s: n={} slope={:+.4f} corr={:+.4f} sd(r60)={:.5f} sd(dmid)={:.4f}c",
             f"{d[0]:,}", d[1], d[2], d[3], d[4])
    log.info("half-spread earned at the touch: {:+.4f}c on mean price {:.4f}",
             100 * hs[0], hs[1])

    sched = era_schedule("crypto", "2026-02-27T00:00:00+00:00")
    log.info("fee schedule week-of: rate={} exp={} rebate={} ({})",
             sched.rate, sched.exponent, sched.rebate_rate, sched.source)

    policies = [
        ("token", "PARTITION BY token_id"),
        ("market", "PARTITION BY market_id"),
        ("portfolio", ""),
        ("live15", "RANGE"),
    ]

    log.info(f"\n=== cap {args.cap:,.0f} shares ===")
    log.info("| cap policy | fills | shares | no-hedge gross | no-hedge net | complement net | perp gross | perp net | peak |inv| | mean |inv| |")
    log.info("|---|---|---|---|---|---|---|---|---|---|")
    for name, part in policies:
        p = cap_policy(con, name, part, args.cap)
        s = report(con, p, gamma, sched.rate, sched.exponent, sched.rebate_rate)
        log.info(f"| {name} | {s['fills']:,} | {s['shares']:,.0f} | ${s['pnl_none']:+,.2f} | "
              f"${s['pnl_none'] + s['rebate']:+,.2f} | ${s['comp_net']:+,.2f} | "
              f"${s['perp_gross']:+,.2f} | ${s['perp_net']:+,.2f} | "
              f"{s['inv_peak']:,.0f} | {s['inv_mean']:,.0f} |")
        log.info("[{}] fills {} shares {} | nohedge gross ${:+.2f} rebate ${:+.2f} net ${:+.2f} | "
                 "comp lock ${:+.2f} hedgefee ${:+.2f} net ${:+.2f} | perp gross ${:+.2f} binance ${:+.2f} net ${:+.2f} | "
                 "peak inv {:.0f} mean {:.0f}",
                 name, f"{s['fills']:,}", f"{s['shares']:,.0f}",
                 s['pnl_none'], s['rebate'], s['pnl_none'] + s['rebate'],
                 s['comp_lock'], s['comp_fee'], s['comp_net'],
                 s['perp_gross'], s['perp_fee'], s['perp_net'],
                 s['inv_peak'], s['inv_mean'])

    # ---- uncapped, for the per-share economics -------------------------------
    p_unc = cap_policy(con, "uncapped", "PARTITION BY token_id", 1e15)
    u = report(con, p_unc, gamma, sched.rate, sched.exponent, sched.rebate_rate)
    sh = u['shares']
    log.info("\n=== uncapped (all admissible fills) ===")
    log.info("| variant | fills | shares | PnL/wk | PnL/share | peak inv |")
    log.info("|---|---|---|---|---|---|")
    for lbl, v in (("no hedge, gross", u['pnl_none']),
                   ("no hedge, + rebate", u['pnl_none'] + u['rebate']),
                   ("complement, gross", u['comp_lock']),
                   ("complement, net of hedge fee", u['comp_net']),
                   ("perp, gross", u['perp_gross']),
                   ("perp, net of Binance fee", u['perp_net'])):
        log.info(f"| {lbl} | {u['fills']:,} | {sh:,.0f} | ${v:+,.2f} | {100*v/sh:+.3f}c | {u['inv_peak']:,.0f} |")
    log.info("uncapped: shares {:.0f} nohedge ${:+.2f} (+rebate ${:+.2f} = ${:+.2f}) comp lock ${:+.2f} "
             "hedgefee ${:+.2f} comp net ${:+.2f} perp gross ${:+.2f} binance ${:+.2f} perp net ${:+.2f} "
             "edge@touch {:.4f}c/share",
             sh, u['pnl_none'], u['rebate'], u['pnl_none'] + u['rebate'], u['comp_lock'],
             u['comp_fee'], u['comp_net'], u['perp_gross'], u['perp_fee'], u['perp_net'],
             100 * u['edge'] / sh)

    # ---- per-share decomposition --------------------------------------------
    log.info("\nper-share decomposition (uncapped):")
    log.info(f"  half-spread earned at touch      {100*u['edge']/sh:+.4f}c")
    log.info(f"  carried markout (no hedge)       {100*u['pnl_none']/sh:+.4f}c")
    log.info(f"  complement lock cost             {100*u['comp_lock']/sh:+.4f}c")
    log.info(f"  hedge-leg taker fee              {-100*u['comp_fee']/sh:+.4f}c")
    log.info(f"  maker rebate credit              {100*u['rebate']/sh:+.4f}c")
    log.info(f"  perp delta PnL (gross)           {100*(u['perp_gross']-u['pnl_none'])/sh:+.4f}c")
    log.info(f"  perp fee (Binance)               {-100*u['perp_fee']/sh:+.4f}c")

    # ---- hold to settlement ---------------------------------------------------
    dres = con.sql(
        """
        SELECT count(*), regr_slope(payout - mid_t, dir * r_end),
               corr(payout - mid_t, dir * r_end), stddev_samp(r_end),
               avg(payout - mid_t), stddev_samp(payout - mid_t) * 100
        FROM settled WHERE abs(r_end) > 1e-9
        """
    ).fetchall()[0]
    gamma_res = dres[1]
    log.info("d(payout-mid)/d(ln S) to end_ts: n={} slope={:+.3f} corr={:+.3f} sd(r_end)={:.5f} "
             "mean gap={:+.4f} sd(gap)={:.2f}c",
             f"{dres[0]:,}", dres[1], dres[2], dres[3], dres[4], dres[5])

    log.info("\n=== held to settlement (same admissible sets) ===")
    log.info("| cap policy | fills | shares | no-hedge gross | no-hedge + rebate | complement net | perp gross | perp net | perp notional | peak |inv| |")
    log.info("|---|---|---|---|---|---|---|---|---|---|")
    rows = []
    for name, part in policies:
        p = cap_policy(con, name, part, args.cap)
        z = report_settle(con, p, gamma_res, sched.rate, sched.exponent, sched.rebate_rate)
        rows.append((name, z))
        log.info(f"| {name} | {z['fills']:,} | {z['shares']:,.0f} | ${z['pnl_none']:+,.2f} | "
              f"${z['pnl_none'] + z['rebate']:+,.2f} | ${z['comp_net']:+,.2f} | "
              f"${z['perp_gross']:+,.2f} | ${z['perp_net']:+,.2f} | "
              f"${z['perp_notional']:,.0f} | {z['inv_peak']:,.0f} |")
        log.info("[settle:{}] fills {} shares {:.0f} nohedge ${:+.2f} +rebate ${:+.2f} comp net ${:+.2f} "
                 "perp gross ${:+.2f} fee ${:+.2f} net ${:+.2f} notional ${:,.0f} peak inv {:.0f} "
                 "mean payout {:.3f} mean |price-payout| {:.3f}",
                 name, f"{z['fills']:,}", z['shares'], z['pnl_none'], z['pnl_none'] + z['rebate'],
                 z['comp_net'], z['perp_gross'], z['perp_fee'], z['perp_net'],
                 z['perp_notional'], z['inv_peak'], z['payout_mean'], z['price_payout_gap'])

    # ---- cluster-robust errors ----------------------------------------------
    # Fills inside one 15-minute market are one bet, not many: the effective
    # sample is ~673 markets, not 276k fills. Cluster by condition_id.
    g60 = con.sql(
        f"""
        WITH x AS (
          SELECT condition_id, our_shares,
                 our_shares * sgn * (price - mid60) AS a,
                 our_shares * sgn * (dir * {gamma} * r60) AS b,
                 CASE WHEN sgn = -1 THEN our_shares * (1 - price - comp_ask)
                      ELSE our_shares * (price + comp_bid - 1) END AS c
          FROM {p_unc}
        ),
        m AS (SELECT condition_id, sum(a)/sum(our_shares) a, sum(b)/sum(our_shares) b,
                     sum(c)/sum(our_shares) c, sum(our_shares) w FROM x GROUP BY 1)
        SELECT count(*), avg(a), stddev_samp(a)/sqrt(count(*)), avg(b),
               stddev_samp(b)/sqrt(count(*)), avg(c), stddev_samp(c)/sqrt(count(*)), sum(w)
        FROM m WHERE w > 0
        """
    ).fetchall()[0]
    gset = con.sql(
        f"""
        WITH x AS (
          SELECT condition_id, our_shares,
                 our_shares * sgn * (price - payout) AS a,
                 our_shares * sgn * (dir * {gamma_res} * r_end) AS b
          FROM {p_unc}
        ),
        m AS (SELECT condition_id, sum(a)/sum(our_shares) a, sum(b)/sum(our_shares) b,
                     sum(our_shares) w FROM x GROUP BY 1)
        SELECT count(*), avg(a), stddev_samp(a)/sqrt(count(*)), avg(b),
               stddev_samp(b)/sqrt(count(*)) FROM m WHERE w > 0
        """
    ).fetchall()[0]
    gc = con.sql(
        f"""
        WITH x AS (
          SELECT condition_id, our_shares, our_shares * sgn * (price - payout) AS a
          FROM path_portfolio
        ),
        m AS (SELECT condition_id, sum(a)/sum(our_shares) a, sum(our_shares) w
              FROM x GROUP BY 1)
        SELECT count(*), avg(a), stddev_samp(a)/sqrt(count(*)) FROM m WHERE w > 0
        """
    ).fetchall()[0]
    log.info("cluster [portfolio cap, settlement]: markets {} {:.4f}c +- {:.4f} (t={:+.2f})",
             gc[0], 100*gc[1], 100*gc[2], gc[1]/gc[2])
    log.info(f"  [portfolio cap, settlement] markets {gc[0]}: {100*gc[1]:+.3f}c +- {100*gc[2]:.3f} "
          f"(t={gc[1]/gc[2]:+.2f})")

    # concentration of the headline number
    conc = con.sql(
        f"""
        WITH m AS (
          SELECT condition_id, sum(our_shares * sgn * (price - mid60)) p,
                 sum(our_shares * sgn * (price - payout)) q
          FROM {p_unc} GROUP BY 1
        )
        SELECT count(*), sum(p), sum(q),
               (SELECT sum(p) FROM (SELECT p FROM m ORDER BY p DESC LIMIT 5)),
               (SELECT sum(p) FROM (SELECT p FROM m ORDER BY p DESC LIMIT 5)) / sum(p),
               stddev_samp(p)
        FROM m
        """
    ).fetchall()[0]
    log.info("concentration: {} markets, 60s total ${:+.2f} (top-5 ${:+.2f} = {:.0%}), "
             "settle total ${:+.2f}, sd(per-market) ${:,.2f}",
             conc[0], conc[1], conc[3], conc[4], conc[2], conc[5])
    log.info(f"\nconcentration: {conc[0]} markets, 60s total ${conc[1]:+.2f}; top 5 markets "
          f"${conc[3]:+.2f} ({conc[4]:.0%}); sd per market ${conc[5]:,.2f}")

    log.info("\n=== cluster-robust (per market, cents/share, t = mean/SE) ===")
    log.info(f"  markets {g60[0]}  60s markout {100*g60[1]:+.3f}c +- {100*g60[2]:.3f} (t={g60[1]/g60[2]:+.2f})"
          f"   perp leg {100*g60[3]:+.3f}c +- {100*g60[4]:.3f}")
    log.info(f"  settlement  {100*gset[1]:+.3f}c +- {100*gset[2]:.3f} (t={gset[1]/gset[2]:+.2f})"
          f"   perp leg {100*gset[3]:+.3f}c +- {100*gset[4]:.3f}")
    log.info("clustered: markets {} 60s {:.4f}c +- {:.4f} (t={:+.2f}) | settle {:.4f}c +- {:.4f} (t={:+.2f})",
             g60[0], 100*g60[1], 100*g60[2], g60[1]/g60[2],
             100*gset[1], 100*gset[2], gset[1]/gset[2])

    # ---- inventory path ------------------------------------------------------
    log.info("\n=== inventory path, portfolio-capped book (end-of-day net shares) ===")
    q = f"""
    WITH p AS (SELECT * FROM path_portfolio),
    net AS (
      SELECT strftime(to_timestamp(timestamp), '%Y-%m-%d') d,
             sum(-sgn * our_shares) OVER (ORDER BY timestamp
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS pos
      FROM p
    ),
    last AS (SELECT d, max(pos) hi, min(pos) lo, last(pos ORDER BY d) FROM net GROUP BY d)
    SELECT * FROM last ORDER BY d
    """
    for x in con.sql(q).fetchall():
        log.info(f"  {x[0]}  net {x[3]:+,.0f} shares  (intraday {x[2]:+,.0f} .. {x[1]:+,.0f})")

    log.info("\n=== inventory path, uncapped book ===")
    q2 = f"""
    WITH net AS (
      SELECT strftime(to_timestamp(timestamp), '%Y-%m-%d') d,
             sum(-sgn * our_shares) OVER (ORDER BY timestamp
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS pos
      FROM {p_unc}
    )
    SELECT d, max(pos), min(pos), last(pos ORDER BY d) FROM net GROUP BY d ORDER BY d
    """
    for x in con.sql(q2).fetchall():
        log.info(f"  {x[0]}  net {x[3]:+,.0f} shares  (intraday {x[2]:+,.0f} .. {x[1]:+,.0f})")

    # ---- break-even hedge cost ----------------------------------------------
    # 1) complement: the max extra per-share cost it can absorb before going negative
    be_comp = u['comp_net'] / sh
    # 2) perp: fee bps per side at which the hedged book matches no-hedge
    perp_notional_total = con.sql(
        f"SELECT sum(abs(our_shares*{gamma})) FROM {p_unc}"
    ).fetchall()[0][0]
    be_perp_zero = (u['perp_gross'] + u['rebate']) / perp_notional_total * 10000 / 2
    be_perp_parity = (u['perp_gross'] + u['rebate'] - u['pnl_none']) / perp_notional_total * 10000 / 2
    log.info("break-even complement cost {:.4f}c/share; perp notional ${:,.0f} "
             "(avg ${:,.2f}/fill); perp net=0 at {:.2f}bps/side, parity with no-hedge at {:.2f}bps/side",
             100 * be_comp, perp_notional_total, perp_notional_total / u['fills'],
             be_perp_zero, be_perp_parity)
    log.info(f"\nbreak-even complement extra cost: {100*be_comp:+.4f}c/share "
          f"(it locks {100*u['comp_net']/sh:+.4f}c after the hedge fee)")
    log.info(f"perp notional traded: ${perp_notional_total:,.0f} (${perp_notional_total/u['fills']:,.2f}/fill)")
    log.info(f"break-even Binance fee: net=0 at {be_perp_zero:.2f} bps/side, "
          f"parity with holding at {be_perp_parity:.2f} bps/side")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
