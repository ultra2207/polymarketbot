# Results log

Every screen, including the failures. The point is to stop re-testing the same idea
and to keep the record of what was actually measured, on what window, at what precision.

Format: idea → window → sample → result → verdict → what would change it.

---

## #1 Longshot / favourite bias — **no edge after costs** (2026-11-01)

**Window:** markets created 2024-01-01 → 2026-03-31; entry at T−24 h → T−6 h before
resolution; hold to resolution; on-chain payout labels; fees via `schedule_at`.

**Sample:** 152,895 token-entries across 8 quarters (path
`data/processed/screen_calibration_parts/entry=T-24h..T-6h/`).

| Entry bucket | n | mean price | mean payout | net edge/share | t | 2026 fee |
|---|---|---|---|---|---|---|
| 0.00–0.05 | 6,463 | 0.019 | 0.013 | −0.0066 | −4.8 | 0.16 % |
| 0.05–0.10 | 3,881 | 0.075 | 0.051 | −0.0239 | −6.8 | 0.18 % |
| 0.10–0.20 | 8,509 | 0.152 | 0.128 | −0.0241 | −6.7 | 0.19 % |
| 0.20–0.35 | 17,831 | 0.278 | 0.256 | −0.0229 | −7.1 | 0.18 % |
| 0.35–0.50 | 31,778 | 0.445 | 0.445 | −0.0008 | −0.3 | 0.33 % |
| 0.50–0.65 | 29,098 | 0.558 | 0.569 | +0.0093 | +3.2 | 0.25 % |
| 0.65–0.80 | 20,552 | 0.723 | 0.754 | +0.0307 | +10.3 | 0.07 % |
| 0.80–0.90 | 10,654 | 0.849 | 0.884 | +0.0343 | +11.1 | 0.03 % |
| 0.90–0.95 | 5,915 | 0.926 | 0.955 | +0.0287 | +10.7 | 0.02 % |
| 0.95–0.98 | 6,207 | 0.967 | 0.983 | +0.0156 | +9.4 | 0.00 % |
| 0.98–1.00 | 17,007 | 0.994 | 0.997 | +0.0032 | +7.3 | 0.00 % |

**Verdict: dead after costs.** The bias is real and strongly significant (t ≈ 7–11),
about 2–3 pp per share on favourites and −2.3 pp on longshots. But:

- the entry is a **VWAP of executed trades**, i.e. roughly mid. A taker pays about half
  the quoted spread, and the median quoted spread is **2 ¢** — ~1.2 pp of notional on a
  0.85 favourite. That consumes most of the +3.4 pp.
- fees were ~0 for most of the sample. In 2026-Q1, the only era with fees, the effect had
  already shrunk to **+1.2 pp** on favourites against ~0.8 % in fees.
- standard errors here are `sd/√n` and ignore that the two tokens of a market are perfectly
  anti-correlated, so the true interval is wider than printed.

**What would change the verdict:** measuring the *quoted ask* at entry rather than the VWAP
(needs the book, i.e. 2026-04-13 onward), and a 2026-only window. If the ask-side version
still clears fees plus half-spread, the idea comes back — but at that point it is a
capacity-limited 1 pp trade, not a franchise.

### Correction recorded, because it nearly went the other way

The first version of this screen used entry = VWAP of the market's **first 48 h**, and
reported a +7.6 pp gap at 0.80–0.90 and −21.6 pp at 0.20–0.35 — twenty times larger. That was
an artefact: for the median 24-hour market, "first 48 h" is the *entire life*, and averaging
a price path that terminates at 0 or 1 pulls the average toward 0.5, so the realised 0/1 is
mechanically more extreme than the measured entry. The tell was that the gap was exactly
antisymmetric around 0.5 and cancelled to ~0 in the aggregate (crypto: −0.187 low bucket,
+0.203 high bucket, +0.015 overall).

**Rule that follows:** an entry price must be a point-in-time price with resolution still
ahead of it. Never average across a window that contains the terminal price.

---

## Track A framing — wallet cohort built, and what it already says (2026-11-01)

**Source:** `/v2/leaderboard` (30 pages) + `/v2/biggest-winners` (5 pages) + one
`/v2/user-stats` per wallet. **3000 wallets**, no null PnL, no duplicates.

| Archetype | n | realized PnL | rebates | rebate share | median trades |
|---|---|---|---|---|---|
| directional | 2,331 | $62.8 M | $6.0 M | **9.5 %** | 338 |
| maker | 564 | $26.4 M | $17.9 M | **67.7 %** | 1,772 |
| combo | 82 | $4.2 M | $0.0 M | 0.2 % | 420 |
| unknown | 23 | — | — | — | 2 |

**What this changes.** The maker archetype is not primarily a trading strategy: **two
thirds of its realised PnL is maker rebate**, not price movement. That is a fee-structure
artefact available only to whoever the rebate schedule pays, and it is not replicable by a
directional strategy at any size we could reach. Any "market making looks profitable, let's
do that" inference from leaderboard data is therefore measuring the rebate schedule, not
skill.

Directional PnL — the only kind we could in principle replicate — is $62.8 M spread across
2,331 wallets, i.e. a mean of ~$27 k each, while the top few carry most of it. The
distribution matters more than the mean; it is the input to Track A.

**Caveat that makes this unusable as a feature:** the boards rank by *today's* PnL, so the
cohort is survivorship-biased and not point-in-time. It is a list of whom to study, never a
score to put in a backtest.

