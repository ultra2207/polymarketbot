# Kalshi → Polymarket: hypotheses, tests, decision rules

Compiled 2026-11-06 before any Kalshi signal work. Every hypothesis names the
data it needs, the statistic, the decision rule, and what would make it false.
Nothing here is a result; results land in `docs/kalshi-signal-results.md`.

## 0. What the advantage has to be

A cross-venue edge is one of three things, and only the third is tradeable here:

1. **Arbitrage** — buy YES on one venue and NO on the other for less than $1.
   Needs both books simultaneously; blocked because Kalshi production access
   requires US KYC, so neither the production WebSocket nor an order path is
   available. Public REST snapshots (2 s cadence, p50 340 ms) are read-only and
   too coarse for a cross-venue arb. Not pursued.
2. **Directional trade on Kalshi** based on Polymarket. Kalshi demo has no real
   liquidity; production Kalshi orders need a production key. Not pursued.
3. **Defensive quote control on Polymarket**: Kalshi reprices first, so the
   Polymarket maker pulls or shrinks the side about to be swept before the taker
   arrives. This is what the OFI gate already does with Polymarket's own flow
   (`docs/predictive-mm-analysis.md`); the question here is whether Kalshi
   carries the same information *earlier or more cleanly*.

The measurable objective is therefore: **does a Kalshi-derived signal predict
the next Polymarket fill's markout better than the Polymarket-only signal, after
the same quote mechanics?**

## 1. What is already known (and what it rules out)

| Evidence | Reading |
|---|---|
| 60 s lead-lag on 233 mapped pairs: 76 quoted both, 69 with a basis, pooled r peaks at lag 0 (r = 0.21) | No co-movement at 60 s that a maker can trade; the grid is too coarse to see a 1–5 s lead |
| Same study: basis exists (per-pair, 60 s buckets) | The venues do disagree, but slowly; a persistent basis is not a signal |
| Mapping is 233 tickers, mostly 2026 midterm elections (`KXMIDTERMMOV` 61, `KXHOUSERACE` 46) | The tested set is the set with the *least* information asymmetry; it is also decaying as those markets settle |
| Polymarket live universe: 204 crypto-question markets, 92 macro-question markets, 200 crypto sentinels | The untested overlaps are where Kalshi is deep and fast |
| Kalshi REST: orderbooks 100 tickers/request at p50 340 ms; trades 226 prints/s global, 0.449/s for our mapped set; 1-min candles ≥ 90 days back | Sub-second books are not available on the public REST path, but 1 Hz is, and 90 days of 1-min history is |
| Polymarket tape: event-driven L2 + trades, ~9.4 GB/day, timestamped to the microsecond | The PM side is the high-resolution side; the test can be honest about the Kalshi resolution limit |

So the prior is **negative at 60 s**, and the two live opportunities are (a) a
sub-second lead that only appears at 1–5 s, and (b) the crypto/macro families
that were never mapped.

## 2. Hypotheses

Notation: `t` is the Polymarket tape clock; `k_t` is the Kalshi mid; `p_t` the
Polymarket mid; `r_t = Δ logit(p_t)`; a markout is the signed mid move over the
next Δt after a simulated fill.

**H1 — Kalshi basis displacement predicts the Polymarket reprice.**
Mechanism: the same outcome is priced off the same information; Kalshi's book is
deeper in macro/crypto so it absorbs a shock while PM's book is still stale.
Statistic: regression of `r_t(PM, next 1–10 s)` on `basis_{t-k} = logit(k) −
logit(p)` for k ∈ {0, 1, 5, 15 s}, per series family, clustered by condition.
Decision rule: coefficient positive and significant after clustering (|t| > 3
over ≥ 30 condition-days) **and** a maker placing on the side the basis points
at earns a markout improvement ≥ 0.5 c/share versus the both-sides quote.
Needs: 1 Hz Kalshi orderbook tape + PM tape for ≥ 7 days per family.
False if: the coefficient flips sign at family level, or the markout gain is
smaller than the added pull cost (reward forfeiture).

**H2 — Kalshi taker flow leads Polymarket taker flow.** *(Primary test under the
REST-only constraint: prints carry event timestamps at millisecond resolution,
so H2 is not limited by the 2 s book cadence the way H1/H3 are.)*
Mechanism: a large informed taker on Kalshi is one of the same wallets, or reads
the same news, as the PM taker seconds later.
Statistic: cross-correlation of signed Kalshi prints with PM signed trades, at
1 s resolution, by lag; then the PM-mid return following Kalshi flow deciles.
Decision rule: a monotone return ladder across flow deciles at lag 1–5 s, with
the top decile ≥ 2× the median absolute return.
Needs: Kalshi trade tape at ~1 s resolution. REST global trades give this at 226
prints/s but cost ~6 GB/day of bandwidth to capture every mapped print; if the
budget does not allow it, H2 is tested on candlesticks only and downgraded to
1-minute. False if: the ladder is flat or inverted.

**H3 — Kalshi book imbalance is a better sweep predictor than PM OFI.**
Mechanism: Kalshi's book reflects resting informed quotes before PM's book does.
Statistic: AUC of `imbalance_k` for predicting `PM |markout| > 1 c` in the next
5 s, compared with the PM-only OFI gate's AUC on the same episodes; net USD of
the quote rule under each.
Decision rule: AUC gain ≥ 0.03 and net USD gain ≥ +$10 on the same tape.
Needs: 1 Hz Kalshi books (the new `kalshi_books` stream) + PM tape.
False if: the AUC gain disappears under a one-tick quote (i.e. it only matters
where the spread is wide).

