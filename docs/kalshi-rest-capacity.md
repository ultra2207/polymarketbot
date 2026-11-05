# Kalshi REST capacity: the measured envelope

What one unauthenticated poller can pull from `api.elections.kalshi.com` from
this machine, and the parameters a stream should use. Every number below was
measured on **2026-11-05 18:53-18:57 UTC (2026-11-06 00:23-00:27 IST)**;
anything reasoned rather than measured is marked *inference*.

**Verdict.** A 233-ticker BBO poll plus a full trade tape costs
**~370 MB/hour (~8.9 GB/day)** and **~1 request/second (~10 tokens/s at
Basic)**, well inside the Basic tier's 200 tokens/s read budget. The hard
limits are structural, not volumetric: **100 tickers per `/markets/orderbooks` request**
(101 -> HTTP 400), **1,000 prints per `/markets/trades` page** (1001 -> HTTP
400), and **5,000 candles per candlestick request**. The existing poller's
`/markets?tickers=` path is 4.4x heavier per market than `/markets/orderbooks`
for the same book coverage, and the mapped trade tape moves only
**0.45 prints/s**, so a 5 s cadence with `min_ts` catch-up loses nothing.

## Measurement conditions

| item | value |
| - | - |
| Wall clock | 2026-11-05T18:53:49Z - 18:57Z (00:23-00:27 IST) |
| Host | Windows 10.0.26100 x64; `.venv` Python 3.14; requests 2.34.2, urllib3 2.8.0 |
| Auth level | **none** (public endpoints; no key, no signature) |
| Transport | `install_doh_fallback({"api.elections.kalshi.com"})` + `EdgePool.first_reachable()` TLS probe + `_new_session(ip, retry_total=0)`, keep-alive |
| Edge | CloudFront anycast: `52.84.45.103` (main run), `3.175.86.31`, `13.224.245.57` (probes) |
| Requests | 117 total across all probes, 75 in the main run; **zero 429s** |

Reproduce with `scratch/kalshi_capacity_probe.py`,
`scratch/kalshi_trades2.py`, `scratch/kalshi_candles.py`,
`scratch/kalshi_candles2.py`, `scratch/kalshi_candles3.py`; raw results in
`scratch/kalshi_capacity.json` and `scratch/kalshi_candles*.json`.

Ticker universe: the 233 distinct tickers in
`data/derived/kalshi_mapping.parquet`. Their live status split at measurement
time was **155 `active` / 78 `finalized`**.

## 1. `/markets/orderbooks` latency

Twenty sequential batches of 100 tickers, rotating over all 233 mapped
tickers, one keep-alive connection.

| metric | value |
| - | - |
| n | 20 batches (2,000 ticker-fetches) |
| HTTP 200 / books returned | 20 / 20, 100 orderbooks per response |
| p50 | **339.9 ms** |
| p95 | **477.8 ms** |
| min / max | 322.3 / 738.2 ms |
| mean | 378.7 ms |
| payload p50 | **52,061 B** (min 44,153, max 57,089) |

The max is the **first** request on a cold session (738 ms; a separate cold
probe measured 1.94 s and 2.31 s on two other edges). After that the
distribution is tight at 322-478 ms; the 20-batch run took 8.5 s wall.

Response shape (docs, confirmed by the payload keys): one entry per requested
ticker, `{"ticker": ..., "orderbook_fp": {"yes_dollars": [[price, count], ...],
"no_dollars": [...]}}`, arrays ascending by price with the best bid **last**.
Finalized tickers return an empty book (51 B for a single-market request).
`/markets/{ticker}/orderbook` still works (310 ms; 51 B on an empty book) but at
one request per market it is only usable as a repair path.

## 2. Effective batch cap

`/markets/orderbooks` takes a **repeated** `tickers` parameter
(`?tickers=A&tickers=B&...`). A comma-joined value is parsed as **one** ticker:
5 mapped tickers comma-joined returned HTTP 200 with a single book whose
`ticker` field was the entire comma string; 100 comma-joined returned 400.

| tickers in one request | result |
| - | - |
| 100 | **200**, 100 orderbooks, 11,435 B (100 open MVE tickers, mostly empty books) |
| 101 | **400** `Field validation for 'Tickers' failed on the 'max' tag` |
| 200 | **414** CloudFront HTML (`The request could not be satisfied`) |

