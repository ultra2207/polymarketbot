# Kalshi taker flow leads Polymarket taker flow by about one second

claim:      Mapped Kalshi prints predict the direction of PM taker flow at 1 s (measured 0.696 agreement overall, 0.942 for size >= p90); gating the pressured PM side on a Kalshi print of at least p90 size cuts adverse markout by at least 1 c/share.

mechanism:  The same informed wallets, or the same readers of the same headline, hit Kalshi first because its book is thinner and its tick and fee structure are friendlier; the PM taker arrives seconds later. A block-sized Kalshi print is the earliest observable of that information, and the PM side it points at is the one that will be swept.

data:       `kalshi_trades` (ticker, yes_price, no_price, count, taker_side, taker_book_side, is_block_trade, created_time, recv_ts_ns; 8 s poll, 105,196 rows, 66 mapped tickers with 75,851 prints) + mapping artifact + `clob_events`. Prior: 6 h, 29 conditions, 6,841 mapped prints, agreement 0.696 at 1 s (n = 1,143) and 0.942 for size >= p90.

module:     `on_trade` for PM prints is unused; the Kalshi prints arrive through `signals` (`kalshi_flow_5s`, `kalshi_flow_z`, `kalshi_print_age_ms`, `kalshi_block_flag`). If `kalshi_print_age_ms <= 8000` and (`kalshi_block_flag` or `|kalshi_flow_z| >= 1.5`): SHRINK the PM side the flow points at to 0.25 for 5 s of decision time, then restore. Positive Kalshi buying means PM YES up, so the YES ask is the threatened side; direction-corrected by the mapping row. The 8 s poll is a hard latency floor: the module may not treat a print it received late as if it were early.

metric:     Paired per-condition net; secondary = AUC of this gate versus the PM OFI gate for predicting PM fills with |markout| > 1 c on the same episodes.

kill:       Kill if the AUC gain over the PM OFI gate is < 0.03, or paired net <= 0, or if one condition carries > 50 % of the effect (concentration check from plan.md section 6).

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-kalshi-trade-lead-gate/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `kalshi_flow_5s` | signed contracts | sum(count * sign) over trailing 5 s, YES-equivalent | 8 s |
| `kalshi_flow_z` | sigma units | kalshi_flow_5s vs trailing 30 min | 8 s |
| `kalshi_print_age_ms` | ms | decision ts - print recv_ts_ns | 8 s |
| `kalshi_block_flag` | 0/1 | kalshi_trades.is_block_trade | 8 s |

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
) TO 'data/derived/ideas/cross-kalshi-trade-lead-gate/kalshi_map.parquet' (FORMAT PARQUET);

COPY (
  WITH t AS (
    SELECT ticker, recv_ts_ns, count, is_block_trade,
           CASE WHEN lower(taker_side) = 'yes' THEN 1 ELSE -1 END * count AS signed
    FROM read_parquet('data/tape/kalshi_trades/**/*.parquet')
  ),
  w AS (
    SELECT *,
           sum(signed) OVER (PARTITION BY ticker ORDER BY recv_ts_ns
                             RANGE BETWEEN 5000000000 PRECEDING AND CURRENT ROW) AS kalshi_flow_5s,
           avg(signed)  OVER (PARTITION BY ticker ORDER BY recv_ts_ns
                              RANGE BETWEEN 1800000000000 PRECEDING AND CURRENT ROW) AS flow_mean_30m
    FROM t
  ),
  m AS (SELECT * FROM read_parquet('data/derived/ideas/cross-kalshi-trade-lead-gate/kalshi_map.parquet'))
SELECT m.condition_id, w.recv_ts_ns AS ts_ms,
       w.kalshi_flow_5s * CASE WHEN m.direction = 'yes_no' THEN -1 ELSE 1 END AS kalshi_flow_5s,
       w.kalshi_flow_5s / nullif(w.flow_mean_30m, 0) AS kalshi_flow_z,
       CAST(w.is_block_trade AS INT) AS kalshi_block_flag
FROM w JOIN m ON m.kalshi_ticker = w.ticker
) TO 'data/derived/ideas/cross-kalshi-trade-lead-gate/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `kalshi_trades.ticker|count|taker_side|is_block_trade|created_time|recv_ts_ns` is on the tape from 11-04 06:32 (105 k rows). This is the strongest ready-made cross-venue result on the tape (0.696 / 0.942 agreement).

## Provenance

- `docs/kalshi-hypotheses.md` H2 and status table; `docs/kalshi-trade-lead.md` for the 6 h / 29-condition run and the reverse-direction z = -14.9.
- arXiv / Stanford study *Adverse Selection in Prediction Markets: Evidence from Kalshi* (VPIN adaptation): one-sided order flow predicts maker losses in single-name markets.

## What would change our mind

If the AUC gain over the PM-only OFI gate is inside 0.03, Kalshi prints duplicate information the PM book already carries and the cross-venue branch loses its cheapest live test.
