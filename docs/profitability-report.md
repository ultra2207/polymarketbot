# Profitability on measured data

Evidence date: 2026-11-10. Every number below is read from a named artifact or
log with its window and sample size. Anything modelled is labelled **modelled**;
anything assumed is labelled **assumption**. The distinction matters here,
because the single largest number in the repository (+39.5 %/day) is a formula
crediting itself.

## 0. Verdict summary

| Pillar | Mechanism | Measured return | Independent verification | Verdict |
|---|---|---|---|---|
| RewardHarvester | LP-incentive market making | **0.115 $/$/day** honest (0.395 was one snapshot) | venue terms verified; **0 payout receipts** | **Terms real, magnitude unverified** |
| SettlementHarvester | buy resolved-but-unfinalised below par | **+4.91 % per cycle** at par (VWAP 0.9510); **0.0726 $/$/day** on observed cadence | 8 fills, cash 97.1 % idle | **Edge real, capacity-bound** |
| CtfMinter (a) complete-set mint | buy YES ask + NO ask | 0 of 76,097 markets below par | full-universe census | **Dead** |
| CtfMinter (b) maker parity bundle | rest bids so bid_YES + bid_NO < 1 | best 0.0589 $/$/day vs 0.0924 hurdle | 2,172 real books, 0 accepted | **Dead** |
| Outside the three | — | one real gate, two unmeasured | see §5 | **No new centre** |

All three pillars are ranked against one hurdle: the reward pillar's shadow
yield, **0.0924 $/$/day** (`scripts/settlement_tail_budget_check.py:198`,
`app.log` 2026-11-10 17:01 UTC). That hurdle is itself produced by the reward
model, which §1 shows is unverified. The ranking is therefore circular at its
top: reward is the hurdle, and reward's own number has zero independent
corroboration. Read the verdicts below with that in mind — the *comparisons*
between settlement/CTF and the hurdle are real measurements; the hurdle's own
validity is not established.

## 1. RewardHarvester

### 1.0 Revision after the documentation audit

The terms were verified against `docs.polymarket.com/programs/liquidity-rewards`
and `/maker-rebates` on 2026-11-10, and our implementation was compared line by
line. **The program is real and our formula matches it**: order score
$S=((v-s)/v)^2 b$, two-sided rule $Q_{\min}=\max(\min(Q_1,Q_2),\max(Q_1,Q_2)/c)$
with $c=3.0$ inside mid ∈ [0.10, 0.90], one-sided scores 0 outside the band,
1,440 samples per UTC-day epoch, paid daily at 00:00 UTC, minimum $1. What does
**not** match is the denominator:

| Item | Venue | Ours | Effect |
|---|---|---|---|
| Competing score $D$ | mean over 1,440 samples/day | **one book snapshot**, extrapolated | **manufactures the yield** |
| Denominator sum | $\sum Q_{\min}$ | $Q_1+Q_2$ | conservative by 1.0–4.0× (safe) |
| Spread origin | size-cutoff-adjusted mid | raw mid | direction unknown, logged |
| Minimum rest | none (scores whatever rests at the sample) | **3.5 s invented** | removed |

Measured program size today: **$113,232.88/day across 11,381 funded markets**
(382 pages / 190,631 rows). The claimed $767.79/day is **0.678 %** of the entire
program — arithmetically possible, but it implies every liquidity provider on
Polymarket collectively rests under $295k against that pool. The same pipeline's
out-of-sample window says **11.5 %/day**, not 38.4 %. The honest number is
therefore **0.115 $/$/day**, and the 39.5 % figure is a single-snapshot artefact.

Defects found and fixed in this pass: the rebate was hardcoded to `0.0` in
`client.py` and never read `GET /rebates/current`; the maker rebate was credited
through a **non-existent** `reward.maker_rebate`, which raised `AttributeError`
on every fee-paying fill *after* booking the fill and thereby skipped that
tick's settlement and mark stages; per-market accrual was logged but not
persisted, so no venue join was possible. All three are fixed, with a
failing-before regression test.

