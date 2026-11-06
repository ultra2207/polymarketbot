# A stale complement is a free option: pull when the sibling repriced and we did not

`slug:` micro-complement-staleness
`claim:` YES and NO mid must sum to 1; when the sibling token's mid has moved and ours has not, our resting pair is priced off a fair value the other book has already rejected, and fills in that state carry worse 5-minute markout than fills at `|gap| <= 1 tick`.
`market scope:` all conditions with both tokens on the tape (both are registered per condition by `replay_all_markets.py`).
`data:` module state only — `BookContext.asset_id`, `BookContext.mid`, `BookContext.tick_size` from `on_book`; `MarketView.tokens` in `setup`. No prepared signals.

## Mechanism

The two tokens of a condition are complements: `mid_YES + mid_NO = 1` up to the spread, and arbitrage by merge/split enforces it (`src/polymarketbot/rewards/merger.py`). The engine recomputes each token's mid only from *that token's* book. On a fast tape the two books update out of order: the sibling's book takes two or three prints and its mid moves 2-3c while our book has not ticked, because the flow hit the other token first.

While that gap is open, our quote prices are anchored to the stale mid. A taker who watches both books — or simply the next print in the sibling — can lift our ask (cheap, because our implied fair value is now higher) or hit our bid at a price the sibling already left. We are the last maker in the condition to reprice, so we are the one who absorbs the fill. This is pure adverse selection with no reward compensation: the reward is unchanged by the gap, only the markout is.

Measured on the tape: of 188 conditions with both tokens traded in one hour partition, the mean absolute deviation of the cross-token mid sum from 1 is 2.55c — above the 1c tick, so the dislocation is a real, recurring state rather than a rounding artefact.

The rule is symmetric in the sense that matters: it pulls *both sides of the lagging token*, never one side. The leading token keeps quoting, so the condition is never left one-sided.

## Rule

One module instance per condition; `setup` stores `self.tokens` and clears `self.last_mid` per asset.

```python
def on_book(self, b):                       # BookContext
    self.last_mid[b.asset_id] = (b.mid, b.tick_size)

def decide(self, ctx):
    sibling = next(t for t in self.tokens if t != ctx.book.asset_id)
    if sibling not in self.last_mid:                 return QUOTE both
    mid_s, tick_s = self.last_mid[sibling]
    implied = 1.0 - mid_s
    gap     = ctx.book.mid - implied                 # >0: we are too high (ask is cheap)
    tol     = max(2.0 * ctx.book.tick_size, 0.015)   # 2 ticks or 1.5c, whichever larger
    if abs(gap) <= tol:                              return QUOTE both
    if sign(gap) == sign(self.prev_gap) and abs(gap) > abs(self.prev_gap):
        return PULL both, "gap widening: sibling is leading, we are the stale side"
    return QUOTE both, "gap present but closing"
```

The widening test is the discriminator: a gap that is closing is a gap we can quote through (the sibling is coming back to us); a gap that is widening means the sibling is the informed book and we are the stale one. That avoids pulling on every tick of cross-book noise.

## Why not ruled out

- Facts 1 and 2 are about price and size; this rule changes neither.
- Fact 3 (adverse markout at every width) is exactly the loss this rule avoids, without touching width.
- Fact 8 (mid cohorts) is orthogonal: the rule does not depend on the price level.
- Neither `micro-cross-token-substitution` (moves *size* between tokens to preserve `Q_min`) nor `micro-sibling-ofi` (uses the sibling's *order flow* to predict which of our sides is threatened) reads the sibling's *mid level*. This rule is about a price-level inconsistency between complementary books, and its action is a two-sided pull on the lagging token, not a reallocation.

## Kill criterion

Kill if the `|gap| > tol` state occurs in under 1 % of events (no dislocation to trade around), or if the 5-minute markout of fills made while `|gap| > tol and widening` is not at least 50 bps worse than fills made at `|gap| <= tol`. The fill log from any baseline replay already carries `markout_5m` per fill, so this is a post-hoc join, not a new experiment.

## Replay cost

No SQL. One strategy run on the standard slice; the module needs no exogenous data. The harness registers both tokens per condition (`MarketConfig.tokens` in `replay_all_markets.py`), so sibling state is populated without extra plumbing. Cheap enough to test on the full 500-market slice rather than a subsample.
