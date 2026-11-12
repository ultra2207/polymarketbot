# Paper trader audit — cycle 5

Window: **2026-11-09 08:21:03 → 08:51:06 UTC** (13:51:03 → 14:21:06 IST).
Duration: 1,802.97 s. 180 snapshots, 165 trades. 0 WARNING / ERROR / CRITICAL in the
paper trader log. This is the first run with the cycle-4 remediation, before the cycle-6 dynamic
optimizer rewrite.

## Result

| Pillar | Ending equity | Realized PnL | Note |
|---|---:|---:|---|
| RewardHarvester | $1,004.48 | **$0.00** | $4.48 is model accrual; no resting order (now labelled `model_only`) |
| SettlementHarvester | $1,047.51 | **−$2.95** | +$50.46 par edge held separately; PnL is cash, not mark-to-par |
| CtfMinter | $1,002.08 | **+$2.08** | 165 total trades; EV gate reduced the negative flow |
| **Total** | **$3,054.06** | **+$3.60** | |

## What the run proves

- Settlement mark-to-par is no longer booked as realized PnL. The dashboard shows
  −$2.95 cash PnL alongside the +$50.46 par edge; the old run showed +$47 of fake PnL.
- The threshold-candle filter stopped the 20,000 % ARR queue: the top ARR is ~2,276 % on a
  proposed market, not a 5-minute stale candle.
- CTF can produce a losing run; the fee/markout EV gate is not a one-way PnL printer.
- The log is clean; no unhandled exceptions.

## Remaining defects addressed in cycle 6

Cycle 5 still contains the fixed caps and hand filters: reward `max_active_pools=4`,
`search_candidates=12`, `$0.50` switching hurdle, fixed refresh multiples; settlement
`target_settle_slots=6`, `max_market_pct=0.18`, dead `--settle-hold-sec`; CTF `$15/$60`
cap, censored fill-size draw, dead `min_net_edge_usd` on the maker path, top-50 scan.
Cycle 6 replaces these with measured thresholds and optimizer decisions.
