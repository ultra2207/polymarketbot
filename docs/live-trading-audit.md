# Live real-money trader — audit register

Scope: `polymarketbot.trading.runner`, `src/polymarketbot/trading/*`,
`risk/*`, `execution/fast_cancel.py`, `simulator/feed.py`,
`rewards/scanner.py`, and the shared strategy base used by the paper engines.

Baseline at audit time: `pytest -q` → 451 passed. The suite encodes several of
the defects below (dry-run merges as success, `/orders` endpoint, mocked
balances), so a green suite is **not** evidence that live trading is safe.

Severity is about real money: **C** = loses money / corrupts accounting /
cannot trade live at all; **S** = likely to degrade PnL or stop the bot;
**M** = correctness/telemetry hygiene.

---

## C — Critical

| ID | Issue | Evidence |
|---|---|---|
| C1 | **Live CTF merge/redeem is a no-op that reports success.** `merge_positions` logs "Ready to broadcast", returns `success=True`, a random `0x…` tx hash and `usdc_unlocked = amount_shares`. The engine then subtracts inventory and **adds cash that was never received**. Same in `redeem_positions`. | `trading/ctf.py:472-505`, `:597-625`; engine credits at `trading/engine.py:638-643` |
| C2 | **Any open-order API failure books every resting order as fully filled.** `get_open_orders` returns `[]` on non-200/exception; `sync_live_orders_and_fills` treats "absent from the list" as "fully filled". One 429/timeout creates phantom inventory and cash. Also when an order really is cancelled by us, the final matched delta is never read. | `trading/client.py:564-581`; `trading/engine.py:846-870` |
| C3 | **The order list is read from the wrong endpoint and parsed with the wrong shape.** CLOB exposes `GET /data/orders` returning `{data:[…], next_cursor}`. Code calls `/orders` and looks for `orders`. | `trading/client.py:569-577`; docs: `docs.polymarket.com/api-reference/trade/get-user-orders` |
| C4 | **Balance/allowance can never be read.** `/balance-allowance` requires `asset_type=COLLATERAL&signature_type=N`; without them it returns 0. `/data/balances` and `/data/allowances` are not CLOB endpoints. Result: preflight sees $0, flips the engine to dry-run, live trading can never start. | `trading/client.py:583-628` |
| C5 | **`sync_live_balances` re-applies `budget_pct` to the live wallet on every 5 s sync and overwrites `cash`.** With `budget_pct < 1`, cash decays on every sync; PnL is computed against the wrong base; the "isolated" cash is double-counted. It also throws away the fill-updated cash. | `trading/engine.py:344-352` |
| C6 | **Resting BUY orders lock collateral that is not counted anywhere.** `cash` becomes the free balance; equity therefore drops by the full open-order notional, which can trip the $10/$30 drawdown breaker immediately after startup. | `trading/engine.py:408-414`, `:326-354` |
| C7 | **No cost basis, no realized PnL, no fees, shorts clamped to zero.** `_apply_fill` moves cash and shares only; `avg_cost_yes/no` and `realized_pnl` stay 0 forever. SELL beyond inventory silently clamps, hiding a short/lost tokens. | `trading/engine.py:753-776` |
| C8 | **Modelled rewards are credited to real cash and to the risk PnL.** The live engine's `credit_reward` adds the modelled epoch payout to `cash`; `process_reward_epoch` runs every 60 s and feeds `total_rewards_credited` into `realized_pnl`, which **masks trading losses and defeats the daily-loss breaker**. | `trading/engine.py:422-423`; `trading/base.py:840-846`, `:877-883`; runner `:705-707` |
| C9 | **Tier-2 taker de-skew books a fill on order placement.** `place_order` returns "accepted", not "filled"; the engine still records a fill at the target price, moves inventory, and can then merge sets it does not own. | `trading/engine.py:555-592` |
| C10 | **No startup reconciliation.** After a crash/restart the engine starts with zero inventory and no open orders; live orders rest untracked and are duplicated, while pre-existing CTF tokens are invisible to merges/sells. | `trading/engine.py:217-244` |
| C11 | **`cancel_all_quotes` clears local state before/regardless of the cancel result.** A failed `DELETE` leaves live orders that the engine believes are gone. | `trading/engine.py:532-539`; base `:1041-1046` |
| C12 | **Order signing identity is wrong for proxy wallets.** `maker` and `signer` are both set to `POLY_ADDRESS`; if that is a proxy, the EIP-712 signature (from the EOA) cannot verify. `signatureType` is hard-coded 0, `owner` is set to an address (the CLOB expects the API key), `feeRateBps` is hard-coded 0, `verifyingContract` is always the non-neg-risk exchange, and non-numeric token IDs are silently hashed into a garbage token. | `trading/client.py:351-401`; `risk/fast_signer.py:35-36` |
| C13 | **No tick-size or min-size normalisation.** Prices are rounded to 3 dp regardless of the market tick (`0.01` tick markets reject 0.413); sizes can drop below `rewards_min_size`, which earns zero rewards while still risking fills. | `trading/base.py:713-715`, `:726`; `trading/engine.py:436` |
| C14 | **No explicit live gate.** With credentials present, `polymarketbot.trading.runner` places real orders on first run; there is no `--live` acknowledgement. Conversely, preflight silently downgrades to dry-run, which hides fatal configuration errors. | `polymarketbot.trading.runner:503-504`, `:632-639` |
| C15 | **Every order is signed for a retired protocol.** `https://clob.polymarket.com/version` returns `{"version": 2}` (checked 2026-11-03). The client signs the **V1** order struct against the **V1** exchange `0x4bFb41d5…` / `0xC5d563…`. Production is CTF Exchange V2 (`0xE111180000d2663C0091e4f400237545B87B996B`, neg-risk `0xe2222d279d744050d28e00520010520000310F59`). V1 was retired 2026-04-28. **No order the bot produces is valid on the live venue.** | `risk/fast_signer.py:25-36`; `docs.polymarket.com/resources/contracts`; `GET /version` |
| C16 | **The V2 signed object is different**: `Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)`; domain name `Polymarket CTF Exchange`, version `"2"`; `timestamp` in ms; `side` is an int in the hash and a string in the payload; payload carries `expiration`, `signatureType`, `signature`, plus top-level `owner` (API key), `orderType`, `postOnly`, `deferExec`. There is no `taker`, `nonce` or `feeRateBps` in the V2 order. | `ctf-exchange-v2/src/exchange/libraries/Structs.sol`; `py-clob-client-v2/order_utils/…/order_data_v2.py` |
| C17 | **The trading balance is read from the wrong account.** CLOB collateral is what the venue will spend; the bot reads on-chain USDC.e and takes `max(clob, onchain)`. V2 adds pUSD/adapters, so the on-chain balance is not the tradable balance. | `polymarketbot.trading.runner:516-533`; `trading/engine.py:339-350` |
| C18 | **V2 order timestamps are not clock-checked.** V2 signs a millisecond `timestamp` that the exchange validates against server time. There is no `GET /time` offset and no skew guard; a drifting clock rejects every order. | `risk/fast_signer.py` (V1 has no timestamp field); `GET /time` |
| C19 | **Adaptive/defer execution is not requested.** The V2 payload supports `postOnly` and `deferExec`; the bot sends neither, so a "maker" quote can cross and take (fees + adverse selection) and settlement is synchronous. | `ctf-exchange-v2` order payload |

