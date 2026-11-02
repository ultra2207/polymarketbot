# Alpha register

Every idea examined, with its measured status. Statuses:

- **FALSIFIED** — tested, no edge (or the opposite sign). Do not revisit without new data.
- **WEAK** — tested, real but smaller than the cost of trading it.
- **PROMISING** — tested, positive and causal, but a material unknown (capacity/queue/infrastructure) remains.
- **CONTAMINATED** — tested, but the measurement was confounded; needs a cleaner definition before it means anything.
- **UNTESTED** — no measurement yet. Reason stated.

Data used: SII tape (`data/raw/sii`, 1.27 B fills), DOME book (`data/raw/dome`, 339 M snapshots, 2025-10-17 → 2026-03-31), HF crypto ticks (5 m/15 m, 2025-10 → 2026-03), Binance spot feed (1 month, 2026-03-26 → 2026-04-26), labels from the `/v2/tokens` reconstruction.

---

## A. Directional / model edges in crypto

| # | Idea | Test | Result | Status |
|---|---|---|---|---|
| 1 | GBM vol model, 15 m up/down | 2.25 M ticks | Spearman(dev, realised−price) ≈ 0; model anti-predictive vs market. "+1.6 pp at 60–300 s" = selection bias | **FALSIFIED** |
| 2 | GBM vol model, 5 m | 11.5 M ticks | Spearman −0.006; "+0.6 pp" = selection | **FALSIFIED** |
| 3 | Cross-coin vol model (ETH/SOL/XRP/DOGE/HYPE) | 1.5 M ticks | Signs inconsistent (ETH +2.9 pp, XRP −1.5 pp) → noise | **FALSIFIED** |
| 4 | Daily "above $X" model | 318 markets, 393 k trades | VWAP 0.5727 vs outcome 0.5818; gap +0.9 pp, **t = 1.87** | **FALSIFIED** |
| 5 | Near-expiry convergence | 1.2 M ticks | −0.9 pp in last 15 s | **FALSIFIED** |

## B. Speed / latency

| # | Idea | Test | Result | Status |
|---|---|---|---|---|
| 6 | Lag at 1 s (average ticks) | 1.7 M obs | corr(dev, next move) 0.053 | **WEAK** — catch-up ≈ 0.24 ¢ vs ~2.3 ¢ entry cost |
| 7 | Lag on large spot swings | 5.1 M ticks | corr **+0.094** at ≥0.1 % 2 s moves; PM moves +0.24–0.29 ¢ in the spot's direction | **WEAK** — 10× below the 2.3 ¢ cost; only a >1 %/s move could clear it, and those are too rare to sample |
| 8 | Co-located sniper ruin of market making | 1 month BTC 15 m, 2.03 M at-touch fills, book-move split | fills after **≥3 ¢** move: 158 k shares, **+1.24 ¢** markout; 1–3 ¢: +0.62 ¢; <1 ¢: +0.41 ¢ | **LOW RISK** — big-move fills are the *most* profitable, not toxic |

## C. Event-market behaviour

| # | Idea | Test | Result | Status |
|---|---|---|---|---|
| 9 | News-reaction momentum | 200 largest markets, 4.8 M bars | corr(1 m ret, next 5 m ret) −0.25 was an artifact of mixing Up/Down tokens | **FALSIFIED** |
| 10 | News-reaction reversal | same | per token, corr **+0.02**, reversal PnL −0.1 ¢ | **FALSIFIED** |

## D. Market making / liquidity

| # | Idea | Test | Result | Status |
|---|---|---|---|---|
| 11 | Earn the quoted spread at the touch | 1 week 2026-03, 7.14 M at-touch fills, by category | positive in **every** family: sports **+$1.76 M/wk** (+0.75 ¢), crypto **+$403 k/wk** (+0.49 ¢), geopolitics +$277 k (+0.86 ¢), politics/weather/economics **+1.6–1.8 ¢**, culture +2.69 ¢ | **PROMISING** — broad and uniform, not concentrated |
| 12 | Maker edge by price (longshot vs favourite) | 1 week 2026-03, 4.17 M sampled book mids vs realised payouts | favourites (≥0.85) **+0.29 pp** underpriced; longshots (≤0.15) **−0.30 pp** overpriced; every bucket within ±0.8 pp | **PROMISING** — FLB is real but ≈½ the half-spread |
| 13 | Queue share / competition | 1 week BTC 15 m sim + auditable repo economics + hedge feasibility | **+$9.14/week** at 2,000-share cap, **−$4/week** at 500, **+$95/week** uncapped; flow **73 % one-sided**; audited capture **+0.10 %** vs markout **−1.01 %**; cohort **67.7 % rebate**. Hedge: complement = same position (not a hedge); perp = local spot covers only **5 days** of DOME | **TESTED, WEAK** — ~zero after unwind + fees; needs a live perp venue |
| 14 | Exploit a specific known maker | 365 makers, 7.1 M fills | top 25 makers all mark out **positive** (+0.04 to +0.91 ¢); negative markout only in small wallets (−1.3 to −2.2 ¢, total −$30…−$92) | **FALSIFIED** — no large exploitable counterparty |

## E. Structural arbitrage

