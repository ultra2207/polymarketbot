# Gamma comments — do they predict a Polymarket mid move?

**Verdict: no.** Once the mid series is read with a fresh snapshot at both ends
of every bucket, comment activity predicts nothing about the next 5 minutes of
price. The only non-trivial measurement in the tape is a **×1.2 increase in
mid-change *count*** (jitter, not displacement) that is itself almost entirely
explained by the market's own recent churn (incremental R² = 0.0007). Comments
are also far too sparse to rule out anything smaller than a ×1.5 volatility
multiplier.

Scope warning up front: the collector polls comments for the **top 50 markets by
`daily_pool`** (`src/polymarketbot/data/streams/news.py::_gamma_loop`), i.e. the
reward universe — the same markets the replay notes say have almost no flow.
This dataset can therefore never tell us anything about liquid markets.

---

## 1. What is in the tape

| quantity | value |
|---|---|
| parquet rows | 32,915 |
| unique comments (`comment_id`) | **3,612** (the rest are poll duplicates) |
| distinct authors | 1,521 |
| parent entities observed | **40 events** — all `parent_entity_type = Event` |
| markets inside those events | 469 condition ids (the Gamma `Event` parent is shared by every market in the event) |
| `created_at` span | 2025-10-13 → 2026-11-06T08:21Z |
| collection (`recv_ts`) span | 2026-11-04T00:37Z → 2026-11-06T08:21Z |
| days / files | 3 days, 227 files, 8.7 MB |

Gamma rejects `parent_entity_type=Market` with 422, so comments arrive at event
granularity. I mapped event → condition through
`data/raw/gamma/snapshot_2026-11-01.parquet` (`event_ids` column): 36 of 40
events resolved, 269 conditions.

**Comment rate.** 3,612 comments across 40 events in 392 days of comment
history, but the activity is concentrated in the last ~5 weeks of it. Inside the
window where a Polymarket book exists (2026-11-04T00:36Z → 2026-11-06T09:39Z,
50 h) there are **409 comments across 24 events** — about 8 per hour in total,
across the whole top-50 universe. In 5-minute buckets that is 362 commented
event-buckets over 50 h.

