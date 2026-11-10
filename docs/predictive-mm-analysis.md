# Predictive Market Making: What the Tape Supports, What Was Built

Compiled 2026-11-05 from the live tape (`data/tape`, 2026-11-04 00:00 UTC onward),
the derived L3 queue (`data/derived/l3_queue`, 37 asset-hours), and the working
record in `docs/two-week-results.md`. Every number states its window and sample;
proxy values are labelled.

## 1. Conclusion first

**Directional prediction of the next move is not supported by the data.** The
prior models (daily GBM, near-expiry GBM, Kalshi 60 s lead-lag, cross-venue
basis) fail after costs. The measured Binance lead is **defensive**: it lets a
maker cancel a threatened side before the slow taker prints, not take a
direction.

**A short-horizon, side-selection prediction is supported at the fill-markout
level.** A trailing 60 s price-only order-flow-imbalance (OFI) sign decides
which resting side the next trade is about to run over. On 29 political
asset-hours (780 marked fills, 2026-11-04 01:00 and 05:00 UTC), quoting both
sides at one tick earns **-$309.69**; quoting only the side the trailing OFI
does not threaten earns **+$17.52**. The gate removes the negative-markout side
(net/share -0.01046) and keeps the positive one (+0.00100). A sign-flipped
placebo at the same threshold makes it worse (-$335.16), so the result is not
just "quote less".

The result is **in-sample, one tape, one threshold, spread/markout only**. It
does not include the liquidity reward, and the gate is single-sided, so it may
forfeit reward score. The 14-day paper run has to measure the combined
net (spread + markout + reward). The paper trader is wired with this gate behind
`--microstructure-gate` and a unit test pins the pull behaviour.

Joining the same fills to the L3 queue splits the gate's content: the fills it
removed as unprofitable were the *back-of-queue* ones (-2.01 c/share), while the
front-of-queue fills it also removed were profitable (+0.36 c/share). Gating on
the queue position instead of the side alone keeps those front fills and earns
**+$66.88** on the same 777 marked fills (section 3b). That is the implemented
`--orchestrator` policy.

## 2. What the existing evidence already ruled out

| Hypothesis | Evidence | Verdict |
|---|---|---|
| Daily / near-expiry GBM predicts the mid | alpha register #4/#5; `docs/two-week-results.md` Gate 1 | Not supported |
| Binance gives a directional taker edge | Gate 3 quote reaction p50 52.7 ms, taker 1,067.7 ms; catch-up 0.24–0.29 ¢ vs a 2.3 ¢ entry cost | No; the lead is defensive |
| Kalshi leads Polymarket on a 60 s grid | 8.3: 233 mapped pairs, 76 quoted both, 69 with a basis; lead-lag peaks at lag 0 (r=0.21) | Not tradeable at 60 s |
| Basket/OFI/trade-flow alpha | alpha register #6/#7; the live microstructure engine's 850/1.5 c gate | Weak alone |
| Fill count is a costless signal | The 29-hour A/B baseline | Negative net; the reactionary quote is adversely selected |

The measured facts that survive are: Polymarket CLOB timestamps are clean
(median skew ~3 ms); Binance BBO is throttled at 100 ms; our cancel path upper
bound is 217.5 ms (CLOB min RTT, no order sent); the L3 shadow-fill model is
optimistic (the shadow order joined its level earlier).

## 3. The new A/B: trailing OFI side selection

### Method

* Data: the six political conditions with derived L3 in
  `data/derived/l3_queue/date=2026-11-04/hour=01` and `hour=05` — 29
  asset-hours (26 tokens at 01:00, 3 at 05:00), 6 conditions. Two tokens of one
  condition are anti-correlated; fills inside a condition-hour are not
  independent. The counts below are token-hours, not independent markets.
* Baseline (reactionary): quote one tick either side of the current mid, hold
  until the book top changes (`scripts/backtest_quotes.py` semantics), 100 %
  L3-shadow fills, 5-minute markout, zero maker fee, no merge gas.
* Gate: at the quote, compute the mean price-only OFI step over the trailing
  60 s (bid up / ask down = +1 each; bid down / ask up = -1). Positive OFI
  threatens a resting **ask**; negative threatens a resting **bid**. Skip the
  threatened side at the configured threshold. Signal is computed before the
  quote; the fill is not used to set it.
* Threshold 0.0 means every non-zero trailing imbalance pulls one side. The
  p90 threshold (0.26) is also reported; it removes too few fills to matter.
* `scripts/backtest_predictive_gate.py` reproduces every number; the placebo
  flips the signal sign at the same threshold.

### Result (29 asset-hours, 780 marked fills, 48,869 shares)

| Policy | Fills | Net / fill | Net / share | Net USD |
|---|---:|---:|---:|---:|
| Reactionary (both sides, 1 tick) | 780 | -0.00813 | -0.00634 | **-309.69** |
| OFI sign gate, threshold 0.0 | 298 | -0.00725 | **+0.00100** | **+17.52** |
| OFI gate, p90 0.26 | 771 | -0.00825 | -0.00645 | -309.43 |
| OFI sign placebo, p90 0.26 | 776 | -0.00826 | -0.00693 | -335.16 |
| Live size-weighted engine, 850 / 1.5 c | 679 | -0.00899 | -0.00737 | -307.22 |

