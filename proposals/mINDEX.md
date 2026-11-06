# Venue-mechanism and portfolio proposals — ranked by expected value

Twelve proposals that change *when* or *where* we stand, *which markets* we enter, or *what
the reward formula actually pays*, without touching quote width or quote size. Ranked by
expected value: probability the mechanism survives its own kill criterion times the size of
the change it would make to `net`. Files are `proposals/m-<slug>.md`; sibling microstructure
work is `proposals/micro-*.md` and its index is `proposals/INDEX.md`.

| # | File | Mechanism | Answers | Scope | Why it sits here |
|---|---|---|---|---|---|
| 1 | `m-two-rung-convexity.md` | The quadratic distance term is decreasing and convex, so a second rung is dominated; an out-of-band rung scores exactly 0 and still fills. Rule: pull any side whose realised distance exceeds `v`. | Q1 two-level | all | The state is produced by the engine's own inventory skew in the **baseline** (offset reaches 1.25c on a `v=1c` band), and removing a zero-score side changes reward by exactly zero in the replay while removing its fills. Free, certain, immediate. |
| 2 | `m-merge-min-threshold.md` | Merge threshold is 100 shares against a 20-share quote size, so no pair ever merges on a 10-minute slice; merging is worth gas only through restored fill capacity, and capacity is worth more than $0.015. | Q3 merge | all | The merge channel is currently inert, so a whole term of the PnL is switched off; lowering the threshold is a config change and the ledger already counts merges and gas. |
| 3 | `m-one-sided-competitor-haircut.md` | The venue discounts a one-sided competitor to a third (`max(Q1,Q2)/3`); the replay counts them in full, so `competing_score` can overstate the denominator 3x. Rule: re-price competition as `C_two + C_one/3`. | formula | all | A 3x error in the denominator of every share estimate, in one direction, on the published formula. Changes which markets look crowded. |
| 4 | `m-min-order-notional.md` | The venue's 5 USDC minimum order makes a 20-share quote illegal below `mid = 0.25`; the harness's `quote_size` is shares-at-50c, so low-price markets are measured as quotable when they are not. | venue constraint | all (worst at low mid) | A legality screen that also corrects cross-price comparisons of reward per dollar. Cheap: universe plus first book. |
| 5 | `m-competition-minute-integral.md` | The sample is one random instant per minute, so the statistic is `E_t[S/(S+C_t)]` (convex, so above `S/(S+E[C])`); the replay's sampler is phase-locked to `:00`, which makes presence timing an artefact. | Q2 cadence | **high-volume only** | Corrects the variable every other rule reads. The harness half (phase-locked sampling) is a measurement defect worth knowing before any duty-cycling idea is believed. |
| 6 | `m-depth-arrival-exit.md` | A durable doubling of band depth halves our share while leaving our fill rate alone — the one state where leaving is free. Exit at 2x, re-enter at 1.2x, guarded by own PnL. | Q4 rotation | all | Direct answer to the rotation question. Power depends on the persistence of arrivals, which is the thing being measured. |
| 7 | `m-marginal-share-rank.md` | Payout is concave in our own size (`P*C/(S+C)^2` marginal), so ranks by pool size are wrong and the allocator that solves this correctly is built but unwired. | portfolio | all | The concavity is certain; what is uncertain is whether the cross-sectional rank predicts net. Needs a `prepare.sql` and hysteresis. |
| 8 | `m-single-token-deployment.md` | One token with two sides already fills both venue `Q` axes; quoting both halves the reward per unit of capital. The replay pays the market pool once **per token**, so the comparison must be net per committed dollar. | capital | liquid conditions | Potentially the largest capital effect in the set, but the replay's 2x payout artefact makes the naive test unreadable. |
| 9 | `m-rest-clock-reprice-budget.md` | A price change restarts the venue's 3.5 s eligibility clock and the FIFO queue; the replay models the queue reset but not the clock, so a reprice budget's measured gain is a lower bound. | Q2 cadence | all, worst on liquid | Real rule, small measured magnitude. Worth running because it is a wrapper that cannot hurt. |
| 10 | `m-event-basket-legs.md` | K legs of one neg-risk event are one bet; reward is paid per leg, risk is the overround. Gate on `|sum p - 1|` and cap quoted legs by per-leg pool rank. | portfolio | **high-volume only** | Needs a live multi-leg event for power; the risk-side cap it proposes is dormant today because the replay attaches no risk manager. |
| 11 | `m-complement-mid-basis.md` | The venue measures a NO order against `1 - mid_YES`, so a cross-book basis is a scoring subsidy; the replay uses each token's own mid and cannot see it. | formula | fast books only | Real but likely small: needs `|delta|` above a tick to beat rounding. Cheap to falsify with one offline computation. |
| 12 | `m-cross-market-merge-basis.md` | YES of one market plus NO of another is not a complete set at any gas price; the only cross-market netting is neg-risk conversion inside one event, whose value is the overround and whose break-even is a few shares. | Q3 merge | multi-leg events | Mostly a negative result with a quantified replacement. High information, low expected `net`. |

