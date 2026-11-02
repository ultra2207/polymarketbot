# Working thesis: the top decile is an EV trader, not a market maker

Recorded 2026-11-02, from the operator. This is the hypothesis the next round of analysis
tests. It is written down before the test so it cannot be rationalised after.

## The claim

The profitable top decile does not earn a spread by quoting both sides. It enters a bet when
the **cost of entry is below its calculated EV**, and exits — often reversing to the other
side of the same market — when that calculation flips. Holding a position to resolution is
rare. The edge is therefore in the **EV calculation itself**, i.e. in detecting mispriced
bets, not in providing liquidity.

## Why it matters

If true, the replicable object is a *pricing model*, not an execution strategy. That is a
completely different research program from market making: it says the money is in knowing
when 0.40 is worth 0.55, and that the winning wallets differ from losers by the quality of
that estimate — not by speed, size, or rebate access.

## The exact decomposition that tests it

For any wallet and token, total profit is identically:

```
profit = (sell_cash - buy_cash) + payout * (buy_sh - sell_sh)
```

and, since `buy_cash = b * vwap_buy` and `sell_cash = s * vwap_sell`, it can be rewritten
with no approximation as:

```
profit = ENTRY_EDGE + EXIT_EDGE

ENTRY_EDGE = b * (payout - vwap_buy)     -- did they buy below fair value?
EXIT_EDGE  = s * (vwap_sell - payout)    -- did they sell above fair value?
```

Both terms use the *realised* payout as the fair-value proxy. That is a choice with a known
bias — payout is only known ex post — so the two terms must be read as an accounting split,
not as evidence of foresight. The foresight question is answered by **dispersion**, below.

### What each hypothesis predicts

| | Market maker | EV trader (this thesis) |
|---|---|---|
| Carry to resolution | small; residual ≈ 0 | small too — they exit, they do not hold |
| ENTRY_EDGE per share | ≈ half the spread, ~1–2 ¢ | typically ≫ half the spread |
| ENTRY_EDGE vs EXIT_EDGE | roughly equal (symmetric quoting) | **entry dominates**; exit is de-risking |
| Dispersion across tokens | low; many small wins, few losses | **high**; conviction bets, fat tails |
| Win rate | near 50 %, small | closer to the price itself, but larger wins |
| Edge vs price | flat across price bands | concentrated where mispricing is largest |

The discriminator is **entry vs exit**, not carry. A market maker must earn on both legs;
an EV trader earns on the entry and uses the exit to flatten, often at a worse price than
entry, which is why their realised spread can even look negative.

### Falsification

The thesis is dead if, for the top decile:

1. `ENTRY_EDGE ≈ EXIT_EDGE` (symmetric quoting), or
2. `ENTRY_EDGE` per share is within one half-spread of zero, or
3. PnL per token shows low dispersion with a ~50 % win rate.

If instead entry dominates and dispersion is high, the next question is *what they know*:
which observable features at entry time predict the eventual payout better than the price
does. That is the EV model, and it is a modelling problem on data we already hold.

## What is already known, and must not be contradicted

- PnL is monotone in wallet size: top decile +0.192 ¢/$, every other decile negative
  (`docs/results-log.md`).
- The top decile carries 91 % of maker volume, so "top decile" and "the market" are nearly
  the same set.
- Aggregate PnL is zero by conservation — this is verified, and any new metric must satisfy
  it.

## Data required

Held: `data/processed/wallet_token/*.parquet`, role-split (maker/taker) per (wallet, token,
month). Enough for ENTRY_EDGE / EXIT_EDGE, carry, and dispersion.

Not held: order-level time ordering within a token, which is needed for "exit and reverse"
as a sequence rather than a net position. That is a tape-level question and costs one pass.

## Verdict (first pass, 2026-11-02)

**Supported, with a correction.** The round-trip PnL for the top decile is +$191.98 m and
the settlement of what it did not close is −$141.13 m. So the profit is taken by exiting,
and what is carried is a cost — the operator's reading survives.

