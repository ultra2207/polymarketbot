# A durable rise in competitor band depth is a tax with no offsetting fill saving

`slug:` m-depth-arrival-exit
`focus:` portfolio — should capital rotate out of markets whose observed competitor depth rose?
`market scope:` all markets; strongest on liquid ones where `C` moves in steps (a new maker
arrives and stays) rather than in noise.

## Mechanism

Our per-minute reward is `P/1440 * S/(S + C)` where `C` is the band score of everyone
else. `C` is a *stock*, set by whoever else is resting. When a new maker arrives and
stays, our share is cut by `S/(S+C_new)` for as long as that maker rests — the loss is a
step change in the reward channel, not a transient.

Fills do not move with `C`. The replay's fill model is FIFO against the tape's prints at
our own level (`src/polymarketbot/paper_trading/engine.py:780-875`), driven by
`bid_size_at_level` and queue position, not by the band score. A new maker resting at the
band edge leaves our queue position and our fill rate essentially unchanged while halving
our share. That asymmetry — reward falls, markout does not — is the one state in which
leaving costs nothing but the reward we were no longer going to earn.

The mirror case matters for the guard: a rise in `C` that is *our own* score (we just
moved a quote into the band) must not trigger an exit, and a rise in `C` in a market that
is still paying us must not either.

Entry is free and reversible; staying is not. So the policy needs hysteresis: exit at a
large multiple, re-enter at a small one.

## Rule

`setup`/first 60 s: record `C0` = median of `ctx.book.competing_score` (`base.py:79`)
over that window. Thereafter maintain a time-weighted median over a rolling 15 min using
`ctx.seconds_elapsed` (`base.py:98`), plus the market's own score
`S = ctx.bid_score + ctx.ask_score` (`base.py:111-112`).

```
if C_med >= 2.0 * C0  and  frac_of_window(S > 0) > 0.9   # the rise is not ours
   and ctx.realized_pnl <= 0                              # base.py:108
   and ctx.seconds_elapsed - entry_t >= 900:
        PULL both sides, latch off
if latched and C_med <= 1.2 * C0:
        unlatch, QUOTE both
```

The `S > 0` condition keeps this from firing on our own quote entering the band: the
engine reports our side scores (`base.py:111-112`), so the module subtracts its own
contribution instead of trusting `C` alone. The `realized_pnl` guard keeps it distinct
from `micro-adaptive-exit`, which exits on *negative own PnL* and therefore never fires on
a market that is paying.

**Already implemented:** nothing. `C` is computed and exposed but never acted on as a
regime variable; there is no exit/entry hysteresis anywhere in the strategy layer.
`SwitchingConfig` / `calculate_switching_cost` (`src/polymarketbot/rewards/switching.py:39,
83-152`) implement a *cost hurdle* for moving between markets, but the replay never calls
them (`scripts/replay_all_markets.py` imports neither `rewards.allocator` nor
`rewards.switching`), and they price a move, not a durable-depth test.

**Portfolio caveat, stated plainly:** the replay gives every market its own engine and its
own cash (`scripts/replay_all_markets.py:363-368`), so freed capital cannot be redeployed
in-engine. The measurable half is the *withdrawal*: net in a pulled market goes to 0 and
the sum over the universe improves iff that market's baseline net was negative. The
rotation half needs a portfolio harness and is not claimed here.

## Why not ruled out

Fact 2 says more size *within* a market is right; it says nothing about which markets to be
in. `micro-competition-dilution` shrinks size when `C` is large — a static, within-market
size rule with no exit and no hysteresis, and fact 2 constrains its lever. `micro-payout-
floor` is a static `$1` screen, not a depth-change detector. Facts 3-8 do not touch `C`
dynamics; nothing in the registry measures the *persistence* of a competitor arrival.

## Kill criterion

- Kill if `log C` has 15-minute autocorrelation below 0.3 on the slice: arrivals mean-revert,
  the exit fires at the top, and the pull forfeits reward for nothing.
- Kill if the pulled cohort's baseline net is already `>= 0` per share (there is nothing to
  save; the depth rise was harmless).
- Kill if fewer than 30 markets in the slice ever double `C0` (no power at 10 min; rerun at
  `--minutes 60`).

## Data needed

`DecisionContext` only: `book.competing_score`, `bid_score`, `ask_score`,
`seconds_elapsed`, `realized_pnl`. No prepared table, no new feed. A per-(asset, minute)
`C` series from `clob_events` is optional and uses the `Ladder` reconstruction already in
the harness (`scripts/replay_all_markets.py:213-233`).
