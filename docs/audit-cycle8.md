# Audit cycle 3 (window 5) — findings and fixes

Window: `2026-11-10 10:10:23 → 10:40:28 IST`, telemetry ts `1794285638–1794287428`,
114 snapshot rows per pillar, fresh DB. Host: CLOB RTT p50 **570 ms** (1.7× the
344 ms of the 04:14 window). Universe completed at 185,146 active markets
(11,017 rewarded) on minute 27 of 30; tick 4.61–5.19 s.

| Pillar | Equity | Fills | Notes |
|---|---|---|---|
| RewardHarvester | 1000.00 → 1011.93 | 0 | pure accrual; `reserved=1445.65` on `cash=1000.51` |
| SettlementHarvester | 1000.00 | 0 | verdict tape bound 2 → 1 proposals; 0 eligible |
| CtfMinter | 1000.00 | 0 | refused: 244.4 s requote gap, fill table ended at 225 s |

## Shared-infrastructure defects (fixed here, orchestrator-owned files)

1. **Rate limits are per credential, not per host.** One Alchemy key serves
   `eth-mainnet` and `polygon-mainnet`; cooling only the URL that answered
   rotated to its sibling on the same quota and climbed a second ladder —
   measured `56s → 142s → 921s` in one window. `chain.rate_limit_key()` now
   groups endpoints by credential; `mark_rate_limited` rests the whole group and
   `mark_ok` resets the shared ladder. Regression test:
   `test_rate_limit_cools_every_endpoint_sharing_one_credential`.
2. **Block interval was inverted.** `_make_uma_poller` computed
   `(second − first) / elapsed` = blocks per second and used it as seconds per
   block, logging `0.79s` while the 407-header tape measured `1.501s`. Every
   proposal age and liveness window derived from it was ~1.9× wrong. Now the
   tape measurement is primary and the live fallback divides correctly.
   Regression test: `test_block_interval_is_seconds_per_block_from_the_tape`.
3. **CLOB RTT was sampled once.** `main.py` measured p50 at startup and the
   engine reused it for 30 minutes. The probe is now refreshed every 60 s inside
   the tick (own `rtt` stage) so the landing model follows the path.
   Test: `test_rtt_probe_is_refreshed_on_its_cadence_not_only_at_startup`.
4. **Gas price was sampled once** (42.70 Gwei at 05:14 vs 46.33 at 10:10). The
   engine re-prices on the same cadence via `gas_probe`; a failed refresh keeps
   the last measured value and never substitutes zero.
   Test: `test_failed_gas_refresh_keeps_the_last_measured_cost`.
5. **`ctf_merge` flew on the CLOB round trip.** A merge lands after the Polygon
   RPC round trip plus one block, so the ledger credited par 570 ms after
   submission even during a 921 s RPC cooldown. `ActionBook.submit` now accepts a
   measured `rtt`; both merge sites resolve `ctf.resolve_on_chain_timing` and
   refuse when the head, RPC RTT or block interval is unmeasured. Live proof:
   `flight 1.93s (rpc 0.42s + block 1.50s)`.
6. **`RttProbe` overwrote the latency report.** It wrote its CLOB p50 into
   `data/derived/local_latency.json`, the file `load_persisted_rpc_rtt_s` reads
   for the RPC round trip, which is why no measured RPC RTT ever existed. The
   probe now persists to `clob_rtt.json` and only reads the report as a legacy
   fallback. Report regenerated: `polygon_rpc` p50 **424–464 ms** (two runs).

## Reward (auditor + wiring)

- The allocator was handed the full balance each tick while quotes from earlier
  ticks still rested: `sum(resting) / cash` reached **+44.5%**. `plan_quotes`
  commits the optimum in payout-per-dollar order against net cash; the engine is
  wired to it. Before/after on the same input: t2 1503.94 → 605.28, t3 1920.38 →
  817.08 on $1,000 cash.
- The invariant costs no payout at a tick boundary (the MILP already caps its own
  plan); it removes an overhang that had no funding behind it.
