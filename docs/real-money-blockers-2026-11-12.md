# Real-money blocker sweep — 2026-11-12

Three independent read-only sweeps of git `c58ad31` (execution/venue, strategy/risk/economics,
infra/data/deploy) before the first live canary. Every finding below is either fixed in this
commit or recorded with the exact live measurement that still gates it.

**Result: no proven P0 static money-loss path remains.** The remaining items are P1/P2 either
(narrowly) fixed now or verifiably dependent on live canary data.

## Fixed now (no live data required)

| id | severity | defect | fix | evidence |
|---|---|---|---|---|
| B1 | P1 | Tier-2 emergency taker de-skew bypassed every capital/position guard: it placed a live marketable order and only then subtracted cash, so it could overdraw, breach the per-token/portfolio cost caps, or execute after a halt. | `DeskewMixin._taker_risk_refusal` runs before the cross: refuses while halted/kill-switched; a BUY must fit free cash, `max_position_cost_per_market` and `max_portfolio_inventory_usd`. SELL (risk-reducing) is not blocked by cash. | `live/engine/deskew.py`, `tests/test_deskew.py` (3 new tests) |
| B2 | P1 | Live startup silently traded with the microstructure gate unwired when `data/derived/risk_calibration.json` was absent (`RiskManager._micro is None` → toxic-flow/queue/adverse-fill veto skipped). | `run_startup_check` refuses live (exit 1) when the engine reports `micro_gate_active=False`; dry-run is unaffected. | `live/supervisor/startup.py`, `tests/test_live_runner_gating.py` |
| B3 | P2 | Paper `_rtt()` returned `0.0` when the RTT probe degraded mid-run, so cancellations/quotes could be scheduled at zero latency in the same engine that refuses unmeasured submissions. | Raises instead of substituting zero; startup already refused an unmeasured probe. | `paper/engine.py`, `tests/test_paper_core.py` |
| B4 | P1 | Deploy `rsync` had no secret excludes: a workstation `.env`/`key.txt`/`*.pem` would be copied into `/opt/polymarketbot`. | Explicit excludes plus a post-sync guard that aborts if any secret file is found in the app tree. Secrets stay in `/etc/polymarketbot/polymarketbot.env` (0600). | `deploy/deploy.sh` |
| B5 | P1 | systemd unit: `Restart=always` re-enters after a clean `--duration` exit, the default unit named three retired strategies, and the 1.2/1.5 GB caps conflicted with the documented >=2 GB host profile. | `Restart=on-failure`/10 s, `MemoryHigh=1.5G`/`MemoryMax=2.0G`, default command is the canonical 1800 s paper soak, comments name the single RewardHarvester pillar. | `deploy/polymarketbot.service` |
| B6 | P1 | Rebate telemetry overwrote the per-fill modelled credit with the venue total without recording the difference, hiding the only evidence that can calibrate it. | `sync_rewards` logs `venue vs modelled` rebate dollars (no threshold) before updating the counter; cash still carries the modelled per-fill credit. | `live/engine/rewards.py` |
| B7 | P2 | `storage.py` docstring claimed the WAL/busy contract was wired nowhere, contradicting `strategy/ledger.py` and `live/store.py`. | Docstring now states the real wiring. | `storage.py` |
| B8 | P1 | `MarketAllocation.quote_size` divided dollar capital by the **leg mid**, so the governor resized the live engine to 2-4x the intended shares (a $38 allocation at a $0.475 mid became 80 shares, $76 of capital). The activation lot made the mirror error: it charged the two-leg pair price to the **one-sided paper pillar**, refusing a valid 20-share bid at `$19.6` because it demanded $20. | Every candidate source publishes `capital_per_share` (pair cost on the two-sided live path, quote price on the one-sided paper path); `activation_lot_capital` returns `shares * capital_per_share` (default `$1.00` for legacy callers); `allocation_for_capital` divides by the same field. This also fixes the pre-existing `test_decision_parity` failures. | `scanner/candidates.py`, `quote_candidates.py`, `allocator_candidates.py`, `marginal.py`, `tests/test_allocator.py` |
| B9 | P2 | Deploy rerun preserved a loosened `0600` env-file mode; the secret guard was name/depth limited; the unit kept an app-tree `.env` fallback; the cloud guide still named London/1 GB and the checklist used non-canonical credential names. | Enforce `chmod 600`/`chown` on every deploy; broaden the guard (`.p12`, `.pfx`, `id_rsa*`, `.netrc`, `credentials*.json`) and prune `.venv/.git/data`; single `/etc` EnvironmentFile; guide uses `eu-central-1` + `t3.small` (2 GB) and 1.5/2.0 GB caps; checklist lists canonical names + aliases. | `deploy/*`, `docs/pre-deposit-checklist.md` |

Verification for these: targeted tests below; `ruff` clean on every touched file; `bash -n`
on the LF form of `deploy.sh`.

## Deferred — needs live canary data (carry into tomorrow's deployment)