## S — Serious

| ID | Issue | Evidence |
|---|---|---|
| S1 | **Cancel/replace on every book tick** (>1e-4 price change), so queue priority — the actual source of LP income — is destroyed and the CLOB rate limit is hit. | `trading/engine.py:437-451`; `trading/base.py:522-595` |
| S2 | **All engine work runs inside WebSocket listener callbacks on the event loop**: blocking REST (5 s timeout), SQLite, RPC. The feed stalls, misses pings and disconnects. | `simulator/feed.py:462-482`; runner `:642-655` |
| S3 | **One normal 2 ¢ trade permanently halts the engine.** `on_trade_update` feeds the *last trade price* into the jump monitor; a trip sets `is_halted` with no auto-resume. The bot keeps running with no quotes. | `trading/engine.py:896-912`; `risk/manager.py:322-345` |
| S4 | **Equity is marked with fictional prices.** Missing book → YES and NO both default to 0.49/0.51; stale books are never invalidated; own quotes move the mid. Risk breakers and PnL run on this. | `trading/engine.py:416-420`; base `:411-416` |
| S5 | **Reward score model is Q1+Q2 instead of Q_min** (the formula already exists in `rewards/scoring.py` and is used by the backtester). Overstates LP income by up to 2–3×, and `get_current_daily_yield` multiplies by 2 again. | `trading/base.py:822`, `:952-970`; `simulator/engine.py:1546` |
| S6 | **Every public trade triggers a REST open-order sync** — a rate-limit storm during volatility. | `trading/engine.py:914-920` |
| S7 | **Competing L2 score is frozen at startup**; the epoch model keeps using the initial depth. | runner `:299-307`; engine `:824-827` |
| S8 | **On-chain merge from an EOA is attempted without verifying the token balances or the account type**, fabricated gas accounting (`total_gas_fees` uses an estimate, not the receipt), and no gas/nonce management. | `trading/ctf.py:428-505`; engine `:633-667` |
| S9 | **No market lifecycle handling.** Resolution/close/`accepting_orders=false` are never checked; the bot keeps quoting a dead market and never redeems resolved positions. | runner loop `:689-728` |
| S10 | **`price_change` WS messages are collapsed into a fake 1-level book with hard-coded size 100**, and per-change fields are read from the wrong nesting level, so live depth/OFI is wrong. | `simulator/feed.py:570-586` |
| S11 | **Dead advertised features.** `--scan-interval`, `--holding-horizon-hours`, `--disable-switching`, and the whole migration panel are parsed/rendered but never executed; `evaluate_migration_hurdle` is never called. | runner `:172-190`, `:449-475` |
| S12 | **Shared mutable clients**: one `requests.Session` and one SQLite connection used from the main loop and the WS callback with no lock; SQLite cursor interleaving can double-apply a fill. | `trading/client.py:302`, `trading/base.py:205`, engine `:778+` |
| S13 | **`sync_live_balances` calls `sync_rewards` every 5 s**, which issues one `/order-scoring` request per resting quote. | engine `:359`, `:379-386` |
| S14 | **Risk limits are never applied in the live path.** `validate_quote`, `record_fill`, `check_heartbeat`, `can_merge`, and `evaluate_synthetic_hedge` are used by the paper engines only. No position, notional or cash cap is enforced live. | `risk/manager.py:421` (callers: `paper_trading/engine.py` only) |
| S15 | **No disconnect kill.** Heartbeat is never checked in the runner; quotes rest through a feed outage. | `risk/manager.py:368`; runner loop |
| S16 | **Market auto-selection can hand back fake token IDs** (`{cid}_YES`) when the market is missing, and does not check `neg_risk`, `accepting_orders`, tick size, or `fee_rate_bps`. | runner `:283-284`; `rewards/scanner.py:566-636` |
| S17 | **`salt` is `ms_timestamp + pid%1000`** — two orders in the same millisecond collide and the second is rejected as a duplicate. | `trading/client.py:380` |
| S18 | **Preflight can print the private key.** `addr = clob_client.address or ctf_client.private_key`. | `trading/engine.py:261`, `:314` |
| S19 | **Merges fire on every fill** because the threshold collapses to 1.0 share on cheap gas; each merge is a synchronous RPC call inside the fill path. | `trading/engine.py:202-208`, `:774-776` |
| S20 | **The paper engines credit the same modelled rewards and use Q1+Q2**, so the "paper is profitable" evidence is inflated by the same factor as S5. | `simulator/engine.py:1546-1580` |
| S21 | **De-skew uses a constant taker fee** instead of the market's fee schedule, so the synthetic-merge vs direct-cross comparison can choose the wrong exit. | `rewards/deskew.py` (`self.taker_fee`), `trading/engine.py:541-592` |
| S22 | **Aged inventory re-fires the tier-2 taker on every book tick.** Once `holding_time >= 300 s`, `evaluate_skew` returns `SYNTHETIC_MERGE`/`DIRECT_CROSS` every call; the old engine only stopped because it booked a fake fill. With confirmed fills this would stack taker orders. | `rewards/deskew.py:380-400`; `trading/engine.py:541-592` |
| S23 | **Dry-run fills repeat.** A public print at/through a mock quote books a fill on every trade and never reduces the quote, so one quote fills repeatedly. | `trading/engine.py:922-951` |
| S24 | **The feed's legacy event queue is unbounded** and never drained by the runner, so a multi-day live run accumulates every book and trade in RAM. | `simulator/feed.py` `_event_queue` |
| S25 | **Tier-1 telemetry names a SELL while the institutional executor bids the other leg to complete sets.** The action is coherent, but the recorded `deskew_events` row says the opposite of what was done. | `rewards/deskew.py:443-470`; `trading/base.py:738-770` |
| S26 | **Reward telemetry parses the wrong shapes.** `GET /rewards/user/total` requires `date` and returns a JSON *array*; `GET /rewards/user` returns `{next_cursor, data:[...]}`. The client sends no `date` and expects `total_rewards`/`daily_rewards` objects, so it either 400s into the fallback or reports nothing — the operator cannot verify earnings. Confirmed against the published OpenAPI. | `trading/client.py:1665-1860` |
| S27 | **Neg-risk markets cannot be settled by the current CTF client.** It implements only vanilla `mergePositions`; neg-risk positions need `NegRiskAdapter.convertPositions`. The scanner does not exclude neg-risk markets, so a live run on one reads 0 on-chain inventory and every merge fails, stranding capital against the risk caps. | `trading/ctf.py:1-80`; `rewards/scanner.py`; `polymarketbot.trading.runner` |
| S28 | **The $1/day floor is applied to the market pool, not the maker's earnings.** Polymarket pays a maker only when that maker earned ≥ $1 **in that market on that day**; `simulate` credits any modelled share whenever the pool ≥ $1, overstating small-bankroll rewards. The polysim DB's daily rollup applies it correctly (per-maker). | `simulator/engine.py:1580-1600`; `paper_trading/polysim_db.py:217-240` |

