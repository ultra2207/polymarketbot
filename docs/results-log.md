# Results log

Every screen, including the failures. The point is to stop re-testing the same idea
and to keep the record of what was actually measured, on what window, at what precision.

Format: idea → window → sample → result → verdict → what would change it.

---

## #1 Longshot / favourite bias — **no edge after costs** (2026-11-01)

**Window:** markets created 2024-01-01 → 2026-03-31; entry at T−24 h → T−6 h before
resolution; hold to resolution; on-chain payout labels; fees via `schedule_at`.

**Sample:** 152,895 token-entries across 8 quarters (path
`data/processed/screen_calibration_parts/entry=T-24h..T-6h/`).

| Entry bucket | n | mean price | mean payout | net edge/share | t | 2026 fee |
|---|---|---|---|---|---|---|
| 0.00–0.05 | 6,463 | 0.019 | 0.013 | −0.0066 | −4.8 | 0.16 % |
| 0.05–0.10 | 3,881 | 0.075 | 0.051 | −0.0239 | −6.8 | 0.18 % |
| 0.10–0.20 | 8,509 | 0.152 | 0.128 | −0.0241 | −6.7 | 0.19 % |
| 0.20–0.35 | 17,831 | 0.278 | 0.256 | −0.0229 | −7.1 | 0.18 % |
| 0.35–0.50 | 31,778 | 0.445 | 0.445 | −0.0008 | −0.3 | 0.33 % |
| 0.50–0.65 | 29,098 | 0.558 | 0.569 | +0.0093 | +3.2 | 0.25 % |
| 0.65–0.80 | 20,552 | 0.723 | 0.754 | +0.0307 | +10.3 | 0.07 % |
| 0.80–0.90 | 10,654 | 0.849 | 0.884 | +0.0343 | +11.1 | 0.03 % |
| 0.90–0.95 | 5,915 | 0.926 | 0.955 | +0.0287 | +10.7 | 0.02 % |
| 0.95–0.98 | 6,207 | 0.967 | 0.983 | +0.0156 | +9.4 | 0.00 % |
| 0.98–1.00 | 17,007 | 0.994 | 0.997 | +0.0032 | +7.3 | 0.00 % |

**Verdict: dead after costs.** The bias is real and strongly significant (t ≈ 7–11),
about 2–3 pp per share on favourites and −2.3 pp on longshots. But:

- the entry is a **VWAP of executed trades**, i.e. roughly mid. A taker pays about half
  the quoted spread, and the median quoted spread is **2 ¢** — ~1.2 pp of notional on a
  0.85 favourite. That consumes most of the +3.4 pp.
- fees were ~0 for most of the sample. In 2026-Q1, the only era with fees, the effect had
  already shrunk to **+1.2 pp** on favourites against ~0.8 % in fees.
- standard errors here are `sd/√n` and ignore that the two tokens of a market are perfectly
  anti-correlated, so the true interval is wider than printed.

**What would change the verdict:** measuring the *quoted ask* at entry rather than the VWAP
(needs the book, i.e. 2026-04-13 onward), and a 2026-only window. If the ask-side version
still clears fees plus half-spread, the idea comes back — but at that point it is a
capacity-limited 1 pp trade, not a franchise.

### Correction recorded, because it nearly went the other way

The first version of this screen used entry = VWAP of the market's **first 48 h**, and
reported a +7.6 pp gap at 0.80–0.90 and −21.6 pp at 0.20–0.35 — twenty times larger. That was
an artefact: for the median 24-hour market, "first 48 h" is the *entire life*, and averaging
a price path that terminates at 0 or 1 pulls the average toward 0.5, so the realised 0/1 is
mechanically more extreme than the measured entry. The tell was that the gap was exactly
antisymmetric around 0.5 and cancelled to ~0 in the aggregate (crypto: −0.187 low bucket,
+0.203 high bucket, +0.015 overall).

**Rule that follows:** an entry price must be a point-in-time price with resolution still
ahead of it. Never average across a window that contains the terminal price.

---

## Track A framing — wallet cohort built, and what it already says (2026-11-01)

**Source:** `/v2/leaderboard` (30 pages) + `/v2/biggest-winners` (5 pages) + one
`/v2/user-stats` per wallet. **3000 wallets**, no null PnL, no duplicates.

| Archetype | n | realized PnL | rebates | rebate share | median trades |
|---|---|---|---|---|---|
| directional | 2,331 | $62.8 M | $6.0 M | **9.5 %** | 338 |
| maker | 564 | $26.4 M | $17.9 M | **67.7 %** | 1,772 |
| combo | 82 | $4.2 M | $0.0 M | 0.2 % | 420 |
| unknown | 23 | — | — | — | 2 |