- Four hardcoded literals removed/re-derived: `spread_cents=0.5` → measured
  distance (`tick/2` as the restable upper bound), `mid_price=0.50` → measured
  token price, `gas_fee_per_merge == 0.015` sentinel → unmeasured gas refuses the
  market (`-inf`), `bankroll*0.35` ceiling → `sum(h_i)` (simultaneous-fill
  notional). The 75/25 buffer at `scanner.py:863` → `_pair_cost_usd` refusal.
- One-sided scoring in `rewards/scanner.py` now applies the 1/c fraction inside
  the [0.10, 0.90] band; same-capital one-sided scores 2/3 of two-sided.
- Markout: no booking term. `_mark` re-values every lot at the live bid, so a
  booking term would double count; the *ex-ante* term needs a sample larger than
  the 2 fills available (mean −0.209 c/share), and that is named as the gap.

## CTF (auditor)

- **Staleness does not decay the opportunity, it decays queue position.**
  4,242 binary pairs, 1.95 M pair-observations on a 5 s grid: 95.0 % of
  under-par pairs are still under par 225 s later, but only 79.5 % of quotes
  priced from a 225 s-old book are still at or above the best bid (vs 97.5 % at
  one tick).
- **The fill model binds by 4–5 orders of magnitude.** Break-even needs
  `edge > 2(1−p)/p · unwind`: 10,300× the unwind at a 1-tick scan and 310× at a
  48-tick hold, against a measured locked edge of 2–4× the unwind. Cadence
  improves the loss rate by 1.37×, so the 52-tick budget is not the constraint.
- **Window 5's zero quotes were a data gap, not an EV verdict.** The operating
  gap is 52 × 4.70 s = 244.4 s, beyond the table's 225 s support, so `p_fill`
  returned `None` for every candidate. Now logged as missing data. The table is
  being re-measured at 244/300 s.

## Tooling defect

`ctf_quote_fill_horizon` crashed on a 4-horizon re-run:
`pyarrow.lib.ArrowInvalid: offset overflow while concatenating arrays` — it
concatenated a whole hour's `asset_id` string chunks before reducing. The series
is now built one partition file at a time.

## Window table

| # | Window (IST) | Universe | Tick | Fills | Reward equity |
|---|---|---|---:|---:|---:|
| 1 | 03:5x | 117,918 incomplete | 25 s | 0 | 1000.00 |
| 2 | 04:0x | 189,076 | 24 s | 0 | 1004.97 |
| 3 | 04:22–04:52 | 189,142 | 4.4 s | 2 (fabricated) | 1006.64 |
| 4 | 05:14–05:45 | 188,928 | 4.5 s | 0 | 1011.82 |
| 5 | 10:10–10:40 | 185,146 | 4.7 s | 0 | 1011.93 |

## Taker fee schedule — re-verified 2026-11-10 (user-reported change)

The screenshot provided is the **Polymarket US** schedule (`docs.polymarket.us/fees`),
not the venue this bot trades. Verified from primary sources:

| | global (`clob.polymarket.com`) | US (`polymarket.us`) |
|---|---|---|
| formula | `C × feeRate × p(1−p)` | `Θ × C × p(1−p)` |
| rate | per category (Crypto 0.07, Sports 0.05, Politics/Finance/Tech/Mentions 0.04, Econ/Culture/Weather/Other 0.05, Geopolitics 0, Combos 0.05) | per account tier by trailing 30-day taker volume: 0.095 / 0.085 / 0.075 / 0.0695 |
| rounding | 5 dp, half-up | cents, half-even |
| last change | **Jul 10 2026** (sports 0.03→0.05, rebate 25%→15%) — already in `configs/fees.yaml` | Nov 6 2026 13:00 ET (tiers); Nov 11 2026 13:00 ET combos 0.085→0.06 on `(1−p)^4` |

- Live Gamma confirms the global per-category table: `politics_fees → rate 0.04,
  exponent 1, rebateRate 0.25`, matching the published Politics row.
- Global changelog has no taker-fee change through Nov 2 2026, so the era table is
  current. Nothing to change economically for our venue.
