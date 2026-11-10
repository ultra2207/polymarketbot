# Live-Trader Data Gap Analysis

Audit date 2026-11-05 12:15 IST. Scope: the running collector (9 streams), the
Kalshi feed, the two-week unattended run, and the data the real-money trader
needs that the collector does not record. Every number below came from the tape,
the monitor log, or the live process table.

## 1. Bottom line

* **Kalshi is healthy and now polls at 2 s; it is still not sub-second.** It
  records fine (monitor `kalshi_fail=0`), trades at 8 s, has 233 mapped tickers
  (136 quoted both sides), and only **26 of the 200 largest reward markets**
  mapped. The WebSocket is auth-only and no Kalshi key is configured, so the
  stream is REST-only. A 60 s probe at 2 s returned 30/30 clean rounds, so the
  cadence was lowered from 15 s to 2 s at 12:16 IST; that is adequate for the
  0.5–3 s macro lead, not for sub-second timing.
* **The two-week run can continue, with two must-dos.** Disk and rate are safe
  (205 GB free, 5.5–5.8 GB/day → ~78 GB for 14 days; monitor WARN at 60 GB /
  15 GB/day). But nothing restarts a dead collector and nothing starts it after
  a reboot. A watchdog script now exists; it must be installed in Task
  Scheduler, and Windows sleep must be disabled for the window.
* **The tape is genuinely useful for research, not as the live trader's
  feed.** It is the empirical foundation for fills, queue position, markouts,
  toxicity, reward share, basis and lead-lag. It does not replace the live
  clients, and it is missing the live trader's **own order lifecycle, account
  state and actual reward accrual** — the P0 gaps below. Event/fee metadata is
  no longer a gap: the manifest now carries `event_id` and `fee_type` for all
  2,496 markets, and the scorecard fee coverage is 832/834 traded conditions
  (was 138/647).

## 2. Kalshi audit

| Check | Measured | Verdict |
|---|---|---|
| Connectivity | monitor `kalshi_fail=0` last hour; 199 historical connection resets in the log | fine (edge pinning fixed the reset storm) |
| BBO cadence | `poll_seconds: 2` (lowered from 15 s at 12:16 IST); trades every 8 s | adequate for 0.5–3 s macro |
| Coverage | 233 mapped tickers; 136 quoted both sides in the last 10 min; 54 traded in the hour; 2,762 BBO changes/hour | thin but live |
| Mapping depth | 26 of the top 200 reward markets mapped (13 %); methods `structured_winner` / `margin_threshold` | **insufficient for the live universe** |
| WebSocket | config comment: WS requires auth (HTTP 401); no `KALSHI_*` key in `.env` | REST-only by design |
| Basis study | 76 both-quoted pairs; 60 s grid peak r=0.21 at lag 0 | research-grade, not a trade trigger |

Why it was 15 s: the stream deliberately backs off to 15 s after connection
resets (the log shows 6 s / 10 s / 15 s phases). The resets were DNS-edge
resets, now mitigated by TLS-probed edge pinning; the 15 s setting was never
re-tested after that fix. On 2026-11-05 a 60 s probe (30 rounds × 3 requests at
2 s) returned 30/30 rounds, 0 resets, 0 retries, ~0.5 s per round; the running
collector was restarted with `poll_seconds: 2`. The stream is still bounded by
REST.

Remaining options:

1. **Kalshi API key + WebSocket** (authenticated `orderbook_delta`/`ticker`/
   `trade` channels). This is the only way to get sub-second BBO. Keys are not
   in `.env`; the user must create them (RSA key pair).
2. **Event-basket mapping** for the composite top markets (a question like
   "Republicans lose AK, IA, KS, …" maps to a basket of Kalshi seat markets or
   the Senate-control series, not one ticker). The current per-market mapping
   cannot cover those composites.

Until one of these is done, Kalshi is a **slow cross-check**, not a Gate 3
macro trigger. The crypto leg of Gate 3 is already PASS via Binance; the Kalshi
leg is unmeasured.

## 3. Two-week unattended run

Measured on the live process and monitor:

