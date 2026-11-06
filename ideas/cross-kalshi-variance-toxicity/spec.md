# Cross-venue variance as the toxicity gauge PM order flow cannot see

claim:      30 s realized variance of the Kalshi mid separates PM adverse selection better than PM own OFI; at equal time-quoted fraction the variance gate earns at least 0.5 c/share more than the OFI gate.

mechanism:  PM own OFI fires on harmless small trades, while cross-venue variance aggregates the deeper venue reaction to shared information. Holding PM OFI fixed, Kalshi variance should still separate markouts; if it does not, the toxicity signal is local and cross-venue data has no defensive value.

data:       `kalshi_books` (mid, recv_ts_ns; 5 s) + mapping artifact + `clob_events` / `clob_trades`. Variance is computed on logit(mid) so a 5 c move at 0.50 and at 0.95 are comparable.

module:     `k_var_30s` = sum of squared 5 s logit changes over 30 s, in bps^2. Top tercile of its trailing 1 h distribution -> SHRINK both to 0.5; top decile -> PULL both. The module must keep its quote time comparable to the OFI baseline: the gate reports `time_quoted_frac` and the comparison is made at matched quoted time, not at matched threshold.

metric:     Markout separation between variance terciles **within** PM OFI deciles (two-way sort), plus paired net at matched `time_quoted_frac`.

kill:       Kill if the tercile separation within OFI deciles is < 0.3 c/share, or if the separation vanishes when PM own spread is held fixed (it was only a wide-spread effect), or paired net <= 0.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-kalshi-variance-toxicity/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `k_var_30s` | bps^2 | sum of squared 5 s logit(mid) changes over 30 s | 6 s |
| `k_var_z_1h` | sigma units | k_var_30s vs its trailing 1 h distribution | 6 s |

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
) TO 'data/derived/ideas/cross-kalshi-variance-toxicity/kalshi_map.parquet' (FORMAT PARQUET);

COPY (
  WITH k AS (
    SELECT ticker, recv_ts_ns,
           ln(mid / (1 - mid)) * 1e4 AS lg
    FROM read_parquet('data/tape/kalshi_books/**/*.parquet')
    WHERE mid > 0.001 AND mid < 0.999
  ),
  d AS (
    SELECT ticker, recv_ts_ns, lg - lag(lg) OVER (PARTITION BY ticker ORDER BY recv_ts_ns) AS dlg
    FROM k
  ),
  v AS (
    SELECT ticker, recv_ts_ns,
           sum(dlg * dlg) OVER (PARTITION BY ticker ORDER BY recv_ts_ns
                                RANGE BETWEEN 30000000000 PRECEDING AND CURRENT ROW) AS k_var_30s,
           avg(dlg * dlg) OVER (PARTITION BY ticker ORDER BY recv_ts_ns
                                RANGE BETWEEN 3600000000000 PRECEDING AND CURRENT ROW) AS var_mean_1h
    FROM d
  ),
  m AS (SELECT * FROM read_parquet('data/derived/ideas/cross-kalshi-variance-toxicity/kalshi_map.parquet'))
SELECT m.condition_id, v.recv_ts_ns AS ts_ms, v.k_var_30s,
       v.k_var_30s / nullif(v.var_mean_1h, 0) AS k_var_z_1h
FROM v JOIN m ON m.kalshi_ticker = v.ticker
WHERE v.dlg IS NOT NULL
) TO 'data/derived/ideas/cross-kalshi-variance-toxicity/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `kalshi_books.mid|recv_ts_ns` at 5 s. The trailing-1 h distribution needs a contiguous hour, which the 10-hour book window supplies; slices before 11-05 19:11 UTC have no Kalshi books and must be excluded.

## Provenance

- `docs/kalshi-hypotheses.md` H5 (cross-venue variance as a toxicity gauge) and its decision rule of >= 0.3 c/share separation between terciles.
- Cont, Kukanov & Stoikov (2014) for the local-flow baseline this must beat.

## What would change our mind

If Kalshi variance adds nothing within PM OFI deciles, toxicity is a local phenomenon; the cross-venue branch is then limited to flow and basis, and the variance monitor is dropped.
