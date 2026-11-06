# Proposal expiry is the one instant when a resolved outcome can flip

claim:      The +/-10 min around a proposal expiry carries a jump-risk premium; pulling both sides in that window is net positive after the reward it forfeits.

mechanism:  Expiry is the single instant at which the resolution can flip from the proposal to a dispute, and the flip is binary and total (0 to 1, or 1 to 0). A resting quote at that instant has unbounded binary risk, while the timestamp is public in advance. The per-event premium is small precisely because disputes are rare, so the test is the reward-versus-risk trade, not the mean jump.

data:       `uma_events` (expiry, event, disputed, condition_id; 234 proposals with an expiry) + `clob_events`. Disputes are currently zero on the tape, which sets the expected cost of the risk, not the size of the sample.

module:     `uma_secs_to_expiry` signed. |s| <= 600 -> SHRINK both to 0.25; -60 <= s <= +120 -> PULL both; after s > 120 resume. If a `disputed` row appears for the condition, PULL both for 60 min (the escalation rule, which is the only part of this idea that survives if disputes ever appear).

metric:     Paired per-condition net; report reward foregone per condition and the number of expiry windows covered as separate columns.

kill:       Kill if reward foregone exceeds markout saved in >= 2/3 of covered conditions, or if fewer than 10 expiry events fall inside the tested slices (no power; keep it as a forward-armed monitor rather than a result).

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-uma-expiry-clock/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `uma_secs_to_expiry` | seconds | (expiry - now)/1000, signed | 60 s |
| `uma_phase` | enum 1/2/3 | uma_events sequence per condition | 60 s |
| `uma_disputed` | 0/1 | uma_events.disputed / DisputePrice presence | 60 s |

## prepare.sql

```sql
COPY (
  WITH e AS (
    SELECT condition_id, event, expiry, disputed, recv_ts_ns,
           max(CASE WHEN event = 'ProposePrice' THEN recv_ts_ns END)
             OVER (PARTITION BY condition_id) AS prop_ts
    FROM read_parquet('data/tape/uma_events/**/*.parquet')
    WHERE condition_id IS NOT NULL
  ),
  p AS (
    SELECT DISTINCT condition_id, expiry, prop_ts
    FROM e WHERE prop_ts IS NOT NULL AND expiry IS NOT NULL
  ),
  d AS (
    SELECT condition_id, max(CASE WHEN disputed THEN 1 ELSE 0 END) AS uma_disputed
    FROM e GROUP BY 1
  )
  SELECT p.condition_id,
         p.prop_ts / 1000000 AS ts_ms,
         (p.expiry - p.prop_ts / 1000000) / 1000.0 AS secs_window,
         p.expiry,
         coalesce(d.uma_disputed, 0) AS uma_disputed
  FROM p LEFT JOIN d USING (condition_id)
) TO 'data/derived/ideas/cross-uma-expiry-clock/signals.parquet' (FORMAT PARQUET);

-- The decision-time signal `uma_secs_to_expiry` = (expiry - decision_ms)/1000;
-- the harness computes it from this table, so the expiry is a static fact per
-- condition and never a tape read inside decide().
```

## Runnable now

**yes, no new collector.** `uma_events.expiry|disputed|condition_id|recv_ts_ns` is on the tape. Population: 234 proposals, so about 234 expiry instants exist; a 10-minute slice covers one only if it is chosen around an expiry.

## Provenance

- UMA Optimistic Oracle liveness/expiry mechanics: `docs/data-collection.md` (request -> proposal -> expiry -> settle).
- The binary asymmetry of settlement risk is the argument in `docs/institutional-exogenous-market-making.md` section 3.2 (expected PnL of resting into a jump).

## What would change our mind

If the expiry windows are indistinguishable from matched non-window minutes, the dispute option is priced by nobody and the pull is pure reward loss; the rule dies until a dispute actually appears in `polygon_logs`.
