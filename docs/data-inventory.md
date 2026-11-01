# Data inventory

What we hold, what we derive, and how to rebuild it. Sizes and row counts are from
2026-11-01; derived files are reproducible from the raw set.

## Raw inputs (`data/raw/`, gitignored)

| File | Size | Coverage | Notes |
|---|---|---|---|
| `sii/trades.parquet` | 45.85 GB | 2022-11-21 → 2026-10-04 | 1,266,885,846 fills; the backbone |
| `sii/markets.parquet` | 0.55 GB | 4,049,900 markets | **latest** metadata only; no tags |
| `dome/dome_…2025-10_2026-03.parquet` | 14.24 GB | 2025-10-14 → 2026-03-31 | 339 M top-10 book snapshots |
| `chain/condition.parquet` | 0.13 GB | 1,117,152 conditions | on-chain payouts; stops 2026-04 |
| `gamma/snapshot_YYYY-MM-DD.parquet` | ~0.02 GB/day | open markets | daily metadata snapshot |
| `docs/` | 3.8 MB | `llms-full.txt` + 96 pages | mirrored Polymarket docs |

## Derived (`data/processed/`)

| File | Rows | Built by | What |
|---|---|---|---|
| `labels_chain.parquet` | 1,117,152 | `build_labels.py` | normalised on-chain payout vectors |
| `market_labels.parquet` | 4,049,900 | `build_labels.py` | markets + authoritative labels |
| `market_categories.parquet` | 4,049,900 | `build_categories.py` | slug rules + Gamma tag overrides |
| `gamma_category_overrides.parquet` | 106,566 | `enrich_categories_gamma.py` | tags→category for the residual |
| `market_trade_stats.parquet` | 2,435,871 | `build_market_stats.py` | first/last fill, fills, volume, v1/v2 |
| `market_tick.parquet` | 163,908 | `build_tick_sizes.py` | tick from Dome (2025-10→2026-03) |
| `market_metadata_gamma.parquet` | ~2.4 M (filling) | `enrich_market_metadata.py` | version, token/position ids, feeSchedule, tick, min order |
| `markets_pit.parquet` | 4,049,855 | `build_universe.py` | **the point-in-time market table** |
| `labels_api.parquet` | ~1.5 M (filling) | `fetch_labels_api.py` | 2026-04+ settlements from `/v2/tokens`; `final_price` validated 371/371 against on-chain payouts |
| `screen_calibration_rows.parquet` | 152,895 | `screen_calibration.py` | scored token-entries behind the idea-#1 result |
| `screen_calibration_parts/entry=…/quarter=…` | per quarter | `screen_calibration.py` | resumable screen output, one file per quarter |
| `pmxt_reduced/dt=…/hour=…` | per hour | `reduce_pmxt.py` | reconstructed book per (token, bucket); exists only once the raw archive is downloaded |
| `trades/year=…/month=…` | per month | `build_trades.py` | canonical fills: no dust, no self-trades, no audit hashes |
| `wallet_cohort.parquet` | sample | `fetch_wallet_cohort.py` | ranked wallets + PnL decomposition |
| `resolution_timing.parquet` | sample | `fetch_resolution_timing.py` | `resolved_at`, disputes, lag |
| `indexer_crosscheck.*` | 200 | `cross_check_indexers.py` | SII vs moose-code agreement |
| `resolution_reconciliation.*` | 450 | `reconcile_resolutions.py` | payouts vs live API |
| `universe_spotcheck.json` | 24 | `check_universe.py` | live Gamma spot-check |

## Scripts

| Script | Purpose | Bounded by |
|---|---|---|
| `download.py` | resumable core download (56.5 GB) | ETag-pinned, resumable |
| `verify_core.py` | Phase 1 integrity gate | — |
| `build_labels.py` | on-chain payouts → labels | — |
| `build_categories.py` | rule-based categories | — |
| `enrich_categories_gamma.py` | tag-based reclassification | `--max-seconds` |
| `build_market_stats.py` | 1.27 B-row aggregation | ~2 min |
| `build_tick_sizes.py` | Dome tick sizes | ~2 s |
| `enrich_market_metadata.py` | Gamma metadata, concurrent | `--max-seconds`, `--workers` |
| `build_universe.py` | assemble `markets_pit` | ~30 s |
| `check_universe.py` | invariants + live spot-check | 24 URLs |
| `cross_check_indexers.py` | SII vs moose per-tx | per-query timeout |
| `reconcile_resolutions.py` | labels vs `/v2/tokens` | `--n` |
| `fetch_wallet_cohort.py` | ranked wallets + PnL split | `--max-wallets`, `--max-seconds` |
| `fetch_resolution_timing.py` | resolution lag / disputes | `--n`, `--max-seconds` |
| `snapshot_gamma.py` | daily open-market snapshot | `--max-pages` |
| `download_docs.py` | mirror the docs site | `--max-seconds` |
| `profile_job.py` | cProfile / EXPLAIN ANALYZE | — |
| `fetch_labels_api.py` | 2026-04+ labels from `/v2/tokens` | `--max-seconds`, journal-resumable |
| `build_trades.py` | canonical monthly trades | one month per invocation |
| `screen_calibration.py` | idea #1 screen | one parquet per quarter, resumable |
| `reduce_pmxt.py` | PMXT hour → book buckets | `--dir` over a local archive |

## Rebuild order

```bash
uv run scripts/build_labels.py
uv run scripts/build_categories.py
uv run scripts/build_market_stats.py
uv run scripts/build_tick_sizes.py
uv run scripts/enrich_market_metadata.py     # optional, slow, improves coverage
uv run scripts/build_universe.py             # depends on the five above
uv run pytest -q
uv run scripts/check_universe.py --n 24
```