The correction: the edge is **entry-dominated** (87 % of it) while the exit contributes
almost nothing, and the **token win rate is identical** to the losing deciles (54.0 % vs
53.9 %). So this is not a forecasting edge — it is paying less for the same outcomes. An EV
model that predicts winners would show up as a win-rate or win/loss gap; there is none.

That sharpens the research question from "what do they know?" to **"how do they price the
entry?"** — because they are not winning more bets, they are buying the same bets cheaper.

Not yet explained: 23 % of top-decile flow is `sell_only` (minting and selling one leg).
That is market making rather than EV trading, and both behaviours live in the same wallets.

## Correction and refinement (2026-11-02, after the disposition test)

The entry/exit split reported earlier is payout-dependent and was withdrawn — see
`docs/results-log.md`. The load-bearing facts are now:

- Top decile: +$192 m captured on round trips, −$141 m on positions never closed.
- Exits are taken in profit 56.2 % of the time vs 47.8 % if held (+8.4 pp) — a real but
  modest tilt toward closing winners, not a clean disposition effect.
- The loss sits in **50.5 M never-sold positions averaging −$2.94 at a 50.0 % win rate**,
  not in a few large losing holds.

So the operator's mechanism is half-supported: they do take profit early, but the money is
lost in a long tail that is never dealt with at all, rather than in big positions they
refuse to cut.

---

## Consolidated understanding (end of overnight session, 2026-11-02)

### Established, with the arithmetic verified

| finding | evidence | confidence |
|---|---|---|
| The top decile earns by **taking**, not making | takers +$150.9 m at +5.6–7.7 %; pure makers −$150.0 m at −1.0 % of volume | high |
| Market making is a **loss centre** | round-trip spread capture +$14.2 m on $14.9 b (0.1 %) vs inventory marking −$163.0 m | high |
| The edge concentrates in **sports** | taker edge +0.0555 in sports vs +0.0134 crypto, +0.0057 politics | high |
| Sports has a large **favourite–longshot bias** | 20–30 % priced resolves 12.2 % (−0.129); 70–80 % priced resolves 88.5 % (+0.136) | high, multiple controls |
| That bias is **decaying** | May −0.094/+0.087 → Sep −0.045/+0.041 | high |
| Their taker edge **does not decay** | +0.031 to +0.043 per share, every month | high |
| No disposition effect | losers closed 61.0 %, winners 55.9 % | high |
| Losses sit in the **middle** of the size distribution | $10–$10 k is 95 % of it; positions above $1 m are profitable | high |

### Not established — and the contradictions that block it

1. **Within price buckets, the top decile's buys resolve ~10 pp better than everyone else's.**
   But measured per token, the two groups trade the *same* token universe (2.0 M shared
   tokens, 100 %) and the top decile pays **more** (+7.1 ¢ mean, cheaper in only 29.7 % of
   tokens) while earning a buy-side edge of ≈0 on those shared tokens.
   Both cannot describe the same trades unless the difference is *timing within a token*,
   which the per-month and per-ttl cuts did not isolate. **This is the open contradiction.**

2. **Buy-side edge is not PnL.** A market maker buys at the bid and sells at the ask; their
   buy-side edge versus the eventual payout understates their economics by the whole spread.
   Several conclusions above were drawn from buy-side edge, and the role split is the only
   cut that used full PnL. The sports/crypto numbers should be re-derived with full PnL
   before they are treated as final.

3. **The `ttl_h` anchor is `end_date`, not actual resolution.** `resolution_timing.parquet`
   has `resolved_ts`, but only 140 sports markets have it, so the timing dimension is weaker
   than it looks. Median `end_date` to `resolved_ts` is +4 h but the spread is huge
   (p10 −133 h).

### What the next session should do, in order

1. **Re-derive the category and price-band results with full PnL** (closed + held + exit)
   instead of buy-side edge. The role split exists; apply it to the category cube cells.
