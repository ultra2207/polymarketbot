# Polymarket mechanics — distilled reference

Everything below is taken from the mirrored docs (`data/raw/docs/`, 96 pages plus
`llms-full.txt`), verified against live endpoints where possible. It exists so the code can
be read against one accurate model instead of assumptions.

---

## 1. Two version axes — do not conflate them

This is the single most important correction to the original plan.

| Axis | Values | What decides it | Where it shows up |
|---|---|---|---|
| **Exchange generation** | v1 / v2 | which exchange contract settled the fill | `trades.contract`: `CTF_EXCHANGE` vs `CTF_EXCHANGE_V2` |
| **Position system** | CTF (v1) / PositionManager (v2) | where outcome balances live | Gamma `version`: `"v1"` / `"v2"` |

The April 2026 cutover changed the **exchange** (new contracts, pUSD collateral, fees at
match time). It did **not** migrate every market to PositionManager. Markets created in
June 2026 still report `version: "v1"` and trade on CTF Exchange V2.

`markets_pit` therefore carries both:

- `exchange_generation` — `v1` / `v2` / `mixed`, derived from fills
- `position_system` — `ctf` / `position_manager`, from Gamma `version`

**Our tape is CTF-only.** SII `trades.parquet` contains only four contracts
(`CTF_EXCHANGE`, `NEGRISK_CTF_EXCHANGE`, and their `_V2` successors). It contains no
ExchangeV3 (`0xe333…`) fills at all, so PositionManager markets are out of scope for the
historical backtest. They matter for live trading, not for 2022–2026 replay.

---

## 2. Identifiers

| Identifier | Shape | Notes |
|---|---|---|
| Condition ID | `bytes32`, `0x…` | `keccak256(abi.encodePacked(oracle, questionId, outcomeSlotCount))` |
| CTF token ID | uint256 | the ERC-1155 id in Conditional Tokens; gamma `clobTokenIds` |
| Position ID | uint256 | the ERC-1155 id in PositionManager; gamma `positionIds` |
| Event / market id | integer | Gamma's application-level ids, not on-chain |

A **Position ID embeds its own routing**:

```
[moduleId 8 | baseHash 128 | arity 16 | reserved 64 | resolutionChain 16 | conditionIndex 16 | outcomeIndex 8]

moduleId      = positionId >> 248        (1 Binary, 2 NegRisk, 3 Combinatorial)
outcomeIndex  = positionId & 0xff        (0 = YES, 1 = NO)
conditionId   = positionId with bits 0..7 cleared
eventId       = positionId with bits 0..23 cleared
```

Never send a CTF token ID to PositionManager or a Position ID to Conditional Tokens.

**Slot order for CTF binary markets is proven, not assumed**: `positionIds[0]` (index set 1)
is YES, `[1]` (index set 2) is NO. Our label build confirmed `token1 == positionIds[0]` for
934,889 of 934,957 conditions.

---

## 3. Markets, events, negative risk

- A **market** is one binary question with YES/NO. An **event** groups markets.
- **Single-market events** are common for crypto up/down; **multi-market events** for sports
  and elections.
- **Negative risk**: in a neg-risk event, 1 NO share in any market can be converted into
  1 YES share in **every other** market. This is why "bet against A" is capital-equivalent
  to "bet for everyone else".
- **Augmented negative risk** handles outcomes that appear after trading starts, using
  named outcomes + placeholders + an explicit **Other**. Placeholder outcomes should be
  ignored until named; the UI hides them.
- Neg-risk markets are decomposed into **binary conditions** (one per outcome), which is why
  `condition.parquet` contains only 2-slot conditions.

Implication: neg-risk baskets create real arbitrage structure (sum of YES prices ≠ 1 across
an event) and a real PnL trap (a NO position is economically several YES positions).

---

## 4. Order lifecycle and matching

1. Order is signed (EIP-712) and submitted to the CLOB.
2. It is validated (balance, allowances, tick size).
3. If marketable, it matches immediately; otherwise it rests.
4. Settlement happens on-chain; only fills are emitted.

Details that matter:

- **Taker delay** exists on some markets (crypto). History from the changelog:
  250 ms → **50 ms** on 2026-08-17 → **150 ms** on 2026-09-04. Latency strategies must model
  this; hold-to-resolution strategies do not.
