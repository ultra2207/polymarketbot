# Deribit-implied digital probability versus the Polymarket price

claim:      The signed gap between the PM mid and the Deribit-volatility-implied digital probability predicts the next 60 minutes of PM mid movement; quoting the rich side as the resting ask earns at least 0.6 c/share.

mechanism:  A Polymarket crypto binary and a Deribit digital on the same strike are the same cash-or-nothing claim. PM is priced by retail buying convexity while the option-implied digital is priced risk-neutral through Breeden-Litzenberger, and the published gap is PM 6.3 pp above option-implied on average (arXiv 2606.19517). The rich side is a premium paid to whoever sells it, and our resting ask is that seller.

data:       `deribit_surface` (index_name, dvol, atm_iv, skew_25d_call, skew_25d_put, term_7d, term_14d, term_30d, venue_ts_ms; 27,392 rows at 10 s) + `binance_bbo` (spot reference) + `data/tape/universe/*.json` (question text with the strike, end_date) + `clob_events`.

module:     `setup` parses symbol, strike, direction (above/reach/dip/below) and end_date from the question; anything unparsable -> QUOTE both. `fair_p_digital = Phi(d2)` on the term-matched ATM IV adjusted by the 25-delta skew: `sigma_adj = atm_iv + kappa * skew_25d_call` above the strike, `atm_iv - kappa * skew_25d_put` below, `kappa = 0.5` and reported. `pm_gap = mid - fair_p_digital`. Gap > +3 c -> the YES token is rich: keep the YES ask at full size, SHRINK the YES bid to 0.25. Gap < -3 c -> mirror. Inside +/-3 c (2 c median spread + 1 c fee) do nothing. Surface stale by > 60 s -> QUOTE both.

metric:     Paired per-condition net, plus the per-condition regression of the forward 60 min PM mid change on `pm_gap` (t on the condition unit).

kill:       Kill if the forward-return coefficient on `pm_gap` has t < 2 over >= 30 condition-days, or if the mean |gap| is < 2 c (inside the cost), or if the paired markout gain is < 0.6 c/share.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-deribit-digital-basis/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `fair_p_digital` | probability | Phi(d2) from deribit_surface atm_iv + 25d skew and binance_bbo spot | 60 s |
| `pm_gap` | cents | clob mid - fair_p_digital | decision-time |
| `iv_used` | annualized vol | term-matched ATM IV after the skew adjustment | 60 s |

## prepare.sql

```sql
-- Fair digital probability on a 1-minute grid per (condition, minute).
-- r = 0 (crypto); d2 = (ln(S/K) - 0.5*sigma^2*tau) / (sigma*sqrt(tau)).
COPY (
  WITH u AS (
    SELECT unnest(markets) AS m
    FROM read_json('data/tape/universe/*.json', maximum_object_size=50000000)
  ),
  c AS (
    SELECT m.condition_id,
           CASE WHEN lower(m.question) LIKE '%bitcoin%'  THEN 'BTC'
                WHEN lower(m.question) LIKE '%ethereum%' THEN 'ETH' END AS cc,
           TRY_CAST(replace(regexp_extract(m.question, '\$([0-9][0-9,]*\.?[0-9]*)', 1), ',', '')
                    AS DOUBLE) AS strike,
           lower(m.question) LIKE '%dip%' OR lower(m.question) LIKE '%below%' AS is_below,
           CAST(replace(m.end_date, ' ', 'T') AS TIMESTAMPTZ) AS end_ts
    FROM u
  ),
  c2 AS (SELECT * FROM c WHERE cc IS NOT NULL AND strike IS NOT NULL),
  s AS (
    SELECT CASE symbol WHEN 'BTCUSDT' THEN 'BTC' ELSE 'ETH' END AS cc,
           (venue_ts_ms / 60000) * 60000 AS min_ms,
           arg_max((bid + ask) / 2, venue_ts_ms) AS spot
    FROM read_parquet('data/tape/binance_bbo/**/*.parquet')
    WHERE symbol IN ('BTCUSDT','ETHUSDT')
    GROUP BY 1, 2
  ),
  v AS (
    SELECT index_name AS cc, (venue_ts_ms / 60000) * 60000 AS min_ms,
           arg_max(atm_iv, venue_ts_ms)        AS atm_iv,
           arg_max(skew_25d_call, venue_ts_ms) AS skew_call,
           arg_max(skew_25d_put,  venue_ts_ms) AS skew_put
    FROM read_parquet('data/tape/deribit_surface/**/*.parquet')
    GROUP BY 1, 2
  ),
  j AS (
    SELECT c2.condition_id, s.min_ms AS ts_ms, c2.is_below, c2.strike, s.spot, v.atm_iv,
           CASE WHEN c2.is_below THEN v.atm_iv - 0.5 * v.skew_put
                ELSE v.atm_iv + 0.5 * v.skew_call END AS iv_used,
           greatest((epoch(c2.end_ts) * 1000 - s.min_ms) / 3.15576e10, 0.0000019) AS tau_years
    FROM c2
    JOIN s ON s.cc = c2.cc
    JOIN v ON v.cc = c2.cc AND v.min_ms = s.min_ms
  )
  SELECT condition_id, ts_ms, strike, spot, atm_iv, iv_used, tau_years,
         CASE WHEN is_below
              THEN 1 - 0.5 * erfc(-((ln(spot / strike) - 0.5 * iv_used * iv_used * tau_years)
                                     / (iv_used * sqrt(tau_years))) / sqrt(2))
              ELSE     0.5 * erfc(-((ln(spot / strike) - 0.5 * iv_used * iv_used * tau_years)
                                     / (iv_used * sqrt(tau_years))) / sqrt(2))
         END AS fair_p_digital
  FROM j
) TO 'data/derived/ideas/cross-deribit-digital-basis/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector** for the inputs (`deribit_surface.atm_iv|skew_25d_put|skew_25d_call`, `binance_bbo`, manifest strike/end_date). The harness needs the strike parser and a `--min-pool 0` run over the crypto sentinels; the reward component is nil there, so the decision is on markout and on the forward-return coefficient.

## Provenance

- arXiv 2606.19517, *Do Prediction Markets Match Option Prices? Bitcoin Threshold Evidence from Binance and Polymarket*: pooled mean discrepancy 0.0633 (6.3 pp), PM above option-implied.
- Breeden & Litzenberger (1978); the skew correction is derived in `docs/institutional-exogenous-market-making.md` section 1.2.
- `docs/alpha-pipeline.md` C1 (crypto up/down vs reference price) is the calibration cousin; this conditions on the option surface rather than on distance alone.

## What would change our mind

If the gap is real but does not predict PM returns, the overpricing is a static premium already harvested by arbitrage and the maker has no edge in it; the surface then only serves as a spread-widening input.
