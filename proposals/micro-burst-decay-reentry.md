# Fills arrive in clusters, so the cluster is the unit of risk

`slug:` micro-burst-decay-reentry
`claim:` Trade arrivals are clustered (a burst is one parent order split into children); the markout of fills inside a burst is worse than baseline, and the burst's own duration forecasts when it is safe to rest again, so a cooldown proportional to burst length improves net on liquid markets at unchanged price and size.
`market scope:` high-volume only — needs enough prints per 10 s to define a baseline arrival rate; on the reward universe the estimator never has a sample.
`data:` `TradeContext.seconds_elapsed`, `TradeContext.size`, `TradeContext.side` via `on_trade`; `DecisionContext.seconds_elapsed`, `DecisionContext.book.spread_cents`, `DecisionContext.book.mid`. No prepared signals.

## Mechanism

Informed execution is not a single trade: it is a parent order sliced into children, arriving over tens of seconds to minutes. That is a *cluster* in the arrival process, and it is exactly the period in which a resting maker is run over: the first child takes the front of the queue, the second takes us, the third happens after the mid has moved. The reward we earn for resting through the burst is the same as any other minute; the markout is not.

The useful part is the *decay*: a burst has an observable duration, and the hazard of further children falls after the parent is filled. So the correct re-entry time is a function of how long the burst ran, not a fixed cooldown. A 5-second flap should re-enter quickly; a 90-second execution programme should keep us out for minutes.

This is a timing rule at market level. It is the arrival-process dual of `micro-jump-withdrawal` (which reacts to a single price jump) and of `micro-oscillation-vs-trend` (which classifies mid runs); neither looks at inter-arrival times.

## Rule

```python
# on_trade
self.ts.append(t.seconds_elapsed)                     # deque(maxlen=500)
# baseline: at setup, lam_b = (len-1)/(ts[-1]-ts[0]); refresh with an EMA of 5-minute bins

# decide
now  = ctx.seconds_elapsed
recent = count of ts in [now-10, now]
expected = lam_b * 10.0
if lam_b is None or len(self.ts) < 40:      return QUOTE both     # no baseline

if recent >= max(4.0, 4.0 * expected):
    if self.burst_start is None:
        self.burst_start = now
    self.burst_end = now
    return PULL both, f"burst {recent}/10s vs {expected:.1f} expected"

if self.burst_end is not None:
    length = self.burst_end - self.burst_start
    cool   = min(300.0, max(20.0, 4.0 * length))
    if now - self.burst_end < cool:
        return PULL both, f"post-burst cooldown {cool:.0f}s"
    if recent <= 1.5 * expected:                          # decayed: safe to re-enter
        self.burst_start = self.burst_end = None
        return QUOTE both

return last_state
```

`lam_b` must be refreshed slowly (EMA over 5-minute bins) so one burst does not redefine "normal"; the setup sample is only a seed. The 4x multiplier on burst length is the free parameter and should be grid-tested at 2x/4x/8x, not tuned to a single slice.

## Why not ruled out

- Fact 1 (spread) and fact 2 (size): neither is touched. The rule removes minutes, not width or shares.
- Fact 3 (adverse markout at every width) is the loss being concentrated: a burst is the sub-population of minutes where a fixed width is most expensive.
- Fact 4/5 are sizing dead ends; irrelevant here.
- Fact 7 (`SHRINK` no-op) does not bind: the action is PULL.
- The engine calls `on_trade` for every print (`replay_all_markets.py` line ~520) and `decide` on every book update, so the burst state machine is re-evaluated at the right cadence on a liquid book; on a thin book it degrades to baseline by the `len < 40` guard.

## Kill criterion

Kill if fewer than 50 bursts occur in the replay window (no sample), or if the 5-minute markout of fills made inside bursts is not at least 25 % worse than outside, or if the cooldown removes more than 25 % of a liquid market's quoting time (it is over-firing, or the baseline `lam_b` is mis-estimated), or if paired net is not positive on both slices.

## Replay cost

No SQL, no signals, one pass on the highvol universe. Use at least the busiest hour partition (`date=2026-11-06/hour=07`: 2,252 trades across 597 assets) and preferably the full day; a 10-minute slice cannot contain enough bursts to estimate anything.