The gate's excluded set (482 fills) has net/share **-0.01046**; the retained
set (298 fills) **+0.00100**. The placebo's excluded set has the opposite sign
(+0.05278), confirming the same-sign gate is doing the selection. The live
`MicrostructureEngine` with its configured 850/1.5 c thresholds excludes only
101 fills and does not fix the baseline; the paper therefore uses the
price-only sign gate, not the size-weighted threshold.

## 3b. The queue split: what the side gate was really selecting

The side gate above is price-only. Joining every marked fill to the L3 queue row
at its quote price (`scripts/analyze_queue_positions.py`; 934 fill rows, 777
with a queue row, same 29 asset-hours) splits it by the FIFO position of the
shadow order, `queue_ratio = q_ahead / (q_ahead + q_behind)`:

| Queue position at the quote | Fills | Net / share | Net USD |
|---|---:|---:|---:|
| Front, ratio < 0.60 | 199 | **+0.00364** | **+46.17** |
| Back, ratio >= 0.60 | 281 | **-0.02014** | **-372.72** |
| Baseline (all marked fills) | 777 | -0.00628 | -305.85 |

The unconditional side gate removed the profitable front-of-queue fills and
kept its value for the back. Gating on the queue position instead — keep both
sides at the front, shrink or pull the threatened side at the back — is the
best measured policy:

| Policy | Fills | Net / share | Net USD |
|---|---:|---:|---:|
| Reactionary, both sides | 777 | -0.00628 | -305.85 |
| Side gate (price only) | 297 | +0.00118 | +20.71 |
| **Queue-aware, ratio >= 0.60** | **496** | **+0.00222** | **+66.88** |
| Queue-aware kept (was side-gated out) | 199 | +0.00364 | +46.17 |
| Queue-aware excluded (back of queue) | 281 | -0.02014 | -372.72 |

Mechanism: a front-of-queue fill is reached by a small trade at the level, while
a back-of-queue fill needs the level swept — which is exactly the adverse
event. This is the direction Moallemi & Yuan (2016) give queue position value:
depth ahead of a resting order raises the adverse-selection cost of the fill it
does receive.

`polymarketbot.tools.fill_calibration` fits the print-conditional fill probability
on the same rows (6,817 level observations, 2,184 fills, 2026-11-04
01:00-05:59 UTC), `logit P = 3.2769 - 1.1421*ratio - 0.5733*log1p(size)`,
|z| > 14 on every coefficient. It is a ranking signal, not an absolute fill
forecast: the backtester's per-episode fill rate is only 0.98 %.

### What is implemented

* `src/polymarketbot/paper_trading/orchestrator.py` — pure decision unit.
  `policy` is `reactionary` / `side_selection` / `queue_aware` / `reward_first`;
  `signal` is `ofi` or `binance`; `gate_action` is `shrink` (default) or `pull`;
  `min_adverse_queue_ratio` 0.60; `shrink_scale` 0.25. Disabled by default, so
  every existing run is unchanged.
* `PaperTradingEngine(orchestrator=...)` — replays the same FIFO state as
  `build_l3_queue.py` per resting quote (`q_ahead` at join = displayed level
  size, arrivals behind grow `q_behind`, decreases consume the front first), so
  the calibrated 0.60 threshold transfers unchanged. The gate then applies the
  side decision to the quote size; a shrunk side is floored at
  `rewards_min_size` so it stays reward-eligible, and a released shrink returns
  to full size.
* `scripts/run_paper_trading.py --orchestrator` plus
  `--orchestrator-policy|signal|gate-action|min-adverse-ratio|shrink-scale`;
  the flags are recorded in the session metadata.
* `tests/test_orchestrator.py` — 11 cases: front keeps both, back shrinks or
  pulls the threatened side, `side_selection` still gates the front,
  reactionary/missing-signal/missing-queue keep both, threshold boundary, fill
  probability monotone in the ratio, plus three engine-level checks (shrink to
  the reward floor, keep at the front, pull removes the side).

Shrinking rather than pulling is the reward-preserving choice: Polymarket's
scoring halves a quote's effective value when one side is missing inside
0.10-0.90 (single-sided quotes are divided by 3) and scores zero outside that
band, so a pull forfeits the epoch. The A/B cannot price that yet — the reward
is not in any net above — which is what the 14-day paper run measures.

### Why this is not yet a strategy result

* The 29 asset-hours are two UTC hours of six conditions; the fills are
  correlated within a condition and the threshold is fit on the same tape.
* L3-shadow fills are an optimistic upper bound on queue position.
* The gate is single-sided; the Polymarket reward score is not in the net. A
  two-sided reward quote that pulls one side on flow may earn less reward. The
  combined objective is `spread + markout + reward - gas`.
* One tape day. The planned 14-day window is the sample that decides.