- **Matching-engine restarts**: order actions return HTTP `425` with `Retry-After`; after a
  restart the engine runs **post-only for two minutes**.
- **Post-only** orders that would cross are rejected rather than taking.
- Order statuses and trade statuses are distinct; a matched trade settles on-chain shortly
  after.

Queue position is still unknowable from public data — see `docs/limitations.md`.

---

## 5. Fees

```
fee = C × rate × (p × (1 - p)) ^ exponent
```

- `C` = shares, `p` = price, makers pay **zero**.
- Rounded to **5 decimal places**; below `0.00001` rounds to zero.
- Symmetric around 50¢.

Rates (from 2026-07-10):

| Category | Rate | Maker rebate |
|---|---|---|
| Crypto | 0.07 | 20% |
| Sports | 0.05 | 15% |
| Finance / Politics / Tech / Mentions | 0.04 | 25% |
| Economics / Culture / Weather / Other | 0.05 | 25% |
| Geopolitics | 0 | — |
| Combos | 0.05 | 0% |

History: fee-free before 2026-01-05 → crypto pilot (peak 1.56%) → Fee Structure V2 on
2026-03-30 → sports 0.03→0.05 and rebate 0.25→0.15 on 2026-07-10.

**The authoritative source is the per-market `feeSchedule`**, not the category. Gamma returns:

```json
"feeType": "sports_fees_v3",
"feeSchedule": { "rate": 0.05, "exponent": 1, "takerOnly": true, "rebateRate": 0.15 }
```

Observed `feeType` values in a 200-market sample: `crypto_fees_v2`, `sports_fees_v3`,
`sports_fees_v2`, `weather_fees`, `crypto_fees`, `crypto_15_min`, `finance_prices_fees`.
Across ~35 k markets the full set also includes `sports_fees` (legacy 0.0175),
`sports_fees_nfl_cfb_oct26` (0.03, a per-league exception), `zero_fees`, `general_fees`,
`culture_fees`, `politics_fees`, `tech_fees`, `economics_fees`, `mentions_fees`.

**The exponent is part of the schedule.** `crypto_fees` and `crypto_15_min` are
`rate 0.25, exponent 2`, not `rate 0.0625, exponent 1`. They share a 1.5625 % peak at 50¢ but
differ by ~3× at 10¢. See `docs/fees.md`.
See `docs/fees.md`.

---

## 6. Trading constraints

| Constraint | Values |
|---|---|
| Minimum price increment (tick) | `0.1`, `0.01`, `0.001` |
| Minimum order size | e.g. `5` USDC notional |

Prices must be multiples of the active tick. Ticks change over a market's life
(`tick_size_change` events). In a 200-market sample, **165 used `0.001`, 35 used `0.01`** —
so assuming `0.01` is often wrong. `markets_pit.tick_size` now prefers Gamma, then Dome,
then an explicit `assumed` flag.

---

## 7. Position lifecycle

| Operation | Effect | Event (CTF) |
|---|---|---|
| Split | 1 pUSD → 1 YES + 1 NO | `PositionSplit` |
| Merge | 1 YES + 1 NO → 1 pUSD | `PositionsMerge` |
| Redeem | winning tokens → payout | `PayoutRedemption` |
| Convert (neg risk) | 1 NO → 1 YES in every other outcome | `PositionsConverted` |

Balances are ERC-1155 `TransferSingle` / `TransferBatch`. **A lifecycle event and its
underlying transfer are not two balance changes** — count transfers for balances, lifecycle
events for context.

This matters for PnL: a trader who splits and sells one leg has a cost basis that cannot be
recovered from fills alone. Splits/merges are available in moose-code only through
2026-04-28.

---

## 8. Resolution

Two sources:

- **UMA Optimistic Oracle** for prediction markets.
  - Proposal with a bond (~$750), then a **2-hour challenge period**.
  - Disputed: new proposal, then 24–48 h debate, then ~48 h UMA vote.
  - Undisputed ≈ **2 hours**; disputed ≈ **4–6 days**.
  - Can resolve **50/50**, which pays $0.50 per share — a void, not a loss.
- **Chainlink TWAP** for crypto up/down.
  - TWAP at start = price to beat; TWAP at end = final price.
  - Up if final ≥ start, Down otherwise. Deterministic, no dispute.
  - 5-minute markets use a 30 s lookback; 15-minute and 4-hour use 60 s.