**Next:** decompose the directional cohort's PnL by market family and holding time, then
ask which parts are reachable at our size. Combo wallets are the anomaly worth one look —
one has rank 1222 with $2.36 M realized against $231 of market PnL.

---

## C2 (free half) Base-rate models — **market price wins, decisively** (2026-11-01)

**Window:** all resolved binary markets; entry VWAP for the T−24 h → T−6 h slot.
**Sample:** 350 recurring families (≥ 20 resolved instances), 20,652 priced token-observations.
**Method:** `scripts/screen_base_rates.py`. Family = slug with its date suffix stripped.

| Forecaster | Brier |
|---|---|
| **market price** | **0.0875** |
| family base rate | 0.1843 |
| 50/50 blend | 0.1127 |

**Verdict: dead.** A constant family base rate is roughly *twice* the Brier loss of the
market price, and blending it in makes the forecast **worse** than the price alone
(0.1127 vs 0.0875). The market already knows its own history. Mean |base rate − price| is
23.8 ¢, an order of magnitude above the ~2 ¢ round trip, but that distance is the base
rate being wrong, not the market.

**A bug worth recording, because it produced a beautiful fake result.** The first version
computed the base rate over *both tokens* of each market. Every binary market resolves one
YES and one NO, so the mean payout over both tokens is exactly 0.500 for every family, and
the screen printed 0.500 for all 350. It still "ran" and produced a full table. Two tokens
of one market are not two observations.

