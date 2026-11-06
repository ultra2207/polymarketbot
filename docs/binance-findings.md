# Binance as a signal: what it can and cannot buy

Measured on the collected tape (2026-11-06, hours 04-07). Everything below is a
number from a query, not a model.

## The addressable reward income from crypto is $50/day

| universe | markets | daily pool |
|---|---|---|
| reward universe | 2,152 | **$111,857** |
| of which crypto (BTC/ETH/SOL/XRP/DOGE keywords) | 201 | **$50** (0.045 %) |

Binance cannot improve the ROI of liquidity rewards by any material amount,
because there is almost no liquidity reward on crypto markets to improve. The 201
crypto markets in the reward universe are far-dated price targets ("Will Bitcoin
reach $250,000 by December 31, 2026?"), which is also the class where spot data
matters least.

## The markets where Binance *would* matter are not being recorded

Polymarket runs 5-minute `BTC/ETH/SOL/XRP/DOGE Up or Down` markets. On the
snapshot:

| | value |
|---|---|
| markets | **62** |
| combined historical volume | **$277,852** |
| current liquidity | $49,204 |
| reward rate | **$0.00 on all 62** |
| present in `clob_events` | **zero** |

The collector follows the reward list, and these pay nothing, so none of them is
on the tape. The one market class where a fast spot feed converts into money is
exactly the class we do not subscribe to.

## On the crypto markets we do have, there is no usable lead

Binance BBO is collected for 5 symbols (BTC, ETH, XRP, SOL, DOGE), 28 k rows per
symbol-hour, spanning 4 hours in the test window. PM mid was rebuilt for the five
busiest crypto conditions, as-of aligned to a 10-second grid, and correlated with
BTC mid changes at lags 0-6 bars:

| condition | PM mid sd (10 s) | corr at lag 0 | lag 1 | lag 2 | lag 6 |
|---|---|---|---|---|---|
| 0x6c0b515022 | 0.69 c | **+0.700** | +0.215 | +0.124 | −0.101 |
| 0xb8a08ffe30 | 0.43 c | **−0.497** | −0.319 | −0.113 | +0.148 |
| 0x86c7eaafe7 | 0.42 c | **−0.361** | −0.265 | −0.125 | −0.105 |
| 0xe573a60430 | 0.36 c | **+0.262** | +0.304 | +0.110 | −0.214 |
| 0x426a9048e7 | 0.16 c | +0.009 | −0.115 | −0.016 | +0.121 |

The correlation lives at lag 0 and collapses by lag 1. Polymarket tracks Binance
**contemporaneously** at this resolution; there is no forward window to act in. At
1-second resolution there is a hint of a ~1 s lag (+0.109 against −0.097 at lag
0 on the busiest condition), but that market printed 26 distinct mids in an hour
while BTC moved $4.9 per second - a frozen price cannot be traded on a 1-second
signal.

## Verdict

Binance is a **cost/valuation input, not an alpha source**, for the business as
currently configured:

1. It cannot raise reward income: crypto is 0.045 % of the pool.
2. It does not lead the crypto markets we quote: correlation is contemporaneous.
3. The 62 markets where it could work are unrecorded and pay no rewards.

## What would make it pay - in order of cost

1. **Record the 62 Up/Down markets** (~free: one more subscription). $278 k of
   historical volume and $49 k of resting liquidity in a 5-minute product is a
   directional market-making business judged on markout, not on rewards.
2. **Only then** build a Binance-toxicity gate for that venue, where a 1-second
   lead against a 5-minute binary is worth something real.
3. Do not build crypto signals for the reward universe - there is $50/day behind
   them.
