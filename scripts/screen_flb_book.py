"""Rows #12 / #19: is the favourite-longshot bias present *in the book itself*?

#12 and #19 rest on tape markouts (who filled, at what price). This screen asks the book
directly, with no fills and no maker identity:

1. ``book``     - per price bucket: quoted spread, both half-spreads, level-0 size, total
                  depth, and the ask/bid depth asymmetry.
2. ``cal``      - book-implied probability (mid, bid, ask) vs the realised payout from
                  ``market_labels``, per horizon and price decile. One observation per
                  (market, token, horizon) so markets are not weighted by how often they
                  were snapshotted.
3. ``overround``- paired YES/NO snapshots: sum(bid), sum(ask), sum(mid) and the round-trip
                  capture per decile, i.e. what a two-sided quote keeps at the touch.

``YES`` is ``token1`` / ``payout_0`` (verified: corr(mid, payout_0) = +0.557 vs -0.557
swapped on a 0.5 % scan).

Sections are separate so each run stays inside the 300 s budget. Every section writes a
parquet and logs its own table, because console output dies with the terminal.

Usage
    uv run scripts/screen_flb_book.py --section book
    uv run scripts/screen_flb_book.py --section cal
    uv run scripts/screen_flb_book.py --section overround
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

import duckdb

from polymarketbot.logs import get_logger

DOME = "data/raw/dome/dome_polymarket_2025-10_2026-03.parquet"
LABELS = "data/processed/market_labels.parquet"
OUT = Path("data/processed")
LOG = get_logger("screen_flb_book")

# A snapshot is only used when both sides are quoted inside the unit interval. One-sided
# and crossed books are counted, not silently dropped.
CLEAN = ("{p}.best_bid > 0 AND {p}.best_ask < 1 AND {p}.best_ask > {p}.best_bid "
         "AND {p}.mid > 0 AND {p}.mid < 1")


def clean(alias: str = "") -> str:
    return CLEAN.format(p=alias) if alias else CLEAN.replace("{p}.", "")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads=6")
    con.execute("SET memory_limit='6GB'")
    con.execute("SET temp_directory='data/tmp_duck'")
    con.execute("SET preserve_insertion_order=false")
    return con


def table(rows: list[list[str]], cols: list[str]) -> None:
    """Log a fixed-width table; no pandas in this env."""
    widths = [len(c) for c in cols]
    for r in rows:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(v))
    LOG.info("  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)))
    LOG.info("  ".join("-" * w for w in widths))
    for r in rows:
        LOG.info("  ".join(v.ljust(widths[i]) for i, v in enumerate(r)))


# --------------------------------------------------------------------------- book


def section_book(con: duckdb.DuckDBPyConnection, *, sample: float = 5.0) -> None:
    t0 = time.time()
    LOG.info("book: scanning {} ({} % row sample, 0.05 price buckets)", DOME, sample)
    # Level-0 size without parsing JSON: "[p, s]" -> split on ',' then ']'.
    q = f"""
    CREATE OR REPLACE TABLE book_buckets AS
    SELECT
        LEAST(CAST(floor(mid * 20) AS INT), 19)                          AS bkt,
        count(*)                                                         AS n_clean,
        avg(spread)                                                      AS spread,
        approx_quantile(spread, 0.5)                                     AS spread_med,
        avg(mid - best_bid)                                              AS hs_bid,
        avg(best_ask - mid)                                              AS hs_ask,
        avg(bid_sz0)                                                     AS sz0_bid,
        avg(ask_sz0)                                                     AS sz0_ask,
        approx_quantile(bid_sz0, 0.5)                                    AS sz0_bid_med,
        approx_quantile(ask_sz0, 0.5)                                    AS sz0_ask_med,
        avg(total_bid_depth)                                             AS depth_bid,
        avg(total_ask_depth)                                             AS depth_ask,
        avg(n_bid_levels)                                                AS lv_bid,
        avg(n_ask_levels)                                                AS lv_ask,
        sum(total_ask_depth) / nullif(sum(total_bid_depth), 0)           AS depth_ratio,
        sum(ask_sz0) / nullif(sum(bid_sz0), 0)                           AS touch_ratio,
        approx_quantile(total_bid_depth, 0.5)                            AS depth_bid_med,
        approx_quantile(total_ask_depth, 0.5)                            AS depth_ask_med,
        approx_quantile(total_bid_depth * best_bid, 0.5)                 AS notl_bid_med,
        approx_quantile(total_ask_depth * best_ask, 0.5)                 AS notl_ask_med,
        approx_quantile(bid_sz0 * best_bid, 0.5)                         AS notl0_bid_med,
        approx_quantile(ask_sz0 * best_ask, 0.5)                         AS notl0_ask_med,
        avg(neg_risk::INT)                                               AS neg_risk
    FROM (
        SELECT mid, best_bid, best_ask, spread, total_bid_depth, total_ask_depth,
               n_bid_levels, n_ask_levels, neg_risk,
               TRY_CAST(split_part(split_part(top_bids_json, ',', 2), ']', 1) AS DOUBLE) AS bid_sz0,
               TRY_CAST(split_part(split_part(top_asks_json, ',', 2), ']', 1) AS DOUBLE) AS ask_sz0
        FROM (SELECT * FROM read_parquet('{DOME}') USING SAMPLE {sample} PERCENT (bernoulli))
        WHERE {clean()} AND mid IS NOT NULL
          AND top_bids_json IS NOT NULL AND top_asks_json IS NOT NULL
    )
    WHERE bid_sz0 IS NOT NULL AND ask_sz0 IS NOT NULL
    GROUP BY 1 ORDER BY 1
    """
    con.execute(q)
    res = con.execute("SELECT * FROM book_buckets ORDER BY bkt").fetchall()
    out = OUT / "flb_book_buckets.parquet"
    con.execute(f"COPY book_buckets TO '{out}' (FORMAT PARQUET)")
    LOG.info("book: {} buckets -> {}", len(res), out)

    rows = []
    for r in res:
        b = r[0]
        rows.append([f"{b*0.05:.2f}-{b*0.05+0.05:.2f}", f"{r[1]:,}",
                     f"{r[2]*100:.2f}", f"{r[3]*100:.2f}",
                     f"{r[8]:,.0f}", f"{r[9]:,.0f}",
                     f"{r[16]:,.0f}", f"{r[17]:,.0f}",
                     f"{r[20]:,.0f}", f"{r[21]:,.0f}",
                     f"{r[20]/r[21] if r[21] else float('nan'):.2f}",
                     f"{r[16]/r[17] if r[17] else float('nan'):.2f}",
                     f"{r[14]:.1f}", f"{r[15]:.2f}", f"{r[22]:.2f}"])
    table(rows, ["bucket", "n_clean", "spread_c", "spread_med_c",
                 "sz0_bid_med", "sz0_ask_med", "dbid_med_usd", "dask_med_usd",
                 "notl0_bid", "notl0_ask", "notl0_ratio", "depth_notl_ratio",
                 "depth_ratio_raw", "touch_ratio_raw", "negrisk"])
    LOG.info("book: done in {:.1f}s", time.time() - t0)


# ---------------------------------------------------------------------------- cal


HORIZONS = """
    CASE
        WHEN h < 0        THEN '00 post_end'
        WHEN h < 0.25     THEN '01 <15m'
        WHEN h < 1        THEN '02 15m-1h'
        WHEN h < 6        THEN '03 1-6h'
        WHEN h < 24       THEN '04 6-24h'
        WHEN h < 168      THEN '05 1-7d'
        ELSE                   '06 >7d'
    END
