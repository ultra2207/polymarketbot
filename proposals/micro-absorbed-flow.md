# Depth that refills is information; depth that empties is a warning

`slug:` micro-absorbed-flow
`claim:` At the touch, the ratio of aggressive volume to net depth removed separates a market where patient makers refill the level (trades are absorbed, markout mean-reverts) from one where the level is consumed and not replaced; gating market participation on that ratio lowers adverse markout on liquid markets with no change to price or size.
`market scope:` high-volume only — needs enough prints at the touch to estimate the ratio; on the reward universe (median 2 book events per 6 h) the estimate is undefined and the rule degrades to baseline.
`data:` `TradeContext.price`, `TradeContext.size`, `TradeContext.side` via `on_trade`; `DecisionContext.bid_size_at_level`, `DecisionContext.ask_size_at_level`, `DecisionContext.bid_queue_ratio`, `DecisionContext.ask_queue_ratio`, `DecisionContext.book.best_bid`, `DecisionContext.book.best_ask`. Optional `prepare.sql` over `data/tape/clob_events` (`price_changes[].price/size/side`) if the rule is precomputed instead of computed in-module.

## Mechanism

When a taker sells into the bid, the level's displayed size falls by the traded size and then either (a) refills within a second or two, because other makers were sitting behind it and re-post immediately, or (b) stays gone, because the makers who were there pulled and do not want the risk.

Case (a) is a *liquid* level: the flow is being absorbed by patient capital, the print is noise, and the mid reverts. Case (b) is a level whose owners know something: the next print goes through, and the mid moves with it. The distinguishing statistic is measurable without any model of who is right — it is the refill ratio

```
resilience = sum over window of max(0, +dDepth at touch)
           / sum over window of max(0, -dDepth at touch)
```

computed separately for each side. A value near 1 means the level is replaced as fast as it is eaten; a value near 0 means the book is being consumed.

Our exposure to the second state is asymmetric: the reward for resting is identical in both states, but the markout is not. So participation should be conditioned on `min(resilience_bid, resilience_ask)` being high — both sides supported, not just the one that happens to be printing.

This is not `micro-depth-imbalance` (which is the *stock* of resting size on each side) nor `micro-print-sweep-detector` (which compares a single print to the level's size). It is the *flow* — how much of the removed depth came back — and it needs at least a handful of prints to estimate, which is why it is a high-volume rule.

## Rule

In-module, per token:

```python
# on_book: remember the touch — TradeContext carries no quote, only price/size/side
self.best_bid, self.best_ask = b.best_bid, b.best_ask

# on_trade: classify the print against the last touch
if t.side == "SELL" and t.price <= self.best_bid + 1e-9:  self.sell_vol += t.size
if t.side == "BUY"  and t.price >= self.best_ask - 1e-9:  self.buy_vol  += t.size

# decide: decay with a 5-minute time constant (not per event: event rate varies), then
# take the depth deltas since the previous call
dt = max(1e-3, ctx.seconds_elapsed - self.prev_t)
k  = math.exp(-dt / 300.0)
for name in ("refill_bid", "eat_bid", "refill_ask", "eat_ask", "sell_vol", "buy_vol"):
    setattr(self, name, k * getattr(self, name))
self.prev_t = ctx.seconds_elapsed
d_bid = ctx.bid_size_at_level - self.prev_bid_depth
d_ask = ctx.ask_size_at_level - self.prev_ask_depth
self.prev_bid_depth, self.prev_ask_depth = ctx.bid_size_at_level, ctx.ask_size_at_level
self.refill_bid += max(0.0,  d_bid);  self.eat_bid += max(0.0, -d_bid)
self.refill_ask += max(0.0,  d_ask);  self.eat_ask += max(0.0, -d_ask)

res_bid = self.refill_bid / max(1e-9, self.eat_bid + ctx.quote_size)   # + quote_size: floor
res_ask = self.refill_ask / max(1e-9, self.eat_ask + ctx.quote_size)

if self.sell_vol + self.buy_vol < 200.0:        return QUOTE both     # not enough flow to judge
if min(res_bid, res_ask) >= 0.8:                return QUOTE both     # absorbed
if min(res_bid, res_ask) <= 0.3:                return PULL both      # consumed, stand aside
return last_state                                                     # hysteresis
```

Window: the accumulators decay with a 5-minute time constant, so the estimate is a recency-weighted half-hour of flow at any event rate. The `+ quote_size` floor stops a single 1-share delta from producing an infinite ratio when the level is thin.

## Why not ruled out

- Facts 1 and 2 are price and size decisions; this is neither.
- Fact 3 (adverse markout at every width) is the exact cost the rule targets on liquid books, where a fixed width cannot help.
- Fact 6 (Kalshi) is irrelevant: no second venue.
- Fact 7 (`quote_shares == rewards_min_size` on 90 % of markets) means the *reward* cost of standing aside is small in absolute dollars on most markets — but this rule is scoped to high-volume markets, where the highvol universe runs with `daily_pool = 0` (`scripts/build_highvol_universe.py`), so `net` is markout-only and the decision is exactly "is the spread capture bigger than the adverse move".
- The engine exposes `bid_size_at_level`/`ask_size_at_level` from the real ladder (`_update_level_queue`), so the resilience statistic is computed on venue data, not on our virtual quote.

## Kill criterion

Kill if fewer than 15 % of assets in the highvol universe accumulate 200 shares of touch prints in an hour (no sample), or if fills made when `min(resilience) <= 0.3` do not have worse 5-minute markout than fills made when it is `>= 0.8`, or if the gate removes more than 40 % of quoting time on markets whose net is positive (it is over-firing).

## Replay cost

No SQL if computed in-module from `on_trade` and ctx depth fields; the same pass as any strategy. If precomputed instead, one `prepare.sql` over `data/tape/clob_events/date=2026-11-06/hour=07` (5.5M `price_change` rows, unnested `price_changes`), roughly 30-60 s in DuckDB. Run against the highvol universe (`--universe data/tape/universe/highvol_latest.json`) because the reward universe has too few prints.
