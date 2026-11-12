# Strategy sweep — findings after the 2026-11-11 fix pass

Independent sweep of `strategy/reward/`, `strategy/sizing.py`, `strategy/books.py`,
`risk/`, `paper/`, `fees.py`. Started from `docs/quality-fix-plan.md`,
`docs/codebase-quality-report.md` (status section) and `docs/audit-cycle8.md`; the
items already listed there as fixed are not repeated.

Priorities used: protocol correctness and money paths first, then audit-trail
truthfulness, then dead code. A defect whose honest fix is a model refit (needs a
new measurement artifact and a cross-owner signature change) is listed as
**deferred** with the measurement it needs, not patched with another number.

## Fixed

| id | sev | file:line | defect | fix |
|---|---|---|---|---|
| S1 | P1 | `strategy/reward/waterfill.py:89-118` | KKT fallback funded **every** starved activation lot unconditionally, so when the lots did not all fit the budget it returned `sum(allocated) > usable_capital` (two \$60 lots on a \$100 budget → \$120). The caller's cash invariant broke *after* the decision was taken. | Divisible KKT allocation first, then only the cash that optimum left idle goes into indivisible lots, best yield-per-dollar first, never beyond the budget. Regression test `test_water_filling_never_spends_more_than_the_budget`. |
| S2 | P1 | `strategy/reward/accrual.py:253-268` | `denom = competing + our_score` is right only when the supplied books **exclude** our order. The venue L2 snapshot a live engine reads contains our resting order, so the claim was divided by itself twice (up to 2× under-credit; paper and live priced the same order differently — a parity break). | `books_include_our_order` states which book the caller holds; when True our raw score is trimmed from the measured total before the `1/c` score is added. Paper caller passes `False` explicitly. Test `test_measured_accrual_does_not_count_our_own_score_twice`. |
| S3 | P1 | `risk/manager.py:461-478` | `validate_quote` defaulted the account state: `current_cash_usd=300.0`, `current_pos_shares=0.0`, `current_pos_cost=0.0`, `current_portfolio_inventory_usd=0.0`. A caller that forgot any of them gated the quote against an account that does not exist, and every forgotten position/cash field **widened** the limit instead of narrowing it. | All four required; the docstring says why. `tick_size` is documented as accepted-but-unused (rounding belongs to the placement path). |
| S4 | P2 | `risk/manager.py:512-534` | Micro gate: `eff_mid = mid_price if mid_price is not None else price` priced the hold cost from a mid equal to our own limit (zero capture), so any measured hold cost exceeded it and every toxic tick became a pull. | `mid_price` is required; a missing mid refuses with a stated reason. |
| S5 | P1 | `risk/manager.py:144-211`, `risk/models.py:151-155` | `GasMergeManager.can_merge` priced the captured spread at a fabricated `assumed_spread_cents=1.0` and the gas at a fabricated `gas_fee_usd=0.015`; `record_merge` booked \$0.015 of "gas" whenever a caller omitted the measured fee. The test `test_default_merge_gate_is_unchanged` pinned it. | `can_merge(shares, captured_spread_usd, gas_fee_usd)` requires both measurements (refuses when absent); `record_merge(shares, gas_fee_usd)` requires the fee; both config fields deleted. Test rewritten as `test_merge_gate_requires_measured_spread_and_gas`. |
| S6 | P1 | `strategy/reward/queue.py:745-753` | Switching cost added `max(0.02, …)` — \$0.02 of friction no measurement produced (4× a \$0.005 true cost) — and defaulted `transit_latency_seconds=4.5`, 8–14× the measured CLOB p50 (322–570 ms). Its docstring claimed the venue has "no minimum rest time", so the destination quote was assumed to start earning on landing. | Floor removed (total is the sum of measured parts); latency required; downtime now includes the documented 3.5 s eligibility age (`REWARD_ELIGIBILITY_SECONDS`), which increases the cost — the safe direction. |
| S7 | P2 | `strategy/reward/queue.py:762-770` | `holding_horizon_hours=4.0` default converts the one-off switch cost into a daily hurdle (a \$1 cost → \$6/day). | Required; docstring states the 3× sensitivity. |
| S8 | P1 | `strategy/reward/switching.py:41-46,118-150` | `crossing_spread_friction_pct=0.0025` priced the cost of converting cash into the candidate position at a hand-set 25 bps, and the **live** auto-selector relied on that default (`live/runner_market.py` passes only the scan interval and the hurdle). Understating the cost of switching *toward* a market is the dangerous direction. | No default; unmeasured → `+inf` with a warning so no migration is chosen on a guessed cost (same fail-closed rule the queue model uses). |
| S9 | P2 | `strategy/reward/switching.py:462-486` | `SwitchEvent.hurdle_threshold` was recomputed as `y_from·(1+h) + cost` (unamortized) while the decision used the amortized cost — 6× disagreement at the default horizon, so the audit record contradicted the decision it documents. | Both come from `evaluate_switching_hurdle` with the same amortization. |
| S10 | P2 | `strategy/reward/governor.py:37-60` | `hurdle_pct=0.15` was never read, while the class docstring promised `Delta_Yield · T_holding ≥ Switching_Cost · (1+hurdle_pct)`. The governor emitted `SWITCH_MARKET` on dwell time alone and had no switching-cost term at all. | Dead parameter deleted; docstring says the hurdle needs measured migration cost and lives in `switching.evaluate_switching_hurdle`. |
| S11 | P1 | `strategy/sizing.py:258-330` | The offset search multiplied the **measured** participation by a fabricated `0.70 · exp(-1.5·distance)` decay, awarded `1.0` inside the spread and `0.70` at the touch, and bounded itself with `0.95·v`, `max(1.5·spread, 3.5c)`. Three invented constants decided the distance the strategy rests at, on top of an input that was already measured. | Candidates are the offsets whose fill probability the tape measured — at or inside the measured touch, capped by the reward band and the price room. The measured participation *is* the fill probability (`fills/day = prints/day × participation`); no offset scaling is invented. Docstrings record why. |
| S12 | P3 | `strategy/reward/scoring.py:254-284`, `reward/__init__.py`, `tests/test_rewards.py` | `DEFAULT_COMPETITOR_LADDER` (35/30/20/15 % at 0.5/1/2/3.5 c) and `compute_competitor_ladder_score` were a fabricated competitor distribution, re-exported in the public API, with no production caller — and a test that pinned the fabricated ladder as if it were measured. | Deleted with the test and the exports. The real competitor measure is `competition.market_competing_detail`. |
| S13 | P2 | `strategy/reward/scoring.py:1-20` | Module docstring asserted the 3.5 s eligibility rule "appears nowhere in the documentation" and that the venue "pays whatever rests at that instant". It is documented (`data/raw/docs/llms-full.txt:53855`) and `accrual` enforces it — the file contradicted the money path it feeds. | Docstring separates the *score* (no time term) from *eligibility* (3.5 s, applied to accrual time). |
| S14 | P2 | `fees.py` (Taker Rebate Program block) | A complete second implementation of the Taker Rebate ladder (`REBATE_TIERS`, `W_VOLUME_WEIGHT`, `weighted_volume`, `taker_tier`, `taker_rebate`, `turnover_needed_for_tier`, `REBATE_LIVE_FROM`) duplicated the config-driven model with its own copy of every threshold, had no production caller, and could disagree with `configs/fees.yaml` silently. | Deleted, together with the seven tests that exercised the duplicate (the published worked examples and the tier ladder stay pinned on `weighted_taker_volume`/`taker_rebate_rate`). |
| S15 | P2 | `fees.py:51` | `CONFIG = Path("configs/fees.yaml")` resolved only when the process ran from the repository root; anywhere else the first fee lookup raised `FileNotFoundError`. | Anchored to `Path(__file__).resolve().parents[2]`, like the measurement artifacts. |