| Item | Status | Risk |
|---|---|---|
| Disk | 205.1 GB free; CLOB rate 5.5–5.8 GB/day; 14 days ≈ 78 GB; monitor WARN <60 GB, CRIT <30 GB | safe |
| Writer | queue 0–2, spills 0, compaction errors 0 | safe |
| Monitor | `collector-monitor` persistent, 5 min; alerts to `data/logs/collector_alerts.log` | local file only; **no external notification** |
| Process persistence | collector and monitor are persistent tool services | **no restart on crash/reboot**; a reboot kills both |
| CLOB disconnects | 5–6 per hour, WARN; each resync gap 5–11 s | ~3–6 h of CLOB gaps over 14 days; the tape records the gap; cannot be backfilled |
| Polygon | 0 unbackfilled blocks; Alchemy 429s + dRPC rate limits rotate; backfill gap 0 | provider quota is the main external dependency |
| Kalshi | 0 failures last hour; 2 s cadence since 12:16 | mapping breadth, not reliability |
| UMA | polls resume at the persisted block, zero duplicates | fine |
| Universe | reward walk ~5 s every 6 h; poll 60 s; sentinel cap 200 | fine |
| Sleep/reboot | not verified; the watchdog is not installed | **the one thing that can silently lose days** |

Required before leaving it for two weeks:

1. Install the watchdog every 5 minutes (as the logged-in user):
   ```
   schtasks /create /tn PolymarketCollector /tr "\"C:\Users\Sivasai\Documents\GitHub\polymarketbot\.venv\Scripts\python.exe\" -m polymarketbot.ops.watchdog --once" /sc minute /mo 5 /f
   ```
   It detects a live `run_data_collector.py` (including the uv trampoline
   parent/child), starts one detached when none exists, and logs to
   `data/logs/watchdog_collector.log`. It never kills a live process.
2. Disable sleep: `powercfg /change standby-timeout-ac 0`. A sleeping host
   stops every stream, watchdog or not.
3. Check `data/logs/collector_alerts.log` daily; add an external alert
   (email/webhook) if the machine is not watched.
4. Keep the display/network awake; the collector needs the internet path, not
   the screen, but Windows power plans can suspend the NIC.

## 4. Data the live trader needs that the collector does not record

Priorities are for the live trader and post-trade risk; the tape is unaffected
by P0/P1 labels.

| # | Need | Have now | Gap | Priority |
|---|---|---|---|---|
| 1 | **Own order/fill/cancel lifecycle** | v2 `LiveSessionRecorder`: venue orders/trades REST, authenticated user WebSocket, engine SQLite, quote/risk hooks, written to `data/live_tape/<session>` | queue position from the venue (`get_order_scoring` now polled) | **P0 (v2 done)** |
| 2 | **Account snapshots** | v2 recorder: CLOB balance/allowances, data-api positions, on-chain POL/USDC.e/CTF balances, settlement receipts | allowances remain CLOB-reported (no on-chain allowance call yet) | **P0 (v2 done)** |
| 3 | **Market rules & resolution** | manifest + `universe_metadata`: question, slug, endDate, negRisk, tokens, event_id/title/slug, resolution source, feeType, tick size; `RiskConfig.max_event_*` caps wired through the paper, rewards and live engines | event description/bond, UMA adapter, `accepting_orders` | **P1 (rules only)** |
| 4 | **Fees per market** | `universe_metadata`: fee_type/schedule for 2,496/2,496 (152 are `zero_fees`); scorecard 832/834 traded fee-known | 2 traded conditions are not in the current manifest | **done** |
| 5 | **Reward accrual/epoch** | v2 recorder: `/rewards/user/total`, `/rewards/user` daily rows, `get_order_scoring` per order | epoch-boundary granularity from the venue (endpoints are daily) | **P1 (v2 done)** |
| 6 | **Kalshi timing** | 2 s BBO (was 15 s), 8 s trades, 233 mapped | WebSocket (needs keys) for sub-second; event-basket mapping; 26/200 top markets | **P0** for macro |
| 7 | **Macro/news calendar** | 4 RSS feeds (crypto + Google News "polymarket"), Gamma comments 120 s | economic calendar (CPI/FOMC/releases), X/GDELT, paid wires | P1 |
| 8 | **UMA mapping** | OO V2 + adapter v1/v2; 1 event recorded | adapter v3.1.0 not decoded; condition_id mapping from ancillary text only | P1 |
| 9 | **On-chain self state** | v2 recorder: our wallet's POL/USDC.e/CTF balances every 30 s + settlement receipts | on-chain allowance calls; redemption status beyond `pending_settlements` | **P0 (v2 done)** |
| 10 | **Binance tape fidelity** | BBO throttled to 100 ms per symbol; depth 500 ms | sub-100 ms research on the recorded tape; the live WS is unthrottled | P2 (research) |
| 11 | **Venue latency/clock** | v2 recorder: per-poll REST latency + engine place/cancel RTT; feed staleness in `session_health` | clock skew vs venue (`/time`) | **P2 (v2 metric)** |
| 12 | **Backup** | one C: drive | no copy of the 14-day raw tape | P2 |

