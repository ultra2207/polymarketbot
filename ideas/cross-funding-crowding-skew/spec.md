# Funding crowding: quote against the leveraged side of the perp book

claim:      Extreme annualised funding (|APR| > 40 %) marks a crowded leveraged position and predicts same-direction overpricing in crypto binaries; skewing size against the crowd earns at least 0.5 c/share.

mechanism:  Funding is the price of leverage, so it measures how crowded one side of the perp book is. A crowded book is a queue of forced sellers (or buyers): liquidations are market orders that move the underlying against the crowd, and the crowd is also the PM taker who buys the binary on that side. We quote the other side and are paid by the de-leveraging flow.

data:       `binance_funding` (symbol, funding_rate, mark_price, index_price, funding_time_ms, next_funding_time_ms, venue_ts_ms; 60 s, 12,441 rows) + `binance_bbo` (spot) + `clob_events`. A 7-day funding z-score is **not** on the tape (2.2 days only); the spec uses a trailing 24 h z and says so.

module:     `funding_apr = funding_rate * 3 * 365 * 100`; `mark_index_bps = (mark/index - 1) * 1e4`. For an up binary token: `funding_apr > +40` -> keep the YES ask at 1.0 and SHRINK the YES bid to 0.25 (do not buy what the crowd is long); `< -40` -> mirror; 20..40 -> scale 0.6. Require `mark_index_bps` of the same sign, else do nothing. The module may only change its skew when `funding_age_s <= 120`; it must not interpolate between 60 s samples.

metric:     Paired per-condition net; secondary = markout of fills on the crowded side versus the uncrowded side, split into funding terciles.

kill:       Kill if fills on the crowded side are not worse in markout by >= 0.5 c/share than the uncrowded side (no toxicity to avoid), or if |APR| > 40 % occurs in < 10 % of tested condition-minutes (no population), or paired net <= 0.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-funding-crowding-skew/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `funding_apr` | percent/year | binance_funding.funding_rate * 3 * 365 * 100 | 120 s |
| `funding_z_24h` | sigma units | funding_apr vs its trailing 24 h mean/std | 120 s |
| `mark_index_bps` | bps | (mark_price / index_price - 1) * 1e4 | 120 s |

## prepare.sql

```sql
-- 1) asset map: which Binance symbol prices each crypto condition. One row per
--    condition whose question names a covered underlying; a condition with no
--    parsable symbol gets no row and is never quoted on this signal.
COPY (
  SELECT m.condition_id,
         CASE
           WHEN lower(m.question) LIKE '%bitcoin%'  OR lower(m.slug) LIKE '%btc%' THEN 'BTCUSDT'
           WHEN lower(m.question) LIKE '%ethereum%' OR lower(m.slug) LIKE '%eth%' THEN 'ETHUSDT'
           WHEN lower(m.question) LIKE '%solana%'   OR lower(m.slug) LIKE '%sol%' THEN 'SOLUSDT'
         END AS symbol
  FROM (SELECT unnest(markets) AS m
        FROM read_json('data/tape/universe/*.json', maximum_object_size=50000000)) x
)
WHERE symbol IS NOT NULL
  TO 'data/derived/ideas/cross-funding-crowding-skew/asset_map.parquet' (FORMAT PARQUET);

COPY (
  WITH f AS (
    SELECT symbol, venue_ts_ms, funding_rate, mark_price, index_price,
           funding_rate * 3 * 365 * 100 AS funding_apr
    FROM read_parquet('data/tape/binance_funding/**/*.parquet')
    WHERE symbol IN ('BTCUSDT','ETHUSDT','SOLUSDT')
  ),
  z AS (
    SELECT *,
           (funding_apr - avg(funding_apr) OVER w)
             / nullif(stddev_samp(funding_apr) OVER w, 0) AS funding_z_24h,
           (mark_price / nullif(index_price, 0) - 1) * 1e4 AS mark_index_bps
    FROM f
    WINDOW w AS (PARTITION BY symbol ORDER BY venue_ts_ms
                 RANGE BETWEEN 86400000 PRECEDING AND CURRENT ROW)
  ),
  map AS (SELECT * FROM read_parquet('data/derived/ideas/cross-funding-crowding-skew/asset_map.parquet'))
SELECT map.condition_id, z.venue_ts_ms AS ts_ms, z.funding_apr, z.funding_z_24h, z.mark_index_bps
FROM z JOIN map USING (symbol)
) TO 'data/derived/ideas/cross-funding-crowding-skew/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `binance_funding.funding_rate|mark_price|index_price|symbol|venue_ts_ms` is on the tape at 60 s (12,441 rows).

## Provenance

- arXiv 2212.06888 *Fundamentals of Perpetual Futures*: a one-unit rise in annualised perp-spot deviation lowers perpetual taker order imbalance by 1.11 pp (t = -13.3) and price impact grows with the premium.
- Funding as a crowding gauge and liquidation cascades as forced market orders: `docs/institutional-exogenous-market-making.md` section 1.4.

## What would change our mind

If the crowded side markout is not worse, funding carries no PM information beyond price and the perp tape reduces to the depth and micro-price signals.