## Deferred — reported, not patched with another number

| id | sev | file:line | defect | why deferred / what it needs |
|---|---|---|---|---|
| D1 | **P1** | `strategy/reward/deskew.py:135,187,86,115,128,144,168,446,465,249-254` | The de-skew model is parameterised by hand-set constants, and the one that matters most reaches a money decision: `DynamicDeSkewOptimizer.__init__` → `calibrate_all(...)` never passes `trades_per_min`, so every `max_holding_seconds` is fitted from an invented **0.35 prints/min** — the threshold that force-liquidates aged inventory at a loss. Also `(0.20·bankroll)` q_soft cap, `(0.60·bankroll)` risk cap, `(0.30·bankroll)` merge cap, `3.5·quote_size` absorption, `1.5·q_soft` floor, `0.15·expiry` pin ceiling, `0.40·spread` skew aggressiveness, `gamma=0.02`, `temporary_impact_eta=1e-4`, `ofi_toxicity_threshold=850`, `drift_toxicity_cents=1.5`. | Needs a measured print rate and a fitted holding-time distribution from the tape, plus a new **required** constructor argument that `live/base.py:_build_deskew_optimizer` must supply (cross-owner signature change). Swapping 0.35 for another number is exactly what the rules forbid. |
| D2 | **P1** | `strategy/reward/reserve.py:17,34,50-51,81-82` | The cash reserve is built from unfitted coefficients: `jump_shock_factor=0.20` (default), L2 diversification weight `0.15`, a \$0.35 per-market buffer whenever the caller supplies a measured gas base, `×100`/`×25` merge-gas multiples, and a `max(0.20, …)` gas floor. These set how much of the bankroll is withheld from quoting (2-pass allocator, `derive_quote_size`). | Replacing them with a derived bound (`gas × (positions+1) + max h_i`) moves deployable capital 1.5–3× on the live path. Needs the measured fill-shock distribution and an explicit reserve-policy decision. Recorded here rather than changed silently. |
| D3 | P2 | `strategy/reward/microstructure.py:71-73,132-134,148` | Threshold model constants: `critical_imbalance=0.80`, `volatility_ref=0.02`, `min_drift_cents=0.10`, `base_cooldown_seconds=120`, `severity_exponent=1.25`, `horizon_exponent=0.20`, clamps `0.25–2.0` / `0.70–1.50` / `0.90`. | A refit of the OFI/drift thresholds against the tape, not a constant swap. The gate is disabled without `data/derived/risk_calibration.json`, so nothing is mispriced today. |
| D4 | P2 | `strategy/reward/governor.py:113` | Resize hysteresis `max(15.0, 10 % of old capital)` is a hand-set constant; it suppresses churn but nothing measures the churn cost it is standing in for. | **Resolved this pass:** `solve_hysteresis` derives both gates from measurements — churn cost = accrual yield × (2 × CLOB RTT + 3.5 s eligibility), marginal yield = the allocator's KKT price of capital, and both the resize break-even and the dwell are their ratio per cycle. `live/base.py` passes the persisted RTT and the measured accrual yield; an unmeasured cycle holds the position and says so. |
| D5 | P2 | `strategy/reward/switching.py:41-42,174` | `switch_hurdle_pct=0.20` and `amortization_horizon_hours=4.0` defaults are policy rather than measurement. They make switching *harder* (conservative), and `live/runner_market.py` already sets the hurdle to 0.0. | **Resolved this pass:** both defaults deleted. `solve_switching_hurdle_inputs` solves `hurdle = round_trip_cost / capital_moved` (crossing friction + gas + latency × yield) and `payback_hours = round_trip_cost / yield_differential_per_hour`; the caller passes a measured holding horizon or the derived payback is the amortisation horizon (at which the switch is break-even, so the premium rejects it). `live/runner_market.py` supplies the persisted CLOB RTT. |
| D6 | P3 | `strategy/reward/deskew.py:529` | `mid_no = … if (best_bid_no and best_ask_no) else 0.50` — an unreachable fabricated mid (the function already refused a one-sided book). | Left with D1: the whole Tier-2 cost model is fabricated, a one-line deletion would imply the rest was measured. |

