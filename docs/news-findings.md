# News arrivals as a Polymarket signal — measured, negative, blocked by entity resolution

Owner: `NewsSignal`. Dataset: `data/tape/news_items`.
Question: does a news arrival predict a Polymarket mid move, or the toxicity of a fill,
better than the book itself does?

**Verdict: no measurable effect, and the binding limitation is entity resolution,
not effect size.** The corpus is crypto news; the venue's tradable universe is
weather, sports props, politics and macro. 5,637 rows dedupe to 1,022 distinct
articles and exactly **32 of them resolve to a condition that is on the tape**
(30 of those yield a usable window). At that n the effect is measured to ±5 bps
and is zero. Even a large effect could not have been detected here, which is why
the resolution rate — not the t-statistic — is the finding.

All mid changes below are Δ(probability) × 1e4, i.e. **basis points of contract
price**; 100 bps = 1 cent. A 31.7 bps move is a 0.32 c move.

---

## 1. What the file is

`data/tape/news_items/date=*/hour=*/*.parquet` — 241 parts, 5.4 MB.

| property | value |
|---|---|
| rows | 5,637 |
| distinct articles (dedupe on `url`) | **1,022** |
| duplicate factor | 5.5× — every poll re-emits the whole feed |
| collector ran | 2026-11-04 00:37Z → 2026-11-06 09:11Z |
| `published_ts_ms` span | 2026-05-26 (one GDELT backfill) → 2026-11-06 09:07Z |
| columns | `recv_ts_ns`, `recv_mono_ns`, `source`, `title`, `url`, `published_ts_ms`, `summary`, `language`, `raw_json` |

Distinct articles by source: `news.google.com` 704, `coindesk.com` 85, `gdelt` 80,
`cointelegraph.com` 78, `decrypt.co` 74.

**No structured entities.** `raw_json` is the untouched RSS item — `title`, `link`,
`guid`, `description`, plus `domain`/`sourcecountry` for GDELT. There is no ticker
field, no condition id, no gazetteer output, nothing that names a market. Every
join in this document is a keyword match on `title || summary`, which is precisely
why resolution is the constraint.

Arrival timing is a collector artefact: first sighting per article minus
`published_ts_ms` has p5 = 92 s, p50 = **5.4 h**, p90 = 5.2 d; only 67 of 1,022
(6.6 %) arrive within 2 minutes. The same feed snapshot is re-emitted every few
minutes and backlogs arrive in bursts, so `recv_ts_ns` measures the collector, not
the news. The study is therefore anchored on `published_ts_ms` (information time)
and repeated on first-receipt time for the timely subset.

## 2. Resolution — the number that decides this channel

Universe `20261106T092900.json`: 2,152 conditions. News corpus: crypto only.

| step | count | share of rows |
|---|---|---|
| rows on tape | 5,637 | 100 % |
| distinct articles | 1,022 | 18 % of rows |
| articles naming BTC / ETH / SOL | 356 | 6.3 % |
| **articles resolving to a crypto price condition that is on the tape** | **32** | **0.57 %** |
| items with ≥ 1 valid (item, condition) window | 30 | 0.53 % |

That last pair is the binding number: **30 / 5,637 = 0.5 %**. The other 99.5 % of
the corpus names nothing this venue lists — weather cities, NFL players, Senate
candidates, Fed decisions. Zero of 1,022 articles match a non-crypto universe
subject.

Two independent reasons the intersection is this thin:

1. **Disjoint corpora.** 200 of the 201 crypto conditions have `daily_pool = 0`, so
   even a working signal there would fund no reward book; and the reward-paying
   universe (weather/sports/politics) is not in the corpus at all.
2. **Coverage.** Crypto `condition_id`s appear in `clob_events` only from
   2026-11-05 03:58Z to 2026-11-06 09:50Z (11-04 carries 7,128 distinct assets, none
   of the 362 crypto tokens; 11-05 00Z has 4 book assets — a collector restart).
   Only 237 of the 1,022 articles were published inside that window, of which 32
   resolve.

## 3. Measurement

Per-condition mid from the tape's own L1: `price_changes[].best_bid/ask`, YES token,
`(bb+ba)/2`, reduced to change points, aligned as-of with
`searchsorted(side='right')` — no interpolation. A window is used only if the
condition quoted within 120 s of the anchor. 95 of 181 conditions have ≥ 500 mid
changes; those are the ones that actually quote.

**Baseline**: mean |Δmid| per condition on a 60 s grid over its own quoting life,
so a ratio of 1.00 means "no reaction relative to a typical minute in this market".
For the conditions the items touch, the baseline is **32.03 bps**.

### 60-second windows (n = 30 items, 423 item-condition observations)

| | mean \|Δmid\| |
|---|---|
| 60 s **after** the item | **31.70 bps** |
| 60 s **before** the item | **44.27 bps** |
| baseline, same conditions, random times | **32.03 bps** |

After the headline the market moves exactly as much as it does in a random minute.
Before it, it moves 38 % more. **The move precedes the publication, not the other
way round.**

