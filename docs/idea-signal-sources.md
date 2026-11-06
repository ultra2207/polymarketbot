# Idea signal sources

What each `ideas/<slug>/prepare.sql` reads, the window it reads, and the row
count it actually produced. Built with
`scripts/build_idea_signals.py --slug <slug>`; every table below passed the
runner's validation (required `asset_id`/`ts_ms`, numeric signals, no duplicate
`(asset_id, ts_ms)`).

Rows are per `(asset_id, ts_ms)` on a fixed grid, and the replay harness joins
them **as-of backwards** (`scripts/replay_all_markets.py::SignalBook`), so a row
stays valid from its `ts_ms` until the next row for that asset. A snapshot too
stale to use stays in the table: the module, not the SQL, applies the max-age
cut-off (that is how "absent, not zero" is implemented).

| slug | tape table(s) | window (UTC) | rows | signal columns |
|---|---|---|---|---|
| `cross-kalshi-basis-side-pull` | `kalshi_books` + `clob_events` + `data/derived/kalshi_mapping_v2.parquet` | 11-06 02:00–04:00 (two hour partitions, built by Main) | 10,240 | `basis`, `pm_age_s` |
| `cross-kalshi-trade-lead-gate` | `kalshi_trades` + mapping | 11-06 04:00–05:00 | 159,928 | `signed_flow_5s`, `flow_z`, `kalshi_age_s`, `is_block` |
| `cross-binance-microprice-lead` | `binance_bbo` + `universe/*.json` (+ `clob_events` for liveness) | 11-06 04:00–05:00 | 395,806 | `microprice_dev_bps`, `sigma_60s_bps` |
| `cross-funding-clock-window` | `binance_funding` + `universe/*.json` (+ `clob_events` for liveness) | 11-05 23:00–24:00 (hour that ends on the 00:00Z settlement) | 221,208 | `secs_to_funding`, `funding_apr` |
| `cross-uma-liveness-convergence` | `uma_events` + `universe/*.json` | whole table (272 rows) | 94 | `phase`, `proposed_p`, `secs_to_expiry` |
| `cross-tick-grid-shock` | `clob_events` (`tick_size_change`) | 11-06 04:00–05:00 | 64,800 | `secs_since_tick_change`, `tick_size` |

## Expected vs actual row counts

| slug | expected before the run | actual | why they differ |
|---|---|---|---|
| basis (Main's) | — | 10,240 | built outside this ticket; left untouched |
| trade-lead | 25 mapped tickers x 2 tokens x 3600 s = 180,000 | 159,928 | grid rows exist only after each ticker's first print in the window: median first print is at +123 s, and the median asset is missing exactly 123 s (measured `min(recv_ts_ns)` per ticker) |
| microprice | 55 live crypto conditions x 2 tokens x 3600 s = 396,000 | 395,806 | 110 assets, 3,598 rows each: the leading seconds before the first Binance tick of the hour carry no mid and are dropped |
| funding | 156 live crypto conditions x 2 tokens x 720 grid points = 224,640 | 221,208 | 312 assets x 709: the 11 grid points before the hour's first `binance_funding` row (60 s poll) have no rate |
| uma | 47 attributable conditions x 2 tokens = 94 | 94 | exact; see the limitation below |
| tick-grid-shock | 18 changed assets x 3600 s = 64,800 | 64,800 | exact |

## Measured data limits (not guesses)

- **UMA is the one slug whose data does not support its mechanism.** 272 rows
  total: 234 `ProposePrice`, 38 `RequestPrice`, 0 settle/dispute. 234/234
  proposals have `condition_id = NULL`; 37/234 (15.8 %) can be attributed by
  exact question text to a universe market, and **0 of those 37 are in the
  2026-11-06 universe snapshot or have any quote points in
  `data/derived/market_scorecard.parquet`** — they are resolved markets the
  harness never replays. The 10/38 `RequestPrice` rows that do carry a
  `condition_id` are live and quoted, but this tape gives them
  `expiry = NULL`, no matching proposal shares their `request_timestamp`, and no
  settle event exists, so `phase` never reaches 2 or 3 on a replayable market.
  The table therefore holds 94 rows of phase 1/2 for assets the replay will not
  call, and `proposed_p`/`secs_to_expiry` are NULL on 21.3 % of rows (the
  RequestPrice ones).
- **Kalshi trade flow is thin in one hour.** 1,768 prints over 25 tickers, and
  **0 block prints**, so `is_block` is all zeros until a slice with a block is
  chosen; `flow_z` starts cold because the 30-minute baseline does not exist at
  the beginning of a one-hour window.
- **The Binance micro-price deviation is much smaller than the spec assumes.**
  `microprice_dev_bps` spans -0.37 .. +0.22 bps on BTC/ETH/SOL in that hour
  (median -0.001), so the spec's +/-3 bps deadband and 10 bps pull threshold are
  never reached: at a ~$100k BTC price, 3 bps is $30 per coin, ~3,000 ticks.
  `sigma_60s_bps` has median 0.55 bps, max 1.26 bps.
- **The funding window needs two hour partitions to be fully testable.** This
  table reads only `date=2026-11-05/hour=23`, where `secs_to_funding` runs
  3,545 -> 5 s; the +30 s after settlement (the module's pull side) lives in
  `hour=00` and is deliberately not read here (one partition per query).
- **`clob_events` has no `best_bid_ask` event type.** In
  `date=2026-11-06/hour=04` the types are `price_change` (10.0 M),
  `book` (7,138) and `tick_size_change` (36), and the top-level
  `best_bid`/`best_ask` columns are null throughout; the PM mid has to come from
  `price_changes[].best_bid/best_ask`. Filtering that array on two struct fields
  inside the scan costs 36 s for one hour partition, while unnested access
  without the predicate costs 2 s, so the SQL aggregates to 1 s first and filters
  after the aggregate.
