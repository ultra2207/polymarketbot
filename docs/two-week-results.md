# Two-Week Data Window: Results and Gate Status

Draft compiled 2026-11-05 (Day 0 of the 14-day window). Every number states the
window it came from; proxy values are labelled. This file is the working record
behind the Day 14 memo.

## 1. Collector status

The collector is running with 9 streams (CLOB, Kalshi, Polygon, Binance,
Deribit, news, whales, UMA, health) on the Wyrmwood stack against `data/tape`.

| Stream | Measured | Evidence |
|---|---|---|
| Polymarket CLOB | 1,000–1,240 rows / 300 s; 3,378 assets across 5 shards | `data/logs/data.collector.log`, `data.tape.health/stream=clob` |
| Timestamp coverage | min/max `ts` ≈ `recv_ts` on all frames | health `ts_skew_ns_max` ≈ 3 ms |
| Queue ledger | ~388 k rows / 6 h, 6 books, 29 asset-hours | `data/derived/l3_queue/` |
| Kalshi trades | ~35 MB/day for mapped tickers only | global feed ~104 prints/s, 273 written in a 26 s smoke |
| Polygon logs | backfill gap 0 after the fix; 2,267 + 590 logs recovered on restart | `data/logs/data.polygon.log` |
| UMA oracle | 60 s poll; first live poll recorded 1 `ProposePrice`; resumable anchor prevented a rewrite on restart | `data/logs/data.uma.log`, `data.tape/uma_events/` |
| Run metadata | one JSON per restart | `data/tape/runs/` |

Crashes during the window: 1 unhandled exit (`DBLiftApply` fixed to async
`await`), 2 CLOB shard drops (reconnects 94 and 33 during a 178 s window; drift
was present before the drops), 1 empty `CLOB-PRICE-REJECTED` series, 1 Kalshi
segment starting 8 s late, 1 pre-cache Polygon segment. All were handled by the
collector's resync; the gaps are visible in the raw tape.

## 2. Price history integrity

`scripts/rebuild_l3_queue.py` reconstructs per-asset books from `clob_events`
and writes one Parquet per asset-hour. Six books (29 asset-hours) were processed:

* Level rows: ~388 k; the book is monotone (bids ≤ 1, asks ≥ 0) in every file.
* Queue invariants hold: the 6 verify tests and the rebuild test pass.
* Two CLOB data faults surfaced: a nested `price_change` with a full `book`
  payload (previously counted 0 rows) and a missing `event_type` (new shares
  treated as cancellations). Both are fixed in the parser with tests.

## 3. Fees: the measured truth

`docs/fees.md` + the live SDK: the platform charges **taker fees on every market
by default**. Polymarket has **no maker rebate**; makers earn only the
liquidity-reward pool, and maker orders pay 0 fee. The final fee is in the Gamma
`feeType` field (7 schedules: `zero_fees`, `crypto_fees_v2`,
`crypto_fees_5pct`, `crypto_fees_10pct`, `fees_politics`, `fees_finance`,
`fees_geopolitics`). `scripts/check_fee_schedule.py` verifies the SDK rates
against the documented tables; the duplicated `fees` / `feeRateBps` fields in
CLOB payloads are consistent.

**Consequence the spec must carry:** a Gate 1 check needs the fee *per share*
for the specific market, not a global assumption. In the 2026-11-01 snapshot
only 175 of 589 traded conditions carry a `feeType`; 414 print `edge` n/a.
Fee coverage is the binding limitation of the scorecard.

## 4. Universe

The live universe after the 2026-11-05 09:28 full crawl is **2,502 markets
(2,302 reward-eligible + 200 sentinels), 5,022 assets, pool $132,792/day**.
The spec's 1,028 / $65,793 snapshot was 2026-11-01 and undercounts the current
campaign set; every Gate 2 denominator must use the live epoch data, not that
number. The cutoff `> $15/day` still holds for the reward set.

Sentinel markets (crypto up/down) are added regardless of cutoff, capped at 200
with a 1 h endDate staleness filter. The incremental poll adds new sentinels
every minute, so `_cap_sentinels()` now enforces the 200 total on the poll path
too (the crawl-only cap allowed the live set to drift to 215 in 4 minutes).

The Gamma crawl was the expensive part: **240 k open markets** (mostly
five-minute binaries that stay `closed=false` past their endDate), ~20 min,
2,400 pages at 2 pages/s. The rolling walk now uses the **CLOB reward-campaign
endpoint** instead: ordered by `rate_per_day`, it stops at the $15 cutoff —
3,000 rows / 6 pages / **~5 s** — and the Gamma crawl remains the fallback. The
endpoint returns campaigns and token ids directly, so the 20-min sweep is no
longer on the hourly path.

