# Alpha pipeline — what gets tested, in what order

`plan.md` holds the original backlog (ideas #1–28). This is the *queue*: every idea we
intend to test, what data it needs, and what would kill it.

Three facts learned on 2026-11-01 reshape the order:

- **The median quoted spread is 2 ¢.** Round-trip cost is ~2 ¢ per share before fees. An
  edge smaller than that is unmeasurable and untradable at our size. This deletes most of
  the small-mispricing backlog before it costs anything to test.
- **Exits are chosen by the system.** An idea that only works while holding to resolution
  is an assumption, not an edge. Nothing graduates without the exit comparison.
- **Half the "blocked" ideas were not blocked.** They were parked on the unavailability of a
  *raw variable* while an observable proxy, a contractual payout, or a public API covered
  the same economics. See the unblocking audit at the end.

---

## Master list

| ID | Idea | Needs | Status |
|---|---|---|---|
| A1 | Executable-entry re-test of favourite/longshot | tape + labels + fees | **ready** |
| A2 | Neg-risk coherence with conversion bounds | tape + labels + fees | **ready** |
| A3 | Ladder monotonicity | tape + labels | **ready** |
| A4 | Scalar/range-market anchoring | tape + labels | **ready** |
| A5 | Lifecycle-stage inefficiency | stored screen rows | **ready** |
| A6 | Resolution-timing premium | tape + resolution timing | **ready** |
| A7 | Term-structure drift | tape + labels | **ready** |
| D2 | Cancel-pressure residual (toxicity proxy) | PMXT book | Tier 1 |
| E1 | Within-market complete-set arbitrage | tape → then book | **existence ready** |
| D1 | Liquidity-reward farming | live Gamma `clobRewards` | **ready, forward-only** |
| E2 | Fee-holiday windows | daily Gamma snapshots | **ready, forward-only** |
| E5 | Reward rotation (yield surface) | live Gamma + book | forward-only |
| E3 | Post-only / cancel-only / restart states | live API | forward-only |
| E4 | Taker-delay asymmetry | per-market `takerDelayMs` + book | needs book |
| B1 | **Exit-policy comparison** | PMXT book | blocked: download |
| B2 | Stale-quote pickoff | PMXT book | blocked: download |
| B3 | Book imbalance → returns | PMXT book | blocked: download |
| B4 | Depth-aware scheduling | PMXT book | blocked: download |
| C1 | Crypto up/down vs reference | Binance klines | **unblocked** |
| C2 | Base-rate models | labels + weather/econ feeds | **unblocked** |
| C3 | Wallet identity map | on-chain logs | **partly unblocked** |
| C4 | Cross-venue vs Kalshi | Kalshi public API | **unblocked** |
| C5 | News latency | GDELT | **unblocked, rate-limited** |
| E6 | Perps funding carry / liquidations | Binance funding + Polymarket perps | **unblocked** |
| E7 | Mechanism transplantation | tape (behaviour) | **unblocked at address level** |

---

## Tier 0 — runnable now

### A1. Executable-entry re-test of idea #1
**Edge story:** the only measured effect (+3.4 pp at 0.80–0.90, t ≈ 11).
**First test:** entry = fills where the taker *bought*, i.e. the price paid was the ask;
T−24 h → T−6 h before resolution; hold. Implemented as `--side ask`.
**Kill if:** the ask-side gap falls below the 2026 fee plus observed spread.
**Expected:** killed at ~1 pp. That is a useful result, not a failure.

### A2. Neg-risk coherence with conversion bounds (#2 + #27)
**Edge story:** mutually exclusive outcomes must sum to 1. Legs are priced independently.
Negative-risk conversion (1 NO → 1 YES in every other leg) sets hard bounds; ignoring it
overstates the opportunity.
**First test:** reconstruct simultaneous leg prices per event, Σ mid, count violations
beyond fees + conversion cost, with size available.
**Kill if:** violations are inside cost, or only where there is no depth.
**Strength:** arithmetic, not information.

### A3. Ladder monotonicity (#3)
Probability must be monotone in strike. Detect inversions across ladders in one event;
measure persistence and the size that could clear.
**Kill if:** ≤ 1 tick, < 1 min, or 1-share size only.

### A4. Scalar / range-market anchoring (#25)
64 resolved binary conditions pay a *fraction*. Test calibration of fractional-payout
markets against the realised payout fraction.
**Kill if:** too few markets to size, or < 2× fees.

### A5. Lifecycle-stage inefficiency (#4)
Calibration error versus market age and time-to-resolution, reusing the stored screen rows.
**Kill if:** flat across age.

### A6. Resolution-timing premium (#26)
Sports resolve a median +4.1 h (p90 +5.8 h) after `end_date`; crypto ~0 h. Measure price
drift in that window, and whether it is tradable at all.
**Kill if:** the window is closed or no discount exists.

### A7. Term-structure drift (#5)
Drift to terminal by (price bucket × hours-to-resolution).
**Kill if:** no monotone pattern, or inside the spread.

### E1. Within-market complete-set arbitrage
`ask(YES) + ask(NO) < 1` → buy both, merge for $1. No forecast, no speed requirement.
**Decided by fees, not signal:** crypto peaks at 1.75 % of notional and kills any 1–2 ¢
gap; geopolitics is fee-free, so the same gap survives. **Category selection is the
strategy.**
**First test:** from the tape, timestamps where last YES + last NO imply a sum under 1 net
of that category's fee, and how long the state persists. Book confirmation later.
**Kill if:** sub-1 sums exist only where the fee exceeds the gap, or last under a second.

---

## Tier 0.5 — forward-only monitors (no history needed)

These are strategies about *where to be now*, so missing history is not a blocker. They
start earning the day they run.

### D1. Liquidity-reward farming
Daily payouts to makers whose resting orders meet `rewardsMinSize` and sit inside
`rewardsMaxSpread`. Eligibility is a function of **quoting, not of being filled**, so
queue position is irrelevant to accrual. **Measured:** 27 % of 400 open markets carry a
pool, $23,993/day summed, mean $224/day per market.
**First test:** (our size / total qualifying size) × `rewardsDailyRate`, minus inventory
risk and adverse selection on fills we do get.
**Kill if:** yield per unit capital trails a directional idea, or qualifying size exceeds $250.

### E5. Reward rotation as a yield surface
`ratePerDay / (qualifying size × capital tied up)`. New or neglected markets can have a
high rate against thin competition. Small capital is an *advantage*: pro-rata share is not
diluted by our own size.
**Kill if:** the required qualifying size is out of reach.

### E2. Fee-holiday windows
Markets are "temporarily made fee-free during a fee holiday". Everything killed by the fee
overlay revives at zero fee. We snapshot Gamma daily, so `feesEnabled`/`feeType`
transitions are recordable from now.
**Kill if:** holidays are rare, short, or confined to depthless markets.

### E3. Venue restriction states
The API distinguishes *matching-engine restart*, *cancel-only* and *post-only*, with
`retry_after_seconds`. During post-only, takers cannot cross, so a resting maker is
protected. If a window overlaps news, the book cannot reprice through takers.
**Kill if:** windows are too rare, or makers flatten rather than benefit.

---

## Tier 1 — needs the PMXT book (2026-04-13 → 2026-08-10)

Blocked on the operator download (`docs/pmxt-runbook.md`), not on analysis.

### B1. Exit-policy comparison — the structural bet
Every entry gets five counterfactuals on identical fills: hold, taker exit, limit exit,
stop, early-exit-on-signal. Report the distribution of the best policy and how often
holding wins.
**Kill if:** holding dominates, or every alternative needs fills we cannot get.

### B2. Stale-quote pickoff (#15)
~23 % of resolved markets had no book in their final hour yet orders rested. Detect fills
outside the reconstructed touch, net of fees.

### B3. Book imbalance → short-horizon returns (#13)
Imbalance at t → return over t+1 m / 5 m / 1 h, net of spread. Likely dead at 2 ¢.

### B4. Depth-aware execution scheduling (#16)
TWAP-by-time versus TWAP-by-depth on identical signals.

### D2 / E4 — need the book and are already listed above
Cancel-pressure residual (#17) and taker-delay adverse selection both require book state.

---

## Tier 2 — unblocking audit (probed 2026-11-01)

Every source below was called live during this audit. Results are what the endpoints
returned, not what the docs claim.

### C1. Crypto up/down vs reference price — **UNBLOCKED, no key**
- `GET api.binance.com/api/v3/klines` → **200**, 1-minute OHLCV with ms timestamps.
- `data.binance.vision` monthly dumps → reachable, for bulk backfill.
- **Key simplification:** we do **not** need Chainlink to replicate resolution, because
  resolution labels already come from on-chain payouts. The reference series is only a
  *feature*, so Binance klines are sufficient. Chainlink would only be needed to reproduce
  the TWAP exactly, which we never have to do.
- **Remaining risk:** 5-minute horizons make this a latency game. Measure the mispricing
  magnitude first; do not build an execution path before knowing it is worth one.

### C2. Base-rate models — **UNBLOCKED in two halves**
- **Base rates need no external data at all.** Market families repeat in the slug
  (`…-on-october-24`, `highest-temperature-in-nyc-on-…`), so the historical base rate is a
  `GROUP BY` over labels we already hold. Cheapest idea in the backlog.
- **Forecasts** need feeds, and they are free: Open-Meteo archive → **200** with daily
  temperature series; `api.weather.gov` → **200**. Econ releases via a free FRED key
  (optional, not blocking).

### C3. Wallet identity map — **PARTLY UNBLOCKED**
- `polygon-rpc.com` → **HTTP 401 "API key disabled, tenant disabled"**. The `HEAD` looked
  healthy, which is why this needed a real JSON-RPC probe.
- `polygon-bor-rpc.publicnode.com` → 200 for `eth_blockNumber` and Chainlink reads, but
  **archive `eth_getLogs` requires a paid plan**.
- `1rpc.io/matic` → 200, `eth_getLogs` capped at ~5 blocks. Unusable for indexing.
- **`polygon.drpc.org` → 200, `eth_getLogs` works** (199 logs in a 10-block range) with a
  **10,000-block cap**.
- Consequence: historical indexing *is* possible on a free endpoint, chunked. Polygon is
  ~2.1 s/block, so 10 k blocks ≈ 5.8 h. Covering 2025-05 → 2026-11 (~550 days, ~22.6 M
  blocks) is **~2,260 chunked requests**. Feasible, rate limits permitting.
- **Also partly avoidable:** for mechanism transplantation we can start at the *address*
  level using the tape's `maker`/`taker` fields and detect sybils from co-trading, deferring
  the on-chain EOA↔proxy map. That removes the dependency from E7's first pass.
- **One key would materially help here:** a free Alchemy/Infura Polygon key raises the log
  range and rate limit, turning ~2,260 chunked requests into a handful of large ones. Not
  blocking, but it is the single highest-leverage key in this project.

### C4. Cross-venue vs Kalshi — **UNBLOCKED, no key**
- `GET api.elections.kalshi.com/trade-api/v2/markets` → **200** with market data and
  cursors, unauthenticated. Trading would need auth; reading does not.
- This makes the spread measurement possible today. Transfer friction and capital-on-both-
  venues remain modelling problems, not data problems.

### C5. News latency — **UNBLOCKED, rate-limited**
- `api.gdeltproject.org/api/v2/doc/doc` → **429** with an explicit "one request every 5
  seconds" limit. Workable with throttling for event studies; not for real-time.

### E6. Perps funding carry / liquidation capture — **UNBLOCKED, no key**
- `fapi.binance.com/fapi/v1/fundingRate` → **200** with `fundingRate` and `markPrice`.
- Funding dispersion against another venue is therefore measurable immediately, and
  Polymarket exposes its own perps funding and mark price in the expectations.

---

## Genuinely parked

| Idea | Reason |
|---|---|
| Fill-dependent market making (#22) | queue position exists only inside the operator's matching engine; the public book aggregates size by price and discards arrival order. **D1 is the substitute** — it pays for quoting, not for fills. |
| Order-to-trade ratio / spoof *attribution* (#23) | individual order placement and cancellation are never published, so a cancel cannot be tied to a wallet. The **residual** (book delta − fills) is computable and is D2. |
| Historical comment sentiment (#21) | stored off-chain, no bulk export, read endpoint is current-state only. Substitute feeds make the *hypothesis* testable (C5). |

---

## The global filter

1. **Effect** — gross edge > 2× round-trip cost (≈ 2 ¢ spread + fee).
2. **Executability** — measured at the ask, not at a VWAP.
3. **Exit** — survives whichever exit the system picks, including the cost of getting out.
4. **Capacity** — depth at $250 ≥ 3× the order.
5. **Crowding** — top-10 wallet share < 60 %, or our edge is structurally different.
6. **Stability** — holds out-of-sample across two periods.

## Cost model correction

The **Taker Rebate Program** returns part of taker fees by tier
(`wV = Size × (1 − Entry Price) × Category Weight × Bonuses`). Every "dead after fees"
conclusion in `docs/results-log.md` used the **gross** taker fee and therefore overstates
cost. At $2,000 the tier is low so the effect is small — but over-stating cost hides real
edges, which is the wrong error to leave in.
