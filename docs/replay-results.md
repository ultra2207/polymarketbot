# Replay factory — first results and what is verified

Status of the automated strategy-search pipeline described in `plan.md`.
Numbers here are from `scripts/replay_all_markets.py` on the live tape; every
claim is reproducible with the command shown.

## What exists

| piece | file | state |
|---|---|---|
| all-markets replay | `scripts/replay_all_markets.py` | runs; synthetic audit passes |
| strategy protocol | `src/polymarketbot/strategy/base.py` | protocol + context + loader |
| engine hook | `PaperTradingEngine(strategy=…)` | module replaces the orchestrator block; sizing, post-only and risk stay in the engine |
| built-in baselines | `strategy/builtin.py` | `reactionary`, `queue_aware_ofi` |
| serial scheduler | `scripts/run_idea_queue.py` | runs `implemented` ideas, paired against a baseline, writes `result.json` |
| idea registry | `ideas/registry.json` | schema + generator protocol |

## Verified by test, not by eye

`tests/test_replay_all_markets.py` builds a two-token tape with a known answer:

- a thin bid level (5 shares) with our quote joining it, one 30-share sell
  print → the engine fills the surplus, the queue is consumed first;
- a degenerate one-sided book at 0.01 → **no quote**, market counted as skipped
  (the reward program scores nothing below 0.02, and quoting there produced
  impossible fills);
- a mid series with a known jump → the markout is exact; a series that ends
  before five minutes → **censored**, not invented;
- five replayed minutes → exactly five reward epochs.

P&L identity checked on the worst real market: `net = cash − initial_cash +
inventory_mtm + rewards` reconciles to the cent, including short inventory
(negative `market_value`).

## First real slice

500 markets (top by liquidity, pool ≥ \$15), 10 minutes, one strategy per pass:

| metric | value |
|---|---|
| book events replayed | 516 |
| fills | 87 across 35 markets |
| rewards | \$32.42 total — \$18.46 with competitor book observed, \$13.97 on top-only books |
| markout, 5 min after fill | **+65.6 bps** |
| net | \$41.61 |
| wall clock | 10.6 s per strategy |

Command:

```
.venv/Scripts/python.exe scripts/replay_all_markets.py \
  --minutes 10 --max-markets 500 --order-by liquidity \
  --strategies reactionary,queue_aware_ofi --tag wave0
```

## First strategic result: rest at the touch or stay inside it?

`join_level` (rest at `best_bid`/`best_ask`) versus `queue_aware_ofi` (quote
inside the spread), same 500 markets, same 10 minutes:

| strategy | fills | rewards | markout 5 min | net |
|---|---|---|---|---|
| `join_level` | 129 | \$18.44 | **−201 bps** | \$15.05 |
| `queue_aware_ofi` | 104 | \$34.48 | **−115 bps** | \$31.97 |

Paired per condition: mean net difference −\$0.032, t = −2.84 over 500
conditions, in favour of quoting inside the spread. Two effects, both predicted
by the mechanism: joining the touch gets filled 24 % more often, and those
fills are 86 bps more adversely selected. Inside the spread also scores more of
the pool, because the reward score increases with distance from the touch
penalty curve.

This is one slice of one regime. It is recorded as the direction the first wave
must beat, not as a promotion.

## What this slice says, and what it does not

**It says** the harness produces a stable, reproducible, paired measurement on
real data, fast enough to run tens of ideas per hour. The 87 fills and 516 book
events are enough to rank module behaviour on this slice.

**It does not say** that any strategy is profitable. Three limits are load-bearing:

1. **The two baselines tie** (identical fills and net). That is correct
   behaviour, not a bug: the engine quotes inside the spread, so `queue_ratio`
   is 0 and `queue_aware_ofi` keeps both sides. The queue gate only has
   something to do when a module *joins* an existing level. This is itself a
   finding: the first wave of ideas must include level-joining quotes.
2. **Reward numbers depend on the seeded competitor ladder.** For 298 of 500
   markets the tape has a full book snapshot before the slice, so the
   competitor score is real; for the rest only the top is known and the reward
   is reported separately (`rewards_unobserved_competition`). Never quote the
   combined number alone.
3. **Ten minutes is one regime.** Promotion needs two non-overlapping slices and
   the gates in `plan.md` §6 — 200 fills, 50 conditions, paired t ≥ 2, and no
   single condition above 25 % of the effect.