| # | Idea | Test | Result | Status |
|---|---|---|---|---|
| 16 | Oracle-liveness "discount to par" (crypto) | 329 k resolved, winner's **last traded price** (`last_fill_ts` window) | winner last price avg **0.9616**; only **5.7 %** below 0.98; window median 50 m for crypto | **TESTED, WEAK** — ~4 ¢ discount, below the 1.75 ¢ fee + spread |
| 17 | Oracle-liveness "discount to par" (non-crypto) | same 329 k, by category | sports window median **188 m**, politics **1 476 m**; discount still ~4 ¢ on the winner | **TESTED, WEAK** — long windows but the discount does not clear the 5 % fee |
| 18 | Longshot bias (sell overpriced tails) | 1.3 M ticks | longshots 0.89 ¢ → 0.22 %; favourites 99.21 ¢ → 99.63 %; **0.4–0.7 ¢** | **WEAK** — smaller than the 1.75 ¢ fee |
| 19 | Favourite-longshot in the book | 4.17 M sampled book mids, 10 deciles | gap −0.22 to +0.74 pp across deciles; tail split **+0.29 pp** (fav) / **−0.30 pp** (longshot) | **TESTED, WEAK** — present but small; the spread dominates |
| 20 | Monotonicity violations (strike ladder) | 1 day of BTC above ladder, YES side, DOME | 1,748 minute-buckets, 3,169 adjacent pairs, **0 violations** (ask[K1] < bid[K2]) | **TESTED, NO EDGE (1 day)** — widen to more days before closing |
| 21 | CTF split/merge basket arbitrage | 1 BTC-range neg-risk event (11 legs, 7 quoted), 25 minute-baskets | sum_bid max **0.993**, sum_ask min **1.033** → no leg-set is cheap or rich enough | **TESTED, NO EDGE (1 event)** — widen to sports/weather events |
| 22 | TVM discount on long-dated contracts | 2.1 M resolved markets, VWAP vs outcome by horizon | 30–90 d gap **−0.5 pp**, >90 d **+0.15 pp**; no material overpricing | **TESTED, NO EDGE** — the discount is inside the noise |
| 23 | Oracle specification edge cases | 2,113 BTC above markets vs Binance spot at `end_date` | **34/103** near-strike markets (<0.5 %) resolved opposite to Binance (e.g. K $78,000, spot $78,027, payout 0) | **TESTED, WEAK** — the gap is real but a few bps wide; needs the actual resolution source (Chainlink/Pyth) live to trade it |
| 24 | Cross-instrument basis (perp vs PM BTC-above) | Binance mark/index/funding free (mark 78,215 vs index 78,255 = −5.1 bps; ~1 bp/8 h); Deribit reachable; live Nov-6 PM weekly event **$761, 0/11 legs >$10 k** | +180–310 bps PM-vs-spot is stale-print noise on $761 volume | **TESTED, NO EDGE** — market too small to trade |
| 25 | Digital-option skew vs Deribit | Deribit free (`-dC/dK` from `BTC-6NOV26` calls) vs PM Nov-6 ladder | P(>78 k): **0.557 vs 0.558 = 6 bps**; P(>80 k) gap inside Deribit's ±6–12 pp call-spread width; all-in cost 400–600 bps | **TESTED, NO EDGE** — free data, zero actionable skew |
| 26 | DAO quorum / delegated-voting math | Snapshot spaces free (votes need POST GraphQL); Tally key-blocked; Gamma governance sweep | **8 vote-pass markets, ~$452 k** spread over 2020–2025 and 5 venues; **0 quorum-worded markets**; 0 Arbitrum vote markets | **TESTED, IGNORED** — no close-margin series to test |
| 27 | GitHub commit early-signal | GitHub free (60/hr/IP, 5 k/hr with PAT); keyword sizing, 45 'upgrade' + 12 'merge' markets >$10 k but all price/ETF markets; no Fusaka/Firedancer/Uniswap-v4 markets exist | Open-source upgrade markets are absent; ship/launch volume is closed-source | **TESTED, IGNORED** — too thin to map |
| 28 | DeFi solvency / liquidation cascade | Free HF feed probe (subgraph 401, Dune view-only, DeFiLlama liqs Pro-only, Chaos host dead) + Gamma search | No free health-factor feed; only 3 solvency markets ever, **$32.5 k lifetime**, all closed | **TESTED, IGNORED** — data gated AND market too small |
| 29 | Liquidity-rewards harvester | Gamma 2026-11-01 pool + official quadratic score + $1 payout floor | 8,000 pools, **$92,634/day**; top $3,000/day (200 sh, 3.5¢). Score is **quadratic** — quoting *at* max spread scores 0; a 1¢ quote is ~0.73 weight. Head pool at 10–100 rivals → **$20–210/day gross**; median $3/day pool splits below the **$1 floor → unpaid** | **TESTED, WEAK** — tail dead by the payout floor; head needs live quoting + hedge, not history |

---

## The one line that matters

After 28 ideas, **none is untested**. **No directional or model edge survived.** The two remaining positive results are both liquidity provision: #11 earns the touch spread in **every** market family (sports +$1.76 M/wk, crypto +$403 k/wk), and #12 finds the FLB is real but ~0.3 pp — about half the half-spread. #13 shows inventory, not queue, is the binding constraint: at a realistic cap the strategy degrades to ~$40/month pre-cost and ~zero after unwind and fees.

The five external-data branches (#24–#28) were checked directly this session: Deribit and Binance are free but the matched live market is **$761** (#24/#25), Tally is key-gated and governance markets total **$452 k across five years** (#26), open-source upgrade markets do not exist (#27), and no free health-factor feed exists (#28). Each is closed on **data gated or market too small**, not on an unresolved test.

The one live lead that is not dead is the **head reward pools** (#29) — $20–210/day gross on the $3,000/day market before adverse selection, but it needs a live quoting + hedge test, because the DOME cannot backtest November markets and the tail pools fall below the $1 payout floor.