## Out of scope — for the owner of `live/` (not edited here)

- **D7** `live/yield_accounting.py:180,260` keep `transit_latency_seconds=4.5` and
  `holding_horizon_hours=4.0` defaults and feed them into the now-required
  `queue.py` parameters. The fabricated 4.5 s latency therefore survives on the live
  path until that file passes measured values (CLOB p50 and the intended hold).
- **D8** `live/runner_market.py:184-187` builds `SwitchingConfig` without
  `crossing_spread_friction_pct`. With S8's fail-closed rule the auto-selector now
  logs `switching cost unknown: crossing friction is unmeasured` and keeps the
  current market. **This is the one behaviour change Main should decide on:** supply
  a measured crossing fraction (half the candidate's spread + the taker fee at its
  quoted price), or accept switching disabled.
- **D9** `live/engine/quotes.py:62` passes `mid_price=token_mid if token_mid is not
  None else price`, i.e. it re-creates the fabricated mid that S4 now refuses
  inside the gate. The gate cannot see a caller that substitutes the value itself.
- **D10** Three failures in `tests/test_quote_lifecycle.py`
  (`'_HarnessEngine' object has no attribute '_unrealized_pnl'`) come from in-flight
  sibling edits to `live/base.py` / `live/engine/accounting.py`;
  `_unrealized_pnl` exists only in `live/engine/accounting.py`. Unrelated to this
  sweep's files.

