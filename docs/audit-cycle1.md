# Paper-soak audit — cycle 1

- Window: 2026-11-09 06:31:35 → 07:01:35 UTC (12:01:35 → 12:31:35 IST)
- Command: `uv run python scripts/run_live_paper_soak.py --capital 3000 --duration 1800 --interval 10 --fresh`
- Basis: 160 snapshots, 4,324 CTF fills, 6 settlement entries, 0 reward fills.
- Final dashboard: total +$311.97; RewardHarvester +$5.81; SettlementHarvester +$41.69
  (unrealized mark-to-par); CtfMinter +$264.47.

This report records the three independent audits and the remediation that was applied
before cycle 2. The audits were run in parallel, read-only, with SQLite/log evidence.

## 1. RewardHarvester — critical

**Measured:** `pnl_domain2` grew $5.81 in 30 min with **zero orders and zero `soak_trades`
rows**. The script contains no order-placement call; the pillar is an accrual formula.

| # | Finding | Evidence |
|---|---|---|
| R1 | **No resting order is ever placed.** Rewards are credited from cached pool data only. | `scripts/run_live_paper_soak.py:1075-1099`; no `place_order` in the file; 0 RewardHarvester rows |
| R2 | **The model credits ~92-99.6 % of a pool's budget.** `comp_score` is floored at 25 in the radar and 1.0 in `fetch_market_competing_score`; a failed/empty book gives `share ≈ 0.996`. | `:927-930`, `:1092-1096`, `:616`, `:522` |
| R3 | **Score construction is inconsistent.** `alpha` prices a YES+NO bid pair (each one-sided, 1/3 penalty), but `normalise_two_sided` credits bid+ask (2×). | `:170-183`, `:1109`, `paper_trading/rewards.py:121-139` |
| R4 | `scan_reward_universe` fetched one page only; `max_batch_pages` was inert and the log claimed "full exchange". | `:822-875`, `:967` |
| R5 | Allocator `alpha` used `spread_offset_cents=0.50`; accrual used `min(0.5, 0.25*v)`. | `:166`, `:1079` |
| R6 | Capital could never be released: the $0.50/day hurdle blocked `{}` allocations and disappeared pools; empty radar kept stale allocations accruing. | `:1000-1021` |
| R7 | Budget used `current_equity` with no reserve; `max_active_pools=4` and `search_candidates=12` hardcoded; stale `active_pools` rates. | `:992-1005`, `:288-290`, `:1044-1060` |
| R8 | Page/book failures and allocator exceptions were DEBUG, so a dead radar looked idle. | `:845`, `:1034`, `:545` |

**Remediation applied:** one-sided score penalty (÷3) in `alpha` and accrual; candidate
`spread_offset_cents = min(0.5, 0.25*v)`; sequential pagination until the rate floor or a
short page; page failures at WARNING; unconditional release of stale/disappeared
allocations; empty queue/no-eligible release; 5 % reserve in the reward budget; radar log
no longer claims "full exchange".

**Still open (needs redesign):** R1/R2 cannot be repaired by arithmetic. Either place
simulated resting orders and accrue through `compute_epoch_payout` with 1-minute epochs,
or report `pnl_domain2` as a model output and exclude it from portfolio PnL.

## 2. SettlementHarvester — critical

**Measured:** `pnl_settlement` was +$41.69 from the first 10 s and never changed. Six
`BUY_ASK` fills, 993.34 shares, $950.01 cost, $1.65 fees; zero realized exits.

| # | Finding | Evidence |
|---|---|---|
| S1 | **`clean_ladder` was discarded; the raw ladder was passed to the allocator.** A stale 0.05 ask could absorb the whole budget. Prior history contains a $0.2304 entry → `PAR_REFUND` at $0.0005, **−$350.64**. | `:1399`, `:1306`; `settlement_allocator.py:57-67`; DB id 24196/24197 |
| S2 | **Resolution used live indicative `outcomePrices >= 0.99`, not `closed`/UMA state.** All six held markets were `closed=False`, `uma=None`; a 0.975 print can force-settle an open position. | `:1563-1579` |
| S3 | **Horizons were hardcoded** (`900+7200+180` = 2.30 h) for every market. 15-minute crypto candles were ranked at 11,424-23,884 % ARR. `robust_hazard.calculate_physical_cycle_time` existed and was unused. | `:1198-1219`; `robust_hazard.py:56-115` |
| S4 | `pnl_settlement` includes unrealized par edge while `soak_trades` records only entry fees; the two disagree by the whole edge. Reserve/caps were computed from marked-up equity. | `:322-332`, `:1436-1446`, `:1397`, `:1751` |
| S5 | Allocator `net_profit`/`annualized_arr` omitted taker fees; the fee was hardcoded `0.04`/exponent 1.0 instead of Gamma `feeSchedule`. | `:377-378`, `:550-551`, `:1401`, `:1433`, `:1530` |
| S6 | A position whose market vanished from the Gamma batch was locked forever; batch-poll failures were DEBUG. | `:1504-1508`, `:1499` |
| S7 | `--settle-hold-sec` is stored and never read; slots/caps/reserve/ticket are literals. | `:664-671`, `:704-707` |
| S8 | One `GET /book` per open position per second; the early-exit path ran even when the 0.99 bid was unreachable. | `:1522-1525` |

