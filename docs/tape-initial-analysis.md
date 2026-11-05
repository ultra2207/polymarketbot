# Tape initial analysis

Window 2026-11-04 06:07 → 2026-11-05 13:14 IST (31.1 h). This is **not** the
14-day set; it is the first full day of the rebuilt collector. Sizes and counts
come from `scripts/analyze_tape_initial.py` (`data/derived/tape_initial_analysis.txt`).

## Inventory

| Table | Size | Rows | Window | Notes |
|---|---:|---:|---|---|
| `clob_events` | 5.72 GB | 228,789,635 | 31.1 h | books, best-quote events, nested price changes |
| `clob_trades` | 26 MB | 162,530 | 31.1 h | public prints |
| `binance_bbo` | 79 MB | 2,524,455 | 31.1 h | 5 symbols, ~4.5 rows/s/symbol |
| `binance_depth` | 74 MB | 727,553 | 31.1 h | 500 ms snapshots |
| `binance_funding` / `binance_oi` | 2 MB | 6,109 each | 31.1 h | 60 s |
| `kalshi_markets` | 103 MB | 1,869,428 | 31.1 h | **1.70 M distinct tickers** (full-library history) |
| `kalshi_trades` | 3 MB | 71,140 | 31.1 h | 177 tickers |
| `polygon_logs` | 46 MB | 1,441,210 | 31.1 h | 49,471 blocks, 284,903 txs |
| `polygon_gas` | 4 MB | 47,665 | 31.1 h | |
| `gamma_comments` | 7 MB | 27,670 | 31.1 h | |
| `news_items` | 4 MB | 4,606 | 31.1 h | 5 sources |
| `whale_activity` | 18 MB | 116,025 | 31.1 h | 216 wallets, 4,302 conditions, $382.8 M |
| `health_metrics` | 2 MB | 268,108 | 31.1 h | 10 streams |
| `deribit_surface` | 2 MB | 13,608 | 31.1 h | |
| `uma_events` | — | 1 | 11:29 | OO v2 only; v3 not decoded |

## CLOB: complete, but message-level

The event table is **93.5 % `price_change` messages** (213,970,397 rows). The
frame itself carries no `asset_id`; the per-asset changes are in the nested
`price_changes` list, each with `asset_id`, `price`, `size`, `side`,
`best_bid`, `best_ask`. Sampling confirms the list is populated. This is not a
defect — the comment in `data/streams/clob.py` says the venue's list is kept
verbatim — but it shapes every query:

* `build_l3_queue.py`, `backtest_quotes.py`, `measure_lead_time.py`,
  `report_tape_costs.py` already `UNNEST(price_changes)`, so the core analysis
  paths are correct.
* The other 14.8 M rows are usable directly: 14,263,929 `best_bid_ask`,
  464,682 `book`, 88,216 `new_market`, 2,026 `tick_size_change`, 292
  `market_resolved`, 84 `gap`, 9 `heartbeat`.
* `event_hash` is null on `price_change`, so the 228.3 M "duplicate" rows in the
  raw count are an artifact of the null hash, not duplicates. Do not use that
  column for dedup on this event type.
* The tape touches **17,460 conditions / 22,614 assets** over 31 h; the current
  manifest is 2,493 markets / 4,986 assets. The extra conditions are universe
  churn and the nested lists. Filter to `manifest_current.json` for
  current-universe work.

## Trades and liquidity

* 162,530 prints across 3,083 conditions and 5,091 assets; **$33.4 M** notional,
  median print **$9.99**, median price 0.53, max single print $612 k.
* Top conditions by prints are the same high-volume events the collector was
  built around (LoL/Worlds, the presidential nominee, Senate markets).
* The scorecard's 3 h window: 2,511 conditions, **834 traded**, **832
  fee-known**, median spread 3.0 c, median half-spread 1.5 c. Markout(300 s)
  median **-0.5 c**; only **28** conditions have ≥30 matched markouts. The
  earlier summary reports 76/129 Gate 1 passes, all flagged low-sample. **No
  Gate 1 conclusion is justified from 31 h.**

## Kalshi: two populations in one table

1,869,428 rows over **1,704,934 distinct tickers** is the full-library fallback
history (the stream pages all open markets when the mapping is absent). The
current stream polls the 233 mapped tickers every 2 s. For analysis, filter to
`data/derived/kalshi_mapping.parquet`; the full-library rows are not the live
monitor set and are not comparable to the 2 s series. This is the clearest
"old data in the same table" hazard in the tape.

## What this means for the trader

* Market selection: enough trade flow exists in the reward universe
  ($33 M/day notional, 834 traded conditions in 3 h), but the **edge is not
  established** — spread 3 c, markout -0.5 c, low sample. No parameter or
  strategy change is justified yet; the 14-day tape is the evidence.
* Execution: the CLOB tape is good enough for L3/queue work *if* queries go
  through the nested changes; the fill model is still calibrated only on public
  prints, never on our fills.
* The private side is still missing: our orders, fills, queue position, account
  state, and actual reward accrual are not in the tape. That is the
  architecture in `docs/live-trading-data-architecture.md`.

## Changes recommended from this analysis

1. **Keep the public collector as-is.** The 5.7 GB/day CLOB rate is the budgeted
   rate; the nested schema is correct. Do not "fix" it into one row per change
   unless a consumer needs it; the existing consumers unnest.
2. **Filter Kalshi to the mapping** in every report. Optionally tag the stream
   source (`mapped` vs `full_sweep`) so the two populations cannot be mixed
   again.
3. **Filter current-universe reports to the manifest**; the 17,460/2,493
   condition difference is universe churn, not extra coverage.
4. **Add the live session recorder** (private user orders/fills/account) before
   the real-money pilot. This is the P0 gap; the public tape cannot substitute
   for it.
5. **No strategy change from 31 h.** The data does justify running the
   real-money pilot as a recorded session rather than a blind quote loop; it
   does not justify changing spreads, sizes, or market selection yet.