**What would change the verdict:** a *conditional* base rate — the family rate given the
regime (e.g. distance from the strike, previous day's move), i.e. a model rather than a
prior. That is a different, larger project and no longer "the cheapest idea in the backlog".

---

## E1 Complete-set arbitrage — **candidates exist, but none persist** (2026-11-01)

**Window:** 2026-04-01 → 2026-07-01 on the trade tape.
**Sample:** 42,806 quotable minute-cells (both legs printed ≥ $25 and priced inside
0.02–0.98); 2,022 with a positive net edge after fees.

| Category | cells | net > 0 | net > 0.5 c | best net | median $/cell |
|---|---|---|---|---|---|
| sports | 27,766 | 2,022 | 1,769 | 0.550 | 198 |
| geopolitics | 7,328 | 379 | 283 | 0.200 | 187 |
| politics | 3,781 | 67 | 55 | 0.272 | 187 |
| crypto | 2,033 | 128 | 90 | 0.103 | 146 |
| other+ | 1,898 | 65 | 42 | 0.098 | 120 |

**Verdict: unconfirmed, and the persistence says no.** Of 1,769 cells exceeding the
threshold, only **16 lasted 2 consecutive minutes** and **none lasted longer**. A 55 %
round-trip edge that cannot survive 60 seconds is not an edge; it is a stale last-trade
price on one leg.

The first naive pass claimed 12.8 % of all minute-buckets were arbitrage with a minimum
sum of 0.011 — dust prints of $0.31–$5.79 at prices like (0.001, 0.010). Both filters now
applied (≥ $25 per leg, both legs inside 0.02–0.98) removed that entire class.

**What this establishes:** the tape alone cannot settle E1. Last-traded price is only a
lower bound on the ask, and it goes stale exactly where arbitrage would live. The question
is decided by the book, so E1 moves to B-series once the reduced book exists — this screen
now supplies the candidate list to check against it.

**Category selection still matters** (the plan's point): crypto carries the 1.75 % peak fee,
geopolitics is fee-free, and the fee overlay is what kills a 1–2 ¢ gap.

---

## A2 Neg-risk coherence — **no persistent violation found** (2026-11-01)

**Window:** 2026-04-01 → 2026-07-01. **Sample:** 522,059 legs across 82,949 neg-risk events;
only **487 complete snapshots** where every leg of an event printed inside the same 5-minute
window with ≥ $25.

| Side | snapshots | gross > 0 | net > 0.5 c | best gross |
|---|---|---|---|---|
| buy-all-NO | 387 | 84 | 24 | 0.140 |
| buy-all-YES | 100 | 18 | 6 | 0.060 |

**Verdict: no persistent violation.** Every candidate is an isolated 5-minute cell; **zero**
runs lasted 2 or more windows. Largest net was +0.12 on a 3-leg event, with nothing above
+0.06 after that.

**The honest weakness of this test.** Requiring every leg to quote in the same window is
necessary — a leg you cannot buy is not arbitrage — but it restricts the sample to the
*liquid* subset of neg-risk events, and 487 snapshots over three months is thin. Violations,
if they exist, are most likely in the illiquid events this filter excludes. So the correct
reading is narrow: **no violation among liquid, fully-quoted neg-risk events**, not "no
violation exists". Settling the wider question needs the book, where resting depth is
visible without a trade printing.

**Two methodology notes.** A 1-minute window was tried first and produced only 742
snapshots for the same reason; 5 minutes is a compromise between freshness and coverage.
And the buy-all-NO bound (`sum < k-1`, not `sum < 1`) is the one that actually binds —
the YES side showed just 6 net-positive cells against 24 for NO.

---

## A5 Lifecycle-stage inefficiency — **dead: not monotone, and inside the spread** (2026-11-01)

**Sample:** 157,240 token-observations with a resolvable age at entry, across 101,585
markets, entry in the stored T−24 h → T−6 h slot.

| Age at entry | markets | mean px | payout | calib gap | ±se | net edge |
|---|---|---|---|---|---|---|
| < 1 d | 11,193 | 0.522 | 0.524 | +0.0024 | 0.0027 | +0.0008 |
| 1–3 d | 21,868 | 0.604 | 0.613 | +0.0087 | 0.0016 | +0.0072 |
| 3–7 d | 34,133 | 0.648 | 0.654 | +0.0068 | 0.0010 | +0.0063 |
| 7–30 d | 30,172 | 0.569 | 0.575 | +0.0053 | 0.0015 | +0.0049 |
| > 30 d | 4,219 | 0.737 | 0.745 | +0.0079 | 0.0023 | +0.0079 |

**Verdict: dead, on two independent grounds.**

1. **Not monotone.** Deltas between bins are `+0.0063, −0.0019, −0.0015, +0.0026`. The gap
   rises then falls then rises, so "new markets are inefficient" is false; whatever moves
   is a composition effect — which markets happen to live in each bucket — not a lifecycle
   signal.
2. **Inside the cost.** The largest gap is +0.87 ¢ against a ~1 ¢ taker half-spread (median
   quoted spread 2 ¢). Nothing here is tradable even if the pattern were real.

Note the first bin: markets under a day old show a gap of +0.24 ¢ with a standard error as
large as the effect. The intuition that brand-new markets are the softest is not supported.

**What would change the verdict:** quoted spreads *by age*, because a young market may be
both more mispriced and wider. That needs the book, so this moves behind PMXT rather than
being closed outright.

---

## A4 Scalar / range-market anchoring — **dead: the population does not exist** (2026-11-01)

Of 886,390 resolved binary conditions, 13,130 carry a non-{0,1} payout:

| payout_0 | count | what it is |
|---|---|---|
| 0.500 | 13,069 | **voided markets refunding half** |
| other fractions | 61 | genuine scalar markets |

**Verdict: dead by sample size, exactly as the pipeline's kill criterion anticipated
("too few markets to size").** A4 was written as "64 resolved binary conditions pay a
fraction"; 64 is the number of real scalar markets in the entire dataset, and the 13 k
apparent cases were refunds. There is no population to model calibration against.

Worth recording because the shape of the error is common: the first query — count of
non-{0,1} payouts — returned 13,130 and looked like a real population. Only splitting by
*value* separated refunds from scalars. A count is not a sample.

---

## A7 Term-structure drift — **CANDIDATE: far-side prices are too extreme** (2026-11-01)

**Window:** 2026-01-01 → 2026-10-01. **Sample:** 2,117 resolved tokens that printed at all
four leads (168, 72, 24, 6 h before `end_date`).

| p168 bucket | n | 168→72 | 72→24 | 24→6 | **168→6** | ±se | t | payout |
|---|---|---|---|---|---|---|---|---|
| 0.00–0.10 | 414 | +0.0423 | +0.0217 | +0.0115 | **+0.0756** | 0.0101 | **+7.5** | 0.126 |
| 0.10–0.25 | 407 | +0.0304 | +0.0305 | +0.0071 | **+0.0680** | 0.0137 | **+5.0** | 0.214 |
| 0.25–0.50 | 413 | −0.0030 | −0.0061 | +0.0123 | +0.0032 | 0.0124 | +0.3 | 0.349 |
| 0.50–0.75 | 357 | −0.0144 | −0.0010 | +0.0017 | −0.0137 | 0.0136 | −1.0 | 0.622 |
| 0.75–0.90 | 259 | −0.0377 | −0.0263 | −0.0021 | **−0.0660** | 0.0171 | **−3.9** | 0.745 |
| 0.90–1.00 | 267 | −0.0535 | −0.0419 | −0.0175 | **−0.1129** | 0.0146 | **−7.7** | 0.831 |

**Reading:** at 168 h the prices are systematically too extreme in both tails and converge
toward the realised payout. A favourite quoted 0.95 with a 0.83 payout rate drifts down
11 ¢; a longshot quoted 0.05 with a 0.13 rate drifts up 7.6 ¢. The middle of the book
(0.25–0.75) shows no drift at all, which is what a genuine tail effect should look like.

**Against the 2 ¢ cost** (two crossings, in and out), four of six buckets clear with
|t| > 3. That is the first thing in this project to survive a cost test.

**Why this is not yet a conclusion — three specific reasons:**

1. **Selection.** Only **2,117 of 1.75 M** resolved tokens printed at all four leads. Those
   are long-lived, heavily traded markets, not a random sample. The tails are exactly where
   thin markets live, and they are under-represented.
2. **It contradicts A1**, which found the level dead after costs. Both cannot be right in
   the same market. The difference is horizon and the entry/exit method, and that difference
   is the thing to test.
3. **Traded prices, not quotes.** This measures the drift in last-traded prices. Capturing it
   means buying at the *ask* at 168 h and selling at the *bid* at 6 h, which is wider than
   the drift measured here.

**Next test (needs the book):** buy at `best_ask` at 168 h, sell at `best_bid` at 6 h, and
require the drift to survive both crossings plus fees, on every market rather than the
self-selected four-lead subset.

---

## A3 Ladder monotonicity — **dead: markets agree with themselves** (2026-11-01)

**Sample:** 33,398 ladder legs across 3,447 events. Prices taken from the last taker-BUY
print inside a **single common hour** (24–23 h before `end_date`), so every leg in a
comparison is quoted at the same time by construction.

| size floor | legs priced | events comparable | adjacent pairs | inversions > 1 tick | worst |
|---|---|---|---|---|---|
| ≥ $25 | 241 | 53 | 73 | **0** | 0.005 |
| ≥ $5 | 558 | 151 | 282 | **0** | 0.005 |

**Verdict: dead.** Across 282 adjacent strike pairs in 151 events, the largest inversion is
**half a tick** (0.005). Ladders are internally consistent — the market does not price a
higher strike above a lower one by a tradable amount.

This is a *good* negative result: ladder coherence is a structural property that either
holds or leaks money to anyone watching, and it holds. The kill criterion was "≤ 1 tick",
and nothing reached even that.

**Limitation to keep in mind:** only 558 of 33,398 legs (1.7 %) had a print inside the
common hour with ≥ $5, because most ladder legs are short-dated crypto markets whose
24 h-before window sees little trading. So this establishes coherence for the **actively
quoted** ladders, not for every ladder. A book-based version could cover the rest.

---

## Cost-model correction — Taker Rebate Program modelled, no verdict changes (2026-11-01)

`alpha-pipeline.md` warned that every "dead after fees" conclusion used the **gross** taker
fee, because the Taker Rebate Program returns part of it. The rebate is now modelled
(`polymarketbot.fees`, 6 new tests).

**The program went live 2026-05-28**, is tiered on 30-day weighted volume, and pays 3 %
at the entry tier. On a crypto fee at its 50 ¢ peak (1.75 ¢/share), that is **0.053 ¢** —
2.6 % of a 2 ¢ round trip. Reaching Gold (18 %) at $2,000 capital requires **87 turns of
30-day capital** in crypto, the most generous category.

**No screen changes verdict.** A5's largest gap was 0.87 ¢ against a 1 ¢ half-spread; a
Bronze rebate closes 6 % of the difference. A7's drift was never fee-limited (7–11 ¢ against
a 2 ¢ cost). So the correction was worth making explicit and does not need any screen re-run.

The warning was directionally correct and quantitatively negligible — exactly the kind of
thing that should be measured rather than argued, because "our cost estimates overstate fees"
sounds like it could rescue an idea, and here it cannot.

---

## C1 Crypto vs reference price — **directionally consistent, magnitude not stable** (2026-11-01)

**Sample:** 4,850 crypto `above-` markets with a parsed strike, a stored entry VWAP and a
Binance reference bar matched by ASOF join. 5.52 M kline-minutes on disk (BTC/ETH/SOL to
2026-09).

**The robustness test that decides this.** The stored entry price is a VWAP over
T−24 h → T−6 h, while the reference price is read at one instant. Those are misaligned, so
the reference was sampled at four offsets. A real effect should survive the choice; an
artefact should not.

| distance bucket | 6 h | 12 h | 18 h | 24 h |
|---|---|---|---|---|
| < −10 % | −0.0069 | −0.0064 | −0.0017 | −0.0053 |
| −10…−2 % | −0.0464 | −0.0194 | −0.0146 | −0.0171 |
| −2…0 % | **−0.0951** | **−0.0916** | −0.0536 | −0.0298 |
| 0…2 % | −0.0123 | −0.0596 | −0.0586 | −0.0504 |
| 2…10 % | +0.0343 | +0.0263 | +0.0077 | **−0.0012** |
| > 10 % | +0.0058 | +0.0049 | +0.0055 | +0.0045 |

**Verdict: partial.** Two things are true and one is not.

- **The sign is stable.** Markets priced below the strike are overpriced, above the strike
  underpriced, at every offset. That is the same tail effect A7 found, seen from a
  different angle.
- **The only magnitude that holds is tiny.** `> 10 %` shows +0.45 to +0.58 ¢ at every
  offset (t = 3.0–13.6) — consistent, but only a quarter of a 2 ¢ round trip.
- **The large numbers are not stable.** The −2…0 % bucket moves from −9.5 ¢ to −3.0 ¢ as the
  offset changes, and `2…10 %` **changes sign**. An effect whose size halves when you move
  the measurement clock is measuring your clock.

**Why the misalignment matters, not just as noise:** the market price and the reference must
be read at the *same instant* before either can be compared. The stored VWAP spans 18 hours;
a 12-hour reference instant cannot be aligned to it. The next version must take the market
price from the tape at exactly the reference instant rather than reuse the calibration VWAP.

**This also explains part of A7.** A7's 168 h → 6 h drift is measured between two traded
prices, so it is internally aligned and unaffected by this problem. C1's instability does not
invalidate A7; it means C1 cannot yet confirm it.

**One process note:** the first run matched only 1,869 markets and returned a `< −10 %` bucket
priced at 0.507 — nonsense that still looked like a table. The cause was an ASOF join whose
right side was unsorted, so every market matched the same bar (67,540). An ASOF join needs an
explicitly ordered right side; DuckDB does not sort it for you.

---

## C1 aligned — **the mispricing was a clock artefact** (2026-11-01)

**Fix under test:** read the market price and the reference price at the *same instant* —
the last taker print at or before the lead, and the kline at that exact trade's timestamp.
The earlier version compared an 18-hour VWAP against a single-instant reference.

| window | n | −2…0 % gap | 2…10 % gap | > 10 % gap |
|---|---|---|---|---|
| 30 min | 240 | −0.0207 (t = −0.3) | −0.0131 (t = −0.6) | +0.0100 (t = 4.4) |
| 120 min | 362 | −0.0431 (t = −0.7) | +0.0025 (t = 0.2) | +0.0090 (t = 6.7) |
| 480 min | 511 | −0.0984 (t = −1.9) | −0.0054 (t = −0.4) | +0.0075 (t = 16.0) |

**Verdict: no tradable mispricing once the clocks agree.** Every middle bucket is
statistically indistinguishable from zero. The earlier version reported **−9.2 ¢ (t = −5.2)**
in the −2…0 % bucket and +2.6 ¢ in 2…10 %; both vanish under alignment.

The only survivor is the `> 10 %` bucket at **+0.75 to +0.90 ¢** (t = 6.7–16.0) — highly
significant and far too small: a quarter of the ~2 ¢ round trip, on markets where the asset
is already past the strike.

**What this means for A7.** A7's tail drift stays the only candidate, and this does *not*
corroborate it. That is the useful part: the two measurements disagreed, and the aligned one
won because its method was right. A7's defence is different — its drifts are measured between
two traded prices, so it is internally aligned and immune to this specific error. It still
needs the book test.

**Sample caveat:** 240–511 markets, because thin daily crypto markets rarely print near a
fixed lead. That is why the window was widened, and why the vote is decided by the *pattern
across windows* (stable near zero) rather than by any one row.

**The general lesson, now demonstrated twice today:** an effect whose magnitude changes when
you move the measurement clock is measuring your clock. Both the ASOF row-matching bug and
this one produced full, plausible-looking tables.

---

## D1 Liquidity-reward pools — **reward side measured; yield needs the book** (2026-11-01)

**Source:** Gamma snapshot 2026-11-01, 40,000 open markets, now capturing `clobRewards`.

| | |
|---|---|
| markets with a reward pool | **8,000 (20.0 %)** |
| total paid across pools | **$92,634 / day** |
| daily rate | median $3.0, p90 $19.0, max $3,000 |
| median minimum qualifying size | 20 shares |
| median `rewardsMaxSpread` | 4.5 ¢ |

**The yield cannot be computed from the snapshot, and my first attempt at it was
worthless.** I reported a "ceiling" as `daily_rate / (min_size x price)`, assuming we were
the only qualifier. That produced a 5,475 % median annual return and a 730,000 % maximum.
The arithmetic is right and the metric is meaningless: a pro-rata pool splits across
everyone who qualifies, so "one minimum-size quote captures the whole pool" measures the
size floor, not the opportunity. **A metric that ranks pools by their minimum size
requirement is worse than no metric**, because it looks like an answer.

Removed. The screen now reports the reward side descriptively and stops.

**What the book adds:** total qualifying size resting inside `rewardsMaxSpread` of the mid.
Yield is then `our size / that total x daily rate`, which needs reconstructed depth — so D1
joins the other book-dependent ideas rather than being closed.

**One untested lead:** Gamma exposes a `competitive` field (0.83–1.00 on the richest pools)
which may already encode the competition measure. Its definition is undocumented, so it
cannot be trusted as a substitute, but it is one probe away from being checkable against
depth once the book lands.

**Note on scale:** the earlier estimate in `alpha-pipeline.md` was $23,993/day over 400
markets (27 %). The true figure across 40,000 open markets is **$92,634/day over 8,000** —
larger in absolute terms, and a lower share of markets (20 %).

---

## E7 Wallet behaviour from the tape — the top of the volume distribution is automated

**Window:** 2026-04-01 → 2026-10-01. **Sample:** 238,240 wallets with ≥ 200 fills,
covering **$37.2 B** of taker-plus-maker volume.

| Role mix | wallets | volume | median fill | median markets (busiest month) |
|---|---|---|---|---|
| **maker** | 26,486 | $15.4 B | **$13** | 620 |
| **taker** | 166,074 | $13.3 B | **$19** | 308 |
| mixed | 45,680 | $8.5 B | $17 | 312 |

Top 12 wallets by volume: **2,468 to 60,616 markets per month**, median fill **$5–40**,
median time between fills **3–19 seconds**.

**What this says.** The highest-volume participants are broad, automated, small-clip
makers. They are not large directional traders; they are quoting robots spread across
thousands of markets with sub-20-second replacement times. Across the entire dataset only
**18 wallets** have a median fill ≥ $1,000, and they account for $360 M — 1 % of volume.

**Three consequences for the plan:**

1. **Size dependence is less binding than assumed.** A $250 clip is *larger* than the
   median fill of the top-volume wallets. Section 3.1 of the roadmap treats our size as
   the constraint; at $250 we are not the small participant, we are the large one in most
   of these books. That changes which mechanisms are open to us — and means impact, not
   anonymity, is the thing to measure.
2. **Directional large-clip participation is rare.** The replicable population is mostly
   not doing what the plan's Track A hoped to copy. Copying is even less promising than
   the thesis said, for a second and independent reason.
3. **Maker share is 41 % of all volume** ($15.4 B), consistent with the cohort finding that
   maker wallets draw 67.7 % of PnL from rebates. Two independent datasets now point at the
   same structural fact.

**Two columns were wrong and are fixed, recorded because both looked plausible:**

- `markets` was originally `approx_count_distinct` of the per-month market *counts*. A
  wallet with monthly counts 3,4,5,4,3 came out as "3 distinct values", which is why every
  wallet read ~5 markets. It is now `max()`, a lower bound: distinct markets in its busiest
  month.
- `rhythm` classified **all 238,240 wallets** as "bursty". The classifier is not wrong, it
  is useless: median gap 22 s with variance above threshold. A constant column is worse
  than no column and should be dropped rather than shipped.

**Caveat that limits conclusion 1:** `usd_amount` is per **fill**, and makers are filled in
many small pieces. A wallet pushing $50 k through a market may show 1,000 fills of $50. So
this measures fill size — the right quantity for impact — not order size, which is not the
right quantity for capital.

---

## ⚠ Data staleness found: `market_labels` predated the API label fetch (2026-11-01)

**What happened.** `market_labels.parquet` was built at 18:41. The Data API label fetch
finished at 20:49 — two hours later. The merge step was never re-run, so 1,571,580
resolved conditions fetched from the API were present in `labels_api.parquet` and absent
from `market_labels.parquet`, which took only **999** rows from it.

**Effect on coverage, by month of `end_date`:**

| 2026 month | resolved before | resolved after |
|---|---|---|
| Jan–Mar | 99.9 % | 99.9 % |
| Apr | 80.3 % | **90.9 %** |
| May | **0.6 %** | **80.4 %** |
| Jun | **0.3 %** | **77.8 %** |
| Jul | 0.0 % | **70.5 %** |
| Aug | 0.0 % | 49.2 % |
| Sep | 0.1 % | 37.0 % |

The forward-test window (2026-06-01 → 08-10) went from essentially unlabelled to
**641,331 resolved of 685,918 markets (93.4 %)**. The rebuild takes 10 seconds.

**How it was caught.** An inner join in `analyse_fill_quality.py` matched only 1,546 of
3.83 M maker fills and reported "$0.00 B" of volume. The join was not wrong — the labels
were. The "0.00 B" was the tell, not the join.

**Which results this invalidates.** Any screen that filtered on `resolved` over 2026 data
was running on 0.3 % coverage for May onwards:

| Screen | Status |
|---|---|
| A7 term-structure drift | **re-run below** |
| C1 crypto reference | needs re-running |
| A5 lifecycle | needs re-running (also used stale screen rows) |
| C2 base rates | needs re-running |
| E1, A2, A3, A4 | unaffected — they do not filter on `resolved` |

**The lesson, which is the third instance today.** A silent, plausible-looking zero is more
dangerous than an error: no query failed, nothing warned, and every screen returned a
well-formed table computed on a thin, non-random subsample (Jan–Apr only). The check that
caught it was a cross-table consistency test — a join that should have matched nearly
everything matched almost nothing.

---

## A7 re-run on corrected labels — **stronger, and now monotone** (2026-11-01)

| p168 bucket | n | 168→6 drift | ±se | t | vs 2 ¢ cost |
|---|---|---|---|---|---|
| 0.00–0.10 | 563 | **+0.0953** | 0.0097 | **+9.8** | beats |
| 0.10–0.25 | 656 | **+0.0902** | 0.0115 | **+7.8** | beats |
| 0.25–0.50 | 664 | +0.0219 | 0.0110 | +2.0 | beats (weak) |
| 0.50–0.75 | 592 | −0.0340 | 0.0121 | −2.8 | beats (weak) |
| 0.75–0.90 | 449 | **−0.0985** | 0.0143 | **−6.9** | beats |
| 0.90–1.00 | 372 | **−0.1172** | 0.0131 | **−9.0** | beats |

The effect is now **monotone in price** — positive in the low tail, negative in the high
tail, near zero in the middle — which is the signature of a genuine tail effect rather than
a composition artefact. Magnitudes grew from +7.6 / −11.3 ¢ to **+9.5 / −11.7 ¢**.

**But the selection problem is now precisely quantified and it is severe.** Only
**3,296 of 4,869,752** resolved tokens (0.07 %) printed at all four leads. The reason is
structural: the median market lives ~24 h, so a 168-hour lead does not exist for it. A7 can
only ever speak about markets that live longer than a week.

So A7 remains a candidate, not a result, and the constraint is now about *breadth*, not
method: the effect is real in the population it can see, and that population is 0.07 % of
markets. Whether it is worth anything depends on whether those markets carry enough volume
— which is the next question, not a bearing test.

---

## Fill quality — do the automated makers earn, or are they paid? (2026-04 → 10)

**Method.** For every maker fill, mark the token at its realised payout and compute
`edge = (payout − price)` for a maker buy, `(price − payout)` for a maker sell. Aggregate
per wallet, then by volume decile.

**Sample.** 67,239 makers with ≥ 200 priced fills · 598,381,359 fills · **$15.85 B** of
maker volume. Marked against `market_labels` (rebuilt).

| Volume decile | makers | volume $m | edge $m | edge per $ |
|---|---|---|---|---|
| **1 (largest)** | 6,724 | **14,634** | **+8.6** | **+0.00059** |
| 2 | 6,724 | 680 | −6.7 | −0.00978 |
| 3 | 6,724 | 253 | −1.8 | −0.00712 |
| 4–9 | 40,344 | 275 | −4.1 | −0.011 … −0.025 |
| 10 (smallest) | 6,723 | 3 | −0.1 | −0.02086 |
| **all** | 67,239 | 15,850 | **−4.0** | **−0.00026** |

The largest decile carries **92 % of all maker volume** and is the only one with a positive
terminal edge (+0.06 ¢ per $1). Every smaller decile loses on the trade, and loses more the
smaller it is. Aggregate terminal edge is **−$4 M on $15.85 B — indistinguishable from
zero.**

**What this does and does not say.** It says the top of the maker population holds inventory
to resolution that is, on average, *correctly* sided — they are not systematically left
holding the losing token. It does **not** measure spread capture: a maker who buys and sells
the same token flattens to zero terminal exposure whatever they earned on the round trip, so
this metric sees only the inventory carried to resolution. The honest reading is therefore
"the biggest makers do not systematically eat adverse selection at resolution", not "the
biggest makers are profitable".

Proving *paid not skilled* needs round-trip PnL (fill-to-fill, matched by token and time),
not a resolution mark. That is the next build.

**What it does establish.** The concentration is extreme: 6,724 wallets do 92 % of maker
volume. Combined with E7 (median fill $13, 3–19 s between fills), the maker side is a small
number of automated participants with near-total coverage — which is why the *whitespace*
question (idea 3) matters more than the *copying* question.

---

## Round-trip maker PnL — corrected, conservation-verified (2026-04 → 10)

**First, a bug that produced a clean wrong answer.** The first version reported makers
earning **+0.75 ¢ per $1** in every decile. That uniformity was the tell: aggregate trader
PnL is zero by conservation — every trade has a buyer and a seller at the same price, and a
complete set costs $1 and pays $1.

The cause was a NULL comparison. For a wallet with only one side (buy_sh IS NULL or
sell_sh IS NULL), `buy_sh > sell_sh` evaluates to NULL, so the CASE fell through to ELSE
and produced NULL — **silently dropping every one-sided wallet's settlement term while
still counting two-sided wallets' realised PnL.** Dropping a negative term inflates the
total.

Two fixes:
1. `coalesce` before the comparison, so one-sided wallets settle correctly.
2. Use `price * token_amount` for cash instead of `usd_amount`, which is **rounded to the
   cent** — wrong by up to 50 % on sub-dollar fills, and 55 % of June rows differ from
   `price * token_amount`.

**Verification (the test that matters).** With no volume filter at all, June aggregate PnL:

| version | PnL |
|---|---|
| before fix | **+$44.38 M** on $6.21 B |
| after fix | **−$0.00 M** on $6.21 B |

Conservation now holds exactly. A per-token check agrees: one token with 77,845 trades and
104.8 M shares balanced on both sides previously summed to +$2.27 M and now sums to zero.

**Result — PnL is concentrated in the top decile, and the rest subsidise it:**

| decile | makers | volume $m | PnL $m | per $ | positive |
|---|---|---|---|---|---|
| **1 (largest)** | 35,404 | **29,413 (91 %)** | **+56.60** | **+0.192 ¢** | 40.7 % |
| 2 | 35,404 | 1,288 | −16.05 | −1.246 ¢ | 32.6 % |
| 3 | 35,404 | 577 | −9.45 | −1.636 ¢ | 37.9 % |
| 4 | 35,404 | 337 | −6.74 | −2.002 ¢ | 32.0 % |
| 5 | 35,404 | 209 | −4.84 | −2.316 ¢ | 30.8 % |
| 6–9 | 141,616 | 359 | −12.0 | −2.6…−3.1 ¢ | ~31 % |
| 10 (smallest) | 35,404 | 40 | −1.22 | −3.068 ¢ | 30.8 % |
| **all** | 354,040 | **32,224** | **+8.32** | **+0.026 ¢** | 32.9 % |

The relationship is monotone: **the larger the wallet, the better its PnL per dollar**,
from +0.19 ¢ for the top decile down to −3.07 ¢ for the smallest. Only 32.9 % of wallets are
profitable at all.

The aggregate is +$8.32 M rather than 0 because the payout join keeps only the 4.87 M tokens
that are resolved in our labels, out of ~8.1 M traded; the missing tokens are the recent,
unresolved tail. The 0.026 ¢ residual is that selection, not a leak in the metric.

**The largest wallets, individually:**

| PnL $m | volume $m | per $ | tokens |
|---|---|---|---|
| +15.69 | 480 | +3.27 ¢ | 203,254 |
| +8.19 | 482 | +1.70 ¢ | 11,053 |
| +6.67 | 439 | +1.52 ¢ | 158,995 |
| +3.05 | 564 | +0.54 ¢ | 111,704 |
| −6.84 | 293 | −2.34 ¢ | 435,345 |
| −5.11 | 153 | −3.34 ¢ | 39,993 |

Note the **inverse relation with token count**: the most profitable large wallet trades
203 k tokens at +3.27 ¢, while the worst trades 435 k tokens at −2.34 ¢. Breadth is not a
proxy for skill — covering more markets diluted the spread.

**What this establishes.** Trading PnL is not evenly distributed and not zero-mean-within-
makers: a small top slice earns the spread and everyone below pays it. The top decile's
+0.192 ¢ is *trading* PnL, before rebates, so the biggest makers do earn on the round trip
— they are not merely paid. The 32 % win rate means the median participant loses.

---

## Performance: materialised cache, 3.7× faster and identical

`data/raw/sii/trades.parquet` is 45.85 GB / 212 partitions. Every analysis rescanned it.

`scripts/build_wallet_token.py` materialises per-(wallet, token, month) sums once, into
`data/processed/wallet_token/` — 6 months, 261 M pairs, 14.6 GB, built in 13 minutes,
resumable per month.

| path | full-window round trip |
|---|---|
| direct tape scan + union + payout join | **9 m 22 s** |
| cache read + payout join | **2 m 33 s** |

Identical deciles to the last digit, which cross-validates both paths.

The cache is only 3× smaller than the tape because 600 M fills collapse into 261 M
(wallet, token) pairs — most pairs contain ~2 fills. The remaining cost is the 261 M-row
group-by, which is memory-bandwidth bound, not arithmetic bound.

---

## Do the big wallets hold to resolution, or exit? (2026-04 → 10)

**Method.** For every (wallet, token) position over the window:

    closed = 2 * min(buys, sells) / (buys + sells)     1.0 = fully round-tripped

and the PnL is split into the round trip (`realised`) and the settlement of what is left
(`settled`). `buy_only` (sold nothing) is the honest holding measure; `sell_only` (bought
nothing) is a market maker who minted a complete set and sold one leg, which looks open in
the tape but is not.

**Positions by type, share of traded shares:**

| decile | buy-only (held) | sell-only (minted) | mixed | closed fraction of mixed |
|---|---|---|---|---|
| **1 (largest)** | 26.6 % | 23.1 % | 50.4 % | **0.583** |
| 2 | 21.0 % | 35.5 % | 43.5 % | 0.766 |
| 5 | 19.5 % | 33.1 % | 47.3 % | 0.845 |
| 10 (smallest) | 20.4 % | 26.9 % | 52.7 % | **0.917** |

The largest wallets close **58 %** of what they enter; the smallest close **92 %**. Holding
falls monotonically with size — the opposite of what a naive "big players hold positions"
story predicts.

**Where the PnL comes from:**

| decile | realised $m | settled $m | net $m |
|---|---|---|---|
| **1** | **+191.98** | **−141.13** | **+50.85** |
| 2 | +9.80 | −25.44 | −15.64 |
| 3 | +3.07 | −11.07 | −8.00 |
| 4–10 | +4.43 | −24.48 | −20.05 |

**The top decile earns its money on the round trip and loses $141 m on what it carries to
resolution.** Closing everything before resolution would give it +$192 m instead of
+$50.9 m; holding costs it 74 % of its profit.

**Reading.** This is consistent with the recorded thesis (`docs/strategy-thesis.md`): the
edge is taken by exiting when the calculation reverses, and the residue left open is
adverse. It is *not* a pure EV-trader story, because 23 % of top-decile flow is
`sell_only` — minting a complete set and selling one leg is market making, and it coexists
in the same wallets.

**Caveat on the measure.** The closed fraction is bimodal, not uniform: 18 % of mixed
shares sit in positions that are 0–10 % closed and 33 % in positions that are 90–100 %
closed. A single mean hides two different behaviours inside the top decile.

---

## Disposition effect? Within-position test (2026-04 → 10)

**Question.** The top decile earns +$192 m closing positions and loses −$141 m on what it
carries. Is that "take profits early, wait on losers" (disposition), or fair-value timing?

**Within-position test.** For the 9,322,452 partially-exited net-long positions of the top
decile, split the booked PnL of the sold shares from the PnL of the shares left open:

| sold / held | positions | closed PnL | held PnL |
|---|---|---|---|
| sold win, held loss | 2,628,559 | +$71.24 m | −$413.64 m |
| sold win, held win | 2,606,525 | +$112.64 m | +$497.77 m |
| sold loss, held loss | 2,233,678 | −$71.79 m | −$405.46 m |
| sold loss, held win | 1,853,690 | −$61.39 m | +$322.63 m |

**Result: a modest tilt, not a disposition effect.** Exits are taken in profit 56.2 % of the
time against 47.8 % if the same positions had been held — **+8.4 pp**. But 43.8 % of exits
are taken at a loss, and 20 % of positions are "sold at a loss while holding a winner",
which a pure disposition account cannot produce. Held parts net to +$1.3 m across these
positions while sold parts earn +$50.7 m.

**Where the loss actually is.** Separate from partially-exited positions, the top decile has
**50,522,914 positions it never sells at all**, holding them to resolution:

| | positions | profitable | total PnL |
|---|---|---|---|
| never sold | 50,522,914 | **50.0 %** | **−$148.72 m** |

The −$141 m settlement loss is therefore not concentrated in a handful of conviction bets
gone wrong. It is spread over 50.5 M positions averaging **−$2.94 each at a coin-flip win
rate**. That is consistent with dust — positions too small to close — but the notional
distribution of the never-sold set has not yet been measured, so "dust" remains an
inference, not a finding.

---

## Correction: the entry/exit edge split was not meaningful

An earlier result reported that 87 % of the top decile's edge was "entry edge". That
decomposition is arithmetically exact but economically empty:

```
entry_edge = buys  * (payout - buy_vwap)
exit_edge  = sells * (sell_vwap - payout)
entry + exit = spread          (for a fully round-tripped position)
```

The *sum* is payout-independent, but the *split* moves with the payout. A token resolving
YES makes any bought-and-sold position look like entry edge and negative exit edge. The
87 % figure therefore measured where the outcomes landed, not where the skill was.

The valid decomposition, which never touches payout, is the one used above:

    realised  = matched * (sell_vwap - buy_vwap)     spread actually captured
    settled   = residual * (payout - buy_vwap)       what the unclosed part earned

Testing entry skill requires the fair value **at the moment of entry** — the mid then, not
the payout later. That needs the order book, which is the next data dependency.
