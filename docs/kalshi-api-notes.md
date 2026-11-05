# Kalshi API notes — market-data surface

Working spec for the Kalshi Trade API v2 surface this repo uses, written so a
client can be implemented from this file alone.

**Sources.** `C:/Users/Sivasai/Desktop/kalshi-docs.txt` (12,571 lines, Kalshi
docs snapshot; newest changelog entry in it is **November 5, 2026**). Claims are
tagged:

* `[docs]` — stated in that file; the section/heading name is given in
  parentheses, e.g. `(docs: Rate Limits and Tiers § Token-based limits)`.
* `[measured 2026-11-06]` — observed by this repo against the live API today.
* `[inference]` — derived, not directly stated or observed.

Nothing here is copied from a secret. No key ID, PEM or account identifier
appears in this file.

---

## 1. Environments

REST (`docs: API Environments and Endpoints § REST API`):

| Environment | Recommended base URL | Also supported |
|---|---|---|
| Production | `https://external-api.kalshi.com/trade-api/v2` | `https://api.elections.kalshi.com/trade-api/v2` |
| Demo | `https://external-api.demo.kalshi.co/trade-api/v2` | `https://demo-api.kalshi.co/trade-api/v2` |

The `elections` subdomain is a legacy name; production serves *all* Kalshi
markets, not only election markets (`docs: API Environments and Endpoints`;
`docs: Quick Start: Market Data § Making Unauthenticated Requests`).

WebSocket (`docs: API Environments and Endpoints § WebSocket API`;
`docs: WebSocket API`):

| Environment | Recommended URL | Also supported |
|---|---|---|
| Production | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` | `wss://api.elections.kalshi.com/trade-api/ws/v2` |
| Demo | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` | `wss://demo-api.kalshi.co/trade-api/ws/v2` |

The WS path segment is `/trade-api/ws/v2` — note it is *not* `/trade-api/v2/ws`.
The signed path equals the URL path here (§2.3).

Dedicated lower-latency hosts exist for **only** `fill` and `user_orders`
(`docs: API Environments and Endpoints § Fill and order updates`):
`wss://warp-ws.kalshi.com/trade-api/ws/v2` (prod), `wss://warp-ws.demo.kalshi.co/trade-api/ws/v2`
(demo). Subscribing to any other channel there returns error code 8.

Credentials are not shared between environments; a demo key works only against
demo hosts and a production key only against production hosts
(`docs: API Environments and Endpoints`). Demo prices and behaviour "may not be
reflective of those in real markets" (`docs: Test In The Demo Environment`).

Measured today: production REST is fully usable **without any key**; production
WS rejects a demo key at the handshake with HTTP 401; demo WS accepts it.

---

## 2. Authentication

### 2.1 Key types

`docs: API Keys § Key types`

| Key type | Signature | Notes |
|---|---|---|
| **Ed25519** (recommended) | Ed25519 (RFC 8032) over the pre-sign text | PKCS#8 PEM; 64-byte signature; lower signing cost |
| **RSA** (2048-bit) | RSA-PSS / SHA-256, MGF1-SHA256, salt length = digest length | PKCS#1 PEM; needed only by clients limited to RSA-PSS |

The PEM header does **not** identify the key type (PKCS#8 RSA also starts
`-----BEGIN PRIVATE KEY-----`); decide from the parsed key object. Kalshi picks
the verification algorithm from the registered public key, so sign with the
algorithm that matches the key. Headers, pre-sign text and errors are identical
for both types. The three `KALSHI-ACCESS-*` headers are required together.

### 2.2 Pre-sign text and headers

`docs: API Keys § Using a API Key`; `docs: API Environments and Endpoints § Request Signing`

```
pre_sign = str(unix_ms) + HTTP_METHOD + path_without_query
```

* `unix_ms` is milliseconds (seconds is a documented cause of signature
  failures — `docs: Quick Start: Authenticated Requests` troubleshooting table).
* `HTTP_METHOD` is uppercase, e.g. `GET`.
* `path_without_query` is the full path from the API root **including**
  `/trade-api/v2`, with the query string stripped. `/trade-api/v2/portfolio/orders?limit=5`
  signs `/trade-api/v2/portfolio/orders`. The host never enters the signature,
  so all hosts of one environment share the signed path.

Headers (`docs: API Keys § Using a API Key`):

| Header | Value |
|---|---|
| `KALSHI-ACCESS-KEY` | API key ID (UUID) |
| `KALSHI-ACCESS-TIMESTAMP` | request timestamp in ms |
| `KALSHI-ACCESS-SIGNATURE` | base64 of the signature bytes |

### 2.3 WebSocket handshake

`docs: Quick Start: WebSockets § Authentication`, `§ Signing the WebSocket Request`

The handshake uses the same three headers and the same pre-sign construction,
with the WS path:

```
pre_sign = str(unix_ms) + "GET" + "/trade-api/ws/v2"
```

The connection is authenticated once, at handshake; there is no in-band auth
message. Channels split into (`docs: Quick Start: WebSockets § Authentication`):

* **private** (need the authenticated session *and* channel access):
  `orderbook_delta`, `fill`, `market_positions`, `communications`,
  `order_group_updates`;
* **public market data** (authenticated session, no extra per-channel check):
  `ticker`, `trade`, `market_lifecycle_v2`, `multivariate_market_lifecycle`,
  `multivariate`.

Measured today: there is **no server hello frame**. Connect, send `subscribe`
immediately; the first frame back is `{"type":"subscribed", ...}` followed by
`orderbook_snapshot`. Demo WS on `wss://demo-api.kalshi.co/trade-api/ws/v2`
works with the demo key; the same key against production WS fails the handshake
with HTTP 401.

---

## 3. Value conventions (fixed point)

### 3.1 Two different suffixes

`docs: Fixed-Point Representation § Overview`

| Suffix | Means | Precision |
|---|---|---|
| `*_dollars` | **price / money** in fixed-point dollars | up to 4 dp (`"0.1200"`) |
| `*_fp` | **contract quantity** in fixed-point contracts | up to 2 dp (`"10.00"`) |

Do not read `_fp` as money: the suffix alone does not distinguish a contract
count from a dollar amount. (The exception is the *Clearing Member / Margin*
API, which has `_fp` **dollar** strings with 6 dp — `docs: Fixed-Point
Representation § Margin Markets`. Not used by this repo's spot market-data
surface.)

