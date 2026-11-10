# Minimum live capital for the liquidity-rewards harvester

**Question (2026-11-11, one day before the Friday live start):** below what bankroll is going live with the reward harvester counterproductive, and which constraint sets that number?

**Answer: `$350`.** Derivation: `$299` — the position size that clears the venue's $1.00-per-pool payout floor at the *median* of the 36 head pools the bot's own screen would accept, sized so that the measured in-band competing score can **double** and the pool still pays — plus `$52.54`, the reserve the repo's own allocator holds around such a position. Below `$20` nothing at all is possible (no qualifying order); below `$200` only 14 of those 36 pools can be funded and every position sits exactly on the payout cliff. **The input that would change it is the measured competing score `D` of the pool you pick — the answer is linear in `D` and falls as `1/(pool − $1)`.** At the measured `D` of the fattest pool (`D = 1.5e6`) the same rule asks for `$3,664,260`; at the leanest (`D = 33`) for `$20`.

Everything below is tagged: **[M]** measured in this run (query named), **[D]** derived from a named protocol constant or a definitional identity, **[P]** a policy parameter of this repo (named, not measured), **[A]** a stated stress level / assumption. No number is a default.

---

## 1. The binding constraint, in one sentence

**The $1.00-per-pool daily payout floor binds** — it sets a higher capital requirement than the venue's minimum order size in 33 of the 52 head pools measured, and it is what the recommended `$350` is derived from; gas never binds (a merge costs `$0.0011`, i.e. **0.11 % of the $1 floor, 922× of headroom**) and dilution cannot bind below `$10^6` in a single pool.

The month's second-order constraint is the **venue minimum lot** (`rewards_min_size`), which binds in the other 19 of 52 and sets the absolute floor of `$20`: 7,941 of 10,973 reward markets publish `rewards_min_size = 20` shares, and a pair costs `≤ $1.00` (`[D]`, a binary pair redeems for exactly $1.00), so the smallest qualifiable position in the universe is `≤ $20`.

---

## 2. Every constraint, measured, and whether it binds

| # | Constraint | Measured / derived value | Source | Binds? |
|---|---|---|---|---|
| 1 | **Fixed cost per capital cycle (merge/redeem gas)** | `$0.001085` per merge at 160.08 Gwei, POL `$0.08691` **[M]** — `149–162 Gwei` across four runs this session | `estimate_merge_cost_usd()`, `polymarketbot.strategy.onchain.ctf.gas`; 78,000 gas units is the repo's documented `mergePositions` budget **[D]** | **No.** 0.11 % of the $1 floor (922× headroom); 0.0056 % of the smallest viable position; the position at which one merge equals a full day's *marginal* reward is `≤ $3.29` versus a `$20` lot. The repo's own threshold `calculate_optimal_merge_threshold` returns its floor of **1.0 complete set** — gas cannot even force batching. |
| 2 | **$1.00-per-pool payout floor** | Requires a score share of `1/R` of the pool; measured capital to clear it: p10 `$25`, p50 `$198` across 36 screened head pools **[M]** | Protocol: *"The minimum reward payout is $1"*, docs.polymarket.com/programs/liquidity-rewards **[D]** | **Yes — the binding one.** 33/52 head pools. |
| 3 | **Minimum order size / tick granularity** | `rewards_min_size`: 20 sh in 7,941/10,973 markets, p90 50, p99 200, max 1,000 **[M]**. CLOB `orderMinSize` 5 sh, tick `0.001` or `0.01` **[M]** (`GET /tick-size`) | `/rewards/markets/multi` payload; `GET /tick-size?token_id=` **[M]** | **Yes, at the bottom.** 19/52 head pools; sets the absolute `$20` floor. Tick size decides the *minimum quotable distance* (`0.05¢` at tick 0.001, `0.5¢` at tick 0.01) and therefore `alpha` ∈ `[0.735, 1.000]` measured, which scales every other requirement. |
| 4 | **Quadratic score dilution** | Marginal reward at the minimum viable position: p10 `0.00033`, p50 `0.00679`, p90 `0.15266` `$/$/day` **[M]** — it *falls* with capital | `marginal_payout()` = `R·α·D/(D+αC)²` **[D]** from the published `S(v,s)=((v-s)/v)²·size` | **No, not at this scale.** Marginal reward equals the measured fill cost only at `C ≈ $3.6e6` in the single pool where the fill side is net negative; in the other measured pools dilution cannot bind below `$10^6`. It is a *ceiling*, not a floor. |
| 5 | **Adverse selection / inventory** | Net fill yield at the 60 s horizon: `−0.00032` to `+0.00290` `$/$/day` **[M]**. One-sided-fill probability per day: `0.187` at (tick 0.01, 20 sh), `0.056` at (tick 0.01, 200 sh), `0.083`/`0.013` at tick 0.001 **[M]** | `expected_fill_yield()` over `data/derived/ctf_quote_fill_horizon.json` (8,773,815 placements) and `ctf_maker_markout.json` (44,068 fills) **[M]** | **No — but it is the inventory driver.** Fill *yield* is scale-invariant (both spread capture and markout are per share), so it sets no minimum; the capital it ties up is one leg, i.e. `≈ C/2`, already inside `C`. Its measured size is what the `$52.54` reserve is for. |
| — | **Maker fee** | `0.0000` **[M]** | `configs/fees.yaml` (measured across 1,937,137 markets, Gamma `feeSchedule`) | **No.** Makers pay nothing on the global CLOB; there is no per-fill fixed cost to amortise. |