2. **Resolve the within-bucket contradiction** by controlling for token identity: for each
   (token, price bucket), compare the top decile's realised spread against the rest's. If
   they are identical, the earlier "selection" was a composition artefact of bucketing by a
   wallet's *average* price.
3. Only after (1) and (2): decide whether the replicable object is a sports pricing model
   (a real forecasting task) or a liquidity-provision business (which the role split says
   loses money at the top).

### The one-sentence summary

The durable, measurable edge in this data is **buying sports favourites and avoiding
longshots**, it is worth roughly 4–5 % of money deployed, and it is being arbitraged away
month by month while the top decile's own edge stays flat — which is the strongest hint that
they are not simply harvesting that bias.

---

# CORRECTION (2026-11-02, later the same session)

The two findings recorded above — a large favourite–longshot bias in sports, and a +4.8–5.6 %
edge concentrated in a top decile — **do not survive their own controls.** Both were
measurement artifacts. This section replaces them.

## 1. There is no favourite–longshot bias. The market is calibrated.

The earlier calibration used the **volume-weighted average price over the window**
(12–6 h before `end_date`). Re-measured using the price at a **single point in time**, the gap
collapses to ±1 pp:

| price band | edge @ VWAP | edge @ first trade | edge @ last trade |
|---|---|---|---|
| 20–30 % | −0.027 | **+0.004** | −0.005 |
| 30–40 % | −0.028 | **+0.005** | −0.005 |
| 60–70 % | −0.007 | **−0.004** | +0.000 |
| 70–80 % | +0.002 | **−0.006** | +0.001 |
| 80–90 % | +0.007 | **−0.007** | +0.000 |

The VWAP is biased because **volume correlates with the outcome inside the window**: tokens
that go on to win trade more at high prices, so their VWAP sits below the final price. Averaged
across tokens, that looks like "favourites are underpriced". It is an average over a path, not
a price anyone can pay.

Confirmed independently: the edge at the 95th-percentile price and at the maximum price in the
window is also ≈ 0 in every bucket. And the same test one window earlier (17–12 h) gives the
same ±1 pp.

**There is no exploitable favourite–longshot bias in this data.**

## 2. The market is exactly zero-sum.

Summed over 1.2 M wallets and $16.18 b of buys with known payouts:

```
total PnL = $0.0 m
aggregate buy-side edge = −0.0023 per dollar
```

Payouts are funded entirely by the losing side. Every dollar one wallet earns, another loses.

## 3. The "top decile" was a selection artifact.

`wallet_deciles` was built by ranking wallets **on realised PnL over the whole period**, then
the "edge" of the top decile was measured **over that same period**. That is circular: by
construction the top decile of a zero-sum distribution is profitable.

Ranking by **volume** instead (independent of outcome):

| volume decile | wallets | bought | PnL | return |
|---|---|---|---|---|
| 10 (largest) | 120,327 | $16,054 m | +$33.9 m | **+0.21 %** |
| 1–9 | 1,082,943 | $130 m | −$33.9 m | — |

The wallets that carry 99 % of the volume earn **+0.21 %**, not +5 %.

A second test confirms the artifact: within price buckets, matched on entry price to within
2 ¢, the "top decile" and everyone else have **the same edge** (60–70 %: +0.257 vs +0.282 — the
rest is slightly better). The earlier +10 pp within-bucket gap came from bucketing by a
wallet's *average* price, which mixes tokens.

## 4. What does survive: skill persists out-of-sample, but it is small and low-capacity

Split Apr–Jun (A) vs Jul–Sep (B). Rank wallets on A, measure on B. Wallets with ≥ $10 k bought
in both halves (n = 10,444):

| A return decile | wallets | traded in B | PnL in B | B return |
|---|---|---|---|---|
| 1 (worst) | 1,045 | $266 m | −$13.0 m | **−4.91 %** |
| 2 | 1,045 | $253 m | −$10.6 m | −4.17 % |
| 6 | 1,044 | $1,456 m | +$7.7 m | +0.53 % |
| 9 | 1,044 | $430 m | +$9.0 m | +2.11 % |
| 10 (best) | 1,044 | $267 m | +$9.0 m | **+3.38 %** |

