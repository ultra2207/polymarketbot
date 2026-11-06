# A 20-share quote at 5c is a $1 order: the venue's 5 USDC minimum makes the reward minimum unreachable in low-price markets

`slug:` m-min-order-notional
`focus:` venue constraint — minimum order notional versus the reward minimum size
`market scope:` all markets. The constraint binds on low-price conditions (mid below ~0.25
at a 20-share reward minimum), which are also the conditions fact 8 says carry the worst
markout.

## Mechanism

Two minimums apply to the same order and they are denominated differently:

- the reward minimum size, `rewards_min_size` shares (20 in this universe: p10 = p50 = 20 over
  5,809 markets in `data/tape/universe/*.json`), enforced by the venue's scorer and mirrored
  by the engine (`rewards/scoring.py:69-72`, and the replay's floor at
  `src/polymarketbot/paper_trading/engine.py:640-644`);
- the venue's **minimum order size, e.g. 5 USDC notional** (`docs/polymarket-mechanics.md`
  §6), which the replay does not model anywhere.

At a 20-share reward minimum the two are equal only at `mid = 0.25`. Below it, the smallest
legal order is `5 / mid` shares — 100 shares at 5c, 250 at 2c — which is 5-12x the reward
minimum:

```
min_legal_shares = max(rewards_min_size, 5.0 / mid)
```

Three consequences, all invisible today:

1. **The pool-scaled allocation cannot place a legal order.** The harness sets
   `quote_size = max(min_size, notional / 0.5)` (`scripts/replay_all_markets.py:295-299`,
   always at `mid0 = 0.5`) and `max_inventory_shares = max(min_size, 3 * quote_size)`
   (`:368`). At 5c, a legal 100-share order is 5x `quote_size` and 1.7x the inventory cap, so
   the market is unquotable at the harness's capital — yet the replay happily rests 20 shares
   and books a reward for it.
2. **Score per dollar is not what it looks like.** The score is linear in shares, so the
   legal 100-share order scores 5x the 20-share one, but it also commits 5x the inventory and
   takes 5x the adverse selection at a price where fact 8 says markout is worst.
3. **The reward eligibility and the order eligibility can be mutually exclusive** in exactly
   the markets where the band is wide relative to the price.

This is a *selection* mechanism, not a size rule: the actionable decision is whether a market
can be quoted at all inside the capital the replay gives it.

## Rule

At `setup`/first book, compute `min_legal = max(market.rewards_min_size, 5.0 / ctx.book.mid)`
using `MarketView.rewards_min_size` (`base.py:59`) and `ctx.book.mid` (`base.py:71`). Then:

```
if min_legal > 3.0 * ctx.quote_size:        PULL both sides   # no legal order fits the cap
elif min_legal > ctx.quote_size:            QUOTE both, expect a share the reporting understates
                                            # (the replay rests quote_size; the venue rests min_legal)
else:                                       QUOTE both
```

The middle branch is a *reporting* flag rather than an action: it marks markets where the
replay's reward per share is a lower bound on the venue's (the venue would force a larger
order) and where its markout is an upper bound (the venue would take more risk). Those markets
should be excluded from any ranking that compares reward per dollar across prices.

The replay cannot *validate* the `PULL` branch, because the net it reports there is earned on
an order the venue would reject. The replay test is therefore an accounting one — how much of
the low-price cohort's reported `net` comes from orders below 5 USDC — and the pull itself is
a live-trading gate that should be dormant in backtest.

**Already implemented:** the reward minimum size in the research scorer
(`rewards/scoring.py:69-72`) and the engine's floor of any positive size up to
`rewards_min_size` (`engine.py:640-644`). **Not implemented:** the venue's 5 USDC minimum
order notional anywhere in `src/` or in `scripts/replay_all_markets.py`.

## Why not ruled out

Fact 8 (adverse markout worst below 0.10) makes low-price markets unattractive on the markout
side; this idea says they are *also* unquotable at the harness's capital, which is a different
and stronger statement, and it predicts the direction of the bias rather than the level.
`micro-payout-floor` screens on the `$1` daily payout floor; `micro-tail-price-bps` argues
reward and risk per dollar are both `1/price`-shaped — this idea says the *order grid* is
`1/price`-shaped and can make the order illegal, which neither covers. No registry entry
mentions the 5 USDC minimum.

## Kill criterion

- Kill if fewer than 5 % of the slice's markets have `5 / mid > quote_size` at their median
  mid (the constraint almost never binds and the screen is decoration).
- Kill if the excluded cohort's net is indistinguishable from the kept cohort's (then the
  legality argument has no economic content on this tape).
- Kill if the universe's mid p10 is above 0.25 across the tested slices — every market is then
  legally quotable at `min_size` and the whole mechanism is vacuous.

## Data needed

Universe snapshot (`data/tape/universe/*.json`: `min_size`, `daily_pool`) plus the first book
per asset from `data/tape/clob_events` to get the mid. No new feed. The 5 USDC figure should
be re-verified per market against Gamma's `minimum_order_size` field before the rule is
promoted out of shadow, since `docs/polymarket-mechanics.md` §6 states it as an example value.