## 5. Fill model (measured, 29 asset-hours)

`scripts/calibrate_fill_model.py`, window 2026-11-04 01:00–05:59 UTC:

* 6,880 print events → **6,817 observations, 2,184 fills (base 32.0 %)**.
* P(fill) by pre-event queue ratio: 0.634 / 0.634 / 0.498 / 0.423 / 0.285 /
  0.232 / 0.094 from ratio 0 → 0.85–1. Ratio 1.0 (nothing behind) is 0/3,376
  fills — the separate "fill only by emptying the level" regime.
* By depth: 0.655 (<50) → 0.016 (≥5,000).
* Logistic: logit P = 3.277 − 1.142·ratio − 0.573·log1p(size), |z| > 14.
* The backtester's **0.98 % fill rate** is the probability for any resting quote
  episode, a different denominator from the print-conditional 32 %.

## 6. Market scorecard (last 2 UTC hours)

`scripts/score_markets.py`, window 2026-11-05 05:00–07:00 UTC: **2,511
conditions**, 21.6 M quote points, 4,422 trades, markout matched 88.6 %.

* **76 of 129 scored conditions pass Gate 1 — and all 76 are flagged
  `markout_low_sample` (n < 30)**. They are single-print artifacts, not
  measurements; the snapshot spread is not an edge.
* Fee coverage: 138/647 traded conditions on the 2026-11-01 snapshot; 832/834 after the 2026-11-05 live metadata refresh (the snapshot predates 509 of the traded markets).
  All rows are kept and a `fee_known` boolean column flags the gap.

## 7. Toxicity

`scripts/scan_toxicity.py`, 2 h window: 161 / 3,779 condition-buckets flagged
(4.3 %) — 46 via 30 s markout, 57 via 5 m, 97 via OFI. Markout matched 96.8 %
at 30 s. The flags are inputs to the quote gate; they are not a strategy result.

## 8.1 Gate 3: event-level lead (PASS)

`scripts/measure_lead_time.py` and `scripts/bench_cancel_path.py`. Window
2026-11-05 06:00–06:10 UTC, crypto sentinels (BTC 224, ETH 144, SOL 44 assets),
fresh 20-sample clock probe:

* Quote reaction p50 **52.7 ms**; placebo (unrelated Binance moves) **140.7 ms**
  — the real moves are faster than random, but the leg is weak.
* Taker reaction p50 **1,067.7 ms** (n=25); lead **1,015 ms**, bootstrap CI
  **749–1,532 ms**. The lead is a CLOB-venue difference, so the cross-venue
  clock cancels.
* Fresh clock bracket **339 ms** (the earlier 1,268 ms was four samples plus
  stale parts); our path upper **217.5 ms** (sign p99 0.012 ms + CLOB min RTT
  217.5 ms, no order sent); point margin **850 ms** (the verdict uses the sampling + clock lower bound) → **PASS**.

Caveats: Binance BBO is throttled to 100 ms; the taker sample is 25 fills; the
our-path bound uses one full RTT; Gate 3 is a latency test, not a PnL result.

## 8.2 UMA oracle stream

`src/polymarketbot/data/streams/uma.py` polls the Polymarket UMA adapters and
the Optimistic Oracle V2 (`eth_getLogs`, 10-block windows, resumable anchor) and
writes `uma_events`: request/propose/dispute/settle, requester/proposer/
disputer, identifier, request timestamp, ancillary data, bond/reward, block and
tx. It cannot recover a Polymarket `condition_id` from the OO log alone; rows
map to the universe only when the ancillary `q: title:` matches exactly, and
most keep null ids (never guessed). Seven tests pass; the live poll recorded a
`ProposePrice` and the restart resumed at the persisted block with zero
rewrites.

## 8.3 Cross-venue basis

`scripts/report_kalshi_basis.py`, 120 min: 233 mapped pairs, 76 quoted on both
venues, 69 with a basis. Median per-market basis **−1.01 ¢**, median |basis|
**2.45 ¢**. Pooled lead-lag peaks at lag 0 (r = 0.21, 508 pairs, 17 markets) and
+1 (0.13) — contemporaneous co-movement, not a tradeable lead at a 60 s grid.
The event-level study (8.1) resolves Gate 3; this 60 s grid cannot.

## 9. Quote backtest (one L3 asset-hour)

`scripts/backtest_quotes.py`: 3,283 episodes, 6,566 quoted sides, 64 fills
(0.98 %), 1,770 shares. Net +0.0336/fill (+0.0409/share, +$72.33) in hour 01;
hour 05 of the same market has markout −0.0505. One asset-hour is a sanity
check, not a strategy result; the 14-day tape is the sample that decides.

## 10. Gate status

