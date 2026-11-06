# Repricing restarts both the 3.5 s eligibility clock and the FIFO queue; a reprice budget is free insurance

`slug:` m-rest-clock-reprice-budget
`focus:` epoch cadence — the only place timing binds inside a minute
`market scope:` all markets; fires hardest where the mid ticks often, i.e. liquid markets.
On a quiet book the budget is never hit.

## Mechanism

The venue requires an order to rest at least **3.5 s** to score
(`src/polymarketbot/rewards/scoring.py:26,51,73,105`: `min_duration_seconds = 3.5`, and both
`quadratic_order_score` and `compute_book_side_scores` drop shorter orders). On a CLOB a price
change is a cancel plus a new order, so **every reprice restarts that clock** and resets the
FIFO position at the level. Two costs, one of which the replay models and one it does not:

1. Queue priority: modelled. When our price differs from the previous quote the engine resets
   `queue_ahead` to the whole displayed level
   (`src/polymarketbot/paper_trading/engine.py:732-737` for the bid, `:761-769` for the ask);
   when the price is unchanged it retains the old `queue_ahead` (`engine.py:723-731`).
   So a frozen price is a strictly better queue position.
2. Eligibility: not modelled. The replay's scoring path calls
   `compute_quadratic_score(v, actual_bid_spread, bid_size)`
   (`engine.py:710-711`) into `src/polymarketbot/paper_trading/rewards.py:18-35`, which has no
   duration argument. A strategy that reprices every 0.5 s is therefore paid in full by the
   replay and paid **zero** by the venue whenever the sample lands inside a 3.5 s dead window.

So the measured benefit of a reprice budget is a *lower bound*: the queue-priority gain is
real in the replay, and the eligibility gain is real but invisible. Freezing the price cannot
be worse on either model, because a frozen quote still rests at the last chosen price and the
engine's post-only clamp is re-evaluated against the current book every update
(`engine.py:535-543`).

## Rule

In `decide(ctx)`, keep per side the last price actually returned and the
`ctx.seconds_elapsed` (`base.py:98`) at which it changed. Compute the target price the engine
would use from `ctx.book.mid`, `ctx.book.tick_size` and the offset, then:

```
if ctx.seconds_elapsed - last_change_t[side] < 3.5:
        return SideActions(bid_price=last_bid, ask_price=last_ask, ...)
else:
        return the rule's own price, and record the change
```

The price override is the documented mechanism for this: `SideActions.bid_price` /
`ask_price` (`base.py:37-38`) are applied verbatim (tick-rounded, then post-only clamped) at
`engine.py:583-592`. Resting the same price is what preserves `queue_ahead`
(`engine.py:723-731`).

Do **not** combine this with a re-quote-every-update policy: the budget only makes sense if
the underlying rule would otherwise churn. Compose it as a wrapper around any price-producing
rule.

**Already implemented:** queue-priority retention at an unchanged price, and the price
override. **Not implemented:** the 3.5 s minimum-rest rule in the replay's scoring path
(`paper_trading/rewards.py:18-35`), and any reprice cadence control in a strategy.

## Why not ruled out

`micro-queue-erosion-velocity` measures how fast `queue_ahead` erodes at a fixed level;
`micro-reward-priced-pull` prices a *pull*; neither prices a *reprice*, which is the only
action that resets both the clock and the queue. Facts 1-8 are about width, size and markout,
not about action cadence. The venue's 3.5 s rule is documented in `rewards/scoring.py` and is
simply absent from the replay, so no measured result contradicts this claim.

## Kill criterion

- Kill if the baseline changes a side's price less than once per 3.5 s on average (measure
  price changes per side per minute from the module's own state or from
  `engine.last_orchestrator_decision`): the budget never binds and the idea is a no-op.
- Kill if fill count is unchanged after adding the budget (the queue-priority argument then
  has no effect in this tape).
- Kill if paired net is unchanged or worse at unchanged reward — that would mean the extra
  fills from repricing were profitable, contradicting fact 3.

## Data needed

`DecisionContext` only: `book.mid`, `book.tick_size`, `seconds_elapsed`. No prepared signal,
no new feed. The venue-side half (dead windows) cannot be measured without a duration-aware
scorer; that is a scoring change, stated here as a lower-bound caveat rather than a data need.
