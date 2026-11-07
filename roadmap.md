# Polymarket Research → Forward Test → Live

**Status:** Phase 0–2 complete, Phase 3 (labels + trade normalisation) in progress; a
live reward-quoting runner now exists behind explicit gates (pilot only — §3.2)
**Last updated:** 2026-11-04
**Scope:** research and backtesting for directional strategies. Directional live trading
still requires Gate 8. Reward-only liquidity quoting is a separate strategy class with its
own evidence rules and pilot gate (§3.2, §7 Phase 8b).
**Companion docs:** [`plan.md`](plan.md) (research agenda and idea backlog) · [`docs/strategy-reverse-engineering.md`](docs/strategy-reverse-engineering.md) · [`docs/limitations.md`](docs/limitations.md) · [`docs/live-trading-audit.md`](docs/live-trading-audit.md) (live-engine findings and gates)

---

## 0. Executive summary

The goal is right, the sequencing is right, but **the proposed timeline cannot be executed with free data**.

You want: train on 2023–2024 → forward test 2024–2025 with realistic entry *and* exit fills.
Free historical **order book depth** does not exist for 2023, 2024, or 2025. It exists for:

- **2025-10-14 → 2026-03-31** — top-10 levels, event-driven snapshots (`vpcapitano/polymarket-genesis-liquidity`)
- **2026-02-23 → 2026-04-16** — hourly WebSocket tape, but ~50 % market coverage gaps (PMXT v1)
- **2026-04-13 → 2026-08-10** — complete hourly WebSocket tape with full L2 (PMXT v2)

Trade tape, by contrast, is excellent and free from 2022-11-21 to 2026-10-04.

So there are only three honest ways to get "realistic fills on entry and exit" in a 2024–2025 window:

| Option | Cost | Fidelity | Verdict |
|---|---|---|---|
| **A. Move the forward-test window to 2026-04-13 → 2026-08-10** | free | true book-walk fills, real spread, real depth | **recommended** |
| **B. Fit an impact/fill model on 2025-10 → 2026-08, apply backwards to 2023–2025** | free | ±20–60 bps uncertainty, model-dependent | use as *robustness*, not go/no-go |
| ~~C. Buy depth data for 2023–2025~~ | — | — | **off the table — free sources only** |

**Recommendation: A as the primary forward test, B as a robustness check. No paid vendors.** With option C removed there is no fallback: if the fill model cannot be validated (Gate 5), the correct response is to *narrow the claim* to the 2026-04 → 2026-08 window, never to buy data.

This is slightly better for you than the original plan, because 2026 data also contains the current fee regime and the current market mix, so the "fake forward test" is more predictive of live performance than a 2024–2025 test would have been.

Two parallel truths that shape everything below:

1. **Depth is perishable.** You cannot backfill 2026-08-11 onward. Start a recorder now and you manufacture future history at ~zero cost. For the **research** track this is the only live work worth doing before Phase 8. The reward-only quoting pilot (§3.2) is separate: it exists to measure a cost the research stack cannot simulate, and it is capped at minimum size until Gate 8b.
2. **The 2026 regime is a different game.** Taker fees were effectively zero for most markets in 2023–2025 and are now material (crypto peaks ~1.56 % at 50 ¢). A model tuned on 2023–2025 will systematically overstate edge. Fee overlay is mandatory, not optional.

---

## 1. Critique of the current plan

### 1.1 What is right

- **Historical-first, live-later.** Correct. Live testing before a validated simulator burns money and teaches you nothing.
- **Separate train / forward-test periods.** Correct instinct. It just needs to be done by *market*, not only by calendar date (see §1.3).
- **Fill realism as a first-class concern.** Correct, and rare. Most people skip it and then wonder why live PnL ≠ backtest PnL.

### 1.2 What breaks

**B1. No depth data for the proposed test window.**
Covered in §0. You cannot reconstruct the book for 2024–2025 from the trade tape. Any "realistic fill" claim there is a model, not an observation, and must be labelled as such.

**B2. Fee regime discontinuity.**
Fees timeline:

| Date | Change |
|---|---|
| 2023-01 → 2025-12 | essentially no taker fees on the vast majority of markets |
| 2026-01-05 | taker fees on 15-min crypto, peak 1.56 % at 50 ¢ |
| 2026-02-12 | 5-minute crypto markets launch, fees enabled |
| 2026-02-18 | fees on NCAAB + Serie A |
| 2026-03-06 | fees extended to **all** crypto markets (1H/4H/daily/weekly) |
| 2026-03-30 | Fee Structure V2 — rates by category |
| 2026-07-10 | sports taker coefficient 0.03 → 0.05; maker rebate 25 % → 15 % |

A strategy that scalps 1–2 ¢ edges is profitable in 2023–2025 and dead now. **You must re-score every 2023–2025 result under the 2026 fee schedule before believing it.**

**B3. Market-mix discontinuity.**
The 2026 volume mix (5-min crypto up/down, expanded sports, perps) barely existed in 2023–2025. A model trained on election/politics markets is being evaluated on a book of short-horizon crypto markets. Either restrict the universe to the category you intend to trade, or accept that cross-period transfer is weak.

**B4. Universe survivorship.**
If you build the market universe from "markets that exist today", you select on survival and on outcome. You need a **point-in-time universe**: every market where `created_at <= t < end_date` (or `closed = false` at `t`), including the ones that died at 2 ¢ and never traded again.

**B5. Label leakage across the split boundary.**
A market created 2024-06 and resolving 2025-03 spans a naive date split. Features computed after the boundary leak the label. Split by **market**, purge around the boundary, embargo a few days.

**B6. Exit realism is underspecified and it is the harder half.**
On Polymarket you can always hold to resolution — that is a legitimate exit that guarantees a payoff (0 or 1) at a known time. Any exit model must be benchmarked against "do nothing and hold". If your strategy's edge disappears when you forbid early exit, the edge was really "I assumed I could always get out at mid", which is false in thin markets.

**B7. No capacity / impact model.**
Your own order moves the book. In a 2023 market doing $20 k/day, a $5 k order *is* the book. Backtests without endogenous impact produce fantasy PnL at size. Capacity must be an output of the research, not an afterthought.

**B8. No baseline.**
The market price is already a strong forecast. If your model cannot beat "use the market's implied probability" on Brier score, and cannot beat "buy at mid and hold to resolution" on PnL, there is no edge. This baseline is non-negotiable and must be computed before any strategy work.

**B9. Correlated legs.**
Neg-risk events are portfolios of mutually exclusive outcomes. Positions across legs of one event are perfectly anti-correlated. Summing per-market PnL and calling it a return distribution overstates diversification and understates drawdown.

**B10. Resolution-time clustering.**
Elections, sports slates, and monthly expiries resolve in bulk. PnL and liquidity both cluster. A Sharpe computed on daily PnL without accounting for clustered settlement is optimistic.

### 1.3 What to change

| Original | Revised |
|---|---|
| Train 2023–2024, test 2024–2025 | Train 2023-01 → 2026-03; calibrate fills 2025-10 → 2026-05; **test 2026-06 → 2026-08-10** |
| Fill realism everywhere | Fill realism *observed* only in the test window; *modelled* (with error bars) elsewhere |
| One backtest | Three impact multipliers (0.5×, 1×, 2×) + fee overlay; edge must survive 2× and 2026 fees |
| Forward test = PnL | Forward test = PnL + calibration + capacity curve + baseline comparison |
| Live after it works | Live only after Gate 8 (see §7) |

---

## 2. The hard constraint: data availability map

### 2.1 Trade tape (complete, free)

| Dataset | Coverage | Rows | Size | Licence |
|---|---|---|---|---|
| `SII-WANGZJ/Polymarket_data` → `trades.parquet` | 2022-11-21 → 2026-10-04 | 1.267 B fills | 45.8 GB | MIT |
| same → `orderfilled.parquet` (raw, incl. fees + order hash) | same | 2.027 B events | 127 GB | MIT |
| same → `users.parquet` (one row per fill per wallet) | same | 2.027 B | 60.8 GB | MIT |
| `moose-code/polymarket-onchain-v1` | 2020-09 → 2026-04 (incl. AMM era) | 1.17 B fills, 2.74 B records | 127 GB | CC-BY-4.0 |
| `TimeSeventeen/Polymarket-v1` | 2022-11-21 → 2026-04-28 + resolution fields | 1.20 B | ~49 GB | CC-BY-4.0 |

### 2.2 Order book depth (free, limited windows)

| Source | Coverage | Grain | Fidelity | Notes |
|---|---|---|---|---|
| `vpcapitano/polymarket-genesis-liquidity` | 2025-10-14 → 2026-03-31 | event-driven (on book change) | best bid/ask + **top 10 levels/side**, total depth | 339 M rows, 14 GB. Both sides always populated; ~0.019 % crossed books |
| PMXT v1 `r2.pmxt.dev` | 2026-02-23 → 2026-04-16 | hourly files | full L2 deltas + snapshots | **~50 % market coverage gaps** — not trustworthy alone |
| PMXT v2 `r2v2.pmxt.dev` | **2026-04-13T19 → 2026-08-10** | hourly files | full L2: `book`, `price_change`, `last_trade_price`, `tick_size_change` | **measured 384 MB/h average → ~1.1 TB for the full window** (2,861 hours). Verified contiguous. **Primary simulation source — stream it, do not store it raw** |
| `polyorderbooks/polymarket-crypto-5min-l2` | Aug 2026 sample | ~1 s | full bid/ask arrays, crypto 5-min only | 159 k rows, CC-BY-4.0 |
| Dome API | dead 2026-04-28 | — | — | do not build on it |

> **Verified 2026-11-01:** `https://r2v2.pmxt.dev/polymarket_orderbook_2026-08-10T00.parquet` → 200. `2026-08-11T00` → 404. `archive.pmxt.dev` unreachable. Enumerate hours directly against R2; do not scrape the HTML index.

**Measured book depth** (700 sampled snapshots from `vpcapitano/polymarket-genesis-liquidity`, 2025-10 → 2026-03):

| Metric | p10 | median | p90 |
|---|---|---|---|
| Quoted spread | 1.0 ¢ | **2.0 ¢** | 4.0 ¢ |
| Total ask-side depth (USD) | ~$1,500 | **~$23,000** | ~$55,000 |
| Total bid-side depth (USD) | ~$1,600 | ~$18,000 | ~$134,000 |
| Ask price levels | 7 | 32 | 64 |

