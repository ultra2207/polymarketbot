# Binance micro-price lead: pull the side the consolidated book already moved

claim:      On crypto binaries, pulling the side that a 1 s size-weighted Binance micro-price move points at improves 5-minute fill markout by at least 0.5 c/share over `queue_aware_ofi` on the same slice.

mechanism:  Binance is the price-discovery venue for crypto binaries; the PM quote reacts in p50 52.7 ms while the PM taker arrives at p50 1,067.7 ms, so a quote that is stale against a Binance micro-price move is picked off by a taker whose information was public on Binance for almost a second. We earn the spread on noise flow and lose it in that window; removing the threatened side removes the loss, not the revenue.

data:       `binance_bbo` (symbol, bid, ask, bid_qty, ask_qty, venue_ts_ms; 100 ms throttle, 5.29 M rows 11-04 00:37 -> 11-06 05:15) + `data/tape/universe/*.json` (condition_id, question, slug, daily_pool) + `clob_events` (best_bid, best_ask, price_changes). Window: any sealed hour in the tape. Population caveat: crypto threshold binaries are sentinel assets (daily_pool 0) in the current cut and only one crypto market clears the $15/day floor, so run `--min-pool 0` on the sentinels, report `rewards_usd` separately and decide on markout.

module:     `setup` parses the Binance symbol and the strike/direction from the question; no symbol -> QUOTE both. In `decide`, take `bn_micro_ret_1s` (bps). A positive micro return means YES fair value rose, so the resting ask on the YES token is the threatened side and the resting bid is safe. Do nothing inside a deadband of +/-3 bps (one 100 ms BBO tick plus a 2 c spread is noise); SHRINK the threatened side to 0.25 at 3-10 bps; PULL it above 10 bps. Restore when the signal decays below 1 bps. No signal -> QUOTE both.

metric:     Paired per-condition mean `markout_5m_bps` and `net` of the gated module minus `queue_aware_ofi`, both on the same slice hash, with `rewards_usd` reported next to it.

kill:       Kill if the paired mean net improvement is < +0.3 c/share, or t < 2.0 on the condition unit on two non-overlapping slices, or if `markout_5m_bps` improves by < 0.5 c/share (the gate only fires on harmless moves), or if in > 50 % of conditions the forfeited reward exceeds the markout saved.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-binance-microprice-lead/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `bn_micro_ret_1s` | bps | binance_bbo 1 s resample of (bid*qty+ask*qty)/(qty sum) | 500 ms |
| `bn_micro_ret_5s` | bps | same, 5 s lag | 500 ms |
| `bn_toxicity` | sigma units | bn_micro_ret_1s / 60 s realized sigma | 500 ms |

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
  TO 'data/derived/ideas/cross-binance-microprice-lead/asset_map.parquet' (FORMAT PARQUET);

-- 2) signals: 1 Hz grid per symbol, joined to every condition that names it.
COPY (
  WITH b AS (
    SELECT symbol,
           CAST(venue_ts_ms / 1000 AS BIGINT) AS sec,
           arg_max((bid*bid_qty + ask*ask_qty) / nullif(bid_qty + ask_qty, 0), venue_ts_ms) AS micro
    FROM read_parquet('data/tape/binance_bbo/**/*.parquet')
    WHERE symbol IN ('BTCUSDT','ETHUSDT','SOLUSDT') AND bid_qty > 0 AND ask_qty > 0
    GROUP BY 1, 2
  ),
  g AS (
    SELECT symbol, sec,
           ln(micro / lag(micro, 1) OVER w) * 1e4 AS bn_micro_ret_1s,
           ln(micro / lag(micro, 5) OVER w) * 1e4 AS bn_micro_ret_5s,
           stddev_samp(ln(micro / lag(micro, 1) OVER w))
             OVER (PARTITION BY symbol ORDER BY sec ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) * 1e4
             AS sigma_60s
    FROM b
    WINDOW w AS (PARTITION BY symbol ORDER BY sec)
  ),
  map AS (SELECT * FROM read_parquet('data/derived/ideas/cross-binance-microprice-lead/asset_map.parquet'))
SELECT map.condition_id, g.sec * 1000 AS ts_ms,
       g.bn_micro_ret_1s, g.bn_micro_ret_5s,
       g.bn_micro_ret_1s / nullif(g.sigma_60s, 0) AS bn_toxicity
FROM g JOIN map USING (symbol)
WHERE g.bn_micro_ret_1s IS NOT NULL
) TO 'data/derived/ideas/cross-binance-microprice-lead/signals.parquet' (FORMAT PARQUET);
```

## Runnable now

**yes, no new collector.** `binance_bbo.bid|ask|bid_qty|ask_qty|symbol|venue_ts_ms` is on the tape at 100 ms since 11-04 00:37 (5.29 M rows); `data/tape/universe/*.json` supplies condition -> question. Population-limited to the crypto sentinels, so the test is markout-only.

## Provenance

- `docs/data-collection.md` (Binance 100 ms BBO) and `docs/predictive-mm-analysis.md` section 3 / Gate 3 (quote p50 52.7 ms, taker p50 1,067.7 ms, our cancel path 217.5 ms upper bound).
- Cont, Kukanov & Stoikov (2014) order-flow imbalance; the size-weighted micro-price is the standard consolidated-book estimator.

## What would change our mind

If the gate fires often but markout does not improve, the PM book is already fast enough that a sub-second Binance lead has no residual value, which would close the whole latency-anchor branch for crypto binaries.
