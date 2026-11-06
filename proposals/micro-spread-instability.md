# A flickering touch is a tax: quote through direction, not through churn

`slug:` micro-spread-instability
`claim:` When the touch updates many times while the mid stays within one tick, the book is being probed rather than repriced; resting a fixed quote through that state yields fills that mean-revert immediately, so gating on touch-update intensity at constant mid improves paired net at unchanged width and size.
`market scope:` all markets with frequent book updates; meaningless where the book updates a few times an hour.
`data:` `BookContext.best_bid`, `BookContext.best_ask`, `BookContext.mid`, `BookContext.spread_cents`, `BookContext.tick_size` sampled in `on_book`; `DecisionContext.seconds_elapsed`, `DecisionContext.ofi`. No prepared signals.

## Mechanism

Two different things produce book events: a genuine repricing (mid moves, informed) and a probe (a maker nudges a quote by a tick and pulls it back). The engine's `ofi` is the mean of top-of-book step signs, so a symmetric probe sequence averages to zero and the gate sees nothing. But the *number* of updates is visible, and it is the difference between a quiet book and a book where a latency arbitrageur is cycling quotes.

During flicker our quote is systematically the stale side: the prober lifts our ask, the mid reverts, we are down the move. The reward is unchanged (we are resting either way), so the only effect is markout. It is a pure loss-avoidance rule.

This is the second moment of the touch, not its level: `micro-spread-widening-retreat` reacts to the spread getting wider, and `micro-oscillation-vs-trend` classifies mid moves by same-sign runs. Neither counts touch updates at a constant mid.

## Rule

On each `on_book` (the engine calls it for every book update of this asset):

```python
# decide(): one sample per book update; DecisionContext carries the replay clock,
# BookContext does not, so the sample is taken here, not in on_book().
self.touch.append((ctx.seconds_elapsed, ctx.book.best_bid, ctx.book.best_ask,
                   ctx.book.mid, ctx.book.spread_cents))            # deque, maxlen 40

window      = [s for s in self.touch if s[0] >= ctx.seconds_elapsed - 10.0]
updates_10s = len(window)
mid_range   = max(s[3] for s in window) - min(s[3] for s in window)
tick        = ctx.book.tick_size

flicker = updates_10s >= 8 and mid_range <= 1.5 * tick        # churn without direction
calm    = updates_10s <= 3 or mid_range > 3.0 * tick

if len(self.touch) < 20:          return QUOTE both
if flicker:                       return PULL both, f"churn {updates_10s}/10s at range {mid_range:.3f}"
if calm:                          return QUOTE both
return last_state                 # hysteresis in the ambiguous band
```

The window is measured on `DecisionContext.seconds_elapsed`, which the replay drives with the simulated clock (`clock=lambda: now_ts[0]` in `replay_all_markets.py`), so no wall-clock read is involved.

## Why not ruled out

- Fact 1 establishes the winning offset; this rule keeps it and only decides when the offset is safe to rest.
- Fact 2: size is untouched, and fact 7 (`SHRINK` no-op) does not bind because the action is `PULL`, not `SHRINK`.
- Fact 3 (adverse markout at every width) is the premise: flicker is one of the mechanisms that makes markout adverse at a width that looks profitable on paper.
- Fact 4 and 5 (equalisation, time-vs-size) are about sizing; this is about timing.
- `ofi` already exists in `DecisionContext` and is explicitly *not* used as the trigger, because the measured step-mean nets to zero in a symmetric probe — the idea is falsifiable precisely because it claims the second moment carries information the first moment discards.

## Kill criterion

Kill if `flicker` states cover under 2 % of book events (nothing to gate), or if fills made during flicker do not have worse 5-minute markout than fills made during `calm`, or if paired net is not positive on both slices. Also kill if `ofi`-gated baseline already dominates: a rule that only reproduces the existing OFI gate adds nothing.

## Replay cost

No SQL, no signals, one strategy run. The deque is 40 tuples per asset; replay cost is unchanged from baseline. Test on a full-hour slice (`date=2026-11-06/hour=07`, 5.5M `price_change` rows, 2,026 assets) rather than the 10-minute slice, because flicker episodes are short-lived and a 10-minute window may contain too few to measure.
