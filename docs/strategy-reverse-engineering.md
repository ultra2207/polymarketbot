# Reverse-engineering the competition

Goal: understand what profitable Polymarket participants actually do, decide which of it is
replicable, and find segments where the marginal dollar of edge is high but sophisticated
capital is not yet competing for it.

Companion to `roadmap.md` §3.2 and Phase 6b.

---

## 0. The premise, sharpened

The instinct is right: copying a top wallet fails because your order arrives later, at a
worse price, into a book they helped move. But "deploy where the large traders are not" is
only half a thesis. Absence of large traders has two very different causes:

| Cause | What it looks like | Is it an opportunity? |
|---|---|---|
| **Neglect** — the market is real, liquid enough, but nobody with capital has looked | decent volume, wide spread, slow price discovery, poor calibration | **Yes** |
| **Uninhabitability** — no exit, no depth, resolution risk, or information that is impossible to obtain | near-zero volume, stale book, ambiguous rules | **No** — it is a trap that looks like whitespace |

So the search is over two axes, not one:

```
                    high crowding        low crowding
high edge      │  crowded alpha      │  ★ TARGET: uncrowded alpha
               │  (compete on speed) │  (the only place worth deploying)
───────────────┼─────────────────────┼──────────────────────────────
low edge       │  efficient market    │  trap: no exit / no edge
               │  (don't bother)     │  (most zero-volume markets)
```

Everything below is a method for populating that grid **from data**, rather than from
anecdote about "whales".

---

## 1. Why copying fails, formally

Four distinct mechanisms. They need different countermeasures, and conflating them is why
most copy-trading attempts lose money.

1. **Latency / adverse selection.** You see their fill at price `p`. By the time you act,
   the book has moved to `p + Δ`, where `Δ` is roughly their impact plus everyone else's
   reaction. You inherit the worse price and none of the information.
2. **Information asymmetry.** Their entry was the *output* of information you don't have
   (a poll, a model, an injury report, a private feed). Copying the trade copies the
   shadow, not the cause. The shadow decays.
3. **Size dependence.** Some edges only exist at their size (they are the liquidity
   provider, or they are being paid to absorb risk). A smaller participant cannot earn the
   same spread per unit, and a larger one cannot earn it at all.
4. **Exit into you.** If enough capital copies them, they exit into the copy flow. This is
   not hypothetical: it is the standard failure mode of on-chain copy-trading.

**Implication:** the deliverable of reverse engineering is not a trade rule. It is a set of
**hypotheses about information sources and frictions**. Only hypotheses that survive
without privileged information or speed become strategies.

---

## 1.5 The actual objective: mechanism transplantation

Copy trading was never the goal, and treating it as the goal produced the wrong analysis.
The objective is narrower and much more useful:

> Find wallets that trade **algorithmically**, infer the **rule** they run, then deploy that
> rule in markets where they are **not present**.

That is not copying. Copying replicates their *trades*; this replicates their *rule* and
then exploits the part of the condition space their coverage does not reach. Three
differences make it a different research programme:

| | Copy trading | Mechanism transplantation |
|---|---|---|
| Unit of analysis | the wallet's signal | the **predicate** they run |
| Success metric | did our fills match theirs | does the predicate pay **where they are absent** |
| Failure mode | latency, size, exit-into-copy | the predicate was theirs because of infrastructure we lack |

The economic basis is that a systematic trader's coverage is a **proper subset** of the
condition space, and the exclusions are structural: minimum size they need to make a fill
worth it, categories they whitelist, markets they can even scan, capital they will lock,
and their own risk limits. Those exclusions are the opportunity.

### Step 1 — identify algorithmic wallets from the tape alone

No identity data needed; behaviour is enough.