---

## 3. Derivation of the recommended number

### 3.1 The model

One market, quoted two-sided as `q` shares on each leg at the best bid improved by one tick (never crossing). Then, with `[D]` from the published rules:

```
score per share      alpha = ((v - s) / v)^2        v = rewards_max_spread (cents), s = measured distance from mid
capital              C     = q * pair_cost          pair_cost = bid_yes + bid_no <= $1.00  (binary pair redeems at $1.00)
score per dollar     A     = alpha / pair_cost
our score            m     = A * C
share of the pool    share = m / (D + m)            D = sum of both books' in-band scores ((v-|p-mid|)/v)^2 * size
daily payout         R * share                      floored to $0.00 below $1.00
```

`D` is the *measured* denominator: the venue's own rule applied level by level to both outcome-token books (`GET /book`), summed — the same upper bound `market_competing_score()` uses, hence conservative (our share is understated by `1…1+c`).

### 3.2 Step 1 — the payout floor sets a capital requirement

`R · A·C/(D + A·C) ≥ $1.00  ⟺  C ≥ D / (A · (R − $1))` `[D]`

This is `calculate_min_floor_capital()` verbatim. Note the consequence: **a pool of `$1.00/day` or less can never pay**, and a pool of `$R/day` pays only if you hold `1/R` of its score. That is why 327 of the 10,973 censused markets are unpayable by construction `[M]`.

### 3.3 Step 2 — the lot and the floor together

`C_min = max( rewards_min_size · pair_cost , D / (A · (R − $1)) )` `[D]`

Measured over the 52 priced head pools `[M]`: the lot binds in 19, the floor in 33; `C_min` is p10 `$25`, p50 `$198`, and reaches `$3,018`, `$8,318` and `$11,197` in the fattest books.

### 3.4 Step 3 — the floor is a cliff, not a slope

At `C = C_min` the payout is *exactly* `$1.00`. One new rival, or one snapshot in which `D` is 10 % higher, and the pool pays `$0.00`. A bankroll that only just clears the floor is therefore not a viable position; the position that is viable is the one that still clears `$1.00` after the measured `D` **doubles** `[A: the doubling is a stated stress, chosen so the requirement is not a hand-set multiplier]`:

`R · A·C/(2D + A·C) ≥ 1  ⟺  C ≥ 2D / (A · (R − $2))`  **[D, with the 2 labelled A]**

Measured over the 36 head pools that pass the bot's own mid screen `[M]`:

| | p10 | p50 | p90 | max |
|---|---|---|---|---|
| `C_min` (1× floor, the cliff edge) | `$25` | `$198` | — | — |
| `C_robust` (`D` doubling) | `$50` | **`$299`** | `$6,920` | `$22,451` |

### 3.5 Step 4 — add the reserve the allocator already demands

`calculate_endogenous_reserve()` for one `$299` position at `mid = 0.50` returns **`$52.54`** `[M]`: `0.15 · L2norm(exposure)` + `0.20 · max(exposure)` + a gas buffer of `max($0.20, 100 × gas)` `[P: 0.15 and 0.20 are the two parameters `reserve.py` itself labels unfitted; the $0.20 floor is a hand-set "so contract calls never fail" bound]`.

