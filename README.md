# polymarketbot

Research and backtesting system for Polymarket, built on free data only.

The goal is not to copy top traders. It is to simulate fills honestly, reconstruct what
skilled wallets actually did, and find segments that are under-crowded before risking
capital.

- **Build plan:** [`roadmap.md`](roadmap.md) — phases, gates, data constraints
- **Research agenda:** [`plan.md`](plan.md) — thesis, idea backlog, kill criteria
- **What cannot be obtained:** [`docs/limitations.md`](docs/limitations.md)
- **Reverse-engineering method:** [`docs/strategy-reverse-engineering.md`](docs/strategy-reverse-engineering.md)
- **Polymarket mechanics (verified):** [`docs/polymarket-mechanics.md`](docs/polymarket-mechanics.md)
- **Fee model:** [`docs/fees.md`](docs/fees.md)
- **Data inventory:** [`docs/data-inventory.md`](docs/data-inventory.md)
- **Results log (including failures):** [`docs/results-log.md`](docs/results-log.md)
- **Alpha pipeline (test queue + kill conditions):** [`docs/alpha-pipeline.md`](docs/alpha-pipeline.md)
- **PMXT download + reduce runbook:** [`docs/pmxt-runbook.md`](docs/pmxt-runbook.md) — the 1.1 TB archive is an operator task

## Status

| Phase | Gate | State |
|---|---|---|
| 0 — Scaffolding | repo, configs | done |
| 1 — Data acquisition and integrity | counts, payouts, cross-indexer | **passed 2026-11-01** |
| 1b — Missing sources | fees, wallets, reference prices | **partial** — fees and categories done (per-market schedules for 1.94 M markets; 2.7 % `other`); wallet identity, crypto reference prices, UMA/sports metadata pending |
| 2 — Point-in-time universe | no lookahead, explicit rules | **passed 2026-11-01** |
| 3 — Trade normalisation and PnL | labels, unified fills, positions | **in progress** — label backfill running (682 k of ~1.9 M done); `build_trades.py` written, not yet validated |
| 4 — Book replayer | PMXT reduce + replay invariants | reducer works (verified against an independent replay); waits on the 1.06 TB archive download |
| 5+ | — | not started |

## Data we hold

| File | Size | What |
|---|---|---|
| `data/raw/sii/trades.parquet` | 45.8 GB | 1,266,885,846 fills, 2022-11-21 → 2026-10-04 |
| `data/raw/sii/markets.parquet` | 0.5 GB | 4,049,900 markets (latest metadata) |
| `data/raw/dome/…2025-10_2026-03.parquet` | 14.2 GB | 339 M top-10 book snapshots |
| `data/raw/chain/condition.parquet` | 0.1 GB | 1.1 M on-chain conditions + payouts |

Everything else is derived and reproducible from these four files.

## Quick start

```bash
uv run scripts/verify_core.py              # if data exists, re-verify integrity
uv run scripts/build_labels.py             # authoritative payout labels
uv run scripts/build_categories.py         # slug rules + Gamma tag overrides
uv run scripts/build_market_stats.py       # per-market activity from the tape
uv run scripts/build_tick_sizes.py         # tick sizes from Dome
uv run scripts/build_universe.py           # point-in-time market table
uv run pytest -q                           # universe invariants
uv run scripts/check_universe.py --n 24    # invariants + live spot-check
uv run scripts/fetch_wallet_cohort.py      # ranked wallets + PnL decomposition
```

## Things this dataset will teach you the hard way

- **`outcome_prices` is not a payout.** It is a final traded price and differs from the
  on-chain payout. Labels come from `payoutNumerators`.
- **Voids are encoded three ways** (`0.5/0.5`, `1/1`, `0/0`) and pay 0.5 per share. They are
  neither wins nor losses.
- **0.87% of fills are zero-size** dust and must be filtered.
- **V2 is 41% of all fills.** Any analysis ignoring the 2026-04-28 cutover misses half of
  recent activity.
- **44% of markets never trade.** Filtering them is not optional; it is the universe.
- **The lifecycle archive stops 2026-04-28**, before the forward-test window. Post-May
  resolution labels must come from `/v2/tokens` or on-chain logs.

## Conventions

- Python 3.14, managed with `uv`. Run everything as `uv run <script>.py`.
- DuckDB for all local analytics; Parquet everywhere; ZSTD compression.
- `data/raw` is immutable and gitignored. `data/processed` is derived and rebuildable.
- Every gate is re-runnable and leaves an artefact in `data/processed/`.
