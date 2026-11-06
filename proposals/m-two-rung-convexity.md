# The distance term cannot be gamed with two levels: it is convex, and an out-of-band rung scores exactly zero

`slug:` m-two-rung-convexity
`focus:` venue formula — can the quadratic distance term be gamed with one qualifying and
one non-qualifying quote?
`market scope:` all markets. The residual rule (pull ineligible sides) fires most on
coarse-tick markets and on wide books, so it is not a liquid-only idea.

## Mechanism

Per-order score is `((v - s)/v)^2 * size` with a hard eligibility cliff at `s > v`
(`src/polymarketbot/rewards/scoring.py:69-79`; the replay's copy at
`src/polymarketbot/paper_trading/rewards.py:18-35`). Two structural facts settle the
two-level question.

1. **For a fixed total size, splitting across two rungs always loses.** Write `w(s)` for the
   score per share. `w` is decreasing and convex in `s` (`w'' = 2/v^2 > 0`). For total size
   `S` split as `a*S` at `s1` and `(1-a)*S` at `s2` with `s1 < s2`,
   `S*[a*w(s1) + (1-a)*w(s2)] <= S*w(s1)`, with equality only at `a = 1`: every share moved
   from the far rung to the near rung gains. Convexity sharpens it — the marginal gain is
   largest for the first share moved — so the optimum is the single tightest rung, not a
   spread of sizes. There is no residual "participation" benefit to a second rung, because
   score is additive across orders on the same side (`rewards/scoring.py:82-143` sums every
   level; `paper_trading/rewards.py:38-92` does the same for the band).
2. **An out-of-band rung contributes exactly zero and still fills.** Eligibility is tested
   only on distance (`rewards/scoring.py:75`); the replay's scoring path never checks the
   band when it decides to rest an order (`engine.py:710-711` scores 0 but the quote is
   still built at `engine.py:721-771` and still fills through the FIFO model at
   `engine.py:780-875`). A non-qualifying resting order is therefore a free option written
   to takers: zero reward, full adverse selection.

So the answer to the question is no, in both directions: the qualifying rung already takes
all the credit available at that price, and the non-qualifying rung is strictly dominated by
pulling it. And in this engine a module cannot even express two orders per side:
`SideActions` carries exactly one `bid_price` and one `ask_price` (`base.py:37-38`), applied
at `engine.py:583-592`, and `generate_quotes` builds one `VirtualQuote` per side
(`engine.py:721-771`).

There is a second reason not to build the experiment: the replay does *not* price the venue's
`Q_min`. The venue takes `max(min(Q1,Q2), max(Q1,Q2)/3)` inside `[0.10, 0.90]`
(`rewards/scoring.py:145-165`), but the replay pays `bid + ask` whenever both sides rest and
`max/3` only when one side is empty (`paper_trading/rewards.py:120-138`). Any measured gain
from a second rung would be an artefact of that additive model.

The one second level that *does* pay is on the other token at the same Q axis (YES bid and
NO ask both feed Q1) — that is exactly `micro-cross-token-substitution`, already tested, and
no rung-count variant survives it.

## Rule

Runtime eligibility hygiene, in `decide(ctx)`: reproduce the price the engine will rest
(`floor((mid - s)/tick) * tick` for the bid, `ceil` for the ask, `engine.py:531-532`), then
compute `s_eff = abs(rested_price - ctx.book.mid) * 100` from `ctx.book.mid` (`base.py:71`),
`ctx.book.tick_size` (`base.py:74`) and `MarketView.rewards_max_spread` (`base.py:60`).

```
if s_eff > v: PULL that side        # it earns 0 and is a free option
else:         QUOTE at full scale   # never split into a far rung
```

The mid moves between updates, so `s_eff` is recomputed every call; a quote that was
eligible at placement can become ineligible without any fill. Pulling costs nothing because
the side was scoring zero, and it removes the fill — in the replay the score is `bid + ask`
(`paper_trading/rewards.py:132-138`), so removing a zero-score side changes the reward by
exactly nothing while removing its fills.

**The state is produced by the engine's own inventory skew, in the baseline.** The target
offset is `max(0.2, 0.75 + skew)` with `skew = (inv / max_inventory) * max_skew_cents`
(`engine.py:517-521`) and `max_skew_cents = 0.5` (`engine.py:78`), so a side's realised
distance ranges from 0.75c to 1.25c before tick rounding (which adds up to `tick`). On a
`v = 1c` market the skewed side is out of band as soon as `|inv| / max_inventory > 0.5`, which
is two fills out of the cap of three (`scripts/replay_all_markets.py:368`). A one-sided or
zero-score side then rests for free until the position is unwound.

**Already implemented:** the quadratic and its cliff (`rewards/scoring.py:75`,
`paper_trading/rewards.py:20-27`); one price per side (`base.py:37-38`). **Not implemented:**
any eligibility check by the engine before resting an order, and the venue's `Q_min`
aggregation in the replay's payout.

## Why not ruled out

Facts 1-8 concern our width, size and markout, not the eligibility of the order that
actually rests. `micro-score-density-split` argues that a smaller `s` is worth more — a
statement about *where* to stand given a choice — while this idea is about the discontinuity:
the order either scores or does not, and when it does not, resting it is pure cost. No
registry entry checks `s_eff > v` at runtime, and none notices that the replay's reward
aggregation is additive rather than the venue's minimum.

## Kill criterion

- Kill if fewer than 2 % of quote-minutes in the slice are ineligible: the cliff is not being
  crossed and the rule is a no-op.
- Kill if the pulls do not improve paired net: i.e. the ineligible sides' fills carried
  positive markout, which would contradict fact 3's adverse-at-every-width measurement.
- Kill the *ladder* half permanently if a two-rung simulation under the venue's `Q_min`
  formula earns less than a single rung at the same total size — which the convexity
  argument says it must.

## Data needed

`DecisionContext` only: `book.mid`, `book.tick_size`, `quote_size`, plus
`MarketView.rewards_max_spread`. No prepared table, no new feed. The `Q_min` re-pricing
test is a harness change (score both sides, then `max(min, max/3)`), not a data need.
