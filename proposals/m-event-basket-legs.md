# K legs of one event are one bet: the reward is paid per leg, the risk is the event overround

`slug:` m-event-basket-legs
`focus:` portfolio — correlated inventory across markets
`market scope:` high-volume only. The mechanism needs a live multi-leg event with flow on
most legs (elections, big sports markets). On single-market events there is nothing to
correlate and the rule is inert.

## Mechanism

In a neg-risk event, `sum_i p_YES,i = 1` is enforced by conversion (1 NO in one leg returns
1 YES in every other leg, `docs/polymarket-mechanics.md` §3). So the K legs are not K
independent quoting opportunities: they are one bet on the event's overround. A taker basket,
or a converter working the set, fills several of our legs in the same instant, which means
the inventory we accumulate across legs is perfectly correlated rather than diversified.

The reward, by contrast, is paid per leg against each leg's own pool. That asymmetry is the
whole idea: revenue scales with the number of legs we quote, risk scales with the *overround*
of the basket we would be holding, and the two are not proportional. Quoting K legs whose
`S - 1` is large is collecting K pools in exchange for one guaranteed loss equal to the
overround; quoting the same K legs when `S - 1` is within the touch spread is collecting K
pools for free.

Leg *selection* matters as much as leg count: the pools are not equal, so the marginal reward
of the K-th leg can be a tenth of the first, while the incremental correlated risk is
identical.

## Rule

`prepare.sql` emits per (condition, minute): `event_leg_sum` (sum of fresh legs' mids,
staleness <= 60 s), `event_leg_count`, `event_leg_rank` (percentile of this leg's
`rate_per_day` within the event), and `event_leg_fresh`. In `decide(ctx)`:

```
if signals["event_leg_fresh"] == 0:            PULL both
elif abs(signals["event_leg_sum"] - 1) >= 0.02: PULL both   # the basket is a bet, not a hedge
elif signals["event_leg_count"] > 4 and signals["event_leg_rank"] < 0.5:
                                                PULL both   # pay-your-share leg, full basket risk
else:                                           QUOTE both
```

The overround threshold is the touch spread, not zero: below it the basket risk is already
priced by the maker who fills us, and pulling there would forfeit the pool for nothing.

**Already implemented:** event identity is carried end to end — `MarketConfig.event_id`
(`src/polymarketbot/paper_trading/models.py`) and `_event_exposure`
(`src/polymarketbot/paper_trading/engine.py:360-377`) aggregate market value and cost across
every condition in one event, feeding `validate_quote` (`engine.py:651-699`). **But the replay
attaches no risk manager** (`scripts/replay_all_markets.py:363-368`), so event limits never
bind in any measured result. **Not implemented:** the overround as a signal, and any leg-count
or leg-rank rule.

## Why not ruled out

`cross-negrisk-convert-flow` trades the *direction* that converter flow implies and never
looks at how many legs we hold. `micro-cross-token-substitution` moves size between the two
tokens of one condition, which is orthogonal to leg count across an event. Facts 1-8 are
single-market. Nothing in the registry computes `sum p - 1` as a risk budget, and nothing caps
leg count.

## Kill criterion

- Kill if fewer than 20 multi-leg events are live in the slice (no power; use an election or
  playoff hour rather than a random one).
- Kill if `|sum p - 1|` has p50 below 0.005: the overround is below the touch, so the risk
  side of the trade is negligible and the leg cap is only a reward loss.
- Kill if adding the leg cap and the overround gate moves universe `net` by less than
  $0.001/share, with quoted-leg-count and basket-filled-leg-count as the mechanism checks.

## Data needed

Tape only: `data/tape/universe/*.json` (`condition_id`, `event_id`, `neg_risk`, `clob_tokens`,
`daily_pool`) plus `clob_events` for every leg of the event. No new feed, no new venue.