Per item, net (post-window movement − that item's baseline) = **−2.75 bps**,
sd 15.51, **n = 30, t = −0.97**. A rule that traded the headline direction would
have to overcome a point estimate of zero with a standard error of 2.8 bps.

### Matched control (200 placebo anchors per item, ±3 h, excluding ±15 min of any item)

The placebo matters because item times cluster in volatile regimes — random minutes
next to an item run 1.16× the all-time baseline. Against that control:

| window | observed ÷ baseline | placebo ÷ baseline | paired difference |
|---|---|---|---|
| 60 s after | 0.84 | 1.16 | −0.32 (t = −2.97, n = 31) |
| 60 s before | 1.24 | 1.47 | −0.21 (t = −1.65) |
| 300 s after | 1.04 | 1.18 | −0.12 (t = −0.88) |
| 300 s before | 1.00 | 1.42 | −0.42 (t = −3.73) |

Absolute 5-minute numbers: 80.2 bps after, 87.9 bps before, 85.1 bps baseline,
signed Δ300 = −5.0 bps (t = −0.82). Nothing survives.

### Receipt-anchored (what could actually have been traded)

19 items with ≤ 300 s receipt latency, 215 observations: mean |Δmid| 60 s after =
1.158× baseline against a matched placebo of 1.242×, paired difference −0.084
(t = −0.51). The liquid subset behaves like the whole.

### Direction

Lexicon sentiment on the 23 items with a directional headline (12 bullish, 11
bearish): hit rate **13/23 = 57 %** against a 50 % base (one-sided p ≈ 0.34), and
the mean signed response is −7.5 bps on *bullish* headlines — the wrong sign. No
single item produces a 60 s move above 42 bps against a 13.7 bps median baseline.

Caveat on the anchor: several CoinDesk "Live updates" posts are continuously edited
rolling articles stamped with their original creation time, and one Bitcoin story
appears as 8 near-duplicate rows. That inflates the item count and smears the
anchor; it cannot manufacture a reaction that is not there, but it is another
reason not to trust 30 as a stable n.

## 4. Fill toxicity — no overlap, therefore untestable

`data/derived/replay/*/fills.parquet`: 95,391 fills, of which **15,742 crypto fills**
carry `markout_5m` (mean +957 bps, median +289 bps). They cover 12 conditions and a
single 69-minute window, **2026-11-06 02:13Z – 03:22Z**.

News items within ±1 h of that window: **1**. Fills inside 5 minutes after it: **0**.

Where fills exist, news does not; where news exists, fills do not. "Does a news
arrival precede toxic fills" has no measurement on this data. This is a coverage
fact, not a null.

## 5. What this rules out, and what it does not

Ruled out: that this corpus, as instrumented, is a usable predictor of Polymarket
mid moves. Both the point estimate and the sign are against it — post-window
movement equals the baseline, pre-window movement exceeds it, and on the timely
subset the ratio is indistinguishable from a matched placebo.

Not ruled out: that crypto news contains information the venue's book does not
price. n = 30 cannot exclude an effect smaller than roughly ±5 bps per item, and
the threshold ladders tested are long-dated contracts whose 60 s beta to spot is
small. The honest version of that question — does the headline predict the
*residual* of the ladder mid against `binance_bbo` — needs an order of magnitude
more resolved items than this tape can supply.

What would change the answer, in order of leverage:

1. **Entity resolution** (the binding constraint): a reported-entity feed — injury
   reports for player props, NOAA for the weather ladder, FOMC/CPI calendars for
   macro. One of those corpora would resolve thousands of items against markets
   that actually pay a pool, instead of 30 against markets that pay nothing.
2. **Coverage**: crypto price markets on the tape for the whole collector window,
   plus a collector that stamps true arrival rather than re-emitting snapshots
   (median first-sighting latency is 5.4 h; p5 is 92 s).
3. **Sample**: ≥ 200 resolved items over ≥ 30 days, so a sentiment hit rate can be
   resolved to ±0.07 and a 5 bps effect to ±1 bps.

## Reproduction

```sql
-- recon and dedupe
SELECT source, title, url, min(recv_ts_ns), any_value(published_ts_ms)
FROM read_parquet('data/tape/news_items/date=*/*/*.parquet') GROUP BY 1,2,3;
```

Mid extraction is a hash semi-join on the token set against
`clob_events/date=*/hour=*`: an `IN`-list of 362 tokens over 9.6 M rows costs
100 s per partition, the semi-join 2.3 s — the whole 52-partition series built in
248 s.

```
data/derived/NewsSignal/mids/<date>-<hour>.parquet   raw per-token L1, 118 M rows
data/derived/NewsSignal/series.parquet               181 conditions, change points only
data/derived/NewsSignal/item_cond_windows.parquet    423 item x condition windows
data/derived/NewsSignal/obs_windows.parquet          windows with pre/post both valid
data/derived/NewsSignal/placebo_windows.parquet      200 placebo anchors per item
data/derived/NewsSignal/item_stats.parquet           per-item ratios
data/derived/NewsSignal/item_sentiment.parquet       per-item signed response
data/derived/NewsSignal/items_dedup.parquet          1,022 deduped articles
```
