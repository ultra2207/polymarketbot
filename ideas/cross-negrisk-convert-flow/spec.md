# Neg-risk conversion: quote the leg the converters are forced to buy

claim:      Inside a neg-risk event, the leg furthest below its conversion-implied price attracts conversion arbitrage buying; quoting that leg bid-heavy and ask-light earns at least 0.5 c/share.

mechanism:  Conversion (burn one NO in every other leg to receive one YES) makes the sum of YES prices a hard constraint. When a leg is cheap relative to one minus the sum of the others, arbitrageurs must buy that leg YES, and the rest of the set is quoted by retail who never compute the sum. We sit on the constrained leg and let the converters lift our offer while we keep the bid that the constraint says is below fair.

data:       `data/tape/universe/*.json` (condition_id, event_id, neg_risk, clob_tokens; hourly snapshots) + `clob_events` for every leg + `universe_metadata.parquet` (fee_rate). Population: 3,606 distinct neg-risk conditions and $206 k/day across the snapshots, but the current cut holds only 4 legs at pool 0, so the slice must be an hour when a neg-risk event was live (for example the 2026-11-04 election hours) and the harness must register those conditions explicitly or run `--min-pool 0`.

module:     `setup` loads the leg set and the sibling ids for its event from the signal table. `neg_dev = mid_leg - (1 - sum(other legs mids))`; require at least 3 legs fresh (<= 60 s) before acting. `neg_dev < -3 c` means the leg is cheap, so its fair value is above the mid: keep the bid at 1.0 and SHRINK the ask to 0.25. `neg_dev > +3 c` mirrors. Inside +/-3 c quote both. Fewer than 3 fresh legs -> QUOTE both, never a synthetic price from stale legs.

metric:     Paired per-condition net per leg, plus the event-level coherence statistic (sum of leg mids across the event).

kill:       Kill if paired net <= 0, or if |neg_dev| > 3 c occurs in < 5 % of leg-minutes (the constraint is never binding), or if the freshness requirement excludes > 80 % of event-minutes (the rule can never fire).

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-negrisk-convert-flow/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `neg_dev` | cents | leg mid - (1 - sum of the other legs mids) | 60 s |
| `neg_rank` | rank | rank of |neg_dev| within the event | 60 s |
| `n_legs_fresh` | count | legs of the event quoted within 60 s | 60 s |

## prepare.sql

```sql
COPY (
  WITH u AS (
    SELECT unnest(markets) AS m
    FROM read_json('data/tape/universe/*.json', maximum_object_size=50000000)
  ),
  legs AS (
    SELECT DISTINCT m.condition_id, m.event_id
    FROM u
    WHERE m.neg_risk AND m.event_id IS NOT NULL
  ),
  pm AS (
    SELECT condition_id, (venue_ts_ms / 60000) * 60000 AS min_ms,
           arg_max((best_bid + best_ask) / 2, venue_ts_ms) AS mid
    FROM read_parquet('data/tape/clob_events/**/*.parquet')
    WHERE event_type = 'best_bid_ask' AND best_bid > 0 AND best_ask < 1
    GROUP BY 1, 2
  )
  SELECT l.condition_id, pm.min_ms AS ts_ms, pm.mid,
         pm.mid - (1 - (sum(pm.mid) OVER (PARTITION BY l.event_id, pm.min_ms) - pm.mid)) AS neg_dev,
         count(*) OVER (PARTITION BY l.event_id, pm.min_ms) AS n_legs_fresh
  FROM pm JOIN legs l USING (condition_id)
) TO 'data/derived/ideas/cross-negrisk-convert-flow/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `data/tape/universe/*.json` (neg_risk, event_id) + `clob_events` for all legs. The constraint needs those specific conditions registered in the replay, so the scheduler must pass an explicit universe or `--min-pool 0` for a historical neg-risk hour.

## Provenance

- Executable-arbitrage study of Polymarket: ~$1.12 M of arbitrage profit across converter-enabled and settlement-based channels, with converter strategies dominating (~$1.086 M) (arXiv 2608.00666).
- Polymarket negative-risk documentation (convert operation) and `docs/alpha-pipeline.md` A2 (neg-risk coherence measured with conversion bounds; no persistent *take-side* violation, which is why the maker side is the untested one).

## What would change our mind

If the deviation never exceeds cost, converters keep the set tight and a maker earns nothing from parity; the idea then only exists as a taker arb, which the repo already measured as not persistent.
