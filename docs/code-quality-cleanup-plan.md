# Code-quality cleanup plan

Evidence date: 2026-11-10. Produced from four read-only sweeps (duplication, hot
paths, `scripts/` triage, test topology) plus measured line counts. Every claim
carries a `path:line` anchor; anything unanchored is labelled a judgement.

## 0. What changed already (this pass)

The 9,300-line soak monolith is gone. The live system is now:

| Item | Before | After |
|---|---|---|
| Soak entry | `scripts/run_live_paper_soak.py`, 9,300 lines | `src/polymarketbot/paper/main.py`, **98 lines** |
| Engine | one class, ~7,000 lines | `paper/engine.py` (~500), one clock, three pillars |
| Decisions | inline in the daemon | `paper/pillars/{reward,settlement,ctf}.py`, pure functions |
| Accounting | ad-hoc counters + SQLite | `paper/ledger.py`, cash + lots + mark-to-par, resumable |
| Latency | ad-hoc timestamps | `paper/latency.py`, actions land only after measured RTT |
| Venue reads | duplicated HTTP code | `paper/venue.py`, Gamma keyset walker, `/books`, tape |
| UMA | inside the daemon | `paper/uma_poll.py` (12 tests) |
| Tests | 1 monolith test file | 4 focused files: core, engine, pillars, UMA |
| Suite | — | **1,956 passed, 1 skipped, 173 s** |
| Deploy unit | launched the deleted script (P0, broken) | `python -m polymarketbot.paper.main` |

Measured shape today: `src/` 145 files / 73,640 lines, `tests/` 128 / 42,491,
`scripts/` **149 / 41,643**. The soak was ~1.5 % of `src/`; the real problem is
the rest of the tree, which contains three parallel paper engines and 120
disposable scripts.

## 0b. Cleanup execution — wave 1 + 2 (2026-11-10)

Greenlit A, B, D, E executed. C (oracle) swept and deleted: all 8 modules had no
production consumer; `GroundTruthOutcome`/`OracleCategory`/`OracleStatus` moved
into `trading.domain2.types`.

| Metric | Before | After |
|---|---:|---:|
| `src/` | 73,640 | **75,700** (41.6k lines of runnable code moved in) |
| `tests/` | 42,491 | **33,940** |
| `scripts/` | 41,643 across 149 files | **0 — folder removed** |
| Total Python | 157,772 | **109,640** |
| Full suite | 1,956 passed | **1,602 passed, 1 skipped, 177 s** |

Landed:

- **A** — 120 appendix scripts + 10 tests deleted; data-build scripts restored (see
  appendix correction) and reclassified as class d.
- **B** — `paper_trading/` (12 files), `rewards/engine.py`, `rewards/queue.py`,
  `rewards/inventory.py`, `trading/virtual_queue_book.py` deleted; `ui/app.py`
  rewritten as a read-only `paper_engine.db` dashboard; `QuoteOrchestrator`
  moved to `strategy/orchestrator.py`; `MarketConfig`/`TokenConfig` live in
  `polymarketbot.market`.
- **C** — `oracle/` deleted (5,185 src lines, 2,331 test lines).
- **D** — promoted: collector + watchdog + monitor + clock probe
  (`data.collector`, `ops.*`), real-money runner + gating (`trading.runner`),
  latency probe, fill calibration, preflight, tape validation, yield report,
  gamma snapshot, Kalshi backfill, UMA replay, universe-metadata refresh,
  Kalshi mapping v2 pipeline. Deploy unit and runbooks now use `python -m`.
- **E** — `trigger_ctf_merge` no longer holds the engine lock across SQLite/RPC;
  `flush_ledger` computes locked cash once; `MarketEconomics` replaces the eight
  hardcoded knobs (loaded from `data/derived/market_economics.json` when present);
  `DEFAULT_FALLBACK_BID/ASK` removed at all call sites; `place_order` split into
  prepare/sign/dispatch with `GET /version` outside the lock; simulator marks are
  no longer fabricated (`MissingBookPriceError`, unmarked positions excluded and
  labelled); scanner reuses sessions/executor with bounded LRU caches and locked
  telemetry.

