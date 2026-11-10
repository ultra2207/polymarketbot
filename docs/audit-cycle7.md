# Audit cycle 7 — measured findings and fixes (2026-11-10, local PC)

Three 30-minute paper soaks on the canonical command, then three pillar auditors
on the window, each implementing its own fix. All numbers below are measured on
this host unless labelled otherwise.

## Windows

| # | Window (IST) | Universe walked | Snapshots | Fills | Reward equity |
|---|---|---|---:|---:|---:|
| 1 | 02:12–02:46 | 2,000 → 117,918 (never complete) | 71 | 0 | $1,000.00 |
| 2 | 03:13–03:44 | 189,076, `pass_complete=True` | ~110 | 0 | $1,004.97 |
| 3 | 04:22–04:52 | 189,142, `pass_complete=True` | 326 | **2** | $1,006.64 |
| 4 | 05:14–05:45 | 188,928, `pass_complete=True` | 331 | 0 (fabricated fills removed) | $1,011.82 |

Window 3 is the first with executions: 2 reward fills, one lot of 40.77 shares
at 0.2899 marked 0.258 (a ~$1.30 adverse move), equity +$6.64 of which +$6.64 is
accrued reward revenue. Settlement and CTF still produced no fills.

Window 4 (post-cycle-2 fixes) is the clean baseline: those two fills were proved
fabricated and the corrected price rule books none; equity +$11.82 is entirely
measured reward accrual. CtfMinter quotes zero legs because the horizon-matched
fill probability makes the maker pair negative EV — the correct output, not a
failure. Settlement finds no eligible market (0 of 27 sampled proposed/disputed
markets had a fee-aware +EV ask). Tick 4.47–4.59 s with the full universe.

## Why fills were exactly zero (measured)

1. **The global trade tape is CDN-cached.** `GET /v2/trades?limit=500` returns
   `Cache-Control: public, max-age=300`; six calls 8 s apart returned pages whose
   newest print aged 129 → 170 s. `_apply_resting_fills` only accepts prints with
   `p.ts >= quote.placed` (placement + RTT), so the gate was unsatisfiable. Fixed
   with `venue.condition_tape` (`/v2/trades?condition=<cid>`), a 45 s TTL cache,
   a 48-request/tick budget, and a per-quote `seen_ts` watermark.
2. **The reward pillar's revenue was never credited.** Equity was flat by
   construction: only maker rebates on fills existed. Fixed by accruing
   `daily_payout × dt/86400` per resting quote.
3. **Sub-lot flow was discarded.** Measured prints are 1–5 shares against a
   20–200-share minimum. Now accumulated per quote until it can clear.
4. **Reward allocator carried invented constants**: `spread_offset_cents=1.50`,
   `floor_target=1.35`, a silent `1.15` retry ladder, a $200-vs-$180 lot unit
   bug, a `d <= 0` filter that discarded the best markets, and a "water-filling"
   that put the whole bankroll in one market. Replaced with the exact KKT
   equalisation `dR/dc = PαD/(D+αc)²`, measured offsets, and a logged dominance
   prune (486 → 26 candidates, shadow yield floor logged).
5. **CTF maker edge was fabricated**: `bid_yes + bid_no < 1` fired on 99.94 % of
   20,630 recorded snapshots while realised taker parity was 2/20,630 (0.0097 %).
   Replaced with an expected-value gate using the fitted fill model
   (`data/derived/fill_model.json`, n=6,817) and measured unwind cost; it fires
   on 24.4 % of the same tape.

## Settlement

Verdict coverage was 0 of 117,918 in window 1: all 9 overlay verdicts belonged
to `closed=true` markets the walker excludes. The hazard prior was also invented
(α=1, β=1000 → p_win 0.9970); it is now seeded from the UMA event tape
(235 proposals, 0 disputes → Clopper-Pearson upper-95 1.2614 %, p_win 0.9874).
Settlement now sources candidates from the verdict index
(`live_verdict_condition_ids`), binds proposals to walked condition ids, prices
verdict tokens first in every book window, reserves minimum notional instead of
zeroing the budget after one order, and writes losing legs off at zero through a
new `Ledger.write_off`.

## Engine throughput

| Stage | Before | After |
|---|---:|---:|
| tick | 14–46 s | **3.85 s** (measured smoke) |
| books | 8.6 s | 1.5 s |
| tape | 29.7 s (429 storm) | 0.4–2.3 s |
| settlement | 9.5 s | 0.97 s |
| ctf | 12 s | 0.40 s |

- The keyset universe walk is sequential by construction; it now runs in a
  background thread (`UniverseWalker.run_forever`) and completed the full
  189,076-market universe inside one soak for the first time.
- Settlements no longer call the hazard model on ~72 k priced markets; the
  verdict index is the radar.