**Remediation applied:** reject raw ladders below 0.940 and pass `clean_ladder`; require
`closed` + final 0/1 prices (and re-check disputed/rejected); crypto Up/Down candles are
excluded until closed/proposed, and all horizons come from
`calculate_physical_cycle_time`; past-calendar markets more than 2 h stale with no UMA
proposal/closure are rejected as unresolved-in-fact; fees are subtracted in both allocator backends and the
market's Gamma `feeSchedule` is threaded through entry/exit; reserve and caps use realized
capital (`available_cash + locked_capital`); entry/exit latency uses measured RTT.

**Still open:** split realized vs unrealized settlement PnL in telemetry; wire or delete
`--settle-hold-sec`; batch position book fetches; warn/age-out missing markets.

## 3. CtfMinter — critical

**Measured:** 4,318 maker fills in 30 min (2.4 fills/s), 22,226 shares, +$264.47 net,
$40.46 rebates, $10.86 fees. Run-rate ≈ $12,700/day on a $1,000 bucket; zero losing fills.

| # | Finding | Evidence |
|---|---|---|
| C1 | **Both legs fill from one Bernoulli draw at one size; parity is not atomic.** A one-sided fill is unmodelled broken-leg risk. | `:1913-1945` (pre-fix), 2,601/2,601 sample rows |
| C2 | **Hardcoded `tick=0.001` on 0.01-tick markets.** 99.6 % of quoted prices were off the cent grid. | `:1889`, `:1899` |
| C3 | **`max(1.0, …)` size floor is below the 5-share CLOB minimum.** 64 % of fills were unplaceable; `cap_per_quote` was dollars used as shares. | `:1916`; Gamma `orderMinSize=5` |
| C4 | `vol24` (USD) was divided by 5 shares; `inside_touch_share=0.35` captured 35 % of flow with no queue model; unseeded RNG. | `:1906-1916` |
| C5 | Expected profit was credited at decision time; taker path never locked capital; `latency_us=18.0` was fabricated. | `:1845-1861`, `:1957` |
| C6 | Static gas `0.00136/100` assumed a 100-share batch that never formed; two gas constants for one tx class. | `:1929`, `:1599` |
| C7 | Inner `except Exception: log.debug` hid a missing `import random` for a whole commit. | `:1969-1970` |
| C8 | `self.ctf_engine` was dead code (second, divergent CTF engine). | `:695`; zero references |
| C9 | `maker_rebate_share_pct=0.20` is an inert sentinel; missing `feeSchedule` degrades to zero rebate. | `:1923`, `fees.py:142-149` |
| C10 | 2,242 L2 books every 1.5 s feed a depth sweep that produced zero taker trades; one SQLite connection per trade. | `:1820`, `:2051`, `record_trade` |

**Remediation applied:** per-market `orderPriceMinTickSize` / `orderMinSize`; grid-snapped
quotes; reject spreads below the measured −63 bps maker markout; dollar caps converted to
shares; gas charged as one merge tx; rebate uses the Gamma schedule; measured latency;
scanner exceptions at ERROR (rate-limited). **Independent leg arrivals** are now modelled:
each leg fills on its own Bernoulli draw and size, the common size is merged at par, and
any unmatched residual is sold at the current best bid (booking the spread loss and taker
fee) instead of being booked as a risk-free merge. This makes losing fills possible, as
required.

**Still open:** queue-position/flow calibration (the 5-share mean, 35 % inside-touch share
and USD/share dimensional error remain); `self.ctf_engine` is still dead code; the maker
path still has no persistent open-leg inventory (it unwinds immediately). These are the
next-cycle model work.

## Cross-cutting

- Every source file now logs through `polymarketbot.logs`: `data/logs/app.log` plus a
  rotating per-source-file log (`10 MB`, 3 rotations, compressed). Per-file sinks are
  synchronous, removing ~70 enqueue threads from the soak.
- `print()` is gone from `src/` and `scripts/`; the remaining CLI/table output goes through
  `log.info`/`log.error`.
- Telemetry `soak_trades` entry rows still record only fees for settlement buys; use
  `soak_snapshots` for equity and the `details` field for par edge.

## Post-remediation verification (cycle 4)

- **Settlement fee schedule:** a 189.47-share buy at $0.95 recorded a $0.62999 fee; Gamma
  returned `feeSchedule {rate: 0.07, exponent: 1, rebateRate: 0.2}` and
  `189.47 * 0.07 * 0.95 * 0.05 = 0.62999`. The hardcoded 0.04/1.0 charge is gone for that
  market.
- **CTF tick/min size:** sampled cycle-4 markets returned Gamma
  `orderPriceMinTickSize` 0.001 and `orderMinSize` 5.0; recorded prices (`0.985`, `0.986`,
  `0.993`) are exact tick multiples and recorded sizes are >= 5.
- **CTF broken legs:** `CTF_BROKEN_LEG` rows are now produced (e.g. a 10.3-share one-sided
  fill bought at $0.651 and unwound at the $0.650 best bid, fee $0.0951), so the maker path
  can lose. Merged and broken legs are both recorded.
- **Reward universe:** the radar now logs 2,390–2,401 primed pools (previously one page /
  ~330), and reward accrual is materially lower after the one-sided Q penalty.
- **Settlement ladder:** cycle-4 entries are 0.945–0.992, inside the 0.940–0.995 band.

These are point checks, not proof that every market/parameter is correct; the cycle-4
audit is the authority on the remaining model gaps.
