# Data collection runbook

The 14-day tape collector. One process, one asyncio loop, one writer thread, a
JSONL write-ahead log per table, and hourly Parquet parts.

## Why this exists

`report.md` and `docs/two-week-data-plan.md` define the institutional data
foundation. This runbook is the operational half: what runs, where it writes,
how to prove it is alive, and how to restart it without losing an hour.

Two corrections to the master plan are baked into this implementation:

* **The universe rolls.** The 2026-11-01 snapshot's 1,028 markets carry
  $65,793/day, but 709 of them ($49,915/day, 75.9%) have an `endDate` before
  2026-11-18 and 677 ($44,380/day) end in the week of the 2026-11-02 midterms.
  A frozen 14-day set would lose most of its pool in the first two days. The
  collector re-cuts Gamma hourly (`rewards_daily_rate > 15`, active campaigns
  only), writes a versioned manifest, and tells the CLOB sockets which assets
  to add or drop. Between full walks it polls `GET /markets` every 60 s for
  newly created and newly closed markets (`universe_poll_seconds`), so a new
  market is subscribable within a minute and a resolved one is dropped without
  waiting for the hour. The full keyset walk is the authoritative
  reconciliation; a restart loads the cached manifest and
  `universe_state.json` and schedules the next walk from the cached
  `last_full_walk_at + 1 h`, not from the restart time. The CLOB stream also
  drops a market immediately on a venue `market_resolved` frame.
  At 2026-11-04T01:00Z the live cut is **1,538 markets / $108,466/day**, not
  the snapshot's 1,028 / $65,793: campaigns changed after 11-01 (for example
  `0x9345d514…` went from $500 to $2,000/day on 11-02). The election markets
  resolve at 04:59Z and the next hourly re-cut drops them. At
  2026-11-04T10:10Z the live cut is **3,255 markets / $241,157/day**; the
  midterms resolved and new campaigns opened.
  `clob.universe_mode` selects the policy: `rolling` (default) is the behaviour
  above and records every refresh under `data/tape/universe/<stamp>.json`, so
  the membership at any tape hour is recoverable. `locked` pins the set to
  `clob.locked_manifest` (the 1,028-market / $65,793/day snapshot), keeps only
  those condition ids, retains a pinned market absent from a Gamma page, and
  logs `locked universe: N/1028 markets present at Gamma`; it never adds a
  market outside the pinned set. Use `locked` only when the analysis needs the
  frozen universe; the rolling cut is what the tape has been collecting.
* **The tape is event-driven.** A row is written when the book changes, not at
  a fixed 1 Hz. The 1 Hz grid is reconstructed downstream with `ASOF JOIN`.
  Quiet markets get a 60 s liveness heartbeat, not 60 identical rows.

## Streams

| Stream | Transport | Cadence | Tables |
|---|---|---|---|
| `clob` | CLOB WS, 8 shards, DoH | event-driven + 60 s heartbeat | `clob_events`, `clob_trades` |
| `binance` | Binance WS + REST | 100 ms BBO/depth, 1 s mark, 60 s funding/OI | `binance_bbo`, `binance_depth`, `binance_funding`, `binance_oi`, `binance_liquidation` |
| `deribit` | Deribit REST | 10 s | `deribit_surface` |
| `kalshi` | Kalshi REST (no key) | 2 s mapped-ticker poll (100/request), 8 s trade feed, BBO change-detected + 5 min heartbeat; mapping is the audited v2 artifact (107 pairs) | `kalshi_markets`, `kalshi_trades` |
| `kalshi_books` | Kalshi REST `/markets/orderbooks`, no key | 5 s, repeated `tickers=` params, 100/request, unchanged books re-emitted at 60 s | `kalshi_books` |
| `news` | RSS + Gamma (+ optional NewsAPI) | 60–120 s (NewsAPI hourly) | `news_items`, `gamma_comments` |
| `polygon` | WSS `eth_subscribe`, provider rotation | real-time logs + blocks; 10-block HTTP fallback, 10 s gas sample | `polygon_logs`, `polygon_gas` |
| `whales` | Polymarket Data API v2 | 1 h | `whale_activity` |
| `uma` | Polygon JSON-RPC `eth_getLogs` | 60 s poll, 10-block windows | `uma_events` |
| `health` | internal | 30 s | `health_metrics` |

### Access notes

* **Polymarket CLOB WS** needs the DNS-over-HTTPS fallback. The local resolver
  returns IPv6-only answers that fail; `polymarketbot.net.install_doh_fallback()`
  pins IPv4 and caches. The orchestrator installs it before any stream starts.
* **Kalshi** market data is public over REST. The WebSocket returns HTTP 401
  without an API key, so the stream polls REST and emits only changed markets
  plus a 5 min heartbeat. Two facts shape the polling design:

  * `GET /markets` caps a page at `limit=1000` (2,000 → HTTP 400) and there are
    **> 20,000 open markets**; a 1,000-market page is ~5 MB. `?status=open`
    returns the stable list in a fixed order, so the front of it is the
    `KXMVECROSSCATEGORY` parlay block — the first 3,000 open markets are 100 %
    parlays (verified over pages 1–30 on 2026-11-04), so a 3,000-market cap
    captured *only* parlays. That is why the stream now polls a mapped
    ticker subset: `?tickers=A,B,...` returns 100 markets in ~480 KB / ~0.3 s,
    so a 500-ticker poll is ~5 requests and ~2.5 MB.
  * `?event_ticker=`, `?series_ticker=` and `?tickers=` (comma list, ≥100
    verified) all filter server-side, which is what makes targeted polling
    possible.

  **Measured:** a mapped poll of 233 tickers is 3 requests / 1.54 s and
  returns all 233 markets with zero resets (2026-11-04T06:29Z); the trade feed
  carries **~104 prints/s** globally (1,000-print window = 9.6 s). **Rate
  limit:** no HTTP 429 at 10 requests/s for 3 s or 5 requests/s for 5 s from
  this IP. At the old 15 s cadence the stream averaged **~0.33 requests/s**;
  a 2 s cadence is **~1.6 requests/s**, still well below what the API has been
  observed to sustain. A 60 s probe on 2026-11-05 (30 rounds × 3 requests at
  2 s) returned **30/30 rounds, 0 resets, 0 retries**, ~0.5 s per round, so the
  stream now polls every 2 s. The real constraint on trades is the 1,000-print
  window (poll faster than 9.6 s or lose prints); 8 s stays.
  Trades are then filtered to mapped tickers client-side — 3,000 global prints
  yielded 273 mapped ones in a 26 s smoke run.
* **Kalshi edge resets (WinError 10054).** `api.elections.kalshi.com` answers
  with four A records. Some edges accept the TCP handshake and then reset the
  **TLS** handshake, so a TCP-only reachability probe (like
  `net._tcp_reachable`) calls them healthy. The Windows resolver returns the
  same address order on every lookup, so a client pinned to a bad edge fails
  100 % of polls until the DNS TTL expires — that is the ~180
  `ConnectionResetError 10054` warnings in `data/logs/data.kalshi.log` between
  06:33 and 11:10 local, not a size or rate problem. The stream now resolves
  every A record, probes each with a TLS handshake, pins a working edge, and on
  a reset excludes that edge for 300 s, rebuilds the session against the next
  candidate and retries with full-jitter exponential backoff.
* **Polygon** uses `polymarketbot.chain.rpc_url()`, which rewrites the Alchemy
  app key in `ETH_RPC_URL` to `polygon-mainnet`. Alchemy's free tier caps
  `eth_getLogs` at 10 blocks, so the primary path is WSS `eth_subscribe`; the
  HTTP path walks 10-block windows and never skips a block. Providers are
  rotated on a 429 and rested (see below).
* **News** has publisher RSS/Atom feeds, Polymarket Gamma comments and one
  optional paid-capable adapter:

  | Wire | Cost | Enabled by | Notes |
  |---|---|---|---|
  | Publisher RSS/Atom (Google News, CoinDesk, Cointelegraph, Decrypt) | free | `news.rss_urls` | one task per feed |
  | Polymarket Gamma comments | free | always | top-`gamma_comment_events` markets, per Event |
  | NewsAPI.org (`/v2/everything`) | free Developer tier (100 req/day, dev use only); paid plans for production | `NEWSAPI_KEY` env var and `news.newsapi_queries` | off by default; skipped when the env var is unset |

  **GDELT was removed on 2026-11-05**: it answered bursts from this IP with
  HTTP 429 and a circuit open for 900 s, so the stream produced no usable news
  after 11:58 UTC on 2026-11-04. No GDELT key or query remains in the config.
  **AP Wire and Tree News have no public API**, so they are not implemented.
  Only documented public endpoints are wired; adding a credential for an
  undocumented feed is out of scope.

