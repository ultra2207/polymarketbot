# Quality fix plan (issues 1–35)

Orchestrated in waves so parallel agents never edit the same file. Each agent owns an
explicit file list; files not on that list are read-only for it.

## Verified venue semantics (from `data/raw/docs`)

1. **Reward floor.** "The minimum reward payout is **$1**; amounts below this will not be
   paid." (`site/programs/liquidity-rewards.md:21`). It is a per-market, per-payout
   threshold. Accrue `rate × dt` per condition **per UTC day**, and pay only when that
   day's total ≥ $1. Never apply the floor to an instantaneous rate.
2. **Eligibility age.** Orders must be active **≥ 3.5 s** to be reward-eligible
   (`llms-full.txt:53855`). Time before that accrues nothing.
3. **Queue.** Matching is FIFO within a price level (`docs/data-collection.md:678,693`),
   but the public book aggregates size by price and **destroys arrival order**
   (`docs/limitations.md:96-128`, `docs/polymarket-mechanics.md:99`). The L3 model is
   therefore an *inference*, and no API surface exposes the true position.
4. **Fees.** Per-market `feeSchedule` is authoritative (`docs/fees.md`); prefer
   `FeeSchedule.from_gamma`. An unparseable schedule is a refusal, never a zero.

## Wave 1 — module-level (disjoint files)

| Agent | Owns | Fixes |
|---|---|---|
| **FEE** | `src/polymarketbot/fees.py`, `src/polymarketbot/strategy/venue/reads.py`, `tests/test_fees.py`, `tests/test_clob_truth.py` | #14 unparseable schedule ≠ zero; add `schedule_for_market()` returning a schedule or raising `FeeScheduleUnreadable`; keep `FeeSchedule.zero()` only for `feesEnabled is False` |
| **SIZING** | `src/polymarketbot/strategy/sizing.py`, `src/polymarketbot/live/supervisor/sizing.py`, `src/polymarketbot/live/supervisor/switch.py`, `tests/test_sizing.py`, `tests/test_dynamic_sizer.py` | #2 remove fabricated `competing_depth/participation/fill/adverse` defaults; require measured competing score, mid, trades/min and markout from the caller; refuse when absent |
| **SWITCH** | `src/polymarketbot/strategy/reward/queue.py`, `src/polymarketbot/live/yield_accounting.py`, `tests/test_market_switching.py`, `tests/test_queue_valuation.py` | #3 `taker_fee_rate` required in `compute_switching_cost`, refuse when unmeasured; #12 require a fitted queue model (or its measured parameters) instead of four defaults; replace the formula-reimplementation test with an economic-property test |
| **DESKEW** | `src/polymarketbot/strategy/reward/deskew.py`, plus the deskew-specific test file(s) it finds | #A2 `quote_size`/`bankroll`/`cash_reserve`/`taker_fee_bps` required and used by `evaluate_skew` (pass `holding_time_seconds`); #A3 no `bankroll*0.25` reserve fallback; no hardcoded `taker_fee_bps=0` |
| **REWARD** | `src/polymarketbot/paper/pillars/reward_runner.py`, `src/polymarketbot/strategy/reward/accrual.py`, `tests/test_rewards.py` | #15 per-UTC-day accumulator with the $1 floor at rollover; 3.5 s eligibility; #6 re-place after a full fill (guard on `shares > 0 and cancel_at is None`) |
| **RISK** | `src/polymarketbot/risk/*.py`, `src/polymarketbot/strategy/reward/microstructure.py`, `tests/test_unified_risk_engine.py`, `tests/test_queue_position.py`, `tests/test_microstructure.py`, `tests/test_risk_micro_gate.py`, `tests/test_live_risk_wiring.py` | #10 remove `c_depth/z_score/v_ref/beta` defaults (require measured or tape-fitted); #11 `pull_multiplier`/`widen_offset_multiplier` measured or derived; #13 streak threshold has explicit provenance; #23 mark queue position as **inferred** (`queue_position_inferred`) in `contracts.py` + `as_record`; #25/#26 de-duplicate evaluation and match horizon to the reprice cadence |
| **MEASURE** | `src/polymarketbot/strategy/reward/competition.py`, `src/polymarketbot/strategy/reward/scanner/pricing.py`, `src/polymarketbot/strategy/reward/governor.py`, `src/polymarketbot/strategy/reward/allocator.py`, `src/polymarketbot/strategy/reward/fill_measurement.py`, `tests/test_allocator.py` | #16 UTC-epoch reset; #18 load under a lock, set the flag after assignment; #19 `MAINTAIN` emits 0.0; #20 `bankroll_usd` required; #22 return `None`, not 0.0 |
| **HYGIENE** | `src/polymarketbot/data/uma.py`, `tests/test_uma.py`, `src/polymarketbot/tools/reward_tape.py`, `docs/*.md`, `tests/test_settlement_routing.py`, `tests/test_ui.py`, `tests/test_fast_cancel.py`, `tests/test_optimal_lp_scanner.py` | #33 move `uma.py` + test to `archive/`; #35 shared expiry parser; #34 fix stale script references; #27 network opt-in marker + let non-network failures fail; #29 repo-root path; #31 assert real `DELETE` calls; #28 cache isolation |

## Wave 2 — wiring (shared files, sequential)

| Agent | Owns | Fixes |
|---|---|---|
| **WIRE-CORE** | `src/polymarketbot/live/base.py`, `live/engine/trading.py`, `live/engine/deskew.py`, `live/engine/fills.py`, `live/engine/lifecycle.py`, `live/engine/orders.py` | #1 pass `fee_schedule` from the market into the engine and the deskew optimizer; #A2 pass size/bankroll; #5 log `on_entry` failure at warning; #6 delete `_canceled_order_ids`; #17 epoch samples; #21 staleness check |
| **WIRE-QUOTE** | `src/polymarketbot/live/quoting.py`, `live/engine/quotes.py`, `live/runner_market.py`, `src/polymarketbot/execution/*` | #7 persist measured `queue_ahead`; #8 apply `--min-size`/`--max-spread` before validation; #9 Tier-2 taker pays measured RTT; #A4 fix or delete the three dormant execution modules |

## Verification protocol

- Agents do **not** run the full suite (one compute process). Each runs only its own
  narrow pytest files, plus `ruff check` on its files.
- Orchestrator runs `ruff check src tests` and the full suite after each wave, then a
  bounded dry run.
- Any agent that cannot finish a fix must report it, not suppress the finding.
