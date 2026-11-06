# The merge channel is inert: the threshold is 5x the quote size, so no pair ever merges

`slug:` m-merge-min-threshold
`focus:` treasury — merge/redeem economics and cash recycling
`market scope:` all markets. Effect scales with fill count, so it is largest on liquid
markets and on any slice long enough to produce three or more fills per market.

## Mechanism

Merging is fully implemented and, in isolation, correct: `check_and_execute_merge`
(`src/polymarketbot/paper_trading/engine.py:1137-1160`) fires after every fill
(`engine.py:957` and `engine.py:1051`) and `execute_merge` (`engine.py:1162-1233`) burns
`min(YES, NO)` of one condition into $1 minus `merge_gas_fee_usd` (`engine.py:81,1183`),
recording the pair PnL and reducing both token positions.

The economics are entirely about *capacity*, not about carry. Holding one YES and one NO
to resolution pays exactly $1 with no gas, so a merge is never worth more than the gas it
costs in PnL terms — `net` marks inventory at mid (`scripts/replay_all_markets.py:556`),
and the two mids sum to 1 up to the touch spread. What a merge buys is *room*: the quote
size test is

```
bid_size = quote_size * bid_scale if inv < max_inventory_shares else 0.0
ask_size = quote_size * ask_scale if inv > -max_inventory_shares else 0.0
```

(`engine.py:634-635`), and the harness sets `max_inventory_shares = max(min_size, 3 *
quote_size)` (`scripts/replay_all_markets.py:368`). Three fills on a side therefore kill
that side, at which point the book is one-sided and `normalise_two_sided`
(`src/polymarketbot/paper_trading/rewards.py:132-138`) pays `max(bid, ask) / 3`. Merging
pairs is the only operation that gives the room back, and it costs $0.015.

But the threshold is `auto_merge_threshold = 100.0` shares (`engine.py:80`), or
`min_merge_threshold_shares = 30` when a risk manager is attached
(`src/polymarketbot/risk/models.py:149`; the replay attaches none,
`scripts/replay_all_markets.py:363-368`). Against a typical `quote_size` of 20-40 shares
and roughly 0.1 fills per market per ten minutes (fact 1: 59 fills over 500 markets in
10 min), **the merge never triggers on a 10-minute slice, and rarely on a full day.** Any
conclusion about merging, gas or capital recycling in the existing results is vacuous.

## Rule

Engine/portfolio rule: set the merge threshold to the smallest economically meaningful
pair, `auto_merge_threshold = max(1.0, 1.0 * min_size)` — a single fill pair — and merge on
the *first* call after `min(yes, no) >= 1`.

Be honest about the break-even, because it is a time budget, not a share count. With the two
books consistent the pair's mids sum to 1, so the merge is MTM-neutral and the only return is
the restored side. That side earns, per minute,

```
(rate_per_day / 1440) * share * w * quote_size
```

which at `rate_per_day = $7.5` (a $30 pool at the 0.25 multiplier), `share = 0.2`,
`w = ((1c - 0.75c)/1c)^2 = 0.06` and `quote_size = 20` is about **$0.00125/min**. Gas of
$0.015 is therefore repaid by roughly **12 minutes of restored two-sided quoting**. Two
consequences: a 10-minute slice cannot show the benefit (it should not), and the test must be
run at `--pool-multiplier 1.0` (fact 7's rule) or with `--minutes 60`, where the same quote
earns 4x and the break-even is ~3 minutes.

Module-expressible companion, for the half a module *can* control: when
`abs(ctx.inventory_shares) / ctx.max_inventory_shares > 0.5` (`base.py:99-100`) and the
sibling token holds an opposite position (both books are visible to one module instance per
condition), do **not** ramp the adding side down — the cap is a temporary accounting
constraint that the merge releases, and a ramp (as in `micro-cap-breadth-preserve`)
treats it as permanent. Keep quoting at full scale while the mergeable pair exists.

**Already implemented:** the merge itself, the gas accounting, the threshold check and the
per-market `merges` ledger (`engine.py:1325`, `get_portfolio_summary` at `engine.py:1288`).
Only the threshold value is wrong for this harness. This is a configuration change plus a
module guard, not new engine behaviour.

## Why not ruled out

No registry idea is about merging; `micro-net-complement-inventory` argues about *sizing
into* the cheap complement, and `micro-cap-breadth-preserve` argues about the cap ramp —
neither touches the threshold that makes merging happen at all. Facts 1-3 are about width,
size and markout; fact 7 is about `SHRINK` at `quote_size == min_size`, a different cliff.
Nothing measured so far is sensitive to the merge threshold, because nothing measured so
far has crossed it.

## Kill criterion

- Kill if fewer than 30 markets in the slice ever produce one mergeable pair (rerun at
  `--minutes 60`; at 10 min this is expected and is itself the finding).
- Kill if `net` moves by less than $0.001/share with the threshold at 1 versus 100, with
  merge count and one-sided-quote minutes as the mechanism check.
- Kill if the merged pair's `mid_YES + mid_NO - 1` has p90 below the gas per share, and
  fill counts are unchanged (then gas dominates and capacity was never binding).

## Data needed

`DecisionContext` (`inventory_shares`, `max_inventory_shares`, both books' mids) plus the
engine ledger, which already records `merges`, `total_gas_fees_usd` and
`total_merges_count` (`engine.py:1288-1323`). No new feed.