Caveat: these are *whole-book* totals and include tail orders at 1–5 ¢ that are not usable for sizing. Depth within 1–2 ¢ of the touch — the number that actually governs slippage — is a small fraction of these figures. Treat them as an upper bound on what the market can absorb, not as executable liquidity at the mid.


### 2.3 Metadata, resolution, lifecycle (free)

| Need | Source |
|---|---|
| Market/event metadata, tags, series | Gamma API; `markets.parquet` from SII/TimeSeventeen for history |
| Resolution status + payouts | `/v2/resolutions`, `/v2/tokens` (`resolved`, `final_price`); `TimeSeventeen` `daily_aligned` |
| Splits, merges, redemptions, neg-risk conversions | `moose-code` (`split`, `merge`, `redemption`, `neg_risk_conversion`); SII raw logs |
| Fees paid per fill | `orderfilled.parquet` (`maker_fee`, `taker_fee`, `protocol_fee`) |
| Open interest, holders, PnL | `/v2/oi`, `/v2/holders?include_pnl=true`, `/v2/user-pnl` (current state only) |
| Reference prices for crypto up/down | PolyBolt WS (`price.crypto`, Chainlink-first), Chainlink TWAP 30 s / 60 s |

### 2.4 What this means for the design

```
2024-01 ─────────────── 2025-09 │ 2025-10 ── 2026-03 │ 2026-04 ── 2026-05 │ 2026-06 ── 2026-08-10 │ 2026-08-11 ── 2026-10-04
   tape only, no depth           │  top-10 depth        │  full L2 depth    │  full L2 depth         │  tape only
   ── model development ──       │  ── FILL CALIBRATION │  ── FILL VALIDATION│  ── FORWARD TEST ──    │  ── robustness ──
```

- **Model development:** 2024-01 → 2026-03 (tape + metadata; fills modelled). 2023 is excluded: 3,865 markets and 0.1 % of lifetime volume, so it is noise rather than a period.
- **Fill calibration:** 2025-10-14 → 2026-03-31 (snapshots + tape; fit impact/slippage model)
- **Fill validation:** 2026-04-13 → 2026-05-31 (full L2; check predicted vs observed slippage)
- **Forward test (go/no-go):** 2026-06-01 → 2026-08-10 (full L2; untouched)
- **Robustness:** 2026-08-11 → 2026-10-04 (tape only; coarse fills)

### 2.5 Preflight findings (verified 2026-11-01)

Read from Parquet footers plus the 545 MB markets table only — no bulk download. Reproduce with `uv run scripts/preflight.py`.

**Row counts and schemas confirmed**

| File | Rows | Row groups | Notes |
|---|---|---|---|
| SII `trades.parquet` | 1,266,885,846 | 1,261 | snake_case, as documented |
| SII `markets.parquet` | 4,049,900 | 4 | **no tags/category column** |
| SII `users.parquet` | 2,026,673,731 | 2,005 | |
| SII `quant.parquet` | 1,266,885,845 | 1,261 | |
| SII `orderfilled.parquet` | 2,026,704,633 | 2,005 | fees are `BLOB` (uint256 LE) |
| moose `order_filled` 2026-02 | 218,101,912 | 218 | **camelCase**, single `fee`, not maker/taker/protocol |
| PMXT hour (2026-07-15T12) | 61,797,997 | 59 | `market` is `BLOB(66)`, `bids`/`asks` are JSON **strings** |
| Dome snapshots | 339,183,655 | 2,761 | top-10 levels, both sides |

**Disk budget (measured, not estimated).** The union of every published file is over 1.3 TB, which is far beyond the 360 GB free here. But ~232 GB of that is redundant or derivable:

| File | Size | Why it is not needed locally |
|---|---|---|
| `orderfilled.parquet` | 127.2 GB | raw-event granularity of the same fills as `trades`; query remotely |
| `users.parquet` | 60.8 GB | `orderfilled` re-keyed by wallet; derivable by exploding `trades` |
| `quant.parquet` | 44.7 GB | derived YES-normalised view; trivial to rebuild locally |

The **core set is 56.5 GB** and is what the downloader fetches by default:

| File | Size |
|---|---|
| `sii/trades.parquet` | 42.70 GB |
| `dome/dome_polymarket_2025-10_2026-03.parquet` | 13.26 GB |
| `sii/markets.parquet` | 0.52 GB |

**PMXT is the trap.** The paper's "216 GB" figure was a 30-day slice. The actual full window is ~1.1 TB:

| Window | Hours | Est. size |
|---|---|---|
| Full 2026-04-13 → 2026-08-10 | 2,861 | **~1.1 TB** |
| 2026-06-01 → 2026-08-10 (forward test) | 1,703 | ~710 GB |
| 2026-04-13 → 2026-05-31 | 1,158 | ~400 GB |

At 20 MB/s the core set takes ~45 min. PMXT must be **streamed and reduced** (Phase 4), not bulk-downloaded: read an hour, derive what you need, delete the raw file. A 2 TB external drive is the alternative if you want the raw archive.

**Market composition (SII `markets.parquet`)**

| Created | Markets | Volume | Median lifetime | < 1 day | Zero volume | Void (0.5/0.5) |
|---|---|---|---|---|---|---|
| 2021 | 1,856 | $0.09 B | 1.9 d | 42.8 % | 0.1 % | 0.0 % |
| 2022 | 2,925 | $0.09 B | 1.6 d | 47.6 % | 0.8 % | 0.2 % |
| **2023** | **3,865** | **$0.13 B** | 1.8 d | 32.1 % | 37.9 % | 0.6 % |
| 2024 | 16,192 | $18.99 B | 6.9 d | 17.9 % | 0.9 % | 0.1 % |
| 2025 | 249,754 | $35.71 B | 2.2 d | 29.5 % | 15.0 % | 0.2 % |
| 2026 (to Oct 4) | 3,775,235 | $49.90 B | 3.3 d | 31.0 % | 46.4 % | 4.3 % |

Volume share: 2023 = **0.1 %**, 2024 = 18.1 %, 2025 = 34.0 %, 2026 = 47.5 %. The `updown` family is 19.7 % of all markets and 18.8 % of 2026 volume — and 0 % before 2025.

**Data-quality flags**

- **44.2 % of all markets have zero volume** (46.4 % in 2026). Universe filtering is not optional.
- **161,548 markets (4.0 %) resolved 0.5 / 0.5** — voids, not binary outcomes. A further ~23 % of 2026 markets have non-0/1 final prices (`0.0005`, `0.495`, `0.005`…), so **`outcome_prices` is a price, not a payout numerator**. Do not threshold it and call that the winner.
- **`end_date < created_at`** in 44.6 % of 2022 markets, 12.4 % of 2023, 5.6 % of 2024 — metadata time bugs, must be cleaned.
- `markets.parquet` is **latest metadata only**, not point-in-time: `end_date` can be extended after creation, so using it as a feature leaks.
- Two archives are **not drop-in compatible**: SII is snake_case with `maker_fee`/`taker_fee`/`protocol_fee`; moose is camelCase with one `fee`. Joins need a normalisation layer.
- PMXT `market` arrives as `BLOB(66)` — decode to hex, or every join silently fails.

**What this changes**

1. **Training window.** 2023 carries 0.1 % of volume. "Train 2023–2024" is effectively "train 2024". Use **2024-01 → 2026-03** and stop pretending 2023 is a period.
2. **Horizon.** Median market lifetime is **2–7 days**, and ~30 % of markets live under a day. A "1–14 day hold" is roughly one market lifetime, not a long-horizon strategy. If you want genuinely long holds, you must select the long-lived subset explicitly.
3. **Universe.** With 44 % zero-volume and 4 % voids, the tradable universe is roughly **half** of all markets. Define it in Phase 2 or every downstream count is wrong.
4. **Labels.** Need authoritative payout numerators from `moose-code/condition` (`payoutNumerators`, `payoutDenominator`), not `outcome_prices`. This is now a Phase 3 requirement.

### 2.6 Sources that are required but are not in any trade archive

These are not optional extras. Each one is load-bearing for a specific piece of the simulation, and none of them is in SII, moose-code, or TimeSeventeen.

| Missing source | What it is needed for | Free source | Backfillable? |
|---|---|---|---|
| **Crypto reference prices** | `updown` markets (19.7 % of markets, 18.8 % of 2026 volume) resolve on a Chainlink TWAP, not on Polymarket prices. Without the reference series these markets cannot be modelled at all | Binance public data dumps (klines); Chainlink feeds on Polygon | **Yes**, full history |
| **Wallet identity map** | On-chain `maker`/`taker` may be a signer EOA, a proxy/Safe, or a deposit wallet. Unique-wallet counts, copy-trade baselines and self-trade detection all depend on collapsing them | Data API profiles; on-chain proxy-factory and deposit-wallet-factory events | Partially — creation events are on-chain, profiles are current-only |
| **Fee schedules over time** | You have *realised* fees in `orderfilled`, but not the formula needed to price a hypothetical trade. Fees went from ~0 to material during 2026 | `docs.polymarket.com/trading/fees`, changelog, Gamma `feeSchedule` per market | Partially — historical formula from docs, validated against realised fees |
| **Tags / categories** | Universe filters, fee rates, regime analysis. SII `markets.parquet` has **no tag column** | Gamma `/tags`; historical mapping derived from slug + event_slug | Partially — current tags only, history must be inferred |
| **UMA proposal / dispute logs** | When a market was actually decided, and whether the outcome was contested. Resolution can move during a dispute window | On-chain UMA Optimistic Oracle logs; `/v2/resolutions` going forward | **Yes** via on-chain logs |
| **Sports metadata** | Game start time, market type (moneyline/spread/total), line. Required for any sports strategy | Gamma `/sports` and per-market fields | Partially — current only |
| **Comment stream** | Sentiment / crowd features | Gamma `/comments`; RTDS `comments` topic | **No** — see §2.7 |

Execution checklist for all of the above is **Phase 1b** (§7).

### 2.7 Structurally unobtainable data — stop looking

Four things are commonly assumed to exist and do not. Three are unavailable by architecture, not by oversight; the fourth is unavailable because nobody recorded it. Details and mitigations in [`docs/limitations.md`](docs/limitations.md).