On-chain events: `ConditionPreparation`, `ConditionResolution`, `OutcomeReported`,
`PayoutRedemption`. Resolution timing is therefore recoverable from logs, and
`/v2/resolutions` exposes `resolved_at`, `resolved_block`, `was_disputed` for recent markets.

---

## 9. Data surfaces

### Gamma (`gamma-api.polymarket.com`)
Market/event metadata, tags, `version`, token/position ids, `feeSchedule`, tick and minimum
order size. Keyset pagination uses **`after_cursor`**. **Omitting `closed` on
`/markets/keyset` returns open markets only** — the cause of a silent zero-row bug during
this build.

### Data API v2 (`data-api.polymarket.com/v2`)
The most under-used free surface:

| Endpoint | Gives |
|---|---|
| `/v2/trades` | **taker-perspective** fills: a pure-maker wallet returns zero rows here. Use `/v2/activity` for a complete history |
| `/v2/activity` | per-wallet lifecycle: `TRADE` (maker **and** taker), `SPLIT`, `MERGE`, `REDEEM`, `CONVERSION`, deposits/withdrawals (opt-in) |
| `/v2/positions` | open/closed positions with realized/unrealized PnL, `entry_cost_usdc`, `entry_fees_usdc`, `first_entry_at` |
| `/v2/holders` | current holders, optionally with per-holder economics (`include_pnl=true`, ≤100/page) |
| `/v2/oi` | open interest per market or globally |
| `/v2/prices-history` | token price series: `interval=max` from 2022 at 12 h buckets; 3 h from Jul 2026; finer grains windowed (raw ≥7 d, 5 m ≥60 d, 30 m ≥90 d) |

> **Caveat, measured on 52 tokens (one per month, 2022→2026):** `interval=max` returns
> 12 h buckets back to 2022, but the **first point is frequently later than the market's
> `created_at`**, and a few old token histories look inconsistent with the market's life
> (e.g. a January-2022 market returning points through 2024). Treat it as a coarse
> cross-check and gap-filler *after* validating the token's series, not as a guaranteed
> full-life tape.
| `/v2/resolutions` | resolution state, `payouts`, disputes, resolved_at |
| `/v2/tokens` | token ids, `module` (`v1_ctf` / v2), `final_price`, resolved |
| `/v2/leaderboard`, `/v2/biggest-winners` | ranked wallets without scanning the tape |
| `/v2/user-stats` | all-time PnL **decomposed**: `realized_market_pnl`, `realized_lp_pnl`, `realized_combo_pnl`, `fees_refunded`, `maker_rebate`, `taker_rebate` |
| `/v2/user-pnl` | the same decomposition as a time series (`points`) |
| `/v2/value` | current portfolio value |

**`/v2/activity` is the answer to the post-April lifecycle gap.** It returns splits, merges,
redeems and conversions per wallet, which the `moose-code` tables stop providing after
2026-04-28. The feed is keyset-paginated on `(block_timestamp, sequence_id)`; re-send every
filter on each page, because the cursor carries only its seek anchor.

**`/v2/user-stats` decomposes PnL by source.** A wallet whose `maker_rebate` is material while
`realized_market_pnl` is small is a market maker; a wallet with `realized_lp_pnl > 0` earns
liquidity rewards; a wallet with only `realized_market_pnl` is directional. This is the
cheapest archetype signal available and it needs one request per wallet.

**`/v2/holders` is current-state only.** A fully redeemed market returns zero holders, so it
cannot supply final PnL for closed markets. Use `/v2/positions?user=&status=CLOSED` instead.
Closed positions expose `current_size`/`total_size` (not `size`), plus `mergeable`,
`redeemable` and `negative_risk`.

### CLOB (`clob.polymarket.com`)
Live book and prices (`/book`, `/books`, `/price`, `/midpoint`, `/prices-history`), tick
size. Authenticated endpoints for orders.

### RTDS / PolyBolt (`ws-live-v2.polymarket.com`)
Market stream (`book`, `price_change`, `last_trade_price`, `tick_size_change`,
`best_bid_ask`, `new_market`, `market_resolved`) and **`prices.crypto.twap`**, Chainlink
TWAP with a fixed 60 s window. TWAP requires an authenticated `SecureClient`.