Why #1 and #2 are P0: the fill model in `data/derived/fill_model.json` is
calibrated on **public** prints (6,817 observations). It predicts the chance a
resting order fills given queue ratio and depth, but it has never been checked
against **our** fills, our queue position, or our realised slippage. The live
trader's actual adverse selection, cancel effectiveness, and inventory risk
cannot be measured without the private user channel and account snapshots.

Why #3/#4 were listed as P0: this was a pipeline omission, not a missing
source. Gamma `/markets?condition_ids=` returns the event object and
`feeType`/`feeSchedule`; the 2026-11-01 snapshot already had `event_ids` for
40,000/40,000 markets and `feeType` for 39,446/40,000 — but 509 of the 647
traded conditions were created after the snapshot, so the manifest dropped the
fields and the scorecard's snapshot join left them n/a.
`polymarketbot.data.refresh_universe_metadata` now fetches the live manifest in 25
Gamma requests, writes `data/derived/universe_metadata.parquet`, and backfills
`event_id`/`event_title`/`fee_type`/`resolution_source`/`tick_size` into the
manifest. The 2026-11-05 scorecard window reports **832/834** traded conditions
fee-known (199/834 on the snapshot alone); the 2 missing are conditions that
traded but are no longer in the manifest. Event grouping is now queryable
directly (985 events across 2,496 markets), and the risk engine enforces it:
`RiskConfig.max_event_inventory_usd` / `max_event_position_cost_usd` cap the
sum of all positions sharing an `event_id` (default 50% of bankroll), wired
through the paper engine's cross-market aggregation, the rewards engine and the
live engine; `MarketConfig`/`MarketOpportunity`/`MarketRewardParameters` carry
`event_id`.

## 5. Is the tape useful for the trader?

Yes, for **research and post-trade analysis**, not as the live feed:

* Event-driven L2 books for 4,994 assets, public trades, Binance BBO/Binance
  depth/funding/OI, Deribit surface, Polygon CTF/gas, UMA, news, Kalshi BBO.
* The analysis scripts already exist and run on the tape: L3 queue rebuild,
  fill model, markout/scorecard, toxicity, Kalshi basis, quote backtest,
  Binance→CLOB lead. The 14-day sample removes the current 6-hour limitation
  (29 asset-hours, 6 books) that constrains every queue conclusion.
* The Gate 3 crypto leg is decided on the same tape: taker p50 1,067.7 ms, lead
  1,015 ms (CI 749–1,532), our path 217.5 ms, PASS.

Limits of the tape as research evidence:

* Kalshi at 2 s is adequate for the 0.5–3 s macro lead but cannot resolve sub-second timing; the existing basis study was built on 15 s data at a 60 s grid and should be rebuilt from the 2 s feed.
* The Binance BBO is throttled to 100 ms in the tape; sub-100 ms tape research
  is not possible, though the live WS feed is unthrottled.
* Fee coverage and market metadata are incomplete for markets created after
  2026-11-01, so fee-aware edge/reward studies miss them.
* CLOB disconnects leave 5–11 s gaps 5–6 times an hour; the L3 rebuild must
  treat them as gaps, not as zero flow.

## 6. Recommended order of work

1. **Watchdog + Task Scheduler + sleep off** (done: script; install task).
2. **Private user-channel + account snapshot recorder** (P0): a new stream
   writing `user_orders`, `user_fills`, `account_snapshot` to the tape; cadence
   1–10 s for orders/fills, 30–60 s for balances/positions/allowances.
3. **Event-level risk wiring** (done): the manifest carries
   `event_id`/`fee_type`, and `RiskConfig.max_event_*` caps are enforced across
   markets sharing an event; `tests/test_risk_event_caps.py` covers the cap and
   the paper engine's cross-market aggregation.
4. **Kalshi** (P0 for macro): 2 s REST is done; next is keys→WS for sub-second,
   and event-basket mapping for the composite top markets.
5. **Reward accrual** (P0/P1): per-epoch score/payout snapshot from the CLOB
   account; the Gate 2 denominator.
6. **News** (P1): economic calendar + X/GDELT; the current RSS set is crypto-
   centric and misses macro releases.
7. **UMA v3.1.0 + condition mapping** (P1).
8. **Tape backup** (P2): copy the raw tape once a day; the 14-day window is the
   only irreplaceable asset.