Integer-cent price fields are gone: `yes_bid`, `no_ask`, `last_price`,
`liquidity`, `previous_price`, `tick_size` etc. were removed from Market
responses on 2026-01-15 in favour of `*_dollars` and
`price_level_structure`/`price_ranges` (`docs: API Changelog`, Jan 6, 2026 and
Jan 15, 2026 entries), and integer count fields with `_fp` equivalents were
removed on 2026-03-12 (`docs: API Changelog`, Feb 21, 2026 entry). A client that
reads only `*_dollars` / `*_fp` is correct today.

Contract granularity is 0.01 contracts; represent quantities in hundredths for
integer math (`"1.55"` → 155) (`docs: Fixed-Point Representation § Fractional
Contracts`). Fills can be fractional even when whole-contract orders were
submitted.

### 3.2 Price grid

`docs: Fixed-Point Representation § Price Level Structures`

Every market response carries:

* `price_ranges` — array of `{start, end, step}` bands in fixed-point dollars.
  **This is the source of truth** for valid prices; snap order/quote prices to
  the `step` of the band containing the price.
* `price_level_structure` — a human-readable label only. Never key logic off it.

Measured sample (`KXBTC15M-26NOV051400-00`, `[measured 2026-11-06]`):

```json
"price_level_structure": "tapered_deci_cent",
"price_ranges": [
  {"end": "0.1000", "start": "0.0000", "step": "0.0010"},
  {"end": "0.9000", "start": "0.1000", "step": "0.0100"},
  {"end": "1.0000", "start": "0.9000", "step": "0.0010"}
]
```

Structures documented: `linear_cent`, `deci_cent`, `tapered_deci_cent`,
`center_whole_edge_half_cent`, `center_whole_edge_quint_cent`,
`center_half_edge_half_cent`, `center_half_edge_quint_cent`,
`center_half_edge_deci_cent`, `center_quint_edge_quint_cent`,
`center_quint_edge_deci_cent`, `center_centi_edge_centi_cent`,
`center_deci_edge_centi_cent`. Newer names follow
`center_{center}_edge_{edge}_cent` with `whole`=\$0.01, `half`=\$0.005,
`quint`=\$0.002, `deci`=\$0.001, `centi`=\$0.0001. Whole-cent prices are valid in
every structure. On a structure change the `price_level_structure_updated` event
on `market_lifecycle_v2` carries the new `price_ranges`
(`docs: Fixed-Point Representation § Price Level Structures`;
`docs: API Changelog`, July 2, 2026 entry).

### 3.3 One book, two sides, bids only

`docs: Orderbook Responses § Why Only Bids?`, `§ Understanding the Arrays`

Kalshi returns **bids only, on both sides**; there is no ask array anywhere.

* A YES bid at X ≡ a NO ask at \$1.00 − X
* A NO bid at Y ≡ a YES ask at \$1.00 − Y

Derived quotes (same page, `§ Calculating Spreads`):

```
best_yes_bid = max(yes bid prices)
best_yes_ask = 1.00 - max(no bid prices)
best_no_bid  = max(no bid prices)
best_no_ask  = 1.00 - max(yes bid prices)
```

Level arrays are `[price_dollars, count_fp]` pairs **sorted ascending by price**,
so the best bid is the **last** element of the array (`docs: Orderbook
Responses § Understanding the Arrays`). Empty sides: REST includes the key with
an empty array; WS omits the key entirely (`[measured 2026-11-06]`).

### 3.4 REST vs WS key names for the same book

`[measured 2026-11-06]`

| Surface | Shape |
|---|---|
| `GET /markets/{ticker}/orderbook` | `{"orderbook_fp": {"yes_dollars": [["0.4200","13.00"], ...], "no_dollars": [...]}}` |
| `GET /markets/orderbooks` | `{"orderbooks": [{"ticker": "...", "orderbook_fp": {...}}, ...]}` |
| WS `orderbook_snapshot` | `msg.yes_dollars_fp` (⚠ different key: `_fp` suffix, no wrapper) |
| WS `orderbook_delta` | `msg.price_dollars`, `msg.delta_fp`, `msg.side` |

The WS snapshot array name `yes_dollars_fp` is observed; the no-side counterpart
is `no_dollars_fp` `[inference — demo books were one-sided today, so a non-empty
no-side snapshot array was never observed]`. Code must tolerate either key
being absent.

### 3.5 WS price leg convention (`use_yes_price`)

`docs: Order direction (outcome_side and book_side) § Orderbook pricing convention`

By default `orderbook_delta` and `orderbook_snapshot` report **no-side levels in
no-leg pricing**: a no delta at `price_dollars = 0.30` means "no at 30¢", which
matches "yes at 70¢". Toggling sides flips the price scale.

Passing `use_yes_price: true` in the subscribe `params` reports both sides on the
yes scale (the same level arrives as `0.70`). The flag defaults to `false`;
Kalshi has announced it will flip the default to `true` and later remove the
flag. **New integrations should send `use_yes_price: true`** and store the leg
explicitly.

---

## 4. Timestamps

REST (`docs`-derived field types; formats `[measured 2026-11-06]`):

* All timestamps are **UTC**; RFC3339 strings carry a `Z` suffix, and every
  `*_ts` integer is Unix epoch (seconds or milliseconds as named).
* Market/trade timestamps are **RFC3339 UTC strings**, sometimes with
  microseconds: `"2026-11-05T18:45:00Z"`, `"2026-11-04T21:01:38.681074Z"`,
  trade `created_time: "2026-11-05T18:54:03.084146Z"`.
* Candlestick buckets use integer **`end_period_ts` in Unix seconds**.
* `start_ts` / `end_ts` query parameters are Unix **seconds**.
* Cutoff fields from `GET /historical/cutoff` are timestamps partitioned by the
  documented event time (`docs: Historical Data § Cutoff Timestamps`).

WebSocket:

| Channel | Event time field |
|---|---|
| `orderbook_delta` | `msg.ts` (RFC3339) and `msg.ts_ms` (Unix ms) — both present `[measured]` |
| `orderbook_snapshot` | none in `msg` `[measured]`; order by `clock`/`seq` |
| `ticker` | `msg.ts` (Unix **seconds**), `msg.ts_ms` (ms), `msg.time` (RFC3339) `[measured]` |
| `trade` | `msg.ts` (Unix seconds) plus `msg.ts_ms` (`docs: API Changelog`, Apr 15, 2026) |
| `fill` | `msg.ts` (Unix seconds) plus `msg.ts_ms` (same entry) |
| every frame envelope | `sending_ts_ms` (Unix ms) `[measured]` |