Wave 3/4 (same day, after greenlight feedback):

- **`scripts/` is empty.** All 13 data builders, both enrichment builders, both
  verification gates, the scorecard, Kalshi probe, fee validator, tape-cost
  report, labels backfill, L3 corpus builder, live-rewards snapshot, liquid-queue
  monitor, collector stack and live runner now run as
  `python -m polymarketbot.<area>.<module>`; README, runbooks, `configs/fees.yaml`
  and module docstrings updated. True one-offs (`profile_job`,
  `build_idea_signals`) deleted.
- **Mid-run opportunity switching is wired** into `trading.runner`: every 60 s it
  re-scans the rewards universe, compares the measured gain against the module's
  switching cost (merge gas + crossing friction over its 4 h horizon), and
  migrates only when the gain exceeds cost; `--condition-id` stays pinned.
  Tests: better-market switch, sub-cost hold, empty universe hold (29 tests).
- **Replay rebuild declined deliberately** (not a silent loss): the live paper
  soak already scans the full universe every tick and journals every trade; a
  historical replay harness is rebuilt only when a concrete question needs it.

Known capability gaps (deliberate, must be rebuilt rather than silently lost):

1. The full-universe replay harness (`replay_all_markets.py`) and the idea-queue
   scheduler depended on the deleted `paper_trading` strategy/epoch hooks; both
   were removed. Rebuild on `paper` + `simulator` if research needs them.
2. The real-money runner auto-selects one rewards market per run and does not
   re-scan/switch mid-run; `rewards/switching.py` still encodes the hurdle and
   has no production consumer. Wire it into `trading/runner.py` or delete it.
3. 12 `build_*` / `download*` / `fetch_wallet_cohort` scripts remain in
   `scripts/` until their pipeline is promoted; `monitor_liquid_queues.py` is the
   last production daemon still outside `src/`.
4. Doc strings in `docs/live-trading-*.md`, `docs/data-collection.md` history
   sections and `roadmap.md` still name retired scripts; historical audit docs
   keep their original anchors on purpose.

## 1. Findings that drive the plan

### 1.1 Three parallel paper-trading engines (P0)

| Engine | Size | Status |
|---|---|---|
| `paper/engine.py:41` `PaperEngine` | ~500 | **live entry** |
| `simulator/engine.py:161` `NativePaperTradingEngine` | 2,073 | scripts + tests only |
| `paper_trading/engine.py:78` `PaperTradingEngine` | 2,071 | scripts + `ui/app.py` only |
| `rewards/engine.py:132` `LiquidityRewardsBotEngine` | 497 | backtest script + tests only |

They duplicate `evaluate_quotes`, `_execute_tier1/2`, `process_reward_epoch`,
`compute_switching_cost`, `flush_ledger` almost line for line
(`simulator/engine.py:1730-1873` vs `trading/base.py:1313-1445` vs
`paper_trading/engine.py:952-1143`). `paper/` imports none of them.

### 1.2 One concept, many definitions

| Concept | Copies | Evidence |
|---|---|---|
| Reward scoring | **4** | `rewards/scoring.py:44`, `paper_trading/rewards.py:18`, `rewards/queue.py`, `paper/books.py:72` |
| CTF address/selector/calldata | **3** | `trading/ctf.py:58-65,718`, `trading/settlement.py:32-34,62-64,308`, `rewards/merger.py:22-23,167` |
| Merge-gas entry points | **7** | `trading/ctf.py:535`, `ctf_split_arbitrage.py:101`, `rewards/queue_valuation.py:172`, `ctf_allocator.py:126`, `paper_trading/engine.py:274`, `negrisk_engine.py:…` |
| Oracle verdict vocabulary | **3** | `trading/domain2/settlement_fair_value.py:87` (live), `oracle/base.py:107,121`, `trading/domain2/types.py:27,57` |
| `OrderBookLevel` | **5** | `simulator/feed.py:87`, `trading/ctf_allocator.py:101`, `negrisk_engine.py:99`, `binance_quant_engine.py:68`, `virtual_queue_book.py:90` |
| Fill-probability model | **3** | `paper_trading/engine.py:247`, `paper_trading/orchestrator.py:74`, `paper/pillars/reward.py:174` (live) |
| Complete-set inventory | **3** | `rewards/merger.py:40`, `simulator/portfolio.py:109`, `paper/pillars/ctf.py:345` |

