# Running the Three Paper Strategies on a Local India PC

**Question:** can the trader run on a local India PC (16 GB RAM, 24 threads,
RTX 4080 Mobile) instead of paying for a larger EC2 instance, and what does the
high India→London latency do to RewardHarvester, SettlementHarvester and
CtfMinter?

**Answer in one line:** the local PC solves the *memory and CPU* failure
completely, but the high-RTT path is not neutral: it is acceptable for
SettlementHarvester, marginal for RewardHarvester, and dangerous for CtfMinter
live. The current paper soak also **does not price most of that latency**, so a
local paper PnL will look better than a local live deployment would achieve.

## 1. Why the t3.micro failed

Measured on the current host:

| Item | Evidence |
|---|---|
| Instance | `t3.micro`, 2 vCPU |
| RAM | `MemTotal: 930,428 kB` (~908 MiB) |
| Swap | 2 GiB, already 372–713 MiB used |
| Soak RSS (1 GB profile) | 441–558 MB |
| Agent harness RSS | ~320 MB idle, ~570 MB while reasoning |
| Kernel | repeated `systemd-journald: Under memory pressure, flushing caches` |
| Cycle 6 | stopped at 8m45 with reward page 15 in flight; a later 30-minute run restarted the instance |

The full-universe scans are not huge by themselves; the host was simply
oversubscribed: 908 MB total − 320–570 MB harness leaves ~340–590 MB for a
process whose measured peak is 558 MB. The 16 GB local PC removes this entire
failure mode; the soak used <0.6 GB in the 1 GB profile and needs no memory
guard.

The 24-thread CPU also matters: the t3.micro has one physical CPU budget, and
the three full-universe sweeps plus HTTP worker pools contend for it. The
`ctf_scan` and telemetry watchdogs stalled 49–52 s under memory pressure even
though the CTF compute itself was 1.8–5.8 s. Local 24 threads and 16 GB should
remove those stalls.

## 2. Network baseline: what is actually measured

### London host (this EC2, eu-west-2)

`scripts/measure_local_latency.py` run from the London host, 5 samples each:

| Endpoint | p50 | p95 | jitter | DNS | TCP | TLS |
|---|---:|---:|---:|---:|---:|---:|
| CLOB `GET /time` | 39.35 ms | 44.71 ms | 2.93 ms | 0.64 ms | 1.01 ms | 11.74 ms |
| Gamma `GET /markets` | 17.91 ms | 19.00 ms | 4.53 ms | 1.35 ms | 0.56 ms | 11.99 ms |
| Binance `GET /fapi/v1/time` | 225.01 ms | 226.66 ms | 1.54 ms | 1.31 ms | 0.77 ms | 11.53 ms |
| CLOB `POST /books` | 36.88 ms | 40.14 ms | 1.55 ms | 0.73 ms | 1.09 ms | 12.24 ms |

Normal dashboard CLOB RTT is 25–41 ms. During the memory-pressure episodes it
was 200–3,800 ms; that was host starvation, not network distance.

### India → London physical path

From London, an AWS Mumbai endpoint (`dynamodb.ap-south-1.amazonaws.com`) was
120.4 ms RTT over 5 packets, 0% loss, mdev 0.03 ms. This is not the user's ISP,
but it establishes the physical floor: **India ↔ London is at least ~120 ms
round trip**, i.e. roughly 3–5× the London CLOB RTT before any residential ISP
jitter, CGNAT or peering penalty.

`clob.polymarket.com` resolves to Cloudflare (`2606:4700:...`). A local India
client may reach a nearby Cloudflare edge quickly for DNS/TCP/TLS, but
`/time`, `/books` and Gamma are dynamic origin calls; TTFB, not TLS handshake,
is the relevant number. **Do not substitute a speed-test ping for this.**

**The numbers in this table that are not measured are estimates, not facts:**

| Endpoint from India PC | Estimate to validate | Why |
|---|---:|---|
| DNS + TCP + TLS to CLOB edge | 15–40 ms | Cloudflare Mumbai/Chennai POP |
| CLOB `/time` TTFB | 130–250 ms | London origin + India–London path |
| CLOB `/books` TTFB | 130–250 ms | same; batch size dominates body time |
| Gamma `/markets` TTFB | 120–250 ms | Gamma origin |
| Binance Futures `/fapi/v1/time` | 100–300 ms | route/ISP; Binance may be restricted |
| Jitter / loss | unknown; must measure | residential ISP, time of day |

Run this on the India PC before drawing conclusions:

```bash
uv run python scripts/measure_local_latency.py --samples 100
```

Run it at 10:00, 20:00 and a weekend peak. Use **p95**, not p50, for risk. A
p95 > 250 ms, or >1% failed requests, is a red flag for CtfMinter live.

## 3. How latency enters each strategy in this codebase

### RewardHarvester — medium sensitivity

Mechanism: quote two-sided inside-touch maker orders in reward pools, accrue
the quadratic `Q_min` reward share from the official formula, and harvest maker
rebates. The strategy is not a race to a signal; it is a race to **queue
position and quote freshness**.

