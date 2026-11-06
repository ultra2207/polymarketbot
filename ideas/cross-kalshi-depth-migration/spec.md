# Kalshi depth withdrawal: the maker who leaves there is the one who would have absorbed our flow

claim:      When Kalshi top-5-cent bid depth falls by >= 50 % within 30 s, PM fills in the next 30 s are more adverse by at least 0.8 c/share; pulling PM quotes in that window raises net.

mechanism:  Kalshi and Polymarket are two venues for the same claim, and the balance sheet that provides liquidity on both pulls from the thinner one first. The PM book cannot observe that decision: the depth is on the other venue, so this is information our own context structurally cannot contain (unlike a widening of our own spread). PM is the softer book once the shared maker has left, so the informed flow that arrives next is absorbed by us. This is capital migration between venues, not a price forecast, and the ladder across depth-change deciles is the primary test.

data:       `kalshi_books` (yes_bid_size_1c, yes_bid_size_2c, yes_bid_size_5c, yes_bid_size_10c and the no_* mirrors, yes_bids, no_bids, recv_ts_ns; 5 s) + mapping artifact + `clob_events`.

module:     `k_depth5c_ratio` is current depth over the trailing 5 min median. If the side that would absorb the flow thins (NO bids collapsing means YES buyers are unprotected, so the YES token is about to move up): SHRINK the YES ask to 0.25 for 30 s. If both sides thin below 0.5: PULL both. Re-entry on ratio > 0.8. Stale > 6 s -> QUOTE both.

metric:     Paired per-condition net, plus the PM markout ladder across Kalshi depth-change deciles (monotone is the mechanism test).

kill:       Kill if the markout ladder across depth-change deciles is flat or inverted (spread < 0.3 c/share between extreme deciles), or paired net <= 0, or if fewer than 30 depth-collapse episodes exist in the slice.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-kalshi-depth-migration/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `k_depth5c_ratio_yes` | ratio | yes_bid_size_5c / 5 min median | 6 s |
| `k_depth5c_ratio_no` | ratio | no_bid_size_5c / 5 min median | 6 s |
| `k_depth_age_ms` | ms | decision ts - book recv_ts_ns | 6 s |

## prepare.sql

```sql
-- map: condition -> kalshi ticker + direction, from the audited mapping artifact
-- (scripts/build_kalshi_mapping.py; 107 rows, refreshed hourly by the collector).
-- trend = 'yes_yes' means Kalshi YES and PM YES are the same claim; 'yes_no'
-- means Kalshi YES is PM NO, so every signal below is negated for that row.
COPY (
  SELECT polymarket_condition_id AS condition_id,
         kalshi_ticker,
         direction,
         confidence,
         method
  FROM read_parquet('data/derived/kalshi_mapping_v2.parquet')
  WHERE confidence >= 0.6
) TO 'data/derived/ideas/cross-kalshi-depth-migration/kalshi_map.parquet' (FORMAT PARQUET);

COPY (
  WITH k AS (
    SELECT ticker, recv_ts_ns, yes_bid_size_5c, no_bid_size_5c,
           median(yes_bid_size_5c) OVER (PARTITION BY ticker ORDER BY recv_ts_ns
             RANGE BETWEEN 300000000000 PRECEDING AND CURRENT ROW) AS med_yes,
           median(no_bid_size_5c)  OVER (PARTITION BY ticker ORDER BY recv_ts_ns
             RANGE BETWEEN 300000000000 PRECEDING AND CURRENT ROW) AS med_no
    FROM read_parquet('data/tape/kalshi_books/**/*.parquet')
  ),
  m AS (SELECT * FROM read_parquet('data/derived/ideas/cross-kalshi-depth-migration/kalshi_map.parquet'))
SELECT m.condition_id, k.recv_ts_ns AS ts_ms,
       k.yes_bid_size_5c / nullif(k.med_yes, 0) AS k_depth5c_ratio_yes,
       k.no_bid_size_5c  / nullif(k.med_no, 0)  AS k_depth5c_ratio_no
FROM k JOIN m ON m.kalshi_ticker = k.ticker
) TO 'data/derived/ideas/cross-kalshi-depth-migration/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `kalshi_books.yes_bid_size_5c|no_bid_size_5c|recv_ts_ns` at 5 s (179 k rows, from 11-05 19:11) + the mapping artifact.

## Provenance

- `docs/kalshi-hypotheses.md` H6 (liquidity migration) states the test as a monotone ladder over depth-change deciles.
- `docs/kalshi-api-notes.md` documents the 5 s orderbook poll and the 1c/2c/5c/10c depth buckets the collector persists.

## What would change our mind

If the ladder is flat, makers on the two venues are disjoint populations and only prices (not depth) transmit; H6 dies and with it the liquidity-migration family.