"""


def section_cal(con: duckdb.DuckDBPyConnection, *, side: str = "YES") -> None:
    t0 = time.time()
    LOG.info("cal: one observation per (market, token, horizon), side={}", side)
    q = f"""
    CREATE OR REPLACE TABLE cal_mkt AS
    WITH d AS (
        SELECT market_id, token_id, mid, best_bid, best_ask, timestamp_ms
        FROM read_parquet('{DOME}')
        WHERE token_side = '{side}' AND {clean()}
    ),
    l AS (
        SELECT market_id, end_date, payout_0, payout_1
        FROM read_parquet('{LABELS}')
        WHERE resolved AND payout_0 IS NOT NULL AND payout_1 IS NOT NULL
    ),
    j AS (
        SELECT d.market_id, d.token_id, d.mid, d.best_bid, d.best_ask, l.payout_0, l.payout_1,
               date_diff('second', to_timestamp(d.timestamp_ms / 1000.0), l.end_date) / 3600.0 AS h
        FROM d JOIN l USING (market_id)
    )
    SELECT
        market_id,
        token_id,
        {HORIZONS}                                     AS horizon,
        count(*)                                       AS n_snap,
        avg(mid)                                       AS p_mid,
        avg(best_bid)                                  AS p_bid,
        avg(best_ask)                                  AS p_ask,
        max(payout_0)                                  AS payout0,
        max(payout_1)                                  AS payout1
    FROM j
    WHERE h IS NOT NULL
    GROUP BY 1, 2, 3
    """
    con.execute(q)
    n = con.execute("SELECT count(*) FROM cal_mkt").fetchone()[0]
    LOG.info("cal: {} (market,token,horizon) rows in {:.1f}s", f"{n:,}", time.time() - t0)

    raw = con.execute(f"SELECT count(*) FROM read_parquet('{DOME}') WHERE token_side='{side}'"
                      ).fetchone()[0]
    used = con.execute("SELECT sum(n_snap) FROM cal_mkt").fetchone()[0]
    LOG.info("cal: snapshots side={} raw={:,} used={:,} ({:.1f} %)", side, raw, used,
             100 * used / raw)

    q2 = f"""
    CREATE OR REPLACE TABLE cal_table AS
    SELECT horizon,
           LEAST(CAST(floor(p_mid * 10) AS INT), 9) AS decile,
           count(*)                                  AS n_markets,
           avg(p_mid)                                AS implied_mid,
           avg(p_bid)                                AS implied_bid,
           avg(p_ask)                                AS implied_ask,
           avg(CASE WHEN '{side}' = 'YES' THEN payout0 ELSE payout1 END) AS realised,
           sum(n_snap)                               AS n_snap
    FROM cal_mkt GROUP BY 1, 2 ORDER BY 1, 2
    """
    con.execute(q2)
    res = con.execute("SELECT * FROM cal_table ORDER BY horizon, decile").fetchall()
    out = OUT / f"flb_cal_{side.lower()}.parquet"
    con.execute(f"COPY cal_table TO '{out}' (FORMAT PARQUET)")
    LOG.info("cal: table -> {}", out)

    rows = []
    for h, dec, n_mk, im, ib, ia, rp, ns in res:
        se = math.sqrt(max(rp * (1 - rp), 1e-12) / n_mk) if n_mk else float("nan")
        gap = rp - im
        t = gap / se if se else 0.0
        rows.append([h, f"{dec*0.1:.1f}-{dec*0.1+0.1:.1f}", f"{n_mk:,}",
                     f"{im:.4f}", f"{ib:.4f}", f"{ia:.4f}", f"{rp:.4f}",
                     f"{gap*100:+.2f}", f"{se*100:.2f}", f"{t:+.1f}",
                     f"{(rp-ia)*100:+.2f}", f"{(rp-ib)*100:+.2f}"])
    table(rows, ["horizon", "decile", "n_mkts", "p_mid", "p_bid", "p_ask",
                 "realised", "gap_pp", "se_pp", "t", "real-ask", "real-bid"])
    LOG.info("cal: done in {:.1f}s", time.time() - t0)


# ---------------------------------------------------------------------- overround


def section_overround(con: duckdb.DuckDBPyConnection, *, n_markets: int = 6000) -> None:
    """Pair YES/NO quotes inside one minute and measure the two-sided book.

    sum(bid) is what a maker is paid for selling both legs; sum(ask) is what a taker pays
    for both. 1 - sum(bid) is the round-trip capture at the touch; sum(mid) - 1 is the
    book's own overround.
    """
    t0 = time.time()
    LOG.info("overround: sampling {} quoted markets", n_markets)
    q = f"""
    CREATE OR REPLACE TABLE pair_sample AS
    WITH mk AS (
        SELECT market_id FROM (
            SELECT market_id, count(*) AS nq
            FROM read_parquet('{DOME}')
            WHERE best_ask > best_bid AND best_bid > 0 AND best_ask < 1
            GROUP BY 1
        ) WHERE nq > 50
        USING SAMPLE {n_markets} ROWS (reservoir, 42)
    ),
    snap AS (
        SELECT d.market_id, d.token_side,
               CAST(floor(d.timestamp_ms / 60000) AS BIGINT) AS minute,
               arg_max(d.best_bid, d.timestamp_ms) AS best_bid,
               arg_max(d.best_ask, d.timestamp_ms) AS best_ask,
               arg_max(d.mid,      d.timestamp_ms) AS mid
        FROM read_parquet('{DOME}') d
        JOIN mk USING (market_id)
        GROUP BY 1, 2, 3
    )
    SELECT y.market_id, y.minute,
           y.best_bid AS bid_y, y.best_ask AS ask_y, y.mid AS mid_y,
           n.best_bid AS bid_n, n.best_ask AS ask_n, n.mid AS mid_n
    FROM snap y JOIN snap n
      ON y.market_id = n.market_id AND y.minute = n.minute
     AND y.token_side = 'YES' AND n.token_side = 'NO'
    """
    con.execute(q)
    n = con.execute("SELECT count(*) FROM pair_sample").fetchone()[0]
    LOG.info("overround: {} paired minutes in {:.1f}s", f"{n:,}", time.time() - t0)

    q2 = """
    CREATE OR REPLACE TABLE overround_table AS
    SELECT LEAST(CAST(floor(mid_y * 10) AS INT), 9) AS decile,
           count(*)                                  AS n,
           avg(mid_y + mid_n)                        AS sum_mid,
           avg(bid_y + bid_n)                        AS sum_bid,
           avg(ask_y + ask_n)                        AS sum_ask,
           avg(1 - (bid_y + bid_n))                  AS capture_bid,
           avg((ask_y + ask_n) - 1)                  AS pay_ask,
           avg(ask_y - bid_y)                        AS spread_y,
           avg(ask_n - bid_n)                        AS spread_n
    FROM pair_sample
    WHERE bid_y > 0 AND ask_y < 1 AND ask_y > bid_y AND mid_y > 0 AND mid_y < 1
      AND bid_n > 0 AND ask_n < 1 AND ask_n > bid_n
    GROUP BY 1 ORDER BY 1
    """
    con.execute(q2)
    res = con.execute("SELECT * FROM overround_table ORDER BY decile").fetchall()
    out = OUT / "flb_overround.parquet"
    con.execute(f"COPY overround_table TO '{out}' (FORMAT PARQUET)")
    rows = []
    for dec, n_, sm, sb, sa, cap, pay, sp_y, sp_n in res:
        rows.append([f"{dec*0.1:.1f}-{dec*0.1+0.1:.1f}", f"{n_:,}", f"{sm:.4f}",
                     f"{sb:.4f}", f"{sa:.4f}", f"{(sm-1)*100:+.3f}", f"{cap*100:+.2f}",
                     f"{pay*100:+.2f}", f"{sp_y*100:.2f}", f"{sp_n*100:.2f}"])
    table(rows, ["decile", "n_pairs", "sum_mid", "sum_bid", "sum_ask", "overround_pp",
                 "capture_bid_pp", "pay_ask_pp", "spread_yes_c", "spread_no_c"])
    LOG.info("overround: done in {:.1f}s -> {}", time.time() - t0, out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", choices=["book", "cal", "overround"], required=True)
    ap.add_argument("--side", default="YES", choices=["YES", "NO"])
    ap.add_argument("--markets", type=int, default=6000)
    ap.add_argument("--sample", type=float, default=5.0)
    a = ap.parse_args()
    con = connect()
    if a.section == "book":
        section_book(con, sample=a.sample)
    elif a.section == "cal":
        section_cal(con, side=a.side)
    else:
        section_overround(con, n_markets=a.markets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
