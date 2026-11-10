# Fees

Model: `src/polymarketbot/fees.py` · Config: `configs/fees.yaml` · Tests: `tests/test_fees.py`

## Formula

```
fee = C × rate × (p × (1 - p)) ^ exponent
```

- `C` = shares traded, `p` = price
- **Makers pay nothing.** Only takers pay.
- Fees round to **5 decimal places**; anything below `0.00001` becomes zero.
- The curve is symmetric around 50¢: a trade at 30¢ costs the same as at 70¢.

## Current rates (from 2026-07-10)

| Category | Taker rate | Maker rebate |
|---|---|---|
| Crypto | 0.07 | 20 % |
| Sports | 0.05 | 15 % |
| Finance / Politics / Tech / Mentions | 0.04 | 25 % |
| Economics / Culture / Weather / Other | 0.05 | 25 % |
| Geopolitics | 0 | — |
| Combos | 0.05 | 0 % |

At `p = 0.5`, 100 shares of crypto cost **$1.75**; sports **$1.25**; finance/politics/tech **$1.00**.

## History

| Date | Change |
|---|---|
| before 2026-01-05 | fee-free |
| 2026-01-05 | taker fees on 15-minute crypto markets. Gamma shows `crypto_15_min` / `crypto_fees` as **rate 0.25, exponent 2**, i.e. `0.25 × (p(1-p))²` = 0.015625 per share at 50 % (the documented "peak 1.56 %") |
| 2026-02-12 | 5-minute crypto markets, same curve |
| 2026-02-18 | sports fees on **NCAAB and Serie A only** (legacy `sports_fees`, rate 0.0175) |
| 2026-03-06 | crypto fees extend to all crypto markets (new markets only) |
| 2026-03-30 | **Fee Structure V2** — per-category rates; geopolitics stays free |
| 2026-03-31 | fees must be read from the market's `feeSchedule` |
| 2026-04-28 | V2 cutover — fees set at match time, `feeRateBps` removed from orders |
| 2026-07-10 | sports 0.03 → 0.05; sports rebate 0.25 → 0.15 |
| 2026-10 | `sports_fees_nfl_cfb_oct26` reappears at 0.03 for NFL/CFB — a per-league exception the category table cannot express |

Source: `docs.polymarket.com/trading/fees` and `/changelog`, snapshotted to
`data/raw/docs/`.

## Authority: per-market `feeSchedule`

The changelog is explicit that the market object is authoritative. Gamma returns:

```json
"feeType": "sports_fees_v3",
"feeSchedule": { "rate": 0.05, "exponent": 1, "takerOnly": true, "rebateRate": 0.15 }
```

Observed `feeType` values include `sports_fees_v2` (rate 0.03, rebate 0.25) and
`crypto_fees_v2` (rate 0.07, rebate 0.20). **Prefer this over the era table**: it is
per-market and survives exceptions the date-based schedule cannot express.

`FeeSchedule.from_gamma(...)` parses it directly.

### The measured table

`python -m polymarketbot.tools.validate_fee_model` compares the era fallback with Gamma across **1,937,137
markets**. The 16 types below cover the entire scheduled universe, each with exactly one
`(rate, exponent, rebate)` triple:

| `feeType` | rate | exponent | rebate | markets | first created |
|---|---|---|---|---|---|
| `crypto_fees_v2` | 0.07 | 1 | 0.20 | 692,099 | 2025-06-03 |
| `sports_fees_v3` | 0.05 | 1 | 0.15 | 511,187 | 2026-07-10 |
| `sports_fees_v2` | 0.03 | 1 | 0.25 | 372,270 | 2025-06-19 |
| `weather_fees` | 0.05 | 1 | 0.25 | 140,225 | 2025-11-12 |
| `crypto_fees` | 0.25 | **2** | 0.20 | 69,676 | 2026-03-02 |
| `crypto_15_min` | 0.25 | **2** | 0.20 | 38,169 | 2026-01-05 |
| `finance_prices_fees` | 0.04 | 1 | 0.25 | 31,682 | 2024-12-31 |
| `politics_fees` | 0.04 | 1 | 0.25 | 18,031 | 2025-01-03 |
| `culture_fees` | 0.05 | 1 | 0.25 | 17,704 | 2025-04-07 |
| `zero_fees` | 0.00 | 1 | 0.00 | 15,207 | 2025-09-17 |
| `mentions_fees` | 0.04 | 1 | 0.25 | 11,482 | 2025-12-01 |
| `sports_fees` | 0.0175 | 1 | 0.25 | 9,842 | 2026-02-18 |
| `tech_fees` | 0.04 | 1 | 0.25 | 5,150 | 2025-05-22 |
| `economics_fees` | 0.05 | 1 | 0.25 | 2,712 | 2025-09-23 |
| `sports_fees_nfl_cfb_oct26` | 0.03 | 1 | 0.15 | 1,091 | 2026-08-21 |
| `general_fees` | 0.05 | 1 | 0.25 | 610 | 2025-05-02 |

The era fallback agrees with it on rate for **87.1 %**, exponent **98.9 %**, rebate
**88.6 %**. The disagreements are not noise — they are the three things a date-only table
cannot express:

1. **`feeType` is stamped at market creation.** A 5-minute up/down market created 2026-03
   reports `crypto_fees` (0.25, exponent 2) while its May twin reports `crypto_fees_v2`
   (0.07). Both exist in the data. The type therefore dates *the market*, not the fill.
2. **`sports_fees` 0.0175 from 2026-02-18** (NCAAB/Serie A) — the era table has no sports
   rate before 2026-03-30, so it prices 9,842 markets at zero.
3. **`sports_fees_nfl_cfb_oct26` 0.03** — below the 0.05 sports rate.

### Point in time: `schedule_at(row, when)`

`schedule_from_market` answers "what does this market charge **now**". A backtest needs
"what did it charge **then**", and Gamma publishes only the current schedule — so a 2024
crypto market shows the 2026 rate and an unguarded backtest charges fees that were never
collected. `schedule_at` is the function a simulator must call:

```python
from polymarketbot.fees import schedule_at, taker_fee
sched = schedule_at(market_row, fill_ts)          # row from markets_pit
fee = taker_fee(shares, price, sched)
```

Cascade: known `feeType` (gated on its `effective_from`) → category era gating *whether*
fees applied, with the per-market rate supplying *how much* → category era alone.

Known bias, deliberately conservative: a long-dated crypto market is charged 0.07 from
2026-01-05, whereas the changelog extended crypto fees to 1H/4H/daily/weekly markets only
on 2026-03-06. That over-charges at most ten weeks of one family and never under-charges —
under-stating fees inflates every measured edge, so over-stating is the safe error.

## Caveats

- **`exponent` is part of the curve, not a rounding detail.** The 2026-01 crypto pilot is
  `rate 0.25, exponent 2`. It has the same 50¢ peak as `rate 0.0625, exponent 1` but a very
  different shape away from the centre: at 10¢ the pilot charges **0.2025** per 100 shares
  versus **0.5625** under the exponent-1 approximation. Getting this wrong overstates early
  2026 crypto costs by ~3× away from 50¢.
- **Per-league overrides exist.** `sports_fees_nfl_cfb_oct26` charges 0.03 while the sports
  category is 0.05. The category table cannot express this; the per-market `feeSchedule`
  can.
- **Rebates are a per-wallet entitlement, not aggregate maker-side flow.** The
  `rebateRate` on a market's `feeSchedule` is the share of that market's taker
  fees paid out to makers; the venue then splits that pool by
  `rebate = (your_fee_equivalent / total_fee_equivalent) * rebate_pool`, computed
  **per market**, paid daily in pUSD to the maker address, with a **$1 minimum**
  accrual before any payout
  (`docs.polymarket.com/programs/maker-rebates`). So the table tells you both the
  pool size *and* — once you know your share of filled maker flow in that market
  — your own entitlement. `fees.maker_fee_rebate` prices one fill from the
  published share, which is exact only when the maker is the sole filler; the
  authoritative number is the wallet's own
  `GET /rebates/current?date=&maker_address=`, which returns
  `rebated_fees_usdc` per `condition_id` (an array, or `null` when there are no
  rows). `LiveClobClient.get_current_rebates` reads it and the paper ledger
  credits that number when it is available, falling back to the model only when
  the endpoint is unreachable.