- **Spearman(A return, B return) = +0.231** (n = 10,444). Over the full universe with the
  $1 k threshold: **+0.173** (n = 33,255).
- The relation is monotone across deciles: 1 → 10 rises from −4.91 % to +3.38 % in B.
- So some wallets genuinely repeat. That is skill or a persistent structural advantage,
  not luck.
- **But the skilled cohort is small**: the best A-decile traded $267 m in B, while the
  volume-dominant wallets traded $1.46 b in decile 6 for +0.53 %.

### Behaviour of the persistence-surviving cohort, measured on B only (fully out-of-sample)

| cohort | bought | PnL | return | maker_share | sold / bought |
|---|---|---|---|---|---|
| best (A-dec 9–10) | $696 m | +$18.1 m | **+2.59 %** | 0.80 | 170 % |
| middle | $5,099 m | +$29.3 m | +0.58 % | 0.92 | 47 % |
| worst (A-dec 1–2) | $519 m | −$23.6 m | **−4.55 %** | 0.82 | 193 % |

July alone (one month past the selection window): best **+4.77 %**, middle +0.90 %,
worst **−7.06 %**.

Two things the earlier full-period table got wrong, both from including period A:

1. The persistence cohort is **not** taker-heavy out of sample. Its maker share in B is 0.80,
   essentially the same as the middle cohort's 0.92. The 0.41 seen full-period was A-period
   behaviour.
2. "Sold 85 %" was also A-only. In B both the best and the worst cohort turn their book over
   ~170–190 %, i.e. far more than they buy. High turnover is characteristic of **both**
   extremes, so it does not separate skill from its absence.

What survives is the bare persistence: Spearman 0.23, monotone deciles, best +2.6 % vs worst
−4.6 % out of sample. **The mechanism is not identified by any cut run so far.**

### A correction to the correction

The first version of this table was computed with a **sign error** in the cache query:
`buy_cash − sell_cash − shares × payout` is the *negative* of PnL. The Spearman correlation is
sign-invariant, so the persistence result always stood, but every decile label was inverted.
The numbers above are from the corrected formula, validated by the aggregate returning to
$0.0 m.

## 5. Corrected picture

| claim | status |
|---|---|
| Favourite–longshot bias in sports | **retracted** — VWAP artifact |
| Top decile earns +4.8–5.6 % | **retracted** — PnL-ranked selection over the same window |
| Market is zero-sum | **confirmed** ($0.0 m aggregate) |
| Wallets that carry the volume earn ~0.2 % | **confirmed** |
| Some wallets persist at **+6.2 %** | **confirmed** (Spearman 0.23), low-capacity |
| The persistent cohort is taker-heavy, closes 85 % of buys; the volume cohort is maker-heavy and holds | **confirmed** (full period; needs a B-only re-measure) |
| Maker role loses money | **retracted** — the `bc>0` filter drops the sell-only side, so the split sums to +$66.7 m instead of zero, and the "maker loss" is the drift asymmetry |

## 6. What this means for the plan

The search for "a segment where someone has a big edge we can copy" has produced a **negative**
result so far: the market is efficient in aggregate, the apparent biases are artifacts, and the
only persistent skill is small and low-capacity. Per `plan.md` §6, that is exactly the case in
which lowering the bar is the wrong move.

The live question is no longer "where is the mispricing". It is: **what do the persistent
skilled wallets do differently** — and is their advantage structural (speed, information,
relationships, market-making capital) or replicable (a forecasting model)?

That should be answered from the *persistence-surviving* cohort (A-decile 9–10), not from a
PnL-ranked decile.

### The mechanism hunt: no observable behaviour predicts out-of-sample returns

For the 10,444 wallets with ≥ $10 k bought in both halves, Spearman of period-A features against
period-B return:

| feature (measured in A) | Spearman with B return |
|---|---|
| A return | **+0.231** |
| turnover (sold / bought) | −0.059 |
| average ticket size | +0.042 |
| average entry price | +0.039 |
| number of tokens | +0.022 |
| maker share | +0.008 |