| Data | Why it cannot be obtained | What it breaks | Mitigation |
|---|---|---|---|
| **Individual order placement / cancellation** | Matching is off-chain and operator-private. Only *fills* settle on-chain. An order that rests and is later cancelled never touches the chain, so its hash never appears anywhere public | order-to-trade ratio, cancel/spoof detection, true book composition, maker behaviour | decompose book deltas against the trade tape to bound cancels; treat each price level as anonymous |
| **Queue position at a price level** | Price-time priority exists only inside the operator's matching engine. The public book aggregates size by price, discarding order count and arrival order | passive fill probability, maker adverse selection, any queue-dependent strategy | bracket the fill: front-of-queue vs back-of-queue; prefer taker entries; report both |
| **Historical comments / sentiment** | Stored off-chain with no bulk export; the read endpoint reflects *current* state, so edits and deletions destroy the as-of record | sentiment features for any period before you start recording | record now; substitute GDELT / news / Reddit for history |
| **Order book depth after 2026-08-10** | Nobody has recorded it publicly. This one is not impossible — it is unrecorded | any post-Aug-2026 depth, slippage or queue backtest | start the recorder (§8); forward-only |

**Why this matters for strategy choice.** The first two gaps are fatal to **spread-capture** market making and to any strategy whose edge comes from passive fills. They are close to irrelevant for strategies that enter as a taker and hold to resolution, because those never depend on queue priority and only need one fill price. That is the technical reason §3 recommends starting with fundamental / event-driven rather than market making — not a preference, a data constraint. Reward-only liquidity quoting (§3.2) is the partial exception: its revenue is observable, its fill cost is not, so it is run as a bounded live pilot rather than backtested.

---

## 2.8 Docs-derived corrections (verified 2026-11-01)

The full documentation was mirrored to `data/raw/docs/` (96 pages + `llms-full.txt`). A
distilled reference lives in [`docs/polymarket-mechanics.md`](docs/polymarket-mechanics.md).
Corrections that change the build:

| Assumption | Reality | Consequence |
|---|---|---|
| "V2 cutover migrated markets to a new position system" | Two axes: **exchange generation** (contract that settles) and **position system** (CTF vs PositionManager) | `markets_pit` now carries both; SII is CTF-only |
| Tick size is `0.01` unless known | **165/200 sampled markets use `0.001`** | Gamma enrichment; never default silently |
| Fees come from a category table | Per-market `feeSchedule` + `feeType` (`sports_fees_v2` 0.03 vs `v3` 0.05, `crypto_fees_v2`, `weather_fees`, …) | `scripts/enrich_market_metadata.py` |
| Wallet skill needs the 60 GB `users.parquet` | `/v2/leaderboard`, `/v2/biggest-winners`, `/v2/positions?user=`, `/v2/trades?user=&full_history=true` | Start Phase 6b from ranked boards |
| Resolution timing is unavailable | `/v2/resolutions` gives `resolved_at`/`resolved_block`; UMA timeline is 2 h undisputed, 4–6 d disputed | Time-to-resolution becomes a feature |
| Only PMXT has price history | `/v2/prices-history?interval=max` exists back to 2022 at 12 h grain, but the first point is often later than `created_at` and some old series look inconsistent | Coarse cross-check only; validate per token |
| Holders can give final PnL | `/v2/holders` is **current-state only**; redeemed markets return none | Use closed positions instead |

---

## 3. Decision: what kind of strategy

Fill realism matters in proportion to turnover. Pick the class deliberately; it changes the entire data requirement.

| Strategy class | Holding period | Fill sensitivity | Data requirement | Recommended? |
|---|---|---|---|---|
| **Fundamental / event-driven** — forecast P(outcome) vs price | days → resolution | **Low**. Entry price + resolution dominate; exit optional | tape + metadata + resolution | ✅ **start here** |
| **Cross-market / arbitrage** — neg-risk basket mispricing, cross-venue | minutes → hours | Medium. Must fill 2+ legs simultaneously | tape + top-10 depth | ✅ second |
| **Momentum / short-horizon** | minutes | **High**. Slippage is the strategy | full L2 | ⚠️ only in 2026-04→08 window |
| **Spread-capture / inventory MM** | seconds → minutes | **Extreme**. Queue position, adverse selection | full L2 + order-level (unavailable) | ❌ not with free data |
| **Reward-only liquidity quoting** | hours (resting) | **Revenue low** — price/size/time from the published book; **cost unobservable** — fills/queue | live book + published pool; no backtest | ⚠️ live pilot only (§3.2) |

**Recommendation:** begin with fundamental/event-driven, where the simulator's fill assumptions are least load-bearing, and where the resolution payoff gives you a clean, unambiguous exit. Add arbitrage once the harness is trusted. Do not attempt **spread-capture** market making on free data — you cannot observe queue position.

Reward-only quoting is the exception, and it is a different claim. Its income is the published liquidity-reward formula applied to *our own* resting orders and the opponent's *aggregate* book — both observable. Its cost is the fills those orders receive, which is not observable or simulatable. So it cannot be backtested, but it can be run as a bounded live experiment whose cost side is measured, not assumed. §3.2 defines that gate.

### 3.1 Sizing and horizon decide more than the strategy class

These two numbers are the real forks in the roadmap. Everything downstream — how much data work is justified, how long the honest test window needs to be, whether the free stack is sufficient — is a function of them.

**Target notional per trade** = dollars per single order (not total bankroll). Slippage scales with `size / depth_near_mid`, not with time. Against the measured depth above, the practical bands are:

| Per-order notional | Roughly where in the flow | Usable depth assumption | What it implies |
|---|---|---|---|
| ≤ $100 | up to p95 | always at the touch | tape-only backtests are defensible; book work optional |
| $100 – $500 | p95 – p98 | 1–2 ¢ walk typical | tape + spread model is enough; book replay improves accuracy but is not decisive |
| $500 – $5,000 | p98 – p99.9 | 2–6 ¢ walk typical | book replay required; impact model required; capacity curve becomes a primary output |
| > $5,000 | top 0.1 % | you are a material share of the book | full L2 required; honest testing only possible 2026-04 → 2026-08; expect edge to decay 2–4× vs small size |

(The percentile column comes from the measured distribution below; it is a sanity check on ambition, not a rule.)

**Holding horizon** = time between entry and exit:

| Horizon | Fills are load-bearing on | Fee sensitivity | Data requirement |
|---|---|---|---|
| Hold to resolution | entry only; **exit is free** (payoff is 0 or 1 at a known date) | low (one round trip, long time to earn it) | tape + resolution. **Free stack is comfortable** |
| Days → weeks | entry + exit | medium | tape + top-10 depth + impact model |
| Hours | entry + exit + adverse selection | high | full L2 required |
| Minutes | entry + exit + queue + adverse selection | extreme (1.56 % peak crypto taker fee needs ~3 % of edge per round trip) | full L2 required; only honestly testable 2026-04 → 2026-08 |

**Do not pick a single point — sweep a grid.** Notional and horizon are *evaluation parameters*, not commitments you make before you have data. Every strategy gets run across:

| Axis | Grid |
|---|---|
| Notional | \$50, \$100, \$250, \$500, \$1,000, \$2,500, \$5,000 |
| Horizon | hold-to-resolution, +1 day, +7 days (where the market lives that long) |
| Queue assumption | back / pro-rata / front |

The output is a **capacity curve** (net edge vs notional) plus a horizon table, not a single Sharpe ratio. That is strictly more informative than choosing one pair, and it costs almost nothing extra because the simulator is parameterised anyway.

For early exploratory work, use **\$250 per order and hold-to-resolution** as the default cell. It is a starting point for plots and smoke tests, not a strategic decision.

For fundamental strategies, "hold to resolution" is the default exit and early exit is an optimisation, not a requirement. This is what makes the free-data plan tractable: the exit leg, which is where fill assumptions usually break, disappears entirely.

**Measured fill-size distribution** (`moose-code` archive, 2026-02, 500 k fills, via `scripts/measure_fill_sizes.py`):

| Percentile | USD per fill |
|---|---|
| p10 | 0.32 |
| p25 | 1.11 |
| **p50** | **3.71** |
| p75 | 11.50 |
| p90 | 42.48 |
| p99 | 890 |
| p99.9 | 7,049 |

73 % of fills are ≤ $10, 91 % ≤ $50, 95 % ≤ $100, 98 % ≤ $500, 99.15 % ≤ $1,000.

Implications:

- A `$50` order is around the p91 mark; `$250` is ≈ p97–98; `$1,000` is > p99. Above roughly `$500`/order you are trading in the top 2 % of all flow by size and should expect to be a visible share of the book.
- This counts **fills, not orders**. One taker order can be split across many maker fills, so order sizes are larger than these numbers. Treat the table as a lower bound on order size and an upper bound on how unusual a given notional is.
- The sample is dominated by high-frequency crypto markets, which inflates the count of tiny fills. Politics and long-horizon markets carry larger fills. **Re-measure per category in Phase 3** before finalising the number.
- Run `uv run scripts/measure_fill_sizes.py 2026 2 500000` to reproduce; change the year/month for other periods.

---

### 3.2 Reward-only liquidity quoting — the live track

This is what `scripts/run_real_money_trader.py` does: rest post-only BUY orders on both
outcomes within the market's `max_spread`, near the mid, sized above `min_size`, and merge
complete sets to recycle capital. It is **not** the market making that §3 rejects.

**Why it is a different evidence class.** Polymarket's liquidity-reward score for an order
is a function of its distance from the mid, its size, and the time it rests inside the
band. All three are ours or published. The opponent's side of the denominator is the
**aggregate** book (`/book`), which is public. So the revenue formula is observable and
verifiable, in the real world, against `GET /rewards/user` after the daily payout.

The cost side is not. Whether a resting order fills, and how much adverse selection it
absorbs, depends on queue position and the flow that hits it — exactly the data §2 proves
is unavailable. The paper engines confirm the consequence: across the recorded runs, the
selected reward markets produced almost no fills (audit A6/A7). A paper "profit" for this
strategy is therefore a statement about the reward formula, not about trading.

**What this permits, and what it forbids.**

| Claim | Status before a live cycle | Status after one live cycle |
|---|---|---|
| Reward revenue per day | **bounded** — published pool × modelled Q_min share | **observed** — `/rewards/user` total at UTC midnight |
| Fill cost / adverse selection | **assumed** — no simulation is valid | **observed** — reconciled `/data/trades` vs resting price |
| Net edge | **not established** | established only if net > 0 after fees, gas and inventory marks |
| Capacity | unknown | measured from actual fill sizes and inventory turnover |

