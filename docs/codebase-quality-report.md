# Codebase quality report

Audited by three independent read-only reviewers over `live/ risk/ execution/`,
`strategy/ paper/ fees storage` and `tests/ tools/ data/ ui/ docs/ scripts/`, then
synthesised here. Findings that were cheap and safe to fix are already fixed and
verified; the rest are ranked with a recommended order.

## Snapshot

| Metric | Value |
|---|---|
| Ruff findings at start | 1541 |
| Auto-fixed (`ruff --fix`) | 1415 |
| Fixed manually by four agents | ~300 |
| Remaining justified suppressions | 173 `# noqa` lines (149 blind-except, 15 loguru false positives, 11 try/pass, 6 `TRY004`, 3 unused-import re-exports), each with a reason comment |
| Ruff now | **clean** (`src` + `tests`) |
| Test suite | **785 passed**, ~59 s |
| Files touched by the lint pass | ~170 |

Two regressions introduced by the lint pass were caught by the suite and fixed
properly, not reverted:

- `TRY004` changed `ValueError` → `TypeError` at six validation sites and broke
  callers that contract on `ValueError`. Restored with `# noqa: TRY004`.
- Narrowing removed the catch that let a rebate-read failure degrade gracefully.
  The degradation is now explicit at both call sites (`rebates.py:154`,
  `rewards.py:198`) instead of accidental.

## Fixed in this pass

| # | Defect | Evidence |
|---|---|---|
| 1 | Queue entries leaked on cancel-all and terminal order sync; price-tie selection picked the **oldest** entry, so a re-placed quote inherited a cancelled order's queue position | `lifecycle.py`, `orders.py`, `risk/unified.py:_entry_for` |
| 2 | Engine returned `measured=True` with no tracked entry and no economic inputs | `risk/unified.py` |
| 3 | The economic rule divided out the queue, so every toxic tick pulled even behind thousands of shares. Now `consumption × adverse > capture` | `risk/unified.py:_measured_costs` |
| 4 | Direction was always OFI, so a DRIFT-triggered move protected the wrong side | `risk/unified.py:_direction` |
| 5 | Feed fabricated **100-share** depth for any level with a missing size | `live/engine/feed.py` |
| 6 | OFI threshold ignored measured dispersion (`rolling_ofi_std=0`), collapsing to `1.5 × depth` | `strategy/reward/microstructure.py` |
| 7 | The adverse-fill streak's size reduction was a silent no-op | `live/quoting.py` |
| 8 | Only non-MAINTAIN decisions were recorded, so "why didn't it pull" was unanswerable | `live/quoting.py` |
| 9 | The auto-selector expiry fix covered only the scanner; the live runner still read `end_date_iso`, which the rewards payload never publishes | `live/runner_market.py:533` |
| 10 | A corrupt minute parquet was silently overwritten with the current batch, destroying rows already flushed | `tape_buffer.py`, `crypto_updown_books.py` |
| 11 | Narrowing broke the cancel contract (`ValueError`/`UnicodeError` escaped `send_delete`) and dropped `http.client.HTTPException` from four tool fetch loops | `execution/fast_cancel.py`, `tools/*` |
| 12 | A missing expiry was ranked as "999 days", admitting already-ended markets | `scanner/candidates.py` |
| 13 | Rejection reasons named a field the payload never publishes | `scanner/candidates.py`, tests |

## Open findings — P1 (before the canary)