### Kalshi mapping

The collector polls Kalshi by ticker, so it needs a list of Kalshi tickers that
mean the same thing as a Polymarket condition. There is no shared id; the map is
derived deterministically from structure and tokens (no model calls) by
`scripts/build_kalshi_mapping.py` → `data/derived/kalshi_mapping.parquet`:

1. read `data/tape/manifest_current.json` (the live universe; 3,255 markets
   at 2026-11-04T10:10Z);
2. crawl Kalshi's **~20,100 open events** (titles only, ~160 KB per 200,
   20,063 at 2026-11-04T06:28Z) — not the 20k+ open markets, which would be
   ~100 MB of payload;
3. keep events whose structure (office/state/district/party/year/legislature)
   or title tokens could match a question — 3,741 candidates — and fetch their
   markets only: `?series_ticker=` for series with ≥ 8 candidates, one
   `?event_ticker=` request otherwise (18,684 market rows in 179 s);
4. score every Polymarket × Kalshi title pair and keep the best match per
   Polymarket market above the confidence floor (default 0.5). One ticker maps
   to at most one question: a shared ticker means one of the two rows is
   wrong, so the strongest match keeps it.

Current artifact (2026-11-04T06:28Z): **233 rows, 15.2 % of the universe**,
one ticker per row.

| method | confidence (observed) | rows | rule |
|---|---|---:|---|
| `title_exact` | 0.97 | 6 | identical normalised titles |
| `structured_winner` | 0.80–0.95 | 103 | same office/state/district/party/year, both a win claim |
| `title_fuzzy` | 0.73–0.85 | 41 | token Jaccard ≥ 0.62 and no conflicting number |
| `margin_threshold` | 0.56–0.59 | 64 | PM "by 9 %–12 %" bucket vs Kalshi "at least 9 pts" rung |
| `turnout_threshold` | 0.53–0.54 | 14 | PM vote-count range vs Kalshi "above N votes" |
| `structured_contest` | 0.60–0.62 | 5 | same contest, neither title names the claim type |

Tokens are folded (case, accents, plurals, `nomination`/`nominate`,
`presidential`/`president`) and numbers are preserved, so two different rungs of
the same ladder (`at least 9 %` vs `at least 15 %`, `below 190 seats` vs
`at least 218 seats`) are rejected instead of being joined. Gates on
office/state/district/year/party and on *claim type* (winner vs margin vs
turnout vs vote share vs seat count) reject near-miss contests — that is what
stopped "will the Democrats sweep Texas" from matching a Democratic
margin-of-victory ladder. `local_legislature` separates a state House district
from a US House seat, which otherwise collide on state + district number.

**Coverage is deliberately conservative.** The unmapped 85 % are mostly the
2026 midterm bucket markets (`by 9 %–12 %`, 567 questions): Kalshi's
margin-of-victory ladders only exist at rungs like 1/2/5/10/13/15/26 %, and a
9 %–12 % bucket is not equal to "at least 10 %", so no row is emitted. A wrong
cross-venue join is worse than a missing one. `margin_threshold` and
`turnout_threshold` rows are *derived*, not equal claims: a PM bucket equals
Kalshi `≥ low` minus `≥ high`, which is exactly what an ISO/BBO spread needs,
but the pair is not a legal arbitrage leg.

Columns: `polymarket_condition_id`, `kalshi_ticker`, `confidence`, `method`,
`kalshi_event_ticker`, `polymarket_question`, `kalshi_title`. The stream loads
the ticker column at start and re-reads it when the file mtime changes
(`mapping_refresh_seconds`, default 900 s), so an hourly universe re-cut only
needs the builder re-run.

### Polygon RPC providers

Measured incident, 2026-11-04 (`data/logs/data.polygon.log`): Alchemy answered
**510 WebSocket handshakes with HTTP 429** between 01:39:49Z and 05:56:51Z
(4 h 17 min and still failing when this was written) while HTTPS on the same key
never returned a 429 — the HTTP fallback kept serving rows for the whole window.
The stream retried one endpoint on a fixed ladder with no jitter (506 of the 510
attempts at the 30 s cap, 2 handshakes/min). The cause was not the compute
budget: the fallback ran `eth_blockNumber` + one 10-block `eth_getLogs` + one
`eth_getBlockByNumber` per missed block, about 50 calls/min
(40 headers + 6 `eth_getLogs` + 6 `eth_blockNumber`, ~19 CU/s against a 300 CU/s
free-tier ceiling). Free-tier facts
below are from provider documentation; "probed" means verified against the live
endpoint on this host.

| Provider | Free tier | WSS | Historical `eth_getLogs` | Key | Status here |
|---|---|---|---|---|---|
| Alchemy | 300 CU/s throughput, evaluated over a 10 s rolling token bucket (Alchemy docs) | yes | yes, 10-block cap on the free tier | required | **primary**: only endpoint serving archive logs on a free tier |
| dRPC | 210M CU/30 days, 120k CU/min per IP (minimum regional 50.4k CU/min) | yes, `wss://polygon.drpc.org` (probed) | yes, 10,000-block cap per call (measured 2026-11-01) | not required | **fallback 1** |
| PublicNode | free, no key | yes, `wss://polygon-bor-rpc.publicnode.com` | live windows yes; archive ranges answer "require a paid plan" (measured 2026-11-01) | not required | **fallback 2** |
| Ankr | public/freemium ≈1800 req/min guaranteed (Ankr docs) | `wss://rpc.ankr.com/polygon/ws` | not probed | optional | opt-in via `POLYGON_RPC_URLS` |
| QuickNode | credit-metered free tier | yes | yes, range cap depends on plan | required | not probed |
| Infura | request-metered free plan | yes | yes, range cap depends on plan | required | not probed |
| Self-hosted Erigon/Bor | no external quota | yes | full archive | none | multi-TB NVMe + long sync; the only option that removes the quota |

**Chosen order:** Alchemy → dRPC → PublicNode → (Ankr when configured). It is
the order of `polymarketbot.chain.rpc_urls()`, and the socket path uses the same
list, not just HTTP. Rationale: only Alchemy and dRPC served archive logs for
free, dRPC is keyless so it survives a dead key, and PublicNode is a third
opinion with a different host. Ankr adds a fourth independent operator; enable
it with `POLYGON_RPC_URLS=https://rpc.ankr.com/polygon` rather than hard-coding
it, because its public `eth_getLogs` range cap is unverified here.

Rotation rules (`polymarketbot.chain.ProviderPool`):

* A **429 rests that provider**, it does not fail the call: the next provider is
  tried in the same pass. Cooldown starts at 60 s and doubles per consecutive
  429 to a 900 s cap, with ±25 % jitter, and resets on the first success.
* **Retry waits are capped exponential with equal jitter** (`jittered_delay`),
  so peers that failed together do not return in lockstep.
* **The socket rotates too.** A 429 on a WSS handshake cools that provider and
  dials the next one after a 5 s HTTP top-up, instead of pinning the stream to
  HTTP for the length of the cooldown.
* Writes never rotate: `eth_sendRawTransaction` still goes to `write_url` only.
* Gas is a **sample**: one `eth_getBlockByNumber` for the newest head, at most
  once per `gas_poll_seconds` (10 s), and the skipped blocks are counted in
  `gas_blocks_skipped` instead of logged as gaps.
* **Logs are backfilled, not skipped.** The catch-up walks every window from the
  contiguous `backfill_block` cursor to head in 10-block `eth_getLogs` calls,
  with a 90 s budget per invocation; a background loop retries every 30 s until
  the gap is zero. On startup the cursor is seeded from the tape's
  `MAX(block_number)`, so a restart closes its own gap instead of leaving the
  first live block as a hole. The old floor at `head - span + 1` left a
  permanent hole after every outage longer than one window (measured 2026-11-05:
  5,000–13,000-block holes). `backfill_gap_blocks` reports the remaining gap.

Per-provider health, published every 5 s under `stream="polygon"`:

| Metric | Type | Meaning |
|---|---|---|
| `provider_<name>_cooldown_s` | gauge | seconds that provider must still rest; 0 when healthy |
| `provider_<name>_rate_limits` | gauge | 429s seen on that provider |
| `provider_<name>_errors` | gauge | non-429 failures on that provider |
| `rate_limited` | counter | 429s that triggered a socket rotation |
| `reconnects` | counter | socket sessions that ended abnormally |
| `gas_blocks_skipped` | counter | blocks not sampled between two gas samples |
| `gas_stale_samples` | counter | samples dropped because the provider was behind the covered head |
| `backfill_gap_blocks` | gauge | blocks between the contiguous log cursor and head; 0 is fully covered |
| `backfill_budget_exceeded` | counter | catch-up batches that hit the 90 s budget and will resume |
| `backfill_errors` | counter | background backfill attempts that raised; the loop retries |

### News source metrics

The `health` stream snapshots these under `stream="news"` every 30 s. The
per-query/per-feed suffixes come from `metric_slug` (lowercase, non-alphanumeric
runs collapsed to `_`).

| Metric | Type | Meaning |
|---|---|---|
| `rss_errors`, `rss_errors_<host>` | counter | feed failures, isolated per feed |
| `rss_429_<host>` | counter | per-feed 429s |
| `rss_backoff_seconds_<host>`, `rss_circuit_open_<host>` | gauge | per-feed backoff state |
| `newsapi_429`, `newsapi_errors`, `newsapi_disabled` | counter | optional wire health |
| `rss_rows`, `gamma_rows`, `newsapi_rows` | counter | rows emitted per source |
| `rows_emitted` | counter | total rows emitted by the news stream |

A source that raises is restarted by the supervisor only if the whole stream
fails; each poller is a separate task, so one feed's exception or backoff is
contained and every other feed keeps polling.

### UMA oracle stream

`streams.uma` polls the UMA Optimistic Oracle and Polymarket's resolution
adapters over HTTP JSON-RPC and writes decoded lifecycle events to
`uma_events`. It is independent of `polygon` (which subscribes over WebSocket
and tapes raw logs): the stream works even when the socket path is down, and it
does not require the `polygon` stream to be enabled.

**Source addresses.** Verified before they were trusted; provenance is recorded
in `### Addresses, and the config mismatch` below.

| Address | What it is | How it was verified |
|---|---|---|
| `0xee3afe347d5c74317041e2618c49534daf887c24` | UMA Optimistic Oracle V2 | `eth_call optimisticOracle()` on the v2 adapter returned it (2026-11-04); matches UMA's OOv2 Polygon deployment |
| `0xCB1822859cEF82Cd2Eb4E6276C7916e692995130` | `UmaConditionalTokensBinaryAdapter` v1.0.0 (old) | PolygonScan verified contract name/ABI; still settles pre-cutover markets |
| `0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74` | `UmaCtfAdapter` v2.0.0 (current) | PolygonScan verified contract name/ABI; Polymarket docs contracts page |

**Poll loop.** Every `poll_seconds` (60 s) the stream reads the head and fetches
`eth_getLogs` in `log_block_span` windows (10 blocks, the Alchemy free-tier
cap), up to `max_windows_per_poll` (60) windows per poll. A cold start begins
`initial_lookback_blocks` (600, ~20 min of Polygon blocks) behind the head; a
deeper gap closes across consecutive polls instead of in one burst. Windows are
paused `window_pause_seconds` apart, provider 429s rotate through
`ProviderPool` with a cooldown, and a failed window is retried with capped
jittered backoff before the poll gives up and retries on the next cadence.
There is no WebSocket subscription.

**Schema (`uma_events`).**

| Column | Type | Meaning |
|---|---|---|
| `recv_ts_ns`, `recv_mono_ns` | int64 | collector receive time (wall clock / monotonic) |
| `ts` | int64 | **block timestamp, epoch milliseconds** — the event time |
| `block_number`, `block_hash`, `tx_hash`, `tx_index`, `log_index` | int64 / string / int32 | log identity |
| `address`, `contract`, `event` | string | emitting address, `optimistic_oracle_v2` / `uma_ctf_adapter_v1` / `uma_ctf_adapter_v2`, ABI event name |
| `requester`, `proposer`, `disputer` | string | addresses when the event carries them |
| `identifier`, `identifier_text` | string | bytes32 hex and printable ASCII (`YES_OR_NO_QUERY`) |
| `request_timestamp` | int64 | OO request timestamp (adapter `requestTimestamp`/`ResolutionDataRequested`) |
| `resolution_time`, `expiry` | int64 | adapter resolution time; proposal expiration |
| `ancillary_data`, `ancillary_text` | string | raw hex; UTF-8 text when the bytes decode cleanly |
| `proposed_price`, `settlement_price` | string | int256 as decimal strings, never clipped |
| `disputed` | bool | TRUE for `DisputePrice`, FALSE for `ProposePrice`, `disputer != 0` for `Settle`, NULL elsewhere |
| `payouts` | string | adapter `uint256[]` as text (`[0, 1]`) |
| `currency`, `reward`, `final_fee`, `proposal_bond` | string | amounts as decimal strings |
| `early_resolution`, `emergency_resolution` | bool | adapter flags |
| `condition_id`, `market_slug`, `question` | string | universe mapping (see below) |
| `removed` | bool | provider reorg flag |

Unknown fields are NULL, never zero. Rows are de-duplicated by
`(tx_hash, log_index)`.

**What is recorded.** Optimistic Oracle V2 `RequestPrice`, `ProposePrice`,
`DisputePrice`, `Settle` — for every requester, not only Polymarket's adapter —
plus the adapter lifecycle: `QuestionInitialized`/`QuestionUpdated`,
`QuestionReset`, `QuestionResolved`, `QuestionSettled`,
`QuestionEmergencyResolved`, `QuestionPaused`/`QuestionUnpaused`,
`QuestionFlagged`/`QuestionFlaggedForAdminResolution`,
`ResolutionDataRequested`, and `AncillaryDataUpdated`. Decoding is keyed by
`(contract, topic0)` from the verified ABIs in
`src/polymarketbot/data/uma.py`; a known topic from an unexpected address is
rejected rather than mis-decoded.

**Market mapping.** OO logs carry `keccak256(ancillaryData)` and the question
text inside the ancillary data, not a Polymarket condition id. The stream
parses `q: title: <question>, description:` and matches that title exactly
(whitespace-normalised, case-folded) against `ctx.universe.markets`. A unique
match fills `condition_id`, `market_slug` and `question` from the manifest; a
question shared by more than one manifest market is ambiguous and maps to
nothing. An unmapped title leaves `condition_id`/`market_slug` NULL but keeps
the chain-supplied `question`. Nothing is guessed. Coverage is limited to the
reward-market universe: most of Polymarket's ~240 k markets are not in the
manifest, so expect NULL ids for most events.

**What is not recorded.** Off-chain dispute detail (UMA DVM vote, forum/Slack
discussion, dispute bonds beyond the on-chain amounts), `UmaCtfAdapter` v3.1.0
events (address `0x157Ce2d672854c848c9b79C49a8Cc6cc89176a49` is documented but
neither configured nor decoded), and the lifecycle events of third-party
adapters that request prices at the same OO (their OO events are recorded and
carry `requester`). `disputed` is point-in-time: a `ProposePrice` row stays
FALSE if a later `DisputePrice` follows; join on `question_id` in the derived
table or on `(request_timestamp, ancillary_text)` here.

**Resumability anchor.** `<root>/uma_state.json` (default
`data/tape/uma_state.json`) stores `last_block`, the highest block whose window
was fully decoded, plus a bounded set of recent `(tx_hash, log_index)` keys.
The anchor is written atomically after every window, so a kill loses at most
one window; a poll resumes at `last_block + 1`, and the persisted keys prevent
a row that is already in the tape from being rewritten after a reorg roll-back
or a manual anchor reset. A corrupt state file is logged and starts cold.

**Health.** The `health` stream snapshots `stream="uma"`: counters `polls`,
`errors`, `events`, `duplicates`, `undecodable_logs`, `malformed_logs`; gauges
`last_block`, `last_poll_age_s`.

```bash
# smoke run against the live RPC; separate tape root
.venv/Scripts/python.exe scripts/run_data_collector.py \
  --streams uma --duration 60 --root data/tape_smoke_uma
```