### Rate limits (per IP, 10 s windows)

| Surface | General | Notable |
|---|---|---|
| Gamma | 4,000 | `/events` 500, `/markets` 300, `/tags` 200 |
| Data API v2 | 800 | `/v2/trades` 300; positions/activity/prices-history 200 |
| CLOB | 9,000 | `/book` 1,500, `/prices-history` 1,000 |
| Bridge | 50 | — |

Throttled, not rejected, unless sustained. `429` carries `Retry-After`. Our clients default
to 10–20 req/s, comfortably inside.

### Freshness
`GET /v2/status` reports `serving` lag (projections) and `ingestion` lag (per-contract crawl
cursors). Read it before trusting a freshly fetched snapshot.

---

## 10. Collateral

**pUSD** (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`) replaced USDC.e as collateral with
V2. It is an ERC-20 on Polygon backed by USDC, with on/off ramps. Pre-cutover fills are in
USDC.e; the two are 1:1 in practice but are different contracts.

---

## 11. Contracts (Polygon, chain 137)

| Contract | Address |
|---|---|
| Conditional Tokens (CTF) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| CTF Exchange (v2 generation) | `0xE111180000d2663C0091e4f400237545B87B996B` |
| Neg Risk CTF Exchange | `0xe2222d279d744050d28e00520010520000310F59` |
| Neg Risk Adapter | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| PositionManager (Poly v2) | `0x006F54F7f9A22e0000CC2AB60031000000ae9fEF` |
| Router (Poly v2) | `0x12121212006e4CD160D18e3f00711DA5c3372600` |
| ExchangeV3 (Poly v2) | `0xe3333700cA9d93003F00f0F71f8515005F6c00Aa` |
| BinaryModule | `0x1000008dD9001B968442c1000017eaE6E0dA00Ba` |
| NegRiskModule | `0x200000900045e3B6259600682756002200028933` |
| CombinatorialModule | `0x30000034706C7d8e12009DAB006Be20000c031A8` |
| AutoRedeemer | `0xa1200000d0002264C9a1698e001292D00E1b00af` |
| pUSD | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
| UMA Adapter v3.1.0 | `0x157Ce2d672854c848c9b79C49a8Cc6cc89176a49` |
| UMA Optimistic Oracle | `0xCB1822859cEF82Cd2Eb4E6276C7916e692995130` |

---

## 12. What this changes for the project

1. **Universe table** now separates `exchange_generation` from `position_system`. The
   historical tape is CTF-only; PositionManager is a forward/live concern.
2. **Tick size** must come from Gamma, not a `0.01` default. 82% of sampled markets use
   `0.001`.
3. **Fees** are per-market (`feeSchedule`), with a validated era fallback.
4. **Wallet skill** (Phase 6b) can start from `/v2/leaderboard` and `/v2/biggest-winners`,
   then pull only those wallets' `/v2/positions` and `/v2/trades?full_history=true`. This
   avoids exploding the 60 GB `users.parquet` and is the single biggest shortcut found.
5. **Price history** (`/v2/prices-history`) is a free, permanent 12 h series back to 2022.
   It can fill gaps where PMXT depth is missing and provide a point-in-time mid for
   calibration.
6. **Resolution timing** is recoverable (`/v2/resolutions`, on-chain logs), which the
   original plan assumed was unavailable.
7. **Crypto reference prices**: PolyBolt `prices.crypto.twap` is the exact Chainlink TWAP
   used for resolution, but it requires authentication. Binance klines remain the free
   backfill; they are not the same series, so alignment must be validated.
8. **The post-April lifecycle gap is closable per wallet** via `/v2/activity` (splits, merges,
   redeems, conversions). Our own strategy PnL still does not need redemption events, but
   wallet reconstruction does.
9. **Wallet archetypes can be inferred from PnL decomposition** (`/v2/user-stats`), not only
   from trade patterns: `maker_rebate` vs `realized_market_pnl` vs `realized_lp_pnl` separates
   makers, directional traders and liquidity providers with one request per wallet.

Implementation: `src/polymarketbot/gamma.py`, `src/polymarketbot/dataapi.py`,
`src/polymarketbot/fees.py`, `python -m polymarketbot.data.builders.enrich_market_metadata`.