**What this changes.** The maker archetype is not primarily a trading strategy: **two
thirds of its realised PnL is maker rebate**, not price movement. That is a fee-structure
artefact available only to whoever the rebate schedule pays, and it is not replicable by a
directional strategy at any size we could reach. Any "market making looks profitable, let's
do that" inference from leaderboard data is therefore measuring the rebate schedule, not
skill.

Directional PnL — the only kind we could in principle replicate — is $62.8 M spread across
2,331 wallets, i.e. a mean of ~$27 k each, while the top few carry most of it. The
distribution matters more than the mean; it is the input to Track A.

**Caveat that makes this unusable as a feature:** the boards rank by *today's* PnL, so the
cohort is survivorship-biased and not point-in-time. It is a list of whom to study, never a
score to put in a backtest.

**Next:** decompose the directional cohort's PnL by market family and holding time, then
ask which parts are reachable at our size. Combo wallets are the anomaly worth one look —
one has rank 1222 with $2.36 M realized against $231 of market PnL.

---

## C2 (free half) Base-rate models — **market price wins, decisively** (2026-11-01)

**Window:** all resolved binary markets; entry VWAP for the T−24 h → T−6 h slot.
**Sample:** 350 recurring families (≥ 20 resolved instances), 20,652 priced token-observations.
**Method:** `scripts/screen_base_rates.py`. Family = slug with its date suffix stripped.

| Forecaster | Brier |
|---|---|
| **market price** | **0.0875** |
| family base rate | 0.1843 |
| 50/50 blend | 0.1127 |

**Verdict: dead.** A constant family base rate is roughly *twice* the Brier loss of the
market price, and blending it in makes the forecast **worse** than the price alone
(0.1127 vs 0.0875). The market already knows its own history. Mean |base rate − price| is
23.8 ¢, an order of magnitude above the ~2 ¢ round trip, but that distance is the base
rate being wrong, not the market.

**A bug worth recording, because it produced a beautiful fake result.** The first version
computed the base rate over *both tokens* of each market. Every binary market resolves one
YES and one NO, so the mean payout over both tokens is exactly 0.500 for every family, and
the screen printed 0.500 for all 350. It still "ran" and produced a full table. Two tokens
of one market are not two observations.

**What would change the verdict:** a *conditional* base rate — the family rate given the
regime (e.g. distance from the strike, previous day's move), i.e. a model rather than a
prior. That is a different, larger project and no longer "the cheapest idea in the backlog".

---

## E1 Complete-set arbitrage — **candidates exist, but none persist** (2026-11-01)

**Window:** 2026-04-01 → 2026-07-01 on the trade tape.
**Sample:** 42,806 quotable minute-cells (both legs printed ≥ $25 and priced inside
0.02–0.98); 2,022 with a positive net edge after fees.

| Category | cells | net > 0 | net > 0.5 c | best net | median $/cell |
|---|---|---|---|---|---|
| sports | 27,766 | 2,022 | 1,769 | 0.550 | 198 |
| geopolitics | 7,328 | 379 | 283 | 0.200 | 187 |
| politics | 3,781 | 67 | 55 | 0.272 | 187 |
| crypto | 2,033 | 128 | 90 | 0.103 | 146 |
| other+ | 1,898 | 65 | 42 | 0.098 | 120 |

**Verdict: unconfirmed, and the persistence says no.** Of 1,769 cells exceeding the
threshold, only **16 lasted 2 consecutive minutes** and **none lasted longer**. A 55 %
round-trip edge that cannot survive 60 seconds is not an edge; it is a stale last-trade
price on one leg.

The first naive pass claimed 12.8 % of all minute-buckets were arbitrage with a minimum
sum of 0.011 — dust prints of $0.31–$5.79 at prices like (0.001, 0.010). Both filters now
applied (≥ $25 per leg, both legs inside 0.02–0.98) removed that entire class.

**What this establishes:** the tape alone cannot settle E1. Last-traded price is only a
lower bound on the ask, and it goes stale exactly where arbitrage would live. The question
is decided by the book, so E1 moves to B-series once the reduced book exists — this screen
now supplies the candidate list to check against it.

**Category selection still matters** (the plan's point): crypto carries the 1.75 % peak fee,
geopolitics is fee-free, and the fee overlay is what kills a 1–2 ¢ gap.