## Wave 1 — three ideas, three negative results

The first modules written against the protocol, run against `reactionary` on the
same 500 markets and ten minutes:

| idea | mechanism | net diff per condition | t | verdict |
|---|---|---|---|---|
| `micro-jump-withdrawal` | pull both sides for 5 events after a ≥2-tick move | −\$0.060 | −4.10 | fail |
| `micro-queue-erosion-velocity` | shrink the side whose queue ratio falls ≥0.2 in 10 events | −\$0.095 | −6.02 | fail |
| `micro-thin-level-sweep` | shrink the side whose touch level is in its own bottom quartile | \$0.000 | 0.00 | fail (never fires) |

The two failing ideas fail for the same reason, and it is a real economic
reason: **a pull forfeits reward that outweighs the markout it avoids**, on this
slice. `micro-thin-level-sweep` never fires because every quote rests inside the
spread, so `size_at_level` is zero on both sides and the "thin level" condition
can never hold — the idea is only testable under level-joining quotes.

**This forced a scoring fix before the numbers could be trusted.**
`compute_epoch_payout` summed our two sides additively. The venue normalises:

$$Q_{\text{norm}} = Q_{\text{two}} + \frac{\min(Q_{\text{one}}, Q_{\text{two}})}{3}$$

so a fully one-sided quote earns a third of its raw score. The engine was
overstating every pulled side by 3×, which flatters exactly the defensive ideas
above. `normalise_two_sided` now implements the venue rule (tested in
`tests/test_reward_normalisation.py`); the table above is from the corrected
model.

## Wave 2 — four more, all negative, and why

| idea | rule | net diff per condition | t | verdict |
|---|---|---|---|---|
| `micro-post-fill-defence` | shrink the side just hit, for 5 decisions | −\$0.0016 | −1.96 | fail |
| `micro-oscillation-vs-trend` | shrink the threatened side when the mid is trending | −\$0.0000 | −1.00 | fail (no-op) |
| `micro-vol-scaled-size` | size ∝ 1/σ² over a 100-mid window | −\$0.0119 | −3.01 | fail |
| `micro-inventory-skew-size` | shrink the side that grows inventory | +\$0.0006 | +1.22 | fail (below the t ≥ 2 gate) |

Seven ideas, seven failures, and one common cause: **the reward is linear in
size and the markout saved by shrinking is smaller than the reward forfeited.**
Every defensive idea reduces size, so every defensive idea loses on this slice.
The one positive result (inventory skew) is positive but not significant.

That makes the search direction explicit: **selection and timing beat sizing.**
The next wave must change *which markets* we quote and *when*, not how much.

## Wave 3 — sixteen ideas on one frozen slice

Slice `2026-11-06T05:55Z`, 500 markets, 10 minutes, baseline `reactionary`
(quote both sides inside the spread at pool-scaled size). Paired per condition:

| idea | verdict | net diff/cond | t |
|---|---|---|---|
| `micro-queue-erosion-velocity` | fail | +0.01848 | +0.90 |
| `micro-payout-floor` | fail | −0.00030 | −1.00 |
| `micro-competition-dilution` | fail | −0.00046 | −0.58 |
| `micro-jump-withdrawal` | fail | −0.00657 | −0.73 |
| `micro-inventory-skew-size` | fail | −0.00973 | −1.01 |
| `micro-post-fill-defence` | fail | −0.00985 | −1.09 |
| `micro-cap-breadth-preserve` | fail | −0.01076 | −1.06 |
| `micro-vol-scaled-size` | fail | −0.02273 | −0.91 |
| `micro-thin-level-sweep` | fail | −0.02588 | −1.14 |
| `micro-two-sided-coupling` | fail | −0.02605 | −1.16 |
| `micro-tail-price-bps` | fail | −0.02796 | −1.18 |
| `micro-cross-token-substitution` | inert | 0 | — |
| `micro-depth-imbalance` | inert | 0 | — |
| `micro-net-complement-inventory` | inert | 0 | — |
| `micro-oscillation-vs-trend` | inert | 0 | — |
| `micro-reward-priced-pull` | inert | 0 | — |

(`micro-spread-widening-retreat` is also inert and is included in the 16.)

**Zero passes.** The best idea is positive but has t = 0.90, far below the gate
of 2.0; the rest are at or below zero. Six are `inert`: their trigger needs a
signal nobody has prepared yet, or a condition that cannot hold while quoting
inside the spread.