Rule of thumb: **use `ts_ms`** wherever it exists — it was added for exactly
this reason and the seconds/RFC3339 fields are deprecated in the AsyncAPI spec
(`docs: API Changelog`, Apr 15, 2026 entry). `market_lifecycle_v2` message
timestamps are not enumerated in the docs snapshot `[unknown]`.

`clock` vs `seq` (`docs: Orderbook Updates § Matching real-time updates across
connections`):

* `seq` counts messages **on one subscription** only — it is the gap detector.
* `clock` is the exchange clock, identical on every connection, so it can match
  a market's updates across redundant connections. Clocks skip values, are not
  comparable across markets, can repeat (one exchange action → several deltas),
  and may go **down** during exchange maintenance.

---

## 5. REST endpoints

All paths below are relative to the environment base URL, e.g.
`https://external-api.kalshi.com/trade-api/v2`.

### 5.1 Common response conventions

* List endpoints return `{ "<items>": [...], "cursor": "..." }`; an empty cursor
  means no more pages (`docs: Get Trades`; `docs: Understanding Pagination § How
  Pagination Works`).
* Pagination is cursor-based: `limit` (typically 1–100, default 100) and
  `cursor` (`docs: Understanding Pagination § Pagination Parameters`).
  `/markets/trades` is the exception with `limit` 1–1000 (`docs: Get Trades`).
* Measured: `/markets?limit=200` was accepted and returned 200 rows, so the
  1–100 figure is guidance, not a hard server cap `[measured 2026-11-06]`.
* Paginated endpoints: `/markets`, `/events`, `/series`, `/markets/trades`
  (plus portfolio ones) (`docs: Understanding Pagination § Endpoints Supporting
  Pagination`).
* 429 body is `{"error": "too many requests"}` (`docs: Rate Limits and Tiers
  § When you hit the limit`). Other error bodies are not enumerated in the docs
  snapshot `[unknown]`; measured 401 bodies carry `error` +
  `code` (e.g. `authentication_error` / `NOT_FOUND`) `[measured]`.

### 5.2 `GET /markets`

Params (`docs: Get Markets`):

| Param | Notes |
|---|---|
| `status` | `unopened`, `open`, `closed`, `settled` (filter mapping in §5.10); only one at a time; empty = any |
| `series_ticker` | filter; required to see `KXRFQ*` on-demand markets (`docs: API Changelog`, Nov 5, 2026) |
| `event_ticker` | filter; same `KXRFQ*` caveat |
| `tickers` | filter, comma-separated list `[measured]`; also un-hides `KXRFQ*` markets |
| `min_created_ts` / `max_created_ts` | compatible with `unopened`, `open`, empty |
| `min_close_ts` / `max_close_ts` | compatible with `closed`, empty |
| `min_settled_ts` / `max_settled_ts` | compatible with `settled`, empty |
| `min_updated_ts` / `max_updated_ts` | only with `mve_filter=exclude`; may combine with `series_ticker` |
| `mve_filter` | `exclude` drops MVE (combo) markets |
| `limit`, `cursor` | pagination |

Timestamp filters are mutually exclusive with each other and with incompatible
`status` values, per the compatibility table on that page. Markets settled
before the `market_settled_ts` cutoff are only in `GET /historical/markets`.

Response: `{"markets": [Market, ...], "cursor": "..."}` `[measured]`.

**Market object** — measured key set for a live binary market
(`[measured 2026-11-06]`; documented meanings below):

```
can_close_early, close_time, created_time, custom_strike, event_ticker,
exchange_index, expected_expiration_time, expiration_time, expiration_value,
is_provisional, last_price_dollars, latest_expiration_time, market_type,
mve_collection_ticker, mve_selected_legs, no_ask_dollars, no_bid_dollars,
no_sub_title, notional_value_dollars, open_interest_fp, open_time,
previous_price_dollars, previous_yes_ask_dollars, previous_yes_bid_dollars,
price_level_structure, price_ranges, result, rules_primary, rules_secondary,
settlement_bounds_type, settlement_timer_seconds, status, strike_type, ticker,
title, updated_time, volume_24h_fp, volume_fp, yes_ask_dollars,
yes_ask_size_fp, yes_bid_dollars, yes_bid_size_fp, yes_sub_title
```

Field semantics that matter:

| Field | Meaning |
|---|---|
| `ticker` | market ticker; the join key everywhere |
| `event_ticker` | parent event ticker; the series ticker lives on the event/series objects, not on the market |
| `status` | `initialized`, `active`, `inactive`, `closed`, `determined`, `disputed`, `amended`, `finalized` (`docs: Market Lifecycle § Statuses`) |
| `result` | `yes`, `no`, `scalar`, or `""` before determination (`docs: Market Lifecycle § Determination and settlement`) |
| `open_time` | when trading opens |
| `close_time` | when trading stops; may move earlier if `can_close_early` |
| `expected_expiration_time` | when the outcome is expected (can precede `close_time`) |
| `latest_expiration_time` | latest possible expiry; `expiration_time` is the deprecated legacy field (`docs: Market Lifecycle § Time fields`) |
| `yes_bid_dollars`/`yes_ask_dollars`/`no_bid_dollars`/`no_ask_dollars` | BBO prices as dollar strings; `"0.0000"` = no quote on that side |
| `yes_bid_size_fp`/`yes_ask_size_fp` | contracts resting at the best bid/ask (added Feb 24, 2026) |
| `volume_fp`, `volume_24h_fp`, `open_interest_fp` | contract counts |
| `notional_value_dollars` | \$1 payout per contract |
| `price_ranges`, `price_level_structure` | valid-price grid (§3.2) |
| `settlement_bounds_type`, `settlement_floor_dollars` | `default`/`floor`, floor markets only (`docs: Settlement Bounds § Fields`) |
| `settlement_timer_seconds` | dispute window between `determined` and `settled` (`docs: Market Lifecycle`) |
| `strike_type`, `floor_strike`, `cap_strike`, `custom_strike` | structured strike metadata |
| `exchange_index` | exchange shard (`docs: Exchange Sharding`) |
| `occurrence_datetime` | recorded datetime of the underlying occurrence, when available (added Apr 16, 2026; not present on the sampled market) |

Removed/deprecated and not to be read: `fractional_trading_enabled` (deprecated,
always `true`), `category`, `risk_limit_cents`, `response_price_units`,
`tick_size`, all integer-cent price fields (`docs: API Changelog`, Apr 17, 2026 /
Jan 6, 2026 / Jan 5, 2026 entries).

