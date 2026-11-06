# Kalshi depth imbalance: the other venue direction, not just its timing

claim:      Kalshi 5-cent depth imbalance predicts which side of the PM book is swept next, with AUC at least 0.03 above the PM-only OFI gate; gating on it raises paired net by at least 0.5 c/share.

mechanism:  Kalshi book holds resting informed quotes (automated makers and the informed flow they front-run) before the retail-heavy PM book shows the same information, and the imbalance between its YES and NO bid depth is a direction the PM context structurally cannot contain. PM OFI measures our own venue when it is already moving; the other venue measures where it is about to move. The counterparty is the PM taker who arrives after the Kalshi book has already leaned.

data:       `kalshi_books` (yes_bid_size_1c/2c/5c/10c, no_bid_size_*, yes_best_bid, yes_best_ask, recv_ts_ns; 5 s, 179,362 rows) + mapping artifact (107 pairs, 53 in the reward universe) + `clob_events`. Prior: H3 in `docs/kalshi-hypotheses.md` is written and untested; the 60 s grid already showed Kalshi co-moves at lag 0 (r = 0.21) so the imbalance is the remaining candidate.

module:     `k_imb = (yes_bid_size_5c - no_bid_size_5c) / (yes_bid_size_5c + no_bid_size_5c)`. Positive means YES bids dominate, so the next sweep is into the YES ask. If `k_imb > +0.4` and `k_age_ms <= 6000`: SHRINK the YES ask to 0.25, keep the bid. `k_imb < -0.4` mirrors. |imb| > 0.7: PULL the threatened side. Inside +/-0.4 quote both. Direction-corrected by the mapping row (`yes_no` negates). Stale book -> QUOTE both.

metric:     Paired per-condition net; secondary = AUC of `k_imb` for predicting a PM fill with |markout| > 1 c in the next 5 s, versus the PM OFI gate on the same episodes.

kill:       Kill if the AUC gain over the PM OFI gate is < 0.03, or paired net <= 0, or if the AUC gain vanishes under a one-tick quote (the signal then only matters where the spread is wide), or if one condition carries > 50 % of the effect.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-kalshi-imbalance-lead/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `k_imb_5c` | -1..1 | (yes_bid_size_5c - no_bid_size_5c) / sum | 6 s |
| `k_imb_1c` | -1..1 | same on the 1 c bucket, for the short horizon | 6 s |
| `k_age_ms` | ms | decision ts - kalshi_books.recv_ts_ns | 6 s |

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
) TO 'data/derived/ideas/cross-kalshi-imbalance-lead/kalshi_map.parquet' (FORMAT PARQUET);

COPY (
  WITH k AS (
    SELECT ticker, recv_ts_ns, yes_bid_size_5c, no_bid_size_5c, yes_bid_size_1c, no_bid_size_1c
    FROM read_parquet('data/tape/kalshi_books/**/*.parquet')
  ),
  m AS (SELECT * FROM read_parquet('data/derived/ideas/cross-kalshi-imbalance-lead/kalshi_map.parquet'))
SELECT m.condition_id, k.recv_ts_ns AS ts_ms,
       (k.yes_bid_size_5c - k.no_bid_size_5c)
         / nullif(k.yes_bid_size_5c + k.no_bid_size_5c, 0)
         * CASE WHEN m.direction = 'yes_no' THEN -1 ELSE 1 END AS k_imb_5c,
       (k.yes_bid_size_1c - k.no_bid_size_1c)
         / nullif(k.yes_bid_size_1c + k.no_bid_size_1c, 0) AS k_imb_1c
FROM k JOIN m ON m.kalshi_ticker = k.ticker
) TO 'data/derived/ideas/cross-kalshi-imbalance-lead/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `kalshi_books.yes_bid_size_5c|no_bid_size_5c|yes_bid_size_1c|no_bid_size_1c|recv_ts_ns` at 5 s (179 k rows from 11-05 19:11) + the mapping artifact. Population: 53 reward-universe pairs, of which 66 mapped tickers have prints and 107 have books.

## Provenance

- `docs/kalshi-hypotheses.md` H3 (Kalshi book imbalance as a better sweep predictor than PM OFI) with its stated decision rule: AUC gain >= 0.03 and net >= +$10; also the lag-0 r = 0.21 result that leaves imbalance as the open candidate.
- Cont, Kukanov & Stoikov (2014): imbalance is the state variable the flow moves; here it is measured on the venue that leads.

## What would change our mind

If the AUC gain vanishes under a one-tick quote, the signal is only a wide-spread effect and the cross-venue branch keeps only flow and basis, not book state.