## What 17 negative results mean

They are not noise, and they are not a failed factory — they are a statement
about the economics of this venue as measured:

1. **Reward dominates.** The reward score is linear in size; a fill's markout is
   bounded by the spread. Every idea that reduces size forfeits more reward than
   it saves in markout, and the sign of the paired difference follows the sign
   of the size change almost everywhere in the table.
2. **Price choice has one answer here.** Quoting inside the spread (the
   baseline) beats joining the touch (+\$0.032/condition) because it scores more
   of the pool and gets filled less.
3. **`inert` is a bug report, not a result.** Six modules need a signal: the
   `prepare.sql` pipeline exists (`scripts/build_idea_signals.py`) and one agent
   is authoring six of them, but the modules that read `sibling_*`,
   `depth_imbalance` or `threat` cannot be judged until those land.

The next real test is therefore not another sizing rule. It is either a
**selection** rule (withdraw from a whole market on a market-level feature) or a
**cross-venue** rule with its signal materialised. Both are now unblocked by the
harness; neither has been measured.

## Removed: the implementation of every rejected rule

The modules for rules that were measured and rejected are deleted; their specs,
kill reasons and measurements stay in `ideas/registry.json` and this document, so
the evidence survives without the code that no longer has a use:

| removed | why it cannot be restored |
|---|---|
| `micro-adaptive-exit` | -$3.25, t=-1.28 over 1,048 fills |
| `micro-print-sweep-detector` | exposure effect; per-fill markout t=0.54 |
| `micro-burst-decay-reentry` | same family, same verdict |
| `micro-absorbed-flow` | same family, same verdict |
| `micro-participation-gate` | the union of the three, labelled `exposure-only` by the harness |
| `micro-complement-staleness` | per-fill signs [+54, -42, +257], not robust |
| `micro-maker-vacancy` | +$0.036, t=1.05 over 1,500 condition-slices |

`micro-random-participation-control` is **kept**: it is the instrument that
distinguishes a timing rule from less exposure, and every future participation
idea needs it.

Regenerable analysis caches (558 MB of news mid series, 196 MB of on-chain mid
caches, the whale and comment extracts) were deleted; their findings are in
`docs/signal-audit.md` and the scripts that rebuild them live with the analyses.

## Validation of the synthetic print path: not yet trustworthy

The same slice, same universe, real trade prints against prints rebuilt from
book-level decreases:

| path | fills | rebate | markout median | markout mean | net |
|---|---|---|---|---|---|
| `clob_trades` (real) | **59** | $2.32 | -600 bps | +2,381 bps | -$16.45 |
| synthetic | **2,795** | $428.41 | -341 bps | -571 bps | +$3,591 |

A book level that shrinks is indistinguishable from a cancel, so the synthetic
path counts cancels as fills: **47x more fills and 185x more rebate** than the
trade feed produces for the same slice. The sooner it is used, the more it
inflates; every liquid-universe number in this document inherits that caveat.

Also visible in the same table: the real-path mean markout is **positive**, which
is not a result. Excluding fills whose markout exceeds 5,000 bps - a fill minutes
before resolution marks out to the settlement, not to adverse selection - the
mean becomes **-906.88 bps** against a median of -600. A handful of resolution
jumps had been carrying the sign of the mean.

## The liquid universe: how to test anything on volume

The reward universe and the liquid universe are disjoint, so a strategy that only
works on volume needs three pieces of infrastructure the harness did not have:

| piece | why |
|---|---|
| `scripts/build_highvol_universe.py` | ranks conditions by **all** event types (a `book`-only count measures re-snapshots, not flow), and takes tokens from the tape when a market is newer than the Gamma snapshot |
| `--synthetic-prints` | the trade feed only covers the collector's subscriptions, so the busiest markets have **zero** print rows and could never fill; prints are rebuilt from level decreases (price_change deltas, or consecutive book snapshots for feeds that only send full books) |
| `--metric tape_net` | the replay's own ladder is mutated by every synthetic print, which moves the mid and manufactures markout: ladder median **+312 bps** where the tape shows **-361 bps**, and a "profit" of **+$863** that is really adverse selection. Tape-priced P&L (size x markout x price read from the untouched tape) is the only honest currency here |

`--flat 200` is required alongside: on a zero-pool universe the per-market
notional is otherwise floored at `rewards_min_size`, and every size rule becomes
a no-op for the reason documented below.

