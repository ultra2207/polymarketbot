# Working thesis: the top decile is an EV trader, not a market maker

Recorded 2026-11-02, from the operator. This is the hypothesis the next round of analysis
tests. It is written down before the test so it cannot be rationalised after.

## The claim

The profitable top decile does not earn a spread by quoting both sides. It enters a bet when
the **cost of entry is below its calculated EV**, and exits — often reversing to the other
side of the same market — when that calculation flips. Holding a position to resolution is
rare. The edge is therefore in the **EV calculation itself**, i.e. in detecting mispriced
bets, not in providing liquidity.

## Why it matters

If true, the replicable object is a *pricing model*, not an execution strategy. That is a
completely different research program from market making: it says the money is in knowing
when 0.40 is worth 0.55, and that the winning wallets differ from losers by the quality of
that estimate — not by speed, size, or rebate access.

## The exact decomposition that tests it

For any wallet and token, total profit is identically:

```
profit = (sell_cash - buy_cash) + payout * (buy_sh - sell_sh)
```

and, since `buy_cash = b * vwap_buy` and `sell_cash = s * vwap_sell`, it can be rewritten
with no approximation as:

```
profit = ENTRY_EDGE + EXIT_EDGE

ENTRY_EDGE = b * (payout - vwap_buy)     -- did they buy below fair value?
EXIT_EDGE  = s * (vwap_sell - payout)    -- did they sell above fair value?
```

Both terms use the *realised* payout as the fair-value proxy. That is a choice with a known
bias — payout is only known ex post — so the two terms must be read as an accounting split,
not as evidence of foresight. The foresight question is answered by **dispersion**, below.

### What each hypothesis predicts

| | Market maker | EV trader (this thesis) |
|---|---|---|
| Carry to resolution | small; residual ≈ 0 | small too — they exit, they do not hold |
| ENTRY_EDGE per share | ≈ half the spread, ~1–2 ¢ | typically ≫ half the spread |
| ENTRY_EDGE vs EXIT_EDGE | roughly equal (symmetric quoting) | **entry dominates**; exit is de-risking |
| Dispersion across tokens | low; many small wins, few losses | **high**; conviction bets, fat tails |
| Win rate | near 50 %, small | closer to the price itself, but larger wins |
| Edge vs price | flat across price bands | concentrated where mispricing is largest |

The discriminator is **entry vs exit**, not carry. A market maker must earn on both legs;
an EV trader earns on the entry and uses the exit to flatten, often at a worse price than
entry, which is why their realised spread can even look negative.

### Falsification

The thesis is dead if, for the top decile:

1. `ENTRY_EDGE ≈ EXIT_EDGE` (symmetric quoting), or
2. `ENTRY_EDGE` per share is within one half-spread of zero, or
3. PnL per token shows low dispersion with a ~50 % win rate.

If instead entry dominates and dispersion is high, the next question is *what they know*:
which observable features at entry time predict the eventual payout better than the price
does. That is the EV model, and it is a modelling problem on data we already hold.

## What is already known, and must not be contradicted

- PnL is monotone in wallet size: top decile +0.192 ¢/$, every other decile negative
  (`docs/results-log.md`).
- The top decile carries 91 % of maker volume, so "top decile" and "the market" are nearly
  the same set.
- Aggregate PnL is zero by conservation — this is verified, and any new metric must satisfy
  it.

## Data required

Held: `data/processed/wallet_token/*.parquet`, role-split (maker/taker) per (wallet, token,
month). Enough for ENTRY_EDGE / EXIT_EDGE, carry, and dispersion.

Not held: order-level time ordering within a token, which is needed for "exit and reverse"
as a sequence rather than a net position. That is a tape-level question and costs one pass.

## Verdict (first pass, 2026-11-02)

**Supported, with a correction.** The round-trip PnL for the top decile is +$191.98 m and
the settlement of what it did not close is −$141.13 m. So the profit is taken by exiting,
and what is carried is a cost — the operator's reading survives.

The correction: the edge is **entry-dominated** (87 % of it) while the exit contributes
almost nothing, and the **token win rate is identical** to the losing deciles (54.0 % vs
53.9 %). So this is not a forecasting edge — it is paying less for the same outcomes. An EV
model that predicts winners would show up as a win-rate or win/loss gap; there is none.

That sharpens the research question from "what do they know?" to **"how do they price the
entry?"** — because they are not winning more bets, they are buying the same bets cheaper.

Not yet explained: 23 % of top-decile flow is `sell_only` (minting and selling one leg).
That is market making rather than EV trading, and both behaviours live in the same wallets.

## Correction and refinement (2026-11-02, after the disposition test)

The entry/exit split reported earlier is payout-dependent and was withdrawn — see
`docs/results-log.md`. The load-bearing facts are now:

- Top decile: +$192 m captured on round trips, −$141 m on positions never closed.
- Exits are taken in profit 56.2 % of the time vs 47.8 % if held (+8.4 pp) — a real but
  modest tilt toward closing winners, not a clean disposition effect.
- The loss sits in **50.5 M never-sold positions averaging −$2.94 at a 50.0 % win rate**,
  not in a few large losing holds.

So the operator's mechanism is half-supported: they do take profit early, but the money is
lost in a long tail that is never dealt with at all, rather than in big positions they
refuse to cut.