| Signal | Definition | Why it separates algorithms from humans |
|---|---|---|
| Timing entropy | CV of inter-fill gaps; share of fills within 1 s of :00/:30 or a multiple of 60 s | humans are heavy-tailed and diurnal; loops are not |
| Size quantisation | share of fills in the wallet's own top-3 sizes; roundness of size | algorithms use fixed or derived sizes |
| Price snapping | share of fills at tick-aligned or round decimal prices | algorithms compute prices; humans click them |
| Two-sidedness | share of conditions where they are maker on one side and taker on the other within Δt; pairs summing to ≈ 1 | arbitrage and market-making signature |
| Event locking | share of fills within k seconds of a scheduled boundary | see the slug trick below |
| Maker share | fraction of fills where the wallet is the maker | separates quoting algos from taker algos |
| Coverage structure | distinct conditions per day; Gini of volume across markets | a systematic scan, not occasional conviction |

**The slug trick.** SII `slug` values encode the market's window directly:
`sol-updown-5m-1774560600` is a 5-minute Solana up/down market ending at unix
`1774560600`. That reconstructs the exact scheduled boundary for crypto up/down markets
with **no external price series**, so "does this wallet fill within 400 ms of the boundary"
is computable from the tape we already hold.

### Step 2 — infer the rule as a predicate, not a model

The output must be interpretable enough to be applied elsewhere. Candidate shapes:

- **entry timing**: histogram of time-to-resolution at entry, by market family
- **entry price**: does it fade extremes or follow them; price bucket distribution
- **reaction**: direction vs the prior N-second return (momentum or reversion coefficient)
- **reference deviation**: for crypto, distance from the Chainlink/Binance reference
- **coverage condition**: minimum lifetime volume, minimum depth, category, tick size,
  neg-risk flag, market age, hours-to-resolution floor

Fit the first four as behaviour, and the **coverage condition separately** — the coverage
condition is what defines the gap, so it must be estimated explicitly rather than assumed.

### Step 3 — the discriminator: edge half-life

The whole idea lives or dies on one measurable number. After one of their fills, how long
until the price has completed most of its eventual adjustment?

- **< 10 s → the edge is latency.** Untransplantable. Their absence elsewhere is irrelevant
  because we could not compete even where they *are* present.
- **minutes to hours → the edge is structural or informational.** Transplantable.

Measure as an event study of mid-price response around their fills at 1 s / 10 s / 1 m /
5 m / 1 h, expressed as the fraction of eventual adjustment completed. Honest prior: most
crypto and sports up/down algo flow will land in the first bucket. Politics and economics
are where a multi-minute half-life is plausible.

### Step 4 — the gap set

`predicate ∧ ¬their_coverage ∧ habitable`

`habitable` is not optional: depth at $250 ≥ 3× the order, an exit that exists, and an
unambiguous resolution. Absence of large traders is ambiguous between neglect (opportunity)
and uninhabitability (no exit). This is the same guardrail as `plan.md` thesis #2, and
skipping it is how "whitespace" turns into a graveyard.

### Step 5 — measure with the fill engine

Simulate the predicate on the gap set under all five exit policies. A transplant that needs
a different exit from the original rule is a different strategy and must be justified.

### Why this can fail — stated up front

1. **Most algo flow is latency-bound.** Then there is nothing to transplant, and the honest
   answer is that Track A's output is negative.
2. **The gap may be occupied by other algorithms** we did not identify, because we only
   survey the top deciles of a survivorship-biased leaderboard.
3. **The predicate may be overfit** to their fills. Validate on a held-out period of the
   same wallet before generalising.
4. **Inverse selection.** They may skip markets for a reason absent from our feature set —
   an ambiguous resolution rule, a resolution source they do not trust, a venue quirk.

---

## 2. Ranking traders correctly

Raw lifetime PnL is the wrong metric. It ranks by size and by luck, and prediction-market
PnL is extremely skewed (a handful of longshot wins dominate).

### Filters before ranking

- **Identity collapse** — one entity may hold a signer EOA, a proxy/Safe, and a deposit
  wallet, plus sybil wallets. Cluster before ranking (`roadmap.md` Phase 1b).
- **Exclude market makers** from the directional ranking. Two-sided high-frequency flow with
  tiny holds is a different business; rank it separately.
- **Exclude self-trades and wash** (`maker == taker`, or same cluster on both sides).
- **Exclude contract counterparties** (taker summary events).
- **Require a minimum sample** — e.g. ≥ 30 resolved markets and ≥ $50k deployed, otherwise
  the estimate is noise.

### Skill metrics (not PnL)