## First result on volume: sweeps, not shares

`micro-print-sweep-detector` shrinks both sides for ten decisions when a print
consumes at least half the level it hit - scale-free, unlike the absolute
850-share gate it replaces. Judged on `tape_net`, paired against `reactionary`:

| universe | slice | tape_net diff | t |
|---|---|---|---|
| 40 markets | 02:10-02:30Z | **+$26.25** | 2.82 |
| 40 markets | 02:50-03:10Z | **+$23.48** | 2.41 |
| 70 markets | 02:10-02:30Z | **+$73.36** | 1.52 |

The sign is consistent across all three; the 70-market slice is the honest one
and it does not clear `t >= 2`. Status: promising, not proven.

### The participation family is an exposure effect, not a signal

Pooling the three slices into one statistic (n=210 condition-slices) and adding
the control every participation rule needs - stand down at random for the same
duty cycle, with no information:

| strategy | pooled P&L vs baseline | t | per-fill markout t | verdict |
|---|---|---|---|---|
| `micro-participation-gate` (union of the three) | **+$154.12** | 2.05 | **0.54** | **exposure-only** |
| `micro-random-participation-control` | **+$64.67** | 2.07 | - | pure exposure |
| gate vs control (head to head) | **+$102.92** | 2.01 | - | more stand-down, not better stand-down |

Absolute tape-priced P&L on the liquid universe, one slice:

| strategy | fills | tape_net |
|---|---|---|
| baseline `reactionary` | 2,054 | **-$7,808.76** |
| random stand-down (31 % of decisions) | 1,226 | -$5,049.97 |
| participation gate | 63 | -$126.86 |

Every strategy loses money on liquid markets at a 0.75 c offset: the maker pays
adverse selection and there is no reward pool to fund it. A rule that trades less
therefore loses less, and against a baseline that loses money a "positive P&L
diff" is nothing but less participation. The gate does not survive on per-fill
markout - signs across slices are `[-290, +27, +504]`, pooled t=0.54 - so it has
no timing skill at all.

The harness now refuses to call that a pass: a P&L gain whose per-fill markout
effect is insignificant is reported as `exposure-only`, with the markout t
attached. This is the rule that keeps a loss-avoidance result from being promoted
as an edge, and it is the reason `micro-random-participation-control` exists.

### The three rules judged separately on 02:10 (70 markets):

| rule | what it stands down for | tape_net diff | t |
|---|---|---|---|
| `micro-burst-decay-reentry` | 4x the duration of a >=4-print burst | **+$107.44** | 1.65 |
| `micro-print-sweep-detector` | a print eating half its level | **+$73.36** | 1.52 |
| `micro-absorbed-flow` | a level that only ever shrinks | **+$62.21** | 1.02 |

and on 02:50: burst-decay **+$277.20** (t=1.29), absorbed-flow **+$226.47**
(t=1.13). Every variant is positive on every slice; none reaches `t >= 2`
alone. The reading that fits is a family effect - **reducing participation when
the touch is hostile pays about 1-1.5 % of baseline on liquid markets** - rather
than any one of these rules being the edge.

`micro-adaptive-exit` was killed on the same machinery: -$3.25, t=-1.28 over
1,048 fills. Withdrawing from markets whose own realised P&L is negative
forfeits more than it saves.

## Where to quote: the spread sweep

Baseline (`reactionary`), frozen slice 05:55Z, 500 markets, 10 minutes:

| offset from mid | fills | rewards | net | median markout |
|---|---|---|---|---|
| **0.75c** | 59 | \$12.11 | **−\$8.18** | +222 bps |
| 1.50c | 43 | \$10.06 | −\$9.14 | +375 bps |
| 3.00c | 31 | \$5.17 | −\$10.84 | +600 bps |
| 5.00c | 22 | \$0.64 | −\$13.39 | +1132 bps |

Wider quotes are hit less and their fills are less adverse — and it does not
matter, because the reward score falls faster than the markout improves. The
tightest offset the tick allows wins on every slice where it has been run, which
is why the production default is already 0.75c. Net is negative on this slice at
every width; that is a property of the slice, not of the offset.

## Two ideas the data refutes outright

| idea | why it dies |
|---|---|
| `micro-qmin-equalisation` | The venue pays `Q_norm = Q_two + min(Q_one, Q_two)/3`. With both sides resting `Q_one = 0`, so `Q_norm` is the plain sum and unequal sizes are **not** penalised. The idea assumed the opposite. |
| `micro-time-vs-size` | Refuted by the capital sweep: net rises \$35.19 → \$88.76 as the multiplier goes 0.10 → 1.00. Minimum size is the losing side. |