### 1.3 Hot-path defects (P1)

1. `trading/engine.py:1869-2104` — `trigger_ctf_merge` holds `self._lock` across
   SQLite reads, two chain `balanceOf` RPCs, route verification, a broadcast and
   receipt parsing. The quote cycle (`:2142-2226`) stalls for the whole tx.
2. `simulator/engine.py:247-248,696-697,1171-1172` and
   `trading/base.py:70-71,686-687` price a missing book at `0.50` / `0.49–0.51`.
   That fabricates equity and triggers deskew/risk on invented prices.
3. `rewards/scanner.py:926-973` opens a **new `requests.Session` per market**
   inside a 10-thread pool (TLS handshake per market); `:73-76,988` keeps two
   unbounded module caches and increments telemetry from 10 threads unlocked.
4. `trading/base.py:102-110,166-173,195-196,221` hardcodes eight economic
   knobs in `__init__` (reward rate 20, max spread 1 c, A-S gamma 0.02, q
   thresholds 15/100, OFI 850, loss $15, drawdown $30, 7-day horizon). This
   violates the no-hardcoded-economics rule at the root of every engine.
5. `paper_trading/engine.py:628-951` — 324-line `generate_quotes` mixing risk
   gate, A-S pricing, post-only clamp, OFI statistics, two strategy branches,
   sizing and registry writes; OFI mean rebuilt twice per cycle.
6. `trading/engine.py:2885-2893` computes `_compute_locked_cash` three times per
   ledger flush; `virtual_queue_book.py:383-404` does O(N) list inserts per
   level on the WebSocket path.

### 1.4 `scripts/` — 149 files, 41,643 lines

| Class | Count | Action |
|---|---:|---|
| Answered one-off | **109** | delete |
| Duplicate of package functionality | **11** | delete after parity check |
| Still-needed one-off | 12 | keep with owner + expiry |
| Recurring operational tool | **17** | promote to `src/`, then delete |

- **P0 fixed now:** `deploy/polymarketbot.service:28` and
  `deploy/cloud_deployment_guide.md:217,236` launched the deleted soak script.
  They now run `python -m polymarketbot.paper.main`.
- 8 tests import implementation from `scripts/` (`test_multi_tier_paper_trader.py:15`,
  `test_native_paper_trader.py:19`, `test_realistic_three_domains_apy.py:13`,
  `test_rebalance_ledger.py:27`, `test_yield_measurement.py:24`,
  `test_uma.py:36`, `test_validate_tape.py:18`), so script deletion requires
  moving helpers first.
- Recurring production logic still living in scripts:
  `run_data_collector.py:34-40,217`, `watchdog_collector.py`,
  `monitor_collector.py`, `run_clock_probe.py:28-31`,
  `run_real_money_trader.py` (76 KB), `run_live_rewards_harvester.py`,
  `monitor_liquid_queues.py`.

### 1.5 Test topology (42,491 lines)

- 9 files still exercise the superseded `paper_trading` engine; ~21 load
  `scripts/*` directly.
- 3 tests touch the network (skips on failure): `test_settlement_routing.py:518`,
  `test_oracle_weather.py:514`, `test_polygon_stream.py:552`.
- Private-attribute assertions (brittle): `test_institutional_engine.py:268-288`,
  `test_domain2.py:234-309`, `test_liquidity_router.py:85-146`,
  `test_fast_cancel.py:52-173`, `test_live_accounting.py:369,577`.
