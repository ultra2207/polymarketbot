# UMA liveness window: the losing token is a discount that decays to zero

claim:      After a ProposePrice lands on a condition, the losing side decays toward 0 and the proposed side toward 1; quoting a one-tick ask on the proposed side and pulling the other for the remainder of the liveness window earns at least 1 c/share.

mechanism:  Dispute risk decays with elapsed liveness because no dispute means settlement. Holders of the losing token are locked in and will sell at a discount to zero, and sellers of the winning token want out before redemption. The maker who supplies that exit earns the residual discount, and the pull on the other side avoids the asymmetric tail of a late dispute.

data:       `uma_events` (condition_id, question, event, proposed_price, request_timestamp, expiry, disputed, block_number, recv_ts_ns; 272 rows 11-05 05:59 -> 11-06 04:10, of which 234 ProposePrice and 38 RequestPrice) + `clob_events`.

module:     `uma_phase` is 1 after RequestPrice, 2 after ProposePrice, 3 after settlement. If phase >= 2 and `uma_secs_to_expiry > 0`: quote the token matching `uma_proposed_p` on both sides at one tick, and PULL the opposite token entirely; size at the reward floor. If `uma_proposed_p = 0.5` (a split proposal) quote both normally, because there is no claim to converge to. On phase 3 or expiry: resume both sides.

metric:     Paired per-condition net; secondary = mean (1 - mid) on the proposed side across the window, which is the discount actually harvested.

kill:       Kill if the median same-side edge is < 0.5 c/share after fees, or if fewer than 20 conditions reach phase 2 inside the tested slices (needs_data; do not report a mean on 5 events), or if the forfeited reward on the pulled side exceeds the harvested discount in > 50 % of conditions.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-uma-liveness-convergence/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `uma_phase` | enum 1/2/3 | uma_events.event sequence per condition_id | 60 s |
| `uma_proposed_p` | probability | proposed_price / 1e18 | 60 s |
| `uma_secs_to_expiry` | seconds | (expiry - now)/1000, negative after expiry | 60 s |

## prepare.sql

```sql
COPY (
  WITH e AS (
    SELECT condition_id, question, recv_ts_ns, event,
           TRY_CAST(proposed_price AS DOUBLE) / 1e18 AS proposed_p,
           expiry,
           CASE event WHEN 'RequestPrice' THEN 1 WHEN 'ProposePrice' THEN 2 ELSE 3 END AS phase,
           CASE lower(event) WHEN 'settle' THEN 1 ELSE 0 END AS settled
    FROM read_parquet('data/tape/uma_events/**/*.parquet')
    WHERE condition_id IS NOT NULL
  ),
  seq AS (
    SELECT condition_id, question, recv_ts_ns, proposed_p, expiry,
           max(phase) OVER (PARTITION BY condition_id ORDER BY recv_ts_ns
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS uma_phase,
           max(settled) OVER (PARTITION BY condition_id ORDER BY recv_ts_ns
                              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS did_settle
    FROM e
  )
  SELECT condition_id, recv_ts_ns AS ts_ms,
         CASE WHEN did_settle = 1 THEN 3 ELSE uma_phase END AS uma_phase,
         last_value(proposed_p IGNORE NULLS) OVER (PARTITION BY condition_id ORDER BY recv_ts_ns
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS uma_proposed_p,
         (expiry - recv_ts_ns / 1000000) / 1000.0 AS uma_secs_to_expiry
  FROM seq
) TO 'data/derived/ideas/cross-uma-liveness-convergence/signals.parquet' (FORMAT PARQUET);

-- The harness must forward-fill this table to the decision clock: the last row
-- per condition is valid until the next uma_events row for that condition.
```

## Runnable now

**yes, no new collector.** `uma_events.condition_id|event|proposed_price|expiry|recv_ts_ns` is on the tape (272 rows, 11-05 05:59 onward). The request->proposal->settle sequence is already decoded by `scripts/replay_uma.py`, which also fills `condition_id` and `question`.

## Provenance

- UMA Optimistic Oracle V2 lifecycle and the 2-hour liveness window: `docs/data-collection.md` UMA section; `docs/institutional-exogenous-market-making.md` section 2.3 (`P_settle(t)` decaying with the hazard of a dispute).
- Polymarket resolution documentation: proposal, dispute, settle; a resolved market redeems at $1/$0.

## What would change our mind

If the discount on the proposed side is under one tick, the market has already converged by the time the proposal is on-chain and there is nothing to harvest; the whole oracle-timing family then reduces to an exit-liquidity service, not an edge.