Both are marked `killed` in the registry with the measurement attached, so a
later agent cannot resurrect them without new evidence.

## The intersection of "paid to quote" and "actively traded" is empty

The mapping was rebuilt from the Polymarket side (rank 5,292 conditions with book
events in six hours, take the top 200, parse each question and match it to a
Kalshi ticker). It qualifies **13 matches, every one with >= 411 events**, and it
halves staleness: the PM mid is **39 s** stale at the median against v2's 255 s.

All 13 pay **$0.00/day in liquidity rewards**. Every actively-traded market in
the matched set is a market people trade because it matters, not one the venue
pays us to quote. That is the whole reason 25 cross-venue ideas report `inert`:

| set | book events / 6 h | reward pool |
|---|---|---|
| reward markets (the replay universe) | median **2** per condition | >= $15/day |
| matched active markets | median **1,031** per condition | **$0.00/day** |

They are disjoint. A cross-venue signal can only be measured on the second set,
where `net` is markout and not reward — which the reward harness cannot express.

## Half the universe cannot be shrunk at all

`quote_shares` equals `rewards_min_size` on most markets at the default 0.25 pool
multiplier (median 20 shares, the floor). A `SHRINK` decision on such a market
floors straight back to 20 and changes nothing, so every size-reduction idea was
untested, not failed. The scheduler now says which it is:

```
cross-kalshi-basis-side-pull: verdict=inert: no shrink headroom
    (90% of markets quote at rewards_min_size)
```

Test size-based ideas with `--pool-multiplier 1.0` or larger so the quote has
range above the venue minimum.

## Two more tested, both rejected

| idea | rule | result | verdict |
|---|---|---|---|
| `micro-maker-vacancy` | quote only below the 25th percentile of observed competing score, leave above the 75th | pooled +$0.036 over 1,500 condition-slices, **t=1.05** | directionally right, economically nothing: the payout is dominated by *which* markets hold the pool, not by their intraday vacancy |
| `micro-complement-staleness` | pull both sides of the token whose mid lags its sibling-implied mid | P&L +$133 (t=1.79), per-fill markout **t=1.62, signs [+54, -42, +257]** | not robust: one slice of three is negative |

## Merge economics: the default threshold is correct

Merging complete YES+NO sets converts inventory to cash, but at these sizes the
gas exceeds the benefit. Same slice, same strategy, only the threshold moved:

| `auto_merge_threshold` | fills | rewards | net |
|---|---|---|---|
| 10 shares | 251 | $18.62 | **-$119.89** |
| 25 shares | 251 | $18.62 | -$113.72 |
| 100 (default) | 251 | $18.62 | -$113.72 |

25 and 100 are identical because no market accumulates between them, so the
channel is inert unless the threshold drops below the quote size - and dropping
it costs $6.17. The merge path is now exposed as `--merge-threshold` so the claim
is reproducible rather than assumed.

## Three harness defects found while re-checking

| defect | effect | status |
|---|---|---|
| `latest_universe` sorted filenames, so `highvol_latest.json` outranked every timestamped snapshot | a plain reward replay silently loaded the liquid universe and found no markets at reward hours | fixed: only `YYYYMMDDTHHMMSS.json` counts as a snapshot |
| the venue scores an order only if it rested >= 3.5 s in the sampled minute; the engine scored every quote | rewards overstated; quotes are usually older than 3.5 s, so the correction is small | fixed: quote timestamps come from the engine clock. Reward at 0.75c on the frozen slice: $1.789 -> $1.692 (-5 %) |
| `capital_for` sizes every market at `mid = 0.5`, before any book exists | shares are over-sized on cheap tokens and under-sized on expensive ones by `0.5/mid`; paired comparisons are unaffected | **documented, not fixed**: the mid is not known until the book arrives, and no mid field exists in the universe snapshot |

## A scoring defect the review found: the pool was paid twice

`run_epoch_sample` looped over a market's two tokens and paid
`rate_per_day * our_share` **per token**, so a maker quoting both YES and NO
collected twice the pool the venue actually pays. Every reward figure in this
document before this fix is therefore about 2x too high, and the paired
differences are unaffected in sign but scaled. The payout is now computed once
per market from the sum of both tokens' scores.