## M — Hygiene

| ID | Issue | Evidence |
|---|---|---|
| M1 | `get_open_orders(market=…)` ignores `market`. | client `:564` |
| M2 | Daily loss window is never reset at UTC midnight; `day_start_equity` is process-start equity forever. | manager `:566` |
| M3 | `get_balance` guesses raw units with `> 100_000 → /1e6`. | client `:599-602` |
| M4 | `get_daily_rewards` reads one page of Data API activity and calls fields `usdc_size`; undercounts. | client `:783-804` |
| M5 | `epoch_index` counts process minutes, not wall-clock minutes; `proj_daily = total/epoch*1440` extrapolates partial days. | base `:818-834` |
| M6 | `trigger_ctf_split` mutates balances without any on-chain or CLOB operation. | engine `:671-689` |
| M7 | `total_rewards_credited` and `actual_rewards_usd` are both shown as "rewards"; no distinction between modelled and settled. | telemetry `:1102-1108` |
| M8 | `_canceled_order_ids` grows without bound and is not consulted after restart. | engine `:240` |
| M9 | `.env` uses `POLYMARKET_*` while the client also reads `POLY_*`; there is no `SIGNATURE_TYPE` variable and no proxy-wallet documentation. | `.env.example`, `client.py:67-124` |
| M10 | `estimate_merge_cost_usd` default 78 000 gas is fine, but `POL` price falls back to 0.092 with no provenance in telemetry. | ctf `:141-190` |
| M11 | Shutdown cancels quotes but leaves inventory open with no explicit warning or optional liquidation. | runner `:677-728` |
| M12 | `preflight_check` mutates `dry_run` (a second silent downgrade path). | engine `:304-308` |

