# Kalshi candle cross-check (H7): 1-minute mapping and direction validation

Status: measured 2026-11-06 01:23 IST with the Kalshi 1-minute candle backfill
still running and the Polymarket collector live. The artifact is
`data/derived/kalshi_pm_candle_check.parquet`; the script is
`scripts/report_kalshi_candle_check.py`.

**Verdict: H7 FAILS as written.** Of 43 pairs with at least 20 aligned 1-minute
changes, 39 have a defined change correlation and **0 of them reach |r| ≥ 0.9**
(the rule needs ≥ 20 such pairs). The maximum is r = 0.839 on 1,259 changes
(`KXHOUSERACE-NC03-26-R`, confidence 0.95). Direction is *not* contradicted —
no pair shows the r ≈ −1 signature of an inverted YES/NO mapping, and the
strongest negative is r = −0.230 on 29 changes — but the candle-only test does
not validate the mapping either. Every number below is measured; **Inferred**
items are marked.

## 1. Data actually available at the run

| item | measured value |
|---|---|
| v2 mapping pairs | 107 (all `direction = normal`) |
| candle files on disk under `data/raw/kalshi/candles` | 484 parquet files, 107 mapped tickers |
| candle months present | 2026-08, 2026-09, 2026-10, 2026-11 |
| mapped tickers with a file, by month | 76 / 82 / 84 / 106 (Aug / Sep / Oct / Nov) |
| files per month | 110 / 116 / 118 / 140 |
| PM tape window (`clob_events`) | 2026-11-04T00:37:10Z .. 2026-11-05T19:43:59Z |
| minute marks in the window | 2,586 (≈ 1.80 days) |
| pairs with Kalshi bars inside the window | 106 of 107 |
| pairs with a PM quote event | 107 of 107 |
| pairs evaluated (≥ 20 aligned changes) | 43 |
| pairs excluded (< 20 aligned changes) | 64 |

The backfill was still writing while this ran: the November-only run 4 minutes
earlier saw 130 files / 96 tickers with bars / 40 evaluated pairs, and the
full-set run saw 484 files / 106 tickers / 43 evaluated. Treat the counts as a
snapshot timestamped above, not a stable inventory. The excluded 64 are 63
pairs with fewer than 20 aligned changes (no candle/PM overlap for 20 minutes)
and 1 pair whose November file did not exist yet
(`KXBTCMINY-27JAN01-45000.00`; it has Aug–Oct bars but no November bar and
therefore none in the tape window).

## 2. Method

For every pair and every minute mark `m` in the tape window:

1. **PM YES mid.** From `data/tape/clob_events`, take the last quote for the
   mapping's `polymarket_yes_token` at or before `m` (book snapshot mid, BBO
   frame mid, or the nested `price_changes.best_bid`/`best_ask` mid — the same
   reconstruction as `scripts/report_kalshi_basis.py`). A quote older than
   **180 s** is `missing`; nothing is carried forward beyond that.
2. **Kalshi mark.** The 1-minute bar whose `end_period_ts` equals `m`.
   The API returns `close = null` on most bars because the far-dated ladders
   barely trade (an early probe of the window saw 10,906 trade closes in 35,008
   bars across 48 tickers), so
   the mark is `(yes_bid_close + yes_ask_close) / 2`; `close` is used only when
   the quote is absent. Missing bars stay missing.
3. **Levels and changes.** `logit(x) = log(x/(1-x))` with x clamped to
   `[0.001, 0.999]`. A pair's 1-minute change is the difference of logits
   between two **consecutive** marks where both venues are defined at both
   endpoints; a change spanning a missing mark is dropped, not stretched.
4. **Per-pair statistics.** Pearson r of those logit changes; mean/median basis
   `logit(kalshi) − logit(pm)` and median absolute basis on the aligned levels;
   counts of bars, quotes, aligned minutes and changes.
5. **Panel.** Each pair's change series is z-scored, then pooled. Because each
   series has unit variance, the pooled r is the change-count-weighted mean of
   the per-pair correlations, `Σ nᵢ rᵢ / Σ nᵢ`; the market count is reported
   beside it because pairs are correlated tokens, not independent
   observations. No t-statistic is computed.

Family labels come from the mapping row: `method = crypto_bound` (or a
`KXBTC`/`KXETH`/`KXSOL` series) → crypto ladder; `method = treasury_bound` (or
a `KX2YR`/`KX5YR`/`KX10YR`/`KX30YR` series) → treasury; `KXFEDDECISION`,
`KXRATECUTCOUNT` or a `fed_*` method → FOMC; a fixed list (`KXHOUSERACE`,
`KXHOUSEWINSTATE`, `SENATEMS`, `HOUSEAZ2`, `KXAKSENATE`, `KXDPRESPRIMARY`) →
elections; everything else (French presidential, NASCAR, LOL, …) → other.

All prices are YES/YES. `direction = normal` in the mapping means Kalshi YES
should equal the PM `polymarket_yes_token`; an inverted pair would show a change
correlation near −1.

The statistic is the one assigned for this check — Pearson r of 1-minute logit
*changes*. H7's sentence says "correlation ... of 1-min closes", but levels of
these near-settled markets are dominated by a slow common drift that would
overstate agreement, so no level correlation is reported.