### 3.6 The number

```
C_robust (p50, screened head set)      $299.00   [M]
endogenous reserve for that position    $52.54   [M]
─────────────────────────────────────────────────
minimum live bankroll                  $351.54   ≈ $350
```

**What breaks below `$350`:** not profitability per dollar (the measured marginal reward is *highest* at small size — p90 `0.153`/`$`/day), but the **set of pools you can enter**. Measured, one position per pool at `C_robust` `[M]`:

| Bankroll | Head pools fundable (of 36) |
|---|---|
| `$20` | 3 |
| `$50` | 6 |
| `$100` | 9 |
| `$200` | 14 |
| `$400` | 20 |
| `$500` | 21 |
| `$1,000` | 26 |
| `$2,000` | 28 |

At `$350` you can hold one median-quality position and have cash for a second activation lot. Below `$100` you are running one or two tail positions in pools where a single new rival zeroes the payout, with no capital to move.

---

## 4. Sensitivity

### 4.1 Pool size: median `$3/day` versus head `$1,000/day`

`C_floor ∝ D/(R − $1)`, so a pool 333× larger needs 333× less capital for the same `D`. Applying the measured `D` distribution of the 258-market sample (`D` p10 `1`, p50 `247`, p90 `36,137`; `alpha = 0.70`) `[M]`:

| Pool | `C_floor` p10 | p50 | p90 |
|---|---|---|---|
| `$3/day` (census median) | `$1` | `$176` | **`$25,812`** |
| `$30/day` | `$0` | `$12` | `$1,780` |
| `$1,000/day` (census head) | `$0` | `$0.5` | `$52` |

And directly measured on the 258 real pools of `$16–585/day` (`data/derived/reward_depth_and_yield_sample.parquet`, 2026-11-07) `[M]`: `C_floor` p10 `$4`, p50 `$38`, p90 `$491`, max `$6,107`.

**Reading:** the median `$3/day` pool is only reachable when it is also *quiet*; if it carries the `p90` competitive depth of its own population it needs `$25,812` to clear `$1` and a small bankroll gets `$0.00` from it. Head pools are the opposite: `$52` clears the floor at the p90 depth. This is the alpha-register #29 finding, now with dollars attached — **the tail is dead by the payout floor, the head is where the floor is cheap.**

### 4.2 Rival count

`C` is linear in `D`, and `D` is linear in rival depth at a given distance. One rival lot = `rewards_min_size` shares quoting at our own distance `[D]`:

| Pool | `D` | = rival lots of `min_size` at our distance |
|---|---|---|
| `$1,000/day` | 110,658 | 753 × 200 shares |
| `$600/day` | 51,125 | 346 × 200 shares |
| `$555/day` | 32,729 | 198 × 200 shares |
| `$510/day` | 1,509,772 | 7,688 × 200 shares |
| `$500/day` | 10,793 | 633 × 20 shares |

**Two more rivals of the same size do not move a `$350` position materially; two more *hundred* do.** The doubling stress in §3.4 is worth roughly "the book gains depth equal to everything already in it", which at these measured `D` values is 200–7,700 lots. Realistically, `D` moves continuously and the `$350` answer has `2×` cover, not `10×`.

### 4.3 What moves the answer, ranked

1. **`D` of the pool you choose** — linear. Pick a pool with `D = 10^4` and `$50` of capital clears the doubled floor; pick the `D = 1.5e6` pool and nothing under `$3.6M` does.
2. **`rewards_min_size`** — the hard floor, `$20`; nothing below it scores.
3. **Pool size `R`** — inverse in `(R − $1)`; the good pools are the large ones.
4. **Gas** — irrelevant by 3 orders of magnitude, and would need `922×` (a `~148,000 Gwei` Polygon, or POL at `$80`) to reach the $1 floor.
5. **Adverse selection** — no effect on the *minimum*; it is per-share, so it scales with capital. It sets whether the position is worth holding, not how much you need.

---

## 5. What the model says the capital earns (with the caveats that matter)

Gross model output at the minimum viable size, no adverse selection, before any pool closure `[M]`:

| Bankroll | Deployed | Model total `$/day` | `%/day` on deployed |
|---|---|---|---|
| `$50` | `$50.00` | `$66.39` | `132.8 %` |
| `$200` | `$198.82` | `$92.31` | `46.4 %` |
| `$350` | `$341.35` | `$105.9` | `31.0 %` |
| `$1,000` | `$925.52` | `$138.49` | `15.0 %` |
| `$2,000` | `$1,989.75` | `$150.15` | `7.6 %` |

Three things stop these from being a forecast, and each is why the recommendation is the *median* pool and not the cheapest one:

1. **`D` is one snapshot.** The universe divides by the mean of up to 1,440 samples per epoch; the highest-yield rows above are pools whose book was momentarily empty (`D = 33`) — the model says you take a quarter of a `$200/day` pool with `$20`, which is true for one sample minute and false once you post, because your own order becomes most of `D`.
2. **The reward is a rate, not a cycle.** It accrues per sampled minute over a UTC day and pays once, at midnight UTC. Capital rotated twice a day earns half. There is no fast-capital-cycle trick here, which is also why the per-cycle gas cost is irrelevant.
3. **Adverse selection is measured at a 60 s horizon** (`+0.0001 … +0.0029` `$/sh` markout) — at 400 s the same table measures `0.0016–0.0020` `$/sh`, i.e. 4–7× larger. A quote left to rest is paid more per fill and keeps more of the pool; the two effects have not been traded off in a live run.

---

## 6. Provenance and how to reproduce

```
uv run python tmp_python/min_live_capital.py --top 60 --refresh   # 12–50 s, ~145 HTTP calls
```

(*without* `--refresh` it reuses `tmp_python/out/reward_census.json`; output above: `tmp_python/out/min_live_capital.log`, machine-readable `tmp_python/out/min_live_capital.json`)

| Claim | Where it comes from |
|---|---|
| pool census, `rewards_min_size`, `rewards_max_spread`, prices | `GET https://clob.polymarket.com/rewards/markets/multi?order_by=rate_per_day&position=DESC&page_size=500`, 22 pages, 10,973 rows |
| tick per market | `GET https://clob.polymarket.com/tick-size?token_id=` |
| in-band competing score `D`, book mid and spread | `GET https://clob.polymarket.com/book?token_id=`, both legs, scored with the published rule |
| merge cost | `measure_gas()` (median of `eth_getBlockByNumber`, `eth_gasPrice`, `eth_feeHistory` p50 tip on two RPCs + the gas-station oracle) × `measure_pol_usd()` (median of Binance/Coinbase/Kraken/OKX) × 78,000 gas units |
| `p_fill`, markout | `data/derived/ctf_quote_fill_horizon.json` (8,773,815 placements), `data/derived/ctf_maker_markout.json` (44,068 fills) |
| 258-market mid-tier sample | `data/derived/reward_depth_and_yield_sample.parquet` (2026-11-07) |
| `$1.00` floor, `S(v,s)=((v-s)/v)²·size`, `c = 3.0`, 1,440 samples | docs.polymarket.com/programs/liquidity-rewards, equations 1, 4, 6 and the "$1 minimum payout" note |
| maker fee `0` | `configs/fees.yaml`, Gamma `feeSchedule` |
| mid screen `0.12 … 0.88` **[P]** | `lp_screen.py` defaults — the bot's own execution rule, not a protocol constant; it excludes 16 of the 52 priced head pools (extreme-price markets) |

### Known defect found while building this

`fill_measurement._fill_table()` sets its `_TRIED` flag **before** loading the table, so the first call from a worker thread in a parallel scan sees the flag set, gets `None`, and the process then reports *every* market's fill side as "unmeasured" for the rest of its life (`stay_fraction = 1.0`, fill yield `0.0`). Reproduced here: the first run priced 6 of 24 markets this way; after warming the cache on the main thread, 50 of 60 price. The live scanner calls `expected_fill_yield` from `state._SCAN_EXECUTOR`, so it is exposed. Not edited here (`src/polymarketbot/strategy/` is out of scope for this task).

---

## 7. One line for the desk

Gas is not the reason to stay small and dilution is not the reason to stay big: **the $1-per-pool payout floor is the only constraint that sets a floor on capital, and at `$350` you can hold one position that survives its competing book doubling.** Everything under `$200` is a single-position bet on a quiet tail pool.