### 1.1 What was actually observed

Window 2026-11-09T20:42:21Z → 2026-11-10T09:17:03Z (**4.592 h**, 1,033 snapshot
rows). Source `data/paper_soak_telemetry.db`, tables `snapshots`, `trades`.

| Quantity | Value | n |
|---|---|---|
| Equity Δ | **+$75.58** on $1,000 | 10 windows |
| Cash credited as reward | **$76.8782** | 1,388 `reward_accrual` rows |
| Fills | 2 | `reward_fill` rows, −$11.8186 notional |
| Mark-to-bid on those fills | **−$1.30 (−3.19 ¢/share, −11.0 %)** | n=2 |
| Maker rebate credited | **$0.00** | 0 rows |
| Net yield on cash | **0.3950 $/$/day** (median window 0.3793, range 0.1846–0.5760) | 10 windows |

### 1.2 Why it is not evidence of profit

**The entire PnL is the accrual model crediting its own formula.** Cash credited
= $76.88 over 4.59 h; independent venue payout receipts = **$0**. The accounting
path (`allocator.py:155,235,264`; `reward.py:527-530`) is:

$$\alpha=\left(\frac{v-s}{v}\right)^2,\quad R(c)=\frac{P\alpha c}{D+\alpha c},\quad \text{credit}=\frac{R(c)\,\Delta t}{86400}$$

Only `P` (daily pool) and `D` (competing depth) are measured. The division of
the pool by *all* competing score is modelled, and the credit is booked per
second with no observed payout distribution. Nothing in the run confirms the
venue pays that amount.

Three further discrepancies make the claim worse, not better:

- **Rebate is $0 by construction.** `paper/pillars/reward.py` has no
  `maker_rebate` path (verified by import). `docs/fees.md:129-131` states rebates
  accrue to aggregate maker flow, not as a per-wallet entitlement.
- **The markout used to rank is 21× better than the markout realised.** Ranking
  uses 0.001493 $/share @10 s, tick 0.001 (n=138, CI 0.00007–0.002915;
  `ctf_maker_markout.json`). Two actual fills realised **−0.0319 $/share**. n=2
  is not proof of a 21× error, but it is the only realised cost observation and
  it points the wrong way.
- **Fill rate, not yield, is what is measured.** `p_fill` = 0.0003588 @10 s
  (8,773,815 placements, 3,148 fills) — 1 fill per 2.3 h per quote. The
  unconditional measured base rate is 0.3204 (n=6,817, `fill_model.json`), and
  `realistic_three_domains_apy.json` claims "~3.2 % per episode", 10× below its
  own measured base rate.

### 1.3 What binds it

**Capital per market to clear the $1/day activation floor**:
$c_{\text{floor}} = D/(\alpha(P/1-1))$. At $2,000, only **251 of 8,427**
rewarded markets are fundable (3.0 %), and capital is spent to 99.9 % of
bankroll (`reward_window.json`). Only 71–80 % of cash is ever resting
(`app.log`: "$93.60/day on $706.16 resting").

Secondary: book coverage. The 12,000-token window sees **19.03 %** of the
rewarded universe, a median 45.5 of the 251 fundable markets, and captures
**$229.35/day of a possible $767.79/day** — a **$538.43/day (70.1 %)** shortfall
at **1.4×** the book budget (`reward_window.json`, 20 draws).

### 1.4 Verdict

**Not demonstrably profitable.** The 39.5 %/day is the model's output, not a
receipt. It is internally consistent with the independent census (38.4 %/day
gross at $2,000), but both run the same formula, so agreement between them is
not corroboration.

What would settle it: one reward epoch end-to-end, with the venue's own payout
report joined to the ledger. That is the single highest-value measurement
available in this project.

## 2. SettlementHarvester

### 2.1 Measured cycle

Source `data/paper_soak_telemetry.db` (`snapshots`, `trades`, `lots`),
2026-11-09T20:42:21Z → 2026-11-10T09:17:03Z (12.578 h).

