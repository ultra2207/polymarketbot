# Kalshi high-frequency basis (E1): harness on one hour of book tape

> **Tools (2026-11-11).** `scripts/report_kalshi_hf_basis.py` (and every other
> `scripts/*.py` named here) was deleted in the 2026-11-10 scripts cleanup; it
> has no `python -m` replacement. The measurements are kept as the record of
> that run and cannot be reproduced from this tree.

Status: harness built and run 2026-11-06 01:37 IST on the live `kalshi_books`
tape, over the one **sealed** hour partition
(`date=2026-11-05/hour=19`; the hour=20 partition was still being appended and
is deliberately skipped). Artifacts:
`data/derived/kalshi_hf_basis/hour=2026110519/part-0000.parquet` (2,914 rows)
and `scripts/report_kalshi_hf_basis.py`; the pure helpers are covered by
`tests/test_kalshi_hf_basis.py`.

**Verdict: underpowered sample - no claim about H1 either way.** One hour of
book tape covers 101 mapped pairs for ~49 minutes (≈ 4.2 pooled
condition-days), and only 17 pairs reach the 50-snapshot floor the register
asks for. The point estimates this run prints are a smoke test that the harness
works on live data, **not** evidence of a tradeable basis edge. No markout, no
quote mechanics and no reward accounting are in this cut, so H1's actual
decision rule (markout gain ≥ 0.5 c/share versus a both-sides quote) cannot be
evaluated here at all. Every number below is measured on the artifact; nothing
is projected.

## 1. Data actually available at the run

| item | measured value |
|---|---|
| `kalshi_books` partitions on disk | 1 (`date=2026-11-05/hour=19`) |
| books files in the partition | 11 parquet (the collector appends; the row count was stable at 7,791 across the run) |
| Kalshi snapshot rows for mapped tickers | 7,791 |
| rows with both YES sides present (usable mid) | 7,661 |
| `clob_events` files read (hours 19-20 UTC) | 14 |
| PM quote rows for the 107 mapped YES tokens | 23,383 |
| aligned (pair, snapshot) rows | **2,914** |
| snapshot window (UTC) | 2026-11-05T19:11:14Z .. 19:59:55Z |
| mapped pairs | 107 |
| pairs with at least one aligned snapshot | **101** |
| pairs with ≥ 50 aligned snapshots | **17** |
| median / p90 / max aligned rows per pair | 12 / 92 / 223 |
| PM quote staleness at the snapshot (median / p90 / max) | 5.75 s / 43.6 s / 59.9 s |

The six mapped pairs with no aligned row are `KXBTCMINY-27JAN01-45000.00`,
`KXBTCMAXY-26DEC31-129999.99`, `KXBTCMAXY-26DEC31-139999.99`,
`KX5YRDIRHM-26NOV30H-T4.98`, `KXDPRESPRIMARY-28SC-GNEW` and
`KXLOLAWARD-WORLDS26FMVP-PEYZ`: their Polymarket condition had no quote event
inside the window at all, so there was nothing to align to.

Only 76 of the 107 YES tokens emit `best_bid_ask` frames in the window; all 107
emit full `book` snapshots, and no mapped condition emitted a `price_change`
frame in this hour, so the mid came from the book (107 tokens) and the BBO
frames (76 tokens).

## 2. Method

For every mapped pair and every Kalshi snapshot row `t` of the hour partition:

1. **Kalshi mid.** `(yes_best_bid + yes_best_ask) / 2` from the book tape,
   falling back to the tape's own `mid` column; a row with an empty side is
   dropped, never filled. The tape skips a side when its ladder is empty, and
   130 of 7,791 rows had one.
2. **Polymarket YES mid at `t`.** ASOF join: the last PM quote at or before `t`
   with **max staleness 60 s**, else null. The quote is rebuilt exactly as
   `scripts/report_kalshi_basis.py` does - full `book` best bid/ask midpoint,
   `best_bid_ask` midpoint, or a nested `price_changes` midpoint. Rows of the
   same token at the same nanosecond are de-duplicated (last wins).