## A — Architecture / evidence

| ID | Finding | Evidence |
|---|---|---|
| A1 | **The profitability evidence comes from a different engine than the one that will trade.** `scripts/run_native_paper_trader.py` and `run_multi_tier_paper_trader.py` instantiate `simulator/engine.py:NativePaperTradingEngine` (own quote logic, FIFO, queue model, fees, liquidation). The live runner instantiates `trading/engine.py:RealMoneyTradingEngine`, which inherits only `trading/base.py` for quoting/deskew. Paper PnL is not evidence for the live code path. | `scripts/run_native_paper_trader.py:31`; `run_real_money_trader.py:34` |
| A2 | **The live engine has never been dry-run.** `data/` contains only `native_paper_trading*.db`; there is no `real_money_trading.db` / ledger, so the live engine's dry-run has produced no observable result. | `ls data/*.db` |
| A3 | **Governor unit bug and feedback loop.** `MAINTAIN` fallback uses a dollar allocation as a share quantity (`target_quote_size=current_allocations.get(cid, bankroll*0.75)`), and the live `process_reward_epoch` re-solves OR-Tools every 60 s against `cash` (which excludes locked orders and inventory), so quote size can oscillate and trigger replace storms. | `rewards/allocator.py:717-727`; `trading/base.py:898-910` |
| A4 | **The de-skew horizon is hard-coded to 7 days** (`time_to_resolution_days=7.0`) regardless of the market's real end date, which changes A-S variance and urgency. | `trading/base.py:572` |
| A5 | **No fill/queue model in the live dry-run.** Mock orders never fill unless a public print crosses them, and no queue priority is modelled, so a live dry-run understates fills and cannot be compared with the native paper engine. | `trading/engine.py:922-951` |
| A6 | **Measured: the paper "profit" is 100 % modelled reward credit with ~no fills.** Across the 110 `data/native_paper*.db` runs: 1 429 reward epochs, **20 fills total**, 98 runs with **zero** fills. The best run (`native_paper_trading_10000_0x40107a.db`) earned **$1.2273 in 695 s with 0 fills**, and its own model projects **$203/day** on a $100 bankroll. There is no adverse selection, no inventory loss and no fee in that number. | `data/native_paper*.db` |
| A7 | **The paper fill model is effectively dead.** The matcher needs a queue-consuming print at the exact resting price level; 20 fills over ~24 h of aggregate runtime means the simulator never exercises the cost side of the strategy. Any "paper confirms profitable" claim is a claim about the reward formula, not about trading. | `simulator/matcher.py`, `simulator/engine.py:489-560`; measured fill counts above |
| A8 | **`get_logger` hijacks global logging.** Every call does `logger.remove()` and installs one stderr + one file sink, so whichever module calls it last owns the log file for **all** modules. A live failure can be written to the wrong `data/logs/*.log`, which defeats the repo's "read the log, do not rerun" rule. | `logs.py:35-52` |
| A9 | **The docs contradict the strategy.** `docs/limitations.md` §1 concludes that market making is "ruled out" by unobservable queue position, and `roadmap.md` says "No live trading until Phase 8 gate passes". The live runner is a market maker and is being funded. Either the conclusion or the plan must change, explicitly. | `docs/limitations.md`, `roadmap.md` header |
| A10 | **Three independent `.env` parsers** with different semantics (`BotConfig`, `ClobAuthCredentials.from_env`, `chain._env_file`), so a key can be visible to one component and not another. | `config.py:94`, `trading/client.py:67`, `chain.py:60` |

