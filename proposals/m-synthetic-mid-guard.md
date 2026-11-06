# The engine invents a mid when one side is empty; anything resting on that mid is mispriced by a half-spread

`slug:` m-synthetic-mid-guard
`focus:` venue mechanism — the mid the distance term is measured from is a data state, not a price
`market scope:` all markets; the state is rare on liquid books (which update both sides) and
common on thin ones, so this is a *defensive* rule that pays exactly where the reward is
smallest — its value is in avoiding the tail fill, not in raising the average reward.

## Mechanism

`update_orderbook` derives the mid from the touch, with a synthetic fallback when a side is
missing (`src/polymarketbot/paper_trading/engine.py:286-296`):

```
if best_bid > 0 and best_ask < 1 and best_ask > best_bid:  mid = (best_bid + best_ask) / 2
elif best_bid > 0:                                        mid = best_bid + 0.01;  spread = 0.02
elif best_ask < 1:                                        mid = best_ask - 0.01;  spread = 0.02
else:                                                     mid = 0.50;            spread = 0.02
```

Every quote is then priced at `mid ± s` (`engine.py:520-535`), and the reward distance is
computed from the same synthetic mid (`engine.py:700-711`). When the ask side is empty there
is no observed fair value above the best bid: the true offer could be anywhere up to $1. The
engine places our ask at `best_bid + 0.01 + s`, i.e. within a cent of the bid, and the tape's
next print — if it is a buy — lifts it. In a genuinely one-sided book that is not a
liquidity-provision fill; it is buying the right to be the counterparty to whoever knows the
offer is gone.

The same fallback also sets `spread = 0.02` regardless of the true book, so every
spread-conditioned rule in the programme reads a fabricated 2c. That makes the state doubly
dangerous: the rule cannot tell it is in the fallback, because `book.spread_cents` says 2.0
(`base.py:75`).

Note the harness filters prices before feeding the engine
(`scripts/replay_all_markets.py:452-460`: bids kept in `[0.01, 0.98]`, asks in `[0.02, 0.99]`)
and skips an asset only when **both** sides are empty, so a one-sided book does reach
`update_orderbook`.

## Rule

In `decide(ctx)`, detect the state from the raw touch rather than from the derived mid:

```
one_sided = (ctx.book.best_bid <= 0.0) or (ctx.book.best_ask >= 1.0)
degenerate = ctx.book.spread_cents == 2.0 and 0.01 <= ctx.book.mid <= 0.99 and one_sided
if degenerate: PULL both sides
```

`best_bid`/`best_ask`/`spread_cents`/`mid` are all on `BookContext`
(`base.py:71-75`), so this needs no new signal and no engine change. The rule is
*state-based, not signal-based*: it never fires on a synthetic mid that happens to be close
to a real one, because the one-sided test is on the raw touch.

**Already implemented:** the fallback mid itself, and the harness's skip of fully empty books.
**Not implemented:** any flag distinguishing a synthetic mid from an observed one. Adding
`BookContext.mid_source` (or a boolean `one_sided`) would be the clean fix, and this proposal
works without it by testing the touch.

## Why not ruled out

`micro-thin-level-sweep` and `micro-depth-imbalance` use level sizes as a signal; neither
knows the *mid itself* is fabricated in the one-sided state, and both would read a synthetic
`spread_cents = 2.0` as real. `micro-spread-widening-retreat` reacts to widening, which the
fallback masks by pinning the spread at 2c. Facts 1-8 are measured on markets with both sides
present, so nothing refutes this. The one-sided state is a book-data condition, not a price or
size decision.

## Kill criterion

- Kill if fewer than 0.5 % of `decide` calls in the slice see a one-sided book: nothing to
  guard.
- Kill if fills taken in the one-sided state have markout no worse than the slice median —
  then the synthetic mid is harmless on this tape and the guard is decoration.
- Kill if the guard's removed fills were profitable (positive markout), which would mean the
  fabricated mid is actually a source of good fills and the mechanism is backwards.

## Data needed

`DecisionContext` only. The diagnostic (count one-sided updates per market, and the markout of
fills taken in that state) needs the fills frame (`scripts/replay_all_markets.py:544-570,
:625`) with `markout_5m_tape`, which the harness already computes.