3. **Basis.** `basis = logit(kalshi_mid) - logit(pm_mid)` with
   `logit(x) = log(x/(1-x))` clamped to `[0.001, 0.999]`. Null when either side
   is missing or outside `(0, 1)`.
4. **Forward return.** `ret(h) = logit(pm at t+h) - logit(pm at t)` for
   `h ∈ {5, 15, 60} s`, both endpoints under the same 60 s ASOF rule. Null when
   either endpoint has no usable quote. A quote that did not update inside the
   horizon gives exactly `0.0`, which is a measured absence of reprice - the
   signal H1 is about - not a missing value.
5. **Persistence.** One parquet per books hour partition, written atomically to
   `part-0000.parquet`; an existing partition is skipped unless `--force`. A
   partition is only built once its hour has closed - an hour the collector is
   still appending to would otherwise be frozen at whatever the tape held at
   that moment.

The join is per pair, on the pair's own YES token: a condition's two tokens are
anti-correlated and are never mixed into one series. `direction = normal` for
all 107 pairs, so Kalshi YES is compared with `polymarket_yes_token` directly;
neither the H7 check nor this one found an inverted pair.

## 3. Row accounting

Tables scanned and where the reduction happens, for the artifact-producing run:

| stage | rows in | rows out | where the reduction happens |
|---|---:|---:|---|
| `clob_events` hour=19 (whole partition) | 9,928,863 | 46,872 | DuckDB filter to the 107 mapped `condition_id`s |
| + hour=20 tail (1 file) for the last 60 s of forward returns | — | 462 | quotes in 20:00:00-20:00:55Z |
| quote frames of the 107 conditions, then YES token + valid mid | 47,334 | 23,383 | DuckDB: `best_bid_ask` + `book` + `tick_size_change` for both tokens; keeping the YES token and `0 < mid < 1` leaves 23,383 |
| `kalshi_books` hour=19, filtered to 107 mapped tickers | 7,791 | 7,661 | DuckDB scan of the 11 parquet files (130 rows had an empty side) |
| ASOF alignment + basis + forward returns | 7,661 snapshots | 2,914 rows | numpy per pair (`asof_pos`, `forward_returns`) |
| written | 2,914 | 2,914 | one zstd parquet |

The 4,747 dropped snapshots are pairs whose PM quote was older than 60 s at
that instant - a resolved or silent Polymarket market. That is the guard in
`docs/kalshi-hypotheses.md` §3 ("stale quotes are excluded, not filled") doing
its job, and it is why the aligned panel is only 38 % of the Kalshi rows.

Runtime: **3.7 s** wall (2.7 s inside the process) for the whole hour,
single process, no second scan. Re-running skips the existing partition in
1.4 s. Both are far inside the 600 s budget; the unit is one hour partition so
a multi-day run is N independent ~4 s units.

## 4. Results (one hour, 2,914 rows, 101 pairs)

### 4.1 Per-pair slope of the forward return on the basis

One OLS slope per pair (with intercept), then the unweighted mean of the
pair-level slopes with a standard error **across pairs** - the condition is the
cluster, the rows are not independent observations. A pair enters a horizon's
panel only when that horizon has ≥ 10 usable rows for it.

| h | rows with a defined return | pairs in panel | mean slope | se (across pairs) | t | pairs with slope > 0 |
|---:|---:|---:|---:|---:|---:|---:|
| 5 s | 2,835 | 52 | 0.207 | 0.061 | 3.40 | 38 |
| 15 s | 2,680 | 50 | 0.311 | 0.054 | 5.74 | 39 |
| 60 s | 2,101 | 36 | 0.441 | 0.065 | 6.76 | 30 |

Same statistic restricted to the 17 pairs that clear the 50-snapshot floor:

| h | pairs | mean slope | se | t |
|---:|---:|---:|---:|---:|
| 5 s | 17 | 0.172 | 0.054 | 3.18 |
| 15 s | 17 | 0.227 | 0.059 | 3.84 |
| 60 s | 14 | 0.384 | 0.092 | 4.16 |

Forward-return nulls (of 2,914 rows): 79 at 5 s, 234 at 15 s, 813 at 60 s - the
PM market had no fresh quote at `t+h`, so those cells stay null.

By family (same construction, `family` from the v2 mapping):

| family | 5 s: pairs / mean (t) | 15 s: pairs / mean (t) | 60 s: pairs / mean (t) |
|---|---|---|---|
| treasury | 25 / 0.337 (2.84) | 25 / 0.470 (5.16) | 19 / 0.560 (5.80) |
| elections | 13 / 0.101 (2.37) | 13 / 0.151 (2.62) | 10 / 0.253 (2.60) |
| crypto ladder | 5 / 0.124 (1.32) | 4 / 0.267 (2.40) | 2 / 0.768 (81) |
| other | 6 / 0.069 (1.99) | 5 / 0.157 (1.97) | 4 / 0.294 (2.24) |
| FOMC | 3 / −0.002 (−1.00) | 3 / −0.012 (−1.00) | 1 / 0.000 (n/a) |

The extremes that drive the mean are treasury ladder ticks with 12-35 aligned
rows, e.g. `KX30YRDIRLM-26NOV30L-T5.08` (slope 0.946, r 0.950, n 26) and
`KX5YRDIRLM-26NOV30L-T4.50` (1.144, r 0.852, n 23). The one large negative is
`KX30YRDIRHM-26NOV30H-T5.41` (−2.036, n 17); the biggest-n pair,
`KX2YRDIRHM-26NOV30H-T4.83`, has slope −0.004 on 97 rows. Two crypto pairs
carry the 81 `t` in the 60 s family row: a two-cluster `t` is not a test, it
is a restatement of two numbers.

### 4.2 Decile table (pooled, rank-based, with the pair count in each bucket)

Mean forward return in **logit units**; at `p = 0.5`, 0.05 logit ≈ 1.2 cents.
Deciles are cut by rank, so equal-basis ties still fill ten buckets.

h = 5 s

| decile | n | pairs | basis lo | basis hi | mean ret |
|---:|---:|---:|---:|---:|---:|
| 1 | 284 | 21 | −1.795 | −0.467 | −0.0488 |
| 2 | 283 | 18 | −0.467 | −0.328 | −0.0076 |
| 3 | 284 | 21 | −0.325 | −0.219 | 0.0004 |
| 4 | 283 | 17 | −0.219 | −0.168 | −0.0007 |
| 5 | 284 | 27 | −0.168 | −0.074 | 0.0184 |
| 6 | 283 | 31 | −0.074 | −0.015 | 0.0104 |
| 7 | 284 | 24 | −0.015 | 0.015 | −0.0019 |
| 8 | 283 | 28 | 0.015 | 0.059 | −0.0006 |
| 9 | 284 | 35 | 0.059 | 0.230 | 0.0078 |
| 10 | 283 | 28 | 0.232 | 1.912 | 0.0338 |

h = 15 s

| decile | n | pairs | basis lo | basis hi | mean ret |
|---:|---:|---:|---:|---:|---:|
| 1 | 268 | 20 | −1.795 | −0.475 | −0.0746 |
| 2 | 268 | 18 | −0.472 | −0.329 | −0.0085 |
| 3 | 268 | 19 | −0.328 | −0.221 | 0.0047 |
| 4 | 268 | 17 | −0.221 | −0.177 | 0.0019 |
| 5 | 268 | 23 | −0.177 | −0.090 | 0.0159 |
| 6 | 268 | 32 | −0.088 | −0.020 | 0.0229 |
| 7 | 268 | 22 | −0.020 | 0.005 | 0.0048 |
| 8 | 268 | 28 | 0.005 | 0.057 | 0.0001 |
| 9 | 268 | 36 | 0.057 | 0.232 | 0.0108 |
| 10 | 268 | 26 | 0.232 | 1.912 | 0.0374 |

