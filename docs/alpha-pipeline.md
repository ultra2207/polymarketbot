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

## Parked, and why

| Idea | Reason |
|---|---|
| #22 market making | queue position is unobtainable — structural, not a data gap |
| #23 order-flow toxicity | needs individual order events, which the operator never publishes |
| #24 single-wallet copy trading | fails by construction: latency + impact + exit into copy flow |
| #21 comment sentiment | no historical record exists; forward-only, needs ≥ 3 months of recording |

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