- CTF's EV gate runs on a rotating 1,500-market slice of 12,862 priced markets
  (full sweep every 9 ticks), logged as a measured compute budget.
- Resting bids now reserve cash (`_reserved`), so the engine cannot keep funding
  new quotes from the same dollars.


## Cycle 2 (auditors on the window-3 evidence)

**Reward — the two fills were fabricated.** Both booked sizes equalled prints that
cleared 2.3 c and 0.1 c *below* our bid. `level_shares` was read at
`best_bid + tick`, a level empty in 817/817 candidates, so queue share was 1.0 and
every SELL print at `price <= quote` counted. The fill now requires the print to be
at the quote's own price (`|p − q| ≤ tick/2`). Replayed: 40.77 booked shares
become 0.
- Population adverse selection measured on 8,764 SELL prints with a follow-on
  SELL: mean markout **−0.209 c/share (−0.585 % of notional)**, p10 −1.0 c.
- Allocator phantom revenue: 51 % of candidates price outside the venue's
  two-sided band while a one-sided quote is scored as if two-sided; on the funded
  set, 21.7 % of capital claimed 24.4 % of revenue the venue never pays.
- Engine churn fixed: `allocate` was given `cash − reserved`, its own output, and
  oscillated with period 2 (27 quotes → 5 → 26 → 6 …). It now receives the real
  cash balance, and accrual scales with `shares / initial_shares`.

**CTF — zero quotes is the measured-correct answer.** A new tool
(`polymarketbot.tools.ctf_quote_fill_horizon`) measured, on 8.77 M quote
placements from the L2 tape, P(fill) = **0.000194** per 5 s tick (95 % CI
0.000185–0.000204) and 0.006421 at 225 s. The two-sided maker pair is negative EV
at every measured horizon; the previous gate was ~2,000× too optimistic at one
tick. The gate now consumes that measured horizon, and the engine holds a quote
across the rotating scan slice instead of cancelling it one tick after placement.
Fabricated fee fallbacks (`rate=0.02, exponent=1.0`) and the hand-set
`min_net_edge_usd=0.0015` are gone.

**Settlement — the funding path works; two gates lied.**
- `final_payout` accepted any `closed` market with a 1/0 vector, so *proposed* and
  *disputed* markets (measured: 472 + 2 + 2 of 12,000 closed markets) were marked
  at par (`p_win = 1.0`) instead of the measured 0.9874. Now only
  `{"", "resolved", "settled"}` qualify.
- A real-fixture test (production UMA tape + fair-value model) funds 54 shares at
  0.983 in `_land`, equity $1,000.20; a second fixture shows the window's actual
  book is refused.
- The engine screen is now fee-aware (`settlement.marginal_edge`) and `set_par`
  writes the verdict's own `p_win` instead of 1.0. Fee-free markets
  (`feesEnabled=false`, 4 of 27 sampled) parse as a zero schedule instead of
  "unknown fee".
- Eligibility in the live universe: 27 proposed/disputed markets sampled, 14
  bid-only, 1 with an ask at 0.9880 → edge −$0.0011/sh. **0 funded, correctly.**

## Open items (not fixed, evidence recorded)

1. **Fills are still 0 in the reward pillar** in low-flow markets: measured sell
   flow at/below our bids was 0 for all sampled quotes, and 6 of 8 quoted markets
   had no trade for 185 s–10 days. The pillar quotes where rewards are paid, not
   where flow is; whether that is correct depends on the reward score model, not
   on the fill model.
2. **Fill-model horizon mismatch — resolved for CTF** by the measured
   `ctf_quote_fill_horizon.json` (0.000194/tick). The reward pillar still uses a
   print-conditional queue share with no markout term; measured mean markout is
   −0.585 % of notional per fill.
3. **No open verdict market existed** in any window, so the settlement radar is
   wired but still unexercised end to end.
4. `rewards/scanner.py` (the non-paper runner path) still scores our quote as
   two-sided at a hardcoded 0.5 c offset — the same 3× defect fixed in the paper
   allocator.
5. UMA tape has 235 `ProposePrice` and 38 `RequestPrice` rows but **0**
   `DisputePrice`; the collector must emit it or the dispute posterior can only
   widen, never observe a dispute.
6. `QueuePriorityValuationModel` still defaults five economic inputs and the
   governor still hardcodes `hurdle_pct=0.15`, `min_dwell_time_seconds=300`,
   `re_solve_interval_seconds=30`.
7. Paper books are `connect-time` snapshots on the recorded tape (4,261/hour), so
   leading indicators measured there are sparse.
8. Resting reward quotes can reserve slightly more than cash (measured 1,196 on a
   $1,012 balance) because each tick re-allocates from the full balance; a
   simultaneous fill set would have `Ledger.buy` refuse the overflow. The
   exposure cap belongs in the allocator, not the engine.