- **Rebates are funded by taker fees, so a 0 %-fee market pays no rebate** for as
  long as the fee is zero (including fee holidays), even though the maker's
  resting order may still earn liquidity-reward points. The two programs are
  separate: rewards come from the market's reward pool, rebates from its taker
  fees.
- The **realised `taker_fee` check against `orderfilled.parquet`** (127 GB) is no longer the
  primary validation route: 100 % of the 2024+ traded universe now has an observed per-market
  schedule from Gamma, which is the same source the exchange quotes from. The 127 GB file
  would only confirm that the quote matched what was charged. It stays available as a
  secondary audit, not a blocker.

## Validation

`tests/test_fees.py` asserts the formula reproduces **every row** of the published tables for
crypto (0.07), sports (0.05) and the 0.04 group, plus symmetry, rounding, era transitions, the
geopolitics exemption, the point-in-time gate (a 2025 fill is free), the February sports
introduction, and a contract test that the era table's end state equals the observed
`feeType` rates — failing loudly if the two ever disagree.

Cross-check against ~35 k enriched markets (Gamma metadata vs our slug-derived category):
the fee family agrees with the category for ~96 %. The largest divergence is 211 markets we
label `crypto` that Polymarket charges as `finance_prices_fees` (price/ticker markets). This
does not misprice anything — the per-market `feeSchedule` wins — but it means category-based
segmentation and fee-based segmentation are not identical.

## Taker Rebate Program — modelled, and immaterial at our size

`polymarketbot.fees` now models the rebate: `weighted_volume`, `taker_tier`,
`taker_rebate`, `turnover_needed_for_tier`.

**It went live 2026-05-28**, so it cannot have affected anything before that date, and a
rebate applies only to trades made *from the moment a tier is reached* — it is a property of
the account's history, not of the market.

    wV = Trade Size x (1 - Entry Price) x Category Weight x Bonuses

| Tier | 30-day wV | Rebate |
|---|---|---|
| None | < $2,000 | 0 % |
| Bronze | $2,000 | 3 % |
| Silver | $20,000 | 8 % |
| Gold | $200,000 | 18 % |
| Platinum | $1,000,000 | 32 % |
| Diamond | $4,000,000 | 44 % |
| Obsidian | $10,000,000 | 50 % |

### The correction is real but small

Crypto charges `0.07 x p(1-p)`, i.e. **1.75 ¢ per share at the 50 ¢ peak**. A rebate is
levied on that fee, not on notional:

| Tier | Rebate on a 1.75 ¢ fee | Share of a 2 ¢ round trip |
|---|---|---|
| Bronze (3 %) | 0.053 ¢ | 2.6 % |
| Silver (8 %) | 0.140 ¢ | 7.0 % |
| Gold (18 %) | 0.315 ¢ | 15.8 % |
| Platinum (32 %) | 0.560 ¢ | 28.0 % |

Turnover needed to reach a tier at **$2,000 capital** in crypto at p = 0.5 (the most
generous category, weight 2.3):

| Tier | Turns of 30-day capital |
|---|---|
| Bronze | 0.9 x |
| Silver | 8.7 x |
| Gold | **87 x** |

So a small account reaches Bronze almost immediately and Silver only with heavy turnover;
Gold and above are unreachable without the capital that makes the rebate unnecessary.

**Consequence for the results log:** the correction does not revive a single measured
verdict. The largest dead screen (A5 lifecycle) showed a 0.87 ¢ gap against a 1 ¢
half-spread; a Bronze rebate of 0.05 ¢ closes 6 % of it. A7's drift of 7–11 ¢ was never
fee-limited. The warning in `alpha-pipeline.md` was directionally right and quantitatively
negligible — which is worth knowing precisely, because it means no screen needs re-running
on a rebate-adjusted basis.
