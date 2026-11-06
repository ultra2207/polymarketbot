-- cross-binance-microprice-lead / signals
--
-- Window: 2026-11-06 04:00:00Z .. 05:00:00Z. Reads one binance_bbo hour
-- partition (~126 k rows) plus the distinct condition_ids of one clob_events
-- hour partition, to keep the output to conditions the replay can actually see.
--
-- Symbol map: newest universe snapshot per condition; a condition whose
-- question/slug names BTC, ETH or SOL is priced by that pair. Order is fixed
-- (BTC, then ETH, then SOL) so a market naming two underlyings picks BTC.
-- BTCUSDT/ETHUSDT/SOLUSDT are the three symbols that exist in binance_bbo.
--
-- Signal semantics (as-of backwards; a row stays valid until the next row):
--   microprice_dev_bps  (micro - mid) / mid * 1e4 on the Binance book, where
--                       micro = (bid*ask_qty + ask*bid_qty) / (bid_qty+ask_qty)
--   sigma_60s_bps       stddev of 1 s log mid returns over the trailing 60 s,
--                       in bps; 0 when the last minute was flat
WITH syms AS (SELECT unnest(['BTCUSDT', 'ETHUSDT', 'SOLUSDT']) AS symbol),
u AS (
    SELECT m.condition_id, m.question, m.slug, m.clob_tokens,
           CASE
             WHEN regexp_matches(lower(m.question), 'bitcoin|\bbtc\b')
               OR regexp_matches(lower(m.slug), '(^|-)btc(-|$)') THEN 'BTCUSDT'
             WHEN regexp_matches(lower(m.question), 'ethereum|\beth\b|\bweth\b')
               OR regexp_matches(lower(m.slug), '(^|-)eth(-|$)') THEN 'ETHUSDT'
             WHEN regexp_matches(lower(m.question), 'solana|\bsol\b')
               OR regexp_matches(lower(m.slug), '(^|-)sol(-|$)') THEN 'SOLUSDT'
           END AS symbol
    FROM read_json('data/tape/universe/*.json', maximum_object_size=50000000) f,
         unnest(f.markets) t(m)
    QUALIFY row_number() OVER (PARTITION BY m.condition_id ORDER BY f.generated_at DESC) = 1
),
live AS (                      -- conditions with at least one book frame in the hour
    SELECT DISTINCT condition_id
    FROM read_parquet('data/tape/clob_events/date=2026-11-06/hour=04/*.parquet')
),
assets AS (
    SELECT u.symbol, tok.asset_id
    FROM u JOIN live USING (condition_id), unnest(u.clob_tokens) AS tok(asset_id)
    WHERE u.symbol IS NOT NULL AND len(u.clob_tokens) = 2
),
bbo AS (                       -- one row per (symbol, second): last tick of the second
    SELECT symbol,
           (venue_ts_ms / 1000)::BIGINT AS sec,
           arg_max((bid * ask_qty + ask * bid_qty) / nullif(bid_qty + ask_qty, 0),
                   venue_ts_ms) AS micro,
           arg_max((bid + ask) / 2.0, venue_ts_ms) AS mid
    FROM read_parquet('data/tape/binance_bbo/date=2026-11-06/hour=04/*.parquet')
    WHERE venue_ts_ms >= 1793937600000 AND venue_ts_ms < 1793941200000
      AND symbol IN (SELECT symbol FROM syms)
      AND bid > 0 AND ask > bid AND bid_qty > 0 AND ask_qty > 0
    GROUP BY 1, 2
),
grid AS (
    SELECT s.symbol, g.sec
    FROM syms s, (SELECT unnest(range(1793937600, 1793941200)) AS sec) g
),
filled AS (                    -- dense 1 s grid, carried forward inside the hour
    SELECT grid.symbol, grid.sec,
           last_value(bbo.micro IGNORE NULLS) OVER w AS micro,
           last_value(bbo.mid IGNORE NULLS) OVER w AS mid
    FROM grid LEFT JOIN bbo ON bbo.symbol = grid.symbol AND bbo.sec = grid.sec
    WINDOW w AS (PARTITION BY grid.symbol ORDER BY grid.sec
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
),
rets AS (
    SELECT symbol, sec, micro, mid,
           ln(mid / lag(mid) OVER (PARTITION BY symbol ORDER BY sec)) AS r1
    FROM filled
    WHERE mid IS NOT NULL
),
stats AS (
    SELECT symbol, sec,
           (micro - mid) / mid * 1e4 AS microprice_dev_bps,
           COALESCE(stddev_samp(r1) OVER (PARTITION BY symbol ORDER BY sec
                     ROWS BETWEEN 59 PRECEDING AND CURRENT ROW), 0) * 1e4 AS sigma_60s_bps
    FROM rets
)
SELECT a.asset_id,
       (s.sec * 1000)::BIGINT AS ts_ms,
       s.microprice_dev_bps AS microprice_dev_bps,
       s.sigma_60s_bps AS sigma_60s_bps
FROM stats s JOIN assets a USING (symbol)
