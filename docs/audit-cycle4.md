# Paper-soak audit — cycle 4 (post-remediation)

- Window: 2026-11-09 07:37:39 → 08:07:39 UTC (13:07:39 → 13:37:39 IST)
- Command: `uv run python scripts/run_live_paper_soak.py --capital 3000 --duration 1800 --interval 10 --fresh`
- Final observed snapshot: uptime 1767 s, 161 snapshots, 722 trades.
- This is the first full cycle after the cycle-1 remediation. The cycle-1 run (+$311.97,
  +$264 CTF, 4,318 fills) is not comparable: its maker fills were off-tick, sub-minimum,
  atomic, and could not lose.

## 1. Measured result

| Pillar | Net PnL | Fills | Fees | Rebates | Notes |
|---|---:|---:|---:|---:|---|
| RewardHarvester (`pnl_domain2`) | **+$3.47** | **0** | 0 | 0 | pure model accrual, no orders |
| SettlementHarvester (`pnl_settlement`) | **+$42.04** | 8 buys, 2 CLOB exits | $3.05 | 0 | mostly unrealized par edge |
| CtfMinter (`pnl_ctf_splitter`) | **−$8.10** | 648 broken + 64 merged | $16.49 | $5.27 | now capable of losing |
| **Total** | **+$37.36** | | | | settlement dominates |

Cycle-1 comparison: CTF +$264.47 → −$8.10; total +$311.97 → +$37.36. The reduction is the
intended effect of removing the fake fill/accounting paths, not a performance regression.

## 2. Fix verification

- **CTF tick/min size:** sampled cycle-4 markets returned Gamma
  `orderPriceMinTickSize` 0.001 and `orderMinSize` 5.0; recorded prices (`0.985`, `0.986`,
  `0.993`, `0.99`) are exact tick multiples and sizes are >= 5. The cycle-1 99.6 % off-grid
  and 64 % sub-minimum populations are gone.
- **CTF broken legs:** 648 `CTF_BROKEN_LEG` rows exist; a 10.3-share YES fill at $0.651
  unwound at the $0.650 best bid with a $0.0951 taker fee. The pillar can lose.
- **CTF edge hurdle:** fills are concentrated at 0.98–0.993; the 63 bps measured markout
  filter is binding.
- **Settlement fee schedule:** a 189.47-share buy at $0.95 recorded $0.62999; Gamma
  `feeSchedule {rate: 0.07, exponent: 1}` gives exactly `189.47 * 0.07 * 0.95 * 0.05`.
- **Settlement band:** all 8 entries are 0.945–0.992, inside 0.940–0.995. The prior
  `$0.2304` entry class is gone.
- **Settlement resolution gate:** the two 3AM markets were `closed=False`,
  `umaResolutionStatus=None` at 0.915/0.985 and were **not** force-settled on the
  indicative 0.99 print. Two honest `CLOB_EXIT` early harvests occurred at $0.999.
- **Reward universe:** radar logs 2,390–2,401 primed pools (cycle 1: ~330 from one page).
- **Logging:** 0 `print()` in `src/` and `scripts/`; per-source logs + `app.log`; the soak
  process ran with 8 threads (cycle 1 pre-fix: 82).

## 3. Remaining findings and fixes applied for cycle 5

The three cycle-4 audits found, in addition to the verified fixes:

- **Reward:** no order is ever placed (F1); three incompatible score conventions
  (`compute_minimum_score` vs `normalise_two_sided` vs the soak's 2S/3 numerator and
  Q1+Q2 denominator, F2); `mu(0)` ≈ 1 $/$/day with no plausibility ceiling (F3); 0.5 c
  sub-tick offset (F4); continuous `dt` instead of 1-minute epochs (F5); 68–97 s stale
  depth and fall-through on refresh failure (F6); `dt` catch-up after idle (F7);
  non-200 page responses silently treated as end-of-stream (F8); allocation only at DEBUG
  (F9); radar starves telemetry (max 74 s gap, F10); `max_active_pools=4` /
  `search_candidates=12` (F11); $0.50 hurdle (F12).
- **Settlement:** mark-to-par is reported as PnL ($43.22 of +$37.19 unrealized, F1);
  zero throughput and infinite deferral for missing markets (F2); the crypto filter only
  matched "up or down", not threshold candles (F3); `cand` loop-variable leak mispriced
  fees (F4); `--settle-hold-sec` dead (F5); token-index mapping unvalidated (F6); stale
  gate fail-open (F7); duplicate/per-position book fetches (F8); static slots/caps (F9);
  ARR ranking fee-blind (A1); `to_settlement_candidate` bypassed (A2); batch book helper
  unused (A3).
- **CTF:** the 63 bps screen is fee-blind and the whole path is −EV (F1); unwind only
  charged one tick (F2); fill probability/size are guesses with 40 % mass at the minimum
  size (F3); no bid-depth walk (F4); top-50/$15–$60 caps (F5); `usable_cash` never debited
  (F6); a 4-minute telemetry/scan stall (F7); empty universe refresh clobbers the cache
  (F8); `min_net_edge_usd=0.0015` is dead on the maker path (F9); hardcoded parameters
  (F10); hot-path 2,240-book scans (F11); swallowed errors (F12).

Fixes applied before cycle 5:

- Settlement PnL is now **realized** (`available + locked − start`); mark-to-par remains
  in equity and the dashboard's par-edge field. Cycle-5 smoke showed settlement
  PnL −$2.95 with +$50.46 par edge, not a fake +$47.
- Settlement threshold candles are filtered (`above`/`below`/`dip to`/`reach` with an
  AM/PM time), not just "up or down"; short-dated markets still require closed/proposed.
- Settlement `SettlementCandidate` now carries its Gamma `fee_schedule`; the allocation
  loop maps `alloc.condition_id → candidate` instead of using the leaked `cand`.
- Settlement position books are fetched in one batch per loop, not one GET per position.
- Settlement token-index mapping is validated (`len(prices) == len(tokens)`, outcomes
  length), and an unparseable stale date now fails closed.
- Settlement batch-poll failures are WARNING (rate-limited); missing-market positions
  warn once after 2× horizon / 6 h.
- Reward allocation is INFO with pool ids and amounts; empty/no-eligible/refresh-failure
  paths reset the accrual clock and do not accrue stale state; non-200 page responses are
  WARNING/None; the phantom 1.0 competitor fallback is gone; score offset is 1 c.
- CTF has a probability- and fee-weighted EV gate, a 63 bps markout on the broken-leg
  unwind, per-leg fees, a dollar-volume intensity fix, `usable_cash` debited per fill,
  and the previous universe retained on an empty/failed refresh.

Cycle 5 (started 2026-11-09 08:21:00 UTC) is the first run with all of the above.