The 414 is URL length, not count: 200 open `KXMVECROSSCATEGORY-...` tickers is
an ~11 KB URI. A legal 100-ticker request tops out near 5.7 KB, so the count cap
binds first. *Inference:* the practical URL ceiling is ~8 KB.

For contrast, `/markets?tickers=A,B,...` **is** comma-separated and took all 233
mapped tickers in **one** request: 538,667 B, 698-821 ms. Per-request batching
there costs 2,300 B/market against 521 B/market for orderbooks.

## 3. `/markets/trades`

| property | measured |
| - | - |
| Page size | `limit` max **1,000**; 1001 and 5000 -> HTTP 400 (`Limit` failed `lte`) |
| Page payload | 305,451 B (1,000 prints, ~305 B/print) |
| Page latency | p50 **328 ms**, p95 **400 ms**, range 297-402 ms (n=16) |
| Fields | `trade_id, ticker, created_time` (ISO-8601), `count_fp, yes_price_dollars, no_price_dollars, taker_side, taker_outcome_side, taker_book_side, is_block_trade` — **there is no `created_ts`** |
| Ordering | newest first, by `created_time` |
| One call reaches | the newest 1,000 prints = **1.0-5.1 s** of global tape |
| Cursor | `cursor` in every response; page 2 was contiguous (0 duplicate `trade_id`s at the boundary, no time gap) |
| Cursor + `min_ts` | the cursor **preserves the filter**: page 2 sent with cursor only stayed inside the `min_ts` window |
| `min_ts` | honoured (oldest returned >= `min_ts`) |
| `ticker=` | works (one ticker per request: 100 rows, all matching, 29,835 B); `tickers=` is ignored |

Print rate, measured three ways:

* 1,000-print page spanning **3.115 s** -> **321 prints/s** (18:53Z).
* A later page spanned **1.0 s** -> ~1,000 prints/s (a burst; the tape is
  spiky, dominated by `KXBTC15M`/`KXGOLD15M` 15-minute markets).
* A full **60 s window**, paged back to the boundary: **14,625 prints /
  64.63 s = 226.3 prints/s**. Concentration: `KXBTC15M-26NOV051400-00` alone
  was 4,229 of 14,625 (28.9%).

Mapped tickers in the same window: **29 prints / 64.63 s = 0.449 prints/s**
across 10 distinct tickers (top: `KXPRESNOMD-28-JOSS` 7,
`KXHOUSERACE-NC03-26-D` 6, `SENATEMS-26-R` 4).

Two consequences: (a) the window a fixed 1,000-row poll can cover is
**1-5 s**, so the current 8 s `trades_poll_seconds` drops prints during bursts;
(b) per-ticker filtering is not a way out, because `ticker=` takes exactly one
ticker and 233 requests per cycle is not affordable.

## 4. Candlesticks

The live path is `/series/{series_ticker}/markets/{ticker}/candlesticks`;
`series_ticker` is the ticker's first dash-separated segment (`KXPRESNOMD` for
`KXPRESNOMD-28-JOSS`). `start_ts` and `end_ts` are **required**
(missing `start_ts` -> 400 `Query argument start_ts is required`).
`period_interval` accepts **1, 60, 1440** only (5 -> 400 `oneof`).

Measured on open mapped market `KXPRESNOMD-28-JOSS`:

| request | result |
| - | - |
| 1 h, 1-min bars | 200, **1 candle** |
| 1 d, 1-min bars | 200, **14 candles** (the market was quiet) |
| 3 d, 1-min bars | 200, **2,574 candles**, 1,105,709 B |
| 4 d, 1-min bars | **400** `requested time range with candlesticks: 5760.000000, max candlesticks: 5000` |
| 180 d, 1-h bars | 200, **4,094 candles**, 1,810,592 B, first 2026-05-09T19:00Z |
| 365 d, 1-d bars | 200, 365 candles, 166,068 B |
| 1-min, 27-24 d ago | 200, 952 candles |
| 1-min, 87-90 d ago | 200, **3,406 candles** |
| `/historical/markets/{ticker}/candlesticks`, active market | **404** `failed_to_get_market_by_ticker: not_found` |

* **1-minute availability**: yes, and it goes back at least **90 days** for an
  active mapped market. The binding limit is **5,000 candles per request**
  (5,000 minutes = 3.47 days), so history is walked with successive
  `start_ts`/`end_ts` windows.