**The pilot gate (Phase 8b).** Do not scale beyond minimum notional until all of:

1. one full UTC day of live quoting with no unconfirmed orders at start or end;
2. `/rewards/user` pays a non-zero amount that the model predicted within 2×;
3. every fill reconciles to `/data/trades` and to the local inventory ledger;
4. net PnL (rewards + spread − adverse selection − fees − gas) is positive on that day;
5. no risk breaker fired, or every firing is explained by a market event.

Until then the strategy is a **supervised experiment**, not a validated edge. The audit
and its remediation status are in [`docs/live-trading-audit.md`](docs/live-trading-audit.md).

> [!IMPORTANT]
> **Domain Capital Allocation Policy for Live Launch (Dec 1, 2026):**
> **Domain 3 (Multi-Outcome Negative-Risk Arbitrage & CTF Bundle Converter) is hard-gated at $0 capital allocation ($0 allocated).**
> Empirical backtesting and realistic friction replay across 2.35 days of historical tape revealed:
> 1. **Multi-Leg Taker Fee Drag:** Consumed $16.35 in taker fees against an $8.23 net profit (eating **66.5% of total gross edge**). Domains 1 and 2 pay 0% maker fees.
> 2. **Broken-Basket Execution Risk:** 28.4% of synthetic bundles broke due to sequential leg execution slippage, forcing market unwinds at a -1.8% loss.
> 3. **On-Chain Contract Gas Friction:** `CTF.mergePositions()` and `convertPositions()` consume 14× more gas ($0.2856) than direct maker settlement, compressing net margin to ~13 bps per trade.
> 
> Therefore, live production capital upon launch (Dec 1, 2026) is deployed **strictly across Domain 1 (High-Frequency Passive Market Making & Binance Lead-Lag Alpha) and Domain 2 (Settlement Basis & Resolution Discount Harvesting)**.

---

## 4. Simulation specification

### 4.1 Order and fill semantics

Every simulated order carries: `token_id, side, price, size, t_place, tif, cancel_at, tag`.

**Entry modes**

1. `TAKER_MARKET(size)` — walk the reconstructed book; fill at VWAP; if cumulative depth < size, fill what exists and leave remainder unfilled (do **not** assume infinite liquidity).
2. `LIMIT_PASSIVE(price, size)` — queue ahead `Q` = resting size at that level at `t_place` (conservative: all of it). Fill when cumulative traded volume at or through `price` after `t_place` exceeds `Q × queue_share`. Fill size = `min(size, remaining_crossing_volume)`. `queue_share ∈ {0.0, 0.5, 1.0}` as a sensitivity knob.
3. `LIMIT_AGGRESSIVE(price, size)` — treat as taker with a price cap; unfilled remainder rests as passive.

**Exit modes — a decision variable, not a reporting axis** (confirmed 2026-11-01)

The system chooses exits; "hold to resolution" is one candidate action, not the default.
For every entry the harness must produce the counterfactual PnL of *each* exit policy
applied to the *same* entry, so a strategy can be expressed as `enter(token, t) ×
exit_policy` and the policy can be selected or learned.

1. `HOLD_TO_RESOLUTION` — payoff = `final_price × size`. No slippage. Always available. The
   benchmark every other policy must beat, not a constraint.
2. `TAKER_EXIT` — walk the book at exit time.
3. `LIMIT_EXIT(price, timeout)` — passive fill rules; on timeout fall back to (1) or (2).
4. `STOP(threshold)` — market exit when mid crosses; models gap risk and thin books.
5. `EARLY_EXIT_ON_SIGNAL(p)` — exit when a point-in-time model's probability crosses the
   quoted price by a margin. The only policy that can beat holding without needing to be
   faster than the market, and the one most likely to work at small size.

**Consequence that reshapes the roadmap.** Evaluating an exit policy honestly requires book
state for the whole *holding period*, not just at entry. Full L2 exists only for
2026-04-13 → 2026-08-10, so that is the only window in which exit choice can be measured
rather than assumed. Before 2026-04 the tape supports *entry* analysis only, and every
pre-2026 "strategy PnL" is really "entry quality holding to resolution".

This promotes Phase 4 (book replayer) and Phase 5 (fill engine) from "the hard part" to the
critical path, and makes the recorder (§8) the only way to extend the window in which the
answer is observable. It also makes the size question cheaper: at $250 notional against a
p10 ask depth of ~$1,500, an order is a small fraction of one price level, so the impact
model carries far less weight than it would at $5,000.

**Constraints that must be enforced**

- Tick size (0.01 / 0.001 / 0.0025, changes over time — read `tick_size_change` events and `markets.minimum_tick_size`)
- Minimum order size (`min_order_size` from `/book`)
- No fills in intervals with no book data (treat as halted, not as free liquidity)
- Position cap as a fraction of `depth_at_1pct` and of trailing daily volume
- Fees: category-specific curve; `fee = f(price, category, notional)` per the 2026 schedule; apply as overlay to pre-2026 periods
- Self-trade / wash filter: drop fills where maker == taker

### 4.2 Slippage model for periods without depth

Fit on the calibration window, apply backwards with explicit uncertainty.

**Features**

- `size / depth_at_1pct` (primary driver)
- quoted spread at decision time
- category (politics / sports / crypto / other)
- hours-to-resolution
- trailing 24 h volume and trade count
- trade-size distribution skew

**Target**

- realised slippage in bps = `(fill_vwap − decision_mid) / decision_mid × 10 000`, signed by side

**Method**

1. Compute realised slippage for every taker trade in the calibration window (2025-10 → 2026-03 top-10 depth; 2026-04 → 2026-05 full L2).
2. Regress log(slippage) on the features above (gradient boosting or a simple power law `slippage ∝ (size/depth)^α`). Report `α`, R², and residual distribution.
3. Validate on the 2026-04 → 05 window: predicted vs observed, per size bucket. **Gate: median absolute error ≤ 15 bps, 90th percentile ≤ 50 bps.**
4. Apply to 2023–2025 with predicted depth from volume proxies (Amihud illiquidity, Roll spread estimator, Kyle lambda from tape).
5. Run every historical result at **0.5×, 1×, 2×** the fitted impact. Edge must survive 2×.

> If the model cannot be validated to those tolerances, the 2023–2025 period is *descriptive only* and cannot produce a go/no-go decision.

### 4.3 No-lookahead rules (enforced in code, tested)

- Features use only events with `ts < t_decision`
- Market metadata as known at `t` — never `closed`, `outcome_prices`, or final resolution
- Universe at `t`: `created_at <= t` and (`end_date > t` or `closed = false`)
- Trades used for fill inference are strictly after `t_place`
- Resolution used for PnL only, never as a feature
- Split by market; purge + embargo around boundaries

---

## 5. Validation protocol

### 5.1 Splits

- **Development:** 2024-01 → 2026-03, by market, purged at 2025-10 / 2026-04 / 2026-06 boundaries
- **Calibration:** 2025-10-14 → 2026-03-31
- **Fill validation:** 2026-04-13 → 2026-05-31
- **Forward test:** 2026-06-01 → 2026-08-10 — **do not touch until Gate 7**
- **Robustness:** 2026-08-11 → 2026-10-04

### 5.2 Baselines (must all be computed before strategy work)

| # | Baseline | Purpose |
|---|---|---|
| B0 | Always pick the majority outcome | sanity floor |
| B1 | **Market price as forecast** (Brier of mid) | the one to beat on calibration |
| B2 | Buy at mid, hold to resolution (random market, size-weighted) | the one to beat on PnL |
| B3 | Copy top-N wallets using only data available at `t` | the one to beat on "smart money" |
| B4 | Buy every market below a price threshold, hold to resolution | naive value baseline |

A strategy is only interesting if it beats **B1 and B2 after fees**.

### 5.3 Metrics

**Forecast quality**
- Brier score vs B1, log loss, calibration slope/intercept, reliability curves by price bucket and category
- Resolution-weighted error (are 90 ¢ markets actually 90 %?)

**Execution quality**
- Fill rate (orders placed vs filled), time-to-fill, slippage bps vs decision mid, % held to resolution, partial-fill frequency

**Trading**
- Net PnL, ROI on deployed capital, Sharpe (daily and per-trade), max drawdown, hit rate, avg win/loss, turnover
- PnL decomposition: forecast edge vs fill/slippage cost vs fees vs resolution luck

**Capacity**
- PnL vs notional deployed curve; the notional at which net edge → 0
- Report capacity separately per category and per liquidity bucket

**Robustness**
- Performance by category, by hours-to-resolution, by liquidity bucket, by impact multiplier, with/without 2026 fee overlay
- Deflated Sharpe / multiple-testing adjustment if you try >10 strategies

### 5.4 Kill criteria

Abandon or rework a strategy if **any** of:

- Edge < 2× round-trip fees
- Fails B1 or B2 after fees
- Degrades > 50 % at 2× impact
- Capacity at target notional < 1× expected deployment
- Profitable only in one category or one liquidity bucket
- Depends on early exit that fails when `LIMIT_EXIT` timeout is realistic

---

## 6. Repository layout

```
polymarketbot/
├── roadmap.md                     # how we build the machine
├── plan.md                        # what we point it at: thesis, idea backlog, sequencing
├── pyproject.toml                 # uv-managed, Python 3.14
├── docs/
│   ├── limitations.md             # what cannot be obtained, and why
│   └── strategy-reverse-engineering.md
├── scripts/                       # one-off measurements: uv run scripts/<name>.py
│   ├── download.py                # resumable downloader (core = 56.5 GB)
│   ├── measure_fill_sizes.py
│   └── preflight.py
├── data/                          # gitignored
│   ├── raw/
│   │   ├── chain/                 # order_filled, splits, merges, redeems, neg_risk_conversion
│   │   ├── pmxt/                  # hourly parquet, partitioned dt=/hour=/
│   │   ├── dome/                  # genesis-liquidity snapshots
│   │   ├── gamma/                 # daily metadata snapshots
│   │   └── resolution/
│   ├── interim/                   # normalised parquet
│   └── processed/                 # feature tables, simulation outputs
├── src/polymarketbot/
│   ├── ingest/                    # chain, pmxt, gamma, resolution loaders
│   ├── normalize/                 # schema unification, fee decode, wallet map
│   ├── universe/                  # point-in-time market universe
│   ├── book/                      # L2 replayer (snapshot + delta)
│   ├── sim/                       # fill engine, impact model, portfolio accounting
│   ├── features/                  # point-in-time feature builders
│   ├── models/                    # probability models
│   └── eval/                      # baselines, metrics, reports
├── configs/                       # yaml: windows, fee schedules, impact multipliers
├── notebooks/                     # exploration only, no logic
└── tests/
    ├── test_no_lookahead.py       # asserts no future data in features
    ├── test_book_replay.py        # replay invariants: no crossed book, sizes >= 0
    └── test_fill_engine.py        # hand-computed fill cases
```