Code facts:

- `scripts/run_live_paper_soak.py` uses `MEASURED_MAKER_MARKOUT = 0.0063`
  (0.63¢) for adverse selection in `_walk_bid_exit` and the CTF residual leg.
  That constant came from the repo's cycle-4 fill study; it is **not** a
  function of the local India RTT.
- `compute_downtime_cost` in `src/polymarketbot/rewards/queue_valuation.py`
  does use `last_clob_rtt_ms`, as `daily_yield * latency / 86,400`. At
  $100/day per pool and 200 ms, that is $0.00023; economically negligible
  relative to queue/adverse-selection effects. It is not the main latency
  channel.
- Queue-ahead is measured from the L2 book at snapshot time
  (`queue_ahead_shares`); a 200 ms stale snapshot understates the queue that
  actually existed when the order arrived, and a slower cancel/replace loses
  priority at the touch.
- The paper reward model accrues from the radar snapshot immediately. It has
  no order-ack or cancel latency, so the paper PnL is **not** degraded by
  local ping.

Impact: at 200 ms RTT the quote is ~175 ms behind a 25 ms competitor. At a 1¢
tick, that is the difference between resting first and resting behind a queue.
The reward share itself depends on `Q_min`, not time, but fills, markouts and
queue priority do. A local paper soak will therefore show roughly the same
reward accrual as London; a local live harvester would not.

### SettlementHarvester — low sensitivity

Mechanism: buy discounted outcome tokens on resolving markets and hold to par
redemption. The opportunity horizon is hours to days, not milliseconds.

Code facts:

- The radar scans ~10,000 Gamma markets and batch-fetches CLOB books. A 200 ms
  RTT adds ~100–200 ms per batch, not per market; with 100-token chunks and
  4 workers, the wall-clock penalty is seconds, not minutes.
- The `SettlementCandidate` at `scripts/run_live_paper_soak.py:3191` carries
  price, ladder, fee schedule and `p_win`; it carries **no latency field**.
  The paper fill uses the book snapshot's ladder, so a stale local snapshot
  can make the fill look better (or worse) than the actual book.
- The recorded `latency_us=25.0` on settlement trades is a hardcoded paper
  audit field, not a modelled execution delay.

Impact: latency is not the dominant risk. A 1–2¢ entry slippage on a 10–70¢
par edge is tolerable. The real risks are oracle resolution, UMA liveness,
fee leakage and capital lock-up, which are already in the hazard/settlement
model. Settlement is the strategy most suitable for a local India machine.

### CtfMinter — high sensitivity

Mechanism: quote both legs of a binary complete set at `Bid_A + Bid_B < 1.0`,
merge the pair, and unwind any broken residual leg. The edge is typically a
few cents; the payoff depends on **both legs filling** or on exiting the
residual at a real bid.

Code facts:

- The broken-leg exit `_walk_bid_exit` charges
  `haircut = max(tick, markout * px)` with the fixed 0.0063 markout. At a
  local India RTT, adverse selection is likely larger than 0.63¢, but the
  model will not know that.
- The maker parity EV uses `p_leg = 1 - exp(-lambda * dt)` from measured
  seller flow (`scripts/run_live_paper_soak.py:4116`) and
  `p_leg²(1-quote_cost)ref - p_leg(1-p_leg)(loss0+loss1) + p_leg*rebate`
  (`:4251`). There is no RTT or cancel-race term; a stale quote is assumed to
  have the same fill probability as a fresh one.
- The trade record writes `latency_us=max(1, last_clob_rtt_ms*1000)`
  (`:4399`/`:4441`) but that field is telemetry only; it does not change PnL.
- The live order path has explicit latency assumptions elsewhere:
  `src/polymarketbot/trading/virtual_queue_book.py:685` uses 150 ms reaction /
  cancellation round-trip, and `src/polymarketbot/paper_trading/engine.py:1552`
  has a 120 ms relayer latency with a latency-decay model. The soak's CTF
  path does not apply those.

Impact: for a 1–3¢ parity edge, 200 ms of stale quoting is enough to turn a
positive EV into a one-leg fill followed by an adverse exit. The local paper
soak will overstate CTF PnL because it fixes markout and ignores the
cancel/leg-risk race. **Do not use local India paper results as evidence that
CtfMinter is live-profitable.**

## 4. Strategy verdict

| Strategy | Local PC paper soak | Local PC live | Main latency channel | Verdict |
|---|---|---|---|---|
| RewardHarvester | Works; 16 GB removes OOM; scans 10–30% slower | Conditionally viable; markout/queue must be re-measured at local RTT | Queue priority, cancel/replace, adverse selection | **Paper yes; live only with a latency-adjusted fill model** |
| SettlementHarvester | Works; ideal local workload | Plausibly viable; use limit orders and verify fills | Entry slippage on a multi-hour opportunity | **Best fit for local India** |
| CtfMinter | Runs, but PnL is optimistic | Not recommended from India | Two-leg race, broken-leg markout, cancel latency | **Paper only with latency stress; live in London** |
| LatencySniper | Benched; not in the soak | Requires London/Binance colocation | Direct signal race | **Not relevant** |

