# One headline moves every market in the event; the per-asset book cannot see it

`slug:` micro-event-heat
`claim:` Markets in the same event (the same `event_id` in `data/derived/universe_metadata.parquet`) reprice together on shared news; aggregate event-level trade intensity in the prior 15 minutes leads the local book's own OFI, and gating participation on event heat improves markout on the correlated families that dominate liquid volume.
`market scope:` high-volume events; useless where each condition trades alone (the reward universe's median market has 2 book events per 6 hours and no siblings).
`data:` `prepare.sql` over `data/derived/universe_metadata.parquet` (`condition_id, event_id`), `data/tape/clob_trades` (`condition_id, asset_id, venue_ts_ms, size, side`), and `data/tape/clob_events` (`price_change`) for the synthetic-print variant; signal columns `event_heat_z`, `event_imb`. Module reads `DecisionContext.signals`, `DecisionContext.book.spread_cents`, `DecisionContext.book.competing_score`, `DecisionContext.rate_per_day`.

## Mechanism

A neg-risk event is a family of mutually exclusive outcomes whose prices sum to 1. A single news item moves *all* of them at once: the probability mass redistributes across the family. The engine's `ofi` is per asset (`_ofi_steps[asset_id]`), and the strategy sees one token's book at a time, so by construction it cannot see that the other six outcomes in the event are being repriced. By the time the local book shows the move, the informed traders have already crossed our quote in the market where we happened to be resting.

Event-level activity is the earliest aggregate observable: the sum of trades and the net signed volume across all conditions sharing an `event_id`. When it spikes, the family is in price-discovery mode and every resting quote in it is a stale option. When it is quiet, quotes in the family are protected by the absence of shared news.

This is a *which markets / when* rule and it is orthogonal to the per-market ones: it can be combined with any of the local gates, because it fires precisely when the local book is still quiet (otherwise the local OFI gate would already have caught it).

## Rule

`prepare.sql` produces, per asset, one row per minute:

```sql
with meta as (select condition_id, event_id from read_parquet('data/derived/universe_metadata.parquet')),
     sib  as (select m.event_id, m.condition_id from meta m),
     tok  as (   -- condition -> token ids, from the book snapshots on the tape
       select distinct condition_id, asset_id
       from read_parquet('${TAPE}/clob_events/date=2026-11-06/hour=*/*.parquet')
       where event_type = 'book' and condition_id is not null and asset_id is not null
     ),
     tr   as (   -- event-level flow, one row per (event, minute)
       select s.event_id,
              date_trunc('minute', to_timestamp(t.venue_ts_ms/1000)) as ts_min,
              count(*) as n,
              (sum(case when t.side='BUY' then t.size else 0 end)
             - sum(case when t.side='SELL' then -t.size else 0 end)) / nullif(sum(t.size),0) as imb
       from read_parquet('${TAPE}/clob_trades/**/*.parquet') t
       join sib s using (condition_id)
       group by 1,2
     ),
     heat as (   -- rolling z-score over the prior 16 traded minutes, lagged: no look-ahead
       select event_id, ts_min,
              (n - avg(n) over w) / nullif(stddev_pop(n) over w, 0) as event_heat_z,
              imb as event_imb
       from tr
       window w as (partition by event_id order by ts_min rows between 16 preceding and 1 preceding)
     )
select tok.asset_id,
       (epoch(h.ts_min)*1000)::bigint as ts_ms, h.event_heat_z, h.event_imb
from heat h
join sib s using (event_id)                      -- every condition in the hot event
join tok using (condition_id)                    -- one row per token, which is what the loader joins on
```

`decide`:

```python
z   = ctx.signals.get("event_heat_z")
imb = ctx.signals.get("event_imb")
if z is None:                                  return QUOTE both        # single-market event, baseline
if z >= 3.0 or abs(imb) >= 0.75:               return PULL both         # family in price discovery
if z <= 1.5 and abs(imb) <= 0.40:              return QUOTE both        # quiet family
return last_state
```

The `z <= 1.5` re-entry band plus the `z >= 3.0` exit band is the hysteresis; both thresholds are on the event's own history, so a permanently busy event does not stay gated forever.

## Why not ruled out

- Facts 1 and 2 (price, size) are untouched.
- Fact 3 (adverse markout everywhere) is the loss; the rule is a *when*, and the width sweep did not condition on event-wide activity.
- Fact 6 (Kalshi) says cross-*venue* is dead because the matched sets are disjoint and pay nothing. This rule is cross-*market within Polymarket*, so the disjointness argument does not apply: the siblings are on the same tape, in the same reward universe, and the family shares one `event_id` in the metadata we already have.
- Fact 8 (mid cohorts) composes: cohort decides whether the market is worth quoting, event heat decides which minutes.
- On the highvol universe the pool is zero and the objective is markout only; a shared-news gate is one of the few signals that predicts a *correlated* adverse move rather than a local one.

## Kill criterion

Kill if fewer than 5 % of the replay's markets belong to an event with ≥ 2 conditions that actually trade in the window (then the signal is all nulls and the idea is untestable on this tape), or if `event_heat_z >= 3` does not raise the 5-minute markout of fills in that event at least 25 % above baseline, or if the gate removes more than 30 % of quoting time in the quiet families.

## Replay cost

One `prepare.sql` pass: `universe_metadata` is small (thousands of rows); the trade join is ~2,252 rows/hour in `date=2026-11-06/hour=07`. The synthetic-print variant reads `price_change` (~5.5M rows/hour) once and reuses the same aggregation — 1-2 minutes per hour partition. Then the standard replay pass on the highvol universe.
