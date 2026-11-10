# Archived: CTF complete-set parity (`CtfMinter`)

This package is the `CtfMinter` paper pillar and its supporting trading code,
moved out of the live tree on 2026-11-10 and **kept, not deleted**. It was
archived because measured data refuted it, not because it was untidy: if the
market structure changes (a genuine second book, a fee regime that pays
minting, a non-mirrored complement), the measurement can be repeated against
this code instead of rewritten.

The live paper engine now runs two pillars (`RewardHarvester`,
`SettlementHarvester`). Nothing under `archive/` is imported by
`polymarketbot`, and `pytest tests/` does not collect `archive/ctf/tests/`.

## Why it was archived: the measured evidence

Source of record: `docs/profitability-report.md` §3, and
`data/derived/ctf_census/census.json` (full Gamma universe, two snapshots six
minutes apart, 2026-11-10T10:42Z).

### Mechanism (a): complete-set minting / taker parity — dead

| `census.json` key | Value |
|---|---|
| `priced_markets` | 76,097 |
| `markets_at_or_below_par` | **0** |
| `gross_positive` | **0** |
| `sweep_positive` | **0** |
| best `net_per_set` | **−0.001000** |

Structural cause, verified level-by-level (115 levels / 25 markets, max
deviation **0.0**): the YES and NO books are one book mirrored,
`ask_NO = 1 − bid_YES`, so

$$\text{ask}_Y+\text{ask}_N = 1+(\text{ask}_Y-\text{bid}_Y)\ \ge 1 .$$

Buying both asks *is* buying at the ask and selling at the bid — it pays the
spread. There is no second book to arbitrage.

### Mechanism (b): maker parity bundles — dead, different reason

Top-of-book `bid_YES + bid_NO` is < 1 for most two-sided conditions, but that
is the mirrored spread again: `bid_sum = 1 − (ask_sum − 1)`, and the gate
improves each bid one tick, so the "lock" is `(ask_sum − 1) − 2·tick`.

Gate replay over **2,172 real two-sided books**:

| Outcome | Count |
|---|---|
| `pair_not_under_par` (tick cost consumes the lock) | 547 |
| `depth_below_min_order` | 11 |
| `single_leg_below_measured_cost` | **1,219** |
| EV > 0 | 395 (18.2 %) |
| **accepted (shadow-yield pass)** | **0** |

