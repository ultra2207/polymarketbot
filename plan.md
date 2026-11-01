# Plan

The research agenda. `roadmap.md` is *how we build the machine*; this is *what we point it at*.

Two tracks run in parallel once the data layer exists:

- **Track A — understand the competition.** Reverse-engineer what profitable wallets
  actually do, and classify which parts are replicable. Detail:
  [`docs/strategy-reverse-engineering.md`](docs/strategy-reverse-engineering.md).
- **Track B — find the whitespace.** Score segments on edge × capacity × novelty against
  cost × competition, and only then write a strategy.

The output of A is mostly *negative*: a list of segments and behaviours to avoid. That is
valuable, because it removes most of the search space before any modelling.

---

## 0. Thesis

Three claims this plan is built on:

1. **Copying fails** for four reasons — latency, information asymmetry, size dependence,
   and exit-into-copy-flow. So the unit of analysis is the *behavioural archetype*, not the
   individual wallet.
2. **Absence of large traders is ambiguous.** It can mean neglect (opportunity) or
   uninhabitability (no exit, no depth, ambiguous resolution). Every candidate segment must
   pass an exit-depth and capacity check before it is called whitespace.
3. **Edges here are capacity-constrained.** A prediction-market edge that survives at
   \$500/order may vanish at \$10k. The honest deliverable is a capacity curve, not a Sharpe
   ratio. Small books can compound these; large books cannot. Say so early.

What "winning" means for this phase: **2–3 validated, uncrowded, capacity-checked edges**,
not one impressive backtest.

---

## 1. Idea backlog

Tiered by what data they need. Every row has an explicit kill condition, because the point
of a backlog is to delete things cheaply.

### Tier 1 — testable with trade tape + metadata + resolution

| # | Idea | Why the edge might exist | First test | Kill if |
|---|---|---|---|---|
| 1 | **Longshot / favourite bias** | Betting markets systematically overprice longshots and underprice favourites. Prediction markets are not immune | Calibration by price bucket over 2024–2026; PnL of "sell < 0.05, buy > 0.90, hold to resolution" | Edge < 2× fees, or disappears after 2026 fee overlay |
| 2 | **Coherence arbitrage in neg-risk baskets** | Mutually exclusive outcomes should sum to 1. Legs are priced independently and can drift out of line | For each neg-risk event, compute Σ mid across legs; count and size violations beyond fees | Violations < fees, or only in markets with no depth |
| 3 | **Ladder monotonicity violations** | "BTC above 100k / 110k / 120k" must be monotone in strike. Independent pricing breaks it | Detect inversions across threshold ladders; measure persistence and executable size | Inversions are tiny, transient, or only at 1-share size |
| 4 | **Lifecycle-stage inefficiency** | New markets have thin attention. Price discovery takes time; early prices may be stale or anchored | Calibration error as a function of market age (first hour / day / week) | Error is uniform across age → no lifecycle edge |
| 5 | **Term-structure drift** | Probability paths to resolution may be systematically biased (e.g. favourites drift up too slowly) | Bin by (price, time-to-resolution); measure drift to terminal | No monotone pattern, or within spread |
| 6 | **Resolution-risk premium** | Ambiguous rules and UMA dispute risk are hard to price; most traders ignore them | Dispute rate by category; compare entry price vs realised payout conditional on dispute | No measurable discount, or too few disputes to trade |
| 7 | **Skilled-wallet consensus flow** | Aggregate net flow of *ranked* wallets may predict drift even when copying any single one fails | Build a lagged flow feature from top-decile wallets; test against matched control | No incremental predictive power over price |
| 8 | **Cross-market lead-lag** | Related markets (nomination → general, crypto → updown) may propagate information at different speeds | Lead-lag correlation with lagged returns; test tradability after costs | Lead is < cost, or explained by the reference asset |
| 9 | **Base-rate models for recurring markets** | Temperature ranges, sports totals, and similar markets recur with stable distributions | Fit historical base rates; compare Brier to market price | Model does not beat market Brier by a usable margin |
| 10 | **Crypto up/down vs reference price** | 5-min/15-min markets should track a Chainlink TWAP; short-horizon mispricing may exist away from expiry | Compare implied probability to a model from Binance/Chainlink history; measure error by seconds-to-close | Error is inside spread, or only exploitable with sub-second latency |
| 11 | **Fee-aware category selection** | Identical gross edge nets very differently: geopolitical markets are fee-free, crypto peaks at 1.56 % | Re-score all candidate strategies under per-category fee schedules | Ranking is unchanged by fees (then this is not an edge source) |
| 12 | **Time-of-day / weekend effects** | Attention and maker presence vary; mispricing may cluster when the book is thin | Calibration error by hour-of-day and weekday | Effect is a proxy for liquidity only, with no residual edge |