## 3. Result: full candle set (artifact-producing run)

Command and runtime:

```bash
.venv/Scripts/python.exe scripts/report_kalshi_candle_check.py
# 484 candle files, 107 mapped tickers, 2,586 minute marks; 4 min 02 s (242 s)
```

Pooled panel, full window (market count = pairs contributing at least 20
changes and a defined r):

| family | mapped pairs | evaluated | pooled r | n changes | markets |
|---|---:|---:|---:|---:|---:|
| all | 107 | 43 | **0.6520** | 13,569 | 39 |
| crypto ladder | 23 | 2 | 0.1559 | 59 | 2 |
| treasury | 30 | 7 | 0.0217 | 299 | 7 |
| FOMC | 9 | 4 | 0.4121 | 310 | 4 |
| elections | 26 | 22 | 0.7134 | 12,116 | 22 |
| other | 19 | 8 | 0.0776 | 785 | 4 |

Four evaluated pairs have constant logit changes on one venue (three
`KXFRENCHPRES-27`, one `KXLOLAWARD`) and therefore an undefined r; they are
null in the parquet, not zero. The elections row carries the result: the
high-confidence structural-winner pairs co-move at r ≈ 0.6–0.84. Treasury (30
pairs, the largest mapped family) and crypto (23) have essentially none of
their 1-minute variance in common in this 1.8-day window.

### Basis (aligned levels, 43 evaluated pairs)

| statistic | value |
|---|---|
| mean of per-pair mean basis | 0.0199 |
| median of per-pair mean basis | 0.0123 |
| per-pair mean basis range | −0.2664 .. 0.4404 |
| median of per-pair median absolute basis | 0.0917 |

A basis of 0.09 logit ≈ 2.2 cents at p = 0.5. The basis is persistent and
bounded; it does not look like a sign inversion, which would put
`logit(kalshi) − logit(pm)` far from zero with a consistent sign.

## 4. Direction check: negative-correlation pairs

Of the 39 pairs with ≥ 20 aligned changes and a defined r, **33 have r > 0 and
6 have r < 0**. The six negatives (suspected inverted/incorrect mapping):

| Kalshi ticker | r | n changes | method | confidence | family |
|---|---:|---:|---|---:|---|
| `KX30YRDIRLM-26NOV30L-T5.17` | −0.230 | 29 | treasury_bound | 0.90 | treasury |
| `KX2YRDIRLM-26NOV30L-T4.41` | −0.064 | 24 | treasury_bound | 0.90 | treasury |
| `KXBTCMINY-27JAN01-55000.00` | −0.052 | 35 | crypto_bound | 0.80 | crypto_ladder |
| `KXFRENCHPRES-27-MLEP` | −0.039 | 36 | title_exact | 0.97 | other |
| `KX2YRDIRLM-26NOV30L-T4.44` | −0.011 | 36 | treasury_bound | 0.90 | treasury |
| `KXNASCARTRUCKSERIES-NTS26-LRIG` | −0.006 | 104 | title_fuzzy | 0.73 | other |

None is close to the −1 an inverted YES/NO pair would produce. The one at
|r| > 0.2 has 29 changes; a 95 % band on 29 independent pairs is roughly
±0.37, so it is inside noise even before the token clustering is considered.
The read is: **no evidence of an inverted pair, but also no positive
confirmation** — all six sit in the same low-correlation regime as the rest of
the panel.

## 5. H7 decision

The rule in `docs/kalshi-hypotheses.md` H7: *|corr| ≥ 0.9 on ≥ 20 pairs
validates direction; otherwise name the failures.*

| rule input | measured |
|---|---|
| pairs evaluated (≥ 20 changes) | 43 |
| pairs with a defined r | 39 |
| pairs with \|r\| ≥ 0.9 | **0** |
| pairs required by H7 | 20 |
| best pair | `KXHOUSERACE-NC03-26-R`, r = 0.839, n = 1,259 |

**Verdict: FAIL.** The failures, by name and family:

* **All 39 defined-r pairs are below 0.9.** The closest are six midterm
  House/Senate winners at 0.787–0.839, led by `KXHOUSERACE-NC03-26-R`
  (r = 0.839, n = 1,259); the median evaluated pair has only 123 changes, and
  every treasury and crypto pair is under 0.47.
* **4 pairs cannot be tested**: constant logit changes on one venue
  (`KXFRENCHPRES-27-CLAG`, `-DLIS`, `-JMEL`, `KXLOLAWARD-WORLDS26FMVP-RULER`).
* **64 pairs have too little overlap** (< 20 aligned changes): resourcing, not
  mapping, but it leaves the rule with 43 of the 107 pairs.
* **One pair has no bar in the window** (`KXBTCMINY-27JAN01-45000.00`, no
  November file at run time).

### What this does and does not say

* **Does not say the mapping is inverted.** No pair shows a large negative r;
  the largest is −0.230 on 29 changes, and the basis distribution is centred
  near zero. The direction convention is unrefuted.