Best EV/set among the 395: p50 2.46e-5, max **8.50e-5**. Annualised: p50
0.0056 $/$/day, **max 0.0589 $/$/day** against the **0.0924 $/$/day** hurdle
(the reward book's measured KKT shadow yield, `_tail_policy`/`_shadow_yield`).
The gate therefore accepted **0 quotes out of 2,172 books**.

Fill risk makes it worse: `P(exactly one leg) = 2p(1−p)` = **0.0210** vs
`P(both) = p²` = 1.13e-4, so the naked-leg branch is **186× more likely than
the lock**; gas only falls below 1 % of edge at **≥226 shares** against a median
best-bid depth of **40 shares**.

The historical `soak_trades` rows showing +$1,606.33 (83 merges, 95 broken
legs, 2026-11-09) predate the current gate: they are the phantom lock that
`single_leg_below_measured_cost` exists to block, not evidence.

## What is here

| Path | Was |
|---|---|
| `pillars/` | `src/polymarketbot/paper/pillars/ctf/` (fill_model, markout, parity, taker, maker, onchain) |
| `runner.py` | `src/polymarketbot/paper/pillars/ctf_runner.py` |
| `landing.py` | the CTF branches of `src/polymarketbot/paper/actions.py` (`land_ctf_taker` + the `ctf_*` action kinds) |
| `allocator.py` | `src/polymarketbot/trading/ctf_allocator.py` (`MarginalCtfAllocator`) |
| `split_arbitrage.py` | `src/polymarketbot/trading/ctf_split_arbitrage.py` (fee/gas primitives) |
| `complement_quoter.py` | `src/polymarketbot/trading/complement_quoter.py` |
| `tests/` | `tests/test_ctf_split_arbitrage.py`, `tests/test_ctf_marginal_allocator.py`, `tests/test_complement_quoter.py`, the CTF sections of `tests/test_paper_pillars.py`, and the three CTF engine tests from `tests/test_paper_engine.py` |

Everything was moved with `git mv`; `git log --follow` still reaches the
original history. Test bodies and assertions are unchanged — only import paths
and, for the engine tests, a smaller engine stub.

## What deliberately stayed live

* `src/polymarketbot/strategy/onchain/ctf.py` — the on-chain ERC-1155 primitives
  (split/merge/redeem calldata, EIP-1559 signing, gas, RLP). **Live settlement
  redemption needs it**; `tests/test_chain_settlement.py`,
  `test_settlement_routing.py`, `test_tx_lifecycle.py` exercise it.
* `src/polymarketbot/strategy/ledger.py` — including the `CtfMinter` bucket and
  `merge()`, so an old journal still resumes and reports its equity.
* `data/derived/ctf_*.json` and `data/derived/ctf_census/` — the measured
  artifacts. They were **not copied**: both the archive and the live loaders
  read them in place.
* `src/polymarketbot/strategy/reward/measurement.py` — the live loader for
  `ctf_quote_fill_horizon.json` and `ctf_maker_markout.json`. The reward
  pillar and the opportunity scanner price with these tables, so the loaders
  stayed in the live tree; the archive has its own copy in `pillars/`.
* `src/polymarketbot/tools/ctf_quote_fill_horizon.py` — the builder of both
  artifacts. It is a live dependency of reward pricing, not dead code.

## Copied helpers (why this package is self-contained)

Archive code may import `polymarketbot.fees`, `polymarketbot.fill_model`,
`polymarketbot.logs`, `polymarketbot.market` (plus `strategy.onchain.core` for the measured
on-chain clocks). It must not import `polymarketbot.paper` or
`polymarketbot.live`, so these small, pure definitions were copied:

| File | Copied from | Why |
|---|---|---|
| `books.py` | `polymarketbot.strategy.books` | `best_bid`/`best_ask`/`shares_at`/`ladder` — pure dict math |
| `harness.py` | `strategy.venue.reads` (`Market`, `Print`), `paper.actions` (`Resting`, `pending_for`), `strategy.reward.decision.measured_fill_shares` + `paper.pillars.reward_runner.apply_resting_fills` | the quote record, the market/print views and the fill-accrual rule the runner needs. The paper pillar's rolling log tally was dropped (no reader here); the rebate now calls `fees.maker_fee_rebate`, which is the function the paper path intended. |
| `measurements.py` | `strategy.onchain.ctf` (`load_persisted_rpc_rtt_s`, `load_measured_block_interval_s`, `get_dynamic_gas_price_gwei`, `get_dynamic_pol_usd_price`) | measured on-chain inputs; `strategy/onchain/ctf.py` stays live, but the archive may not import it |
| `strategy_types.py` | `polymarketbot.strategy.base` | the five context dataclasses + `StrategyModule` protocol the quoter implements; the strategy harness was deleted in the same wave |
| `merger.py` | `polymarketbot.rewards.merger` | `CompleteSetPosition`/`MergeEvent` bookkeeping and the `mergePositions` calldata builder the quoter auto-merges with; the live module was deleted once this quoter was its last consumer |

One archived **test** (`tests/test_complement_quoter.py::test_polygon_ctf_client_dry_run_integration`)
imports `polymarketbot.strategy.onchain.ctf.PolygonCtfClient`, which is live on purpose:
it checks the quoter against the real dry-run client contract. It is a test
cross-check, not a package dependency.

## How to re-run

Run the archived tests by explicit path from the repository root:

```bash
uv run python -m pytest -q archive/ctf/tests
```

The complete-set census (`scripts/ctf_complete_set_census.py`) was deleted with
the one-off scripts in a later cleanup wave. The measured artifacts stay in
`data/derived/ctf_census/`, and the script is recoverable from the last commit
that carried it (pre-cleanup, `76886b5`) before re-running:

```bash
git show 76886b5:scripts/ctf_complete_set_census.py > scripts/ctf_complete_set_census.py
uv run python scripts/ctf_complete_set_census.py walk      # page Gamma, keep tradable binary non-neg-risk conditions
uv run python scripts/ctf_complete_set_census.py census    # profit/depth distributions -> census.json
uv run python scripts/ctf_complete_set_census.py probe     # high-cadence persistence poke -> probe.json
uv run python scripts/ctf_complete_set_census.py evaluate  # lifetime / capturable-fraction tables -> episodes.json
```

Outputs (all under `data/derived/ctf_census/`): `universe.jsonl`,
`walk_summary.json`, `census.json`, `census_records.jsonl`,
`census_snapshot1.json`, `records_snapshot1.jsonl`, `probe.json`,
`episodes.json`. The headline keys to check are `priced_markets`,
`markets_at_or_below_par`, `gross_positive` and `sweep_positive`.

Rebuild the measured fill/markout tables (also used by live reward pricing):

```bash
uv run python -m polymarketbot.tools.ctf_quote_fill_horizon
uv run python -m polymarketbot.tools.ctf_quote_fill_horizon --markout
```

Replay the maker gate on new books with the archived pillar, e.g.

```python
from archive.ctf import pillars as ctf
quote = ctf.optimize_maker_parity(
    market, book_yes, book_no, cash_usd=100.0,
    requote_horizon_s=5.0,
    min_daily_yield=<measured shadow yield>,
)
```

`None` means the gate refused: the quote did not clear the measured fill,
markout, gas, rebate and cost-of-capital inputs. The family of refusals is
`_QUOTE_AUDIT` (logged once a minute by `log_quote_audit`).

### Re-wiring the engine loop (only if you are resurrecting the strategy)

`runner.py` and `landing.py` are the two halves of the old pillar. To re-run
them, wire `run_ctf(engine, books, prints)` into a tick and `land_ctf(engine,
books)` into the landing pass, and give the engine the attributes the runner
reads (`_token_index`, `by_condition`, `ledger`, `actions`, `maker_quotes`,
`_fills`, `gas_usd`, `_ctf_budget`, `_ctf_cursor`, `_ctf_coverage_ts`,
`_last_tick_dt`, `_shadow_yield`, `_reserved`, `_rtt`, `feed`). The runner
submits `ctf_taker` / `ctf_quote` / `ctf_cancel` / `ctf_merge`; `landing.py`
handles all four. `archive/ctf/tests/test_ctf_runner.py` is the working
example of that wiring.
