# Funding clock: a known timestamp, not a forecast

claim:      The 120 s around a funding timestamp carries a mechanical PM taker burst; shrinking both sides in that window is net positive after the reward it forfeits.

mechanism:  Funding settlement at a known millisecond forces position rolls and basis capture on the perp, which transmits to the underlying and to the crypto binary. The arrival time is public in advance, so this is a calendar rule with no forecast and a computable reward cost, because the window is about 1 % of the day.

data:       `binance_funding` (funding_time_ms, next_funding_time_ms, symbol, funding_rate; 60 s) + `clob_events`. The timestamps are read from the tape, never hard-coded.

module:     `secs_to_funding` is signed seconds to the nearest `funding_time_ms`. |s| <= 60 -> SHRINK both to 0.25; -15 <= s <= +30 -> PULL both; else QUOTE both. The rule is a clock, so the module carries no other state, and it must not fire on a funding row older than 120 s.

metric:     Paired per-condition net, with reward foregone and markout as separate columns: the claim is an explicit trade of one for the other.

kill:       Kill if window fills are not worse in markout by >= 0.3 c/share than matched non-window fills, or if net after subtracting reward foregone is not > 0, or if fewer than 12 funding events fall inside the tested slices.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-funding-clock-window/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `secs_to_funding` | seconds | nearest binance_funding.funding_time_ms - ts (signed) | 60 s |
| `funding_apr` | percent/year | binance_funding.funding_rate * 3 * 365 * 100 | 60 s |

## prepare.sql

```sql
-- 1) asset map: which Binance symbol prices each crypto condition. One row per
--    condition whose question names a covered underlying; a condition with no
--    parsable symbol gets no row and is never quoted on this signal.
COPY (
  SELECT m.condition_id,
         CASE
           WHEN lower(m.question) LIKE '%bitcoin%'  OR lower(m.slug) LIKE '%btc%' THEN 'BTCUSDT'
           WHEN lower(m.question) LIKE '%ethereum%' OR lower(m.slug) LIKE '%eth%' THEN 'ETHUSDT'
           WHEN lower(m.question) LIKE '%solana%'   OR lower(m.slug) LIKE '%sol%' THEN 'SOLUSDT'
         END AS symbol
  FROM (SELECT unnest(markets) AS m
        FROM read_json('data/tape/universe/*.json', maximum_object_size=50000000)) x
)
WHERE symbol IS NOT NULL
  TO 'data/derived/ideas/cross-funding-clock-window/asset_map.parquet' (FORMAT PARQUET);

COPY (
  WITH f AS (
    SELECT symbol, venue_ts_ms, funding_time_ms, next_funding_time_ms,
           funding_rate * 3 * 365 * 100 AS funding_apr
    FROM read_parquet('data/tape/binance_funding/**/*.parquet')
    WHERE symbol IN ('BTCUSDT','ETHUSDT','SOLUSDT')
  ),
  d AS (
    SELECT *,
           CASE WHEN abs(funding_time_ms - venue_ts_ms) <= abs(next_funding_time_ms - venue_ts_ms)
                THEN funding_time_ms ELSE next_funding_time_ms END AS near_ms
    FROM f
  ),
  map AS (SELECT * FROM read_parquet('data/derived/ideas/cross-funding-clock-window/asset_map.parquet'))
SELECT map.condition_id, d.venue_ts_ms AS ts_ms,
       (d.near_ms - d.venue_ts_ms) / 1000.0 AS secs_to_funding,
       d.funding_apr
FROM d JOIN map USING (symbol)
) TO 'data/derived/ideas/cross-funding-clock-window/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `binance_funding.funding_time_ms|next_funding_time_ms|venue_ts_ms` is on the tape. A 10-minute slice contains a funding event only if it is chosen at 00:00, 08:00 or 16:00 UTC, so the scheduler must select such a slice explicitly.

## Provenance

- Funding mechanics: `docs/data-collection.md` Binance stream; arXiv 2212.06888.
- Scheduled arming is the same rule class as `docs/institutional-exogenous-market-making.md` section 2.1 (pre-scheduled timers cancel passive quotes seconds before a release).

## What would change our mind

If the window shows no extra adverse flow, either the funding flow stays on the perp or PM takers are slow enough that the second is irrelevant; the rule is then deleted rather than tuned.
