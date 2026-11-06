# One token already satisfies the venue's two-sidedness; quoting both doubles capital for one pool

`slug:` m-single-token-deployment
`focus:` capital allocation / reward-formula coupling across the complement
`market scope:` largest effect on liquid conditions where both books are dense enough to be
compared; on thin conditions the two tokens are so similar that the choice is arbitrary.

## Mechanism

The venue scores a *condition*, not a token. Its two aggregates are

```
Q_one = sum of YES bids  + sum of NO asks
Q_two = sum of YES asks  + sum of NO bids
```

(`src/polymarketbot/rewards/scoring.py:82-143`, with the YES/NO mapping implemented at
`scoring.py:112-138`). A maker who rests both sides of **one** token already puts a non-zero
term into both `Q_one` and `Q_two`, so two-sidedness is satisfied by a single token. Quoting
both tokens adds shares — legitimately more score — but it also doubles the inventory, the
capital and the two-sided adverse selection for the *same* daily pool.

The two tokens are not equally good places to stand. Their books are independent snapshots
of the same fair value, so one band is often crowded while the other is empty; the realised
distance also differs by the tick rounding of each mid. The module already sees both books,
because `replay_all_markets.py` binds one strategy instance per condition
(`scripts/replay_all_markets.py:387-401`) and the engine calls `decide` once per asset
(`src/polymarketbot/paper_trading/engine.py:571-576`; documented in
`micro-cross-token-substitution`).

**The replay must not be trusted for this comparison as written.** `run_epoch_sample`
iterates over `mkt.tokens` and calls `compute_epoch_payout(v_score, comp_score,
mkt.rate_per_day)` inside the token loop (`src/polymarketbot/paper_trading/engine.py:1240-1262`),
adding each token's payout to `total_rewards_earned_usd`. The market's daily pool is therefore
paid twice for a both-token strategy. That is a 2x upward bias on exactly the arm this idea
would abandon, so the test has to be run on **net per unit of committed notional**, not on
net.

## Rule

In `setup`, store both token ids. After 60 s of book, compute for each token

```
density_i = ((v - s_eff_i) / v)^2                 # v = MarketView.rewards_max_spread (base.py:60)
score_i   = density_i * ctx.quote_size             # base.py:110
share_i   = score_i / (score_i + C_i)              # C_i = ctx.book.competing_score (base.py:79)
value_i   = share_i * ctx.rate_per_day             # base.py:113
```

Quote both sides of `argmax(value_i)` and `PULL` both sides of the other token. Re-evaluate
at most once per 30 minutes of `ctx.seconds_elapsed` (`base.py:98`) and only if the loser's
`value` is within 20 % of the winner's — otherwise latch and stop paying attention.

Note the two caveats that apply to the winner's ask side: the venue requires inventory to
sell, while the replay permits naked shorts (`engine.py:997-1000` treats a sell from a flat
position as opening a short). On the real venue the ask side is funded by splitting
collateral (1 pUSD -> 1 YES + 1 NO, `docs/polymarket-mechanics.md` §7), which is a capital
cost the replay does not charge.

**Already implemented:** per-condition strategy binding, both books visible in one instance,
per-token quoting, per-token score and payout. **Not implemented:** any token-selection rule,
and any capital normalisation in the reported `net` (`scripts/replay_all_markets.py:556`).

## Why not ruled out

No registry idea asks *whether to quote both tokens*: `micro-cross-token-substitution` and
`micro-net-complement-inventory` both assume both tokens are quoted and then move size
between them (`micro-cross-token-substitution` explicitly uses the "one module instance per
condition" fact to keep `Q_min` alive when one side is pulled). Facts 1-8 are per-market and
say nothing about the complement's capital cost. The 2x payout in `run_epoch_sample` is a
replay artefact, not a venue fact, and is exactly what makes the question worth asking
properly.

## Kill criterion

- Kill if net **per committed dollar** (fills frame net divided by
  `2 * quote_size * mid` for the both-token arm and `quote_size * mid` for this arm) is not
  better for the single-token arm on both slices.
- Kill if `|share_YES - share_NO| / max(share)` is below 0.1 for more than 70 % of conditions:
  there is no asymmetry to select on and the rule is a coin flip.
- Kill if the abandoned token's reward per dollar exceeds the kept token's by more than the
  capital saved — i.e. the venue's cross-token `Q` coupling (which the replay does not model)
  dominates the capital saving in practice.

## Data needed

`DecisionContext` for both assets of the condition plus `MarketView.tokens` — no prepared
signal, no new feed. The capital-normalised comparison needs the fills frame the harness
already writes (`scripts/replay_all_markets.py:544-570`, assembled at `:625`), not new data.