- **Gap closed in this pass:** `paper/pillars/*` had only throwaway checks;
  `tests/test_paper_pillars.py` now pins 13 economic invariants.
- `src/polymarketbot/fill_model.py` is only tested through
  `scripts/calibrate_fill_model.py` (`test_fill_model.py:27`).

### 1.6 Zero-inbound modules and whole packages

- `trading/safe.py`, `trading/relayer.py`, `trading/proxy_wallet.py` — no imports
  anywhere (docstrings only).
- `trading/virtual_queue_book.py` (1,550 lines) — only `trading/__init__.py:102`
  re-export + its own test.
- `universe/` (2 files) — only its two tests; production universe is
  `data/universe.py`.
- `simulator/matcher.py:108` — second `class SyntheticOrderBook`.
- `rewards/queue.py`, `rewards/engine.py` — reachable only via the legacy
  backtest engine.
- `oracle/` (8 files) — no production importer except
  `trading/domain2/harvester_engine.py:82`; competes with the live verdict model.

## 2. Cleanup plan

Ordering is deliberate: each phase is independently shippable, and the suite +
a 60-second paper smoke is the gate after every phase.

### Phase 0 — correctness (done)

- Entry moved to `paper/main.py`; old script deleted; deploy unit + guide fixed.
- `AGENTS.md` documents `scripts/` as temporary-only and the `python -m` entry.
- Suite green; 13 pillar tests added.

### Phase 1 — delete what nothing uses (lowest risk, biggest line drop)

1. Delete the 109 answered scripts + 11 duplicate wrappers (exact list in
   Appendix A). Delete `scripts/__pycache__/run_live_paper_soak*.pyc`.
2. Delete `trading/safe.py`, `trading/relayer.py`, `trading/proxy_wallet.py`,
   `simulator/matcher.py`, `universe/`.
3. Delete the alias exports `trading/__init__.py:206-215`
   (`LatencySniperEngine`, `RewardHarvesterEngine`).
4. Update stale references: `logs.py:93`, `trading/domain2/gamma_universe.py:28`,
   `fill_model.py:3,138`, and the soak anchors in `docs/audit-cycle1.md`,
   `docs/audit-cycle4.md`, `docs/cycle6-status.md`,
   `docs/local-pc-india-analysis.md`.

Gate: `uv run python -m pytest -q` green; 60-second paper smoke writes
`data/paper_engine.db` rows.

Expected: −~20,000 script lines, −~2,100 `src` lines.

### Phase 2 — one definition per concept

1. **CTF constants**: keep `trading/ctf.py:58-65` as the single address/selector
   table; route `trading/settlement.py:62-98,308` and
   `rewards/merger.py:22-23,167` through it. Delete the duplicated calldata
   builders.
2. **Gas**: one measured entry (`ctf_split_arbitrage.py:55` measured POL table /
   `queue_valuation.py:172`); delete literal fallbacks `0.092`, `78_000`,
   `0.0016`, `0.015` (`trading/ctf.py:535`, `rewards/allocator.py:514`,
   `scanner.py:260`, `switching.py:100`, `risk/models.py:151`).
3. **Scoring**: keep `rewards/scoring.py`; delete `paper_trading/rewards.py`
   and make `paper/books.qualifying_score` call it (or document why the
   paper-only variant is separate).
4. **Verdict vocabulary**: `trading/domain2/settlement_fair_value.py` is the
   live one; delete `oracle/base.py`'s competing types or reduce `oracle/` to a
   single adapter if any pipeline still needs it.
5. **`OrderBookLevel`**: define once (e.g. `src/polymarketbot/types.py`), import
   in the five places.
6. **`MarketConfig`/`TokenConfig`**: move out of `paper_trading/models.py` into
   `rewards/` (or `market.py`) so `rewards/scanner.py:28` and
   `strategy/builtin.py:11` stop depending on the package slated for deletion.

