# Payout is concave in our own size, so capital belongs where the marginal share is highest — and the replay never ranks it

`slug:` m-marginal-share-rank
`focus:` portfolio — per-market capital allocation
`market scope:` all markets. This is a selection rule, so it is only as good as the
cross-sectional spread in `P*C/(S+C)^2`, which is widest where pools and depths are unequal.

## Mechanism

Per-minute payout is `P/1440 * S/(S+C)`. It is **concave in our own score** `S`
(`d2/dS2 = -2C/(S+C)^3 < 0`), so the marginal dollar of reward falls as we pile size into one
market:

```
d/dS [P * S/(S+C)] = P * C / (S+C)^2          marginal payout per share of score
```

Two consequences, and only the second is actionable here:

1. Splitting our score across two independent pools pays more than concentrating it:
   `2 * (S/2)/((S/2)+C) > S/(S+C)` for the same `P` and `C`. Diversification of *our own size*
   is not a risk statement, it is an arithmetic one.
2. Ranking markets by marginal payout is therefore the right allocation statistic, and it is
   nothing like ranking by pool size.

The replay does neither. Every market gets its own engine and its own notional from
`capital_for`, which is `min(notional_max, max(notional_min, pool_multiplier * pool))` divided
by the mid (`scripts/replay_all_markets.py:295-299`, applied at `363-368`): allocation is
proportional to the pool and blind to `C`. `LiquidityRewardsAllocator.optimize_allocation_milp`
(`src/polymarketbot/rewards/allocator.py:148-388`) and the re-solve governor
(`allocator.py:608-660`) already solve the correct concave problem with the `$1` floor — and
the harness imports neither. Same for the switching hurdle
(`src/polymarketbot/rewards/switching.py:83-152`).

So the whole portfolio layer is built and unplugged; what is missing in the *replay* is the
expression of it that a `decide(ctx)` can perform: refuse capital to a market whose marginal
payout is poor.

## Rule

`prepare.sql` computes, once per hour per condition, the cross-sectional distribution of
marginal payout over the live universe and emits a percentile per asset:
`marg_rank` in `[0,1]`, plus `marg_p25`, `marg_p50` for the hysteresis band. Per asset:

```
marg = ctx.rate_per_day * C / (S + C)^2       # C = ctx.book.competing_score (base.py:79),
                                              # S = ctx.quote_size (base.py:110)
                                              # rate_per_day = base.py:113
if marg_rank < 0.25 and not latched_off:  PULL both, latch_off
if latched_off and marg_rank > 0.50:      unlatch, QUOTE both
```

The one-sided hysteresis (25th percentile to re-enter, 50th to stay) is what pays the
switching cost exactly once instead of chattering; without it the rule re-enters on noise and
pays the cost repeatedly, which is the failure mode `calculate_switching_cost`
(`switching.py:83`) exists to price.

**Already implemented:** the MILP, the `$1` floor inside it
(`allocator.py:130-147`), the endogenous reserve (`allocator.py:515-595`), the governor, and
the switching cost function. **Not implemented:** any wiring from those to the replay, and any
per-market self-selection rule at the `decide` level. This proposal deliberately adds nothing
to the allocator: it only gives the module the allocator's *conclusion* as a signal.

## Why not ruled out

Fact 2 (size up to the cap is right) is a within-market statement and is respected: nothing
here changes size. `micro-competition-dilution` is also within-market and is a size rule;
`micro-payout-floor` is a static `$1` screen that ignores `C` entirely; `micro-adaptive-exit`
uses our own realised PnL, not the cross-sectional rank. Facts 1-8 contain no cross-sectional
allocation result. The concavity argument is a property of the formula, not a prior.

## Kill criterion

- Kill if Spearman correlation between `marg_rank` and per-market net is below 0.2 on the
  slice: the rank is not informative and the rule is a random pull.
- Kill if more than 40 % of markets sit permanently below the 25th percentile (then this is a
  static filter and `micro-payout-floor` already covers it; the value added is zero).
- Kill if the pulled cohort's baseline net is positive per share — the rule would be removing
  profitable markets.
- Kill if the hysteresis changes nothing: exit count equals re-entry-pair count within 1 %.

## Data needed

`prepare.sql` over `data/tape/universe/*.json` (`daily_pool`, `min_size`, `max_spread`) plus
a per-(asset, minute) band score from `clob_events` to estimate `C` (same construction as
`micro-competition-dilution`: replay `price_change` deltas onto `book` snapshots). Emits
`(asset_id, ts_second, marg_rank, marg_p25, marg_p50)`. No new feed.