### 5.3 `GET /markets/{ticker}`

Same Market object, wrapped as `{"market": {...}}` `[measured]`. Docs describe it
as "data about a specific market by its ticker" (`docs: Get Market`). May 404
briefly right after a `created` lifecycle event — retry with backoff
(`docs: Market Lifecycle § FAQ`).

### 5.4 `GET /markets/{ticker}/orderbook`

* No authentication required (`docs: Orderbook Responses § Getting Orderbook
  Data`).
* Response: `{"orderbook_fp": {"yes_dollars": [[price, count], ...],
  "no_dollars": [...]}}`; ascending prices, best bid last; both keys always
  present, possibly empty arrays (`docs: Orderbook Responses § Response
  Structure`; `[measured]`).
* No asks — see §3.3.
* A `depth` parameter is not documented in the docs snapshot `[unknown]`.

### 5.5 `GET /markets/orderbooks` (batch)

* Added 2026-03-30; accepts a list of market tickers via the `tickers` query
  parameter, **up to 100** (`docs: API Changelog`, Mar 30, 2026 entry;
  `docs: Get Multiple Market Orderbooks`).
* Encoding matters: repeat the parameter (`?tickers=A&tickers=B`). A single
  comma-joined value is treated as one (invalid) ticker and returns one stub
  entry — `[measured 2026-11-06]`.
* Response: `{"orderbooks": [{"ticker": "...", "orderbook_fp": {...}}, ...]}`
  — one entry per requested ticker, same `orderbook_fp` shape as the single
  endpoint `[measured 2026-11-06]`.

### 5.6 `GET /markets/trades`

`docs: Get Trades`:

* Paginated; `limit` 1–1000, default 100; `cursor`; empty cursor = done.
* Block trades included by default, flagged `is_block_trade`; `is_block_trade`
  query param filters `true`/`false` (also `docs: API Changelog`, May 29, 2026).
* Trades older than the `trades_created_ts` cutoff are only in
  `GET /historical/trades` (`docs: Historical Data § Impacted Live Endpoints`).
* `KXRFQ*` on-demand market trades are omitted unless the request sets `ticker`
  (`docs: API Changelog`, Nov 5, 2026).
* Not documented in the snapshot but **measured working**: `ticker` (single) and
  `min_ts` / `max_ts` (Unix seconds) `[measured 2026-11-06]`. Whether `min_ts`
  is inclusive on this endpoint is not stated `[unknown]` — treat the window as
  half-open and dedupe by `trade_id`.

Response: `{"trades": [...], "cursor": "..."}`; trade object measured keys:

```
count_fp, created_time, is_block_trade, no_price_dollars, taker_book_side,
taker_outcome_side, taker_side, ticker, trade_id, yes_price_dollars
```

Sample `[measured 2026-11-06]`:

```json
{"count_fp": "2.00", "created_time": "2026-11-05T18:54:03.084146Z",
 "is_block_trade": false, "no_price_dollars": "0.0900",
 "taker_book_side": "bid", "taker_outcome_side": "yes", "taker_side": "yes",
 "ticker": "KXPLATINUM15M-26NOV051400-00",
 "trade_id": "07271939-af01-92d7-f4cd-a5b19af44593",
 "yes_price_dollars": "0.9100"}
```

`yes_price_dollars` and `no_price_dollars` are the same trade on the two legs and
sum to 1.00 `[inference from §3.3; consistent with the sampled trade]`.
`taker_side` is
deprecated in favour of `taker_outcome_side` (`yes`/`no`) and `taker_book_side`
(`bid`/`ask`) (`docs: Order direction (outcome_side and book_side) § Migration`).
`created_time` is the fill time and is the event time used by the
`trades_created_ts` cutoff.

### 5.7 `GET /series/{series_ticker}/markets/{ticker}/candlesticks`

`docs: Get Market Candlesticks`; `docs: API Changelog`, Jan 16, 2026.

| Param | Values |
|---|---|
| `period_interval` | `1` (1 min), `60` (1 h), `1440` (1 d) — those three are the documented valid values |
| `start_ts`, `end_ts` | Unix **seconds** (`[measured]`; `start_ts` named in `docs: API Changelog`, Jan 16, 2026) |
| `include_latest_before_start` | `true` prepends a synthetic candle carrying the close price of the most recent candle before `start_ts`, with `previous_price` set, for chart continuity |

Historical cutoff: candles for markets settled before `market_settled_ts` are
only in `GET /historical/markets/{ticker}/candlesticks` (`docs: Get Market
Candlesticks`; `docs: Historical Data`).

Response `[measured 2026-11-06]`:

```json
{"ticker": "...",
 "candlesticks": [
   {"end_period_ts": 1793904360,
    "open_interest_fp": "130756.33",
    "volume_fp": "194221.77",
    "price":   {"open_dollars":"0.5000","high_dollars":"0.5800",
                "low_dollars":"0.4800","close_dollars":"0.5100",
                "mean_dollars":"0.5441","previous_dollars":"0.5100"},
    "yes_bid": {"open_dollars":"0.0000","high_dollars":"0.5700",
                "low_dollars":"0.0000","close_dollars":"0.5000"},
    "yes_ask": {"open_dollars":"0.9990","high_dollars":"0.9990",
                "low_dollars":"0.4900","close_dollars":"0.5100"}}]}
```

`price.*_dollars` are trade prices; `yes_bid`/`yes_ask` are quote OHLC;
`end_period_ts` is the **end** of the bucket in Unix seconds (subtract
`period_interval*60` to get the bucket start). `price.mean_dollars` is present on
every bucket; its exact definition (mean trade price, volume-weighted or not) is
not stated in the docs snapshot `[unknown]`, so treat it as a bookkeeping field
rather than a VWAP. Buckets with no activity are not guaranteed to appear
`[unknown]`; index by `end_period_ts` rather than assuming a dense grid.

Batch variant `GET /markets/candlesticks` accepts up to **100 tickers** and
returns up to **10,000 candlesticks total**, grouped by `market_id`
(`docs: Batch Get Market Candlesticks`).

### 5.8 `GET /events`, `GET /events/{event_ticker}`

`docs: Get Events`, `docs: Market Lifecycle § FAQ`.

* `series_ticker`, `event_ticker`, `status` filters; `status` values
  `unopened`, `open`, `closed`, `settled`. The status filter matches **child
  market** statuses — an event appears if *any* child market matches, so one
  event can match both `open` and `settled`.