## The four focus questions, answered plainly

1. **Can the distance term be gamed with one qualifying and one non-qualifying level?** No.
   The score is linear in size, decreasing and convex in distance, and zero beyond `v`
   (`rewards/scoring.py:69-79`), so for fixed total size a single rung at the tightest legal
   distance dominates any split, and the far rung earns nothing while still filling. The engine
   cannot express two orders per side anyway (`base.py:37-38`, `engine.py:583-592`). The only
   second level that pays is on the other token at the same `Q` axis — already tested as
   `micro-cross-token-substitution`. See `m-two-rung-convexity.md`.
2. **Does epoch timing matter, given per-minute sampling?** Inside a minute, no: the offset is
   uniform, so presence for 59 of 60 seconds earns 59/60 of that minute in expectation and no
   placement is better than another. Across minutes, yes, in two ways: the operative statistic
   is the minute-average of competition (convexity makes a flickering competitor worth less
   than its snapshot, `m-competition-minute-integral.md`), and repricing inside the 3.5 s
   minimum-rest window makes the minute worth zero on the venue (`m-rest-clock-reprice-budget.md`).
   The replay's sampler is phase-locked to `:00` (`scripts/replay_all_markets.py:471-474`),
   which must be randomised before any duty-cycling result is trusted.
3. **Is merging YES+NO across two markets ever worth the gas?** It is not possible: a complete
   set is one `conditionId` (`engine.py:1064-1081,1175-1180`,
   `rewards/merger.py:167-198`). Inside one market the question is only capacity, and the
   current threshold makes the merge channel inert (`m-merge-min-threshold.md`). The sole
   cross-market primitive is neg-risk conversion inside one event, and it is worth the
   overround minus gas (`m-cross-market-merge-basis.md`).
4. **Should capital rotate out of markets whose competitor depth rose?** In principle yes,
   because the reward share falls and the fill rate does not (`m-depth-arrival-exit.md`); and
   cross-sectionally the marginal-payout rank says the same (`m-marginal-share-rank.md`). In
   this replay the answer is only half-testable: each market has its own engine and its own
   cash (`scripts/replay_all_markets.py:363-368`), so freed capital cannot be redeployed and
   "rotation" reduces to withdrawal.

## Measurement caveats that affect every row above

These are properties of the harness, not of the venue. They are stated here because several
proposals are only readable in their light.

1. **The pool is paid once per token.** `run_epoch_sample` loops over `mkt.tokens` and calls
   `compute_epoch_payout(..., mkt.rate_per_day)` inside the loop, adding each token's payout
   (`engine.py:1240-1262`). A both-token strategy books up to 2x the market's daily pool.
2. **Reward is additive across sides, not the venue's `Q_min`.** The replay pays `bid + ask`
   whenever both rest (`paper_trading/rewards.py:132-138`); the venue pays
   `max(min(Q1,Q2), max(Q1,Q2)/3)` (`rewards/scoring.py:145-165`). Any laddering or one-sided
   result inherits this gap.
3. **The 3.5 s minimum rest is not scored.** `engine.py:710-711` calls the duration-free
   `compute_quadratic_score` (`paper_trading/rewards.py:18-35`).
4. **No cash or margin constraint.** The replay passes `risk_manager=None`
   (`scripts/replay_all_markets.py:363-368`), `_execute_buy_fill` deducts cash unconditionally
   (`engine.py:889`), and no code path rejects a quote for want of cash.
5. **Naked shorts are allowed.** `_execute_sell_fill` opens a short from a flat position
   (`engine.py:992-1000`); on the venue the ask side must be funded by inventory or a split.
6. **Quote size is price-independent at entry.** `capital_for` is called with `mid0 = 0.5` for
   every market (`scripts/replay_all_markets.py:295-299`, `:362-363`), so shares never scale
   with the real mid and cross-price reward-per-dollar comparisons are flat by construction.
7. **The reward sampler is phase-locked** to `floor(start) + 60k`
   (`scripts/replay_all_markets.py:471-474`), not a uniform random offset.
8. **The merge channel is inert** at the default 100-share threshold (`engine.py:80`) against a
   20-share quote size, so past results say nothing about merging, gas or capital recycling.

## Already implemented — do not re-propose

Per-market capital from pool size (`capital_for`), the MILP allocator and its `$1` floor
(`allocator.py:148`, `:130-147`), the endogenous reserve (`allocator.py:515-595`), the
re-alloc governor (`allocator.py:608-660`), the switching-cost hurdle (`switching.py:83-152`),
same-condition merging with gas accounting (`engine.py:1137-1232`), the `$1` daily floor in
the research scorer (`rewards/scoring.py:180-211`), the queue-position gate and its
orchestrator, the `rewards_min_size` floor on any positive size (`engine.py:640-644`), and the
synthetic cross-hedger (`engine.py:1083`). Only the last two are reachable in the replay as
configured, and the hedger and allocator are not wired to it at all.