Only past return predicts future return. Every behavioural descriptor is within noise of zero.
Quintile cuts show the same:

| quintile | maker share | B return | note |
|---|---|---|---|
| Q1 | 0.13 | **−2.26 %** | pure takers lose |
| Q2 | 0.63 | +2.83 % | |
| Q5 | 0.99 | +0.38 % | |

| quintile (A entry price) | price | B return |
|---|---|---|
| Q1 (longshots) | 0.285 | **−2.70 %** |
| Q3 | 0.525 | +1.81 % |
| Q5 (near-certainties) | 0.964 | −0.11 % |

### Trade-level check

Splitting each wallet's own positions **at random** into two halves (36,476 wallets ≥ $5 k per
half) gives Spearman(half 0, half 1) = **+0.148**. So the edge repeats trade-to-trade, not only
period-to-period. Caveat: correlated tokens inside the same event can inflate this, because the
split is by token and not by event; an event-level split is the next control.

### Concentration

- 437,381 of 947,499 wallets (46.2 %) net positive. Positive PnL totals $409.0 m; the top 100
  wallets carry 33.8 % of it.
- Skill is therefore **broad-based, not a handful of insiders**.

## 7. Where this leaves the project

Every observable, mechanical feature has been tested and rejected as an explanation. What is
left is the possibility that the persistence lives in **which events each wallet chooses** —
i.e. information about the underlying sports/events — which trade data alone cannot reveal.
That is also the least replicable kind of edge.

Before spending more compute, the next session should run these three controls, in order:

1. **Event-level within-wallet split.** Re-run the +0.148 with the split by `condition_id`
   instead of `asset_id`. If it collapses, the "trade-level skill" was correlated-token noise.
2. **A1/A2 vs A/B.** Split A into two random halves and correlate A1 with A2. If that
   correlation is also ≈ 0.23, the persistence is a stable trait (skill or persistent variance);
   if it is much higher, the A→B number is being diluted by something time-varying.
3. **PnL decomposition by event.** For the wallets that persist, check whether their profit
   comes from a small number of events (information) or is spread evenly (superior modelling).

If (1) collapses to zero and (3) shows concentration in a few events, the conclusion is:
**the only persistent edge is private event information, and it is not replicable by us.**
That is a legitimate, plan-relevant answer, and `plan.md` §6 already says what to do with it.

### Control 1 (event-level split): the skill is in event selection

Splitting each wallet's positions by **event** (`condition_id`) rather than by token:

| split | wallets | Spearman(half0, half1) |
|---|---|---|
| token-level | 36,476 | +0.148 |
| **event-level** | 36,209 | **+0.270** |

The event-level correlation is **higher**, not lower. So the persistence is not an artifact of
correlated tokens inside one event — it is concentrated in *which events a wallet chooses*.
Wallets that do well on one disjoint set of events do well on another.

### Control 3 (event concentration): profit is spread, not one lucky hit

| cohort | wallets | median events traded | median share of profit from best single event |
|---|---|---|---|
| best (A-dec 9–10) | 1,575 | 330 | **0.342** |
| middle | 3,090 | 869 | 0.227 |
| worst (A-dec 1–2) | 484 | 320 | **0.544** |

The persistent cohort's profit is spread across ~330 events with a third coming from its best
single event. The losing cohort is far more concentrated (54 % from one event), which is what
a few large mistakes look like. So the skilled wallets are not riding one lucky market; they
are making many slightly-better decisions.

## 8. Session conclusion

What has been established, with the evidence that established it:

| claim | evidence |
|---|---|
| The market is zero-sum | aggregate PnL $0.0 m over 1.2 M wallets |
| The market is calibrated at each point in time | edge at first/last/p95 trade price within ±1 pp |
| No favourite–longshot bias | the VWAP "bias" reverses at a fixed time |
| Wallet skill persists out of sample | Spearman +0.231 (A→B), monotone deciles |
| Skill is at the event-selection level | event-level split +0.270 > token-level +0.148 |
| Skill is not explained by role, price, size, or turnover | all Spearman ≤ 0.06 |
| Skill is spread across many events | 34 % from the best of ~330 events |
| Skill is broad-based, not a small cabal | 46 % of wallets net positive; top 100 = 34 % of positive PnL |

