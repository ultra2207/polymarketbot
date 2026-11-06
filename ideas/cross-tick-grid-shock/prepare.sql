-- cross-tick-grid-shock / signals
--
-- Window: 2026-11-06 04:00:00Z .. 05:00:00Z (one hour partition of clob_events).
-- Grid:   1 s per asset that changed tick inside the window.
-- Rows:   one per (asset_id, second) - about 18 assets x 3600 s. Costs one
--         parquet directory; the price_change firehose is never read.
--
-- Signal semantics (the harness joins as-of backwards, so a row is valid from
-- its ts_ms until the next row for that asset):
--   secs_since_tick_change  seconds since the venue tick change; negative
--                           before it happened (spec: "negative before")
--   tick_size               tick in force at that second: new_tick after the
--                           change, old_tick before it
WITH ev AS (
    SELECT DISTINCT asset_id, condition_id, venue_ts_ms,
           old_tick_size AS old_tick, new_tick_size AS new_tick
    FROM read_parquet('data/tape/clob_events/date=2026-11-06/hour=04/*.parquet')
    WHERE event_type = 'tick_size_change'
      AND old_tick_size IS NOT NULL AND new_tick_size IS NOT NULL
      AND asset_id IS NOT NULL
      AND venue_ts_ms >= 1793937600000 AND venue_ts_ms < 1793941200000
),
one AS (                       -- one row per asset: its first and last change
    SELECT asset_id,
           min(venue_ts_ms) AS first_ms,
           min_by(old_tick, venue_ts_ms) AS first_old
    FROM ev GROUP BY asset_id
),
grid AS (
    SELECT o.asset_id, o.first_ms, o.first_old,
           (g.sec * 1000)::BIGINT AS ts_ms
    FROM one o, (SELECT unnest(range(1793937600, 1793941200)) AS sec) g
),
hit AS (
    SELECT g.asset_id, g.ts_ms, g.first_ms, g.first_old,
           e.venue_ts_ms AS change_ms, e.new_tick
    FROM grid g
    ASOF LEFT JOIN ev e
      ON e.asset_id = g.asset_id AND e.venue_ts_ms <= g.ts_ms
)
SELECT asset_id,
       ts_ms,
       CASE WHEN change_ms IS NULL THEN (ts_ms - first_ms) / 1000.0
            ELSE (ts_ms - change_ms) / 1000.0 END AS secs_since_tick_change,
       CAST(COALESCE(new_tick, first_old) AS DOUBLE) AS tick_size
FROM hit
