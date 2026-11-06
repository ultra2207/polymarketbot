# Kalshi settlement is public before UMA resolves

claim:      In the 0-60 min after a mapped Kalshi market transitions to settled or closed, the PM mid moves in the direction of the Kalshi outcome in at least 70 % of events; pulling both sides in that window is net positive.

mechanism:  The outcome is public on Kalshi the moment it settles, while PM UMA resolution lags hours. PM book quotes are stale against a known fact, and informed takers sweep them. This is a quote-pull calendar, not a directional bet: the claim is about when to be absent.

data:       `kalshi_markets` (ticker, status, last_price, yes_bid, yes_ask, close_time, recv_ts_ns; 2 s, 1.94 M rows) + mapping artifact + `clob_events`.

module:     On a status transition to settled/closed with a decisive `last_price` (<= 0.1 or >= 0.9): PULL both sides for 60 min of decision time, then resume at 0.5 size for the next 60 min. The transition is detected from the `k_status` column changing value across signal rows (state per market, reset in `setup`). A non-decisive final price (0.1 to 0.9) is a void/ambiguous outcome: PULL both for 6 h instead, because the PM book will trade the dispute, not the outcome.

metric:     Sign agreement between the PM 60 min mid move and the Kalshi outcome, and paired per-condition net inside the window versus that condition pre-window.

kill:       Kill if sign agreement is < 70 % over >= 10 settle events, or if PM has already moved >= 80 % of the distance within 60 s of the Kalshi settlement (no stale window), or if window net after reward foregone is not > 0.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-kalshi-settle-calendar/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `k_status` | enum 0/1/2 | kalshi_markets.status: active=0, closed=1, determined/finalized=2 | 10 s |
| `k_final_price` | probability | kalshi_markets.last_price at close | 10 s |
| `secs_since_close` | seconds | recv_ts minus the close_time transition | 10 s |

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
) TO 'data/derived/ideas/cross-kalshi-settle-calendar/kalshi_map.parquet' (FORMAT PARQUET);

COPY (
  WITH k AS (
    SELECT ticker, recv_ts_ns, last_price, close_time,
           CASE lower(status) WHEN 'finalized' THEN 2 WHEN 'determined' THEN 2
                WHEN 'closed' THEN 1 ELSE 0 END AS k_status
    FROM read_parquet('data/tape/kalshi_markets/**/*.parquet')
  ),
  s AS (
    SELECT ticker, recv_ts_ns, k_status, last_price,
           min(recv_ts_ns) FILTER (WHERE k_status >= 1)
             OVER (PARTITION BY ticker) AS first_close_ns
    FROM k
  ),
  m AS (SELECT * FROM read_parquet('data/derived/ideas/cross-kalshi-settle-calendar/kalshi_map.parquet'))
SELECT m.condition_id, s.recv_ts_ns AS ts_ms, s.k_status,
       s.last_price AS k_final_price,
       (s.recv_ts_ns - s.first_close_ns) / 1e9 AS secs_since_close
FROM s JOIN m ON m.kalshi_ticker = s.ticker
) TO 'data/derived/ideas/cross-kalshi-settle-calendar/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `kalshi_markets.status|last_price|close_time|recv_ts_ns` at 2 s (1.94 M rows). Population-limited: a 2-day tape holds few decisive settlements, so expect this to be a forward monitor that accumulates events.

## Provenance

- `docs/kalshi-hypotheses.md` H4 (settlement news is decisive for PM macro markets; the decision rule is a quote-pull calendar).
- Polymarket resolution mechanics and UMA liveness: `docs/data-collection.md` UMA section.

## What would change our mind

If PM prices the outcome before Kalshi settles, the calendar has no stale window and H4 dies; the measurement is still useful as a negative result for the whole settlement-lag family.