- **Added** `venues:` to `configs/fees.yaml` and the US path in `fees.py`:
  `venue_for_host`, `us_taker_tiers`, `us_taker_coefficient` (refuses an
  unmeasured volume), `us_taker_fee`, `us_combo_taker_fee` (coefficient chosen by
  trade timestamp across the cutover). Pinned to the four documented worked
  examples: 1,000 @ 0.10 → $6.26, @ 0.65 → $15.81, @ 0.30 → $14.60, @ 0.90 → $6.26.
- The cent tie-break needed Decimal arithmetic: `0.0695 × 1000 × 0.30 × 0.70` is
  exactly 14.595, which banker's rounding sends to 14.60; in binary floating point
  it is 14.594999999999999 and silently rounds to 14.59.
- **Added** the global Taker Rebate ladder (`taker_rebate_rate`,
  `weighted_taker_volume`) with the published wV examples (312 and 109.25). It is
  not credited in PnL: 30-day weighted volume is unmeasured and tier 0 is exactly
  0 %, so booking nothing is correct today, and the function refuses an
  unmeasured volume rather than defaulting to a tier.

Tests: `tests/test_fees.py` 104 passed (7 new), fee/rebate/pillar suites 162 passed.

## Cycle-3 paper trading run (window 6) — first-hour defects

Window `11:14:55 → 11:44:55 IST`, integrated tree (all three auditor diffs + the
shared fixes). Verified working from the log within the first ten minutes:

- `Paper engine start: … rtt_refresh=60s`, `tick profile: rtt=0.21s` — the CLOB
  probe now re-samples instead of holding one startup value.
- `UMA live polling enabled: block interval 1.50s` — matches the 407-header tape
  measurement, no longer the inverted `0.79s`.
- `reward reserve: kept 10/12 quotes, $997.07 committed of $1,000.04 cash
  (2 unaffordable)` — the reserve invariant holds on the live allocator output.
- `verdict binding: 31 proposal(s) in, 5 bound (26 unbound), 14 live; reasons:
  aged_out=15, bound=5, unmatched_question=11` — the zero-fill cause is now
  stated per proposal rather than asserted.

Two defects the integrated run exposed:

1. **CTF was blind for every unmeasured gap.** The gate requires a horizon cell
   and the table holds 5/10/30/60/225/244/300 s, so the gaps this run actually
   produced (1.0 s, 8.7 s, 14.8 s, 21.3 s) all refused — 2 warnings per market
   per tick. Fix: linear-in-time interpolation between adjacent measured cells.
   Validated by withholding each measured cell and predicting it from its
   neighbours: errors 3.39 %, 2.92 %, 5.31 %, 0.30 %, 0.33 % (worst 5.31 % on
   8.77 M placements); log interpolation is 5–30 % wrong, so it is not used.
   Below the smallest cell or above the largest, refusal stands (`p_fill(1.0s)`
   and `p_fill(400s)` are still `None`). `P_FILL_INTERPOLATION_ERROR` carries the
   bound. Test: `test_horizon_fill_probability_interpolates_only_between_measured_cells`.
2. **Verdict-binding audit logged 12 lines per tick** — 2,160 lines for one run.
   It now logs the summary only when the counts change and each unbound proposal
   once per process, so the state change is visible instead of buried.

## Cycle-4 window (7) — 11:48:52 → 12:19:04 IST

| Pillar | Equity | Fills | Notes |
|---|---|---|---|
| RewardHarvester | 1000.00 → 1009.09 | 0 | `reward reserve: kept 50/51 quotes, $977.73 committed of $1,007.82 cash` |
| SettlementHarvester | 1000.00 | 0 | `5 bound (263 unbound), 16 live; aged_out=250, unmatched_question=13` |
| CtfMinter | 1000.00 | 0 | **100–120 maker quotes resting** (was 0), no fills |

Universe 185,792 complete at minute 28. RTT re-probed 322 → 501 → 321 ms; gas
26–27 Gwei; block interval 1.50 s; tick 8.16–11.66 s against a 10 s interval.

Three defects this window exposed, and what happened to each:

1. **The probes I added were inside the tick.** `rtt=0.22s gas=0.90s` per probe
   tick is pure network time paid on the critical path. Moved to a `venue-probes`
   daemon thread (`_refresh_venue_state` / `_probe_loop`), so the tick never pays
   them; a failed gas probe still keeps the last measured cost. Tests rewritten
   to call the refresh step and to assert a tick issues no probe.
2. **CTF went from 0 to 100–120 resting quotes** once interpolation supplied a
   `p_fill` for gaps between measured cells. That is the gate working as
   designed, but with **0 fills** the EV it prices is unverified: the paper fill
   path accumulates crossing prints until `min_order`, which is a different
   quantity from the measured P(fill). Handed to the CTF auditor with a
   reconciliation table as the deliverable.
3. **`reserved=` in the dashboard summed two pillars' notionals** against
   nothing, so `reserved=1994.20` on $1,000 balances read as a 2× breach. Each
   pillar was individually inside its own cash (`_ctf` sizes against
   `cash("CtfMinter") - reserved(maker_quotes)`). The line now prints
   `reserved=reward 977.73/1007.82 ctf 366.00/1000.00`.

## Cycle 4 — window 7 audit results

### Reward: the gate priced an unfillable price
`reward_candidates` priced every bid at `best_bid + tick`. On a **one-tick book**
that equals the ask, i.e. a marketable price that cannot rest and can never be
hit by a later SELL print; 40 % of candidates sat on one-tick books and they were
the highest-scoring ones (closest to mid). Re-measured on the same code path
(40 live candidates, 4,229 SELL prints):

| bucket | prints | of SELL | expected per 30-min window |
|---|---|---|---|
| at the price, old rule (`bid+tick`) | 166 | 3.9 % | 0.53 |
| at the price, after fix (`min(bid+tick, ask−tick)`) | 279 | 6.6 % | **1.14** |
| one-tick books (16/40 candidates) | 87 → 176 | | 0.22 → 0.81 |

So zero fills over four windows is consistent with the fix's 1.14 expectation,
and the old rule halved the opportunity. Accumulation is not binding: mean print
at the resting price is 305.7 shares and 26/40 markets have a single print that
clears `rewardsMinSize` alone. `min_order` is correctly a share count
(`orderMinSize=5`, `rewardsMinSize=50/100/200` measured live) but it is the
*reward-qualifying* lot, not the venue order minimum — the engine's comment said
otherwise and is wrong.

Quote churn measured: 1.4 cancels / 1.7 placements per tick, 9.6 % turnover,
forgone accrual **$0.008–0.012 over the window (0.09–0.13 % of the $9.09
earned)**, so churn is not the zero-fill cause and no hysteresis constant was
added. Markout stays unmeasured: the tape gives a sample (258 prints, 246
markable) but the next print is a median **1,710 s** away, and the estimate flips
sign (+0.311 c ± 0.133, t=2.33) against the n=2 −0.209 c. Missing input: a
persisted best-bid series at a fixed offset from each fill.

### Settlement: the join was blind, the markets were closed
Fixture join implemented (membership/initial matching, orientation-insensitive,
ambiguity refused by construction). Validated over the window's whole live set
(268 proposals): **precision 4/4, 0 false positives in 268**, built in 254 ms for
185,792 questions. All four sports markets resolve to real condition ids and are
then refused as `no_open_fixture` — they were `acceptingOrders=false` with
proposals landing **166–203 min after `endDate`**. Game markets are structurally
epi-proposal unpricable; the join's money is in series/season/futures markets
whose `endDate` outlives the verdict.

`aged_out=250` is not a deadline bug: the tape's newest `ProposePrice` is
**2026-11-06 10:47**, 97 h before the window, so every tape row is 89 h past its
liveness. The pillar runs on the live poller overlay instead. Root cause found:
the `data-collector` service exited **127** and its last log line is
2026-11-06 17:40; `data/tape/clob_trades` ends the same day.

