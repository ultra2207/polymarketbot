# The tick grid caps the reachable score, so some pools cannot pay for the risk

`slug:` micro-tick-quantum-score
`claim:` At a fixed resting offset, the quadratic score a quote can reach is quantised by the tick; markets whose band holds few ticks are worth strictly less per share than fine-tick markets at the same nominal `rate_per_day`, and the difference is computable from `MarketView.rewards_max_spread` and `BookContext.tick_size` alone, before a single fill.
`market scope:` all markets (structural screen); largest effect on wide-band/fine-tick markets.
`data:` `MarketView.rewards_max_spread` (cents), `MarketView.rate_per_day`, `DecisionContext.book.tick_size`, `DecisionContext.book.spread_cents`, `DecisionContext.book.competing_score`. `data/derived/universe_metadata.parquet` (`tick_size`) only to sanity-check coverage. No prepared signals.

## Mechanism

The venue pays `((v - d) / v)^2 * size` per qualifying order, where `v = rewards_max_spread` and `d` is the distance from mid in cents. `d` is not free: it is a multiple of the tick. So the set of reachable scores at a given band is `{((v - k*t)/v)^2}` for integer `k`, and a market with `v/t = 2.5` can only offer 36 % or 4 % of the maximum, while `v/t = 25` (tick 0.001) offers a near-continuous menu up to ~94 %. Two consequences the pool does not adjust for:

1. **Ceiling.** At the best reachable price (`k = 1`) a coarse market pays at most `((v-t)/v)^2` of nominal per share. The nominal `rate_per_day` overstates the achievable $/share by that factor.
2. **Clustering.** Because only two or three distinct scores exist, every competitor in the market lands on the *same* tick, so `competing_score` is concentrated at the maximum reachable level rather than spread across the band. Our `S/(S+C)` share is therefore computed against a competitor set that is artificially strong, at exactly the price where we must also sit.

Both effects make the reward per unit of markout risk worse in coarse markets at identical pool size, and neither is a spread-width decision: the quote stays at the established offset; we decline markets where that offset cannot reach a score that pays.

## Rule

`setup(market)` stores `self.v = market.rewards_max_spread` (cents) and `self.pool = market.rate_per_day`. The tick is per-book state (`BookContext.tick_size`, and is 0.001 on some markets), so the arithmetic belongs in `decide`:

```python
def decide(ctx):
    t = ctx.book.tick_size                 # probability units, e.g. 0.01 or 0.001
    v = self.v                             # cents
    ticks_in_band = v / (t * 100.0)        # e.g. 2.5 / (0.01 * 100) = 2.5
    best_frac = ((ticks_in_band - 1.0) / ticks_in_band) ** 2   # k = 1 tick, deepest reachable
    ...
```

`decide(ctx)` continues:

```python
if self.pool <= 0.0:                      return PULL both, "no pool"
if ticks_in_band < 2.0:                   return PULL both, "band holds no scoreable tick"
if best_frac < 0.25:                      return PULL both, "ceiling under a quarter of nominal"
if ticks_in_band <= 4 and ctx.book.competing_score > ctx.bid_score + ctx.ask_score:
                                          return PULL both, "crowd is stacked on the only tick"
return QUOTE both at full scale           # price untouched: baseline offset
```

`ticks_in_band < 2` is a hard zero: with `d >= t` and `v < 2t`, no reachable price scores. The `competition` branch is the clustering case: when the crowd's score already exceeds ours, `S/(S+C) < 0.5` at a price we cannot improve on, so the marginal reward does not cover a fill.

## Why not ruled out

- Fact 1 (spread sweep) establishes that *tighter* rests win; this rule never changes the offset. It says the same offset converts to a different fraction of the nominal pool depending on the tick, which fact 1's sweep held fixed.
- Fact 2 (size up is right) is untouched: size is unchanged, the rule only removes markets.
- Fact 3 (adverse markout everywhere) is the reason a capped ceiling matters: when reward per share is capped by the grid but markout is not, the EV ordering flips.
- Fact 7 (`SHRINK` no-op) does not bind: the rule never shrinks.
- The `max_spread` values in `data/derived/market_scorecard.parquet` include 0 (53 conditions) and 2.5c/3.5c with both 0.01 and 0.001 ticks, so both regimes exist in the replay slice.

## Kill criterion

Kill if fewer than 5 % of the scored universe fails the screen (nothing to remove), or if `best_frac` does not rank `rewards_usd` per share across markets at comparable `rate_per_day` (Spearman < 0.2), or if paired net is not positive on both slices. The second test is decisive and cheap: it can be run from an existing replay's per-market rows without a new strategy at all.

## Replay cost

No SQL, no signals; one pass of `scripts/replay_all_markets.py` on the standard 500-market, 10-minute slice. The screen is arithmetic on two scalars, so the only cost is the baseline run itself. Run at `--pool-multiplier 1.0` so the removed reward is visible (fact 7: at 0.25 the reward term is too small to measure).