---

## Fix order

1. Truth layer: CLOB endpoints, balances, order/fill reconciliation, signing identity (C2–C4, C12, S17, M1, M3).
2. On-chain settlement or explicit refusal (C1, S8).
3. Live accounting: cash vs locked vs inventory, cost basis, rewards not credited (C5–C8, S4, S5).
4. Execution safety: startup reconciliation, cancel confirmation, taker fills, merge gating (C9–C11, C13, S19).
5. Risk wiring and breaker semantics (S3, S14, S15, M2).
6. Runner lifecycle: live gate, preflight abort, heartbeat, market resolution, dead flags (C14, S9, S11, S18, M11, M12).
7. Feed integrity and off-loop dispatch (S2, S10, S12).
8. Market selection validation (S16).
9. Paper-engine reward honesty (S20) — changes the profitability baseline and must be reported as such.

---

## Status after remediation (2026-11-04)

**Fixed and verified.** Full suite: 814 passed. Live dry-run smoke (`--dry-run --duration 70`): preflight, baseline, WS-gated quoting, two mock orders, lock accounting, cancellation confirmed, exit 0. Second pass (settlement routing + measurement): live tape smoke recorded 20 rows (18 with non-null mids, real depth and a trade) at a 5 s flush; the two-rate report runs read-only and prints `insufficient data` rather than a fabricated zero when a cell lacks observations.