* `with_nested_markets=true` embeds child markets. Default page size is **200**
  (raised from 100), and `with_nested_markets=true` honours `limit=200`
  (`docs: API Changelog`, Nov 6, 2025 entry). Nested markets older than
  `market_settled_ts` are dropped (`docs: Historical Data § Impacted Live
  Endpoints`).
* `GET /events/{event_ticker}` accepts `exclude_markets=true` for a
  metadata-only read; page bounded outcome sets with
  `GET /markets?event_ticker=...&limit=...&cursor=...` (`docs: API Changelog`,
  2026 entry on `exclude_markets`).
* Paginated (`cursor`).

Measured response keys: top level `events`, `milestones`, `cursor`; event object
`category, collateral_return_type, event_ticker, exchange_index,
last_updated_ts, mutually_exclusive, series_ticker, settlement_sources,
strike_period, sub_title, title` `[measured 2026-11-06]`.

### 5.9 `GET /series`, `GET /series/{series_ticker}`

`docs: Get Series List`, `docs: Get Series`. Browse series templates by
category; paginated with `cursor`; both endpoints take
`include_volume=true` (default `false`) to add a `volume` contract-count field
(`docs: API Changelog`, Jan 15, 2026 entry).

Measured series object keys: `additional_prohibitions, categories, category,
contract_terms_url, contract_url, exchange_index, fee_multiplier, fee_type,
frequency, last_updated_ts, settlement_sources, tags, ticker, title`
`[measured 2026-11-06]`.

### 5.10 Status filter ↔ lifecycle state

`docs: Market Lifecycle § Statuses`:

| Filter value | Matches |
|---|---|
| `unopened` | `initialized` (before `open_time`) |
| `open` | `active` |
| `paused` | `inactive` |
| `closed` | any market past `close_time` not yet `finalized` |
| `settled` | `finalized` |

Transitions: `initialized → active` and `* → closed` are time-based with **no**
WebSocket event; `active ↔ inactive` (`deactivated`/`activated`),
`closed_date_updated`, `closed → determined` (`determined`), and
`determined/amended → finalized` (`settled`) emit events. Reactivation cancels
all resting orders (`docs: Market Lifecycle § Transitions`).

### 5.11 `GET /exchange/status`

`docs: Get Exchange Status`; fields from `docs: API Changelog`, July 2, 2026.
Measured body `[measured 2026-11-06]`:

```json
{"exchange_active": true,
 "trading_active": true,
 "intra_exchange_transfers_active": true,
 "exchange_index_statuses": [
   {"exchange_index": 0, "description": "Default",
    "exchange_active": true, "trading_active": true,
    "intra_exchange_transfers_active": true}, ...]}
```

