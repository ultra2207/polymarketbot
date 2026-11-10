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

**Measured on the local PC, 2026-11-09 (§2a):** the quote path measured **174–260 ms p50** (p95 205–397 ms) and the order endpoints track it — `POST /order` 260/368 ms, `DELETE /order` 264/305 ms (unauthenticated lower bound) — against 37 ms for the same `/books` call from London. Gamma is
3–4× worse than estimated (**444–720 ms** TTFB), and every "lost" request was a
**DNS resolution failure**, not packet loss: the raw resolver failed **8–27 %**
of samples, and it zeroed the CTF universe on the very first Gamma page.
`install_doh_fallback()` removed both failure modes (0/60 requests failed,
Gamma halved to 444 ms). **Do not run the soak on this machine without it.**

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

### 2a. Measured here (2026-11-09, Monday, 16:13–16:24 IST)

`scripts/measure_local_latency.py` (50 and 100 samples), a direct `POST /books`
loop, and an A/B against `polymarketbot.net.install_doh_fallback()`. "Raw" is
this machine's normal `getaddrinfo`; "DoH" is 1.1.1.1 over HTTPS with IPv4
pinned. Raw-resolver numbers are in `data/derived/local_latency.json` and
`data/derived/local_latency_100.json`.

| Endpoint | resolver | p50 | p95 | p99 | jitter | failed |
|---|---|---:|---:|---:|---:|---:|
| CLOB `GET /time` (n=50) | raw | 285 | 339 | 584 | 46 | 14 % |
| CLOB `GET /time` (n=100) | raw | 287 | 363 | 565 | 53 | 8 % |
| CLOB `GET /time` (n=30) | DoH | 262 | 304 | — | — | 0 % |
| CLOB `POST /books`, 1 token (n=30) | raw | 174 | 205 | max 294 | — | 0 % |
| Gamma `GET /markets` (n=50) | raw | 709 | 804 | 827 | 58 | 2 % |
| Gamma `GET /markets` (n=100) | raw | 720 | 852 | 7 389 | 223 | 1 % |
| Gamma `GET /markets` (n=30) | DoH | 444 | 495 | — | — | 0 % |
| Binance `GET /fapi/v1/time` (n=50) | raw | 160 | 225 | 365 | 32 | 0 % |
| Binance `GET /fapi/v1/time` (n=100) | raw | 155 | 195 | 225 | 27 | 0 % |

Order-path endpoints (DoH installed, keep-alive session, 16:39–16:41 IST, n=25
each). These are **unauthenticated** calls: the 401 is returned by the auth
middleware, so the number measures DNS/TCP/TLS/edge/origin plus handler entry
and **excludes** signature verification and matching — a lower bound, not an
ack time.

| Endpoint | status | p50 | p95 | max |
|---|---|---:|---:|---:|
| `POST /order` | 401 | 260 | 368 | 806 |
| `DELETE /order` | 401 | 264 | 305 | 332 |
| `DELETE /cancel-all` | 401 | 284 | 335 | 416 |
| `POST /books`, 1 token (same window) | 200 | 259 | 397 | 412 |

So the previous claim that order endpoints sit near the book latency is
**confirmed**: they track `/books` in the same window (260 vs 259 ms p50).
`/books` itself measured 174 ms p50 at 16:15 and 259 ms p50 at 16:41, so the
honest quote-path range is **174–260 ms p50**, with p95 **205–397 ms**. The
London equivalents for the same endpoints are not measurable from here; the
London `/books` baseline (37 ms p50) is the best available bound, i.e. a 5–7×
penalty on the order path.

What the measurements establish:

- **The failures are DNS, not the wire.** Every failed sample is
  `NameResolutionError`/`ConnectionError` on `getaddrinfo`; `ping
  gamma-api.polymarket.com` fails at the OS resolver and `nslookup -type=A`
  is refused by the router (`192.168.1.1`). Failure rate was 2.5 % at 16:16
  and 27 % at 16:21 — bursty, which is exactly what silently corrupts a crawl.
- **`install_doh_fallback()` fixes it** (60/60 requests OK, 0 % failure). This
  is the repo's existing, documented mitigation (`src/polymarketbot/net.py`),
  already used by the collector and most scripts. It is now installed in
  `run_live_paper_soak.py` (at startup, before any session opens) and on by
  default in `measure_local_latency.py` (`--raw-dns` opts out). Raw-resolver
  numbers above came from `--raw-dns`.