### CTF: the gate now prices the event the execution model can realise
The tool emits `p_fill_exact` (a print **at** the quote within half a tick)
alongside the historical crossing count, and the gate reads the exact value.
Measured exact/crossing ratios are **3.62 %** at (225 s, tick 0.001) and **8.07 %**
at (225 s, tick 0.01) — not one constant. Break-even locked spread
`L* = 2·tick·(1−p)/p` from the measured exact cells: **21.15–2 506** at tick 0.01
and **5.51–626** at tick 0.001, against a maximum of **1.00** a binary complete
set can pay. No two-sealed market can fund a one-tick unwind on both legs at any
measured horizon, so zero maker quotes is the honest output of an honest price.

Guards added: quotes expire at `placed + priced_horizon_s`, an over-long requote
gap is clamped to the longest measured horizon (a lower bound, one-directional),
and tests pin that the loaded model equals the artifact's `p_fill_exact` per cell
and that `p_fill_exact <= p_fill` everywhere.

`P_FILL_INTERPOLATION_ERROR` corrected from **5.31 %** (measured on the looser
crossing series) to **25.71 %**, the measured worst held-out error of the exact
series over 48 withholds; the cells the gate prices most stay inside 7.9 %.

### Engine (orchestrator)
- RTT and gas probes moved off the tick to a `venue-probes` thread (they cost
  0.22 s + 0.90 s on the critical path and pushed the tick past its interval).
- `_settlement` now logs `settlement candidates: N verdict cid(s), M +EV,
  cash $X | refused: reason=count best_ask=...` every 60 s, so a zero-fill window
  states *why* instead of logging nothing.
- Dashboard reserve line prints each pillar against its own balance.

## Cycle 5 - window 8 (12:53:24-13:23:31 IST), defect rate and the continuation verdict

### The window itself

| Pillar | Equity | Fills | Resting | Reserved |
|---|---|---|---|---|
| RewardHarvester | 1000.00 -> 1009.07 | 0 | 38 | $963.34 |
| SettlementHarvester | 1000.00 | 0 | 0 | 0 |
| CtfMinter | 1000.00 | 0 | 106 | $997.38 |

Full 30 minutes, 180 tick snapshots, tick profile 4.85-7.19 s, no crash, no
refusal storms. All three cycle-4 integrations are visible and working:
`settlement candidates: N verdict cid(s), M +EV, cash $X | refused: reason=count`,
`reward reserve: kept k/n quotes, $C committed of $B cash (u unaffordable;
r already resting, w new)`, and per-pillar `reserved=reward R/B ctf R/B`.

### The defect that matters: reward PnL is an unmeasured model

RewardHarvester's entire +$9.07 is `reward_accrual`, and the accrual is not a
fill. Ledger, this window only:

```
select detail, count(*), round(sum(cash_delta),4) from trades
where ts between 1794295404 and 1794297212 group by 1
  reward_accrual  1221   +9.13
  reward_fill        2  -11.82
```

`lots=0 fills=0 pos=$0.00` all window: no position was ever markable, so the
09:07 gain is a projected share of a pool, credited straight into `cash` and
reported as equity. $9.13 / 30 min = **$438/day on ~$900 of capital**.

The venue's own budget, measured by the collector in the same minute:
`universe refreshed: 11197 markets, pool=$120,434/day` - $10.76/day per market
on average. The model therefore asserts **0.36 % of Polymarket's entire
liquidity-reward program** with $900 of capital.

That claim rests on `evaluate_market_payout(cap, P, D, alpha) = P*user/(D+user)`
with `user = alpha*capital` and `D = competing_score`. And `D` is invented:

```
rewards/scanner.py:504   competing_depth = max(1000.0, rate * 40.0)
rewards/scanner.py:168   competing_depth: float = 3000.0
rewards/scanner.py:242   competing_depth: float = 3000.0
rewards/scanner.py:326   comp_side_depth = max(100.0, competing_depth * 0.5)
```