| Quantity | Value |
|---|---|
| Fills | 8, VWAP **0.9510** (4.90 % below par), levels 0.81–0.98 |
| Cost / shares | $997.675 / 1,049.0937 |
| Taker fees | **−$2.32492** (0.2330 % of cost; rate 0.05, `sports_fees_v3`, fill-exact) |
| Gas | −$0.0124 (8 × $0.00155) |
| Gross at par | **+$51.42** |
| Net at par | **+$49.08 (+4.91 %)** |
| Booked mark | **+$38.05 (+3.805 %)** |
| Redeems / merges | **0 / 0** |

The +$49.08 at par is **unrealised** — no redeem event exists; nothing had
settled before the window closed.

### 2.2 The pricing haircut is the largest single term after the discount

Booked +$38.05 vs realised-at-par +$49.08: the model applies a **1.053 %**
dispute haircut against a **measured 0.1335 %** tail (158/134,664 challenged;
Jeffreys upper 95 % bound, `tail_evidence.py:117-143`). That haircut costs
**−$11.03**, i.e. 22 % of the edge, and it is now the conservative side — the
measured tail is 7.9× smaller than the haircut.

The tail is not zero in history: one prior stale-ask entry went `PAR_REFUND` to
$0.0005 for **−$350.64** (`docs/audit-cycle1.md` S1).

### 2.3 What binds it: capital-days, not edge

**Diagnosed root cause of the 97 % idle.** The pillar could only see a market
once UMA emitted a `ProposePrice`, and that proposal arrives **p50 30.6 h after
the market's `endDate`** (p95 39.3 h; 98.2 % after), while the book is gone
**p50 2.23 h / p95 7.30 h after `endDate`**. So every closing-soon market was
invisible for the ~23 h in which it was still tradeable and unpriceable
afterwards. Of 10,028 open markets closing within 24 h, **9,069 (90.4 %)** are
already below 0.99 — the pillar was seeing **2** of them.

Fixed by staging the closing-soon cohort: the radar now asks Gamma for its own
`end_date_min`/`end_date_max` window using the *measured* p95 close span
(7.30 h) and the measured tape liveness, ingests those rows, and binds
question→condition-id every cycle (64 ms over 3,529 questions). It derives **no**
price for an unresolved market — `verdict_for` still refuses without a proposal.

| | before | after |
|---|---|---|
| pillar census | **2** conditions | **3,531** staged (0.02 % → 35.2 % of the 24 h population) |
| priced with both sides | 2 | 3,142 below par of 3,529 |

At the measured 4.91 % gross and a 2.23 h median hold, $1,000 turns ~10.8×/day;
funding 10 conditions/day is ≈ $361/day net = **0.36 $/$/day, 3.9× the hurdle**.
Capital is not the binding constraint once coverage is fixed — ladder depth and
the tradable share are.

| Cadence | Return per capital-day | vs 0.0924 hurdle |
|---|---|---|
| Model's 2 h cycle (`WIN9_SETTLE_SEC`) | 0.4566 $/$/day | **4.9× pass** |
| **Observed** (1 cycle / 12.578 h, n=1) | **0.0726 $/$/day** | **fails by 21 %** |

Cash sat at $1,000 for **950 of 1,033 snapshots — 97.1 % idle**. The scanner saw
**1.56 verdict candidates per beat**, 32 % of beats had ≥1 +EV, and exactly
**1 condition** was funded in 12.6 h. The population is not the constraint:
~6,733 resolves/day, ~267/day resolved-but-pending. The constraint is that the
radar only surfaces 1.56 cids per beat.

### 2.4 Verdict

**The edge is real and measured; the capacity is not.** +4.91 % gross per cycle
on real fills is a genuine, fee-and-gas-inclusive return. It fails its hurdle
only because capital turns 1.7 days per dollar and sits idle 97 % of the time.
Fixing cadence — not pricing — is what makes this profitable.

## 3. CtfMinter

