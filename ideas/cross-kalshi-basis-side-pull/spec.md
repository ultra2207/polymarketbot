# Kalshi basis displacement: pull the side the other book has already repriced

claim:      A Kalshi-minus-Polymarket logit basis beyond +/-3 logit points predicts the PM mid reprice within 15 s; pulling the threatened side raises net by at least 0.5 c/share versus the PM-only gate.

mechanism:  The same outcome is quoted on two books; the deeper book absorbs the shock first and PM quotes stay stale for seconds. A positive basis means PM must reprice up, so the resting ask on the YES token is the side that gets swept. The maker is not forecasting the outcome, only which of its own two quotes is about to be wrong.

data:       `kalshi_books` (ticker, yes_best_bid, yes_best_ask, no_best_bid, no_best_ask, mid, spread, recv_ts_ns; 5 s, 179,362 rows from 11-05 19:11) + `data/derived/kalshi_mapping_v2.parquet` (107 pairs, 53 of them in the reward universe at pool >= $15/day) + `clob_events`. Measured prior: 101 pairs / 2,914 aligned rows in one sealed hour, forward PM-return slope on basis +0.207 at 5 s (t 3.40), +0.311 at 15 s (5.74), +0.441 at 60 s (6.76), deciles not monotone, treasury carries the mean.

module:     Act only when `kalshi_age_ms <= 6000` and `kalshi_conf >= 0.6`. basis > +3 -> SHRINK the YES ask to 0.25 and keep the bid; basis < -3 -> mirror; |basis| > 8 -> PULL. The mapping direction is applied before the sign is read: for a `yes_no` row the basis is negated. No book row in 6 s -> QUOTE both (a silent venue is not a zero basis, per the orchestrator docstring).

metric:     Paired per-condition net and markout over the 53 reward-universe mapped conditions, versus `queue_aware_ofi` on the same slice.

kill:       Kill if paired net <= 0 or t < 2 on two slices, or the basis-conditioned markout gain is < 0.5 c/share, or the 5 s deciles stay non-monotone on a >= 7 day sample (the slope then belongs to one family with < 10 pairs and cannot be quoted).

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-kalshi-basis-side-pull/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `kalshi_basis_logit` | logit points | logit(kalshi_mid) - logit(pm_mid), direction-corrected | 6 s |
| `kalshi_spread_c` | cents | kalshi yes_best_ask - yes_best_bid | 6 s |
| `kalshi_age_ms` | ms | decision ts - kalshi_books.recv_ts_ns | 6 s |
| `kalshi_conf` | 0..1 | mapping confidence | static |

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
) TO 'data/derived/ideas/cross-kalshi-basis-side-pull/kalshi_map.parquet' (FORMAT PARQUET);

-- signals: PM mid and Kalshi mid on the 5 s Kalshi grid, as-of joined backwards.
COPY (
  WITH k AS (
    SELECT ticker, recv_ts_ns, mid, spread,
           row_number() OVER (PARTITION BY ticker ORDER BY recv_ts_ns) AS rn
    FROM read_parquet('data/tape/kalshi_books/**/*.parquet')
    WHERE mid IS NOT NULL AND mid > 0 AND mid < 1
  ),
  m AS (SELECT * FROM read_parquet('data/derived/ideas/cross-kalshi-basis-side-pull/kalshi_map.parquet')),
  pm AS (      -- PM mid per condition, from the top of book carried in clob_events
    SELECT condition_id, venue_ts_ms,
           (best_bid + best_ask) / 2 AS pm_mid
    FROM read_parquet('data/tape/clob_events/**/*.parquet')
    WHERE event_type = 'best_bid_ask' AND best_bid > 0 AND best_ask < 1
  )
  SELECT m.condition_id, k.recv_ts_ns AS ts_ms,
         (ln(k.mid / (1 - k.mid)) - ln(p.pm_mid / (1 - p.pm_mid)))
           * CASE WHEN m.direction = 'yes_no' THEN -1 ELSE 1 END AS kalshi_basis_logit,
         k.spread AS kalshi_spread_c,
         m.confidence AS kalshi_conf
  FROM k
  JOIN m ON m.kalshi_ticker = k.ticker
  ASOF JOIN pm p ON p.condition_id = m.condition_id AND p.venue_ts_ms <= k.recv_ts_ns / 1000000
) TO 'data/derived/ideas/cross-kalshi-basis-side-pull/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `kalshi_books.ticker|mid|spread|recv_ts_ns` (5 s, 179 k rows) + `clob_events.best_bid|best_ask` + the mapping artifact. Population: 53 reward-universe pairs; the 10-hour book window covers 107 tickers, so the slice must be >= 11-05 19:11 UTC.

## Provenance

- `docs/kalshi-hypotheses.md` H1 and its status table: slope +0.207/+0.311/+0.441 at 5/15/60 s, underpowered at 4.2 condition-days, deciles not monotone.
- `docs/predictive-mm-analysis.md` section 1: directional prediction is not supported; the lead is defensive, which is exactly what a side-pull uses.
- `docs/kalshi-hf-basis.md` for the report that produced those slopes.

## What would change our mind

If the basis slope survives a 7-day sample but the maker markout gain does not, the basis is a slow repricing that no quote can exploit at 6 s of stale book data; the cross-venue branch then closes for defensive use too.