## Storage layout

```
data/tape/
  <table>/
    date=YYYY-MM-DD/
      hour=HH/
        part-<run_id>-<YYYYMMDDTHH>-<seq>.parquet
  _wal/<table>/<run_id>/<YYYYMMDDTHH>-<seq>.jsonl
  universe/<YYYYMMDDTHHMMSS>.json
  manifest_current.json
  universe_state.json
  runs/<run_id>.json
```

* The WAL is appended and fsynced every 60 s. A kill loses at most 60 s.
* A WAL part is rotated every 10 minutes and compacted to one Parquet part with
  ZSTD level 7 and dictionary encoding. A burst hour cannot force a multi-GB
  in-memory conversion.
* On restart, `WriterHub.register()` compacts any orphaned WAL for that table
  before new rows are written. No data is discarded.
* `manifest_current.json` is the current universe; `universe/` keeps every
  version. `runs/<run_id>.json` records config, stream list, start/end and
  writer counters.
* `universe_state.json` records the last full-walk and poll timestamps and the
  highest Gamma market id. It is rewritten every poll (a few hundred bytes), so
  the 2 MB manifest is only rewritten when the set actually changes.
* Human-facing timestamps (logs, `universe_state.json`, `manifest_current.json`,
  run meta, run IDs, check-ins) are IST (UTC+05:30). Parquet `date=/hour=`
  partitions and `recv_ts_ns`/`recv_mono_ns` stay UTC.
