# Inside the last minute a crypto binary is an observation, not a probability

claim:      In the final 60 s before a 5m/15m crypto market closes, quoting only the side that the oracle TWAP comparison determines earns at least 1 c/share and removes the unbounded gamma loss of a two-sided quote at the strike.

mechanism:  As tau goes to 0 the outcome stops being a probability and becomes an observation: the settlement rule is a 60 s TWAP of the underlying, so in the last minute the claim is already decidable from public data, and a two-sided quote straddling the strike has unbounded adverse selection because a one-tick move flips the value between 0 and 1. One-sided quoting on the determined side is not a forecast, it is reading the oracle own settlement rule. The measured cost is the reward forfeited by one-sidedness, which the Polymarket score divides by three inside 0.10-0.90.

data:       `data/tape/universe/*.json` (question, end_date, slug for up/down markets; 20 in the current cut, all sentinels at pool 0) + `binance_bbo` (spot, 100 ms) + `clob_events`. Settlement since 2026-08-14 is a 60 s Chainlink TWAP of the underlying, so the module must compare against a TWAP, not a single print.

module:     `tau_s` from the manifest close time; `oracle_twap_60s` and `spot_last` from signals. If `tau_s <= 60` and `spot_age_ms <= 1000`: compute `sigma_60s` from the module own trailing Binance returns (state accumulated in `on_book`, since the signal only carries the TWAP and spot). If `|ln(spot / twap60)| > 2.5 * sigma_60s * sqrt(tau_s / 60)`, quote only the side that comparison implies and PULL the other. Inside the band: PULL both, because the outcome is a coin flip and a resting quote is a lottery ticket we are selling too cheaply.

metric:     Paired per-condition net and markout; secondary = accuracy of the determined side against the eventual payout (must exceed 95 % for the rule to be safe, independent of PnL).

kill:       Kill if determined-side accuracy is < 95 % over >= 200 market-minutes, or paired net <= 0, or the band rule fires in < 10 % of market-minutes (no coverage).

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-pin-risk-tau/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `tau_s` | seconds | manifest end_date - now, for up/down markets only | 1 s |
| `oracle_twap_60s` | price | 60 s time-weighted average of binance_bbo mid | 1 s |
| `spot_last` | price | last binance_bbo mid | 1 s |
| `strike_p` | price | strike / price-to-beat where the question encodes one | static |

## prepare.sql

```sql
COPY (
  WITH u AS (
    SELECT unnest(markets) AS m
    FROM read_json('data/tape/universe/*.json', maximum_object_size=50000000)
  ),
  ud AS (
    SELECT m.condition_id, m.question,
           CAST(replace(m.end_date, ' ', 'T') AS TIMESTAMPTZ) AS end_ts
    FROM u
    WHERE lower(m.question) LIKE '%up or down%'
  ),
  twap AS (
    SELECT (venue_ts_ms / 1000) * 1000 AS sec_ms,
           avg((bid + ask) / 2) OVER (ORDER BY venue_ts_ms
             RANGE BETWEEN 60000 PRECEDING AND CURRENT ROW) AS twap60,
           (bid + ask) / 2 AS spot
    FROM read_parquet('data/tape/binance_bbo/**/*.parquet')
    WHERE symbol = 'BTCUSDT'
  )
  SELECT ud.condition_id,
         epoch(ud.end_ts) * 1000 AS end_ms,
         twap.sec_ms AS ts_ms,
         twap.twap60 AS oracle_twap_60s,
         twap.spot  AS spot_last
  FROM ud CROSS JOIN twap
) TO 'data/derived/ideas/cross-pin-risk-tau/signals.parquet' (FORMAT PARQUET);

-- The CROSS JOIN above must be filtered to the 60 s around each end_ts before
-- materialisation (join on twap.sec_ms between end_ms - 120000 and end_ms);
-- unfiltered it is conditions x seconds and is not a signal table.
```

## Runnable now

**yes, no new collector.** `data/tape/universe/*.json` end_date for the 20 up/down markets + `binance_bbo` + `clob_events`. Rewards are nil on these sentinel markets, so the test is markout and accuracy only; the harness needs them registered explicitly.

## Provenance

- Chainlink Data Streams TWAP settlement for Polymarket up/down markets: 30 s look-back at launch (2026-08-07), 60 s from 2026-08-14; the opening price-to-beat and the final price use the same feed (Polymarket changelog; Chainlink announcement).
- Gamma/delta singularity as tau -> 0: `docs/institutional-exogenous-market-making.md` section 1.3; the reward penalty for one-sided quotes is in `src/polymarketbot/paper_trading/orchestrator.py` docstring.

## What would change our mind

If determined-side accuracy is below 95 %, the TWAP rule we implement does not match the settlement rule, and no near-expiry rule should be trusted until the oracle is replicated exactly.
