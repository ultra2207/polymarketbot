-- cross-kalshi-trade-lead-gate / signals
--
-- ONE hour partition of kalshi_trades (1,768 prints, 25 mapped tickers):
-- window 2026-11-06 04:00:00Z .. 05:00:00Z. The PM side is not read here: the
-- mapping artifact supplies ticker -> (condition, YES token, NO token).
--
-- Grid: one row per (token, second), flow carried as 0 for seconds with no
-- print, so the rolling windows are wall-clock windows and not "last 5 prints".
--
-- Signal semantics (as-of backwards):
--   signed_flow_5s  signed contracts traded on Kalshi in the trailing 5 s,
--                   YES-equivalent (taker YES +count, taker NO -count), sign
--                   flipped when the mapping was audited as inverted
--   flow_z          (signed_flow_5s - 5*mean_1s) / (sqrt(5)*sd_1s), the 1 s mean
--                   and sd taken over the trailing 1800 s (30 min). The hour
--                   starts cold, so the first 30 min have a short baseline.
--   kalshi_age_s    seconds since the last Kalshi print for that ticker
--   is_block        1 if any print in the trailing 5 s was flagged
--                   is_block_trade. This hour has 0 block prints, so the column
--                   is all zeros until a slice with a block is used.
WITH map AS (
    SELECT kalshi_ticker,
           polymarket_condition_id AS condition_id,
           polymarket_yes_token AS yes_token,
           polymarket_no_token AS no_token,
           CASE WHEN direction = 'normal' THEN 1.0 ELSE -1.0 END AS dir
    FROM read_parquet('data/derived/kalshi_mapping_v2.parquet')
    WHERE confidence >= 0.6
),
assets AS (
    SELECT kalshi_ticker, dir, yes_token AS asset_id FROM map
    UNION
    SELECT kalshi_ticker, dir, no_token FROM map
),
prints AS (                    -- one row per (ticker, second) with at least one print
    SELECT t.ticker,
           (t.recv_ts_ns / 1000000000)::BIGINT AS sec,
           sum(CASE WHEN lower(t.taker_side) = 'yes' THEN t.count ELSE -t.count END) AS flow,
           max(CASE WHEN t.is_block_trade THEN 1 ELSE 0 END) AS blk,
           max(t.recv_ts_ns) AS last_ns
    FROM read_parquet('data/tape/kalshi_trades/date=2026-11-06/hour=04/*.parquet') t
    WHERE t.recv_ts_ns / 1000000 >= 1793937600000 AND t.recv_ts_ns / 1000000 < 1793941200000
      AND t.ticker IN (SELECT kalshi_ticker FROM map)
    GROUP BY 1, 2
),
grid AS (
    SELECT a.asset_id, a.kalshi_ticker, a.dir,
           (g.sec * 1000)::BIGINT AS ts_ms, g.sec
    FROM assets a, (SELECT unnest(range(1793937600, 1793941200)) AS sec) g
),
filled AS (
    SELECT grid.asset_id, grid.ts_ms, grid.sec, grid.kalshi_ticker, grid.dir,
           COALESCE(p.flow, 0) AS flow, COALESCE(p.blk, 0) AS blk, p.last_ns
    FROM grid LEFT JOIN prints p
      ON p.ticker = grid.kalshi_ticker AND p.sec = grid.sec
),
roll AS (
    SELECT *,
           sum(flow) OVER w5 AS flow5,
           max(blk) OVER w5 AS block5,
           max(last_ns) OVER wall AS last_print_ns,
           avg(flow) OVER w30 AS mean1,
           stddev_samp(flow) OVER w30 AS sd1
    FROM filled
    WINDOW w5 AS (PARTITION BY kalshi_ticker ORDER BY sec
                  ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
           w30 AS (PARTITION BY kalshi_ticker ORDER BY sec
                   ROWS BETWEEN 1799 PRECEDING AND CURRENT ROW),
           wall AS (PARTITION BY kalshi_ticker ORDER BY sec
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
)
SELECT asset_id,
       ts_ms,
       dir * flow5 AS signed_flow_5s,
       CASE WHEN sd1 > 0 THEN (flow5 - 5 * mean1) / (sqrt(5) * sd1) ELSE 0 END AS flow_z,
       (ts_ms - last_print_ns / 1000000) / 1000.0 AS kalshi_age_s,
       CAST(block5 AS BIGINT) AS is_block
FROM roll
WHERE last_print_ns IS NOT NULL