Gate: per-item tests (CTF routing, fee/gas equality, scoring parity), suite
green, one smoke.

### Phase 3 — delete the parallel engines

Sequence matters:

1. Move `MarketConfig`/`TokenConfig` (Phase 2.6).
2. Migrate `ui/app.py` and the three old paper runners to `paper.main`; delete
   `scripts/run_paper_trading.py`, `run_native_paper_trader.py`,
   `run_multi_tier_paper_trader.py`, `run_polysim_paper_trader.py`,
   `run_autonomous_daemon.py`, `run_ui.py`.
3. Delete `paper_trading/` (12 files, 5,545 lines) and its 9 test files.
4. Extract `CATEGORY_REGIMES` (`rewards/engine.py:34`) into
   `rewards/scoring.py`; delete `rewards/engine.py` and `rewards/queue.py`.
5. Delete `trading/virtual_queue_book.py` and its test (or move to a research
   folder if the L2 book is wanted later).

Expected: −~9,000 `src` lines, −~6,000 test lines. `src` drops to ~60 k.

### Phase 4 — decompose the god objects (behaviour-preserving)

1. `trading/engine.py` (3,084): split merge settlement (snapshot → unlock → RPC
   → relock), quote placement (`_set_quote` 222 lines), ledger flush.
2. `trading/base.py` (1,578): economic knobs into a validated
   `MarketEconomics`/`RiskConfig` loaded from `data/derived/*`; fail closed when
   absent instead of inventing 20/1.0/0.02/15/30.
3. `simulator/engine.py` (2,073): remove fabricated `0.50`/`0.49–0.51` marks;
   return "no mark" and exclude the position from equity.
4. `rewards/scanner.py`: shared session, one executor, bounded caches, locked
   counters.
5. `trading/client.py` (2,365): split the 349-line `place_order`; move
   `GET /version` outside the lock.

Gate: existing suite + targeted tests for each extraction; no quoted-price
change in a deterministic quote fixture.

### Phase 5 — promote operational tools, then empty `scripts/`

Promote, each as `python -m polymarketbot.<area>.<tool>`:

| Tool | Destination |
|---|---|
| `run_data_collector.py` | `data/collector.py` |
| `watchdog_collector.py`, `monitor_collector.py` | `ops/watchdog.py`, `ops/monitor.py` |
| `run_clock_probe.py` | collector stream or `netclock` CLI |
| `run_real_money_trader.py` (76 KB) + `run_live_rewards_harvester.py` | one `trading/runner.py` |
| `build_kalshi_mapping.py` v2 pipeline | `data/kalshi_mapping.py` |
| `measure_local_latency.py`, `calibrate_fill_model.py`, `preflight_verification.py`, `validate_tape.py`, `report_market_yield.py`, `snapshot_gamma.py`, `backfill_kalshi_candles.py`, `replay_uma.py`, `refresh_universe_metadata.py` | their package CLIs |

Then delete the promoted scripts and the 12 keep-list scripts at their stated
expiry.

## 3. What needs your greenlight

| # | Decision | Default if approved |
|---|---|---|
| A | Delete the 120 scripts in Appendix A | execute in one commit after suite green |
| B | Delete `paper_trading/` + `rewards/engine.py` + `rewards/queue.py` + `trading/virtual_queue_book.py` | after MarketConfig move and UI/runner migration |
| C | Delete `oracle/` (8 files) or keep as adapters | delete unless a data pipeline still imports it |
| D | Promote the 17 operational scripts into `src/` | do it before deleting anything still referenced by a systemd unit |
| E | Decompose `trading/engine.py` / `base.py` / `simulator/engine.py` | follow Phase 4 order; no behaviour change |

## 4. Verification for every phase

```bash
uv run python -m pytest -q                                   # 1,956+ tests, ~3 min
uv run python -m polymarketbot.paper.main --capital 3000 --duration 60 --interval 10 --fresh --gas-usd 0
sqlite3 data/paper_engine.db "select strategy, count(*) from snapshots group by 1"
```

