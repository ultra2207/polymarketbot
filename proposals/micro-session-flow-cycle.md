# Liquidity has an hour-of-day signature; quote the balanced hours

`slug:` micro-session-flow-cycle
`claim:` Signed taker flow in a liquid market is systematically one-sided in specific UTC hours (the same hours that carry the volume), and the one-sidedness is persistent enough across hours to be forecast as-of; restricting participation to hours with low historical imbalance improves net while keeping the hours that carry the fills.
`market scope:` high-volume only — the profile needs tens of trades per hour bucket; the reward universe has a median of 2 book events per 6 hours.
`data:` `prepare.sql` over `data/tape/clob_trades` (`asset_id, venue_ts_ms, price, size, side`) and, where the trade feed is absent, `data/tape/clob_events` `price_changes` level decreases as synthetic prints; output columns `hour_imb`, `hour_trades`, `hour_imb_z`. Module reads `DecisionContext.signals`, `DecisionContext.rate_per_day`, `DecisionContext.book.spread_cents`, `DecisionContext.seconds_elapsed`.

## Mechanism

The cost of resting is one-sided flow; the reward is flat per minute. One-sidedness is not stationary: for a market tied to an equity index or a US political event, the imbalance concentrates in the hours when the underlying's cash market is open and the news flow runs. In the balanced hours the same pool pays the same reward against a two-sided tape, so fills mean-revert instead of trending.

The signal is a *forecast of the imbalance*, not of direction: `hour_imb = mean over prior days of |buy-sell|/(buy+sell)` for that UTC hour. Low `hour_imb` means two-sided flow by construction — noise traders on both sides. High `hour_imb` means the hour is dominated by directional takers, and resting there is paying for the privilege of being filled by them.

Two safeguards make it as-of clean: the profile is computed from *previous* hours only (shifted by at least one hour, so the current hour never contributes), and it is shrunk toward the asset's all-hours mean when the bucket has fewer than 30 trades.

## Rule

```python
# decide (per token; signals are keyed by asset_id and joined as-of backwards)
imb   = ctx.signals.get("hour_imb")        # 0..1, forecast for this UTC hour
z     = ctx.signals.get("hour_imb_z")      # standard score of that forecast vs this asset's own history
n     = ctx.signals.get("hour_trades")     # bucket sample size

if n is None or n < 30:            return QUOTE both        # no forecast, baseline
if imb <= 0.45 and z <= 0.5:       return QUOTE both        # balanced hour: normal participation
if imb >= 0.65 and z >= 1.0:       return PULL both         # directional hour: stand aside
return last_state
```

The deadband between 0.45 and 0.65 is the hysteresis; without it the rule flickers at the boundary and pays the queue-reset cost. Note the action is a whole-market stand-down, evaluated per token but driven by a per-asset signal, so it can never produce a one-sided book.

Sketch of the `prepare.sql` (window: prior 24 h, bucketed by UTC hour, `ts_ms` emitted every 60 s so the as-of join has a row to land on):

```sql
with t as (
  select asset_id, venue_ts_ms, size,
         case when side = 'BUY' then size else -size end as signed
  from read_parquet('${TAPE}/clob_trades/**/*.parquet')
), h as (
  select asset_id, date_trunc('hour', to_timestamp(venue_ts_ms/1000)) as ts_hour,
         (sum(case when signed>0 then signed else 0 end)
          - sum(case when signed<0 then -signed else 0 end)) / nullif(sum(abs(signed)),0) as imb_hour,
         count(*) as n
  from t group by 1,2
), prof as (   -- expanding, lagged by one hour: no look-ahead
  select asset_id, ts_hour,
         avg(abs(imb_hour)) over (partition by asset_id, extract(hour from ts_hour)
                                  order by ts_hour rows between 24 preceding and 1 preceding) as hour_imb,
         sum(n)          over (partition by asset_id, extract(hour from ts_hour)
                                  order by ts_hour rows between 24 preceding and 1 preceding) as hour_trades
  from h
) , scored as (   -- standard score against the asset's own PRIOR hours only: no look-ahead
  select asset_id, ts_hour, hour_imb, hour_trades,
         (hour_imb - avg(hour_imb)    over w0) / nullif(stddev_pop(hour_imb) over w0, 0) as hour_imb_z
  from prof
  window w0 as (partition by asset_id order by ts_hour rows between unbounded preceding and 1 preceding)
)
select asset_id, (epoch(ts_hour)*1000)::bigint as ts_ms, hour_imb, hour_imb_z, hour_trades
from scored where hour_imb_z is not null
```

(For assets outside the trade subscription, replace the trade CTE with the synthetic-print derivation already in `replay_all_markets.py`: `prev_size - size` on `price_changes` at the same price.)

## Why not ruled out

- Fact 1 and 2 are price and size; this is neither.
- Fact 3 (adverse markout everywhere) is a cross-sectional statement about widths; this rule conditions on *when*, and the width sweep held the hour mix constant.
- Fact 6 (Kalshi) is irrelevant — the signal is the market's own tape.
- Fact 8 (mid cohorts) is orthogonal and composes: cohort tells you whether a market is worth quoting at all, the hour tells you when.
- The existing `micro-adaptive-exit` reacts to *realised* P&L after ≥ 8 fills; this rule is a forecast made before the hour starts, from flow balance rather than from our own outcome, and it is deliberately evaluated on a signal the engine's `signal_provider` already supports (`data/derived/ideas/<slug>/signals.parquet`).

## Kill criterion

The decisive test is cheap and should precede any replay: kill if `hour_imb` for a given (asset, UTC hour) is not persistent across days (correlation between day-1 and day-2 profiles < 0.2) — with three tape days there is exactly one such comparison, which is enough to falsify. Then kill if the gate removes more than 50 % of trade-count-weighted minutes (it is selecting on activity, not on imbalance), or if paired net is not positive on both slices.

## Replay cost

One `prepare.sql` over three day partitions of `clob_trades` (2,252 trades/hour × 24 × 3 ≈ 160k rows — seconds). For assets without a trade feed, the synthetic-print variant reads `price_change` rows: ~5.5M/hour, so budget 1-2 minutes per hour partition, one time only. Replay itself is the standard highvol pass.
