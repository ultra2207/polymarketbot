# Sweep — live execution, venue protocol, on-chain settlement

Independent defect sweep after the 2026-11-11 fix pass. Scope:
`src/polymarketbot/live/`, `src/polymarketbot/execution/`,
`src/polymarketbot/strategy/venue/`, `src/polymarketbot/strategy/onchain/`.
Already-fixed items from `docs/codebase-quality-report.md` are not repeated.

Evidence base used for "protocol reality" claims:
`data/raw/docs/site/resources/error-codes.md` (per-order `success: true` with an
empty `orderID`, 425 restart, 503 cancel-only/post-only mode),
`data/raw/docs/site/trading/matching-engine.md` (batch post-only rejections),
`data/raw/docs/llms-full.txt:5247-5256` (`orderMinSize` is USDC notional),
`:895-915` (taker delay holds a marketable order, uncancellable while in flight),
`:40261-40305` (`DELETE /order` answers `canceled` / `not_canceled`).

## Fixed

| id | sev | file:line | defect | fix |
|---|---|---|---|---|
| E1 | P0 | `strategy/venue/clob/cancel.py:19-80` | A 2xx `DELETE /order` was reported as a confirmed cancel; the venue's `not_canceled` (`order already matched`) was ignored, so local state, reserved cash and the wash-trade record were dropped for an order that may still rest. | `_cancel_confirmed` parses the documented `canceled`/`not_canceled` body; only a named cancel returns True. STP record released only after confirmation; `cancel_all_orders` likewise. |
| E2 | P0 | `strategy/venue/clob/dispatch.py:168,95` | A 200 body without `success` was treated as a successful placement, and an empty `orderID` was accepted — Polymarket answers per-order rejections with `success: true, orderID: "", errorMsg: …`. The engine then tracked and reserved cash for an order that does not exist. | `_order_response_outcome` requires a non-empty order id and no `errorMsg`; both REST and HFT paths use it. |
| E3 | P0 | `strategy/venue/clob/dispatch.py:154` | 425 (matching engine restarting), 429 and 503 (cancel-only/post-only mode) were classified `REJECTED`, indistinguishable from a permanent rejection, and nothing honoured the server's `retry_after_seconds`. | New `RESTARTING`/`THROTTLED`/`RESTRICTED` statuses; the server's own delay is latched on the client and suppresses submissions until it expires (cancels still allowed). No invented backoff constant. |
| E4 | P0 | `strategy/venue/clob/batch.py:78-85,148-160` | Batch signing omitted `neg_risk` (signed against the wrong EIP-712 exchange domain) and `fee_rate_bps`; a short/sparse response mapped a missing row to `success=True, order_id=""`; a non-list body left every order at `PENDING, success=True`. | `neg_risk`/`fee`/tick resolved per order (dry-run keeps the offline defaults); every item must carry a real order id; a non-list body marks the whole batch unconfirmed. |
| E5 | P0 | `strategy/venue/clob/orders.py:155`, `signing.py:141-147,212-218` | `fee_rate_bps` defaulted to 0 (the V1 struct commits to it → taker fee priced at zero); V1 post-only was never transmitted, so a "maker" quote could cross as a taker. | `fee_rate_bps` is `None` by default and a V1 order without it is refused; `postOnly: true` is added to the V1 payload (documented top-level field). |
| E6 | P1 | `strategy/venue/clob/reads.py:246-277` | `get_market` turned a missing/garbled `minimum_tick_size` into 0.01 and a missing `fee_rate_bps` into 0. | Raises `ClobMarketParamsUnreadable` (a `ClobAPIError` subclass, so existing degrade paths still work). Test that pinned the defaults rewritten. |
| E7 | P1 | `strategy/venue/clob/http.py:174-186` | `_paginate` returned a *truncated* order list at `max_pages`; callers reconcile "absent from open orders" against it, so resting orders beyond page 20 looked cancelled/filled. | Raises `ClobAPIError` instead of returning a partial view. |
| E8 | P0 | `strategy/onchain/ctf/calldata.py:47,94` | `round(amount_shares * 1e6)` — half-to-even, can round **up** to more base units than held (guaranteed revert); `rjust(64,"0")` silently packed malformed ids/addresses; negative amounts produced garbage words. | `amount_to_6dp` (ROUND_DOWN, refuses invalid) + validated, width-checked words on the live path (`strict`); dry-run keeps stable bytes for simulation. Encoding failures become explicit refusals in `merge_positions`/`redeem_positions`. |
| E9 | P0 | `live/engine/fills.py:150` → `live/engine/merge.py:79-89`, `orders.py:239-245` | The CTF merge was triggered *inside* `with self._lock` from the fill path, so the broadcast plus the 60 s receipt wait ran with the engine lock held (contradicting `merge.py`'s own three-phase contract); fills and book updates were blocked for the whole settlement. | Fills set `_merge_requested`; `run_pending_ctf_merge()` runs the merge after `sync_live_orders_and_fills` releases the lock. |
| E10 | P0 | `live/engine/orders.py:35-66,134-141,195-202` | An unparseable/absent `size_matched` became 0.0, so the fill delta was `max(0, 0-prev) = 0`; with a terminal status the order was then dropped — the fill was lost permanently. | `_order_matched_size` returns `None` for "unreadable"; the order stays tracked and no fill is booked until the venue reports a usable figure (logged at warning). |
| E11 | P1 | `live/engine/onchain.py:70-88` | A receipt whose `status` was absent or unparseable was resolved as `REVERTED`, clearing the single-flight row and letting the same complete sets be merged again. | Unreadable status leaves the row `PENDING`. |
| E12 | P1 | `live/engine/onchain.py:189-236` | Merge proceeds changed cash but `realized_pnl` and `ctf_merges.realized_gain` were a literal 0.0: buying a set at 0.96 and merging at 1.00 realised nothing. | Gain booked as `unlocked − gas − sets·(avg_yes+avg_no)`; unbooked (with a warning) only when the cost basis is unknown. |
| E13 | P1 | `live/engine/ledger.py:53,99,179`, `live/base.py:923`, `live/quoting.py:153` | `unrealized_pnl` was the inventory's *market value* (and in the risk feed, `equity − cash`, which also counted locked collateral), so every position read as 100 % profit. | `_unrealized_pnl()` = inventory at the live mid − cost basis (0.0 when the basis is unknown). |
| E14 | P1 | `strategy/onchain/ctf/balances.py:22-65` | A failed POL/USDC read returned `0.0` at `debug` level (and 0.50 POL / 100 USDC in dry-run): a fabricated balance in the tape and a gas-starved wallet that looked funded. | Returns `None` ("unmeasured") at warning level; `preflight`/`sync_live_balances` keep the last figure and say so; the engine's `pol_balance` default is now `None`, not `0.50`. |
| E15 | P1 | `strategy/venue/feed/connection.py:125`, `feed/client.py:82-90`, `feed/decode.py:23-33` | Liveness was measured from *any* frame, and `decode` stamps it for PONGs too — a wedged subscription that still answers heartbeats counted as healthy forever, so the REST fallback never engaged. | Separate `_last_ws_data_time`, set only for a parsed data frame and reset on reconnect; liveness uses it. |
| E16 | P1 | `strategy/venue/feed/book.py:188-206`, `connection.py:124-146` | A malformed `price_change` (or unknown side) was dropped at `debug` and the corrupted book kept being published. | `_mark_desynced` flags the asset (one warning per episode) and the fallback poller re-seeds exactly those assets from REST; cleared by a full snapshot. |
| E17 | P1 | `strategy/venue/feed/rest.py:77-84,106-110` | A 200 body without `mid`/`spread` was returned as `0.0` — a valid-looking extreme price / locked market. | `_parse_opt_float` → `None` ("unknown"). |
| E18 | P1 | `strategy/venue/reads.py:435-500` | A 429 / non-200 / transport failure in `condition_tape` returned `[]` and that empty list was cached as a *fresh* measurement for a full TTL: a throttled market reported "no prints". | The reader returns `None` for a failed read; failed conditions keep their previous cache entry and are retried next tick. |
| E19 | P2 | `live/engine/deskew.py:128-147,228,305` | The dry-run Tier-2 cross skipped the live caps (`max_quote_size_shares`, SELL clamp to owned shares), so simulated size/PnL diverged from what live can execute. | Shared `_tier2_target_size` for both paths. |
| E20 | P1 | `live/supervisor/switch.py:55-97`, `live/supervisor/sizing.py:253-345` | The live auto-selector never supplied `crossing_spread_friction_pct`, so the switching cost was `+inf` and switching was permanently disabled. | `_rescan_with_measured_friction` ranks candidates, then measures the top candidate's friction = half-spread at the touch `(ask−mid)/mid` + its own taker fee at that price from `schedule_for_market(...).rate`, before the hurdle is applied. A missing book/two-sided touch/Gamma row/schedule keeps the refusal and logs which input is missing. |
| E21 | P1 | `live/engine/quotes.py:51-61` | When the mid was missing the quote was risk-checked against its own **limit price** (`mid_price=… else price`), i.e. a fabricated mid, and the reward score/distance then read as if the book were at that price. | Quote refused (and any resting order for that token cancelled) with a warning; `mid_price=token_mid`. |
| E22 | P1 | `live/yield_accounting.py:177-200,274-320` | The switching cost priced migration downtime at a literal `4.5 s` and the hurdle amortised over a literal `4 h`. | `transit_latency_seconds` now resolves from the measured CLOB round trip (the same probe venue actions pay) and an unmeasured flight makes the cost infinite; the amortisation horizon is derived from the engine's own reprice cadence (`min_reprice_interval_s`), and an unset cadence fails closed with an explicit `HurdleDecision`. |

### Second pass — followed up on the deferred list

| id | sev | file:line | defect | fix |
|---|---|---|---|---|
| E23 | P1 | `feed/models.py:60-100,180-200`, `feed/book.py:113-135` | An empty or one-sided book published `mid = 0.50` and `spread = 0.0` — a valid-looking price and a maximum reward score on a book with no quotes. | `mid`/`spread`/`spread_cents` are `None` unless both sides exist; `to_dict` emits `null`. The quote path already refuses a missing mid (E21), so nothing prices the fabricated value. |
| E24 | P1 | `clob/{orders,batch}.py`, `live/{runner_market,supervisor/startup}.py`, `engine/{trading,quotes,deskew}.py`, `clob/reads.py` | No venue minimum-order check: a sub-minimum order was signed and submitted, and the venue rejected it after a full round trip. | The market's published `orderMinSize` (USDC notional) is resolved at market resolution (Gamma, else the CLOB market payload), required (refusal if neither publishes one), stored on the engine, and checked in `_prepare_order`/`place_orders` before signing. |
| E25 | P1 | `clob/reads.py:get_taker_delay_s`, `engine/deskew.py:_taker_delay_s` | A marketable taker cross paid only the CLOB round trip; the venue's published taker delay (a held, non-cancellable order — 250 ms on flagged crypto/finance up-down markets per `site/concepts/order-lifecycle.md`) was not modelled. | `GET /clob-markets/{cid}` → `itode` + `trading.takerDelayMs` (SDK field; `secondsDelay` deprecated). The delay is added to the flight before the landing book is re-read. A market that applies a hold but publishes no duration **refuses** the cross. No delay constant is hard-coded. |
| E26 | P1 | `engine/accounting.py:has_fresh_book`, `engine/trading.py` | `book_stale_seconds = 30.0` gated every placement and mark while the reprice gate used 2 s. | Explicit value wins; otherwise the bound is the engine's own reprice cadence (`min_reprice_interval_s`), because a decision taken at that cadence cannot price an older book. |
| E27 | P1 | `engine/trading.py:127` | `max_quote_size_shares = max(5.0, init_cash)` — a dollar bankroll read as a share count. | The cap is the sizer's measured quote size; failing that, the derived position cost cap. No unit mismatch, no invented share count. |
| E28 | P1 | `live/quoting.py:417` | `skew_adj = net_q * 0.0005` — an unfitted inventory-skew coefficient. | `net_q × 1/(size resting at the touch on the flattening side)`, i.e. the measured price impact of one share (`engine/accounting.py:_inventory_impact_per_share`). No measured depth → no skew, and it says so. |
| E29 | P2 | `engine/quotes.py` locked-cash calibration | The accounting mode was decided by `decrease > 0.5 × reserve`, a hand-set fraction that could latch the wrong mode and double-credit cash. | Compare the observed decrease to the reserve actually placed: ≥ 99 % → excludes, ≤ 1 % → includes, anything in between is inconclusive and leaves the mode uncalibrated with a warning. |
| E30 | P1 | `onchain/ctf/fees.py:_eip1559_fee_params_wei`, `ctf/constants.py` | Hard-coded 25–50 Gwei tip clamps, a 25–1000 Gwei max-fee band and a `oracle × 0.7` base-fee guess overrode the measured oracle in both directions. | Both fields come from the chain: tip = measured oracle − the block's `baseFeePerGas`, max fee = max(oracle, EIP-1559's own next-block base projection + tip). An unreadable block/base fee refuses to sign (`GasUnmeasured`) instead of substituting a guess. The four Wei clamps are deleted. |
| E31 | P1 | `execution/self_trade.py:327-382, fills.py` | The complementary-binary wash check covered BUY only (a taker SELL + resting SELL below $1.00 self-merges unchecked); a partially filled maker kept its full registered size forever, blocking legitimate re-quotes. | Symmetric SELL branch added (sum < $1.00); `update_maker_order` is now called with the remaining size on every maker fill. |
| E32 | P2 | `engine/quotes.py:104` | The "unchanged price/size → keep the order" fast path ignored `status == CANCELLING`, so a quote whose cancel was never confirmed was treated as resting and never retried. | A `CANCELLING` quote retries the cancel and sends no replacement until the venue confirms it. |
| E33 | P2 | `engine/{ledger,quotes,orders}.py` | `active_quotes` was append-only in SQLite: every placement inserted `LIVE` and nothing ever closed the row, so the table asserted that every order ever placed still rests. | `mark_quote_row()` closes the newest row on a confirmed cancel (`CANCELLED`) and on terminal reconciliation (`FILLED`/`CLOSED`). |
| E34 | P2 | `execution/{fast_cancel,hft_pool,presigned_cache}.py` + flags/tests | Three dormant modules whose documented contract was false (the fast-cancel HMAC signed a different body than it sent → every cancel would 401; the HFT pool re-used one auth timestamp across `POST /order` retries; the presigned cache ignored `neg_risk`/fee/post-only and never evicted). No caller in `src/` enabled any of them. | **Deleted** (modules, `test_hft_execution.py`, `test_fast_cancel.py`, the `use_*` flags and the `presigned_cache` plumbing in `client`/`signing`/`batch`/`cancel`/`dispatch`/`http`). `execution/` now exports only `SelfTradeGuard`. |