Per-event counts (window) are concentrated: 57678 = 78, 59233 = 43, 79987 = 39,
31552 = 35, 81557 = 35, 1312156 = 32; the remaining 18 events have ≤27 each.
9.3 % of in-window bodies match bot/spam templates ("X spent N USD on … outcome
Yes", "has a N USD position on …"); excluding them does not change any
conclusion below.

**Book coverage.** Of the 269 conditions in those events, only **81 have any
`clob_events` rows at all** in the tape (the collector subscribes to a different
set), and those are sparse: 174,806 snapshot rows over 50 h for 81 markets ≈
44 rows/hour/market, i.e. a mid that is often minutes stale. The **trade tape**
does cover them — 88,172 prints over the same window — so fill quality is
measurable even where the book snapshots are thin.

---

## 2. Method

* Mid per condition at each snapshot = `(max bid + min ask)/2`; one token per
  condition (the one whose median mid is closest to 0.5, so the grid is not
  pinned to 0/1).
* 5-minute grid, **as-of alignment** (`np.searchsorted(..., side='right')`),
  never interpolation.
* Each condition-bucket is kept only if a snapshot exists **within `fresh_s` of
  both boundaries** — otherwise the "change" is censored, not measured. This
  single choice turns out to decide the answer; see §4.
* Outcome = event-level mean of `|mid_end − mid_start|` over the event's
  covered conditions; event = the unit of observation because comments are
  event-level and the markets inside an event move together.
* Driver = at least one comment in bucket *t*; outcome = bucket *t+1*
  (leading, not contemporaneous).
* Control = the same event's own outcome in bucket *t* (volatility clustering)
  plus event fixed effects, clustered by event (36 clusters).
* Baseline = each event's own median next-bucket `|Δmid|` (hit rate), and the
  event's own trailing churn.

Reproduce: `data/derived/CommentSignal/panel_300s.parquet`,
`panel_120s.parquet` (built here, not run by the replay).

---

## 3. Headline measurement (fresh ≈ 300 s — the flattering version)

n = 2,801 event-buckets, 119 with ≥1 comment (152 comments), 36 events,
2026-11-04T00:36Z → 2026-11-06T09:39Z.

| statistic | control buckets | comment buckets |
|---|---|---|
| mean next 5-min `abs(Δmid)` | **1.13 c** | 0.96 c |
| median next 5-min `abs(Δmid)` | 0.25 c | **0.375 c** |
| share with `Δmid == 0` exactly | **15.9 %** | 9.2 % |
| share above event's own 75th pct | 24.0 % | 31.9 % (n=119, p=0.053) |
| share above event's own 90th pct | 10.0 % | 12.6 % (p=0.36) |

Log-volatility regression, `ln|Δmid|(t+1) ~ comment(t) + own vol(t) + event FE`:

| specification | multiplier | t | n / treated |
|---|---|---|---|
| own vol + event FE | **×1.69** | +3.68 | 2,801 / 119 |
| + log book-update count | ×1.34 | +2.09 | 2,801 / 119 |
| **+ exact-zero changes excluded** | **×1.04** | **+0.32** | 2,363 / 108 |

(dropping exact-zero changes without the update control gives ×1.23, t = +1.57 —
i.e. the "effect" lives almost entirely in the buckets whose mid did not move
at all.)

Note the direction of the raw means: comment buckets have a *lower* mean but a
*higher* median and **materially fewer exact zeros**. The positive log
coefficient is a zero-inflation effect — stale feeds, not volatility.

---

## 4. The control that kills it

The mid series is only trustworthy when the last snapshot before each boundary
is recent. Requiring ≤120 s of staleness at both ends:

| spec (120 s freshness) | multiplier | 95 % CI | t | n / treated |
|---|---|---|---|---|
| own vol + event FE | ×1.34 | [0.94, 1.90] | +1.63 | 1,517 / 85 |
| **+ book-update count** | **×1.16** | **[0.81, 1.67]** | **+0.81** | 1,517 / 85 |
| + exact zeros excluded | ×1.10 | [0.85, 1.42] | +0.70 | 1,341 / 79 |

Tightening further to 60 s: ×1.02, t = +0.06 (n = 927, 61 treated). The
estimator is monotone in the freshness threshold (300 s → ×1.69, 120 s → ×1.16,
60 s → ×1.02), which is the signature of a measurement artefact rather than an
effect.

The decisive control is the **number of distinct mid changes in the outcome
bucket** (13.7 comment vs 9.7 control, 5-min). Conditioning on it:

| spec | multiplier | t |
|---|---|---|
| + log(#mid changes in the outcome bucket) | **×1.02** | +0.12 |
| + log(#mid changes) and zeros excluded | ×0.95 | −0.41 |

**What the market's own recent volatility already implies.** In the same
regression the event's prior-bucket volatility has coefficient 0.239
(t = +7.95) and the prior book-update count 0.665. Adding the comment flag moves
R² from 0.5034 to 0.5036 — an increment of 0.0002. Comments are redundant given
the tape's own recent state.

**Direction (null, as expected).** Signed next-bucket `Δmid` after a comment:
mean −0.00039 vs −0.00009 in control buckets; rank correlation between comment
count and signed change = −0.04 (n = 90 event-buckets). No direction.

**Not even contemporaneous.** The comment bucket's own mean `|Δmid|` is
1.09 c vs 1.16 c in control buckets on the same panel — comments are not a
reaction to a visible price jump either, on this universe. That is consistent
with these being reward markets where almost nothing trades: 12.6 % of comment
buckets show no mid change at all in the bucket the comment lands in.

**Fill toxicity (the print tape is rich enough to test this).** The trade tape
does cover these markets even where the book snapshots are thin:
**88,172 prints** on the 81 comment-markets over the same 50 h (1,545/h), 61,981
aggressor-BUY. Maker markout is measured from the mid at the print to the mid
60 s / 300 s later, signed against the aggressor, in cents. 23,272 prints
(26.4 %) fall within 5 minutes after a comment on their event.

| outcome | control prints (64,900) | after a comment (23,272) |
|---|---|---|
| median markout 60 s | −0.10 c | −0.10 c |
| mean markout 60 s | −0.02 c | +0.03 c |
| median markout 300 s | −0.05 c | −0.05 c |
| share of toxic prints (markout 60 s < 0) | 63.9 % | 58.3 % |

Regression with condition fixed effects, clustered by event: markout 60 s
**+0.035 c, t = +0.44**; markout 300 s −0.007 c, t = −0.08; n = 88,172. Zero,
with the sign pointing *away* from toxicity. Comments do not mark toxic fills.

**Live but useless.** Comments do predict *book activity*: +14.9 % more
mid changes in the next 5 minutes (t = +3.98, n = 1,517, 85 treated), robust to
leave-one-event-out (1.17–1.30 with the prev-churn control). Against a
trailing-3-bucket churn baseline (R² = 0.642) the multiplier falls to ×1.14
(t = 2.03) and adds R² = 0.0007. Book churn is observable from the tape at zero
lag, so a comment-based forecast of it is redundant. A +10.8 % next-bucket
spread response is directionally the same story and not significant (t = 1.77).

---

## 5. Power: too sparse to test anything small

On the 120 s-freshness panel, only **7 events ever have a treated bucket**, and
79 of the 85 (93 %) come from four of them (57678, 59233, 81557, 191531); on the
300 s panel four events carry 94 of 119. Their raw means disagree in sign:

| event | mean `abs(Δmid)` control | comment | n treated |
|---|---|---|---|---|
| 57678 | 1.95 c | 1.14 c | 41 |
| 59233 | 0.83 c | 1.26 c | 26 |
| 191531 | 3.56 c | 3.55 c | 6 |
| 81557 | 0.40 c | 0.59 c | 6 |

(each event's mean over all its 5-min buckets from the 120 s panel)

With 85 treated buckets the standard error of the log coefficient is ≈0.18, so
the 95 % interval rules out a multiplier above **×1.67** and nothing smaller.
A ×1.2 attention effect — realistically the size of anything tradeable — is
undetectable in 50 h of data on 24 events.

---

## 6. Verdict

1. **Comments do not predict a Polymarket mid move**, neither in magnitude nor
   in direction, on the only markets where the comment tape and the book tape
   overlap. Best-controlled multiplier ×1.02 (t = 0.12); own recent volatility
   and update frequency already carry everything (R² +0.0002). Nor do they
   predict **fill toxicity**: 88,172 prints, markout effect +0.035 c
   (t = +0.44) with the sign pointing away from toxicity.
2. The ×1.7 that a coarse as-of grid reports is **an artefact of stale mid
   reads**: comment buckets contain fewer exact-zero changes, and the apparent
   effect disappears as boundary freshness tightens from 300 s to 60 s
   (×1.69 → ×1.16 → ×1.02, t = +0.06 at 60 s).
3. The one real relationship is activity, not volatility: comments precede
   ~15–20 % more mid changes for 5 minutes. That is redundant with the book,
   which shows churn with zero lag, and it adds 0.0007 R².
4. **The dataset cannot answer the question properly at all.** It covers 40
   reward-universe events (top-50 by `daily_pool`), 409 comments inside the book
   window, 81 markets with any book data, and 4 events carrying 79–93 % of the
   treated buckets. If attention is worth testing again, the collector has to
   poll comments for the **liquid** universe (`condition_ids` from
   `build_highvol_universe.py`) — otherwise the test is structurally
   underpowered, whatever the analysis.

### If someone wants to retry
* Point `_gamma_loop`'s `_top_markets` at the high-volume universe, not
  `daily_pool`. Comments must be sampled on the same markets the strategy can
  actually quote.
* Require ≤60 s boundary staleness by construction, or read the mid from
  `best_bid_ask` events (not full-book snapshots) so buckets are not censored.
* Pre-register the multiplier threshold; ×1.2 on volatility is the size the
  reward economics would need, and this tape cannot resolve it.

### Files
`data/derived/CommentSignal/comments_mids_11{04,05,06}.parquet` — per-snapshot
`(condition, token, best bid, best ask)` for the 81 comment-markets with tape;
`mids_all.parquet`, `trades.parquet`, `print_markouts.parquet` — the mid series
and per-print markouts used in the toxicity test; `panel_300s.parquet` /
`panel_120s.parquet` — the event-level 5-minute panels (driver, outcome,
controls).