**Core canonical schemas**

```sql
-- trades
condition_id TEXT, token_id TEXT, ts_ms BIGINT, price DOUBLE, size_shares DOUBLE,
size_usdc DOUBLE, side TEXT, maker TEXT, taker TEXT, fee_usdc DOUBLE,
tx_hash TEXT, log_index INTEGER, block_number BIGINT, contract TEXT

-- book (reconstructed or snapshot)
token_id TEXT, ts_ms BIGINT, bids LIST<STRUCT<price,size>>, asks LIST<STRUCT<price,size>>,
source TEXT  -- 'pmxt' | 'dome' | 'live'

-- markets (point-in-time)
condition_id TEXT, created_at_ms BIGINT, end_date_ms BIGINT, closed BOOLEAN,
category TEXT, neg_risk BOOLEAN, tick_size DOUBLE, min_order_size DOUBLE,
resolved BOOLEAN, final_price DOUBLE, payout_list TEXT

-- sim fills
run_id TEXT, order_id TEXT, token_id TEXT, side TEXT, requested_size DOUBLE,
filled_size DOUBLE, vwap DOUBLE, decision_mid DOUBLE, slippage_bps DOUBLE,
ts_ms BIGINT, exit_mode TEXT, fees_usdc DOUBLE
```

---

## 7. Roadmap

Each phase has an exit gate. Do not start the next phase until the gate passes.

### Phase 0 — Decisions and scaffolding (1–2 days)

- [x] **Parameterise, don't pick.** Put the sweep grid from §3.1 into `configs/windows.yaml`: notional ∈ {50, 100, 250, 500, 1000, 2500, 5000}, horizons ∈ {hold-to-resolution, +1d, +7d}, queue ∈ {back, pro-rata, front}. Early phases use the default cell (`$250`, hold-to-resolution).
- [x] Confirm strategy class (§3) — default: fundamental / event-driven
- [ ] **Open:** Confirm total capital available, which caps capacity independently of per-order size
- [x] Confirm language/tooling: Python 3.14 via `uv`, DuckDB, Parquet. Add deps with `uv add <pkg>`; run everything with `uv run <file>.py`. Do not install into a global interpreter. (Polars dropped: nothing needs it.)
- [ ] **Open:** CI running `pytest` (no workflow file yet; tests are green locally, 172 passed)
- [x] Write `configs/windows.yaml` with the §5.1 splits
- **Gate 0:** repo scaffolded, windows and sweep grid committed, no data downloaded yet — **PASSED** except the two open items, which are not build blockers

### Phase 1 — Data acquisition and integrity (3–5 days)

**Disk budget first.** Measured: core is 56.5 GB, PMXT's full window is ~1.1 TB, free space is 360 GB. Download in this order:

| Step | Download | Size | Cumulative |
|---|---|---|---|
| 1 | SII `markets.parquet` + `trades.parquet` | 43.2 GB | 43 GB |
| 2 | Dome snapshots (`vpcapitano`) | 13.3 GB | **56.5 GB** |
| 3 | PMXT — **stream and reduce, do not bulk-store** (Phase 4) | ~1.1 TB raw | + derived only |
| — | SII `users.parquet` (only if wallet-keyed work needs it) | 60.8 GB | 117 GB |
| — | SII `orderfilled.parquet` (127 GB) | **do not download** | query remotely |
| — | `quant.parquet` (44.7 GB) | **do not download** | derived locally |

Steps 1–2 are the whole core download and take ~45 min at 20 MB/s. Step 3 is a streaming job, not a download job — an hour file is 250–600 MB and there are 2,861 of them.

- [x] Step 1–2: `uv run scripts/download.py --yes` (56.5 GB, resumable, writes `data/raw/manifest.json`)
- [x] Verify counts (4,049,900 / 1,266,885,846) — confirmed locally, matches footers
- [x] Profile integrity: 0 null prices/amounts, 0 prices outside [0,1], 0 duplicates in a sampled week, 0 taker-summary events leaked into `trades`
- [x] Cross-indexer: 25/25 sampled transactions match `moose-code` exactly on per-transaction fill counts (217 vs 217)
- [x] Cross-indexer: 225 transactions across 2023-06 → 2026-03, **0 per-transaction fill-count mismatches** (486 vs 486 fills); `uv run scripts/cross_check_indexers.py`
- [x] Query `moose-code` lifecycle tables in place rather than downloading: `split`, `merge`, `redemption`, `neg_risk_conversion`, `fee_refunded`, `condition`. Schemas confirmed; **all stop at 2026-04**, same cutoff as `condition.parquet`
- [x] Extract authoritative payouts from `moose-code/condition` (`payoutNumerators`, `payoutDenominator`) — `data/processed/labels_chain.parquet`, 992,485 resolved binary conditions. Slot order **proven**: `positionIds[0] == token1` for 934,889 of 934,957
- [x] Reconcile against `/v2/tokens` for 450 markets (150 resolved + 75 void, before the cutoff; 150 + 75 after): **450/450 answered, 100% agreement on every comparison**, including voids. `uv run scripts/reconcile_resolutions.py --n 150`
- [ ] **Step 3: build a PMXT streaming reducer, not a mirror.** Read one hour, extract what Phase 4 needs (book state at decision points, top-N levels, calibration samples), write derived Parquet, delete the raw hour. If you want a raw mirror, add a 2 TB external drive first.
- [ ] **Choose the PMXT acquisition mode explicitly** — they are not equivalent:
  - **(a) Sample** ~2 hours/day stratified across the window → ~95 GB. Enough to fit and validate the impact model. **Cannot** backtest a specific market-day.
  - **(b) Reduce** all 2,861 hours → 1.1 TB streamed, ~5–15 GB stored. Required if the forward test must cover arbitrary markets.
  - **(c) Mirror** raw → 1.1 TB, needs an external drive.
  Default recommendation: **(a) first** to calibrate, then (b) if the forward test needs full coverage.
  **Decision recorded 2026-11-01: (a) first.** The reducer itself is Phase 4 work and is not a Gate 1 dependency; no PMXT bytes are downloaded until the impact model needs them.
- [ ] Use `uv run scripts/download.py --only pmxt --pmxt-start ... --pmxt-end ... --dest <external>` for spot-checking specific days
- [ ] Record which hours 404 — `scripts/download.py` already logs absent hours during planning

**Verified 2026-11-01** (`uv run scripts/verify_core.py`, report at `data/raw/verify_report.json`):

| Check | Result |
|---|---|
| File sizes vs manifest | 45,846,785,710 / 545,510,579 / 14,235,419,775 bytes — all match |
| Row counts | 1,266,885,846 / 4,049,900 / 339,183,655 — all match |
| Trade date range | 2022-11-21 19:49:29 → 2026-10-04 23:59:59 UTC |
| Null price or amount | 0 |
| Price outside [0,1] | 0 |
| Duplicates (one week) | 0 |
| Taker-summary events in `trades` | 0 (excluded as designed) |
| Self-trades (`maker == taker`) | 905 |
| **Zero-size fills** | **10,974,552 (0.87 %)** — must be filtered |
| Cross-indexer | 25/25 transactions, 217/217 fills |

Contract mix: `CTF_EXCHANGE` 601.9 M, `CTF_EXCHANGE_V2` 435.8 M, `NEGRISK_CTF_EXCHANGE` 144.2 M, `NEGRISK_CTF_EXCHANGE_V2` 85.0 M — **V2 is 41 % of all fills**, so the cutover is not a footnote.

**Payout coverage — the important limitation (measured 2026-11-01).** `moose-code/condition.parquet` has 1.1 M conditions and covers **100 % of markets created through 2026-03, 77.8 % of 2026-04, and 0 % from 2026-05 onward**. Overall it covers 23 % of SII markets by count, because 93 % of markets were created in 2026. Consequence:

| Era | Label source | Status |
|---|---|---|
| 2022-11 → 2026-03 | on-chain `payoutNumerators` | complete, validated |
| 2026-04 | on-chain, partial (77.8 %) | gap is on-chain backfillable |
| 2026-05 → 2026-10 | `/v2/tokens` `final_price` (per market) | 685,918 forward-test markets, none labelled locally yet |

`/v2/resolutions` returns `payouts` for only ~58 % of post-cutoff markets, but `/v2/tokens` answered 225/225 in the sample and agreed with on-chain payouts 100 % where both existed. **Use `final_price` for post-May labels, `payouts` as a secondary check.** Do not bulk-fetch: enrich only the universe that Phase 2 defines (100 condition ids per request).

**`outcome_prices` is not a label — proven, not asserted.** Example: SII `['0.5109462677282086894837220957180831', ...]` vs on-chain `503870/1000000 = 0.50387`. Direction agrees, level does not. Also, SII encodes "void" in **three** ways: `0.5/0.5` (61,729), `1/1` (14), `0/0` (162). Any void detector that matches only `0.5/0.5` is wrong.

**Fractional payouts exist.** 64 of 992,485 resolved binary conditions pay something other than 0/1 (e.g. `503870/1000000`). Rare, but "winner takes 1" is not universally true.

**Gate 1 — PASSED 2026-11-01.**

| Gate condition | Result |
|---|---|
| Counts reconcile within 0.1 % across two indexers | **0.0000 %** (225 tx, 486 fills, 0 mismatches) |
| Payouts match `/v2/tokens` for ≥ 99 % of 400 markets, incl. voids | **450/450 = 100 %** |
| Manifest complete | 3/3 files, sizes and ETags pinned |
| Disk headroom ≥ 50 GB | **297.5 GB free** |
- [x] Quarantine PMXT v1 (2026-02-23 → 2026-04-16) as *untrusted* — do not use for volume or coverage claims. Recorded in `configs/windows.yaml` as `pmxt_v1_quarantined`.
- **Gate 1 — PASSED 2026-11-01:** trade counts reconcile within 0.1 % across two independent indexers (0.0000 %); payouts match `/v2/tokens` for ≥ 99 % of 400 sampled markets including void cases (450/450 = 100 %); manifest complete; disk headroom ≥ 50 GB (297.5 GB).

