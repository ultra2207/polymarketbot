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

## Next

- Two generator agents are writing 50 specs (`micro-*`, `cross-*`), each with a
  kill condition, into `ideas/`.
- The scheduler runs `implemented` ideas serially against `queue_aware_ofi` on
  the same slice, writes `ideas/<slug>/result.json`, and flips the registry.
- First wave target: 5 implemented ideas, of which at least two join an existing
  level so the queue gate is actually exercised.
