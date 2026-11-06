# Tick compression to 0.001: the touch becomes ten times more contestable

claim:      After a venue tick compression from 0.01 to 0.001, fill markout at the touch degrades by at least 0.3 c/share for 30 minutes; shrinking both sides for that window improves net by at least 0.3 c/share versus quoting through it.

mechanism:  The reward score is quadratic in cents from the mid, so a tenfold finer tick leaves the scoring geometry unchanged while changing what competitors can do: they can now sit 0.1 c from the mid and still be post-only. Everyone at the touch becomes undercuttable ten times finer, our FIFO position at the level is worth less, and the fills we still get are the ones that swept through the newly crowded top. The change is a venue parameter event with a timestamp in the tape, not a forecast.

data:       `clob_events` with `event_type = tick_size_change` (old_tick_size, new_tick_size, asset_id, condition_id, venue_ts_ms; 3,166 events over 793 conditions, all 0.01 -> 0.001, 11-04 00:37 -> 11-06 05:15) + `clob_events` price_change / best_bid_ask for the same assets + `clob_trades` for fills.

module:     `setup` records `tick_size` from MarketView-token metadata if present; the change itself arrives as `tick_grid_shock_age_s` in signals. If 0 <= age <= 1800: SHRINK both sides to 0.5 for the first 10 min and to 0.75 for the remaining 20 min, then return to 1.0. The module also records its own `spread_cents / tick_size` before and after: if the book did not actually tighten within 5 min the shock was cosmetic and it reverts to full size early (state per market). Never PULL: the reward is paid on resting quotes and a tick change is not an information event.

metric:     Paired per-condition net and `markout_5m_bps` of fills inside the 30 min window versus matched fills from the same condition before the change.

kill:       Kill if fill markout after the change is not worse by >= 0.3 c/share than the same condition before it, or if fewer than 20 conditions change tick inside the tested slice, or if `time_quoted_frac` falls below 0.9 (the reward cost dominates a 30-minute shrink).

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-tick-grid-shock/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `tick_grid_shock_age_s` | seconds | now - the tick_size_change venue_ts_ms for the asset, negative before | 60 s |
| `old_tick` | price | clob_events.old_tick_size | static |
| `new_tick` | price | clob_events.new_tick_size | static |

## prepare.sql

```sql
COPY (
  SELECT t.condition_id, t.asset_id, t.venue_ts_ms AS change_ms,
         t.old_tick_size AS old_tick, t.new_tick_size AS new_tick
  FROM read_parquet('data/tape/clob_events/**/*.parquet') t
  WHERE t.event_type = 'tick_size_change'
    AND t.new_tick_size IS NOT NULL AND t.old_tick_size IS NOT NULL
) TO 'data/derived/ideas/cross-tick-grid-shock/signals.parquet' (FORMAT PARQUET);

-- age = (decision_ms - change_ms)/1000, computed by the harness; the row is
-- static per asset, so the module never reads the tape.
```

## Runnable now

**yes, no new collector.** `clob_events.event_type|old_tick_size|new_tick_size|asset_id|condition_id|venue_ts_ms` is on the tape (3,166 events). Population: about 60 changes per hour, so a 10-minute slice holds roughly 10 events; the paired test should use a 60-minute slice and report the count.

## Provenance

- `docs/data-collection.md` Streams table and `docs/polymarket-mechanics.md`: tick size is a per-asset venue parameter, and the CLOB emits an explicit `tick_size_change` frame, which is what makes the timestamp observable at all.
- The reward score is quadratic in cents from the mid, per `src/polymarketbot/paper_trading/engine.py` and the venue reward formula; a finer tick changes queue contestability, not the score formula.

## What would change our mind

If markout is unchanged after a tick compression, queue position is not what pays at the touch and the whole queue-ratio mechanism (the measured 0.60 split) is a spread artefact rather than a queue effect; that would be a strong negative result for the repo current baseline.