| # | Finding | Location | Why it matters |
|---|---|---|---|
| 1 | `fee_schedule` is never passed to the live engine; fees use the linear fallback and rebates a fixed 20 % share | `live/engine/deskew.py:18-40`, `trading.py:88-89`, `fills.py:231` | On a 0.07 curve the linear model charges 2×; on `crypto_15_min` 8×. `_apply_fill` books that as `realized_pnl`. Cash is reconciled from the venue every 15 s, **equity is not** — the canary's measured edge is corrupted. |
| 2 | Live quote distance comes from fabricated constants (`competing_depth=1500`, `participation=0.15`, hand-set fill/adverse curves) | `strategy/sizing.py:174-184`, called from `live/supervisor/sizing.py:78`, `switch.py:153` | The spread we rest at is the whole strategy. Measured alternatives exist and are unused. |
| 3 | Switching hurdle prices the taker fee at zero | `strategy/reward/queue.py:423-430`, `live/yield_accounting.py:169-181` | A 100-share cross at 0.5 costs ~$1.75/leg; the engine can switch into a net-negative move. |
| 4 | De-skew dynamic calibration never runs live; `max_holding_seconds` is dead, aged inventory never force-liquidated | `strategy/reward/deskew.py:365-390`, `live/quoting.py:182-193` | Thresholds stay static and inventory can age indefinitely. |
| 5 | De-skew prices Tier-2 crossing with `taker_fee_bps=0` and a 25 %-of-bankroll reserve fallback | `deskew.py:243-247,376` | Crossing is priced free; the reserve fallback is the hand-set fraction the allocator refuses elsewhere. |
| 6 | The shared reward runner suppresses re-placement after a **full fill** at an unchanged price | `paper/pillars/reward_runner.py:294-299` | Stops earning until the book moves a tick — and it is not paper-only. |
| 7 | An unparseable fee schedule becomes `None`, which downstream prices as a zero fee | `strategy/venue/reads.py:96-103` | "Fee-free" and "could not read" are conflated; trades on a fabricated zero cost. |

## Open findings — P2

| # | Finding | Location |
|---|---|---|
| 8 | Paper applies the $1 floor to the instantaneous rate; live applies it to the day's sum | `accrual.py:150-155` vs `yield_accounting.py:122-124` |
| 9 | Competing-score window is a trailing 24 h, not the UTC epoch the docstring claims | `competition.py:88-98` |
| 10 | Scanner measurement-table load still has the "flag before assign" race the fill loader fixed | `scanner/pricing.py:63-70,86` |
| 11 | Queue valuation model still ships four hand-set parameters; `QueueParameterFit` is never wired | `queue.py:264-270`, `live/base.py:193` |
| 12 | Governor `MAINTAIN` fabricates `0.75 × bankroll` **into a shares field** | `governor.py:149-155` |
| 13 | Allocator defaults `bankroll_usd=150.0` when a caller forgets it | `allocator.py:155-160` |
| 14 | `_canceled_order_ids` is written and never read (grows for the process lifetime) | `live/engine/trading.py:269` |
| 15 | Quote rows persist `queue_ahead: 0.0` while `on_entry` just measured it | `live/engine/quotes.py:191,206` |
| 16 | `test_settlement_routing` makes a live Polygon RPC call and its broad catch can make the only on-chain smoke test unfailable | `tests/test_settlement_routing.py:518-529` |
| 17 | Scanner tests share a process-global cache key; file order is what makes them pass | `tests/test_optimal_lp_scanner.py:65-117` |
| 18 | `test_ui` resolves `src/` from the CWD, with no `conftest.py` pinning the root | `tests/test_ui.py:9-18` |
| 19 | `data/uma.py` (732 lines) has no importer in `src/`; `test_uma.py` (101 asserts) tests only dead code | `src/polymarketbot/data/uma.py` |
| 20 | 27 docs reference `scripts/*` files that no longer exist; the data-collection runbook's commands raise `ModuleNotFoundError` | `docs/*.md` |

## Open findings — P3

- Duplicate expiry parser in `tools/reward_tape.py:79-89` vs `tools/crypto_updown_books.py:93-112`, with **reversed key precedence** — the same market can get two different expiries.
- `test_queue_valuation` re-implements the model's own formula, so it cannot catch a sign or coefficient error.
- `test_fast_cancel` asserts the mock's echo rather than that three `DELETE /order` requests went out.
- Tests still reach through private layers (`_state`, `.flow`) to assert public behaviour.

## What is healthy