**Nothing in this second pass is blocked on a missing measurement**: every fabricated value was replaced by a measured/derived one or by a refusal.

## Found, not fixed (deferred — each needs a design change beyond this pass)

| id | sev | file:line | defect | why deferred |
|---|---|---|---|---|
| D1 | P1 | `execution/fast_cancel.py:253`, `hft_pool.py:262` | The fast-cancel path signs `json.dumps({"orderID": …})` (with a space) but sends compact bytes, so every fast cancel would 401; `errors="replace"` rewrites non-ASCII ids. Both pools are dormant (`use_fast_cancel_pool`/`use_hft_pool` default False, no caller in `src/`). | Module is unreachable; the correct action is delete (fix plan item A4) — a code-removal decision, not a line fix. |
| D2 | P1 | `execution/hft_pool.py:243-249` | Auth headers (and `POLY_TIMESTAMP`) are built once and reused across `max_retries` for a non-idempotent `POST /order`, so a retry can double-submit or carry a stale signature. | Dormant module; retry policy must be decided with D1. |
| D3 | P2 | `execution/presigned_cache.py:173,174,373,423` | `neg_risk` is accepted and ignored (always signs the default exchange domain); cached V1 orders bake `feeRateBps=0`; the cache key omits post-only/expiry/order-type/fee, so a cached payload can be served to a semantically different request; the advertised 60 s eviction has no callers. | Never constructed in `src/` (dead). Delete or rework as one unit. |
| D4 | P2 | `execution/self_trade.py:327-357,211-235` | The complementary-binary check only covers BUY (a SELL+SELL pair below $1.00 self-merges unchecked); `update_maker_order` has no callers, so a partially filled maker keeps its full registered size and blocks legitimate re-quotes. | Live module, but the fix changes wash-trade policy (must decide per-venue rule) and needs its own tests. |
| D5 | P2 | `engine/quotes.py:104-108` | The "price and size unchanged → keep the order" fast path ignores `status == CANCELLING`, so a quote whose cancel was never confirmed is retained as resting, reserves cash, and is never re-placed or re-cancelled at that price. | A status-aware fast path changes the re-quote loop's hysteresis; needs a test net around the CANCELLING state. |
| D6 | P2 | `engine/quotes.py:256-279` | `active_quotes` is append-only in SQLite (one INSERT, no UPDATE/DELETE, no row for the deskew taker), so the table asserts every quote ever placed is still resting. | Analytics consumers not audited in scope; a schema/upsert change is a separate pass. |
| D7 | P2 | `feed/models.py:84,166-175`, `feed/book.py:113-135` | An empty/one-sided maintained book publishes `mid = 0.50` and, in `to_snapshot`, `spread = 0.0` with `mid = best_bid`, which makes the reward scorer's `alpha` equal 1 on a book that does not exist. | `OrderBookSnapshot.mid` is a non-optional field; only `tools/crypto_updown_books.py` reads it, so the type change (and the spread semantic) is a separate, test-covered refactor. |
| D8 | P2 | `engine/quotes.py:417` | `skew_adj = net_q * 0.0005` — an inventory skew multiplier with no measurement or fitting. | Removing it changes the quoted spread; the replacement has to come from a fitted param (as sizing did), not from another constant. |
| D8b | P2 | `engine/quoting.py` sizing | Not touched in this sweep (strategy-side fit is the owner). | — |
| D9 | P2 | `engine/quotes.py:167-186` | Locked-cash calibration decides "the endpoint includes locked collateral" from `decrease > 0.5 × notional`, a hand-set 50 % threshold on the cash path (misfire double-credits cash). | Needs a deterministic probe (two reads around a known order) rather than a fraction. |
| D10 | P2 | `live/yield_accounting.py:180,261` | `transit_latency_seconds = 4.5` and `holding_horizon_hours = 4.0` are fabricated inputs to the switching hurdle; a real measured CLOB RTT exists and is unused here. | Feeds the allocator/migration decision; wiring measured RTT + a caller-supplied horizon touches `reward/` too. |
| D11 | P2 | `engine/trading.py:90,127` | `book_stale_seconds = 30.0` gates every placement and mark while the quoting gate uses 2 s; `max_quote_size_shares = max(5.0, init_cash)` caps a *share* count with a *dollar* bankroll. | The unit mismatch changes sizing; both need a measured staleness bound and a notional-based cap, with the sizer. |
| D12 | P2 | `strategy/venue/reads.py:261` (`BookFeed(max_age=120.0)`) | Books up to two minutes old are served as current, with a failed `/books` chunk simply absent (indistinguishable from "no market"). | `BookFeed` is consumed by `paper/engine.py` (outside this sweep's scope). |
| D13 | P3 | `strategy/onchain/ctf/constants.py:31-36` | Settlement gas floors/ceilings are hard-coded (25–50 Gwei priority, 250–1000 Gwei max fee) rather than derived from the measured oracle (window data: 26–46 Gwei). | Deliberate over-pricing policy documented in the file; changing it is a policy decision. |
| D14 | P3 | `live/runner_market.py:546-554` | An absent `fee_rate_bps` becomes 0 in the engine's order payload (V1-only impact; V2 struct has no fee field). | Cosmetic on V2 (production is v2); the fee *model* already uses the market schedule. |
| D15 | P3 | `venue/clob/orders.py:124-134` | No venue minimum-order check: `orderMinSize` (5 USDC notional) is read by `parse_market` but never consulted before signing; the only gate is "> 0 after 2 dp". | Needs `min_order_size` threaded from the market into `place_order`; engine quotes are sized by `rewards_min_size` (≥ 50), so only taker/deskew legs are exposed. |
| D16 | P3 | `live/engine/deskew.py:178-186` | A marketable taker cross pays the measured CLOB RTT but not the venue's taker delay (docs: a marketable order is held, uncancellable, before matching); `place_rtt_ms` therefore absorbs the hold only after the fact. | Documented `itode` fetch per condition is needed to price the hold; no measured sample exists yet. |

## Tests run

```
uv run ruff check src/polymarketbot/{live,execution,strategy/venue,strategy/onchain}   → clean
uv run pytest -q -x tests/test_clob_truth.py                  → 42 passed
uv run pytest -q -x tests/test_clob_v2_orders.py              → 15 passed
uv run pytest -q -x tests/test_feed_book_integrity.py         → 13 passed
uv run pytest -q -x tests/test_real_money_engine.py           → 35 passed
uv run pytest -q -x tests/test_deskew.py tests/test_live_accounting.py tests/test_live_risk_wiring.py → 54 passed
uv run pytest -q -x tests/test_chain_settlement.py tests/test_settlement_routing.py → 81 passed
uv run pytest -q -x tests/test_live_runner_gating.py tests/test_hft_execution.py tests/test_session_recorder.py tests/test_fast_cancel.py tests/test_reward_reconciler.py → 69 passed
uv run pytest -q -x tests/test_market_switching.py tests/test_live_runner_gating.py tests/test_real_money_engine.py → 64 passed
uv run pytest -q -x tests/test_live_accounting.py tests/test_deskew.py → 49 passed
```

Three tests were rewritten because they pinned a defect:
`test_open_orders_stops_at_max_pages` → `test_open_orders_refuses_a_truncated_list`,
`test_get_market_defaults_missing_fields` → `test_get_market_refuses_missing_parameters`,
and the V1 signing tests now pass `fee_rate_bps=0` explicitly instead of relying on
the silent default. `test_real_money_engine`'s dry-run merge now uses a real
32-byte condition id instead of `0xTEST_CONDITION_ID`.

## Files changed

`live/engine/{accounting,deskew,fills,ledger,merge,onchain,orders,preflight,quotes,trading}.py`,
`live/{base,quoting,yield_accounting}.py`,
`live/supervisor/{sizing,switch}.py`,
`strategy/venue/clob/{__init__,batch,cancel,common,dispatch,http,orders,reads,signing}.py`,
`strategy/venue/feed/{book,client,connection,decode,rest}.py`,
`strategy/venue/reads.py`,
`strategy/onchain/ctf/{balances,calldata,merge,redeem}.py`,
`tests/{test_clob_truth,test_clob_v2_orders,test_real_money_engine}.py`.