### 3.1 Mechanism (a): complete-set minting — dead

`data/derived/ctf_census/census.json` (full universe, two snapshots six minutes
apart):

| Key | Value |
|---|---|
| `priced_markets` | 76,097 |
| `gross_positive` | **0** |
| `sweep_positive` | **0** |
| `markets_at_or_below_par` | **0** |
| best `net_per_set` | **−0.001000** |

Structural cause, verified level-by-level (115 levels, 25 markets, max deviation
**0.0**): the YES and NO books are one book mirrored, `ask_NO = 1 − bid_YES`, so

$$\text{ask}_Y+\text{ask}_N = 1+(\text{ask}_Y-\text{bid}_Y)\ \ge 1 .$$

Buying both asks *is* buying at the ask and selling at the bid: it pays the
spread. There is no second book to arbitrage.

### 3.2 Mechanism (b): maker parity bundles — dead, different reason

Top-of-book `bid_YES + bid_NO` over 3,313 two-sided conditions (hour 10
partition, 100,987 levels): p50 **0.96**, p25 0.83, p95 0.99, **≥1 in 2 of
3,313**. That looks like a lock but is the mirrored spread again:

$$\text{bid\_sum} = 1-(\text{ask\_sum}-1).$$

The gate improves each bid one tick, so `locked = (ask_sum−1) − 2·tick`. Gate
replay over **2,172 real two-sided books**:

| Outcome | Count |
|---|---|
| `pair_not_under_par` (tick cost consumes the lock) | 547 |
| `depth_below_min_order` | 11 |
| `single_leg_below_measured_cost` | **1,219** |
| EV > 0 | 395 (18.2 %) |
| **accepted (shadow-yield pass)** | **0** |

Best EV/set among the 395: p50 2.46e-5, max **8.50e-5**. Annualised, p50
0.0056 $/$/day, **max 0.0589 $/$/day** against the 0.0924 hurdle.

Fill risk makes it worse: `P(exactly one leg) = 2p(1−p)` = **0.0210** vs
`P(both) = p²` = 1.13e-4 — the naked-leg branch is **186× more likely than the
lock**. And gas only falls below 1 % of edge at **≥226 shares**, against a
median best-bid depth of **40 shares**.

### 3.3 Verdict

**Not profitable on either mechanism, with 0 quotes accepted out of 2,172 real
books.** The historical `soak_trades` rows showing +$1,606.33 (83 merges, 95
broken legs, 2026-11-09) predate the current gate and are the *phantom lock*
that `single_leg_below_measured_cost` now exists to block — they are not
evidence.

One measurement would change (b): `p_fill` and markout are pooled across the
whole tape with **no spread conditioning**, yet the 395 admissible candidates sit
in 0.57–0.74-wide books that the cells never sample. Their EV is an
extrapolation. Closing it is one bounded rebuild of each table (~219 s and
~174 s) with a spread bucket. Until then, (b) is dead on measured inputs, and
the admissible set may be overstated in its favour.

## 4. What is *still* profitable — plainly

1. **SettlementHarvester, per trade.** Measured +4.91 % gross on 8 real fills at
   VWAP 0.9510, fees and gas included. No assumption required.
2. **Nothing else, on independent evidence.** Reward is a formula paying itself;
   CTF is dead on both mechanisms.

The honest ranking by *evidence quality* is therefore settlement first, reward
second (unverified magnitude), CTF last (refuted).

## 5. Profit centres outside the three

Reachability was computed from both entry points over the real AST import graph:
**61 of 198 modules** are live-reachable (`paper.main` and `trading.runner`).