* **Sparse, not dense**: the 3-day window returned 2,574 of 4,320 possible
  minutes (59.6%). Minutes with no bar are omitted — do not index the response
  positionally and do not assume a bar exists every minute.
* The **historical** variant is only for markets archived after the cutoff; on
  an active market it 404s. Do not call it for open tickers.
* **Batch** `/markets/candlesticks?market_tickers=A,B,...` (comma-separated;
  repeated params -> 400 `multiple values for single value parameter`):
  **max 100 markets** (101 -> 400 `requested 101 markets, max markets: 100`)
  and **max 10,000 candles total** (10 markets x 24 h of 1-min = 14,400 -> 400
  `requested candlesticks across all markets: 14400, max candlesticks: 10000`).
  100 markets x 1 h of 1-min bars = 456,368 B in 432 ms.

## 5. Rate-limit accounting (from the docs, not measured)

Source: `getting_started/rate_limits` in
`C:/Users/Sivasai/Desktop/kalshi-docs.txt:10587-10790`.

| item | value |
| - | - |
| Default cost | **10 tokens per request**; `GET /account/endpoint_costs` lists the non-default ones |
| Cost of these endpoints | **10 tokens** each — *inference*: none of `/markets/orderbooks`, `/markets/trades`, `/markets/candlesticks` carries a `Rate limit:` note, so they use the default |
| Basic Predictions Read | **200 tokens/s**, bucket capacity **3 s = 600 tokens** |
| Advanced | 300 tokens/s (same 3 s bucket) |
| Expert / Premier / Paragon / Prime / Prestige | 600 / 1,200 / 2,400 / 4,800 / 12,000 tokens/s |
| Read bucket above Advanced | 1 s of budget (no 3 s pooling) |
| 429 body | `{"error": "too many requests"}`, **no** `Retry-After`, no `X-RateLimit-*`, no penalty or cooldown; the bucket keeps refilling |
| Unauthenticated reads | **no published token budget**. Measured: 20 concurrent orderbook requests all returned 200 at 17.9 rps, no 429 |

What a steady poll costs per second (signed key, Basic, 10 tokens/request):

| workload | requests/s | tokens/s | share of Basic |
| - | - | - | - |
| 233 tickers, orderbooks batch 100, **1 Hz** | 3 | 30 | 15% |
| 500 tickers, orderbooks batch 100, **1 Hz** | 5 | 50 | 25% |
| trades, 1,000-row page, **1 Hz** | 1 | 10 | 5% |
| both of the above (233 tickers + trades at 1 Hz) | 4 | 40 | 20% |

A 3-request BBO tick spends 30 of the 600-token bucket (5%); even a burst of
five consecutive ticks fits. The bucket, not the rate, would only matter if a
client polled at ~20 Hz.

## 6. Recommendation

Implement with these parameters; they are all inside measured limits.

```yaml
kalshi:
  rest_url: https://api.elections.kalshi.com/trade-api/v2
  bbo_endpoint: /markets/orderbooks   # repeated tickers params, NOT comma-joined
  bbo_batch_size: 100                 # 101 -> HTTP 400; 200 -> 414
  bbo_poll_seconds: 5
  trades_endpoint: /markets/trades
  trades_page_limit: 1000             # 1001 -> HTTP 400
  trades_poll_seconds: 5              # page back while oldest > last_seen
  trades_max_pages: 4
  candles_batch_size: 100             # 101 -> HTTP 400
  candles_window_minutes: 4320        # 3 d of 1-min = 2574 candles, under the 5000 cap
  concurrency: 1                      # sequential batches on one keep-alive session
  timeout_seconds: 20
```

1. **Batch size: 100, sequential.** Three requests cover the 233 mapped
   tickers. Do not raise it: 101 is a hard 400. Keep concurrency at 1 (a cycle
   is ~1 s at 340 ms/request); 4 is safe if a 500-ticker universe ever needs
   it, 20 was measured fine but buys nothing.
2. **BBO poll: 5 s.** 112 MB/h, 0.6 req/s, 6 tokens/s. At 2 s it costs
   281 MB/h and 15 tokens/s for book snapshots that change far more slowly.
   If sub-second book state is genuinely required, that is a WebSocket
   `orderbook_delta` job, not a faster REST loop.