## 5. Scan-time impact: batch requests soften the high ping

The full-universe scans are batched. The reward radar has 16 Gamma pages plus
~32 book chunks; settlement has ~120 Gamma pages plus ~25–50 book chunks; CTF
has 22 Gamma pages plus ~34 book chunks. A 150 ms RTT penalty per request is
seconds, not the 8× one would naively apply to every market.

Expected local wall-clock (estimate, validate with the probe and a local run):

| Scan | London observed | India estimate | Main cost |
|---|---:|---:|---|
| Reward full sweep | 38–45 s | 45–60 s | 16+32 requests, scoring 5–10 s CPU |
| Settlement full sweep | 3–27 s | 15–45 s | 120+ pages; 100-token book chunks |
| CTF full scan | 1.8–5.8 s | 8–15 s | 22+34 requests, 24-thread CPU |

A slower scanner means **staler L2 snapshots**, which is a fidelity problem for
all three models, even when the economic latency (order ack) is acceptable.

## 6. Local PC operational assessment

**Positive:**

- 16 GB RAM vs <0.6 GB soak: no memory guard, no swap, no instance restart.
- 24 threads vs the t3.micro's 2 vCPU: HTTP pools and the three engines have
  headroom; the 49–52 s watchdog stalls should disappear.
- 4080 Mobile is unused: this code is CPU/network/JSON-bound; no CUDA path.
- No EC2 credits consumed; the 30-minute loop can run repeatedly.

**Negative / must handle:**

- Residential ISP: jitter, packet loss, CGNAT, dynamic IP, power cuts.
- No London proximity: CTF leg risk and reward queue priority are worse.
- API rate limits: do not raise worker counts to compensate for RTT; the
  current code already uses measured concurrency and a rate-limit warning.
- Time sync: NTP must be healthy; order signing/timestamps and log alignment
  depend on it.
- Polymarket/Binance access from India (including any geo/terms restrictions)
  must be verified by the probe; the probe makes no assumptions.
- Local disk: the 45 GB tape + SQLite + logs need space; monitor.
- Security: API keys on a home machine; disk encryption, no exposed ports,
  backups.

## 7. Recommended operating modes

### A. Research / paper soak locally — recommended

Run the full 30-minute loop on the local PC, no memory guard needed:

```bash
MALLOC_ARENA_MAX=2 uv run python scripts/run_live_paper_soak.py \
  --capital 3000 --duration 1800 --interval 10 --fresh
```

Then run the same three audit agents. Label the CTF results as **latency-
optimistic** unless a latency-stress model is added. Reward and settlement
paper results are usable; settlement is the strongest local result.

### B. Live execution — hybrid, not all-local

Use the local PC for research, backtests and the paper soak, but keep order
placement close to the CLOB:

- London VPS (eu-west-2) for CtfMinter live.
- Local or a small VPS for SettlementHarvester; limit orders, not market
  sweeps.
- RewardHarvester live only after measuring local markout and queue priority;
  a London VPS is still preferred for quote refresh.
- If credits are the constraint, the smallest London execution VPS plus the
  local PC for compute is cheaper than a large EC2 that also runs scans.

### C. Add a latency stress before trusting CTF

Before any local CTF conclusion:

1. Run the probe (`measure_local_latency.py`), use p95 RTT.
2. Fit a local markout: sample public CLOB trades and compute post-fill
   markout at 100/200/300 ms; replace the fixed 0.0063 only with a measured
   value (the repo rule prohibits a new hand-set constant).
3. Re-run CTF sweep at 25/100/200 ms effective latency; compare EV and
   broken-leg losses. If the edge does not survive 200 ms, the local machine
   stays paper-only for CTF.

## 8. Validation checklist

- [ ] `scripts/measure_local_latency.py --samples 100` on the India PC, peak
      and off-peak; save `data/derived/local_latency.json`.
- [ ] Record p50/p95/p99, jitter, loss for CLOB `/time`, `/books`, Gamma and
      Binance.
- [ ] Run the 30-minute soak locally; compare scan durations and fill counts
      against the last London window.
- [ ] If CTF is to be trusted, add the measured local markout and latency
      stress; do not rely on the current fixed 0.0063 markout.
- [ ] Verify Polymarket/Binance access, NTP, UPS/Ethernet and log rotation.

## 9. Bottom line

The local India PC is the right answer to the **memory/CPU** problem, and the
right answer for **SettlementHarvester** and for **paper research** on all
three. It is not a free lunch for **CtfMinter live**: the parity edge is a
few cents, the current paper model ignores the RTT-driven leg race, and a
residential India→London path is 3–5× the London CLOB RTT. Run the probe
first; if p95 RTT is >250 ms or loss is material, keep CTF in London and use
the local PC for everything else.