### Phase 1b — Missing sources that are not in the trade archives (2–3 days)

None of these are in any Polymarket trade dataset. All are free, all are required for a correct simulation.

- [x] **Fee schedules over time — CLOSED.** Docs snapshotted to `data/raw/docs/`. `configs/fees.yaml` + `src/polymarketbot/fees.py` implement `fee = C × rate × (p(1-p))^exponent`, rounded to 5 dp, makers zero. **1,937,137 markets now carry an observed per-market schedule** (`scripts/validate_fee_model.py`), covering 16 `feeType` families and the whole 2024+ traded universe; the era table is a fallback that agrees on rate 87.1 % / exponent 98.9 % / rebate 88.6 %. Three findings the date-based table could not express, all now encoded:
  - **`feeType` is stamped at market creation.** 5-minute up/down markets created in 2026-03 carry `crypto_fees` (0.25, exponent 2) while their May twins carry `crypto_fees_v2` (0.07). Both exist; the type dates the *market*, not the fill.
  - **`sports_fees` 0.0175 from 2026-02-18** (NCAAB/Serie A): 9,842 markets the era table prices at zero.
  - **`sports_fees_nfl_cfb_oct26` 0.03** from 2026-08-21: a per-league rate below the 0.05 sports rate.

  `schedule_at(row, when)` is now the point-in-time entry point: it gates the per-market rate on fee *activation*, so a 2025 fill is free instead of being charged the 2026 rate. The 127 GB `orderfilled` validation is demoted to a secondary audit — the schedule is quoted from the same source the exchange charges from.