h = 60 s

| decile | n | pairs | basis lo | basis hi | mean ret |
|---:|---:|---:|---:|---:|---:|
| 1 | 211 | 19 | −1.795 | −0.496 | −0.1182 |
| 2 | 210 | 17 | −0.496 | −0.369 | −0.0210 |
| 3 | 210 | 18 | −0.369 | −0.252 | 0.0104 |
| 4 | 210 | 16 | −0.252 | −0.200 | −0.0009 |
| 5 | 210 | 15 | −0.197 | −0.144 | 0.0138 |
| 6 | 210 | 21 | −0.144 | −0.067 | 0.0279 |
| 7 | 210 | 23 | −0.066 | −0.000 | 0.0303 |
| 8 | 210 | 22 | −0.000 | 0.039 | −0.0008 |
| 9 | 210 | 33 | 0.039 | 0.222 | 0.0174 |
| 10 | 210 | 19 | 0.222 | 1.912 | 0.0310 |

The shape is not a ladder. Deciles 2-8 stay inside ±0.03 logit and do not
order; only the tails move, and the first decile of the 60 s table
(−0.118) moves *further* than the tenth (+0.031). That is the pattern of a few
extreme-basis pairs (PM stuck near 0.5 while Kalshi quotes 0.05-0.95), not of a
graded signal.

Basis distribution behind the cuts: p5 −0.539, median −0.073, p95 +0.328,
range −1.795 .. +1.912; 300 of 2,914 rows (10.3 %) have `|basis| > 0.5`.

## 5. Effective sample size and the verdict

* **Rows:** 2,914. **Clusters:** 101 pairs. **Effective sample size for any
  panel claim is the cluster count**, and per horizon only 52 / 50 / 36 pairs
  have a defined slope, 17 / 17 / 14 of them above the 50-snapshot floor.
* **Calendar:** one hour of one day. Pooled condition-days = 101 pairs ×
  ~49 min ≈ 82 condition-hours ≈ **4.2 condition-days**; per condition that is
  **0.8 h**, one snapshot of a day. H1 asks for **≥ 30 condition-days** over
  ≥ 50 clusters with `|t| > 3`: the cluster count is there (101), the calendar
  is not.
* **Overlap:** snapshots for the busiest pair are ~12 s apart and the forward
  windows are 5-60 s, so successive rows share information. The per-pair slope
  is estimated on overlapping observations and the clustered `t` measures only
  cross-pair dispersion, not that overlap. A `t` of 3.4-6.8 here is therefore not
  comparable to the register's `|t| > 3`: the register means independent
  condition-days, not a few dozen overlapping snapshots per pair.
* **Tick size:** the PM side is a 1-cent book; 0.2-0.45 logit of forward return
  per 1 logit of basis is being measured on quotes that cannot move in steps
  finer than a cent (≈ 0.04 logit at `p = 0.5`). Whether the measured slope
  survives at tradeable size is exactly what the markout arm is for, and it is
  not in this cut.

**Verdict: underpowered.** This run cannot pass or fail H1. It does establish
that (a) the harness runs on live tape inside budget, (b) the alignment is
non-trivial (38 % of Kalshi snapshots have a PM quote inside 60 s; 101 pairs
clear at least one snapshot, median 12 rows each), and (c) at this quote
density the binding constraint is not pairs - 101 of 107 map - but **hours**.

What the next run needs, from the register (`E1` needs ≥ 3 days of
`kalshi_books`; H1 needs ≥ 30 condition-days and ≥ 50 pairs per family):