- Truth layer: V2 order builder with raw EIP-712 recovery, real `/data/orders` + `/data/trades`, cancel verification, decimal token ids, exact base/taker amounts, `POLY_ADDRESS =` signer EOA.
- Accounting: single-valued free/locked/inventory ledger with empirical balance-mode calibration; fills only from venue-reported `size_matched`/terminal status; no inferred fills; merge receipt- and chain-check-driven; live modelled rewards are telemetry only.
- Execution: startup reconcile cancels and proves; preflight aborts live; every maker quote passes `RiskManager.validate_quote`; tier-2 is post-only=False with tick clamp and in-flight guard; dry-run fills decrement `size_matched`; `halt_trading`/`resume_trading` separated from risk breakers.
- Feed: maintained book from `book`/`price_change`, off-loop serialized dispatch, bounded dispatch and event queues, resync after reconnect, quiet-book liveness refresh.
- Runner: typed `START LIVE` confirmation, `--live` required, exit codes 0/1/2/3, WS-gated quoting, periodic re-evaluation on idle books, rate-limited API calls, shutdown retries with surviving-order reporting, neg-risk and proxy-settlement gates.
- Market selection: live-safe validation (numeric tokens, reward config, unexpired), real L2 audit, dynamic fee schedule.
- Paper/reward honesty: official Q1/Q2 → Q_min scoring, per-maker $1/day floor, run-rate projection with accrued-floor display.

**Remaining, explicitly out of scope or gated:**

1. **Settlement routing is implemented, but not funded-verified.** `trading/settlement.py`
   classifies v1 CTF, v1 neg-risk and v2, and builds the verified calldata: Router
   `merge(bytes31,uint256)` / `redeem(...)`, NegRiskAdapter
   `mergePositions(bytes32,uint256)` (pair merge) and `convertPositions(...)`
   (cross-question conversion), and CTF `mergePositions(...)`. The engine dispatches by
   route and `PolygonCtfClient.execute_settlement` broadcasts to an explicit target.
   Before the first live merge the holder must approve the Router on PositionManager
   (v2) or the NegRiskAdapter on CTF (neg-risk); the first call must be a $1
   complete-set test whose receipt transfers are checked.
2. **Proxy-wallet / Deposit-Wallet settlement is still not implemented.** Type-3 order
   signing (POLY_1271), the relayer batch builder, Safe `execTransaction` and the
   legacy proxy relay are implemented and vector-tested, but `is_settlement_enabled`
   still requires an EOA (signature_type 0). A type-3 account can quote with
   `--allow-no-settlement`; merges through an executor are not wired.