**The honest single sentence:** the market is efficient in aggregate, but a broad set of
wallets persistently chooses *which events* better than the rest, and that edge is neither
explained nor replicated by any mechanical feature — which points at underlying-event
information or modelling rather than at microstructure.

The next session's job is therefore not more data engineering. It is to test whether the
event-selection edge can be **predicted from public information** (schedules, team strength,
venue, rest days) before the event. If it can, it is a forecasting problem — the one the
project is actually set up to solve. If it cannot, the edge is private information and is out
of reach, and `plan.md` §6 governs.

### Calibration by market type (last mechanical avenue)

At the first trade price in the 12–6 h window, every sports market type is calibrated within
±0.005:

| type | tokens | mean price | won | gap |
|---|---|---|---|---|
| other | 110,061 | 0.481 | 0.482 | +0.0010 |
| over/under | 45,486 | 0.477 | 0.472 | −0.0046 |
| match winner | 30,750 | 0.488 | 0.488 | +0.0003 |
| spread | 18,246 | 0.485 | 0.488 | +0.0027 |
| draw | 6,226 | 0.541 | 0.544 | +0.0022 |
| both-teams-to-score | 4,168 | 0.489 | 0.486 | −0.0029 |

Price-band deviations inside types are ≤ 0.04 and inconsistent in sign, i.e. noise. There is no
market type, price band, role, size, or turnover bucket in which the market is systematically
wrong by enough to trade.

### Therefore

Every mechanical explanation is now exhausted:

- aggregate calibration: ±1 pp at a fixed time
- market types: ±0.5 pp
- price bands: ±1 pp
- role, size, turnover: no predictive power (Spearman ≤ 0.06)
- aggregate PnL: exactly zero

The persistence that remains (+0.231 A→B, +0.270 event-level) can only be **event-specific
information or modelling**. That is not a microstructure question and cannot be answered from
trade tape alone. It requires pre-event public data — team strength, schedules, rest, venue,
lineups — and a forecast compared against the market price.

## 9. Is the event-selection skill copyable? No.

The persistence survives, so the natural next question is whether observing the persistent
wallets and following their buys captures anything. Two measurements answer it.

### What they earn

Tokens the persistent cohort (A-dec 9–10) bought in period B: their entry VWAP is **0.4432**,
the realised payout is **0.4654**, so their edge is **+0.0222 per share**.

### What a follower pays: the price impact of their buying

For 5.75 M skilled buy trades in July 2026 (2,088 wallets, 891 M shares), the price of the same
token at the next trade after *h*:

| horizon | observations | mean ΔP | median ΔP |
|---|---|---|---|
| 1 minute | 5,082,557 | **+0.0140** | 0.0000 |
| 10 minutes | 2,987,494 | +0.0095 | 0.0000 |
| 1 hour | 2,417,763 | **+0.0332** | 0.0000 |
| 6 hours | 1,389,251 | +0.0189 | 0.0000 |

The mean rises because a minority of tokens move; the median is flat.

### The subtraction

| follower latency | price paid above theirs | edge left per share |
|---|---|---|
| 1 minute | +1.40 ¢ | **+0.8 ¢** |
| 1 hour | +3.32 ¢ | **−1.1 ¢** |

So the information is real, it is theirs, and it is impounded within about an hour. Copying
after that hour is negative; copying inside a minute leaves less than a cent and is a pure
latency race against 2,088 wallets who are already faster.

**Conclusion: the persistent edge is private event information, it is expressed through taking,
and it is not copyable at our latency.**

### What this means, in one line