3. **Trades: 5 s with catch-up paging.** Send `limit=1000` and
   `min_ts=last_seen_ts` (keep a 2 s overlap for clock skew), then follow
   `cursor` while the oldest `created_time` is newer than `last_seen`, capped
   at 4 pages. Bytes are print-rate-bound, not poll-bound: ~305 B/print x
   226 prints/s = **~250 MB/h** no matter how you slice it. Do **not** poll
   without `min_ts`; you re-read the overlap every cycle and still lose prints
   in bursts (one measured page covered only 1.0 s).
4. **Candlesticks: backfill on discovery, not on a clock.** For a newly mapped
   ticker walk 3-day 1-min windows backwards (2,574 bars, 1.1 MB, 1 request per
   3 days, 30 requests to reach 90 days of a liquid market), then 1-h bars for
   older history (180 days in one request, 1.8 MB). For the whole universe use
   the batch endpoint with 100 tickers and <=100 minutes per request to stay
   under the 10,000-candle cap.
5. **Keep `/markets` for the 5-minute heartbeat only.** It is the only endpoint
   here that carries volume, open interest, liquidity, `close_time` and
   `status` — and it costs 2,300 B/market. One 233-ticker request is 539 KB,
   so 5-minute heartbeats are ~6.5 MB/h; 1 Hz polling of it would be ~1.9 GB/h.
6. **Skip `finalized` tickers in the BBO cycle.** 78 of 233 are finalized and
   return empty books; fetch them only in the heartbeat.

Expected traffic at 233 tickers:

| stream | cadence | bytes per cycle | bytes/hour |
| - | - | - | - |
| orderbooks, 3 x 100 | 5 s | 156 KB | **112 MB** |
| trades, print-bound | 5 s | ~305 KB/page | **~250 MB** |
| heartbeat `/markets` | 300 s | 539 KB | 6 MB |
| **total** | | | **~370 MB/h (8.9 GB/day)** |

Scaling: 500 tickers is 5 orderbook requests/cycle -> 187 MB/h of books and
50 tokens/s (25% of Basic). Trades do not scale with the mapped count, only
with global tape activity.

**If the read budget is exceeded (429):**

1. Full-jitter exponential backoff, base 0.5 s, cap 60 s (the existing
   `backoff_delay`). No `Retry-After` is sent and there is no cooldown; a
   10-token request clears an empty Basic bucket in 50 ms.
2. If a cycle 429s twice, double the interval (5 -> 10 -> 20 -> 40 -> 60 s).
   **Never split a batch to retry sooner** — that multiplies requests by 2-3 and
   deepens the deficit.
3. At 60 s still 429: stop the REST BBO cycle, keep the WebSocket feed, and
   raise a health gauge. Book snapshots can be repaired from
   `get_snapshot`/`/markets/{ticker}/orderbook` once a cycle, not per ticker.
4. Never exceed 100 tickers per request (400), 1,000 rows per trades page
   (400), 5,000 candles per call (400), or 10,000 candles across a batch (400).

## 7. Contract facts an implementer will otherwise get wrong

* `/markets/orderbooks` wants **repeated** `tickers` params. Comma-joined
  silently becomes one ticker (200 + a garbage book) and eventually 400s.
* `/markets?tickers=` and `/markets/candlesticks?market_tickers=` want
  **comma-separated** values. Repeated params on the batch-candles endpoint is
  a 400.
* Trades carry **`created_time`** (ISO-8601 string), not `created_ts`. Parsing
  `created_ts` yields zeros and silently breaks `min_ts` windows.
* A market's `status` in a `/markets` payload is **`active` or `finalized`**
  (measured 155/78), not `open`/`settled` as the filter parameter values
  suggest. Filtering on `status == "open"` returns nothing.
* Trade pages are newest-first and contiguous across `cursor`; 1,000 prints is
  **1-5 s** of tape, and the tape is dominated by 15-minute crypto markets.
* Candlesticks are sparse; the response contains only minutes with a bar. Use
  `end_period_ts` as the key.
* The historical candlestick path 404s for active markets — it is for archived
  markets only.
* `/markets/orderbooks` responses are `orderbook_fp` with **bids only**
  (`yes_dollars`, `no_dollars`, `[price, count]` string pairs, best bid last).
  Yes-ask = 1 - best no-bid.

## What this does not cover

WebSocket capacity (needs a prod key; demo-only here), the signed-tier figures
for a real production key (`GET /account/endpoint_costs` is the authority), and
the per-IP limit on unauthenticated reads, which was not hit at 18 rps and is
not documented.