- **CLOB is edge-close, origin-far:** TCP to the Cloudflare edge is 16–44 ms
  (ICMP 28 ms), so the 262–287 ms `/time` TTFB is the India→origin path, not
  the local ISP hop. The quote/order path is **174–260 ms p50**, and the order
  endpoints are indistinguishable from `/books` in the same window (260 vs
  259 ms), so the estimate band holds at the low end of the day and is
  breached at the high end.
- **Gamma is the outlier:** TCP connect alone is 225–230 ms against the system
  answer (IPv6 CloudFront) and 137–187 ms against the IPv4 edge DoH selects;
  pinning IPv4 halves TTFB from ~720 ms to 444 ms. Against the 17.9 ms London
  Gamma TTFB, that is **25–40×** — and 3–4× the 120–250 ms estimate below.
- **Pagination under the raw resolver is not merely slow, it is wrong.**
  `_paginate_gamma_universe` (100/page, wave 8) failed on its **first page**
  and returned `None`; with DoH the same call returned the full
  **2 100-market universe in 22 pages in 2.0 s**. One DNS hiccup before the
  first page zeroes the CTF scan; after it, `partial_failure` silently
  truncates the universe.
- **Batch bodies are cheap:** `POST /books` with 50/100/200 tokens returns in
  201/274/308 ms, and one reward page (`/rewards/markets/multi`, 500 pools)
  costs 1.27 s. Latency is per request, not per market.

Caveat: one weekday evening window. The report's instruction to re-run at
10:00, 20:00 and weekend peak still stands for jitter/loss; the RTT floors
reproduced across all runs above.

**The estimates this report was written from, checked against §2a:**

| Endpoint from India PC | Estimate | Measured (raw / DoH) | Verdict |
|---|---:|---:|---|
| DNS + TCP + TLS to CLOB edge | 15–40 ms | TCP 16–44 ms | as estimated |
| CLOB `/time` TTFB | 130–250 ms | 287 / 262 ms | **above band** |
| CLOB `/books` TTFB | 130–250 ms | 174 ms | as estimated |
| Gamma `/markets` TTFB | 120–250 ms | 720 / 444 ms | **3–5× above band** |
| Binance Futures `/fapi/v1/time` | 100–300 ms | 155 ms | as estimated |
| Jitter / loss | unknown; must measure | jitter 27–223 ms; **DNS failures 1–27 %**, 0 % under DoH | half as estimated, half new failure mode |

The Binance restriction worry did not materialise: 150/150 probes succeeded at
155 ms p50 (`fapi.binance.com` resolved to a CloudFront edge 8–13 ms away).

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
  has a 120 ms relayer latency with a latency-decay model. Measured from this
  PC, a single `DELETE /order` is **264 ms p50** (305 ms p95, unauthenticated
  lower bound), so a cancel+replace round trip is ~2× the paper assumption
  before any server-side matching delay. The soak's CTF path applies neither
  model.

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

Expected local wall-clock, using the §2a per-request costs ("derived" = my
arithmetic on those costs, not observed end-to-end; the soak's own dashboard
reports the observed sweep):

| Scan | London observed | Local, derived | Basis |
|---|---:|---:|---|
| Reward full sweep | 38–45 s | ~15–25 s | 16 sequential reward pages at up to 1.27 s cold; 32 book chunks over 16 workers |
| Settlement full sweep | 3–27 s | ~10–20 s | ~120 Gamma pages = 15 waves of 8 at 444–720 ms TTFB; 25–50 book chunks over 4 workers |
| CTF full scan | 1.8–5.8 s | ~4–6 s | pagination **measured 2.0 s** (22 pages, 2 100 markets); 34 chunks of 125 tokens at 274–308 ms over 4 workers; scoring 2–6 s |

The important part is the shape: a scan is a few dozen requests, so a 6–19×
per-request penalty costs **seconds**, not minutes. A slower scanner still
means **staler L2 snapshots**, which is a fidelity problem for all three models
even when the economic latency (order ack) is acceptable.

## 6. Local PC operational assessment

**Positive:**

- 16 GB RAM vs <0.6 GB soak: no memory guard, no swap, no instance restart.
- 24 threads vs the t3.micro's 2 vCPU: HTTP pools and the three engines have
  headroom; the 49–52 s watchdog stalls should disappear.
- 4080 Mobile is unused: this code is CPU/network/JSON-bound; no CUDA path.
- No EC2 credits consumed; the 30-minute loop can run repeatedly.

**Negative / must handle:**