| Metric | Why |
|---|---|
| **Calibration edge** | Average `(realised outcome − entry price)`, weighted by size. Measures information, not luck |
| **Post-entry drift** | Price move in their favour over 1 h / 1 d / to resolution, net of spread. The cleanest information test |
| **Hit rate vs price** | Are their 30 ¢ buys winning 30 %, 40 %, or 60 % of the time? |
| **Consistency** | Fraction of profitable months; max drawdown; longest losing streak |
| **Bootstrap CI on edge** | Report the interval, not the point. A 90 % CI that includes zero is not a skill claim |
| **Walk-forward rank stability** | Rank on period 1, evaluate on period 2. If rank doesn't persist, it was luck |

A wallet that is +$2 M from one 3 ¢ longshot is not a top trader. A wallet that is +$200 k
with a 90 % CI of `[+0.03, +0.09]` calibration edge across 200 markets is.

---

## 3. Behavioural archetypes

Cluster wallets on behaviour, not on PnL. The clusters are the actual object of study.

| Archetype | Signature features | How to detect |
|---|---|---|
| **Market maker** | high trade count, two-sided within market, holds minutes, prices near mid, many markets, balanced inventory | maker share of fills, buy/sell balance, median hold, distance from mid |
| **Arbitrageur** | simultaneous legs in one neg-risk event, or YES+NO bundle at combined price < 1 | same `transaction_hash` across legs; `Σprice < 1` repeated |
| **Latency / crypto** | entries seconds after a reference-price move in `updown` markets, high frequency, tiny edge per trade | lag correlation to Binance/Chainlink ticks |
| **Fundamental directional** | concentrated, holds to resolution, few markets, entry far from mid | hold duration ≈ market lifetime, low turnover |
| **Insider / event** | one market, large size, early, high conviction, entry precedes a price jump | size z-score vs their own history; entry before largest move |
| **Copy / reactive** | entry 1–60 min after a leader, same direction, smaller size | lagged cross-correlation with top wallets |

Clustering method: build per-wallet feature vectors (hold-time distribution, maker share,
category mix, trade-size distribution, price-region mix, entry-lag vs news, two-sidedness,
resolution-vs-exit split), then cluster with something simple first (k-means / HDBSCAN on
standardised features). Interpret clusters qualitatively; don't over-fit labels.

**The output is not the cluster.** It is, per cluster: what the edge is, how long it lasts,
how much capital it absorbs, and what it requires (speed, information, capital, or nothing).

---

## 4. From behaviour to an edge hypothesis

For every archetype, in every segment, compute:

1. **Entry percentile** — where in the market's price path did they enter?
2. **Post-entry drift** — how far did the price move in their favour after entry, net of
   half-spread and fees, at 1 h / 1 d / terminal?
3. **Matched control** — compare against random entries in the same market at the same time.
   Without this you are measuring market drift, not skill.
4. **Decay** — does the edge shrink as more wallets enter the same segment?
5. **Requisiteness** — can the edge be earned with (a) public information only, (b) no
   co-location, (c) less than their capital?

Classify each finding:

| Finding | Classification |
|---|---|
| Edge survives with public info, no speed, smaller size | **replicable** |
| Edge survives only with their size | **structural — do not copy** |
| Edge survives only with sub-minute reaction | **not replicable — avoid the segment** |
| Edge survives only with information we can't obtain | **not replicable — but check for a public proxy** |

Most "top trader" edges will land in rows 2–4. That is the useful result: it tells you
which segments to *leave alone*, which is half of the whitespace search.

---

## 5. Crowding map

Build this per segment, where a segment is a cell of:

- category (politics / sports / crypto up-down / culture / econ / weather)
- horizon bucket (5 min / 1 h / 1 day / 1 week / to resolution)
- liquidity bucket (depth at 1 % from mid, at target notional)
- price region (tails < 0.1 or > 0.9 / mid 0.1–0.9)
- structure (binary vs neg-risk multi-outcome)
- lifecycle stage (first 10 % of life / middle / final 10 %)

Metrics per segment:

| Metric | Definition | Reading |
|---|---|---|
| **Top-10 wallet share** | volume by the 10 largest wallets / total volume | > 60 % = crowded |
| **HHI** | Herfindahl on wallet volume | concentration |
| **Wallets per $1 M volume** | distinct active wallets normalised by volume | low = few participants |
| **Entry speed** | median time from market creation to first "skilled" entry | fast = sophisticated money is watching |
| **Maker concentration** | top-3 makers' share of resting size | high = one MM owns the book |
| **Price efficiency** | \|price − eventual outcome\| over horizon; return autocorrelation | high = already efficient |
| **Edge decay rate** | change in calibration edge of incumbents over time | negative = crowding increasing |

This is the map that turns "where the big traders aren't" into something measurable.

---

## 6. Whitespace scoring

For each segment, with `n` = number of markets and `N` = target notional:

```
opportunity = edge_potential × capacity × novelty / (cost × competition)
```

| Term | Proxy |
|---|---|
| `edge_potential` | calibration error + post-entry drift predictability in the segment |
| `capacity` | median depth at 1 % and median daily volume, scaled to `N` |
| `novelty` | `1 − top-10 wallet share` |
| `cost` | fees + half-spread + modelled impact |
| `competition` | wallet concentration × entry speed |

**Guardrails before anything graduates:**

- `capacity ≥ 3 × N` — otherwise your own size is the market
- median market lifetime ≥ 2× intended hold — otherwise you're forced to exit
- exit exists: depth at 1 % on both sides in the final third of the market's life
- not dominated by a single MM whose spread exceeds your edge
- survives the 2× impact stress from `roadmap.md` §4.2

A segment that fails "exit exists" is uninhabitable regardless of how uncrowded it is.

---

## 7. Traps

1. **Absence ≠ opportunity.** The most uncrowded markets are usually untradeable. Always
   compute capacity and exit depth before celebrating low crowding.
2. **Segment p-hacking.** With ~20 categories × 5 horizons × 5 liquidity buckets you can
   find a "significant" niche by chance. **Pre-register the taxonomy** and the scoring
   formula before looking at results.
3. **Skewed PnL.** Prediction-market returns are lottery-like. Use bootstrapped intervals
   and median-based statistics, not means.
4. **Rank survivorship.** The data has no survivorship bias (the chain keeps losers), but
   *ranking by final PnL* does. Rank walk-forward only.
5. **Shadow, not cause.** A timing pattern is not a reason. "They buy 40 minutes after
   polling closes" is only actionable if you can also get the poll at that time.
6. **Decay.** Anything you find will be competed away once it is known. Build a decay
   monitor into the strategy, not an afterthought.
7. **Maker vs taker PnL.** A maker's profit is spread + rebate; a taker's is information.
   Mixing them in one ranking produces nonsense.

---

## 8. Deliverables, in order

| # | Artifact | Contents | Gate |
|---|---|---|---|
| 1 | `wallets` | clustered identities with `signer/proxy/deposit` collapsed | ≥ 99 % of volume mapped |
| 2 | `wallet_skill` | calibration edge, post-entry drift, CI, walk-forward rank stability | top-100 list stable across two periods |
| 3 | `archetypes` | behavioural clusters + per-cluster edge/capacity/requisiteness | every cluster explainable in one sentence |
| 4 | `crowding` | segment-level concentration, entry speed, price efficiency | covers ≥ 95 % of volume |
| 5 | `whitespace` | scored segments with capacity and exit checks | ≥ 5 candidate segments pass guardrails |
| 6 | `hypotheses` | one-page per candidate: edge, source, replicability, kill condition | pre-registered before any backtest |

Only after #6 does Phase 7 strategy work begin. Building a strategy before you know which
segments are structurally unwinnable wastes the most expensive resource you have.

---

## 9. What this changes in the roadmap

- New **Phase 6b** between baselines and strategy research.
- Requires Phase 3 (position/PnL reconstruction) and Phase 1b (wallet identity map) to be
  complete; it cannot start earlier.
- Adds `crowding` and `capacity` as first-class outputs, not side notes.
- Adds a kill criterion: a strategy whose segment has top-10 wallet share > 60 % must show
  a *differentiated* edge (different information, different horizon, or different payoff),
  not merely a better execution of the same trade.
