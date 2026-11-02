# Hypothesis

## The claim, in the user's words

> Existing algo bettors are on sports and they are not very successful. The raw quantity of
> data needed, the competition from existing algo bettors with years of trying, and real
> institutions make that untenable.
>
> In crypto you could theoretically track everything — news, Fed interest rates, tweets from
> famous people — but my hypothesis is that all of that will be almost instantly reflected in
> the underlying crypto data. Institutional crypto traders are far more sophisticated and
> numerous than institutional crypto algo bettors.
>
> So the goal is **not** to find an edge in crypto itself (going up against large
> institutions). Instead it is to take in the crypto data and find a **timing edge, a
> mispricing, or some other edge** by analysing all the data.

## Restated as a research proposition

1. **Sports is out.** Not because the pool is small ($423 M/7 mo, 64 wallets > $1 M) but because
   the information required to compete — team news, lineups, travel, local knowledge — is
   expensive, unstructured, and dominated by operators who have been doing it for years. Our
   own evidence supports this: sports persistence is 0.093, and an Elo model built from the same
   outcomes the market prices loses 2–4 %.

2. **Do not forecast crypto.** Any public information — macro, news, social — is impounded in
   the spot price before we can act on it. Institutions with more capital, better data, and
   lower latency own that game. Attempting to predict BTC's direction is the wrong contest.

3. **The edge, if it exists, is in the prediction market's *representation* of the crypto data.**
   Polymarket's up/down contracts are derivatives written on a price path that is fully
   observable in real time. That creates a different kind of question than "where is BTC
   going": given the current spot price, the time remaining, and the realised volatility, what
   is the correct probability — and does Polymarket price it correctly?

   This is a **measurement** problem, not a forecasting problem. The information is public and
   already in the data. We are not competing with institutions on knowing things; we are
   checking whether a smaller, less sophisticated venue has priced a derivative of their data
   correctly.

## Testable implications (each falsifiable)

| # | implication | test |
|---|---|---|
| H1 | Polymarket's implied probability deviates from a GBM/digital-option price computed from spot + realised vol | compare model to market at every snapshot; measure edge conditioned on disagreement |
| H2 | The deviation is a **timing** effect, not a level effect: the market lags the spot after a move | cross-correlate spot returns with market-price changes at lags 1s…5min |
| H3 | The deviation concentrates near expiry, where the digital's gamma is highest | edge by seconds-to-close |
| H4 | The deviation is larger in thinner contracts (5-min) and thinner coins (DOGE, XRP, HYPE) | edge by contract length and by asset |
| H5 | The market's implied volatility is stale — it does not update with realised vol | compare implied vol (from market price) to trailing realised vol over time |

## The decision rule

If H1 is true and the edge survives 2× costs, the strategy is: price the contract from the
underlying, trade the disagreement, size to liquidity. If H1 is false, the venue is efficient at
this and the honest answer is to stop — which is itself worth knowing before any capital is
committed.

## Data in hand

- **Polymarket derivative prices**: `aliplayer1/polymarket-crypto-updown` — 9,231 BTC 15-min
  markets and 21,226 BTC 5-min markets with full CLOB price history (Dec 2025 → Apr 2026).
- **Underlying**: Binance `btcusdt` and Chainlink `btc/usd` spot feed (26 Mar → 26 Apr 2026),
  and spot-on-fill in the tick files for the earlier months.
- **Outcome**: our own `market_labels.parquet`, which joins to the HF markets at **100 %**.
- **Microstructure** (later, if needed): `polyorderbooks` 1-second L2 for short windows.

## What would make me abandon this

- The model and the market agree to within costs at every horizon and every asset.
- Any apparent edge disappears once the fill is priced at the ask rather than the mid.
- The edge exists only in windows too short or too illiquid to deploy capital into.

Stated now so it is not rationalised later.
