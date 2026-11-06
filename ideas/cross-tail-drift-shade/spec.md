# Time-to-resolution premium: the tails are paid for, not forecast

claim:      Between 24 h and 168 h before `end_date`, tails are too extreme (measured at 168 h: longshots +7.6 c, favourites -11.3 c); shading size toward fading the tail beats the symmetric quote by at least 0.5 c/share.

mechanism:  Holders pay a premium for convexity: a 5 c lottery ticket is bought for the chance, and a 95 c near-certain dollar is sold to avoid waiting. A maker resting an ask at 5 c on a longshot is short convexity at a price above its expected value; a maker bidding 95 c on a favourite is long a near-certain dollar below par. The drift is the premium being paid, not a forecast of the outcome.

data:       `data/tape/universe/*.json` (end_date, question, daily_pool) + `clob_events` + `clob_trades`. Tape window 11-04 -> 11-06, so only conditions whose `end_date` falls inside it contribute; that is a hard sample limit and the spec must report the number of eligible conditions.

module:     `secs_to_end` from the signal; `price_band` from the current mid. In [24 h, 168 h) and mid <= 0.15: keep the YES ask at 1.0, SHRINK the YES bid to 0.25. In the same window and mid >= 0.85: keep the YES bid, SHRINK the ask. Otherwise quote both at 1.0. Never apply inside 24 h (pin risk takes over) or after `end_date`.

metric:     Paired per-condition net; secondary = the 24 h mid drift by price band, paired by condition, to confirm the effect is still there in fresh tape.

kill:       Kill if paired net <= 0 on two slices, or if the drift by price band is not monotone into the tails in fresh tape (the A7 effect was an artifact of the earlier sample), or if the reward foregone from one-sided quoting exceeds the drift captured.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-tail-drift-shade/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `secs_to_end` | seconds | end_date - now from the universe snapshot | 60 s |
| `price_band` | enum | 0 for mid<=0.15, 2 for mid>=0.85, 1 otherwise | decision-time |
| `drift_24h` | cents | signed 24 h mid change for the same condition | 60 s |

## prepare.sql

```sql
COPY (
  WITH u AS (
    SELECT unnest(markets) AS m
    FROM read_json('data/tape/universe/*.json', maximum_object_size=50000000)
  )
  SELECT DISTINCT m.condition_id,
         epoch(CAST(replace(m.end_date, ' ', 'T') AS TIMESTAMPTZ)) * 1000 AS end_ms,
         TRY_CAST(m.daily_pool AS DOUBLE) AS daily_pool
  FROM u
  WHERE m.end_date IS NOT NULL
) TO 'data/derived/ideas/cross-tail-drift-shade/signals.parquet' (FORMAT PARQUET);

-- secs_to_end is (end_ms - decision_ms)/1000 computed by the harness from this
-- static table; drift_24h is built from clob_events mids in the implementation.
```

## Runnable now

**yes, no new collector.** `data/tape/universe/*.json` end_date + `clob_events` mids are on the tape. Sample caveat: the tape is 2.2 days, so the tested conditions are those closing inside it; the 168 h bucket needs `end_date` >= 7 days after the slice, which most reward-universe conditions satisfy.

## Provenance

- `docs/alpha-pipeline.md` A7 and `docs/results-log.md`: at 168 h prices are too extreme in both tails, longshots +7.6 c (t 7.5), 0.90-1.00 favourites -11.3 c (t -7.7), recorded as a candidate because the spread on both crossings was not yet netted.
- `docs/strategies/screen_drift_book.py` is the existing confirmation harness; this idea is the maker-side version of the same effect.

## What would change our mind

If the drift disappears when the book (not traded prices) is used, A7 was a traded-price artifact and the tail premium does not exist for a quoter; the idea dies with it.
