# Binance depth vacuum: pull both sides when the top-5 book thins

claim:      A >= 40 % collapse of Binance top-5 depth within 10 s precedes an outsized PM mid jump; pulling both PM sides while the vacuum lasts improves net by at least 0.4 c/share without lowering time-quoted fraction below 0.9.

mechanism:  Liquidity providers withdraw on the reference venue before they withdraw on Polymarket, and a thin reference book means the next market order moves the underlying further. The PM book has not repriced yet, so our resting quote is about to be gapped through. The vacuum carries no direction, which is why the action is symmetric and cheap.

data:       `binance_depth` (symbol, last_update_id, bids, asks, venue_ts_ms; 1.49 M rows, top-N levels) + `clob_events`. `depth5_side = sum(size) of the first 5 levels`; `ratio = depth5_now / median(depth5, trailing 5 min)`.

module:     `decide` reads `bn_depth5_bid_ratio` and `bn_depth5_ask_ratio`. Both < 0.6 -> PULL both sides. Only the ask side < 0.6 -> PULL the YES ask (a vacuum above the mid means the price is about to jump up). Only the bid side < 0.6 -> PULL the YES bid. Re-enter only after a ratio > 0.8 for 5 consecutive seconds (hysteresis), so one thin snapshot does not flap the quote.

metric:     Paired per-condition `net` and `time_quoted_frac`; secondary = mean absolute PM mid jump in the 30 s after a vacuum versus the same condition baseline.

kill:       Kill if the tested slices contain < 20 vacuum minutes (no power), or paired net <= 0, or `time_quoted_frac` falls below 0.90 without a >= 0.4 c/share markout gain.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-binance-depth-vacuum/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `bn_depth5_bid_ratio` | ratio | binance_depth top-5 bid size / 5 min median | 1 s |
| `bn_depth5_ask_ratio` | ratio | binance_depth top-5 ask size / 5 min median | 1 s |
| `bn_depth5_asym` | log ratio | ln(bid_depth5 / ask_depth5) | 1 s |

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
  TO 'data/derived/ideas/cross-binance-depth-vacuum/asset_map.parquet' (FORMAT PARQUET);

COPY (
  WITH d AS (
    SELECT symbol, venue_ts_ms,
           list_sum(list_transform(bids, x -> x.size)[1:5]) AS depth5_bid,
           list_sum(list_transform(asks, x -> x.size)[1:5]) AS depth5_ask
    FROM read_parquet('data/tape/binance_depth/**/*.parquet')
    WHERE symbol IN ('BTCUSDT','ETHUSDT','SOLUSDT')
  ),
  g AS (
    SELECT symbol, venue_ts_ms, depth5_bid, depth5_ask,
           median(depth5_bid) OVER (PARTITION BY symbol ORDER BY venue_ts_ms
             RANGE BETWEEN 300000 PRECEDING AND CURRENT ROW) AS med_bid,
           median(depth5_ask) OVER (PARTITION BY symbol ORDER BY venue_ts_ms
             RANGE BETWEEN 300000 PRECEDING AND CURRENT ROW) AS med_ask
    FROM d
  ),
  map AS (SELECT * FROM read_parquet('data/derived/ideas/cross-binance-depth-vacuum/asset_map.parquet'))
SELECT map.condition_id, g.venue_ts_ms AS ts_ms,
       g.depth5_bid / nullif(g.med_bid, 0) AS bn_depth5_bid_ratio,
       g.depth5_ask / nullif(g.med_ask, 0) AS bn_depth5_ask_ratio,
       ln(g.depth5_bid / nullif(g.depth5_ask, 0)) AS bn_depth5_asym
FROM g JOIN map USING (symbol)
) TO 'data/derived/ideas/cross-binance-depth-vacuum/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `binance_depth.bids|asks` (list<price,size>), `symbol`, `venue_ts_ms` is on the tape (1.49 M rows). Same sentinel population caveat as the micro-price idea.

## Provenance

- `docs/data-collection.md` Binance depth stream (100 ms).
- Cont, Kukanov & Stoikov (2014) on depth as the leading component of price impact.

## What would change our mind

If depth vacuums do not precede PM jumps, the PM book is driven by PM-specific flow only and the reference book is a distraction; that would also demote the micro-price gate.
