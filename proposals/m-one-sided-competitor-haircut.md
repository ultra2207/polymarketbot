# A one-sided competitor is discounted to a third by the venue and counted in full by the replay

`slug:` m-one-sided-competitor-haircut
`focus:` venue formula — the `Q_min` aggregation applied to *other* makers
`market scope:` all markets; the haircut matters most where a band is walled on one side,
which is common on liquid one-directional flow and rare on quiet books.

## Mechanism

The venue aggregates each maker's orders into `Q_one = YES bids + NO asks` and
`Q_two = YES asks + NO bids`, then scores that maker at
`max(min(Q_one, Q_two), max(Q_one, Q_two)/3)` inside `[0.10, 0.90]` and `min(Q_one, Q_two)`
outside it (`src/polymarketbot/rewards/scoring.py:82-165`). A competitor who rests only on
one side therefore contributes **one third** of their raw score to the denominator they share
with us; a competitor who rests on both contributes all of it.

Our measure of competition does not know this. `compute_competing_depth_score` sums every
qualifying bid and every qualifying ask into one number
(`src/polymarketbot/paper_trading/rewards.py:38-92`), and the engine stores that single value
as `orderbooks[asset]["competing_score"]` (`src/polymarketbot/paper_trading/engine.py:307-319`),
which is what every rule reads as `BookContext.competing_score` (`base.py:79`). So a market
whose band is one walled side is scored as if the wall were three times its venue weight.
Our true share there is `S/(S + C/3)`, not `S/(S + C)`.

The bias is one-directional and large: up to 3x in the denominator, which is up to 3x in the
share when `C >> S`. It changes *which markets* are worth entering, and it is exactly the
formula-level asymmetry the two-sidedness requirement creates — applied to the other side of
the ledger, which no existing idea does.

## Rule

`prepare.sql` emits, per (asset, second), the band score split by side:

```
s_b   = (mid - p) * 100      # bid distance, cents
C_bid = sum over bids with 0 <= s_b <= v of ((v - s_b)/v)^2 * size
s_a   = (p - mid) * 100      # ask distance, cents
C_ask = sum over asks with 0 <= s_a <= v of ((v - s_a)/v)^2 * size
C_eff = C_bid + C_ask                     if C_bid >= eps and C_ask >= eps
        max(C_bid, C_ask) / 3.0           otherwise
# eps = the smallest qualifying score, i.e. ((v - v)/v)^2 is 0, so use one
# minimum-size order at the tightest legal distance: ((v - s_min)/v)^2 * min_size
```

This is an approximation of a per-maker quantity from aggregate book data: one maker resting
both sides and two makers resting one side each look the same in the tape. It is the
conservative direction only when depth is balanced; where the two sides differ by more than
2:1 the approximation is exact enough, because the dominant side is what sets the score.

In `decide(ctx)` use `signals["C_eff"]` in place of `ctx.book.competing_score` wherever the
rule prices competition (entry rank, presence, or any share estimate). Layout per
`m-marginal-share-rank` / `m-competition-minute-integral`: same key `(asset_id, ts_second)`,
add `c_bid`, `c_ask`, `c_eff`.

**Already implemented:** the venue's `Q_min` formula for *our own* score in the research
model (`rewards/scoring.py:145-165`), and the per-token band score in the replay. **Not
implemented:** any recognition that the *competitor* score must be haircut the same way, in
the replay or in any rule.

## Why not ruled out

`micro-competition-dilution` uses the whole-band score as a size input and treats it as
unbiased; `micro-maker-vacancy` thresholds the same unbiased number; neither knows that a
one-sided competitor is discounted 3x by the venue. Facts 1-8 say nothing about how the
denominator is composed. The claim is arithmetic on the published formula, not a prior.

## Kill criterion

- Kill if fewer than 20 % of market-minutes have a one-sided band (`C_bid` or `C_ask` below
  the minimum qualifying score): the haircut almost never applies and the correction is noise.
- Kill if the corrected share does not predict realised per-market reward better than the
  uncorrected share (out-of-sample R^2 not higher on a held-out hour).
- Kill if net is unchanged with the corrected statistic in an entry rule that otherwise
  works — i.e. the ranking is dominated by pool size, not by `C`.

## Data needed

`prepare.sql` over `data/tape/clob_events/date=*/hour=*/*.parquet`, replaying `price_change`
deltas onto `book` snapshots (the harness already does this reconstruction with `Ladder`,
`scripts/replay_all_markets.py:213-233`) plus `max_spread` and `min_size` from
`data/tape/universe/*.json`. Columns `(asset_id, ts_second, c_bid, c_ask, c_eff)`. No new
feed. Same table as `micro-competition-dilution` with a side split, so the extraction work
is shared.
