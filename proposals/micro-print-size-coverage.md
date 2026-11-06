# Queue protection is only as good as the print-size distribution

`slug:` micro-print-size-coverage
`claim:` `bid_queue_ratio` says how much size sits ahead of us, but the size that matters is the *typical print*: on a market whose prints routinely exceed the level, a high queue ratio is not protection. Conditional participation on `P(print > queue_ahead)` improves markout on liquid markets at unchanged price and size.
`market scope:` high-volume only — the empirical print distribution needs a few hundred prints; on thin markets the quantile is undefined and the rule falls back to baseline.
`data:` `TradeContext.size` via `on_trade` (or `DecisionContext.signals` from a `prepare.sql` over `data/tape/clob_trades`); `DecisionContext.bid_size_at_level`, `DecisionContext.ask_size_at_level`, `DecisionContext.bid_queue_ratio`, `DecisionContext.ask_queue_ratio`, `DecisionContext.quote_size`, `DecisionContext.book.spread_cents`, `DecisionContext.seconds_elapsed`.

## Mechanism

The engine's fill model is FIFO: a print at our price fills us only with the surplus above `queue_ahead` (`on_trade`, `surplus = trade_size - queue_ahead`). So the probability that a print reaching our level fills us is `P(S > q_ahead)`, and the probability that it fills us *and* continues past the level — the adverse case, where the mid moves against us by the time we can hedge — is `P(S >> q_ahead)`.

Both probabilities are properties of the market's print-size distribution, not of our queue ratio alone. A market with 200 shares ahead and a median print of 20 shares is protected; a market with 200 shares ahead and a median print of 2,000 is not, even though both show the same `bid_queue_ratio`. The queue ratio is a *relative* measure and the print distribution is *absolute*; the engine exposes both and the two are never combined today.

This gives a clean conditional: quote only when the level ahead absorbs the typical print, i.e. `q_ahead >= q90(print)`. On such a market our fills are the rare large print and our queue position is real. On the opposite market we are paying the full markout of a level that never protected us.

Not a size rule: the rule does not change `quote_size`; it decides whether the *existing* size is worth resting. Not a width rule: prices are untouched. Not `micro-thin-level-sweep`, which compares the level to a *single* print against a threshold, rather than to the market's own print distribution.

## Rule

```python
# on_trade
self.sizes.append(t.size)                          # deque(maxlen=400)

# decide
q_bid = ctx.bid_size_at_level * ctx.bid_queue_ratio       # shares actually ahead of us
q_ask = ctx.ask_size_at_level * ctx.ask_queue_ratio
if len(self.sizes) < 200:               return QUOTE both   # no distribution yet
q90 = quantile(self.sizes, 0.90)

covered_bid = q_bid >= q90
covered_ask = q_ask >= q90
if covered_bid and covered_ask:         return QUOTE both   # level absorbs the typical print
if not covered_bid and not covered_ask: return PULL both    # neither side protects us
return last_state                                           # one-sided coverage: keep hysteresis
```

Note `q_ahead ≈ level_size * queue_ratio`: `_update_level_queue` maintains `q_ahead + q_behind` against the same level, so `ratio = q_ahead / (q_ahead + q_behind)` and the two exposed fields (`*_size_at_level`, `*_queue_ratio`) reconstruct the shares actually ahead of us without reading engine internals.

The one-sided branch is deliberately passed through to hysteresis rather than acted on: acting on one covered side and not the other is a one-sided decision, which this wave forbids. The market-level gate is the only action.

## Why not ruled out

- Fact 1 and fact 2 fix price and size; this rule keeps both.
- Fact 3 is the cost; the rule is a condition on where the cost is actually borne.
- Fact 7 (`SHRINK` no-op at `quote_size == rewards_min_size`): the rule never shrinks, so it is unaffected; but note that on 90 % of reward markets `quote_size == rewards_min_size`, so the only way to reduce *exposure* without dropping below the qualifying size is to drop out of the market entirely — which is what this rule does.
- `micro-join-vs-lead` observes that the queue ratio is only meaningful when we join a level; this rule takes the ratio as given and asks whether the *level's absolute size* covers the market's own print sizes. Different quantity, different data.
- On the highvol universe (`daily_pool = 0`, `scripts/build_highvol_universe.py`) the decision is pure markout economics, which is where this rule is meant to be judged.

## Kill criterion

Kill if fewer than 10 % of highvol assets accumulate 200 prints in the replay window, or if fills made when `not covered_bid and not covered_ask` do not have 5-minute markout at least 25 % worse than fills when both sides are covered, or if the gate is almost always on (coverage true in > 90 % of events — then the market is uniformly deep and the statistic is not informative).

## Replay cost

In-module from `on_trade`, no SQL, one pass against the highvol universe. If the replay's trade feed is thin for the chosen markets, precompute `p50/p90 print size` per asset in 15-minute buckets via `prepare.sql` over `data/tape/clob_trades` (2,252 trades/hour across 597 assets in `date=2026-11-06/hour=07` — one partition is enough for a smoke test, three hours for a stable `q90`).
