# Whale prints are late, but they are positioning, not speed

claim:      Whale net accumulation over 24 h predicts the same-direction PM mid move over the next 24 h; tilting size toward the whales side earns at least 0.4 c/share.

mechanism:  The Data API print is up to an hour late, so this cannot be a speed signal. It can be positioning: a large wallet accumulating at the ask pays the spread repeatedly, which only pays if it expects a reprice, and the PM mid lags by hours because the flow is absorbed by resting makers. The maker earns by not selling to a buyer it believes is informed.

data:       `whale_activity` (wallet, condition_id, asset_id, side, size, price, usdc_size, activity_ts_ms, activity_type; 1 h poll, 161,083 rows, all `activity_type = TRADE`) + `clob_events`. Limitation to state: 1 h polling means rows appear late, and the 24 h forward window is often outside a replay slice, in which case the idea is a portfolio tilt, not a maker rule.

module:     `whale_net_usd_24h` signed and YES-equivalent; `whale_z` normalised by the condition own 24 h traded notional. If `whale_net_usd_24h` is in the top decile: SHRINK the YES ask to 0.5 and keep the bid at 1.0 (do not sell to the accumulator); bottom decile -> mirror. The module reloads only at hour boundaries and must never interpolate within the hour: pretending to faster information would manufacture a result the data cannot support.

metric:     Paired per-condition net; secondary = the regression of the forward 24 h mid change on `whale_net_usd_24h`, per condition (t on the condition unit).

kill:       Kill if the forward-return sign agreement is <= 50 % (no positioning information), or paired net <= 0, or the measurable forward window never lands inside a replay slice (structural: then it is a portfolio tilt and must be re-specified, not tuned).

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-whale-slow-accumulation/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `whale_net_usd_24h` | USD | signed sum(usdc_size) over trailing 24 h, YES-equivalent | 1 h |
| `whale_z_24h` | sigma units | whale_net_usd_24h vs the condition own trade notional | 1 h |
| `whale_flow_age_s` | seconds | now - newest whale_activity.recv_ts_ns | 1 h |

## prepare.sql

```sql
COPY (
  WITH w AS (
    SELECT condition_id, recv_ts_ns,
           sum(CASE WHEN lower(side) = 'buy' THEN usdc_size ELSE -usdc_size END)
             OVER (PARTITION BY condition_id ORDER BY recv_ts_ns
                   RANGE BETWEEN 86400000000000 PRECEDING AND CURRENT ROW) AS whale_net_usd_24h,
           min(activity_ts_ms) OVER (PARTITION BY condition_id) AS first_ms
    FROM read_parquet('data/tape/whale_activity/**/*.parquet')
    WHERE activity_type = 'TRADE'
  ),
  v AS (      -- the condition own 24 h traded notional, for the z-score denominator
    SELECT condition_id, (venue_ts_ms / 3600000) * 3600000 AS hour_ms,
           sum(price * size) AS traded_usd
    FROM read_parquet('data/tape/clob_trades/**/*.parquet')
    GROUP BY 1, 2
  )
  SELECT w.condition_id, w.recv_ts_ns AS ts_ms, w.whale_net_usd_24h,
         w.whale_net_usd_24h / nullif(v.traded_usd, 0) AS whale_z_24h
  FROM w
  ASOF JOIN v ON v.condition_id = w.condition_id AND v.hour_ms <= w.recv_ts_ns / 1000000
) TO 'data/derived/ideas/cross-whale-slow-accumulation/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `whale_activity.condition_id|side|usdc_size|activity_ts_ms|recv_ts_ns` is on the tape (161 k rows, 1 h poll) and `clob_trades.price|size` gives the denominator.

## Provenance

- Polymarket informed-flow coverage: the study of 588 M trades / 1.72 M accounts finding that 3.14 % of accounts move prices 7-12x more per dollar and capture > 30 % of gains (Business Insider coverage of the Polymarket informed-trading study, 2026).
- `docs/data-collection.md` Whales section: Data API v2 polled hourly, which is the latency floor this idea respects.

## What would change our mind

If whale net flow has no 24 h forward information, the concentrated-information finding lives inside a single trade instant that our 1 h poll cannot see, and the whale table is only an attribution dataset.
