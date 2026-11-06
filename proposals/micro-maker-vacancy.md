# Stand aside while the professional is there; the pool pays share, not time

`slug:` micro-maker-vacancy
`claim:` Because epoch payout is `S / (S + C)`, a quote earns almost nothing during the windows when one competitor holds a large score; a rule that only rests when `competing_score` is in the bottom half of its own recent distribution captures most of the reward for roughly half the fill risk, improving paired net.
`market scope:` all markets; strongest where one maker dominates the band.
`data:` `DecisionContext.book.competing_score`, `DecisionContext.bid_score`, `DecisionContext.ask_score`, `DecisionContext.book.spread_cents`, `DecisionContext.book.tick_size`, `DecisionContext.rate_per_day`. No prepared signals.

## Mechanism

`compute_epoch_payout` pays `v_score / (v_score + competing_score)`. The derivative is steep at small `C` and flat at large `C`: if a competitor's score is 10x ours, doubling our time in the market from 0 to full only moves the share from 0 to 9 %. Conversely, when `C` collapses (the professional pulls, or their size rolls off the band), the same quote captures a 5-10x larger share for the same risk.

So the two decision variables — minutes present and dollars of markout risk — are not proportional. Being present 50 % of the time at the low-`C` half can capture more than 50 % of the reward at 50 % of the risk. This is a *timing* rule: it never changes the price, the size, or the two-sidedness, and the pool never notices we were absent.

The second-order effect matters too: `C` is high exactly when a large order sits at the touch, which is also when our queue is longest and our fill is most likely to be a residual of a sweep. The reward-side and risk-side arguments point the same way, which is what makes a pure threshold credible.

## Rule

In-module rolling history of `ctx.book.competing_score`, one deque per token, last 240 book events (≈ 2-10 minutes depending on market activity; a count-based window is required because `decide` fires on book updates, not on a clock).

```python
c      = ctx.book.competing_score
p25, p75 = quantiles(hist, .25), quantiles(hist, .75)
mine   = ctx.bid_score + ctx.ask_score

if len(hist) < 30:                       return QUOTE both          # no history, baseline
if c <= p25 and c <= 3.0 * mine:         return QUOTE both          # vacancy, cheap share
if c >= p75 and c >= 6.0 * mine:         return PULL both           # outgunned: share ~ 0
return QUOTE both if last_state == QUOTE else PULL both             # hysteresis, no chatter
```

Hysteresis (re-enter at `p25`, leave at `p75`) is required: without it the rule oscillates at the median and pays the queue-reset cost on every re-entry. Every re-entry resets `queue_ahead` to the full level size (`generate_quotes`, "Price changed or new quote"), so a rule that re-enters n times per minute is strictly worse than staying out.

## Why not ruled out

- Fact 1 fixes *where* to stand and does not consider competitor presence; the vacancy rule holds the winning offset constant.
- Fact 2 (size up) is about the size of a resting quote, not about which minutes it rests in; nothing here shrinks.
- Fact 4 (`Q_one = 0` when both sides rest, unequal sides unpenalised) removes the equalisation motive but says nothing about presence.
- Fact 7 (`SHRINK` is a no-op at `quote_size == rewards_min_size`) explains why a *size*-based version of this idea cannot work on 90 % of markets — a presence-based version does not have that failure mode, which is the point.
- `competing_score` is computed by the engine over the whole book inside the band (`compute_competing_depth_score`), so the signal is measured, not modelled, on every event.

Overlap to declare: `micro-competition-dilution` reasons about the *level* of `C` (share is concave in size, linear in competition). This rule is the time-domain dual: it keeps the same quote and moves the presence fraction, and its measurement is "reward per minute present", not "share at fixed size".

## Kill criterion

Kill if `competing_score` has autocorrelation < 0.3 at a 30-event lag (then the p25/p75 windows are noise and the rule is coin-flipping), or if the p75 windows carry more than 50 % of reward in the baseline (then high `C` means a big pool, not a dominant competitor — the threshold is mis-specified), or if paired net is not positive on both slices. Also kill if re-entries exceed 2 per market-minute: that is the queue-reset tax dominating.

## Replay cost

No SQL. One baseline run plus one strategy run on the standard 500-market slice. `competing_score` is already in `DecisionContext.book`, so the strategy is self-contained; run at `--pool-multiplier 1.0` because at 0.25 the absolute reward delta is below the noise of a 10-minute slice.
