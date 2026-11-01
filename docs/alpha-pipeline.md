# Alpha pipeline — what gets tested, in what order

`plan.md` holds the full backlog (ideas #1–#28). This is the *queue*: what is next,
what blocks it, and what would kill it. Two things learned on 2026-11-01 reshape the
order:

- **The median quoted spread is 2 ¢.** Round-trip cost is therefore ~2 ¢ per share
  before fees. Any edge smaller than that is unmeasurable and untradable at our size.
  This single fact deletes most of the small-mispricing backlog.
- **Exits are chosen by the system, not assumed.** An idea that only works while
  holding to resolution is not an edge, it is an assumption. Anything that needs to
  be exited cannot be graduated until the book replayer exists.

Costs degrade the measurement, so every claim below is a *gross* figure until the
ask-side version is run.

---

## Tier 0 — runnable now (tape + labels + `schedule_at` fees)

### A1. Executable-entry re-test of idea #1 (favourite/longshot)
**Edge story:** the only idea with a measured effect (+3.4 pp at 0.80–0.90, t ≈ 11).
**First test:** entry = fills where the taker *bought* (so the price paid was the ask),
T−24 h → T−6 h before resolution; hold. Already implemented as `--side ask`.
**Kill if:** the ask-side gap falls below the 2026 fee plus observed spread.
**Why it matters:** it is the cheapest possible test of whether half-spread, not
signal, was the whole story. Expected outcome: killed, at ~1 pp.

### A2. Neg-risk coherence with conversion bounds (#2 + #27)
**Edge story:** outcomes of a mutually exclusive event must sum to 1. Legs are priced
independently and drift. Negative-risk conversion (1 NO → 1 YES in every other leg)
sets hard bounds; ignoring conversion overstates the opportunity.
**First test:** for each neg-risk event, reconstruct simultaneous leg prices from the
tape (last trade before each timestamp), compute Σ mid, and count violations that
exceed fees plus conversion cost, by size available.
**Kill if:** violations are inside cost, or occur only in events with no depth.
**Strength:** arithmetic, not information. No speed requirement if violations persist.

### A3. Ladder monotonicity (#3)
**Edge story:** "BTC above 100k / 110k / 120k" must be monotone in strike. Independent
pricing breaks it.
**First test:** detect inversions across strike ladders in the same event; measure
persistence and the size that could clear at the observed prices.
**Kill if:** inversions are ≤ 1 tick, transient (< 1 min), or only at 1-share size.

### A4. Scalar / range-market anchoring (#25)
**Edge story:** 64 resolved binary conditions pay a *fraction*; traders may price them
like binaries.
**First test:** calibration by price bucket for fractional-payout markets only, versus
the realised payout fraction.
**Kill if:** too few markets to size, or calibration beats price by < 2× fees.

### A5. Lifecycle-stage inefficiency (#4)
**Edge story:** new markets have thin attention; early prices are stale or anchored.
**First test:** calibration error as a function of time-to-resolution and market age,
reusing the screen's scored rows (already stored per quarter, no rescan).
**Kill if:** error is flat across age — then there is no lifecycle window.

### A6. Resolution-timing premium (#26)
**Edge story:** sports resolve a median +4.1 h (p90 +5.8 h) after `end_date`; capital is
locked and dispute risk is unpriceable. Crypto resolves ~0 h, 0 disputes.
**First test:** price drift between `end_date` and `resolved_at`, by category, with the
tradability of that window checked (trading may already be closed).
**Kill if:** the window is not tradable, or no discount is observable.

### A7. Term-structure drift (#5)
**Edge story:** favourites may drift upward too slowly as resolution approaches.
**First test:** drift to terminal by (price bucket × hours-to-resolution).
**Kill if:** no monotone pattern, or the drift is inside the spread.

---

## Tier 1 — needs the PMXT book (2026-04-13 → 2026-08-10)

### B1. Exit-policy comparison — the structural bet
**Edge story:** the largest single lever is *when to leave*, not what to enter. Every
entry gets five counterfactuals on identical fills: hold, taker exit, limit exit, stop,
and early-exit-on-signal. The last is the only one that can beat holding without being
faster than the market.
**First test:** per entry, PnL under all five; report the distribution of the *best*
policy and how often hold-to-resolution wins. If holding wins most of the time, the
user's suspicion is wrong and we learn it cheaply.
**Kill if:** holding dominates, or every alternative exit needs fills we cannot get.

### B2. Stale-quote pickoff (#15)
**Edge story:** ~23 % of resolved markets had no book in their final hour, yet resting
orders existed. Fills against stale levels are a structural transfer.
**First test:** detect fills at prices outside the reconstructed touch; net of fees.
**Kill if:** stale fills are rare or only from other latency bots.

### B3. Book imbalance → short-horizon returns (#13)
**Edge story:** standard microstructure effect, untested here.
**First test:** imbalance at t → return over t+1 m / 5 m / 1 h, net of spread.
**Kill if:** no effect beyond the spread, which is the likely outcome at 2 ¢.

### B4. Depth-aware execution scheduling (#16)
**Edge story:** slicing by depth beats slicing by time for anything above one level.
**First test:** TWAP-by-time vs TWAP-by-depth on identical signals.
**Kill if:** depth-aware scheduling does not reduce slippage at our size.

### B5. Capacity curve for every survivor
Not an idea — a gate. Any Tier-0 survivor must be plotted as PnL vs notional, with the
point where net edge → 0 stated explicitly.

---

## Tier 2 — needs an external series or a long recording

### C1. Crypto up/down vs reference price (#10)
18.8 % of 2026 volume, resolves on a Chainlink TWAP. Needs free Binance klines first.
5-minute horizons make this a latency game; at $250 it may be untradable, but the
mispricing magnitude is worth one measurement.

### C2. Base-rate models for recurring markets (#9)
Weather ranges, sports totals, econ releases. Public data, stable distributions, clear
base rates. Small capacity, genuine information edge, not a race.

### C3. Skilled-wallet consensus flow (#7 + #28)
Aggregate net flow of walk-forward-ranked wallets. Uses `/v2/leaderboard` rather than
the 60 GB `users.parquet`. Needs the wallet identity map (Gate 1b).

### C4. Cross-venue arbitrage vs Kalshi (#19)
Same event, two venues. Needs Kalshi access and transfer-friction modelling.

### C5. News latency (#18)
Needs an event feed with timestamps (GDELT/RSS). Measured lag must exceed cost.

---

## Corrected: these were parked for the wrong reason

The first draft of this list parked four ideas on *data unavailability*. That conflated
"the raw variable is unobtainable" with "the economics are unmeasurable". Three of the
four have a measurable route.

### D1. Liquidity rewards — promoted from parked to Tier 0 (forward-only)

**What was wrong:** I parked market making because queue position is unobtainable. That
is true and it blocks **fill-dependent** maker PnL. But Polymarket pays makers on a
different basis, and that basis is observable.

* **Liquidity Rewards** pay daily at 00:00 UTC to makers whose resting orders meet each
  market's `rewardsMinSize` and sit inside its `rewardsMaxSpread`. Eligibility is a
  function of *quoting*, not of being filled. Share of the pool depends on qualifying
  liquidity, which the aggregate book already reports. So the expected reward is
  estimable from data we hold; queue position is irrelevant to accrual.
* **Maker Rebates** pay a share of collected taker fees on liquidity that actually got
  taken. This one is fill-dependent and therefore inherits the queue-position limit —
  but it is the smaller, optional second leg.

**Measured 2026-11-01** (400 open markets, Gamma): 27 % carry an active `clobRewards`
pool, summing **$23,993/day**, mean **$224/day** per incentivized market.

**Data gap:** our Gamma capture never collected `clobRewards`, `rewardsDailyRate`,
`rewardsMinSize`, `rewardsMaxSpread`, `holdingRewardsEnabled` — all present in the
schema. Adding them is cheap. Only **forward** history is recoverable, which is fine:
this is a strategy about where to quote *now*, not a backtest of the past.

**First test:** for the markets we can quote, compute (our size / total qualifying size)
× `rewardsDailyRate`, minus inventory risk and adverse selection on the fills we do get.
**Kill if:** per-unit-capital yield after adverse selection is below the same capital
deployed in a directional idea — or if the qualifying size required exceeds our $250.

### D2. Order-flow toxicity — proxy is live, direct measure is not

Individual order placement/cancellation is unpublished, so order-to-trade ratios and
spoof attribution are unavailable. But **book delta minus fills gives a net
adds/cancels residual**, which is already idea #17 and is computable from PMXT. It cannot
attribute a cancel to a wallet; it can say the book is withdrawing. That is enough to
test whether extreme cancel pressure predicts anything.

### D3. Single-wallet copy trading — re-scoped, not parked

It fails for latency-sensitive wallets (latency, size dependence, exit into copy flow).
It does **not** necessarily fail for a slow fundamental wallet holding for weeks: days of
lag are then tolerable. That case is really "aggregate skill", so it folds into #7/#28
consensus flow rather than disappearing. A single-wallet variant should be retested on
the `directional` archetype from the wallet cohort, with the lag explicitly modelled.

### D4. Comment sentiment — the source is dead, the hypothesis is not

Comments are stored off-chain with no bulk export and the read endpoint is current-state
only, so no as-of record exists. But the hypothesis ("crowd discussion leads price") is
testable with substitutes: GDELT, RSS, Reddit. That is idea #18 and it needs no
Polymarket comments at all.

### Genuinely parked

| Idea | Reason |
|---|---|
| Fill-dependent market making (#22) | queue position exists only inside the operator's matching engine; the public book aggregates size by price and discards arrival order. Use D1 instead, which does not depend on fills. |

---

## Cost model correction: taker rebates

The **Taker Rebate Program** (live 2026-05-28) returns part of taker fees by tier, with
weighted volume `wV = Size × (1 − Entry Price) × Category Weight × Bonuses`. Every "dead
after fees" conclusion in `docs/results-log.md` used the **gross** taker fee and therefore
overstates cost. At a $2,000 bankroll the tier will be low, so the correction is small —
but it is the wrong direction to leave in, because over-stating cost hides real edges.

## New ideas surfaced by the docs, not previously on the backlog

These are not in `plan.md` (ideas #1–28). Each comes from a documented mechanic rather
than from a market-efficiency story, which makes them structural: they do not require
being right about anything, only being present in the right state.

### E1. Within-market complete-set arbitrage
**Mechanism.** A binary market's two tokens always merge back into \$1. If
`ask(YES) + ask(NO) < 1`, buying both and merging returns \$1 for less than \$1 spent.
No forecast, no information, no speed requirement beyond noticing.
**Why it can exist.** The two books are quoted independently, and a 2 ¢ spread normally
keeps the sum at or above 1. It breaks when one side is mispriced in a thin book.
**The catch that decides it: fees.** This is a taker trade, so the taker fee applies to
both legs. Crypto peaks at 1.75 % of notional at the mid — that kills any 1–2 ¢ edge
outright. Geopolitics is fee-free, so the same 1 ¢ edge survives there. **Category
selection is the whole strategy.**
**First test:** from the tape, find timestamps where the last YES trade + last NO trade
imply a sum below 1 net of the category's taker fee, and measure how long the state
persists. Execution confirmation needs the book.
**Kill if:** sub-1 sums only occur where the fee exceeds the gap, or persist for under a
second.

### E2. Fee-holiday windows
**Mechanism.** The docs confirm markets are "temporarily made fee-free during a fee
holiday". Every idea killed by the 2026 fee overlay comes back to life at zero fee.
**Why it is cheap.** We already snapshot Gamma daily, so `feesEnabled` and `feeType`
transitions are recordable from now on. This is a monitor, not a model.
**First test:** measure how often `feesEnabled` flips and how long holidays last; then
re-run the killed screens restricted to holiday windows.
**Kill if:** holidays are rare, short, or confined to markets with no depth.

### E3. Venue restriction states: post-only, cancel-only, restart
**Mechanism.** The API distinguishes *matching-engine restart*, *cancel-only* and
*post-only* restrictions, and returns `retry_after_seconds`. During **post-only**, takers
cannot cross, so a resting maker is protected from being picked off. During
**cancel-only**, the book can only thin, never deepen.
**Why it can pay.** If a restriction window overlaps a news event, the book cannot reprice
through takers; whoever is already resting gets the first move. This is a genuine
structural edge and it needs no forecasting.
**First test:** log restriction states from the live API alongside the book; measure price
behaviour across the window. Forward-only.
**Kill if:** windows are too rare to trade, or makers flatten rather than benefit.

### E4. Taker-delay asymmetry as a maker-protection measure
**Mechanism.** Crypto markets apply a taker delay — **250 ms → 50 ms (2026-08-17) →
150 ms (2026-09-04)** — exposed per market as `takerDelayMs`. A longer delay gives a
resting quote a head start before an aggressor's order completes.
**Why it can pay.** It ranks markets by how safe passive quoting is there, which is
exactly the input a reward-farming or quoting strategy needs. Combine with E5.
**First test:** does realised maker adverse selection fall with `takerDelayMs` across
markets? Needs the book plus per-market delay.
**Kill if:** the delay is uniform in practice, or adverse selection is dominated by
informed takers rather than by latency.

### E5. Liquidity-reward rotation (yield surface)
**Mechanism.** Extend D1 from "can we earn rewards" to "where is the reward per unit of
capital highest". `ratePerDay` is per market; your share is proportional to qualifying
size, so the yield is `ratePerDay / (total qualifying size × capital tied up)`.
**Why it can pay.** New or neglected markets can have a high rate against thin
competition. This is a rotation problem, and small capital is an advantage because the
pro-rata share is not diluted by our own size.
**First test:** build the yield surface from live Gamma `clobRewards` + book-implied
qualifying size; rank markets.
**Kill if:** qualifying size required exceeds our \$250, or the yield is below the
inventory risk it carries.

### E6. Perps venue: funding carry and liquidation capture
**Mechanism.** The docs include a full perps product — funding, mark price, index price,
liquidation mechanics, ADL, 20 ms taker delay, its own liquidity rewards. Two structural
strategies need no forecast: **funding carry** (hold the leg that is paid, hedge the
other) and **liquidation-cascade capture** (liquidations are mechanical and create
predictable impact).
**Why it is worth a look.** Funding is a contractual cashflow, not a prediction, and small
size can sit inside a cascade without moving it.
**First test:** measure funding-rate dispersion versus another venue and the size of
liquidation bursts.
**Kill if:** funding is near-zero on average, or cascades are too shallow to trade.

### E7. Mechanism transplantation (Track A, redefined)
Find algorithmic wallets, infer the predicate they run, and deploy it where their coverage
does not reach. Full design — including the edge-half-life discriminator that decides
whether any of it is transplantable — in
[`strategy-reverse-engineering.md` §1.5](strategy-reverse-engineering.md).

---

## The global filter

Before any of this is called an edge it must clear, in order:

1. **Effect** — gross edge > 2× round-trip cost (≈ 2 ¢ spread + fee).
2. **Executability** — measured at the ask, not at a VWAP.
3. **Exit** — survives whichever exit the system picks, including the honest cost of
   getting out.
4. **Capacity** — depth at $250 must be ≥ 3× the order; at our size this is usually
   satisfied, which is the whole argument for starting small.
5. **Crowding** — top-10 wallet share < 60 %, or our edge is structurally different.
6. **Stability** — holds out-of-sample across two periods.