`exchange_estimated_resume_time` exists on the *Margin* exchange-status schema
(`docs: API Changelog`, entry stating "`GET /margin/exchange/status` documents
`exchange_estimated_resume_time`"); for the Predictions endpoint it is not
documented in the docs snapshot and was absent in the healthy measurement
`[unknown]`. Treat every field as optional and check for nulls.

### 5.12 `GET /exchange/schedule`

`docs: Get Exchange Schedule` — one line: "Endpoint for getting the exchange
schedule." Measured body `[measured 2026-11-06]`:

```json
{"schedule": {
  "maintenance_windows": [],
  "standard_hours": [
    {"start_time": "2024-12-01T00:00:00Z", "end_time": "2200-12-01T00:00:00Z",
     "monday":   [{"open_time": "00:00", "close_time": "00:00"}],
     "tuesday":  [{"open_time": "00:00", "close_time": "00:00"}],
     "wednesday":[{"open_time": "00:00", "close_time": "00:00"}],
     "thursday": [{"open_time": "00:00", "close_time": "03:00"},
                  {"open_time": "05:00", "close_time": "00:00"}],
     "friday":   [{"open_time": "00:00", "close_time": "00:00"}],
     "saturday": [{"open_time": "00:00", "close_time": "00:00"}],
     "sunday":   [{"open_time": "00:00", "close_time": "00:00"}]}]}}
```

`open_time`/`close_time` are local `HH:MM` windows; `00:00 → 00:00` means
"all day". The Thursday split is the weekly 03:00–05:00 ET maintenance window
(`docs: Maintenance and Pauses § Scheduled Maintenance`). Field semantics beyond
this shape are not documented `[unknown]`.

### 5.13 Historical endpoints (why a client needs the cutoff)

`docs: Historical Data`.

`GET /historical/cutoff` returns per-datatype cutoffs:
`market_settled_ts`, `trades_created_ts`, `orders_updated_ts`,
`market_positions_last_updated_ts`. Live endpoints stop returning records past
those points: `GET /markets` and `/markets/{ticker}` (and candlesticks) drop
markets settled before `market_settled_ts`; `GET /markets/trades` drops trades
filled before `trades_created_ts`. Archived equivalents:
`/historical/markets`, `/historical/markets/{ticker}`,
`/historical/markets/{ticker}/candlesticks`, `/historical/trades`. Events and
series are never archived — old events stay on `/events`, `/series`. Historical
endpoints use the same cursor pagination.

Practical consequence for a tape backfill: read the cutoff once, then split each
ticker's trade history at `trades_created_ts` between `/markets/trades` and
`/historical/trades`.

---

## 6. WebSocket

### 6.1 Frame envelope

Measured `[measured 2026-11-06]`:

```json
{"type": "orderbook_delta", "sid": 1, "seq": 41, "sending_ts_ms": 1793904935244,
 "msg": { ... }}
```

`type` discriminates; `sid` identifies the subscription; `seq` is per
subscription; `msg` is the payload. `type: "error"` frames look like
(`docs: Quick Start: WebSockets § Error Handling`):

```json
{"id": 123, "type": "error", "msg": {"code": 2, "msg": "Params required"}}
```

Unknown `type` values and unknown fields must be ignored, not treated as errors
(`docs: WebSocket API § Forward compatibility`).

### 6.2 Commands

Subscribe (`docs: Quick Start: WebSockets § Subscribing to Data`):

```json
{"id": 1, "cmd": "subscribe",
 "params": {"channels": ["orderbook_delta", "ticker", "trade"],
            "market_tickers": ["KXBTCD-26NOV0514-T100000"]}}
```

Ack: one frame per channel, echoing the command `id`, each with its own `sid`
`[measured 2026-11-06]`:

```json
{"type":"subscribed","id":1,"msg":{"channel":"orderbook_delta","sid":1},"sending_ts_ms":...}
{"type":"subscribed","id":1,"msg":{"channel":"ticker","sid":2},"sending_ts_ms":...}
{"type":"subscribed","id":1,"msg":{"channel":"trade","sid":3},"sending_ts_ms":...}
```

`sid` is assigned per channel per connection; keep the mapping, because
`unsubscribe` and `update_subscription` address `sids`, not channel names.

Unsubscribe — `sids` is mandatory and must be non-empty (error code 4 otherwise)
`docs: Quick Start: WebSockets § WebSocket Error Codes`:

```json
{"id": 2, "cmd": "unsubscribe", "params": {"sids": [1]}}
```

Update an existing subscription — `sids` must contain **exactly one** id
(error 12) and `params.action` is required (error 15):

```json
{"id": 3, "cmd": "update_subscription",
 "params": {"sids": [1], "market_tickers": ["A", "B"], "action": "get_snapshot"}}
```

`params` keys by channel class (`docs: Orderbook Updates`; `docs: Market Ticker`;
`docs: Public Trades`; `docs: User Fills`; `docs: Market Positions`):

| Key | Applies to |
|---|---|
| `channels` | required by `subscribe` (error 3 if absent) |
| `market_ticker` / `market_tickers` | required by `orderbook_delta` (error 14); optional filters on `ticker`, `trade`, `fill`, `market_positions` |
| `market_id` / `market_ids` | documented for `ticker`; explicitly **not** supported by `orderbook_delta` or `market_positions` |
| `update_interval_ms` | `orderbook_delta` only; `0`/omitted = real-time, `100` or `1000` = conflated |
| `use_yes_price` | `orderbook_delta` snapshot/delta pricing (§3.5) |
| `sids` + `action` | `update_subscription`; actions `add_markets`, `delete_markets`, `get_snapshot` on orderbook channels |
| `shard_factor` / `shard_key` | `communications` only (1–100 / 0 ≤ key < factor) |

`get_snapshot` (added for `orderbook_delta`, `docs: API Changelog`, Apr 2026)
returns an `orderbook_snapshot` for the named `market_tickers` **without**
changing the subscription; apply each returned snapshot as a full replacement of
that market's local book. Only `market_tickers` is accepted here — not
`market_ticker`, `market_id`, `market_ids`.

### 6.3 Channels

| Channel | Auth | Market filter | Notes |
|---|---|---|---|
| `orderbook_delta` | required | required (`market_ticker`/`market_tickers`) | snapshot + deltas; **one subscription per connection** |
| `ticker` | session only | optional; accepts `market_id`/`market_ids` | fires on any ticker field change |
| `trade` | session only | optional | fires after each execution |
| `fill` | required | optional | your fills only |
| `market_positions` | required | optional | your positions |
| `market_lifecycle_v2` | session only | **none** (no filtering) | all markets except `KXMVE*` |
| `multivariate_market_lifecycle` | session only | none | MVE markets only |
| `user_orders` | required | optional | your order lifecycle |
| `order_group_updates` | required | none | order-group lifecycle |
| `communications` | required | none | RFQs/quotes |

(`docs: Quick Start: WebSockets § Authentication`; `docs: Orderbook Updates`;
`docs: Market & Event Lifecycle`; `docs: Market Ticker`; `docs: Public Trades`;
`docs: User Fills`; `docs: Market Positions`.)

`market_lifecycle_v2` event types: `created`, `activated`, `deactivated`,
`close_date_updated`, `determined`, `settled`, `metadata_updated`, plus
`price_level_structure_updated`, `event_lifecycle`, `event_fee_update`
(`docs: Market Lifecycle § WebSocket`;
`docs: Fixed-Point Representation § Price Level Structures`). `determined`
carries `settlement_value` as a fixed-point dollar string
(`docs: API Changelog`, Feb 19, 2026). `metadata_updated` carries `floor_strike`,
`cap_strike`, `strike_type`, `custom_strike` where relevant (June 18, 2026).

`KXRFQ*` on-demand markets are excluded from `market_lifecycle_v2` entirely and
from unfiltered `trade`/`ticker` subscriptions, but are delivered when the
ticker is listed in `market_tickers` (`docs: API Changelog`, Nov 5, 2026).

### 6.4 `orderbook_delta` contract

`docs: Orderbook Updates`.

1. On subscribe you receive one `orderbook_snapshot` per market, then
   incremental `orderbook_delta` messages.
2. **Apply deltas in `seq` order.** A gap → request `get_snapshot` for all
   subscribed markets, or `unsubscribe`, wait for `unsubscribed`, then
   `subscribe` again. A repeated `subscribe` adds markets but does **not**
   resynchronise markets already subscribed.
3. One connection has exactly one `orderbook_delta` subscription. A repeated
   `subscribe` extends the same `sid` and **must reuse the same
   `update_interval_ms`** (omitting it means `0`); a different valid interval
   returns error 11 without adding markets. To change the interval, unsubscribe
   and subscribe again.
4. `update_interval_ms` is fixed for the life of the subscription; changing it
   requires `unsubscribe` → `unsubscribed` → `subscribe`. `use_yes_price` is
   likewise a subscribe-time parameter `[inference for the re-subscribe case:
   the docs only spell out the constraint for `update_interval_ms`]`.
5. Snapshots replace the local book wholesale. Never merge a snapshot into a
   book.

Measured payloads `[measured 2026-11-06, demo]`:

```json
{"type": "orderbook_snapshot", "sid": 1, "seq": 1, "sending_ts_ms": ...,
 "msg": {"market_ticker": "KXEBASKETBALLGAME-...-CADE",
         "market_id": "01a1ace4-bab4-7b46-92cb-1ba96f5e574c",
         "clock": 2604931253,
         "yes_dollars_fp": [["0.0100", "470.00"]]}}

{"type": "orderbook_delta", "sid": 1, "seq": 2, "sending_ts_ms": ...,
 "msg": {"market_ticker": "KXEBASKETBALLGAME-...-CADE",
         "market_id": "01a1ace4-bab4-7b46-92cb-1ba96f5e574c",
         "clock": 2604931316,
         "price_dollars": "0.0100", "delta_fp": "-10.00",
         "side": "yes",
         "ts": "2026-11-05T18:55:35.244Z", "ts_ms": 1793904935244}}
```

A delta carries a **signed** `delta_fp` (string) for one price level
(`[measured]`: `"-10.00"`). Applying it as an arithmetic add to that level's
size — dropping levels that reach zero — is the only consistent reading
`[inference]`. Empty sides are omitted from snapshots rather than sent as `[]`
`[measured]`. `side` is `"yes"` or `"no"`; combined with `use_yes_price` it
determines which leg `price_dollars` is on.

Conflated mode (`update_interval_ms` = `100` or `1000`) emits at most one net
delta per changed level per window, skipping levels whose net change is zero.
Consequences (`docs: Orderbook Updates § Conflated updates (opt-in)`):

* `clock`/`ts`/`ts_ms` describe the latest change in the delta, **not** book
  completeness; clocks may go backwards and must not be used to discard deltas.
* Your own orders bypass the interval; snapshots bypass it and replace pending
  held changes.
* A flush is a series of delta messages, **not** an atomic snapshot; there is no
  end-of-flush marker, and the book can transiently look locked or crossed.
* `seq` stays contiguous; a gap is still the recovery trigger.
* Do not merge or dedupe conflated streams from different connections by
  `clock` — rebuild from each connection's own snapshot.

### 6.5 `ticker` channel payload

Measured `[measured 2026-11-06, demo]`:

```json
{"type": "ticker", "sid": 1, "sending_ts_ms": ...,
 "msg": {"market_id": "...", "market_ticker": "...",
         "price_dollars": "0.0000", "yes_bid_dollars": "0.0100",
         "yes_ask_dollars": "1.0000", "volume_fp": "0.00",
         "open_interest_fp": "0.00", "dollar_volume": 0,
         "dollar_open_interest": 0, "yes_bid_size_fp": "420.00",
         "yes_ask_size_fp": "0.00", "last_trade_size_fp": "0.00",
         "ts": 1793904936, "ts_ms": 1793904936053,
         "time": "2026-11-05T18:55:36.053Z"}}
```

`dollar_volume` / `dollar_open_interest` are **integers**, not dollar strings,
and their unit is not stated in the docs snapshot `[unknown — inference: cents]`.
Use `volume_fp` / `open_interest_fp` for contract counts and the `*_dollars`
fields for prices.

### 6.6 `trade` and `fill` payloads

Documented: `trade` fires "immediately after trade execution"; market filter
optional; carries `is_block_trade` (Aug 13, 2026) and `taker_side` /
`taker_outcome_side` / `taker_book_side` (`docs: Public Trades`;
`docs: Order direction (outcome_side and book_side)`). Timestamps are `ts`
(Unix seconds) plus `ts_ms` (Apr 15, 2026). The full field list is not
enumerated in the docs snapshot and no demo trade arrived during measurement,
so the field list is `[unknown]`; treat the REST trade object in §5.6 as the
expected shape plus `ts`/`ts_ms`.

`fill` is user-scoped, requires auth, optional market filter, supports
`update_subscription` with `add_markets`/`delete_markets`; `client_order_id` is
present when the change was caused by your own order
(`docs: User Fills`; `docs: Quick Start: WebSockets § Processing Messages`).
`fill` and `user_orders` are the two channels served by the dedicated warp hosts.

### 6.7 Keep-alive

`docs: Connection Keep-Alive` — Kalshi sends Ping (0x9) every 10 seconds with
body `heartbeat`; clients answer Pong (0xA), and may send their own Pings.
Python `websockets` does this automatically. Scheduled maintenance is every
Thursday 03:00–05:00 **ET**, with disconnections possible; reconnect after
05:00 ET (`docs: Maintenance and Pauses § Scheduled Maintenance`).

### 6.8 Error codes

`docs: Quick Start: WebSockets § WebSocket Error Codes` (plus later changelog
entries):

| Code | Meaning | User error |
|---|---|---|
| 1 | Unable to process message (bad JSON / wrong field type; also a non-integer `update_interval_ms`) | Y |
| 2 | Params required | Y |
| 3 | Channels required | Y |
| 4 | Subscription IDs required (`unsubscribe` without `sids`) | Y |
| 5 | Unknown command | Y |
| 6 | Retired | – |
| 7 | Unknown subscription ID | Y |
| 8 | Unknown channel name (incl. any non-`fill`/`user_orders` channel on a warp host) | Y |
| 9 | Authentication required | Y |
| 10 | Channel error (internal; contact support) | N |
| 11 | Invalid parameter (e.g. `update_interval_ms` mismatch on re-subscribe) | Y |
| 12 | Exactly one subscription ID required | Y |
| 13 | Unsupported action | Y |
| 14 | Market ticker required | Y |
| 15 | Action required | Y |
| 16, 17 | Retired | – |
| 18 | Command timeout (server-side) | N |
| 19–22 | `communications` shard args invalid | Y |
| 24 | Index IDs required (`cfbenchmarks_value`) | Y |
| 25 | Subscription buffer overflow — subscribe to less, read faster | Y |
| 26, 27 | Pyth feed subscription arguments — described only in the Pyth page's integration notes, exact text not in the error table `[unknown]` | Y |
| 28 | Underlying tickers required (Pyth `subscribe_underlyings` / `unsubscribe_underlyings`) | Y |
| 29 | `update_interval_ms` is a value other than `0`/`100`/`1000` (`docs: API Changelog`, Nov 5, 2026) | Y |

Codes 23 and 26–29 are absent from the docs' error table. 28 is stated on the
Pyth Value Feed page and 29 in the Nov 5, 2026 changelog entry; 23, 26 and 27
have no stated meaning in the snapshot `[unknown]`.

### 6.9 Connection and subscription limits

* Concurrent open WS connections are capped **per user** across Predictions and
  Perps (`docs: Rate Limits and Tiers § WebSocket connection limits`): Basic /
  Advanced / Expert **200**, Premier 500, Paragon 750, Prime 1,000, Prestige
  1,000. At the cap, new handshakes return **HTTP 429**.
* Orderbook subscription sanity limits: **max 500k market subscriptions per
  session** and **max 10k commands per second** (`docs: API Changelog`,
  June 18, 2026).
* One `orderbook_delta` subscription per connection (§6.4). There is no
  documented per-subscription market-count cap; the per-connection cap above is
  the binding one.

---

## 7. Rate limits and tiers

`docs: Rate Limits and Tiers`.

* Every **authenticated** request costs **tokens**; the tier sets the refill
  budget in tokens/second. Sustained rate for an endpoint = `budget ÷ cost`.
  Default cost is **10 tokens**; non-default costs are listed by
  `GET /account/endpoint_costs` (authenticated).
* Two independent buckets: **Read** (`GET` and anything not routed to Write) and
  **Write** (orders, amends, cancels, order groups, RFQ quote flow, block-trade
  accepts). REST and FIX drain the same buckets. There is no documented bucket,
  cost or 429 rule for **unauthenticated** requests `[unknown]`.
* Predictions budgets, tokens/s (`docs: Rate Limits and Tiers § Predictions
  budgets`):

| Tier | Read | Write |
|---|---|---|
| Basic | 200 | 100 |
| Advanced | 300 | 300 |
| Expert | 600 | 600 |
| Premier | 1,200 | 1,200 |
| Paragon | 2,400 | 2,400 |
| Prime | 4,800 | 4,800 |
| Prestige | 12,000 | 9,600 |

* Buckets are token buckets with no fixed windows: Basic/Advanced Read buckets
  and Write buckets above Basic hold **three seconds** of budget (burst up to
  3× the per-second budget after three quiet seconds); Read buckets above
  Advanced and Basic-tier Write buckets hold **one second**.
* `GET /portfolio/balance` has its own bucket: 1/s on Basic and Advanced, 5/s
  above; it does not draw on Read.
* Batch endpoints cost the sum of their items; the whole batch must fit the
  bucket at once or the batch is rejected (`docs: Rate Limits and Tiers § Batch
  endpoints don't save tokens`).
* On exhaustion: **HTTP 429**, body `{"error": "too many requests"}`, **no**
  `Retry-After` and no `X-RateLimit-*` headers, no cooldown or penalty — just
  retry with exponential backoff (`docs: Rate Limits and Tiers § When you hit
  the limit`).
* Order writes that name an exchange shard (`exchange_index >= 1`) bill that
  shard's Write bucket; `-1`/omitted auto-routes and bills **every** shard's
  bucket; batch writes always bill the unscoped bucket. Reads are never
  shard-scoped (`docs: Rate Limits and Tiers § Predictions per-shard Write
  budgets`).

Planning number for a market-data poller on Basic: 200 tokens/s ÷ 10 = **20
requests/s** sustained, with a burst of up to 60 requests after idling three
seconds. No market-data-specific per-ticker cost is documented: the documented
rule is that a batch request "costs the same as making each call individually"
(`docs: Rate Limits and Tiers § Batch endpoints don't save tokens`). Until
`GET /account/endpoint_costs` says otherwise, budget `/markets/orderbooks` at
10 tokens **per ticker** (100 tickers = 1,000 tokens, i.e. the whole batch must
fit the bucket at once), not 10 per call `[inference — the only conservative
reading of the documented rule]`.

---

## 8. What works today without a production key

| Capability | Status | Why |
|---|---|---|
| `GET /markets`, `/markets/{ticker}` | **works now, no key** | public REST (`docs: Quick Start: Market Data`) |
| `GET /markets/{ticker}/orderbook`, `/markets/orderbooks` | **works now, no key** | public (`docs: Orderbook Responses`) |
| `GET /markets/trades`, `/historical/trades` | **works now, no key** | public; `/historical/trades` is explicitly documented as public |
| Candlesticks (single + batch), `/historical/markets*` | **works now, no key** | public |
| `GET /events`, `/events/{ticker}`, `/series`, `/series/{ticker}` | **works now, no key** | public |
| `GET /exchange/status`, `/exchange/schedule` | **works now, no key** | public |
| `GET /historical/cutoff` | **works now, no key** | public |
| Demo WS (`orderbook_delta`, `ticker`, `trade` verified) | **works with the demo key** | handshake auth; demo data only |
| Production WS (orderbook/ticker/trade/…) | **needs a production key** | demo key → HTTP 401 at handshake `[measured]` |
| `fill`, `user_orders`, `market_positions`, `communications` | **needs a production key** | private channels |
| `GET /portfolio/*`, orders, fills, settlements | **needs a production key** | authenticated |
| `GET /account/endpoint_costs`, `/account/limits` | **needs a production key** | authenticated |

Consequence for this repo: a Kalshi→Polymarket signal can be built **today**
from production REST (BBO, trades, candles, lifecycle metadata) with no key at
all; sub-second production streaming, and therefore any latency-sensitive
cross-venue measurement, waits for a production key. Demo WS proves the client
code path but not the data (demo prices are not representative).

---

## 9. Unknowns

Not stated in the docs snapshot, and not measured; do not guess them in code:

1. Unauthenticated rate-limit bucket, cost and 429 behaviour.
2. Full `trade` / `fill` WS field lists (only the `taker_*`/`is_block_trade`/
   timestamp additions are documented).
3. `market_lifecycle_v2` message timestamp field names.
4. Exact `/markets/trades` window semantics (`min_ts` inclusive vs exclusive)
   and whether there is a maximum window length.
5. `exchange/schedule` field semantics beyond the measured shape.
6. Whether candle buckets are emitted for periods with zero activity.
7. Error code 23's meaning.
8. Whether `/markets/orderbooks` has a per-request ticker cap lower than the
   documented 100 in practice.
9. `/markets/{ticker}/orderbook` `depth` parameter existence.
10. The OpenAPI spec itself (`https://docs.kalshi.com/openapi.yaml`) and the
    AsyncAPI spec (`https://docs.kalshi.com/asyncapi.yaml`) are the declared
    source of truth for schemas (`docs: Kalshi SDKs`); they are not included in
    the docs snapshot, so any field not named above should be read from there
    before use.

---

## 10. Repo-specific notes

* TLS to Kalshi resets on this machine (WinError 10054 during the handshake), so
  every REST call must go through the pinned-edge client already in
  `src/polymarketbot/data/streams/kalshi.py` (`KalshiClient` + `EdgePool`, which
  probes a real TLS handshake per edge) with
  `polymarketbot.net.install_doh_fallback()` installed first. A plain
  `requests.get` fails.
* That client already raises on 429/5xx and on connection errors, rotating the
  pinned edge only for transport failures — the correct behaviour for the
  documented rate-limit semantics in §7.
* The existing stream stores `yes_bid`/`yes_ask`/`last_price` as probabilities
  parsed from the `*_dollars` fields and `volume_fp` as a count; keep that
  convention (§3.1) for anything new.
* A WS client must be built for production hosts but can only be *verified* on
  demo today; gate the host on whether the key is a demo key
  (`KALSHI_ENV=demo` in `.env`) so a demo key never pays a 401 handshake in
  production.

## Deployment note (2026-11-06)

This integration is **REST-only**. A production Kalshi key requires US KYC, so
the production WebSocket (and every authenticated endpoint) is out of scope for
this deployment. The WS sections above document the contract and were verified
on the demo environment; they are not a production dependency. Public market
data over REST needs no key.