| Gate | Status | Evidence / blocker |
|---|---|---|
| 1 Markout clearance | Measured, partial | 76/129 pass, all low-sample (n<30); 832/834 traded conditions have a `feeType` after the 2026-11-05 metadata refresh (138/647 on the snapshot alone); every row is kept and flagged `fee_known` |
| 2 Reward floor ≥ $1/day, 90 % epochs | Not yet | needs the full 14-day reward-pool denominator |
| 3 Binance/Kalshi lead time | **PASS** | Event-level (8.1): taker p50 1,067.7 ms, lead 1,015 ms (CI 749–1,532), our path 217.5 ms, margin 850 ms; 20-sample clock bracket 339 ms |
| 4 Clean liquidation | Not yet | paper engine runs; 100 % cash-settled audit pending |
| 5 Execution resilience | In progress | 14 days of uninterrupted capture is the test |

## 11. Next

1. Done: reward-campaign walk (3,000 rows / 6 pages / ~5 s); Gamma crawl is
   the fallback.
2. Extend the fill/queue sample beyond 29 asset-hours (the 6 h window limits
   every queue conclusion).
3. Run Gate 2 on the reward epochs once the epoch timestamps are captured.
4. Done: Gate 3 passes with the fresh-probe clock and the local order-path
   bench; re-probe before each decision and keep the path bench in the loop.
5. Keep the scorecard's fee-coverage gap explicit in every Gate 1 table; the
   `fee_known` column flags it and the rows are retained.
6. Decode the UMA CTF adapter v3.1.0 lifecycle; the stream currently covers OO
   V2 and adapters v1/v2.

## 12. Predictive quote gate: the OFI side-selection result (2026-11-05)

Full analysis: `docs/predictive-mm-analysis.md`. Summary:

* The reactionary 1-tick quote over the 29 political asset-hours (2026-11-04
  01:00/05:00, 780 marked fills, 48,869 shares) is **-$309.69 net**
  (net/share -0.00634).
* A trailing-60 s price-only OFI sign gate (skip the side the flow threatens;
  threshold 0, no look-ahead) retains 298 fills and earns **+$17.52**
  (net/share +0.00100). The 482 excluded fills earn -0.01046/share. The
  sign-flipped placebo at the same threshold earns -$335.16; the live
  size-weighted 850/1.5 c engine leaves -$307.22, so the paper uses the
  price-only sign gate.
* This is spread/markout only, in-sample, one tape, L3-shadow fills; the reward
  is not in the net and the gate is single-sided. The 14-day paper run decides
  the combined objective.
* Built: `PaperTradingEngine(microstructure_gate=True)` and
  `scripts/run_paper_trading.py --microstructure-gate`; the unit test passes
  (`tests/test_paper_trading.py`). The A/B harness is
  `scripts/backtest_predictive_gate.py`.

Gate 3 remains a latency PASS, not a PnL result. The crypto L3 hours built for
the Binance gate have 0-3 fills each, so that A/B has no power yet.

### 12.1 Queue split: the gate is a back-of-queue gate (2026-11-05)

Joining the same marked fills to the L3 queue at the quote price
(`scripts/analyze_queue_positions.py`, 777 of 934 rows joined) shows the side
gate was selecting on FIFO position, not just side:

* Front of the level (`queue_ratio = q_ahead/(q_ahead+q_behind)` < 0.60): 199
  fills, **+0.00364/share, +$46.17**.
* Back of the level (ratio >= 0.60): 281 fills, **-0.02014/share, -$372.72**.

An unconditional side gate keeps the back and drops the front: +$20.71. Gating
on the queue position instead — keep both sides at the front, shrink or pull the
threatened side only at the back — keeps 496 fills and earns **+$66.88** on the
same tape. A small trade can reach the front of a level; only a sweep reaches
the back, which is why the back fills are the adverse ones.

The measured fill model (6,817 observations / 2,184 fills, same two hours) is
`logit P = 3.2769 - 1.1421*ratio - 0.5733*log1p(size)`, |z| > 14; it ranks
queue positions but is print-conditional (per-episode fill rate 0.98 %).

Built and tested: `src/polymarketbot/paper_trading/orchestrator.py`
(`policy=queue_aware`, `gate_action=shrink`, `min_adverse_queue_ratio=0.60`,
`shrink_scale=0.25`; disabled by default), wired into the engine with a per-quote
replay of the L3 FIFO ratio, and exposed as
`scripts/run_paper_trading.py --orchestrator`. 11 tests in
`tests/test_orchestrator.py`; full suite 1,254 passed, 1 skipped. The 45 s live
smoke (`--orchestrator`, 2026-11-05 16:48 UTC) ran and persisted, but the
WebSocket streamed too briefly for the gate to fire on live flow.