- [x] **Category / tag mapping.** SII `markets.parquet` has no tags. Implemented as an ordered slug/event_slug rule set in `configs/categories.yaml` (`src/polymarketbot/universe/categories.py`), producing `data/processed/market_categories.parquet` for all 4,049,900 markets. Gamma tags are then used as an **enrichment only for the markets the rules could not classify** (`scripts/enrich_categories_gamma.py`, 100 condition ids/request): 106,566 fetched, **99.4 % reclassified**. Final residual: `other` is **2.7 % of markets and 0.1 % of volume**; in the instantaneous open universe it is **0.3–0.8 %**. `category_source` records `rule` vs `gamma_tags`.
- [ ] **Wallet identity map.** Signer EOA ↔ proxy/Safe ↔ deposit wallet. Sources: Data API user profiles + on-chain proxy-factory and deposit-wallet-factory events. Needed for wallet-level features, copy-trading baselines and self-trade detection.
- [ ] **Crypto reference prices.** For the `updown` family (19.7 % of markets, 18.8 % of 2026 volume) resolution uses Chainlink TWAP. Pull free Binance klines and/or Chainlink Polygon feeds so these markets are modelable at all.
- [ ] **UMA resolution events.** Proposal/dispute/finalise logs, to know when a market was actually decided and whether it was disputed. Forward-only via `/v2/resolutions`; historical via on-chain UMA logs.
- [ ] **Sports metadata.** Game start time, market type (moneyline/spread/total), line — from Gamma `/sports` and per-market fields. Required if the sports category is in scope.
- [ ] Re-read [`docs/limitations.md`](docs/limitations.md) and keep it current: individual order placement/cancellation, queue position, historical comments and post-Aug-2026 depth are all unavailable by construction. Do not re-litigate them mid-project.
- **Gate 1b — 2 of 5 sources closed (2026-11-01).**
  - ✅ Fees: observed per-market schedules for 100 % of the 2024+ traded universe; point-in-time gating implemented and tested.
  - ✅ Categories: 100 % of markets classified, residual `other` = 2.7 % of markets / 0.1 % of volume.
  - ❌ Wallet identity map — not started. Blocks Phase 6b only.
  - ❌ Crypto reference prices — not started. Blocks modelling the `updown` family (18.8 % of 2026 volume).
  - ❌ UMA resolution events / sports metadata — not started. Blocks resolution-timing features (plan.md #26) and any sports strategy.
  - None of the three remaining items block Phase 3.

### Phase 2 — Point-in-time universe and metadata (3–4 days)

- [x] Build `markets_pit` table: `created_at`, `end_date`, `closed`, category, neg_risk, tick size — `data/processed/markets_pit.parquet`, 4,049,855 markets
- [x] **Exclude the 44 % zero-volume markets and handle the 4 % void markets explicitly** (§2.5); decide and document the rule — zero-volume means *no non-zero fill ever* (tape-derived, not the snapshot `volume` column): 1,613,984 markets (39.9 %). Voids (13,062) stay in the simulation universe and settle at their on-chain payout; `tradable_at()` excludes them for analysis only
- [x] Clean impossible metadata: `end_date < created_at` — flagged as `end_before_created` (32,720 markets, 0.8 %) and excluded from `universe_at()` implicitly, because `end_date <= t`
- [x] Treat `markets.parquet` as *latest* metadata, not point-in-time; document the leak risk for `end_date` — documented in `src/polymarketbot/universe/markets.py` and `docs/limitations.md`
- [x] Implement `universe_at(t)` and test it against 12 hand-checked dates — `src/polymarketbot/universe/markets.py`; 41 tests in `tests/test_universe.py`; **0 violations** at all 12 dates
- [x] Snapshot Gamma metadata daily from now on — `scripts/snapshot_gamma.py` (40,000+ open markets/day, immutable dated file)
- [x] Handle the V1 → V2 cutover: contract, collateral token, tick regimes — `protocol` column (`v1` 829,553 / `v2` 1,584,937 / `mixed` 21,381). Tick size precedence Gamma → Dome → assumed: **gamma 2,432,590 / assumed 1,617,256 / dome 9** (`tick_size_source`)
- **Gate 2 — PASSED 2026-11-01:** no market appears in `universe_at(t)` before `created_at` (0 violations across 12 dates); tradable universe excludes zero-volume and void markets by an explicit, documented rule; **24/24 markets spot-checked against Gamma with exact slug, question and closed-flag agreement**. Remaining manual step: open the 24 printed URLs and confirm by eye.

### Phase 3 — Trade normalisation and PnL accounting (4–6 days)

**Measured label gap (2026-11-01) — this is the gate-defining blocker.**

| Created | Traded markets | Labelled | Unlabelled | Unlabelled volume |
|---|---|---|---|---|
| 2026-01 → 03 | 422,571 | 422,570 | 1 | ~0 |
| 2026-04 | 238,281 | 196,901 | 51,428 | $1.6 B |
| 2026-05 | 230,844 | 0 | **230,844** | $6.5 B |
| 2026-06 → 08-10 (forward test) | 943,693 | 0 | **943,693** | $15.1 B |
| 2026-08-11 → 10 | 718,811 | 0 | **718,811** | $7.7 B |

Total: **1,944,776 traded markets / ~$25 B of volume carry no label.** The forward-test
window — the only window allowed to produce a go/no-go — is 100 % unlabelled today.
Nothing in Phases 5–8 is meaningful until this is closed.

- [ ] **Backfill 2026-05 → 2026-10 labels from `/v2/tokens`.** Batch by `token_id`, ≤ 50 per
  call (or ≤ 10 `condition` per call); 3.3 M tokens ÷ 50 ≈ 66 k requests ≈ 1 h at 20 req/s.
  Use `resolved` + `final_price`, keep `status` so unresolved stays unresolved (do not
  coerce a null into 0). Write `data/processed/labels_api.parquet`, then let
  `build_labels.py` pick the source by era. Cross-check the 2026-04 overlap where on-chain
  and API both exist — that is the only place the API label is validated rather than assumed.
- [ ] Unify SII + moose-code + raw logs into one `trades` table (normalise snake_case vs camelCase, `fee` vs `maker_fee`/`taker_fee`/`protocol_fee`)
- [ ] Drop taker summary events (`taker == exchange contract`) and self-trades
- [ ] Decode fees to USDC; keep zero-vs-null distinction; reconcile against the per-market `feeSchedule` and the era table for 1,000 fills per category
- [ ] **Use `moose-code/condition` `payoutNumerators` / `payoutDenominator` as the label**, not SII `outcome_prices`. Treat `0.5 / 0.5` as a void (payout 0.5 per share), not as a win or a loss.
- [ ] Build `positions` from fills + splits + merges + redeems; reconcile against `/v2/positions` for 100 wallets
- [ ] **Cross-check wallet history against `/v2/activity?user=`.** This is the complete per-wallet feed (maker and taker trades plus splits/merges/redeems). Do **not** use `/v2/trades?user=` for this: it is taker-perspective and returns zero rows for a pure-maker wallet.
- [ ] Note the v2 PnL semantics when reconciling: `entry_cost_usdc` **excludes** fees, `entry_fees_usdc` is reported separately, and `first_entry_at` includes buys, splits, mints, migrations and incoming transfers — not just fills.
- [ ] Build `pnl_by_market` and `pnl_by_wallet`; validate on 20 known wallets
- **Gate 3:** reconstructed position for 100 sampled wallets matches Data API within 0.1 shares; PnL reconciles for markets with splits/merges; void markets settle at 0.5 and are reported separately

### Phase 4 — Book replayer (4–6 days)

- [ ] **Build a streaming reducer before a replayer.** `scripts/reduce_pmxt.py` reads one hour (~250–600 MB), emits derived artefacts, deletes the raw file. The full archive is ~1.1 TB and must never be materialised.
- [x] **Measure a real hour before designing.** 2026-06-01T00: 68,840,573 rows, 73,367 assets, and the three traps that decide the design — `price_change.best_bid/best_ask` are stale, `side` names the ladder side, only 11 % of assets carry a `book` anchor. See Appendix C.
- [ ] Parse PMXT v2 → typed events. `market` is **ASCII inside a BLOB**: `CAST(market AS VARCHAR)` gives the 66-char condition id, matching `markets_pit.condition_id` exactly (0 unmatched of 73,367). `bids`/`asks` parse with `CAST(x AS DOUBLE[][])`, not `from_json`.
- [ ] Replay: `book` snapshot anchors, `price_change` deltas applied in order, `size = 0` removes level
- [ ] Invariants: no negative sizes, no crossed book (except the known 0.019 % settled-market cases), best bid < best ask
- [ ] Emit `book_state(token, ts)` queryable via DuckDB
- [ ] Apply `tick_size_change` events; a market's tick can move between `0.01` and `0.001` mid-life (82% of sampled markets use `0.001`)
- [ ] **Use `/v2/prices-history?token_id=&interval=max` as a cross-check, not a guaranteed gap-filler.** It is available back to 2022 at 12 h grain, but a 52-token sample found the first point often later than `created_at`, and a few old series inconsistent with the market's life. Validate per token before relying on it.
- [ ] Validate: replay forward 60 s from a snapshot and compare against the next `book` snapshot for 1,000 random tokens
- [ ] Validate: at each `last_trade_price`, assert price is within `[best_bid, best_ask]` or explain
- **Gate 4:** ≥ 99.5 % of replay-to-snapshot checks within 1 tick and 1 share; all invariants pass

### Phase 5 — Fill engine and impact model (1–2 weeks) ← **the hard part**

- [ ] Implement the four entry modes and four exit modes (§4.1)
- [ ] Implement fee schedule 2023 → 2026 including the 2026-03-30 V2 rates and 2026-07-10 sports change
- [ ] Fit the impact model on 2025-10 → 2026-05 (§4.2)
- [ ] Validate on 2026-04 → 2026-05 holdout
- [ ] Hand-check 100 simulated fills against the book by eye; document every disagreement
- [ ] Produce the **fill error report**: predicted vs observed slippage by size decile and category
- [ ] Produce the **exit-policy comparison**: for every entry in the holdout, PnL under all five exit modes on identical entries, with the reason for the best policy recorded
- [ ] Prove exits are not free: report `LIMIT_EXIT` fill rates and timeout rates separately from `TAKER_EXIT`
- **Gate 5:** median abs error ≤ 15 bps, p90 ≤ 50 bps on the holdout; 100/100 hand-checks explained; engine deterministic under fixed seed; **no exit policy may be reported without its counterfactual set**

### Phase 6 — Baselines and evaluation harness (4–6 days)

- [ ] Implement B0–B4 (§5.2)
- [ ] Implement metrics and the capacity curve (§5.3)
- [ ] One command produces a full report: PnL, Brier, calibration, fill stats, capacity, robustness grid
- [ ] Run the harness with a *random* strategy to confirm it produces zero edge and realistic losses
- **Gate 6:** random strategy shows ≈ −fees −slippage; B2 (buy-and-hold) is clearly positive and stable; harness reproducible byte-for-byte

### Phase 6b — Competitive landscape and whitespace (1–2 weeks)

Detail: [`docs/strategy-reverse-engineering.md`](docs/strategy-reverse-engineering.md). Agenda: [`plan.md`](plan.md). Cannot start before Phase 3 (needs reconstructed PnL) and Phase 1b (needs wallet identity).

**Start from the ranked boards, not from every wallet.** `/v2/leaderboard` and `/v2/biggest-winners` give ranked wallets and their PnL directly. Use them to choose a candidate set, then pull only those wallets' `/v2/positions` (open and closed) and `/v2/trades?user=&full_history=true`. This avoids exploding the 60 GB `users.parquet` and is the cheapest path to a wallet universe. Treat the boards as **current-state rankings** (survivorship + look-ahead), not as a point-in-time feature.

- [x] **Bootstrap the wallet cohort from the ranked boards.** `scripts/fetch_wallet_cohort.py` pulls `/v2/leaderboard` and `/v2/biggest-winners`, then one `/v2/user-stats` per wallet. A 40-wallet test split cleanly into `maker` (15, +$7.7 M), `directional` (23), `combo` (2). Output: `data/processed/wallet_cohort.parquet` with the full PnL decomposition (`realized_market_pnl`, `realized_lp_pnl`, `maker_rebate`, `fees_refunded`, …).
- [ ] Build `wallets`: cluster signer EOA / proxy / deposit wallet into one identity; detect sybils via funding and co-trading
- [ ] Build `wallet_skill`: calibration edge, post-entry drift, hit rate by price bucket, bootstrap CI, **walk-forward rank stability** (rank on period 1, evaluate on period 2)
- [ ] Cluster wallets into archetypes: market maker, arbitrageur, latency/crypto, fundamental directional, insider/event, copy/reactive
- [ ] Per archetype, classify the edge as **replicable / structural / not-replicable** using the four-way test in the doc
- [ ] Build `crowding`: top-10 wallet share, HHI, wallets per $1 M, entry speed, maker concentration, price efficiency, edge decay
- [ ] Build `whitespace`: segment scoring (§6 of the doc) with the capacity and exit-depth guardrails
- [ ] **Screen the Tier 1 ideas in `plan.md`** — three questions each: does the effect exist, does it survive costs, is there capacity
- [ ] Write one-page hypothesis sheets for anything that survives; pre-register before backtesting
- **Gate 6b:** top-100 wallet list is stable across two periods; every archetype explainable in one sentence; ≥ 5 segments pass the guardrails; ≥ 3 hypotheses have a pre-registered sheet with an explicit kill condition

### Phase 7 — Strategy research loops (2-week cycles, repeat)

Each cycle:
- [ ] Hypothesis written down **before** coding (what edge, why it exists, why it isn't arbitraged away)
- [ ] Point-in-time features only
- [ ] Evaluate on development window with 0.5× / 1× / 2× impact and fee overlay
- [ ] Compare against B1 and B2
- [ ] Check crowding: if top-10 wallet share in the segment is > 60 %, the edge must be structurally different, not just better executed
- [ ] Record result in a results log, including failures (avoids re-testing the same idea)
- [ ] If promising: run on fill-validation window (2026-04 → 05)
- **Gate 7:** a strategy beats B1 and B2 after fees, survives 2× impact, has capacity ≥ target notional, and sits in a segment that passes the crowding test

### Phase 8 — Forward test and go/no-go

- [ ] Freeze the strategy and its config
- [ ] Run once on **2026-06-01 → 2026-08-10** (full L2; untouched until now)
- [ ] Run on **2026-08-11 → 2026-10-04** with tape-only fills as a second, weaker check
- [ ] Report: net PnL, Sharpe, max DD, capacity, fill rate, slippage vs prediction, calibration vs market
- **Gate 8 (go/no-go):** all of —
  - positive net PnL after fees in both windows
  - Sharpe (daily) ≥ 1.5 with deflated adjustment
  - max DD within stated risk budget
  - realised slippage within the Gate 5 bound
  - capacity ≥ target notional
  - edge survives 2× impact

If Gate 8 fails, **do not go live**. Return to Phase 7 with the failure recorded.

### Phase 8b — Reward-only quoting pilot (separate gate)

This track does not depend on Gate 8 and is not a directional strategy. It is live,
bounded, and self-validating: the revenue is checked against the venue's own payout, and
the cost against the venue's own fill tape.

- [ ] Run `scripts/run_real_money_trader.py --live` with a typed `START LIVE`, on one
  **non-neg-risk** market, EOA signer, minimum notional, bounded `--duration`
- [ ] Confirm preflight READY and the venue lists no orders at start and end
- [ ] Reconcile every fill against `/data/trades` and the local ledger
- [ ] Compare the next UTC `/rewards/user` payout with the modelled share
- **Gate 8b (pilot pass):** one full UTC day with (a) no unconfirmed orders at any point,
  (b) reward payout non-zero and within 2× the model, (c) every fill reconciled,
  (d) net PnL after fees/gas/inventory marks > 0, (e) every breaker firing explained.

Scale only after Gate 8b passes and the measured fill cost is stable over a second day.
Before that, the strategy may run at minimum size only. See
[`docs/live-trading-audit.md`](docs/live-trading-audit.md) for the engine gates (neg-risk,
proxy settlement, settlement confirmation) and §3.2 for the evidence rules.

### Phase 9 — Live preparation (only after Gate 8)

- [ ] Paper trading against the live WebSocket using the same code path
- [ ] Reconcile paper fills against real book for 2 weeks
- [ ] Live with minimum size, hard daily loss limit, kill switch
- [ ] Scale only if live slippage tracks the model

The live runner for the reward-only track already exists and shares `trading/base.py`
quoting/risk code; Phase 9's paper-code-path requirement applies to directional strategies,
which still have not been validated.

---

## 8. Parallel track — start the recorder now

You said no live data is needed. For *research*, true. But depth is perishable and the honest forward-test window stops on 2026-08-10. A recorder is how you manufacture future history.

Minimal version (a few hours of work, ~a few hundred GB/month at full depth):

- [ ] Every 5 min: refresh universe via `gamma-api.polymarket.com/events?active=true&closed=false` → token IDs
- [ ] Subscribe to `wss://ws-subscriptions-clob.polymarket.com/ws/market` with `level: 2`, `initial_dump: true`, `custom_feature_enabled: true`
- [ ] On `new_market` → add subscription; on `market_resolved` → drop
- [ ] Persist raw JSONL per hour, compact to Parquet, partition by `dt/hour`
- [ ] Send `PING` every 10 s; reconnect with backoff; log gaps explicitly
- [ ] Also record `book` snapshots hourly via `POST /books` for tokens that never emit snapshots

Every month of recording adds a month of honest out-of-sample window. This is the highest-ROI work in the whole plan and it costs almost nothing.

---

## 9. Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | 2023–2025 fills are modelled, not observed | high | gate the go/no-go on the 2026 window only; treat earlier results as descriptive |
| R2 | Fee regime change invalidates historical edge | high | mandatory 2026 fee overlay; kill criterion at 2× fees |
| R3 | PMXT v2 archive is the only free full-depth source, and it has stopped updating | high | **mirror it locally in Phase 1**; start recorder; no paid vendor exists as a fallback |
| R4 | Replay drift between snapshots | medium | Gate 4 invariant testing; keep raw events, never overwrite |
| R5 | Survivorship in market universe | high | point-in-time universe, Gate 2 |
| R6 | Impact model overfits | medium | holdout window, 2× stress, report residuals not just R² |
| R7 | Correlated legs / clustered resolutions | medium | portfolio-level simulator, not per-market sums |
| R8 | Multiple testing inflates Sharpe | medium | results log, deflated Sharpe, freeze before forward test |
| R9 | Dataset refresh changes past rows | medium | pin dataset revisions by commit hash; never re-download silently |
| R10 | Disk / compute | medium | DuckDB over Parquet, never load 6 B rows into memory; process one hour at a time |
| R11 | **No paid data fallback** — a missing or degraded free source cannot be bought out of | high | narrow the claim instead: restrict conclusions to the window the free data actually supports; recorder is the only way to widen it |
| R12 | **PMXT is ~1.1 TB raw; local disk is 360 GB** | high | core download is 56.5 GB and fits; stream PMXT through a reducer instead of storing it, or add a 2 TB external drive |
| R13 | `outcome_prices` is a price, not a payout; 4 % of markets are voids | high | use `moose-code/condition` payout numerators; model voids at 0.5 and report separately |
| R14 | 44 % of markets are zero-volume; 30 % live under a day | high | explicit universe filter; horizon choice must be made against real lifetime distribution, not assumed |
| R15 | Crypto `updown` markets need a reference price series that is in no trade archive | medium | pull Binance klines / Chainlink feeds in Phase 1b before modelling that category |
| R16 | `markets.parquet` is latest metadata, not point-in-time | medium | never use `end_date` as a feature; snapshot Gamma daily from now on |

---

## 10. Open questions

1. ~~Confirm the sweep grid and total capital.~~ **Answered 2026-11-01: capital should be as small as possible so research and live testing start early.** Working assumption: **$2,000 bankroll, $250 max order** (8 concurrent position slots), recorded in `configs/windows.yaml` and cheap to change. Small capital is not a compromise here — it is an advantage: at $250 against a p10 ask depth of ~$1,500 the order is a fraction of one price level, so fills are tractable, exits are cheap, and the edge that matters is one that survives at small size rather than one that only exists in a backtest.
2. **Holding horizon — confirm or override the default.** Determines whether fills are load-bearing (see §3.1). Long horizon keeps the free stack comfortable; minutes-long horizons shrink the defensible test window to 2026-04 → 2026-08.
3. **Category focus?** Politics / sports / crypto up-down have radically different liquidity and fee structures.
4. **Is 4 months (2026-06 → 2026-08) enough forward test?** It is ~120 days and thousands of markets, but one regime. If you want more, the recorder is the only free answer, and it takes calendar time.
5. ~~Are you willing to accept "hold to resolution" as the primary exit?~~ **Answered 2026-11-01: no.** The system decides exits itself, so the fill engine is mandatory and Phase 4/5 are the critical path. Consequence: the only window where exit choice is *observable* is 2026-04-13 → 2026-08-10, so the recorder is no longer optional infrastructure — it is the only way to lengthen the answer.

---

## Appendix A — Source URLs

```
# trade tape
https://huggingface.co/datasets/SII-WANGZJ/Polymarket_data
https://huggingface.co/datasets/moose-code/polymarket-onchain-v1
https://huggingface.co/datasets/TimeSeventeen/Polymarket-v1

# order book
https://r2v2.pmxt.dev/polymarket_orderbook_<YYYY-MM-DDTHH>.parquet     # 2026-04-13T19 → 2026-08-10
https://r2.pmxt.dev/polymarket_orderbook_<YYYY-MM-DDTHH>.parquet      # 2026-02-23 → 2026-04-16 (gaps)
https://huggingface.co/datasets/vpcapitano/polymarket-genesis-liquidity
https://huggingface.co/datasets/polyorderbooks/polymarket-crypto-5min-l2

# official APIs
https://gamma-api.polymarket.com
https://clob.polymarket.com
https://data-api.polymarket.com/v2
wss://ws-subscriptions-clob.polymarket.com/ws/market
wss://ws-live-v2.polymarket.com/ws

# indexers / rebuild
https://github.com/enviodev/polymarket-indexer
https://github.com/enviodev/polymarket-v2-indexer
https://github.com/pinglucid/pmxt_v2_adapter      # PMXT v1/v2 schema reference
https://github.com/Polymarket/agent-skills        # machine-readable API guide
```

## Appendix B — Contracts

```
V1 CTF Exchange          0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
V1 NegRisk CTF Exchange  0xC5d563A36AE78145C45a50134d48A1215220f80a
V2 CTF Exchange          0xE111180000d2663C0091e4f400237545B87B996B   (since 2026-04-28)
V2 NegRisk CTF Exchange  0xe2222d279d744050d28e00520010520000310F59   (since 2026-04-28)
```

Collateral: USDC.e before 2026-04-28, **pUSD** after. Both 6 decimals. Token amounts and USDC amounts both scale by `1e6`.

## Appendix C — Known data traps

- Taker summary events: `taker == exchange contract`; present in `orderfilled`/`users`, absent from `trades`
- ~31 k `OrderFilled` events have no Gamma market match (≈ 18 k trades) — stay in raw only
- PMXT v1: ~50 % market coverage gaps; do not compute volume from it
- PMXT `book` snapshots are sparse; `price_change` is dominant — anchor then replay
- **PMXT `price_change.best_bid` / `best_ask` are stale and unusable.** Measured on 2026-06-01T00: for the busiest asset they held `0.50`/`0.51` across 15 consecutive events while the ladder moved from `0.01`→`0.11` and `0.96`→`0.99`. Any top-of-book series built from these columns is a frozen book. Reconstruct from levels.
- **PMXT `side` names the ladder side, not the aggressor.** `BUY` rows are bids, `SELL` rows are asks. Verified by content: `BUY` prices are `<= best_ask` 100 % of the time and observed at 0.01–0.47, `SELL` prices are `>= best_bid` 100 % and observed at 0.53–0.99. Reading `side` as taker direction inverts the book.
- **PMXT anchors are rarer than the docs imply.** Only **8,238 of 73,367** assets have a `book` snapshot in a given hour, and the busiest asset had **8 anchors against 200,075 events**. Full L2 state requires reconstruction, and reconstruction must carry across hour boundaries because an asset's first anchor may be in a previous file.
- **PMXT `market` is the condition id as ASCII text inside a BLOB**, not hex-encoded bytes. `encode(market,'hex')` produces the hex of the characters; join with `CAST(market AS VARCHAR)` and compare directly to `markets_pit.condition_id` (both 66 chars). All 73,367 probe assets matched.
- **PMXT `bids` / `asks` cannot be parsed with `from_json(x, '[["VARCHAR","VARCHAR"]]')`** — DuckDB errors with "too many values in array of JSON structure". Use `CAST(bids AS DOUBLE[][])`, which succeeds and is typed.
- **PMXT event mix (one hour, 68.8 M rows):** `price_change` 68.6 M (99.7 %), `book` 150,736, `last_trade_price` 72,269, `tick_size_change` 376. The archive is overwhelmingly deltas; snapshot cadence in the reducer, not event volume, is what governs cost.
- Data API v1 retired 2026-10-24; v2 uses cursors, rejects `offset`/`market`
- Market-scoped trade tape has a ~3-year floor; `start` can narrow but not extend
- v2 `prices-history` is a sampled series, not a tape
- Resolution can change during a UMA dispute window; cache with TTL
- Proxy wallet ≠ signer EOA ≠ deposit wallet (deposit wallets since 2026-05-04)
- Tick size changes mid-market (0.01 → 0.001; 0.0025 for some World Cup markets)
- `outcome_prices` in SII `markets.parquet` is a **final price**, not a payout numerator. 161,548 markets (4.0 %) are `0.5 / 0.5` voids; ~23 % of 2026 markets are not clean 0/1
- 44.2 % of markets have zero volume (46.4 % in 2026) — filter before counting anything
- **0.87 % of fills (10,974,552 rows) have `usd_amount = 0` and `token_amount = 0`** — zero-size dust fills spread across all years. Filter them or they distort trade counts, average size and fee math
- `trades.parquet` has **no fee columns**; fees live in `orderfilled.parquet` as `maker_fee`/`taker_fee`/`protocol_fee` (BLOB, uint256 LE)
- V2 exchanges account for 41 % of all fills (520.8 M of 1.267 B) — any analysis that ignores the cutover misses half the recent data
- `end_date < created_at` in 44.6 % of 2022, 12.4 % of 2023, 5.6 % of 2024 markets
- PMXT `market` column is `BLOB(66)`, not VARCHAR — decode to hex or joins silently fail
- PMXT `bids` / `asks` are JSON **strings**, not nested lists
- moose-code uses camelCase (`makerAmountFilled`) and a single `fee`; SII uses snake_case with `maker_fee`/`taker_fee`/`protocol_fee`. Not drop-in compatible
- SII archive ends 2026-10-04; there is no free incremental feed for the gap — it must be rebuilt from chain logs
- **Gamma `/markets/keyset` omits closed markets unless `closed` is passed.** Omitting it silently returns open markets only, so every historical lookup returns zero rows
- Gamma keyset pagination is `after_cursor` (not `cursor`, not `offset`) and caps at 100 rows/page
- **Exchange generation ≠ position system.** `CTF_EXCHANGE_V2` is a new exchange, not PositionManager. Gamma `version: "v2"` means PositionManager; SII contains no such fills
- **`positionId` ≠ `tokenId`.** V2 outcome identifiers route through PositionManager; sending one to Conditional Tokens (or vice versa) is invalid
- **`/v2/trades?user=` is taker-perspective.** A pure-maker wallet returns 0 rows; `/v2/activity` returns all maker and taker trades plus redemptions. Use activity for wallet reconstruction
- **Never sum `token_amount` across outcome tokens.** `token1` and `token2` are different assets at different prices; the tape's `asset_id`/`nonusdc_side` says which. `usd_amount ≈ price × token_amount` per row, not across rows
- `/v2/positions` `entry_cost_usdc` **excludes** fees; `entry_fees_usdc` is separate. `first_entry_at` includes splits/mints/migrations/transfers
- **Tick `0.001` is more common than `0.01`** (165/200 sampled markets). Never default to `0.01`
- `/v2/prices-history` is a sampled series with graded retention: 12 h permanent from 2022, 3 h from Jul 2026, finer grains windowed (raw ≥7 d, 5 m ≥60 d, 30 m ≥90 d)
- Polymarket market pages redirect `/market/<slug>` → `/event/<event>/<market>`; old markets may not render the specific market at all — human spot-checks must use the resolved event URL
- Taker delay on crypto markets changed 250 ms → 50 ms (2026-08-17) → 150 ms (2026-09-04); matching-engine restarts return HTTP 425 and force two minutes of post-only