Success metrics for the whole cleanup: `src` < 45 k lines, `scripts` = 0
long-lived files, one paper engine, one verdict model, one scoring module, one
CTF constant table, and a suite that finishes under three minutes.

## Appendix A — delete list (107 scripts)
> **Correction (2026-11-10, after Phase 1 executed):** the `build_*` /
> `download*` / `fetch_wallet_cohort` scripts were restored from git. They are
> the reproducibility path for `data/derived` documented in `README.md`
> ("derived and reproducible"), so they are class **d** (promote to `src/`),
> not class **a**. They stay in `scripts/` until their pipeline is promoted.


```
analyse_categories.py, analyse_fill_quality.py, analyse_positions.py,
analyse_role.py, analyse_roles.py, analyse_taker.py, analyse_ttl.py,
analyze_clob_jump_response.py, analyze_latency_sniper_rebate_tiers.py,
analyze_live_session_join.py, analyze_queue_positions.py,
analyze_settlement_discount.py, analyze_tape_initial.py,
audit_domain3_l2_tape.py, backtest_domain2_regimes.py,
backtest_predictive_gate.py, backtest_quotes.py,
backtest_rewards_engine.py, backtest_rewards_production.py,
bench_cancel_path.py, bench_hot_paths.py, build_kalshi_mapping_active.py, check_clock_hour15.py, check_negrisk_distribution.py, check_trades_btc82k.py,
check_vance_websocket.py, cross_check_indexers.py, crypto_edge.py,
crypto_lag_test.py, crypto_vol_test.py, detect_binance_jumps.py,
domain2_empirical_audit.py, enrich_categories_gamma.py, enrich_market_metadata.py,
fetch_binance_klines.py, fetch_resolution_timing.py, find_best_lp_market.py, inspect_active_btc_markets.py,
inspect_btc_book_levels.py, inspect_btc_depth.py, inspect_clob_events.py,
inspect_clock_and_tokens.py, inspect_crypto_tape.py, inspect_jump_trades.py,
inspect_reversal_details.py, inspect_sub1_negrisk.py, inspect_tape_schema.py,
inspect_trade_legs.py, jump_distribution.py, measure_dual_latency.py,
measure_fill_sizes.py, measure_lead_time.py, mm_crypto15m.py,
mm_maker_edge_family.py, mm_maker_edge_report.py, optimize_domain2.py,
optimize_portfolio.py, optimize_reward_portfolio.py, preflight.py,
profile_wallets_tape.py, reconcile_resolutions.py, record_market_tape.py,
reduce_pmxt.py, report.py, report_kalshi_basis.py,
report_kalshi_candle_check.py, report_kalshi_hf_basis.py,
report_kalshi_trade_lead.py, round_trip_pnl.py, run_autonomous_daemon.py,
run_multi_hour_audit.py, run_multi_tier_paper_trader.py,
run_native_paper_trader.py, run_paper_trading.py,
run_polysim_paper_trader.py, run_ui.py, scan_toxicity.py,
screen_ask_entry.py, screen_base_rates.py, screen_calibration.py,
screen_complete_set.py, screen_crypto_aligned.py, screen_crypto_reference.py,
screen_drift.py, screen_drift_book.py, screen_flb_book.py,
screen_inventory_hedge.py, screen_ladder.py, screen_lifecycle.py,
screen_negrisk.py, screen_reward_pools.py,
simulate_four_strategies_paper_trader.py,
simulate_historical_ortools_returns.py,
simulate_realistic_three_domains_apy.py, summarize_live_rewards.py,
test_alpha_concentration.py, test_anomaly_detection.py, test_calibration.py,
test_disposition.py, test_ev_thesis.py, test_live_polysim_trade.py,
test_polymarket_latency.py, test_selection.py, test_uma_gamma.py,
test_uma_schema.py, trace_microsecond_reaction.py, validate_fee_model.py
```