## 4. Binance (crypto) A/B

The crypto daily L3 hours built for this analysis (ETH/BTC above-strike tokens,
2026-11-05 05:00 and 06:00 UTC) produce only 0–3 fills per asset-hour at one
tick, so a Binance velocity A/B there has no power. Gate 3 remains the
evidence: the Binance→PM quote reaction is 52.7 ms, the taker 1,067.7 ms, and
our cancel path 217.5 ms — enough to pull before the taker, not enough to take
directionally. More fast-market L3 hours are required before a crypto gate can
be judged on PnL.

## 5. What was built

* `src/polymarketbot/paper_trading/engine.py`
  * `microstructure_gate=True` on `PaperTradingEngine`.
  * `update_orderbook` records the top-of-book OFI steps; `generate_quotes`
    computes the trailing 60 s mean and pulls the threatened side.
  * Pulled side size 0 and no `VirtualQuote`; the reward score for that side is
    zero, so the reward impact is visible in the paper ledger.
* `src/polymarketbot/paper_trading/orchestrator.py` — `QuoteOrchestrator` /
  `OrchestratorConfig` / `QueueSnapshot`; the queue-aware policy described in
  section 3b. Disabled unless injected.
* `PaperTradingEngine(orchestrator=...)` — per-quote FIFO replay of the L3
  queue ratio and the side-level shrink/pull; explicit policy sizing replaces
  the "never grow a resting quote" heuristic while the orchestrator is active.
* `scripts/run_paper_trading.py` — `--orchestrator` with
  `--orchestrator-policy`, `--orchestrator-signal`,
  `--orchestrator-gate-action`, `--orchestrator-min-adverse-ratio`,
  `--orchestrator-shrink-scale`; all recorded in the session metadata.
* `tests/test_orchestrator.py` — 11 cases (policy matrix, engine shrink/keep/
  pull, ratio arithmetic).
* Live smoke (2026-11-05 22:18 IST / 16:48 UTC): `--orchestrator
  --orchestrator-policy queue_aware` ran 45 s on the Mississippi Senate market
  (2 tokens), produced 4 virtual quotes at the reward floor, and persisted the
  session to `data/live_tape/paper-20261105T164813Z-1e9a69`. Wiring check, not
  a PnL sample: the WebSocket did not stream long enough for a gated decision to
  fire.
* `scripts/run_paper_trading.py` — `--microstructure-gate` (legacy), flag
  recorded in the session metadata.
* `tests/test_paper_trading.py` — `test_price_only_ofi_gate_pulls_threatened_ask`
  pins the +OFI → ask-pulled behaviour. 11/11 paper tests pass.
* Live smoke (2026-11-05 20:53–20:54 IST / 15:23–15:24 UTC): the gated engine
  ran 60 s on the Mississippi Senate market with the WebSocket connected; two
  reward epochs sampled +$0.0063 (projected $4.52/day) and the state persisted
  to `data/processed/paper_trading/ledger_ofi_gate.json`. A 60 s run is a
  wiring check, not a PnL sample.
* `scripts/backtest_predictive_gate.py` — the A/B harness (L3 shadow fills,
  trailing OFI, Binance velocity, live engine comparison, placebo, per-market
  split). Reproduce with:

```text
.venv/Scripts/python.exe scripts/backtest_predictive_gate.py --date 2026-11-04 --microstructure --no-write
```

## 6. What has to happen next

1. Run the paper trader with `--orchestrator` (queue-aware, shrink) through the
   14-day window; the ledger's `total_rewards_earned_usd` and `net_trading_pnl`
   are the combined objective. The in-sample spread result is not enough.
2. Tune the two queue parameters the A/B could not price: `shrink_scale` and
   `min_adverse_queue_ratio`. Shrink (not pull) is the reward-preserving choice;
   the reward is not in the A/B net.
3. Build more crypto L3 hours and rerun `--binance`; Gate 3 is a latency result,
   not a PnL result.
4. Measure the Kalshi event-level lead at sub-second resolution; the 60 s grid
   only proves no lagged co-movement.
5. Keep the L3-shadow caveat on every fill table; a live fill is behind the
   queue and the count here is an upper bound.

## 7. Files and numbers

* A/B harness: `scripts/backtest_predictive_gate.py`
* Queue join / A/B: `scripts/analyze_queue_positions.py`
  (`data/derived/queue_gate_summary.txt`)
* Fill model: `polymarketbot.tools.fill_calibration` (`data/derived/fill_model.json`)
* Paper gate: `src/polymarketbot/paper_trading/engine.py`
* Queue orchestrator: `src/polymarketbot/paper_trading/orchestrator.py`
* Paper CLI: `scripts/run_paper_trading.py`
* Tests: `tests/test_orchestrator.py`,
  `tests/test_paper_trading.py::test_price_only_ofi_gate_pulls_threatened_ask`
* L3 build: `polymarketbot.data.l3_queue`; the 29 political asset-hours and the
  8 crypto asset-hours are under `data/derived/l3_queue/`.
* Working record and Gate status: `docs/two-week-results.md`.
