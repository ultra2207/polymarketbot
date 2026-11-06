# Replay factory — first results and what is verified

Status of the automated strategy-search pipeline described in `plan.md`.
Numbers here are from `scripts/replay_all_markets.py` on the live tape; every
claim is reproducible with the command shown.

## What exists

| piece | file | state |
|---|---|---|
| all-markets replay | `scripts/replay_all_markets.py` | runs; synthetic audit passes |
| strategy protocol | `src/polymarketbot/strategy/base.py` | protocol + context + loader |
| engine hook | `PaperTradingEngine(strategy=…)` | module replaces the orchestrator block; sizing, post-only and risk stay in the engine |
| built-in baselines | `strategy/builtin.py` | `reactionary`, `queue_aware_ofi` |
| serial scheduler | `scripts/run_idea_queue.py` | runs `implemented` ideas, paired against a baseline, writes `result.json` |
| idea registry | `ideas/registry.json` | schema + generator protocol |

## Verified by test, not by eye

`tests/test_replay_all_markets.py` builds a two-token tape with a known answer:

- a thin bid level (5 shares) with our quote joining it, one 30-share sell
  print → the engine fills the surplus, the queue is consumed first;
- a degenerate one-sided book at 0.01 → **no quote**, market counted as skipped
  (the reward program scores nothing below 0.02, and quoting there produced
  impossible fills);
- a mid series with a known jump → the markout is exact; a series that ends
  before five minutes → **censored**, not invented;
- five replayed minutes → exactly five reward epochs.

P&L identity checked on the worst real market: `net = cash − initial_cash +
inventory_mtm + rewards` reconciles to the cent, including short inventory
(negative `market_value`).

## First real slice

500 markets (top by liquidity, pool ≥ \$15), 10 minutes, one strategy per pass:

| metric | value |
|---|---|
| book events replayed | 516 |
| fills | 87 across 35 markets |
| rewards | \$32.42 total — \$18.46 with competitor book observed, \$13.97 on top-only books |
| markout, 5 min after fill | **+65.6 bps** |
| net | \$41.61 |
| wall clock | 10.6 s per strategy |

Command:

```
.venv/Scripts/python.exe scripts/replay_all_markets.py \
  --minutes 10 --max-markets 500 --order-by liquidity \
  --strategies reactionary,queue_aware_ofi --tag wave0
```

## First strategic result: rest at the touch or stay inside it?

`join_level` (rest at `best_bid`/`best_ask`) versus `queue_aware_ofi` (quote
inside the spread), same 500 markets, same 10 minutes:

| strategy | fills | rewards | markout 5 min | net |
|---|---|---|---|---|
| `join_level` | 129 | \$18.44 | **−201 bps** | \$15.05 |
| `queue_aware_ofi` | 104 | \$34.48 | **−115 bps** | \$31.97 |

Paired per condition: mean net difference −\$0.032, t = −2.84 over 500
conditions, in favour of quoting inside the spread. Two effects, both predicted
by the mechanism: joining the touch gets filled 24 % more often, and those
fills are 86 bps more adversely selected. Inside the spread also scores more of
the pool, because the reward score increases with distance from the touch
penalty curve.

This is one slice of one regime. It is recorded as the direction the first wave
must beat, not as a promotion.

## What this slice says, and what it does not

**It says** the harness produces a stable, reproducible, paired measurement on
real data, fast enough to run tens of ideas per hour. The 87 fills and 516 book
events are enough to rank module behaviour on this slice.

**It does not say** that any strategy is profitable. Three limits are load-bearing:

1. **The two baselines tie** (identical fills and net). That is correct
   behaviour, not a bug: the engine quotes inside the spread, so `queue_ratio`
   is 0 and `queue_aware_ofi` keeps both sides. The queue gate only has
   something to do when a module *joins* an existing level. This is itself a
   finding: the first wave of ideas must include level-joining quotes.
2. **Reward numbers depend on the seeded competitor ladder.** For 298 of 500
   markets the tape has a full book snapshot before the slice, so the
   competitor score is real; for the rest only the top is known and the reward
   is reported separately (`rewards_unobserved_competition`). Never quote the
   combined number alone.
3. **Ten minutes is one regime.** Promotion needs two non-overlapping slices and
   the gates in `plan.md` §6 — 200 fills, 50 conditions, paired t ≥ 2, and no
   single condition above 25 % of the effect.

## Wave 1 — three ideas, three negative results

The first modules written against the protocol, run against `reactionary` on the
same 500 markets and ten minutes:

| idea | mechanism | net diff per condition | t | verdict |
|---|---|---|---|---|
| `micro-jump-withdrawal` | pull both sides for 5 events after a ≥2-tick move | −\$0.060 | −4.10 | fail |
| `micro-queue-erosion-velocity` | shrink the side whose queue ratio falls ≥0.2 in 10 events | −\$0.095 | −6.02 | fail |
| `micro-thin-level-sweep` | shrink the side whose touch level is in its own bottom quartile | \$0.000 | 0.00 | fail (never fires) |

The two failing ideas fail for the same reason, and it is a real economic
reason: **a pull forfeits reward that outweighs the markout it avoids**, on this
slice. `micro-thin-level-sweep` never fires because every quote rests inside the
spread, so `size_at_level` is zero on both sides and the "thin level" condition
can never hold — the idea is only testable under level-joining quotes.

**This forced a scoring fix before the numbers could be trusted.**
`compute_epoch_payout` summed our two sides additively. The venue normalises:

$$Q_{\text{norm}} = Q_{\text{two}} + \frac{\min(Q_{\text{one}}, Q_{\text{two}})}{3}$$

so a fully one-sided quote earns a third of its raw score. The engine was
overstating every pulled side by 3×, which flatters exactly the defensive ideas
above. `normalise_two_sided` now implements the venue rule (tested in
`tests/test_reward_normalisation.py`); the table above is from the corrected
model.

## The idea inventory

Two generator agents wrote **50 specs** (25 `micro-*`, 25 `cross-*`) with a
dedupe check over mechanism text (no pair above 0.22 token Jaccard). Each spec
has a falsifiable claim, a numeric kill condition, the tape table and columns it
needs, and a `prepare.sql` when the module cannot run from the context alone.
Both generators audited their tables against the tape rather than the docs and
recorded where the two disagree (crypto threshold binaries are sentinels at
pool 0; `kalshi_books` starts 11-05 19:11; `uma_events` has no disputes).

Three of the 25 `micro-` ideas depend on the venue's official `Q_min`/`c=3`
rule that the engine now implements; the reward-model change above means none of
the 50 has been tested against the *old* model.

## Next

- Two generator agents are writing 50 specs (`micro-*`, `cross-*`), each with a
  kill condition, into `ideas/`.
- The scheduler runs `implemented` ideas serially against `queue_aware_ofi` on
  the same slice, writes `ideas/<slug>/result.json`, and flips the registry.
- First wave target: 5 implemented ideas, of which at least two join an existing
  level so the queue gate is actually exercised.
