# Ambiguity is a priced risk, and it is observable before settlement

claim:      Markets with a subjective resolution source carry a dispute-risk discount of at least 1 c relative to machine-readable markets; quoting the unambiguous leg of those markets captures it.

mechanism:  The discount is paid for the tail risk of an oracle dispute: the Ukraine-minerals market went 9 % to 100 % and resolved YES despite no official agreement, and Polymarket called it an unprecedented governance attack. A maker can serve the ambiguous market while pulling at the first on-chain proposal, because dispute risk, unlike price risk, is observable on-chain before settlement. The claim is that the discount is large enough to pay for that monitoring.

data:       `data/tape/universe/*.json` (resolution_source, question) + `uma_events` (proposed_price, disputed, ancillary_text) + `clob_events`. Caveat: 0 disputes exist on the 46-hour tape, so this idea can currently *measure* the discount but cannot estimate the tail with data.

module:     `ambiguity_score` is 0 for a numeric feed URL, 1 for a named outlet or document, 2 for free-text judgement (built in `prepare.sql`). Score 0 -> quote both at full size, one tick. Score >= 1 -> quote at 0.5 size, and when the mid is within 5 c of 0 or 1, quote only the side away from the extreme. No directional claim anywhere: the mechanism is that ambiguity is priced, not that we can resolve it.

metric:     Paired per-condition net by ambiguity class; secondary = mean discount (1 - price on the certain side) by class during the settlement window.

kill:       Kill if the discount difference between class 0 and class >= 1 is < 1 c in the settlement window, or if paired net for class >= 1 is negative (ambiguity is a cost we cannot price, not a premium we can harvest).

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-resolution-ambiguity-discount/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `ambiguity_score` | enum 0/1/2 | prepare.sql classification of resolution_source and question text | static |
| `uma_phase` | enum 1/2/3 | uma_events sequence per condition | 60 s |

## prepare.sql

```sql
COPY (
  WITH u AS (
    SELECT unnest(markets) AS m
    FROM read_json('data/tape/universe/*.json', maximum_object_size=50000000)
  )
  SELECT DISTINCT m.condition_id,
         m.resolution_source,
         CASE
           WHEN m.resolution_source IS NULL OR m.resolution_source = '' THEN 2
           WHEN regexp_matches(m.resolution_source, '^https?://') THEN 0
           WHEN regexp_matches(lower(m.resolution_source),
                'associated press|reuters|official|commission|bureau|federal reserve') THEN 1
           ELSE 2
         END AS ambiguity_score,
         CASE WHEN lower(m.question) LIKE '%official%' OR lower(m.question) LIKE '%credible%'
                OR lower(m.question) LIKE '%confirm%' OR lower(m.question) LIKE '%report%'
              THEN 1 ELSE 0 END AS judgement_wording
  FROM u
  WHERE m.condition_id IS NOT NULL
) TO 'data/derived/ideas/cross-resolution-ambiguity-discount/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector** for the classification (`data/tape/universe/*.json` has `resolution_source` and `question` per market). `docs/data-collection.md` records `resolution_source` as part of `data/derived/universe_metadata.parquet`, so the field is already backfilled for the live manifest.

## Provenance

- UMA whale / governance-attack coverage: Polymarket-UMA dispute over the $7 M Ukraine minerals market, YES probability 9 % -> 100 % despite no official agreement (CoinMarketCap, Yahoo Finance, 2026).
- `docs/data-collection.md` UMA section: proposal, dispute, and the settlement lifecycle that makes the risk observable on-chain.

## What would change our mind

If ambiguous markets show no discount, the market treats resolution risk as unpriced noise and a maker should avoid the class entirely; that would be a kill, not a tuning result.