### Tier 2 — need the L2 window (2026-04-13 → 2026-08-10)

| # | Idea | Why the edge might exist | First test | Kill if |
|---|---|---|---|---|
| 13 | **Book imbalance predicts short-horizon returns** | Standard microstructure effect; untested on Polymarket | Imbalance at t → return over t+1m/5m/1h | No effect after spread |
| 14 | **Signed taker flow / price impact** | Aggressor imbalance may persist | Regress returns on signed volume | Effect only at sizes we can't trade |
| 15 | **Stale-quote pickoff** | Resolved and near-resolved markets keep resting orders; ~23 % of resolved markets had zero book in the final hour | Detect fills against stale levels; net of fees | Stale fills are rare or immediately arbitraged |
| 16 | **Execution scheduling** | Large orders should be sliced relative to depth, not time | Compare TWAP-by-time vs TWAP-by-depth on the same signal | Depth-aware scheduling does not reduce slippage |
| 17 | **Cancellation-pressure proxy** | Book delta minus fills gives net adds/cancels; extreme cancel pressure may signal withdrawal | Build the residual series; test predictive power | Residual is noise |

### Tier 3 — need external data or forward recording

| # | Idea | Why the edge might exist | First test | Kill if |
|---|---|---|---|---|
| 18 | **News-latency strategy** | Prices may react to public news with a delay | GDELT / RSS event timestamps vs price jumps; measure lag | Lag < cost, or only for machine-readable feeds everyone has |
| 19 | **Cross-venue arbitrage vs Kalshi** | Same event, two venues, different clienteles | Match events semantically; compare prices net of fees and transfer friction | Spreads are inside combined costs |
| 20 | **Attention proxies** | Wikipedia pageviews / search trends may lead price | Correlate lagged attention with returns | Attention is contemporaneous, not leading |
| 21 | **Comment sentiment** | Crowd discussion may contain signal | Requires forward recording (§ `docs/limitations.md`) | Parked until the recorder has ≥ 3 months |

### Parked — structurally unavailable, do not revisit

| # | Idea | Why it is parked |
|---|---|---|
| 22 | Market making | Needs queue position (unobtainable) |
| 23 | Order-flow toxicity / spoof detection | Needs individual order events (unobtainable) |
| 24 | Single-wallet copy trading | Latency + impact + exit-into-copy; fails by construction |

### New from the docs (2026-11-01)

The mirrored documentation revealed data surfaces the original backlog assumed were
unavailable. These four ideas are only testable because of that:

| # | Idea | Why the edge might exist | First test | Kill if |
|---|---|---|---|---|
| 25 | **Scalar-market anchoring** | Range markets ("what will the price of X be on date?") pay a *fraction* between 0 and 1, but traders may price them like binaries or anchor at 0.5 | 64 resolved binary conditions pay non-0/1; compare price to realised payout fraction; check calibration by price bucket | Too few markets to size, or calibration beats price by < 2× fees |
| 26 | **Resolution-timing premium** | A market stops trading at resolution, but UMA takes ~2 h undisputed and 4–6 days if disputed. Capital is locked, and dispute risk is hard to price | Use `/v2/resolutions` `resolved_at`/`was_disputed` + tape; measure price drift between `end_date` and resolution. **First 400-market sample (2026-05+): sports resolve +4.1 h median (p90 +5.8 h) after `end_date`; crypto ~0 h; 0 disputes.** So the locked-capital story holds for sports only | No measurable discount, or no tradable window (trading may already be closed) |
| 27 | **Conversion-bounded neg-risk arbitrage** | Docs confirm 1 NO converts into 1 YES in every other outcome. Ignoring conversion overstates arbitrage; including it creates tighter, executable bounds | Recompute neg-risk basket violations with conversion cost + fees included | Violations inside conversion + fee cost |
| 28 | **Leaderboard-cohort flow** | `/v2/leaderboard` and `/v2/biggest-winners` give ranked wallets cheaply, so a cohort can be defined without reconstructing every wallet | Aggregate net flow of leaderboard wallets; test against matched control | No incremental predictive power over price (same kill as #7, cheaper test) |

---

## 2. What I would run first, and why

If only five things get investigated, these:

1. **Coherence and ladder arbitrage (#2, #3).** The only ideas here that need *no*
   information edge — just arithmetic on prices that must obey constraints. Objective,
   cheap to test, and the failure mode is clean (either violations exceed fees or they
   don't). Capacity is limited, which is exactly the "where big traders aren't" thesis.
2. **Longshot bias (#1).** The most robust prior in betting markets. Pure tape + resolution,
   no depth needed, testable immediately. The catch is tail risk: selling longshots has a
   fat left tail, so size it as a portfolio, not a trade.
3. **Lifecycle-stage inefficiency (#4).** Directly targets neglect rather than competition.
   If new markets are mispriced for the first few hours, that is a repeatable, uncrowded
   window that does not require being faster than anyone.
4. **Objective-data markets (#9).** Weather, econ releases, sports totals. Public data, low
   crowding, clear base rates. Small capacity, but a genuine information edge that isn't a
   race.
5. **Skilled-wallet consensus flow (#7).** The correct way to use the reverse-engineering
   output: not "follow wallet X" but "aggregate the net flow of wallets that have survived
   walk-forward ranking". Uses the Track A work without inheriting its latency problem.

Everything else is a candidate, not a commitment.

---

## 3. Sequencing

```
Phase 3 (positions/PnL)  ──►  Phase 6b (landscape)  ──►  screen Tier 1 ideas
                                        │                        │
                                        ▼                        ▼
                              wallet_skill, archetypes    2–3 survive screening
                              crowding, whitespace                │
                                                                  ▼
                                                    Phase 7 strategy loops (full harness)
                                                                  │
                                                                  ▼
                                                          Phase 8 forward test
```

Screening is deliberately cheap: for each idea, answer three questions before writing a
model —

1. **Does the effect exist at all?** Raw frequency × size of the violation/mispricing.
2. **Does it survive costs?** Fees + half-spread + modelled impact.
3. **Is there capacity?** Depth at target notional in the relevant window.

An idea that fails (1) is deleted. One that fails (2) is re-tested under the no-fee regime to
confirm it was a fee artifact. One that fails (3) is logged as "real but untradeable" —
which is still useful, because it explains why the segment is uncrowded.

---

## 4. How ideas die

Pre-registered, before any modelling:

| Gate | Question | Fail action |
|---|---|---|
| **Prior** | Why would this mispricing survive? | If there is no mechanism, stop |
| **Data** | Can it be measured with data we hold? | If no, park it — don't approximate silently |
| **Effect** | Is the raw effect larger than 2× round-trip cost? | Delete |
| **Capacity** | Is depth at target notional ≥ 3× our size? | Log as untradeable |
| **Robustness** | Does it survive 2× impact and the 2026 fee overlay? | Delete |
| **Crowding** | Is top-10 wallet share < 60 %, or is our edge structurally different? | Delete or re-scope |
| **Stability** | Does it hold out-of-sample across two periods? | Delete |

Anything that survives all seven graduates to a full backtest. Most will not.

---

## 5. Structural bets worth revisiting later

Not in the backlog yet, but worth keeping on the radar:

- **Resolution-risk as a product, not a trade.** If UMA disputes are systematically
  mispriced, the edge may be in *providing* certainty (selling insurance) rather than
  taking a directional view.
- **Cross-venue market making.** If Kalshi and Polymarket quote the same event, the
  spread between them is a structural edge that does not require prediction — but it does
  require capital on both venues and transfer friction modelling.
- **Event-calendar specialisation.** A calendar of scheduled information (elections, CPI,
  Fed, fixtures, weather releases) lets you be *present* exactly when mispricing is created,
  instead of scanning continuously. Cheap to build; free sources.
- **Decay monitoring as a first-class product.** Any edge found should ship with a monitor
  that tells you when it has been competed away. Without it, you find out via PnL.
- **Portfolio construction for correlated legs.** Neg-risk baskets are anti-correlated
  positions, not independent bets. A proper event-level risk model may be worth more than
  another signal.

---

## 6. What would make me abandon this whole plan

Stated up front so it isn't rationalised later:

- If **Track A** finds that essentially all durable PnL comes from latency, market making, or
  private information, then there is no replicable directional edge for us, and the project
  should pivot to infrastructure/data rather than trading.
- If **Tier 1 screening** finds no effect exceeding 2× costs in any segment, the market is
  efficient enough at our size and horizon, and we should stop rather than lower the bar.
- If everything that survives is capacity-constrained below the capital we need, the honest
  answer is that this is a research asset, not a trading business.
