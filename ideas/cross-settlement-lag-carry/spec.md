# Post-end-date discount: lock the value, sell the wait

claim:      After `end_date` but before resolution, the leading token trades below its locked value; quoting a tight ask on the leading token during the lag window captures at least 0.5 c/share of that discount.

mechanism:  The value is locked but not redeemable: sports resolve a median +4.1 h after `end_date` (p90 +5.8 h) while crypto resolves in about 0 h. Holders who need cash, or who do not know how to redeem, sell at a discount. The maker supplying that exit earns the discount, and there is no outcome risk, only resolution-mechanics risk.

data:       `data/tape/universe/*.json` (condition_id, end_date, question, resolution_source; hourly snapshots) + `uma_events` (settlement per condition) + `clob_events`. Lag distribution prior: `docs/alpha-pipeline.md` A6 (+4.1 h median, p90 +5.8 h for sports).

module:     `secs_since_end_date` from the signal, `uma_resolved` from the settle presence. When `0 < secs_since_end_date < 21600` and `uma_resolved = 0` and mid >= 0.9 (or <= 0.1): quote the certain token on both sides at one tick, PULL the other, and cap size at 0.5 of normal because the risk is mechanics, not price. Outside the window, or after resolution, quote normally.

metric:     Mean locked discount captured per fill on the certain side net of fees, plus paired per-condition net versus `queue_aware_ofi`.

kill:       Kill if the median discount on the certain side is < 0.5 c, or if the median lag (`end_date` -> resolution) is < 30 min in the tested conditions (no window), or if any fill in the window shows markout consistent with a resolution reversal (the certain side was not certain).

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-settlement-lag-carry/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `secs_since_end_date` | seconds | now - manifest end_date, negative before end | 60 s |
| `uma_resolved` | 0/1 | presence of a settle row for the condition | 60 s |
| `lag_class` | enum | 0 crypto-like (<30m), 1 sports-like, 2 unknown | static |

## prepare.sql

```sql
COPY (
  WITH u AS (
    SELECT unnest(markets) AS m
    FROM read_json('data/tape/universe/*.json', maximum_object_size=50000000)
  ),
  c AS (
    SELECT m.condition_id, m.question,
           CAST(replace(m.end_date, ' ', 'T') AS TIMESTAMPTZ) AS end_ts,
           m.resolution_source
    FROM u
    WHERE m.end_date IS NOT NULL
  ),
  s AS (
    SELECT condition_id, max(CASE WHEN lower(event) = 'settle' THEN 1 ELSE 0 END) AS uma_resolved
    FROM read_parquet('data/tape/uma_events/**/*.parquet')
    GROUP BY 1
  )
  SELECT DISTINCT c.condition_id,
         epoch(c.end_ts) * 1000 AS end_ms,
         coalesce(s.uma_resolved, 0) AS uma_resolved,
         CASE WHEN lower(c.question) LIKE '%nfl%' OR lower(c.question) LIKE '%nba%'
                OR lower(c.question) LIKE '%vs%' OR lower(c.question) LIKE '%win%'
              THEN 1
              WHEN lower(c.question) LIKE '%bitcoin%' OR lower(c.question) LIKE '%ethereum%' THEN 0
              ELSE 2 END AS lag_class
  FROM c LEFT JOIN s USING (condition_id)
) TO 'data/derived/ideas/cross-settlement-lag-carry/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `data/tape/universe/*.json` (end_date, question, resolution_source) and `uma_events.event` are on the tape. The test needs slices that cross an `end_date`, so the scheduler must pick an hour containing one, or run a 60-minute slice around a known close.

## Provenance

- `docs/alpha-pipeline.md` A6 (resolution-timing premium): median +4.1 h, p90 +5.8 h between end_date and resolution for sports, ~0 for crypto.
- `docs/data-collection.md` resolution mechanics; `docs/strategy-thesis.md` records that the operator-facing behaviour is exiting, not holding, which is what creates the discount.

## What would change our mind

If the discount is under a tick, the lag is fully arbitraged and the idea is only a redemption service; the family then shrinks to the pre-resolution drift (the tail-drift idea).