## Checked and healthy (no change)

- Reward: per-condition **per-UTC-day** accrual with the \$1 floor applied at
  rollover only (`RewardDayAccrual`), 3.5 s eligibility anchored at
  `placed + 3.5`, one accumulator per condition so an interval is never counted
  twice, `CompetingScoreWindow` resetting at the UTC boundary (tested), and the
  single-sided `1/c` fraction applied at every candidate/accrual site.
- `commit_quotes` reserve ledger: a kept resting bid is charged at its resting size,
  not the freshly optimised one; unaffordable quotes are dropped rather than
  over-committed.
- Risk: FIFO `ahead` clamped monotone non-increasing, `queue_position_inferred`
  carried into `as_record`, one `RLock` per tracker/engine, breaker trip vs reset
  semantics (`PRICE_JUMP` = bounded cooldown, everything else = halt until
  `resume`), PULL/SUPPRESS/BLOCK precedence in `validate_quote`.
- Paper: maker fills book no fee (makers pay nothing on this venue), rebates are
  venue-first with delta accounting and cannot be double credited, marks never
  invented (unmarked inventory is a counter).
- `books.py` mid refuses locked/crossed books; `qualifying_score` and
  `quadratic_order_score` agree on the band boundary.

## Tests run

```
uv run pytest tests/test_allocator.py tests/test_rewards.py -q -x                    → 37 passed
uv run pytest tests/test_paper_core.py tests/test_paper_engine.py
   tests/test_paper_pillars.py tests/test_reward_epoch_and_rebates.py
   tests/test_reward_rebate_credit.py tests/test_queue_position.py
   tests/test_unified_risk_engine.py tests/test_live_risk_wiring.py -q -x            → 83 passed
uv run pytest tests/test_queue_valuation.py tests/test_market_switching.py
   tests/test_sizing.py tests/test_risk_breaker_semantics.py
   tests/test_risk_micro_gate.py tests/test_fees.py
   tests/test_maker_fee_rebates.py tests/test_fill_measurement_race.py -q           → 197 passed
uv run pytest tests/test_decision_parity.py tests/test_deskew.py tests/test_microstructure.py
   tests/test_optimal_lp_scanner.py tests/test_quote_lifecycle.py
   tests/test_clients.py tests/test_hft_execution.py tests/test_chain_settlement.py  → 147 passed,
                                                                                       3 failed (D10, sibling)
uv run ruff check <every touched file>                                              → clean
```

## Files changed

`src/polymarketbot/strategy/reward/{scoring,waterfill,accrual,queue,switching,governor,__init__}.py`,
`src/polymarketbot/strategy/sizing.py`, `src/polymarketbot/risk/{manager,models}.py`,
`src/polymarketbot/paper/pillars/reward_runner.py`, `src/polymarketbot/fees.py`;
tests: `tests/test_allocator.py`, `tests/test_rewards.py`,
`tests/test_reward_epoch_and_rebates.py`, `tests/test_risk_breaker_semantics.py`,
`tests/test_risk_micro_gate.py`, `tests/test_queue_valuation.py`,
`tests/test_fees.py`.
