# Reward accrues per minute; risk accrues per unit of variance

`slug:` micro-variance-duty-cycle
`claim:` Because the reward for resting is a per-minute quantity that does not depend on volatility, while the expected markout of a fill scales with realised variance, there is a volatility level above which resting is negative EV; duty-cycling participation on realised variance improves net on liquid markets where variance actually moves.
`market scope:` high-volume only — realised variance is only measurable when the mid updates often enough within the window.
`data:` `BookContext.mid` in `on_book`; `DecisionContext.rate_per_day`, `DecisionContext.book.competing_score`, `DecisionContext.bid_score`, `DecisionContext.ask_score`, `DecisionContext.book.spread_cents`, `DecisionContext.quote_size`, `DecisionContext.seconds_elapsed`. No prepared signals.

## Mechanism

Split the per-minute economics into two independent terms:

- Reward per minute, `R = rate_per_day/1440 * S/(S+C)`. Independent of volatility (the venue's score has no variance term).
- Expected markout per fill, `M ≈ -k * sigma * sqrt(H) * quote_size`, where `sigma` is the per-second mid volatility and `H` the horizon over which we cannot hedge. Fills arrive at rate `lambda(sigma)` that increases with volatility.

So `EV/minute = R - lambda(sigma) * k * sigma * sqrt(H) * quote_size`. The second term is increasing in `sigma`, the first is flat. Above a threshold `sigma*`, every extra minute costs money. This is exactly the shape that fact 3 describes ("markout is adverse at every width"): a fixed width cannot escape it, because widening lowers `lambda` but also lowers `R` (fact 1) — but *withdrawing in time* lowers `lambda` without lowering the per-minute `R` of the minutes we do stand.

The key distinction from `micro-vol-scaled-size`: that idea scales the size down with variance, which fact 2 rules out as a primary lever and fact 7 makes a no-op on 90 % of markets. Time is the lever that is still free.

## Rule

```python
# decide(): sample here — DecisionContext carries the replay clock, BookContext does not.
# Keep 5 minutes of (seconds_elapsed, mid); resample onto a fixed 5 s grid.
self.mids.append((ctx.seconds_elapsed, ctx.book.mid))
grid = [mid at t for t in range(int(now - 300), int(now), 5)]   # as-of lookup, last value
r = diff(log(grid))
sigma = stdev(r)                                   # per-5s
h = 300.0 / 5.0                                    # 5 minutes of unhedgeable drift, in steps
cost_per_fill = sigma * sqrt(h) * ctx.quote_size    # $ at mid, first order
share = (ctx.bid_score + ctx.ask_score) / max(1e-9, ctx.bid_score + ctx.ask_score + ctx.book.competing_score)
R_min = ctx.rate_per_day / 1440.0 * share
lam   = (ctx.fills_count - self.prev_fills) / max(1e-3, (ctx.seconds_elapsed - self.prev_t) / 60.0)
self.prev_fills, self.prev_t = ctx.fills_count, ctx.seconds_elapsed

ev = R_min - lam * cost_per_fill
if ctx.rate_per_day <= 0.0:                        # highvol / markout-only universe
    ev = ctx.book.spread_cents / 200.0 * lam_expected - lam * cost_per_fill
if ev >= 0.0:   return QUOTE both
if ev <= -0.25 * abs(R_min + 1e-9): return PULL both   # deadband, no chatter at the crossing
return last_state
```

Both branches use only ctx fields; `fills_per_minute_ema` comes from differencing `DecisionContext.fills_count` against `seconds_elapsed`, and `lam_expected` from the average fill rate observed while quoting.

## Why not ruled out

- Fact 1 fixes width; this rule never changes it.
- Fact 2 (size up is right) is respected: `quote_size` is an input to the cost, never a decision variable.
- Fact 3 is the premise, and it is a *level* statement (markout adverse at every width). This idea is about the *second moment* of that loss: the width sweep held volatility roughly fixed across arms.
- Fact 7 (`SHRINK` no-op): irrelevant, no shrink.
- Fact 8 (markout worst in the low-mid cohorts) is a *cross-sectional* fact about price level; this rule is time-series within a market, so the two compose: run the rule separately per market and it will naturally be less active in the cohorts where the loss is worst.
- On the highvol universe `rate_per_day = 0`, and the `ev <= 0` branch turns the rule into "rest only when the half-spread covers the expected adverse move", which is the only coherent objective when there is no pool.

## Kill criterion

Kill if `ev` has no rank correlation (|rho| < 0.1) with realised per-minute net within markets (compute per market by bucketing minutes by `ev` sign), or if the rule removes more than 60 % of quoting minutes on markets whose baseline net is positive (the cost model overstates), or if paired net is not positive on both slices. Also kill if `sigma` is degenerate (constant) on more than half the highvol assets — then the tape lacks intraday variance and the idea cannot be tested on it.

## Replay cost

No SQL, no signals. One pass on the highvol universe; the module keeps a 60-sample mid deque per token. The 1-hour partition (`date=2026-11-06/hour=07`) is the minimum useful window because a 10-minute slice contains too few volatility regimes.