The same review claimed the engine adds `bid + ask` scores instead of the venue's
`Q_norm`; that is **false** - `normalise_two_sided(bid_score, ask_score)` is what
feeds `compute_epoch_payout`. Checked before adopting, as with every agent claim.

## Why the cross-venue family is blocked: the mapped markets are silent

The first cross-venue idea (`cross-kalshi-basis-side-pull`) was implemented end
to end — `prepare.sql` builds 10,240 basis rows, the harness injects them as-of,
the module shrinks the threatened side and flips the sign for a NO quote — and
the verdict is `inert`. The reason is data, not code:

| measure | value |
|---|---|
| mapped conditions in the reward set | 53 of 107 |
| assets with any book event in a two-hour window | 28 |
| book events per minute across the whole mapped set | **1–3** |
| book events per mapped condition over 6 hours | median **2**, 47 of 107 with **zero**, 6 with ≥500 |
| PM-mid staleness in the basis join | median **255 s**, p90 **2,634 s** |
| maximum \|basis\| observed | **1.72** logit points (the spec's ±3 is unreachable) |
| fills in a 30-minute slice on 53 mapped markets | **0** |

The Kalshi mapping targets reward-tail markets: they carry the pool, but almost
no Polymarket book activity. A cross-venue rule cannot be calibrated on 1–3
events per minute, so more `prepare.sql` files would only produce more `inert`
verdicts. The fix is a mapping audit that prefers *active* Polymarket markets
over high-pool ones — a research task, not a pipeline task.

## Capital scaling (500 markets, 10 minutes, `reactionary`)

| pool multiplier | notional range | fills | rewards | net |
|---|---|---|---|---|
| 0.10 | \$10–200 | 67 | \$33.38 | \$35.19 |
| 0.25 | \$10–500 | 67 | \$37.52 | \$40.33 |
| 0.50 | \$10–1000 | 59 | \$42.01 | \$65.54 |
| 1.00 | \$10–2000 | 59 | \$51.02 | \$88.76 |

Rewards grow with size as the score is linear, but net grows faster than rewards
because the extra size completes inventory pairs rather than adding exposure.
This is one slice; it sets a hypothesis (size up to the inventory cap is
reward-positive), not a rule.

## Markout: three fixes, and one caveat that outranks them

1. **Side sign.** The markout was `(mid − fill)/fill` for both sides; a sell
   gains when the mid *falls*. Every average before this fix was meaningless.
2. **Coverage.** A mark was computed at fill time, so a fill whose horizon lay
   past the last book change was silently censored — 15 of 58 fills were
   marked. Marks are now computed after the series is complete and terminated at
   the tracking horizon: **58 of 58**.
3. **Horizon.** The tracking tail is 900 s so a fill at the end of the slice
   still has a 5-minute mark.

The caveat: with this fill model a maker's markout on the slice is **positive**
(mean +188 bps, median +204 bps over 58 fills). A passive maker should show
*negative* markout (adverse selection). The model fills us at our quote when a
print trades through it, including when that quote was stale after a jump, and
the market then reverts. That is conservative in one direction and generous in
another, so **markout is reported as a mechanism check, never as a promotion
gate**, until it is reconciled against a fill model that cancels stale quotes.

## The idea inventory

Two generator agents wrote **50 specs** (25 `micro-*`, 25 `cross-*`) with a
dedupe check over mechanism text (no pair above 0.22 token Jaccard). Each spec
has a falsifiable claim, a numeric kill condition, the tape table and columns it
needs, and a `prepare.sql` when the module cannot run from the context alone.
Both generators audited their tables against the tape rather than the docs and
recorded where the two disagree (crypto threshold binaries are sentinels at
pool 0; `kalshi_books` starts 11-05 19:11; `uma_events` has no disputes).

Three of the 25 `micro-` ideas depend on the venue's official `Q_min`/`c=3`
rule that the engine now implements; the reward-model change above means none of
the 50 has been tested against the *old* model.

## Next

- Two generator agents are writing 50 specs (`micro-*`, `cross-*`), each with a
  kill condition, into `ideas/`.
- The scheduler runs `implemented` ideas serially against `queue_aware_ofi` on
  the same slice, writes `ideas/<slug>/result.json`, and flips the registry.
- First wave target: 5 implemented ideas, of which at least two join an existing
  level so the queue gate is actually exercised.
