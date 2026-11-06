# The bonded proposal is the first capital-backed statement of the outcome

claim:      The gap between the PM mid and the proposed resolution price at the moment of ProposePrice predicts the next 30 minutes of PM drift; shading toward the proposal earns at least 0.5 c/share.

mechanism:  The proposer posts a bond on a specific outcome, which is the first public, capital-backed statement of resolution. If the PM mid disagrees materially, either the proposer or the market is wrong, and the bond-backed side has the better base rate: zero of 234 proposals in the tape window were disputed. The maker does not need to be right about the outcome, only to avoid adding exposure in the direction the market is about to correct.

data:       `uma_events` (condition_id, proposed_price, event, recv_ts_ns, block_number; 234 ProposePrice rows) + `clob_events` (mid on the same condition) + `derived/uma_events` question text.

module:     `uma_gap = mid - uma_proposed_p` for 30 min after a proposal. If `uma_gap > +5 c` the market is high relative to the bond: keep the ask, SHRINK the bid to 0.25. If `uma_gap < -5 c` mirror. Inside +/-5 c do nothing. The window is per-market state clocked on `ctx.seconds_elapsed`, and the module must ignore a proposal whose row arrives more than 30 min late.

metric:     Paired per-condition net; secondary = the regression of the forward 30 min PM mid change on `uma_gap`, per condition.

kill:       Kill if the forward-return coefficient on `uma_gap` is <= 0 (the proposal carries no information beyond the price), or paired net <= 0, or fewer than 20 proposals fall inside the tested slices.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-uma-proposal-gap/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `uma_gap` | cents | pm mid - proposed_p at the proposal timestamp | 30 min window |
| `uma_secs_since_proposal` | seconds | now - proposal recv_ts | 30 min window |
| `uma_proposed_p` | probability | proposed_price / 1e18 | 60 s |

## prepare.sql

```sql
COPY (
  WITH p AS (
    SELECT condition_id, recv_ts_ns AS prop_ts,
           TRY_CAST(proposed_price AS DOUBLE) / 1e18 AS uma_proposed_p
    FROM read_parquet('data/tape/uma_events/**/*.parquet')
    WHERE event = 'ProposePrice' AND condition_id IS NOT NULL
      AND TRY_CAST(proposed_price AS DOUBLE) IS NOT NULL
  ),
  pm AS (
    SELECT condition_id, venue_ts_ms,
           (best_bid + best_ask) / 2 AS pm_mid
    FROM read_parquet('data/tape/clob_events/**/*.parquet')
    WHERE event_type = 'best_bid_ask' AND best_bid > 0 AND best_ask < 1
  )
  SELECT p.condition_id, p.prop_ts / 1000000 AS ts_ms,
         p.uma_proposed_p,
         q.pm_mid - p.uma_proposed_p AS uma_gap
  FROM p
  ASOF JOIN pm q
    ON q.condition_id = p.condition_id
   AND q.venue_ts_ms <= p.prop_ts / 1000000
) TO 'data/derived/ideas/cross-uma-proposal-gap/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `uma_events.event|proposed_price|condition_id|recv_ts_ns` plus `clob_events.best_bid|best_ask`, both on the tape. 234 proposals in 46 hours is enough for a first paired test.

## Provenance

- `docs/data-collection.md` UMA decoded fields (`proposed_price` as a decimal string, `1e18` = 1.0) and the `ProposePrice` topic0 check against a live log.
- UMA dispute frequency in our own tape: 0 `DisputePrice` logs in `polygon_logs` for the 46-hour window, i.e. a bond-backed proposal is an unusually reliable statement.

## What would change our mind

If the gap has no forward predictive power, the market prices the proposal instantly and the only oracle edge is the exit-liquidity discount (the liveness idea), not the information in the bond.