3. The scanner's spread-capture estimate is regime-assumption driven; measured paper fills are ~0 on the selected markets. Treat yields as reward-share estimates, not P&L predictions.
4. ~~`docs/limitations.md` and `roadmap.md` still say market making is ruled out / not until Phase 8.~~ **Done 2026-11-04:** both docs now distinguish spread-capture MM (still ruled out) from reward-only quoting (§3.2, Gate 8b), and the roadmap header/Phase 8b/Phase 9 reflect the live runner.
5. RPC endpoint: resolution is `POLYGON_RPC_URL` → `ETH_RPC_URL` → free `polygon.drpc.org` (rate-limited). A keyed endpoint removes the chunk pressure; `ChainClient().health_check()` must report `eth_chainId=0x89` with a head younger than 60s before settlement is trusted. The engine now runs that check in preflight.
6. **v2 / neg-risk inventory reconciliation is partial.** v2 reads the Data API
   `/v2/positions` (live-unverified and network-dependent); v1 neg-risk keeps the
   persisted local inventory because the adapter ERC-1155 balance read is not
   implemented. Neither path has been exercised on a funded wallet. A Gamma outage
   falls back to the CTF path, whose on-chain balance preflight refuses rather than
   merging the wrong ledger.

## Capital-light pilot prerequisites (2026-11-04)

**Alchemy key.** A multi-chain Alchemy app key is valid on
`https://polygon-mainnet.g.alchemy.com/v2/<key>`. `.env` currently holds the key in
`ETH_RPC_URL` (eth-mainnet host). `chain.rpc_url()` resolves `POLYGON_RPC_URL` →
`ETH_RPC_URL` → the free endpoint, and rewrites any Alchemy host to
`polygon-mainnet` with the same key. `health_check()` proved `chain=137` with that
key. Setting `POLYGON_RPC_URL` explicitly is optional clarity, not a requirement.
The safety net is preflight: it now calls `health_check()` (chain id + head age), so
a rotated chain-scoped key or a non-Alchemy Ethereum RPC aborts the live run.

**Approvals precede capital.** These are gas-only transactions; they need POL but no
USDC and can be done before funding:
- v2: the holder calls `PositionManager.setApprovalForAll(ROUTER, true)`.
- v1 neg-risk: the holder calls `CTF.setApprovalForAll(NEGRISK_ADAPTER, true)`.
- v1 binary: none; the holder calls CTF `mergePositions` directly.

**$1 settlement test, before scaling.** Fund POL for gas and ~$5–20 USDC.e; acquire
$1 of YES+NO (buy both sides on the CLOB, or split manually); merge one complete set;
assert the receipt status is 1 and the collateral transfer is in the logs
(`settlement.decode_erc20_transfer_total`). Only then scale. The merge requires a
tiny amount of capital, not the full bankroll.

**Account-type gate.** Settlement is signed by the position holder. An EOA
(`signature_type=0`) can run the sequence today. A type-3 Deposit Wallet can sign
orders (POLY_1271, vector-tested against the SDK) but **cannot settle**: the
relayer/executor batch is implemented as a builder only, and
`is_settlement_enabled` refuses non-EOA accounts. Such an account can quote with
`--allow-no-settlement`; inventory can then only exit by selling.

**Go/no-go:** fit for a small, supervised pilot (EOA, one v1 non-neg-risk market, bounded `--duration`). Not fit for unattended size, and not yet fit for v2 or neg-risk live settlement: the edge is still modelled, not measured, and the non-CTF routes are encoded from verified ABIs but have never carried a real transaction. Acceptance for the pilot: preflight READY; venue reports no open orders at start and end; the first real fill reconciles against `/data/trades`; the `/rewards/user` total at the next UTC midnight matches the modelled direction.

## Production Capital Allocation Gate (Dec 1, 2026 Go-Live)

- **Domain 1 (HFT Passive Quoting + Binance Lead-Lag Alpha):** **ACTIVE** (0% maker fee, low inventory holding risk, calibrated queue priority).
- **Domain 2 (Settlement Discount Harvesting):** **ACTIVE** (100% dynamic maker queue pricing, Option A/B/C regimes, $0 maker fee, emergency circuit breaker).
- **Domain 3 (Negative-Risk Arbitrage & CTF Bundle Converter):** **INACTIVE / $0 ALLOCATED**. Hard-gated at zero capital due to empirical audit showing multi-leg taker fees devour 66.5% of gross edge ($16.35 fees on $8.23 profit), 28.4% broken-basket unwinds, and 14× gas friction.