**H4 — Kalshi settlement/resolution news is decisive for PM macro markets.**
Mechanism: for scheduled releases (CPI, FOMC, payrolls) Kalshi's market closes
and settles on the print; PM's UMA resolution lags. The *event outcome* is
public information; the edge is the PM quote that is still resting during the
first minutes after the print.
Statistic: PM mid move and markout of resting quotes in the 0–60 s window after
the Kalshi market transitions to settled/closed, versus the same market's
baseline drift.
Decision rule: a systematic PM move in the direction of the Kalshi outcome, in
≥ 5 events, is a *quote-pull calendar* rule (pull both sides around scheduled
releases), not a directional one.
Needs: Kalshi `market_lifecycle_v2` or REST status polling + PM tape.
False if: PM prices the outcome before Kalshi settles (likely for consensus
prints).

**H5 — Kalshi's own volatility predicts PM adverse selection.**
Mechanism: cross-venue realized variance in 30 s is a cleaner toxicity gauge
than PM's own OFI, which fires on harmless small trades.
Statistic: markout of PM quotes conditioned on Kalshi variance terciles,
holding PM OFI fixed.
Decision rule: ≥ 0.3 c/share markout separation between terciles.
Needs: 1 Hz Kalshi books.
False if: the separation is explained by PM's own spread.

**H6 — Liquidity migration: when Kalshi's book thins, PM's fills turn adverse.**
Mechanism: a maker pulling from Kalshi is about to be picked off on PM too.
Statistic: PM markout conditioned on Δ Kalshi depth (top-3 levels) over 10 s.
Decision rule: a monotone ladder across depth-change deciles.
Needs: level-level Kalshi books, not just BBO.
False if: no monotone relation.

**H7 — Candlestick-only confirmation (cheap, no new tape).**
Mechanism: 90 days of 1-min Kalshi candles cross the same period as the PM tape's
first days; if the venues disagree systematically at 1-minute closes, the
mapping/direction logic is wrong and every higher-frequency test is suspect.
Statistic: correlation and mean basis of 1-min closes on all v2 pairs.
Decision rule: |corr| ≥ 0.9 on ≥ 20 pairs, else fix the mapping before testing H1–H6.
Needs: only the candle backfill.

## 3. Guards (apply to every test)

* **Clusters, not rows.** Two tokens of one condition are anti-correlated; a
  condition-day is one observation. Report the cluster count with every t-stat.
* **No look-ahead.** A Kalshi observation at `t` is usable for a PM quote only
  if its `recv_ts_ns` is before the PM decision timestamp; the REST poll latency
  (p50 340 ms) must be included in the lag, not hidden.
* **Multiplicity.** Seven hypotheses across families is 7× the false-positive
  budget; a family result needs |t| > 3, not 2.
* **Stale quotes are excluded, not filled.** A PM bucket with no fresh quote in
  the last 60 s is `missing`, never carried forward beyond that.
* **Direction is validated per pair.** A pair whose Kalshi YES/Y and PM YES/N
  direction is inferred from title matching is flagged and tested separately.
* **The reward is part of the objective.** Pulling a side forfeits reward
  score; every net quoted for a gate must state whether the reward is included.

## 4. Experiment order (cheapest information first)

1. **E7** candle confirmation — no new tape, validates mapping/direction.
2. **E1** basis displacement at 1 Hz — needs ≥ 3 days of `kalshi_books`.
3. **E5/E6** variance and depth conditioning — same tape as E1.
4. **E4** release calendar — needs 5+ scheduled macro events, so it starts as a
   logged calendar and pays out over the two weeks.
5. **E3** AUC vs the existing OFI gate — same tape as E1, reuses the fill model.
6. **E2** Kalshi flow — only if the bandwidth budget allows the trade tape at
   1 s resolution.

## 5. What would make this whole direction worthless

If, after E1 and E3, the Kalshi-conditioned markout gain is inside the noise and
the AUC gain over PM's own OFI is below 0.03, the honest conclusion is that
Kalshi adds no defensive value for a PM maker in these families. That outcome is
a legitimate deliverable: it closes the cross-venue branch and leaves the OFI +
queue orchestrator as the production rule.

## 6. Status (2026-11-06, after the first harness runs)

| ID | Harness | First result | Verdict |
|---|---|---|---|
| H7 | `scripts/report_kalshi_candle_check.py` | 107 pairs, 43 evaluated (>=20 changes); pooled r 0.652 over 39 markets (elections 0.713, FOMC 0.412, crypto ladder 0.156 on 2 markets, treasury 0.022); 0/39 pairs at \|r\| >= 0.9 | FAIL as written; direction unrefuted (6 negative pairs, none near -1) |
| H1 | `scripts/report_kalshi_hf_basis.py` | 1 hour sealed (2,914 aligned rows, 101 pairs): mean per-pair slope of forward PM return on basis +0.207 @5 s (52 pairs, t 3.40), +0.311 @15 s (50, 5.74), +0.441 @60 s (36, 6.76); deciles not monotone; treasury carries the mean, FOMC flat on 3 pairs | Underpowered (~4.2 condition-days, 17/101 pairs at the 50-snapshot floor); no conclusion |
| H2 | `scripts/report_kalshi_trade_lead.py` | 6 h, 29 conditions, 6,841 mapped Kalshi prints: Kalshi -> PM direction agreement **0.696** @1 s (chance 0.503, n 1,143; **0.942** for size >= p90) and the reverse PM -> Kalshi **0.227** (z -14.9); 7/8 conditions beat their own chance, but the largest condition does not | Supported directionally, unproven (`docs/kalshi-trade-lead.md`) |
| H3-H6 | — | not started | pending |

Rules for the next run (from the register): >= 7 days of `kalshi_books`, >= 50
pairs with >= 50 snapshots, and the maker-markout arm, before any statement
about a tradeable edge. `--orchestrator-signal kalshi` stays off until then.