An x40 multiplier and a 1 000 floor set the denominator of every pool share,
so the payout cannot be validated or refuted by any measurement - which is
precisely what AGENTS forbids ("a value must be fetched dynamically, measured,
or derived; anything else - a cap, multiplier, threshold, reserve, or fallback
- is a bug"). Two of the four instances are dataclass defaults, so a caller
that omits the argument silently gets a 3 000-share competitor.

There is also no cross-check available: `reward fill tape: 1110 evaluations,
1 with flow, 0 prints seen, 0 crossing` - the flow tape ends **2026-11-06 13:01**,
four days before the window. The collector was restarted during this cycle; it
reported `universe refreshed` and began compacting, so future windows have
current flow.

### CTF: the recorded verdict is not enforced where quotes are made

Cycle 4 concluded, from measured exact-price cells, that no two-sealed market
can fund a one-tick unwind on both legs (break-even locked spread 21.15 against
a 1.00 maximum), so zero quotes is the honest output. This window rested
**106 CTF orders reserving 99.7 % of the pillar's cash**, all day, zero fills.

The gate accepts on:

```
expected = p_yes*p_no*edge_if_both
         + p_yes*(1-p_no)*(rebate_yes - cost_yes)
         + p_no*(1-p_yes)*(rebate_no - cost_no)
accepted when expected > _EDGE_EPS,  _EDGE_EPS = 1e-12   # ctf.py:270
```

With exact-price p ~0.03 the two-leg term carries ~1e-3 of the lock, so an
accepted quote is accepted on the **rebate of a one-leg fill**, while
`_unwind_cost` (ctf.py:640) is `quote - best_bid`, one tick. The hurdle is a
trillionth of a dollar, not a measured markout: the loop is not honouring the
verdict recorded two sections above. This is handed to the CTF auditor with the
requirement that every accepted quote log its six EV components.

### Settlement: all refusals are now named, none is yet proven correct

```
settlement candidates: 3-5 verdict cid(s), 0 +EV, cash $1000.00
  | refused: no_live_verdict=2 no_market_in_universe=3
verdict binding: 268 proposal(s) in, 5 bound (263 unbound), 16 live
  reasons: aged_out=250, bound=5, no_open_fixture=5, unmatched_question=8
```

The live unbound set is concrete and mostly resolvable: five near-identical
`WA-05 House seat` questions, a `Junior`/`Cucuta` pair, and four NHL/NFL
fixtures all failing `no_open_fixture`. `aged_out=250` is correct behaviour
(those proposals are older than the horizon). The pillar refuses for named
reasons instead of silently, which was the cycle-4 fix; whether each reason is
*correct* is this cycle's assignment.

### Defect rate and whether cycles 6-10 are justified

| Cycle | Window | Findings | Money-relevant |
|---|---|---|---|
| 1 | 189 k markets, 24 s ticks | 6 | 6 (fabricated reward fills, phantom band 51 %, quote oscillation, CTF fee fallback, settlement par bug, CTF hold across slices) |
| 2 | 4.4 s ticks | 5 | 3 (fills removed, fee-free parse, marginal screen) |
| 3 | 185 k markets, 4.7 s | 11 | 7 (per-key 429 for 921 s, block interval inverted 0.79 vs 1.50, RTT/gas never refreshed, reserve over-commit +44 %, 4 hardcoded literals) |
| 4 | 185 k markets | 6 | 6 (reward priced the ask, settlement join blind, CTF gate priced an unrealisable event, collector dead, dashboard double count) |
| 5 | 187 k markets, 4.9 s | 5 | 3 (fabricated competing depth, accrual booked as cash, CTF EV hurdle 1e-12) |

The rate is falling (11 -> 6 -> 5) but it has **not reached zero**, and its
character has moved: cycles 1-4 were crashes, fabrications and wrong units;
cycle 5 is unmeasured model inputs that produce a plausible-looking number.
That shift is more dangerous, not less - a fabricated fill at least has a tape
row behind it, whereas $438/day of reward accrual has only `rate*40`.

Verdict: **cycles 6-10 are justified**, with the priority order fixed by this
window - (1) measure the reward denominator or refuse to credit accrual,
(2) enforce a measured hurdle in the CTF gate, (3) close the settlement
fixture/question matchers, (4) keep the collector alive so flow exists to
cross-check all of the above.