- Residential ISP: jitter, packet loss, CGNAT, dynamic IP, power cuts.
- **The local resolver is broken, and it is the single worst failure mode
  measured here.** `getaddrinfo` fails for Polymarket hosts in bursts (2.5 %
  to 27 % of requests in §2a); `ping gamma-api.polymarket.com` fails at the OS
  level and `nslookup -type=A` is refused by the router. Under the raw
  resolver the CTF universe pagination died on its first page. Install
  `polymarketbot.net.install_doh_fallback()` in every long-running entry
  point, or fix the resolver at the OS (1.1.1.1/8.8.8.8) — the DoH path is
  already the repo's documented mitigation and it also halves Gamma TTFB by
  pinning IPv4.
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

- [x] `scripts/measure_local_latency.py --samples 100` on the India PC,
      2026-11-09 16:13–16:24 IST — saved `data/derived/local_latency.json` and
      `data/derived/local_latency_100.json` (§2a). Still to do: 10:00, 20:00
      and weekend-peak repeats.
- [x] p50/p95/p99, jitter and failure rate recorded for CLOB `/time`,
      `/books`, Gamma and Binance; failures attributed to DNS, A/B-tested
      against `install_doh_fallback()`.
- [x] Order-path endpoints measured (`POST /order`, `DELETE /order`,
      `DELETE /cancel-all`, unauthenticated lower bound); they track `/books`
      within the same window.
- [x] Install `install_doh_fallback()` in `scripts/run_live_paper_soak.py`
      (startup, before sessions open) and in `scripts/measure_local_latency.py`
      (default; `--raw-dns` measures the unfiltered resolver). Verified: probe
      with DoH reports 0 % loss and now reaches the `/books` endpoint it
      previously skipped when token discovery failed.
- [ ] Run the 30-minute soak locally; compare scan durations and fill counts
      against the last London window.
- [ ] If CTF is to be trusted, add the measured local markout and latency
      stress; do not rely on the current fixed 0.0063 markout.
- [ ] Verify Polymarket/Binance access, NTP, UPS/Ethernet and log rotation.

## 9. Bottom line

The local India PC is the right answer to the **memory/CPU** problem, and the
right answer for **SettlementHarvester** and for **paper research** on all
three. It is not a free lunch for **CtfMinter live**: the parity edge is a
few cents, the current paper model ignores the RTT-driven leg race, and the
measured India→CLOB order path is **174–260 ms p50 / 205–397 ms p95** against
37–41 ms from London — a 5–7× penalty, and not a competitive quote refresh.

The report's own gate, applied to what was measured on 2026-11-09:

| Gate | Threshold | Measured | Result |
|---|---|---|---|
| CLOB order-path p95 (`/order`, `/cancel-all`) | ≤ 250 ms | 305–397 ms | **fail** |
| CLOB `/books` p95 | ≤ 250 ms | 205–397 ms (two windows) | **fail at high end** |
| CLOB `/time` p95 (soak ping) | ≤ 250 ms | 304–363 ms | **fail** |
| Failed requests | ≤ 1 % | 8–27 % raw; **0 % with DoH** | **fail until DoH is installed** |

So: **run paper, research and Settlement locally; keep CtfMinter live in
London.** Before the first local soak, install
`polymarketbot.net.install_doh_fallback()` — without it the soak's Gamma
pagination failed on page one in a live test and returns an empty CTF
universe, which would look like "no opportunities" rather than a network bug.
With DoH, the full 2 100-market universe paginates in 2.0 s and nothing in the
scan path is slow enough to matter.

## Deployment plan update (2026-11-10)

A future move to **Oracle Cloud Zurich** is planned, but only *after* production
deployment — it is not available for this build. Zurich is close enough to the
CLOB that the latency objection in §9 stops applying to CtfMinter live; it does
not change anything today.

What that means for the current work:

- The India numbers stay the operative cost basis: measured CLOB `GET /time`
  p50 **285–570 ms** across hours this morning, `polygon_rpc` p50 **424–464 ms**,
  block interval **1.501 s** from 407 headers. The on-chain flight is therefore
  `~1.9 s`, dominated by the block, not the path — moving to Zurich cannot
  shorten a merge below one block.
- CtfMinter's blocker is the *fill model*, not the path: at the exact-price
  probability the break-even edge is ~26.7 $/share at a 225 s horizon against a
  locked parity edge of a few cents. Lower RTT does not flip that sign, so the
  move does not change the CTF verdict.
- When the instance exists, re-measure rather than assume: one command,
  `uv run python -m polymarketbot.tools.latency_probe --samples 50`, writes
  `data/derived/local_latency.json`, and the paper engine reads the measured
  `polygon_rpc` p50 from it.
