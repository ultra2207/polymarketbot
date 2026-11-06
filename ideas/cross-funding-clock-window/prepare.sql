-- cross-funding-clock-window / signals
--
-- ONE hour partition: window 2026-11-05 23:00:00Z .. 2026-11-06 00:00:00Z, the
-- hour that ends on a funding settlement (funding_time_ms = 1793923200000 =
-- 11-06 00:00:00Z). binance_funding/date=2026-11-05/hour=23 holds 59 rows per
-- symbol, and clob_events/date=2026-11-05/hour=23 is read only for its distinct
-- condition_ids, so the table stays on markets the replay can see.
--
-- The +30 s after the settlement live in the *next* hour partition; a harness
-- slice that must see secs_to_funding > 0 needs hour=00 as well. That second
-- partition is deliberately not read here (one hour per query).
--
-- Grid: 5 s. The funding clock is public to the second; 5 s is enough to fire a
-- +/-60 s window rule and keeps the table small.
--
-- Signal semantics (as-of backwards):
--   secs_to_funding  signed seconds to the *nearest* of funding_time_ms and
--                    next_funding_time_ms, from the last funding row at or
--                    before the grid second. Positive = settlement ahead.
--   funding_apr      funding_rate * 3 * 365 * 100, percent per year (the rate is
--                    charged every 8 h, so 3 per day).
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
live AS (
    SELECT DISTINCT condition_id
    FROM read_parquet('data/tape/clob_events/date=2026-11-05/hour=23/*.parquet')
),
assets AS (
    SELECT u.symbol, tok.asset_id
    FROM u JOIN live USING (condition_id), unnest(u.clob_tokens) AS tok(asset_id)
    WHERE u.symbol IS NOT NULL AND len(u.clob_tokens) = 2
),
f AS (
    SELECT symbol,
           (venue_ts_ms / 1000)::BIGINT AS sec,
           funding_rate,
           funding_time_ms,
           next_funding_time_ms
    FROM read_parquet('data/tape/binance_funding/date=2026-11-05/hour=23/*.parquet')
    WHERE venue_ts_ms >= 1793919600000 AND venue_ts_ms < 1793923200000
      AND symbol IN (SELECT symbol FROM syms)
),
grid AS (
    SELECT s.symbol, g.sec
    FROM syms s, (SELECT unnest(range(1793919600, 1793923200, 5)) AS sec) g
),
j AS (
    SELECT grid.symbol,
           (grid.sec * 1000)::BIGINT AS ts_ms,
           f.funding_rate,
           f.funding_time_ms,
           f.next_funding_time_ms
    FROM grid
    ASOF LEFT JOIN f ON f.symbol = grid.symbol AND f.sec <= grid.sec
)
SELECT a.asset_id,
       j.ts_ms,
       CASE WHEN abs(j.funding_time_ms - j.ts_ms) <= abs(j.next_funding_time_ms - j.ts_ms)
            THEN (j.funding_time_ms - j.ts_ms)
            ELSE (j.next_funding_time_ms - j.ts_ms) END / 1000.0 AS secs_to_funding,
       j.funding_rate * 3 * 365 * 100 AS funding_apr
FROM j JOIN assets a USING (symbol)
WHERE j.funding_rate IS NOT NULL
  AND j.funding_time_ms IS NOT NULL
  AND j.next_funding_time_ms IS NOT NULL