* **Does not validate the mapping either.** H7 was written to be the cheap
  gate before the 1 Hz tests; as measured it cannot pass on this data.
* **Inferred:** the 0.9 bar is calibrated for a shared price path (levels), not
  for 1-minute logit changes. At 1-minute resolution these markets move on
  1-cent quote ticks and stale PM quotes, so even a correctly mapped pair is
  attenuated well below 0.9 — the elections result (0.71 pooled) is consistent
  with that, and the near-zero crypto/treasury results are dominated by sparse
  quoting rather than by wrong pairs. This is an interpretation of the rule,
  not a measurement; changing the rule needs an explicit edit to
  `docs/kalshi-hypotheses.md`, not a silent re-threshold here.
* The practical consequence is the one H7 prescribes: **do not treat direction
  as validated for H1–H6 yet.** The next check should be a level- or
  basis-based direction test on the same pairs (levels are not independent
  minutes) and a re-run of this script once the backfill settles, before any
  sub-second work.

## 6. Second run: one-month slice

Command and runtime:

```bash
.venv/Scripts/python.exe scripts/report_kalshi_candle_check.py --month 2026-11
# 130 candle files (month=2026-11 only), 96 mapped tickers with bars; 3 min 53 s (233 s)
```

Same tape window. It evaluated 40 pairs (36 with a defined r), pooled
r = 0.6576 on 13,344 changes over 36 markets; family pooled r: crypto 0.1559
(n = 59, 2 markets), treasury 0.0215 (n = 236, 5 markets), FOMC 0.4121
(n = 310, 4 markets), elections 0.7171 (n = 11,954, 21 markets), other 0.0776
(n = 785, 4 markets). It lists five negative pairs — the full-set six minus
`KX30YRDIRLM-26NOV30L-T5.17`, which had no November bar at that moment. The
verdict is the same: 0 of 36 defined-r pairs reach 0.9.

**The parquet artifact is from the full-set run**, not this one. Both runs are
inside the 600 s budget.

## 7. Artifact

`data/derived/kalshi_pm_candle_check.parquet`, 107 rows (one per mapped pair),
zstd-compressed. Columns:

```
kalshi_ticker, kalshi_event, series_ticker, family,
polymarket_condition_id, direction, confidence, method, market_window,
candle_minutes, pm_minutes, aligned_minutes, n_changes, corr_changes,
basis_mean, basis_median, basis_med_abs, kalshi_mid_mean, pm_mid_mean,
negative_corr, status, window_start_ns, window_end_ns, bucket_sec, max_stale_sec
```

* `status`: `ok` (≥ 20 changes), `insufficient` (< 20), `no_candles`.
* `corr_changes` is null when fewer than 3 aligned changes or zero variance;
  `basis_*` is null when no minute is aligned; `negative_corr` is null outside
  the evaluated set. Counts are real zeros (`candle_minutes = 0` means no bar
  was found), statistics are never zero-filled.
* `candle_minutes` / `pm_minutes` are the rows loaded inside the tape window,
  not the market's full history.
* The parquet keeps r for pairs with ≥ 3 changes even when `status` is
  `insufficient`; the H7 panel applies the ≥ 20 rule separately.

## 8. Limits

* **1.8 days of PM tape, not 90 days.** The candle side has months of history;
  the tape starts 2026-11-04, so the overlap is two days and 2,586 marks.
* **Backfill coverage was incomplete and moving.** 484 files at 01:23 IST with
  per-month mapped coverage of 76 / 82 / 84 / 106 tickers (Aug / Sep / Oct /
  Nov); one ticker still lacked a November file. The counts above will not
  reproduce exactly.
* **PM staleness removes marks.** The 180 s rule deliberately drops markets
  that stopped quoting; on `KXHOUSERACE-NC03-26-R` it kept 1,357 of ~2,440
  candle minutes. The resulting gaps make the change series noisy, which
  attenuates r in the same direction as a mapping error.
* **Kalshi prices are quote mids, not trades.** With `close` mostly null, the
  bar mark is the bid/ask midpoint; 1-cent tick granularity adds noise to the
  Kalshi change.
* **Pairs are not independent.** Two tokens of one condition move against each
  other, and several pairs share a Kalshi event. Market counts are therefore
  reported with every pooled r; the pooled r is descriptive, not a test.
* **The window is post-midterm.** Most elections resolved 2026-11-03, and the
  macro ladders are far-dated; 1-minute variation is dominated by quoting
  rather than information.

## 9. Reproduce

```bash
# full available candle set (artifact-producing run, ~4 min)
.venv/Scripts/python.exe scripts/report_kalshi_candle_check.py

# one-month slice (~4 min)
.venv/Scripts/python.exe scripts/report_kalshi_candle_check.py --month 2026-11

# inspect the per-pair rows
.venv/Scripts/python.exe -c "import duckdb; print(duckdb.sql(
  \"select kalshi_ticker, family, n_changes, corr_changes, basis_mean, status \
   from 'data/derived/kalshi_pm_candle_check.parquet' order by n_changes desc\").df())"
```

Both runs are single-process DuckDB scans of `clob_events` restricted to the
107 mapped condition ids; no collector or config changes were made.