* `clock_offset/date=YYYY-MM-DD/hour=HH/part-clock-<run>-<seq>.parquet` holds
  the NTP-style offset probes (one part per probe run; see
  [Clock offset](#clock-offset-and-cross-venue-timing)). A probe is a short
  out-of-process job, not a collector stream, so the collector never registers
  this table: the probe writes one part and exits.

## Runbook

Preflight (first run is capped at 300 s):

```bash
.venv/Scripts/python.exe scripts/run_data_collector.py --duration 300
```

One stream only:

```bash
.venv/Scripts/python.exe scripts/run_data_collector.py --streams clob --duration 60
```

14-day run (run it in a persistent terminal; the supervisor restarts a crashed
stream with backoff, and the WAL recovers on restart):

```bash
.venv/Scripts/python.exe scripts/run_data_collector.py --allow-long --duration 0
```

Stop with Ctrl-C. The collector cancels streams, flushes and compacts the
current WAL part, writes the run metadata, and exits.

Smoke runs by parallel agents use `--root data/tape_smoke_<name>` so they never
touch the production tape.

### Long-running supervision

`scripts/watchdog_collector.py` is the missing restart supervisor. It looks for
a live `run_data_collector.py` (it handles the uv venv trampoline, which makes
the process appear twice), starts one detached when none exists, and never kills
a slow but live process. Install it for the current user, no admin needed:

```bash
schtasks /create /tn PolymarketCollector /tr "\"C:\Users\Sivasai\Documents\GitHub\polymarketbot\.venv\Scripts\python.exe\" \"C:\Users\Sivasai\Documents\GitHub\polymarketbot\scripts\watchdog_collector.py\" --once" /sc minute /mo 120 /f
```

The registered task repeats every 2 hours (the collector itself runs detached,
so its lifetime is not tied to the checker). Its decisions go to
`data/logs/watchdog_collector.log`; the monitor's alerts go
to `data/logs/collector_alerts.log`. A sleeping host stops every stream, so for
the two-week window run `powercfg /change standby-timeout-ac 0` as well. The
full gap list between this tape and what the live trader needs is in
[docs/live-data-gap-analysis.md](live-data-gap-analysis.md).

### Universe metadata: event grouping and fees

The CLOB reward walk returns `event_id`/`event_slug` but no fee schedule, and
the 2026-11-01 Gamma snapshot predates most of the current universe. Refresh
the fields keyed by the live manifest:

```bash
.venv/Scripts/python.exe scripts/refresh_universe_metadata.py
```

That is 25 Gamma requests (~8 s for 2,496 markets). It writes
`data/derived/universe_metadata.parquet` and backfills every manifest market
with `event_id`, `event_slug`, `event_title`, `fee_type`, `fee_rate`,
`fee_exponent`, `fee_rebate`, `resolution_source`, `tick_size` and
`min_order_size`. Fee-disabled markets become `zero_fees` (a measured 0, not
an unknown). The collector reloads the parquet whenever it writes the
manifest, so a refresh survives the next 6-hour universe walk. The scorecard
and backtest scripts prefer the live table over the snapshot; as of
2026-11-05 that moved fee coverage from 138/647 to 832/834 traded conditions.

## UMA resolution events (derived)

`data/derived/uma_events.parquet` is rebuilt from `polygon_logs` by
`scripts/replay_uma.py`. It answers the questions Gamma cannot: when a price was
requested, who proposed it, at what price, whether it was disputed, when the
proposal expires, and when it settled.

### Addresses, and the config mismatch

| Config key | Address | What the address actually is | Evidence |
|---|---|---|---|
| `polygon.addresses.ctf` | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | Conditional Tokens Framework (ERC-1155) | verified CTF ABI; transfer logs |
| `polygon.addresses.uma_oo` | `0xee3afe347d5c74317041e2618c49534daf887c24` | UMA Optimistic Oracle V2 on Polygon | `eth_call {to: 0x6A9D…F74, data: 0x22302922}` (`optimisticOracle()`) returned this address on 2026-11-04; matches UMA's OOv2 deployment |
| `polygon.addresses.uma_v1_adapter` | `0xCB1822859cEF82Cd2Eb4E6276C7916e692995130` | `UmaConditionalTokensBinaryAdapter` (v1 adapter), PolygonScan name tag "Polymarket: UMA CTF Adapter Old" | PolygonScan verified contract name + ABI (`QuestionInitialized`, `ResolutionDataRequested`, `QuestionSettled`, …) |
| `polygon.addresses.uma_adapter` | `0x6A9D222616C90FcA5754cd1333cFD9b7fb6a4F74` | `UmaCtfAdapter` v2.0.0 | PolygonScan verified contract name + ABI; 56,402 txs |

`uma_oo` is the Optimistic Oracle, not the v1 adapter. The old config pointed
`uma_oo` at `0xCB18…5130`, which the logs show is the v1 adapter ABI; that
address is now `uma_v1_adapter`. The v1 adapter is old (last transaction
2026-10-19) and is retained for existing tape parts; the v2 adapter's immutable
`optimisticOracle` is `0xee3a…87c24`.

Consequence: the collector now subscribes to four addresses
`[CTF, OO, v1 adapter, v2 adapter]`, so RequestPrice / ProposePrice /
DisputePrice / Settle at the OO are taped. A probe over the last 200 blocks
saw 0 OO events (the rate is bursty: 16, 8, 0, 0 per adjacent 50-block
windows in an earlier probe); the tape window already holds 43 OO logs and 34
decoded `ProposePrice` rows.

### ABI and topic0

Decoding is keyed by `(contract, topic0)`, never by topic0 alone: both adapter
versions emit `QuestionReset(bytes32)`, `QuestionPaused(bytes32)` and
`QuestionUnpaused(bytes32)` with byte-identical signatures.

| Contract | Event | Topic0 |
|---|---|---|
| Optimistic Oracle V2 | `RequestPrice(address,bytes32,uint256,bytes,address,uint256,uint256)` | `0xf1679315ff325c257a944e0ca1bfe7b26616039e9511f9610d4ba3eca851027b` |
| Optimistic Oracle V2 | `ProposePrice(address,address,bytes32,uint256,bytes,int256,uint256,address)` | `0x6e51dd00371aabffa82cd401592f76ed51e98a9ea4b58751c70463a2c78b5ca1` |
| Optimistic Oracle V2 | `DisputePrice(address,address,bytes32,uint256,bytes,int256)` | `0xdb5d525cf38fcf3b5f15304610b0482cea1a86f2c3aac48adf04617cf8a84490` |
| Optimistic Oracle V2 | `Settle(address,address,address,bytes32,uint256,bytes,int256,int256)` | `0x44b71f4dd337078450f2f5aa7bf770c1b0005de6ccd88581311987934eaa5711` |
| Adapter v1 | `QuestionInitialized(bytes32,bytes,uint256,address,uint256,uint256,bool)` | `0x53af6c2e40505bb55a47d917203a12c8f063b6c505c11094d98ac8f772e8a031` |
| Adapter v1 | `ResolutionDataRequested(address,uint256,bytes32,bytes32,bytes,address,uint256,uint256,bool)` | `0x6010c5373137e7c7c584b6bf87be56bf4a685890351251ac0a8d74e599455929` |
| Adapter v2 | `QuestionInitialized(bytes32,uint256,address,bytes,address,uint256,uint256)` | `0xeee0897acd6893adcaf2ba5158191b3601098ab6bece35c5d57874340b64c5b7` |
| Adapter v2 | `QuestionResolved(bytes32,int256,uint256[])` | `0x566c3fbdd12dd86bb341787f6d531f79fd7ad4ce7e3ae2d15ac0ca1b601af9df` |

The full set (18 topic0s, including `QuestionUpdated`, `QuestionReset`,
`QuestionPaused`, `QuestionUnpaused`, `QuestionSettled`,
`QuestionFlaggedForAdminResolution`, `QuestionFlagged`,
`QuestionEmergencyResolved`, `AncillaryDataUpdated`) is `TOPIC0S` in
`src/polymarketbot/data/uma.py`. Admin events (`NewAdmin`, `RemovedAdmin`,
`Authorize`/`Deauthorize`) are deliberately not decoded.

`ProposePrice` topic0 and payload shape were checked against a live log at block
96,710,576: identifier `YES_OR_NO_QUERY`, proposed price `1e18`, expiry
`expirationTimestamp`. `QuestionResolved` was checked against the two logs at
blocks 95,810,506 and 95,810,523 (settled price `0`, payouts `[0, 1]`).

### Decoded fields

| Column | Meaning |
|---|---|
| `contract`, `event`, `address` | `optimistic_oracle_v2` / `uma_ctf_adapter_v1` / `uma_ctf_adapter_v2`; ABI event name; emitting address |
| `block_number`, `block_hash`, `tx_hash`, `tx_index`, `log_index`, `recv_ts_ns`, `removed` | log identity copied from `polygon_logs` |
| `question_id` | adapter `questionID`; for OO events `keccak256(ancillaryData)`, which is exactly how the adapter derives `questionID` (see `initialize`/`priceDisputed` in Polymarket/uma-ctf-adapter) |
| `requester`, `proposer`, `disputer` | addresses (adapter v1 `requestor` / v2 `creator` map to `requester`) |
| `identifier`, `identifier_text` | bytes32 hex, and printable ASCII (`YES_OR_NO_QUERY`) |
| `request_timestamp` | OO request timestamp; adapter v2 `requestTimestamp`; v1 `ResolutionDataRequested.requestTimestamp` |
| `resolution_time` | v1 adapter `resolutionTime` |
| `expiry` | OO `ProposePrice.expirationTimestamp` |
| `ancillary_data`, `ancillary_text` | raw bytes as hex; UTF-8 text when the bytes decode cleanly |
| `proposed_price`, `settlement_price` | int256 as decimal **strings** (`1e18` = 1.0); a price that does not fit int64 is not clipped |
| `disputed` | `TRUE` for `DisputePrice`; `FALSE` for `ProposePrice`; from `Settle` it is `disputer != 0`; `NULL` for requests and adapter lifecycle events |
| `payouts` | v2 `uint256[]` as JSON (`[0, 1]`) |
| `currency`, `reward`, `final_fee`, `proposal_bond` | amounts as decimal strings; `currency` is the OO currency / adapter `rewardToken` |
| `early_resolution`, `emergency_resolution` | adapter booleans |
| `source_part` | tape part the row came from; used to skip parts on rerun |

Prices are strings, not floats: `int256` does not fit int64 and a clipped price
is worse than a parse at the query site.

### Running the replay

```bash
.venv/Scripts/python.exe scripts/replay_uma.py
# decode everything again, replacing the output
.venv/Scripts/python.exe scripts/replay_uma.py --reset
```

The script scans `data/tape/polygon_logs/**/*.parquet` oldest first, filters on
the four configured addresses above, and appends decoded rows to
`data/derived/uma_events.parquet` with an atomic `.tmp` + `os.replace`.
`data/derived/uma_events.parts.json` records every part already scanned
including parts with zero events, and `source_part` records the row-producing
ones; both are consulted on rerun, so a second run is a no-op. `--max-seconds`
(default 300) bounds one invocation and commits what was decoded.

Current tape result (2026-11-04T05:50Z): **64 parts, 12,249 logs, all from the
CTF `0x4d97…6045`, 0 UMA/adapter rows.** Later parts add 43 logs from the OO
`0xee3a…87c24`, of which 34 decode to `ProposePrice` (the v1/v2 adapters are
quiet in the current window).

Assumptions and limits:

* Only logs the collector actually subscribed to and wrote can be replayed.
  Nothing in `polygon_logs` can recover an event that was never streamed.
* `disputed` is a point-in-time fact: a `ProposePrice` row stays `FALSE` even
  if a later `DisputePrice` follows; join on `question_id`/`request_timestamp`
  for the final state, or read the `Settle` row.
* A question can be re-requested after a dispute; `question_id` is stable but
  `request_timestamp` changes, so `(question_id, request_timestamp)` identifies
  a proposal round.
* Adapter v1's `QuestionReset` can also be an admin reset, not only a dispute;
  no `disputed` value is inferred for it.
* OO requests come from adapters other than the configured
  `0x6A9D…F74`: the live `ProposePrice` probe showed requester
  `0x2f5e3684cb1f318ec51b00edba38d79ac2c0aa9d`. Those adapters' own events are
  not decoded, but their OO events at `0xee3a…87c24` are, keyed by the same
  `keccak256(ancillaryData)` question id. Filter on `requester` if only one
  adapter matters.

## Validation

```bash
# per-table row counts, time range, gaps
.venv/Scripts/python.exe scripts/validate_tape.py --summary --root data/tape

# replay one asset/hour and optionally compare to the live REST book
.venv/Scripts/python.exe scripts/validate_tape.py \
  --root data/tape --table clob_events --asset <token_id> --hour 20261104T12 \
  --rest-check
```

The replay applies `book` snapshots and `price_change` deltas in `recv_ts_ns`
order. A `gap` row means the socket reconnected; the next `book` frame is the
resync anchor. A gap is recorded, never silently filled.

## Cost stack: spread, markout, fees, gas

`scripts/report_tape_costs.py` answers the Gate 1 question from the raw tape,
not from the paper engine:

```bash
# default: the last two UTC hours in the tape
.venv/Scripts/python.exe scripts/report_tape_costs.py --top 20

# a named window and a stated POL price for gas in USD
.venv/Scripts/python.exe scripts/report_tape_costs.py \
  --date 2026-11-04 --hour-from 13 --hour-to 14 --pol-usd 0.40
```

It reports, per condition: median spread from the reconstructed top of book;
signed markout at 30 s / 5 min / 30 min after each tape trade (positive = taker
profit = maker adverse selection); the taker fee implied by the market's Gamma
`feeType`; the Polygon base fee and the cost of one CTF `mergePositions`
(78,000 gas); and `half_spread > markout(5m) + fee_per_share` (Gate 1).

Caveats, all printed by the report:

* **`price_change` identity is nested.** A frame can carry changes for both
  outcome tokens, so `clob_events.asset_id` is NULL on those rows by design;
  `price_changes[*].asset_id` is authoritative. The report unnests it. A naive
  `GROUP BY asset_id` over `clob_events` silently drops 93 % of the rows.
* **Markout is matched, not interpolated.** A quote is accepted only within
  60 s of t+h; a market that goes quiet reports fewer matches, never a borrowed
  price. Coverage is printed.
* **Fees come from the Gamma snapshot, not the tape.** `clob_trades.fee_rate_bps`
  is 0 because the venue does not send a fee rate; the per-market `feeType` in
  `snapshot_2026-11-01.parquet` decides the schedule.
* **Gas is in POL unless `--pol-usd` is passed.** The tape records gwei, not the
  POL/USD rate.

## Synthetic L3 queue telemetry

The tape is L2 + trades, but the queue at a price level is still observable:
the first `book` for an asset carries the resting size at every level, and every
later `price_change` carries the *new aggregate size* at the level it touches
(`size` is the aggregate, `0` removes the level). `polymarketbot.data.l3`
replays that into per-level queue telemetry for a hypothetical maker order.

```bash
# first run: the hour's most-traded asset, prints counts
.venv/Scripts/python.exe scripts/build_l3_queue.py --date 2026-11-04 --hour 01

# one named asset-hour
.venv/Scripts/python.exe scripts/build_l3_queue.py --date 2026-11-04 --hour 01 \
  --asset 107118433685650702332040991454078902188473746599223732907923581436160160612542

# expand: every asset with a book in the hour, in batches
.venv/Scripts/python.exe scripts/build_l3_queue.py --date 2026-11-04 --hour 01 \
  --all-assets --assets-per-batch 25
```

Output: `data/derived/l3_queue/date=YYYY-MM-DD/hour=HH/asset=<asset_id>.parquet`,
one file per asset-hour, ZSTD. An existing file is skipped, so the build is
resumable and an expansion never rewrites finished work. The collector and the
tape are read-only here.

| column | meaning |
|---|---|
| `recv_ts_ns`, `asset_id`, `side`, `price` | level identity; `side` is `bid`/`ask` |
| `size_at_level` | observed aggregate size at the level after the update |
| `q_ahead` | size ahead of the shadow order (FIFO) |
| `q_behind` | size that arrived behind it since it joined |
| `queue_ratio` | `q_ahead / (q_ahead + q_behind)` |
| `cancel_size`, `fill_size` | split of this update's decrease |
| `replenishment_latency` | ns from a depletion to the first increase at the level |
| `survival` | 1 while the shadow order rests, 0 at the update that filled it |
| `queue_age_ns` | age of the shadow order at this observation |
| `attribution` | `anchor` / `increase` / `replenish` / `cancel` / `fill` / `ambiguous` |
| `attribution_ambiguous` | the flag for the case the tape cannot separate |
| `discontinuity` | the anchor replaced state the replay could not trust |

### Assumptions

* **The snapshot is the queue.** A level's shadow order joins at the back, so
  `q_ahead` starts at the observed size and `q_behind` at 0.
* **FIFO within the level.** A decrease consumes the front first: it reduces
  `q_ahead`, then `q_behind`. An increase always appends behind.
* **`price_change.side` is the resting side.** `BUY` is a bid level, `SELL` an
  ask level. Verified on the tape: a taker `BUY` print at 0.59 is matched by
  `SELL`-side level decreases at 0.59, and the replay never produces a crossed
  book on a snapshot it agrees with.
* **A level is only as good as the last frame.** Aggregates are taken as sent;
  no order-level identity is invented. This is queue *depth*, not individual
  order priority.
* **`book` re-anchors.** Every snapshot restarts each level's counters
  (`q_ahead` = snapshot size, `queue_age_ns` = 0), because a snapshot is the
  only thing that can repair a hole. A resync that restates the replayed book
  emits no rows (245,085 restated levels suppressed in the measured hour); one
  that disagrees, or any snapshot after a `gap`, sets `discontinuity` on the
  rows it corrects.
* **`gap` unanchors.** After a reconnect the tape may be missing frames, so the
  asset's deltas are dropped until the next `book` (`dropped_changes`, which also counts unusable frames).
  Nothing is guessed across a gap.
* **The previous hour is read for an anchor.** An asset whose first in-hour
  event is a delta is seeded with its last `book` before the hour
  (`--lookback-hours`, default 3, and gaps in those hours are registered).

### Attribution rule

A decrease at a level is explained by prints in the same asset at the same
price whose taker side consumes that book side (`BUY` lifts asks, `SELL` hits
bids), pooled within `--match-window-ms` (default 500) of the decrease. The
window is two-sided because the venue can push `last_trade_price` after the
`price_change` it caused (a print that arrives late is still claimed).

| print volume vs decrease | `attribution` | `attribution_ambiguous` |
|---|---|---|
| none | `cancel` | `false` |
| exactly the decrease | `fill` | `false` |
| only part of it | `ambiguous` | `true` |
| more than it | `ambiguous` | `true` |

The last two rows are the case that cannot be resolved: a cancel and a fill in
the same interval look identical in aggregate. Both sizes are reported as best
estimates and the row is flagged rather than guessed. A dropped
`price_change` for a real fill turns into a false `cancel`; that is why the
per-hour counters report `anchor_mismatches` — the snapshot is the check on the
replay, and the flag is how the tape's own holes show up in the data.

### Measured

Hour `20261104T01` and `T05`, run 2026-11-04T11:29Z (`clob_events` +
`clob_trades`):

| slice | rows | fills | cancels | ambiguous | time |
|---|---:|---:|---:|---:|---:|
| most-traded asset, 1,309 prints (01) | 19,155 | 1,123 | 8,622 | 175 | 3 s |
| busiest asset, 195,457 changes (01) | 134,381 | 2 | 67,158 | 3 | 5 s |
| quiet asset, 63 snapshots + 852 changes (05) | 443 | 59 | 193 | 0 | 1 s |
| 25 assets, one batch (01) | 1,396,326 | — | — | — | 25 s |

`ambiguous` is 1.8% of the most-traded asset's 9,920 level decreases. Anchor
re-checks: 149 of 245,234 level comparisons disagreed with the venue snapshot
(0.06%) for the most-traded asset, 167 of 7,452 (2.2%) for the busiest, and 1
of 7,793 (0.013%) for the quiet one — mismatches track load, which is the
signature of frames dropped under a burst rather than a replay bug. A hand
check found the tape skipping frames outright (replayed bid 0.50 = 335 against
a snapshot 0.31 s later saying 200, with no frames between); that is what the
`discontinuity` flag exists for. The matcher checks out the same way: the quiet
asset's largest fill is 304,174.19 at bid 0.999, and the `SELL` print of
exactly that size at 0.999 arrived 59 ms *after* the `price_change` — the
forward half of the match window is what turns that into a fill instead of a
cancel.

A tape hour costs one DuckDB scan per batch, so a full hour across 3,074 assets
is a multi-invocation job: keep each invocation under the 600 s cap, let
`--max-seconds` (default 280) stop it, and rerun — finished asset-hours are
skipped.


## Clock offset and cross-venue timing

A claim like "the Polymarket book reacted 150 ms after the Binance BBO moved"
compares two *server* timestamps produced by two machines whose clocks are not
the same. `recv_ts_ns` cannot settle it: it is stamped by the collector after
the frame has been parsed, so it carries the collector's own event-loop
queueing, and it hides the inter-venue skew in the network delay instead of
removing it. The tape therefore carries explicit offset measurements.

### Method

`polymarketbot/netclock.py` sends one request per venue and records three
timestamps: `t1` locally before the request, `ts` from the venue's own clock in
the response body, `t2` locally after the body has been read. With the standard
NTP assumption that the two paths are symmetric:

$$
\text{offset} = ts - \frac{t_1 + t_2}{2}
\qquad
\text{rtt} = t_2 - t_1
$$

`offset` is venue clock minus local clock; `rtt / 2` is the largest one-way
error, so a single probe is an interval, not a number. Endpoints:

| venue | endpoint | resolution | window |
|---|---|---|---|
| `binance` | `GET https://fapi.binance.com/fapi/v1/time` (`serverTime`, ms) | 1 ms | `rtt/2` |
| `clob` | `GET https://clob.polymarket.com/time` (bare Unix seconds) | 1 s, truncated | `rtt/2` plus up to 1 s above the estimate |

The CLOB endpoint truncates to whole seconds, so one probe is worth a second.
Several probes taken at different sub-second phases intersect their intervals
(`offset_bracket`) and are much tighter than any single probe. Only the
low-RTT cluster participates: a queued request can have a much larger RTT than
the path's real delay, and intersecting *all* samples collapses to their union
as soon as one sample is delayed. When the low-RTT cluster disagrees, the
tightest single sample interval is reported; the union of all samples is never
used.

`clock_offset` rows (`stream`, `host`, `offset_ns`, `rtt_ns`, `recv_ts_ns`)
are written one Parquet part per probe run:

```bash
# 5 rounds per venue, ~7 s total; a probe is a short job, not a collector stream
.venv/Scripts/python.exe scripts/measure_lead_time.py --probe-only --count 5 --interval 1
```

The local resolver intermittently fails `clob.polymarket.com`, so the script
installs `polymarketbot.net.install_doh_fallback()` before probing — the same
requirement as every other Polymarket client here. Without it the CLOB leg
records nothing and only Binance offsets reach the tape.

Measured on 2026-11-04 between 05:53:47Z and 05:59:54Z, five probe runs of 5
rounds each (the first run recorded Binance only, before the DoH fallback was
wired into the script, which is why Binance has 25 samples and the CLOB 20):

| venue | samples | offset range | rtt | bracket over all samples |
|---|---:|---:|---:|---|
| `binance` | 25 | +800.8 … +1077.0 ms | 104–645 ms | **+767.7 … +864.6 ms** |
| `clob` | 20 | −115.5 … +954.2 ms | 164–501 ms | **+703.8 … +966.6 ms** |

The outliers (1077 ms at a 645 ms round trip, 954 ms at 501 ms) are inert: their
intervals contain the bracket, so they neither move nor widen it — which is the
point of intersecting instead of averaging.

Two facts follow. The host clock is **~0.82 s behind the Binance clock**, so an
uncorrected venue-to-venue comparison is wrong by hundreds of milliseconds. And
the CLOB bracket is still 263 ms wide, because the endpoint answers in whole
seconds. The bracket assumes the host clock did not move during the 6 minutes of
sampling; `load_samples` never mixes more than the newest 8 probe parts, and the
report prints the age of the newest sample so a stale offset is visible.

### Measuring the lead

```bash
# newest hour with both tables, 15 min of it, auto-linked from the manifest
.venv/Scripts/python.exe scripts/measure_lead_time.py --minutes 15

# explicit links when the reward-filtered universe holds no crypto markets
.venv/Scripts/python.exe scripts/measure_lead_time.py --hour 20261104T01 --minutes 10 \
  --link BTCUSDT=<token_id>,<token_id> --link ETHUSDT=<token_id>
```

The script takes every Binance `bookTicker` row whose bid or ask changed, and
for each one the first `best_bid_ask` frame on the linked Polymarket asset(s)
whose top of book changed within `--max-gap-ms` (default 2 s). It reports the
gap distribution in `venue_ts_ms` terms — raw and offset-corrected — plus the
share of gaps beyond 100 ms, over the whole correction bracket. It also reports
a **placebo**: the same statistic measured from an arbitrary instant 30 s after
each Binance move, which is the noise floor of "unrelated book moves". If the
real distribution looks like the placebo, the assets are not linked to the
underlying and the number means nothing.

A 10-minute shakedown on `20261104T01` with two arbitrary (non-crypto) assets
temporarily linked to `BTCUSDT`/`ETHUSDT` produced 1,318 matched gaps out of
2,304 Binance moves (986 had no book move within 2 s): raw p50 **503 ms**,
corrected p50 **484 ms** (correction −19 ms, bracket −198.9 … +160.8 ms),
placebo p50 **457 ms**. The reaction is indistinguishable from the placebo,
which is the expected result for markets with no crypto underlying and confirms
the placebo check catches an unlinked asset. At 05:00Z the reward-filtered
universe held no market whose question names a crypto underlying, so a real
measurement needs `--link`, or a lower `cutoff_daily_reward` than the current
15 USD/day.

On 2026-11-05 the script was extended: the quote series is built from the nested
`price_changes` (the `best_bid_ask` event stops being written after hour 13), a
taker leg measures the first `clob_trades` print on the linked tokens, and the
output is a verdict (`PASS`/`FAIL`/`INCONCLUSIVE`/`NO_DATA`) rather than a point
estimate. The 2026-11-05 run with the crypto sentinel universe (BTC 224, ETH 144, SOL 44
assets) measured quote p50 **471–553 ms**, taker p50 **1,252–2,188 ms**, point
lead **888 ms**, placebo p50 **622 ms**, clock bracket **1268 ms** →
**INCONCLUSIVE**. The sentinel universe fixed the token scarcity (the earlier
run had 1–6 linked tokens and one trade); the remaining blocker is the clock
bracket, not the data.

### What this does not prove

Gate 3 (`docs/two-week-data-plan.md`) asks whether a Binance-triggered
cancellation reaches the matching engine **100 ms faster than the median taker
snipe**. Status on 2026-11-05: **PASS**, with the clock fixed on our side.

* The lead is a difference of two CLOB venue timestamps against the same
  Binance tick; the cross-venue clock correction cancels in it. The study now
  gates the lead on its own bootstrap sampling interval, not on the clock
  bracket.
* A fresh **20-sample** `/time` probe (and only the newest part) brackets the
  correction to **339 ms**; the earlier 1,268 ms came from four samples plus
  stale parts being intersected together. 40 samples gave the same ~0.32 s
  floor; the CLOB's whole-second answer and the ~160 ms RTT are the limit.
* Our order path is measured without placing an order:
  `scripts/bench_cancel_path.py` gives local cancel-auth p99 **0.012 ms** plus
  CLOB min RTT **217.5 ms** → a **217.5 ms upper bound** (one full RTT is
  conservative; the forward leg is less).
* Measured window 06:00–06:10 UTC: quote p50 **52.7 ms**, taker p50
  **1,067.7 ms**, lead **1,015 ms** (bootstrap **749–1,532**), margin
  `taker − our_path = 850 ms` (point; the verdict subtracts half the 339 ms bracket and the taker sampling interval) → Gate 3 PASS.

Remaining caveats: the Binance BBO is throttled to one row per 100 ms; the taker
sample is 25 fills; the our-path bound uses the RTT, not a live cancel ack;
unmatched moves are reported (`pooled.unmatched`). The placebo (unrelated
Binance moves) reacts at 140.7 ms, slower than the real 52.7 ms, so the quote
leg is not pure noise, but it is a weak leg; the lead and the taker leg carry
the verdict.

## Failure semantics

| Failure | Behaviour | Worst-case loss |
|---|---|---|
| Stream exception | supervisor restarts it with backoff | in-flight events |
| WS disconnect | reconnect, resubscribe with `initial_dump=true`, emit `gap` | the disconnect interval |
| Kalshi edge resets (10054) | edge excluded 300 s, next candidate dialled, full-jitter retry; poll interval doubles per consecutive failure (cap 60 s) | one poll |
| Kalshi HTTP 429/5xx | transport retry honours `Retry-After`, then jittered backoff; edge not blamed | one poll |
| Process kill | WAL on disk; next start compacts it | 60 s of WAL not yet fsynced |
| Gamma partial crawl | refresh refused if the universe shrinks >50%; old set kept | none |
| Gamma incremental poll | new/closed poll every 60 s; a failed poll retries in 15 s; the hourly keyset walk reconciles | one poll |
| Market resolved | CLOB `market_resolved` drops the market's assets immediately; closed poll and hourly walk also remove it | none |
| Restart | cached manifest + `universe_state.json`; next keyset walk at `last_full_walk_at + 1 h`; streams subscribe from cache first | none |
| Disk full | writer queue spills to `*.overflow.jsonl`; health metric rises | none until disk is full |
| Clock step | `recv_mono_ns` preserves intervals; `recv_ts_ns` may jump | none for latency math |

## Live run

Started 2026-11-04T01:00:43Z as a persistent service. Bring-up restarts applied
four fixes (Kalshi `raw_json`/heartbeat, recovered-part naming, staggered CLOB
shard startup, and per-module log sinks); every previous WAL was recovered and
compacted, so no rows were lost.

* run id `20261104T010043`
* pid `30336`
* log `data/logs/data.collector.log` (per-module: `data.clob.log`,
  `data.news.log`, `data.kalshi.log`, …)
* metadata `data/tape/runs/20261104T010043.json`
* stop: `taskkill /PID 30336` (or `Stop-Process -Id 30336`)
* monitor: `python scripts/validate_tape.py --summary --root data/tape`

CLOB shards now connect 2 s apart. Eight simultaneous handshakes after a
restart were reset by Polymarket's connection rate limit; a 120 s staggered
test held all eight sockets with zero disconnects.

`net.resolve_ipv4` now probes candidate CloudFront edge IPs with a TCP
handshake before caching one. A single edge (`143.204.55.16`) was refused by
the local firewall with `WinError 10013` while sibling edges worked, which made
the whole Gamma comments poll fail until the 300 s cache expired. The running
process predates this change, so its first cache entries may still fail; the
next re-resolve (≤300 s) picks a reachable edge.

Recovered WALs are written as
`part-recovered-<source_run_id>-<hour>-<seq>.parquet`, so a restart can never
overwrite the previous run's part with the new run's first part.

The service runs until stopped. Restarting with the same command resumes from
the manifest and compacts any orphaned WAL. The universe refreshes hourly; the
2026-11-04 election markets resolve at 04:59Z and will be dropped on the next
refresh.

## Measured preflight

2026-11-04 00:25 UTC, 60 s, all eight streams, `data/tape_preflight`:

| table | rows | note |
|---|---:|---|
| `clob_events` | 214,089 | 5.97 MB Parquet → **27.9 B/row** |
| `clob_trades` | 203 | |
| `binance_bbo` | 2,413 | 100 ms throttle; was 174,943 unthrottled |
| `binance_depth` | 588 | |
| `kalshi_markets` | 6,123 | initial snapshot + changes |
| `whale_activity` | 4,722 | 50 wallets |
| `news_items` | 292 | RSS/Gamma; GDELT removed (was 429 from this IP) |
| `gamma_comments` | 1,537 | resolved per Event |
| `polygon_logs` / `polygon_gas` | 49 / 49 | Alchemy WSS |
| `deribit_surface` | 12 | 2 indexes × 6 polls |

Writer: **230,199 rows emitted / written, 0 spills, 0 compaction errors**.
Replay of one asset: best bid 0.11 / best ask 0.12, no crossed book, 1,262
complement-token changes filtered by the nested `asset_id`.

At the preflight rate the CLOB tape is ~9.6 GB/day; the other streams add
roughly 1 GB/day. That is ~150 GB for 14 days, before the post-election volume
drop and before the election-night peak — measure the first 24 h and let the
health stream's `disk_free_gb` decide. The collector stops itself below 15 GB
free.

Note: log lines use the host's local time (IST here); every `recv_ts_ns` in
Parquet is UTC epoch nanoseconds.

Live rate at 00:41Z (2 min into the run): 466,629 `clob_events` rows in
~120 s, **3,900 rows/s**; 92% `price_change`, 6.4% `best_bid_ask`, 1.1%
`book`. The WAL is 476 B/row JSONL; the Parquet part compresses to ~25.7 B/row,
so the CLOB tape is **~8.6 GB/day (~120 GB/14d)** at this rate. The 5-minute
WAL part is ~0.6 GB and compacts in ~5 s.

Post-restart measurement at 01:05Z (run `20261104T010043`): 1,242,100
`clob_events` rows in 300 s, **4,133 rows/s**, 32.8 MB Parquet →
**9.4 GB/day** for CLOB. The tape has 4.7 M CLOB rows since 00:37Z; the only
6 `gap` rows are from the pre-stagger disconnect storm at 00:42Z (06:12 IST).
Zero disconnects after the 01:00Z restart. `gamma_comments` emits ~1,569 rows
per 2.5 min through the Event fallback.

Kalshi initially wrote 48 MB in 3 min because every row embedded the full
market object. `raw_json` is now null and the full-snapshot heartbeat is 5 min,
so the same interval writes ~7 MB. All parsed fields remain.

### Kalshi poll diagnosis (2026-11-04, 06:00–06:30Z)

Two defects were measured against the live service, not inferred:

* **Only parlay markets were on the tape.** Every row in the open WAL part
  (`data/tape/_wal/kalshi_markets/20261104T010043/20261104T05-10.jsonl`, 4,654
  rows) had an `event_ticker` starting with `KXMVECROSSCATEGORY`. Cause: the
  first 3,000 rows of `?status=open&limit=1000` are 100 % MVE parlays (checked
  on pages 1–30), so the 3,000-market cap never reached a single-event market.
* **`ConnectionResetError 10054` at the old 15 s poll was a bad DNS edge, not rate
  or size.** The host has four A records; a TLS probe found some of them
  accepting the SYN and resetting the handshake, and the Windows resolver
  returns the same order on every lookup, so a session pinned to a bad edge
  failed every poll. The log holds 178 such warnings between 06:33 and 11:10
  local; a fresh `requests` session against a good edge succeeded 5/6 times,
  which is the signature of a rotating pin rather than throttling.

Both are fixed in `streams/kalshi.py`: mapped-ticker polling (233 tickers, 3
requests, 1.54 s, 0 resets measured) and TLS-probed edge pinning with rotation
plus full-jitter backoff. The running process predates the change, so this only
takes effect after the next planned restart.

## Disk budget

Measured per-hour sizes are written by the health stream and the run metadata.
The post-restart measurement is authoritative: CLOB **9.4 GB/day** (1.24 M
rows per 300 s, 25.7 B/row Parquet) plus ~1 GB/day for the other streams →
**~146 GB for 14 days**. The earlier per-market-hour arithmetic (~48–51 GB)
underestimated the tape by 3×; do not use it. Free space is 237 GB, so the
200 GB budget leaves ~54 GB of headroom and ~91 GB free after the run; keep at
least 20 GB free for DuckDB temp. Let the first 24 h measurement confirm the
rate before any cap change.

The new `kalshi_trades` table is small by comparison: the global feed runs at
~104 prints/s but only mapped tickers are written (273 of 3,000 prints in a
26 s smoke run, ~10 prints/s → ~0.9 M rows/day, ~35 MB/day Parquet). The mapped
`kalshi_markets` poll is ~0.5 MB per poll and change-detected, so it stays far
below the old 3,000-market sweep.

## Derived data-use scripts

One reader per question, each writing `data/derived/` and stating its window,
sample size, and n/a rules. They read the tape; none sits in the collector path.

| Script | Answers | Main caveat it prints |
|---|---|---|
| `scripts/score_markets.py` | Per-condition spread, markout(300 s), fee/share, `edge = half_spread − markout − fee`, Gate 1, reward/capital | fee coverage: 832/834 traded conditions (2026-11-05 window) via `universe_metadata`; the 2026-11-01 snapshot alone covers 138/647; passes with n<30 are marked, not measurements |
| `scripts/calibrate_fill_model.py` | P(fill) by pre-event queue ratio × size, isotonic + logistic | `queue_ratio=1` is a separate regime (0/3,376 fills); strict-penetration sensitivity printed |
| `scripts/scan_toxicity.py` | Signed markout(30 s/5 m), trade imbalance, OFI per condition × 5 min | OFI flags a no-trade bucket; wide matched quotes dropped |
| `scripts/backtest_quotes.py` | Quote episodes on one L3 asset-hour: observed fill rate, spread capture, markout, gas | shadow-order fill count is an optimistic upper bound; one asset-hour is not a strategy result |
| `scripts/report_kalshi_basis.py` | Polymarket vs Kalshi mid basis, spreads, lead-lag | only 76 mapped pairs are quoted on both sides; the panel needs ≥30 pairs |
| `scripts/bench_cancel_path.py` | Our Binance-trigger → CLOB-cancel path upper bound | no order is sent; one full RTT is conservative |

Measured on the 2026-11-05 tape (1–6 h windows):

* scorecard: window 05:00–07:00 UTC, **2,511 conditions, markout matched
  88.6 %; 76 of 129 scored conditions pass Gate 1 and all 76 are low-sample
  (n<30)**. Gate 1 from a Gamma snapshot is not evidence; the L3 markout is.
  A `fee_known` column flags the 509 traded conditions without a `feeType`.
* fill model: 6,817 observations / 2,184 fills (base 32.0 %); P(fill) falls
  0.63 → 0.09 as the queue ratio rises 0 → 1; logistic ratio slope −3.97 ± 0.11.
  The backtester's 0.98 % fill rate is a different denominator (every resting
  quote episode), not a contradiction.
* toxicity: 161 of 3,779 condition-buckets flagged (4.3 %), markout matched
  96.8 % at 30 s.
* Kalshi basis: median per-market −1.01¢, median |basis| 2.45¢; pooled lead-lag
  peaks at lag 0 (r=0.21), so the cross-venue edge needs a faster signal than a
  60 s grid.
* quote backtest: one hour, 6,566 quoted sides, 64 fills (0.98 %); net
  +0.0336/fill in that hour but markout −0.0505 in another hour — the reason
  Gate 1 is checked over the whole 14-day sample.

The Gamma crawl was 240 k open markets (mostly 5-minute crypto binaries), ~20
min; the rolling walk now reads the **CLOB reward-campaign endpoint** (ordered
by `rate_per_day`, stops at the $15 cutoff: 3,000 rows / 6 pages / ~5 s) and
keeps the Gamma crawl only as the fallback. The sentinel stale filter caps the
crypto set at 200.