* **≥ 7 days of `kalshi_books`** (the assignment's floor; 3 days is the
  register's absolute minimum for E1). Seven days gives ~707 pooled
  condition-days and 7 days of behaviour per condition, which is the first
  point at which the clustered `t` has a calendar underneath it instead of one
  hour.
* **≥ 50 pairs with ≥ 50 aligned snapshots each** inside the pooled panel. The
  median pair gains 12 aligned rows per hour, so that floor clears after ~4
  hours of tape; what the calendar buys is the clustered `t`, not the counts.
  Treasury (25 pairs in the 5 s panel) is the only family that could clear a
  family-level threshold on a short tape.
* Plus the parts of H1 that are not in this cut: the maker markout (pull cost
  and reward forfeiture included) so a slope can be converted into c/share.

Only after those hold does a family `|t| > 3` with a monotone decile ladder
mean anything. **Nothing in this doc is a tradeable-edge claim.**

## 6. Artifact

`data/derived/kalshi_hf_basis/hour=YYYYMMDDHH/part-0000.parquet`, one row per
aligned (pair, Kalshi snapshot), zstd, one file per books hour partition:

```
kalshi_ticker, polymarket_condition_id, series_ticker, family,
snapshot_ts_ns, kalshi_mid, kalshi_spread, pm_ts_ns, pm_mid, pm_stale_ms,
basis, ret_5s, ret_15s, ret_60s
```

`pm_ts_ns` is the timestamp of the PM quote used at `t`, so `pm_stale_ms` is
auditable per row. Basis and the three returns are null - never zero-filled -
when either endpoint is missing or stale; `ret_*s` is genuinely `0.0` only when
both endpoints resolve to the same quote. Hour partitions whose output exists
are skipped, so a multi-day re-run resumes.

## 7. Limits

* **One hour, one day.** The tape began 2026-11-05 19:11 UTC; the panel is
  49 minutes of it. `kalshi_books` was still being appended while this ran, so
  the row counts move between runs - treat them as timestamped, not stable.
* **The books stream is 5 s with a 60 s heartbeat**, and only writes when
  something changes. Pair-level snapshot spacing here runs from 5 s to 60 s
  (median 53 rows per pair per 49 minutes), so `h = 5 s` mostly measures "did
  the PM book move at all before the next Kalshi observation".
* **Kalshi snapshots are not independent of PM quote timestamps.** PM quotes
  are event-driven; the ASOF rule aligns to the last event, so a pair quotes
  its basis at an arbitrary point inside the PM quote's life. This is the
  honest direction (the Kalshi observation is the decision clock), but it
  means the basis is partly a measure of PM staleness.
* **Resolved / silent markets.** 4,747 snapshots have no PM quote inside 60 s;
  those are mostly resolved midterm elections where the PM book stopped
  updating. They are dropped, which biases the sample toward the markets that
  were still quoting (treasury ladders, crypto ladders).
* **Not tested here:** markout, OFI, queue position, rewards, Kalshi fees. The
  register's decision rule needs all of them.
* **Pairs are not independent**: a condition's two tokens move against each
  other, and several pairs share a Kalshi event (the 2Y/5Y/10Y/30Y ladders).
  Every pooled number above is reported with its pair count for that reason.

## 8. Reproduce

```bash
# build every missing sealed hour partition and print the report (~4 s per hour)
.venv/Scripts/python.exe scripts/report_kalshi_hf_basis.py

# rebuild one partition from scratch
.venv/Scripts/python.exe scripts/report_kalshi_hf_basis.py --hour 2026110519 --force

# the pure helpers only, no tape
.venv/Scripts/python.exe -m pytest tests/test_kalshi_hf_basis.py -q
```

Expected log line for the artifact-producing run:

```
hour=2026110519 kalshi_rows=7791 usable=7661 pm_quote_rows=23383 aligned=2914 snapshots_without_pm=4747
```

Single process, one DuckDB connection, `clob_events` scoped to the hour
partitions that intersect the snapshot window; no collector or config changes.