| id | severity | what must be measured | why code cannot settle it | mitigation for the canary |
|---|---|---|---|---|
| L1 | P1 | Modelled maker rebate credited per fill (`maker_fee_rebate` on the market schedule) vs the venue's `/rewards` `maker_rebates_usd` total. B6 now logs the delta each sync. | Whether the schedule's rebate equals the venue's credited share is an empirical fact about the venue's program; a code change would just pick another assumption. | Treat venue balance as the money answer; watch the reconciliation log line. If the delta is material, stop crediting rebates to cash and credit only venue deltas. |
| L2 | P1 | Reward accrual (`/rewards/user` actual) vs the model on the same market, same UTC day. | The model is the venue formula on one `/book` snapshot; only live accrual proves the snapshot timing/depth assumption. | Canary's own `/rewards/user` accrual on its market; compare with `reward_epochs.modelled`. |
| L3 | P2 | Fill probability and adverse markout at the canary's actual tick/flow cells. | Tables are measured on historical tape cells; the canary's market may pin to pooled support or fall outside it. | Elog/record fills; re-run `ctf_quote_fill_horizon` on the canary window; confirm the scanner's `fill_horizon_s` cell exists. |
| L4 | P2 | Locked-cash accounting mode (does the CLOB balance endpoint include reserved BUY collateral?) | Determined by comparing free-cash reads before/after a known placement; the code leaves the mode uncalibrated inside the inconclusive band. | The canary places one order; read the calibration log. Until resolved, equity uses the conservative branch. |
| L5 | P2 | Venue taker delay (`itode`/`takerDelayMs`) on the canary market versus the measured CLOB RTT used for the deskew landing. | The hold is a venue property of the specific market; without a live order there is no measurement. | Deskew is size-capped and now risk-gated; do not enable a market that publishes a hold but no duration (already refused). |
| L6 | P2 | Microstructure/queue calibration quality on the canary venue: fill rates, queue-ahead at entry, OFI/drift triggers. | `risk_calibration.json` is fitted from historical tape, not live fills. | Watch `risk_events` (`micro_risk_decision`) rows; if every quote is MAINTAIN or every pull is triggered, stop and refit. |
| L7 | P2 | Maker quote landing vs paper's delayed ActionBook model. | Paper simulates a submit→landing delay; live places a post-only limit immediately and measures the RTT around the call. This is a **modelled difference, not a defect** (post-only cannot cross; price is fixed), but fill timing differs. | Compare canary fills with the paper run on the same market; do not treat paper fill counts as live predictions. |
| L8 | P1 | Engine lock held across blocking work: Tier-2 de-skew sleeps the measured flight/taker delay/block interval under `self._lock` (up to ~2.5 s), and `sync_live_orders_and_fills`/`cancel_all_quotes` make venue HTTP calls while holding it. | Refactoring to snapshot-under-lock → I/O-unlocked → apply-under-lock changes the engine's concurrency contract; doing it untested before the canary is riskier than measuring the hold first. | Record lock-hold duration (log timestamps around `evaluate_quotes`, sync, cancel-all) and whether a feed/fill reaction was delayed during Tier-2; if p99 hold > the reprice interval, do the refactor with a dedicated test net. |
| L9 | P2 | The live session recorder's `_seen_orders`/`_seen_trades` dedupe sets grow for the session lifetime. | A bound is an operational memory policy, not a measured constant; the canary session is short. | Watch recorder RSS on a long session; add an aged/LRU bound if it grows. |

## Reviewed and intentionally not changed

- **5% flat reserve** is an explicit operator policy (directive: reserve 5%, deploy the rest), not
  a missing endogenous solve. The allocator logs the policy; a measured reserve is opt-in.
- **Live maker placement is immediate**, unlike the paper `ActionBook` landing queue. The order is
  post-only with a fixed limit, so it cannot cross or pay a taker fee; RTT is measured and logged,
  and replacement is refused while a cancel is unconfirmed. The AGENTS latency contract describes
  the paper simulation; applying it literally to maker placement would add artificial delay, not
  safety.
- **CTF split is refused live** (`trigger_ctf_split` returns an error when `not dry_run`). No live
  caller exists; it is a safe refusal, not a simulated success.
- **`redeem_positions` has no caller** in the live supervisor. The function itself validates
  calldata/key/route refusal and broadcasts; an on-chain outcome/route preflight
  (`verify_route(CTF_REDEEM, …)`) is available but not wired because nothing invokes redemption
  automatically. Do not enable automated redemption before wiring that check.
- **Neg-risk markets, unmeasured merge gas, sub-minimum orders, truncated open-order lists** all
  fail closed already (prior sweeps E3/E7/E24 and startup gates).
- **Shutdown can exit 3** when cancellation cannot be proven, after bounded retries; this is the
  intended fail-closed signal, not a silent success.

## Tomorrow's canary checklist (deltas on top of `docs/pre-deposit-checklist.md`)

1. Confirm `data/derived/risk_calibration.json` is present (startup now refuses without it).
2. Confirm `data/derived/clob_rtt.json` or the live `/time` probe resolves (deskew and paper actions
   refuse at 0 ms).
3. Confirm secrets live only in `/etc/polymarketbot/polymarketbot.env`, mode 0600; `deploy.sh` now
   aborts if they leak into the app tree.
4. Run the canary as a foreground command, not under the systemd paper unit:
   `uv run python -m polymarketbot.live.runner --live --yes --condition-id … --token-yes … --token-no … --min-size 5 --quote-size 5 --daily-reward 0 --max-budget 20 --duration 3600 --record-live-data`.
5. Watch: `Rebate reconciliation:` (L1), `micro_risk_decision` rows (L6), `reward_epochs.actual` vs
   `modelled` (L2), locked-cash calibration log (L4), and `Tier 2 landing:` lines (L5).
6. Stop on: exit code 3, repeated `CANCELLING`, reconciliation failures, or any `GasUnmeasured`.
7. The venue balance, not `realized_pnl`, is the money answer.

## Tests run

```
uv run pytest tests/test_deskew.py tests/test_live_runner_gating.py tests/test_paper_core.py
   tests/test_live_accounting.py tests/test_paper_engine.py tests/test_paper_pillars.py
   tests/test_reward_epoch_and_rebates.py tests/test_real_money_engine.py
   tests/test_chain_settlement.py tests/test_settlement_routing.py -q  -> 268 passed
uv run ruff check <every touched file>            -> clean
bash -n (LF conversion of) deploy/deploy.sh       -> clean
```