The market is efficient; a broad set of wallets still has real event information; that
information is gone from the price within an hour; and there is no mechanical feature of the
book that predicts it. Generating alpha therefore requires being *early with information*, not
being fast with order flow or clever with microstructure.

## 10. Where the persistent edge sits

Period B, persistent cohort (A-dec 9–10), by category:

| category | bought | PnL | return |
|---|---|---|---|
| sports | $568 m | +$12.0 m | **+2.12 %** |
| crypto | $67 m | +$4.7 m | **+6.95 %** |
| geopolitics | $27 m | −$0.2 m | −0.83 % |
| economics / politics / weather / tech | $29 m total | +$1.3 m | +4 to +8 % (tiny) |

And the whole market in B, for contrast, returns **exactly 0.00 % in every category** — the
zero-sum property holds category by category, not only in aggregate.

Inside sports:

| market type | bought | return |
|---|---|---|
| other sports | $265 m | +2.68 % |
| **match winner** | $148 m | **+4.11 %** |
| over/under | $69 m | +0.78 % |
| **advance** | $39 m | **−7.35 %** |
| spread | $24 m | +3.67 % |
| draw | $18 m | −1.12 % |
| both-teams-to-score | $6 m | +8.59 % |

The edge is concentrated in **match-winner markets** and **crypto**, and the cohort *loses* on
"team to advance" markets. That is a specific, falsifiable lead: the information is about
**who wins a match**, not about aggregate scoring or tournament progression.

### What would falsify the "information" reading

If the cohort's match-winner edge came from information, it should decay as the match
approaches only if the information is public (lineups) — or persist if private (injury news
before it breaks). The next session can test the timing of their match-winner buys relative to
kick-off, using `question` text to identify the fixture and the trade timestamp to locate the
buy in the pre-match window. That requires no external data and is the cheapest remaining test.

## 11. The edge is a 6–12 hour pre-event window

Match-winner buys, Apr–Jul 2026, edge per share by hours before `end_date`:

| window | cohort edge/share | everyone else |
|---|---|---|
| 0–6 h | +0.0093 | +0.0077 |
| **6–12 h** | **+0.1327** | +0.0308 |
| 12–18 h | +0.0584 | −0.0500 |
| 18–24 h | +0.1022 | −0.0287 |
| 24–30 h | −0.0301 | +0.0230 |
| 30–36 h | −0.0460 | −0.0197 |
| 36–42 h | +0.0723 | +0.0412 |

Contribution to the cohort's total match-winner edge:

| window | shares bought | edge | % of cohort edge |
|---|---|---|---|
| **6–12 h** | 79.7 m | +$10.6 m | **80.7 %** |
| 0–6 h | 88.2 m | +$0.8 m | 6.3 % |
| 36 h+ | 9.6 m | +$0.7 m | 5.3 % |
| 18–24 h | 6.7 m | +$0.7 m | 5.3 % |
| 12–18 h | 10.2 m | +$0.6 m | 4.5 % |
| 24–36 h | 7.4 m | −$0.3 m | −2.0 % |

**Four fifths of the edge sits in one six-hour band, 6–12 h before the event. In the final six
hours their edge is indistinguishable from everyone else's (0.0093 vs 0.0077).**

That is the shape of an information advantage with a **timing**: whatever they know becomes
knowable hours before the match, they act, and the market closes the gap by kick-off. It is not
speed (the last hours show no edge), and it is not microstructure (the volume-weighted and
fixed-price measurements agree here).

### What this implies for building an edge

A strategy does not need to beat the crowd in the final hour — it cannot. It needs to be **right
in the 6–12 h window**, which means it needs pre-match information that the market has not yet
absorbed: team news, lineups (usually ~1 h, so probably not), travel and rest, weather, venue,
motivation, or a better rating model than the market's.

The cheapest concrete test, and the one the data supports directly: build a team-strength model
from the resolved match-winner markets themselves (the tape gives both the pre-match price and
the outcome for tens of thousands of fixtures), and ask whether it disagrees with the 6–12 h
price in a way that predicts the result. That requires no external data and is the natural next
session.