| Candidate | Mechanism | Measured | Verdict |
|---|---|---|---|
| **Kalshi print-lead cancel** | Kalshi trade → pull PM quote | direction agreement **0.696 @1 s vs 0.503 chance** (n=1,143); **0.942** top size decile; reverse 0.227; 7/8 conditions | **Real, but a gate not a centre** |
| Kalshi cross-venue basis | buy cheap venue, sell rich | median \|basis\| **2.45 c** vs PM spread **2.40 c**, Kalshi 1.00 c; peak corr at **lag 0**; 76/233 pairs quoted both sides | **Unmeasured / not obviously capturable** |
| Neg-risk basket (illiquid tail) | buy all NO when Σask < k−1 | liquid: 26 genuine anomalies, net median **−1.31 c**, duration 20 s; A2: 0 runs ≥2 windows | **Refuted where measured** |
| 5-min crypto Up/Down | price off Binance feed | 62 markets, $277,852 volume — **zero rows recorded** in `clob_events` | **Not measurable today** |
| Latency sniper | snipe stale quote after Binance jump | 2 trades / 56.5 h; anomaly duration **24–39 ms** vs CLOB RTT **p50 212 ms** (286 ms India) | **Refuted** |
| Complement quoter | quote both legs, merge | 110 genuine, net median +0.31 c, duration **24 ms**; duplicate of pillar 3 | **Refuted standalone** |
| Cross-venue news/whales/deribit | lead the news | 0.5 % of 1,022 articles resolve, effect 0 ±5 bps; whale median arrival **6 h 26 m**; Deribit 6 bps vs 400–600 bps cost | **Refuted** |
| On-chain mint/merge flow | front-run merges | merge burst +3.1 bps (t=2.50) vs 0.75 c quote offset; transport **13.5 s** | **Refuted as alpha** |

**Answer: no.** There is no profit centre outside the three that survives
measured data. There is exactly one real, measured, exploitable signal —
Kalshi print-lead — and it is worth ~zero standalone because it fires in 8
conditions at a 2 s cadence; its value is toxic-flow avoidance inside the reward
pillar, where it should be wired as a cancel trigger.

Two candidates remain genuinely unmeasured and are cheap to close:

| Candidate | Bounded measurement | Cost |
|---|---|---|
| Kalshi basis | `report_kalshi_hf_basis.py` per sealed hour + missing markout arm | ~4 s/hour, 2.9k rows; 7 days ≈ 11 min |
| Neg-risk illiquid tail | one DuckDB query on one hour partition, `event_type='book'`, YES assets of the 333 k≥3 events, count Σbid > 1+fees and Σask < 1−fees with duration | **25–60 s**, a few thousand rows |

## 6. Latency reality (applies to all of the above)

Measured from the current host (India), `data/derived/local_latency*.json`:

| Endpoint | p50 | p95 | loss |
|---|---|---|---|
| CLOB `/time` | 286–297 ms | 363–401 ms | 0–8 % |
| CLOB `/books` | 290 ms | 353 ms | 0 % |
| Gamma `/markets` | **720–765 ms** | 852–986 ms | 1 % |
| Polygon RPC | 424 ms | 559 ms | 0 % |
| persisted `clob_rtt.json` | 212 ms | — | — |

Consequences that are already priced into the verdicts above: any strategy whose
opportunity half-life is shorter than ~300 ms is unusable from this host
(latency sniper: 24–39 ms; complement quoter: 24 ms). Settlement and reward
making are latency-tolerant and are not affected. Gamma at 720 ms dominates
universe-walk cost.

## 7. What would change these answers, ranked

1. **One real reward epoch reconciled against venue payout** — converts the only
   +39.5 %/day claim into either a measurement or a refutation. Highest value.
2. **Settlement cadence** — the radar surfaces 1.56 cids/beat and funds 1 per
   12.6 h. At 2 h cadence the pillar returns 4.9× its hurdle; at observed
   cadence it fails by 21 %. This is a pipeline-rate problem, not a pricing one.
3. **Spread-conditioned `p_fill` / markout** — one bounded rebuild each; decides
   whether CTF (b) is merely dead or dead-and-mis-measured.
4. **Kalshi basis with a markout arm** — 11 minutes of probe time to settle
   whether the 2.45 c median basis is real and capturable.
5. **Neg-risk illiquid tail** — one 25–60 s query to close the last measured gap.