- **Fail-closed discipline is real.** Stale/missing books are refused rather than pinned at 0.50; `_mark_prices` never invents a price; unmarked inventory is a counter, not a zero; `fill_model` refuses to load a bad artifact; allocator refuses a missing pool or competing score.
- **Venue-truth reconciliation.** Orders leave `active_quotes` only after a confirmed cancel or a `get_order` read; absence from the open list never implies a fill; matched-size deltas are clamped.
- **Latency contract.** Place and cancel both pay a measured RTT and emit `place_rtt_ms` / `cancel_rtt_ms`; nothing in the quote path assumes 0 ms. (Exception: the Tier-2 taker cross.)
- **Concurrency.** `QueuePositionTracker` and `UnifiedRiskEngine` each serialise state behind one `RLock`, with consistent lock order; the FIFO invariant is implemented once and tested.
- **Accounting.** `Ledger.reconcile` checks both `equity == cash + lots` and `Δcash == journal SUM`; rebate crediting is venue-first and cannot double count.
- **Tests where it counts.** 638 test functions, only 30 mock assertions; the money paths have behavioural tests (venue-confirmed cancel, breaker fan-out, EIP-155/1559 vectors). The new queue model has 14 deterministic FIFO tests; the fill-table race test genuinely hammers 16 threads.

## Recommended order

1. **Before funding (canary):** items 1 and 2 — otherwise realised PnL and the spread we quote are both wrong, and the canary cannot measure edge. Items 3–7 are pre-existing paper-to-live gaps that do not block a 5-share mechanics run.
2. **Immediately after the canary:** items 16–19 (test integrity and dead code), then item 20 (docs an operator follows).
3. **Before scaling capital:** the replay validation of the risk gate against `ctf_maker_markout.json` / `ctf_quote_fill_horizon.json`, which is still the one claim never tested: that the new gate is *better*, not merely wired.

## Status after the 2026-11-11 pass

All 20 open findings and the P3 items are fixed; the 13 "fixed in this pass" rows were
already closed. Evidence: `uv run pytest -q` → **811 passed**; `uv run ruff check src tests`
clean.

| Item | Fix |
|---|---|
| P1 1 fee schedule | `fees.schedule_for_market`; `FeeSchedule` passed into the live engine; deskew prices taker fees from the curve and books maker rebates from `rebate_rate`. Unparseable → `FeeScheduleUnreadable`, never zero. |
| P1 2 sizing | `optimize_spread_offset` has no defaults; supervisor measures competing score, trade rate, markout and participation, or refuses. |
| P1 3 switching | `taker_fee_rate` required; measured `0.0` still valid, absent refused. |
| P1 4–5 deskew | Optimizer requires size/bankroll/reserve/fee; thresholds always calibrated; `holding_time_seconds` supplied from tracked inventory age; Tier-2 crossing pays measured RTT + block interval and re-reads the book at landing. |
| P1 6 reward re-place | Full fill re-places; no cancel submitted for a zero-share remainder. |
| P1 7 fee parse | Same as item 1. |
| P2 8 floor | `RewardDayAccrual` banks per condition per UTC day, settles at rollover, $1 floor on the day total, 3.5 s eligibility. |
| P2 9–13 | UTC-day reset; loader race fixed under lock; queue model must be fitted (else switching disabled/logged); `MAINTAIN` emits measured/NULL, not `0.75 × bankroll`; bankroll required. |
| P2 14–15 | `_canceled_order_ids` deleted; measured `queue_ahead` persisted in quotes, telemetry and SQLite. |
| P2 16–20 | Network marker + real failures; cache isolation; CWD-free `test_ui`; `uma.py` archived; docs reference real entry points. |
| P3 | One expiry parser; queue test asserts economic properties; `test_fast_cancel` asserts the `DELETE` calls; tests use public observations. |

**Current live state:** the queue model and microstructure calibration artifacts
(`data/derived/queue_fit.json`, `data/derived/risk_calibration.json`) do not exist, so
capital switching and the micro gate start **disabled with a warning**; hard risk limits,
fees, deskew, reward accrual and staleness gates are active. This is the intended
fail-closed state after removing fabricated parameters. Producing those artifacts from the
tape is the next measurement task, not a code default.
