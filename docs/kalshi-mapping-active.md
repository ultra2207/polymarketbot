# Kalshi mapping built from the active Polymarket universe

> **Status (2026-11-11).** The artifact and the numbers below are a 2026-11-06 record. The
> builder named here (`polymarketbot.data.kalshi_mapping`) no longer exists, so neither
> command in this file can be re-run from this tree.

`data/derived/kalshi_mapping_active_v2.parquet` — 13 Polymarket conditions that
actually trade, each joined to the Kalshi market that states the same claim,
with the six-hour book-event count and the measured freshness of the Polymarket
mid against the Kalshi book.

Built by the retired active-mapping builder (superseded by `python -m polymarketbot.data.kalshi_mapping --artifact v2`):

```
python -m polymarketbot.data.kalshi_mapping --artifact v1
```

Direction is reversed versus `kalshi_mapping_v2.parquet`: v2 started from every
open Kalshi series and found a Polymarket question, which lands on reward-tail
markets. This artifact starts from the Polymarket conditions with the most book
events and asks whether Kalshi lists the same claim.

## Numbers

| step | value |
|---|---|
| Polymarket conditions with any event in the window | 5,292 |
| conditions ranked (top by book events) | **200** (cut at 364 events) |
| attempted (every ranked condition is parsed and tested) | **200** |
| matched to a Kalshi market | **13** |
| qualified (≥ 200 book events in 6 h) | **13 of 13** |
| matched tickers that have Kalshi book rows in the window | **11 of 13** |

Window: `clob_events` `date=2026-11-06` hours 00–05 (six complete hour
partitions; hour 06 is partial and excluded). Every partition is read alone and
its row count is checked against the parquet metadata before use — 60,299,385
rows over the six hours, the largest partition 10.6 M rows. The whole script,
ranking plus matching plus both measurements, runs in about 40 s.

`pm_events_6h` counts **condition-level** rows: every `clob_events` row whose
`condition_id` is non-null (`book`, `best_bid_ask`, `new_market`,
`tick_size_change`, `gap`). This is the row set the replay harness consumes —
`iter_events` joins `clob_events` to the wanted conditions on `condition_id`, so
its "book events" counter advances on exactly these rows. `price_change` rows
carry a NULL `condition_id` (the payload is a list of per-asset changes) and are
excluded on both sides of the comparison.

That distinction matters and is stated plainly here: under the narrower
`event_type = 'book'` count quoted in `docs/replay-results.md`
(median 2 per condition over six hours), **none** of the 13 would clear 200 —
their median is 4, maximum 150. `event_type = 'book'` counts full snapshots,
which are emitted on (re)subscription; hour 00 alone carries 19,033 of them
against ~7,000 in each other hour, while condition-level rows are *lower* in
hour 00 (100k) than in hour 02 (150k). The snapshot count tracks collector
reconnects, not trading.

### Events per condition, the 13 matches

| bucket | conditions |
|---|---|
| 200–500 | 1 |
| 500–1,000 | 5 |
| 1,000–5,000 | 6 |
| 5,000+ | 1 |

min 411 · p25 811 · median **1,031** · p75 1,677 · p90 3,491 · max 20,968.

For comparison, counting `event_type='book'` on this window leaves the v2 map's
107 pairs at a median of 2 and 42 of 107 with zero (the earlier audit quoted 47).
Eleven of these 13 rows also appear in v2 with the same ticker; the other 96 v2
rows are the silent tail (six-hour median 2 `book` snapshots).

### PM-mid staleness against the Kalshi book

Gap = Kalshi snapshot `recv_ts_ns` − last Polymarket book event `venue_ts_ms`
for the same condition (the join `cross-kalshi-basis-side-pull/prepare.sql`
uses). Pairing is inside one hour partition; a Kalshi row with no Polymarket
event in its own hour is dropped rather than paired across the boundary.

| measure | value |
|---|---|
| aligned snapshots | 10,737 |
| pooled median gap | **39 s** |
| pooled p90 gap | **589 s** |
| share of snapshots within 60 s | 55.9 % |
| per-condition median, distribution | min 3 s · median **83 s** · p90 147 s · max 210 s |
| per-condition p90, distribution | min 170 s · median 873 s · p90 1,832 s · max 1,951 s |

The v2 map's basis join reported a median of 255 s and p90 of 2,634 s
(`docs/replay-results.md`); the active map is about 6× fresher at the median,
which is the whole point of selecting on activity.

Hour 04 has **zero** `best_bid_ask` rows in the tape (10.0 M `price_change` rows,
no condition-level top-of-book stream). It is named here because it would
otherwise manufacture an hour of staleness; the per-hour pairing rule makes the
hour contribute nothing, and no other hour is affected.

## The 13 matches

| PM question | Kalshi ticker | events 6 h | median gap | p90 gap | conf | rule |
|---|---|---:|---:|---:|---:|---|
| Will Democrats win exactly 5 House seats in Arizona in the 2026 midterm elections? | `KXHOUSEWINSTATE-AZD-E5` | 20,968 | 3 s | 386 s | 0.95 | house_seats_exact |
| Will Democrats win exactly 6 House seats in Arizona in the 2026 midterm elections? | `KXHOUSEWINSTATE-AZD-E6` | 3,645 | 8 s | 226 s | 0.95 | house_seats_exact |
| Will the 30-year Treasury yield dip below 5.08% in November? | `KX30YRDIRLM-26NOV30L-T5.08` | 2,876 | 36 s | 873 s | 0.92 | treasury_bound |
| Callum Turner announced as next James Bond? | `KXBOND-30-CAL` | 1,677 | – | – | 0.72 | title_fuzzy |
| Will the Republican Party win the AZ-02 House seat? | `HOUSEAZ2-26-R` | 1,067 | 90 s | 669 s | 0.93 | house_race_winner |
| Will the 30-year Treasury yield dip below 5.11% in November? | `KX30YRDIRLM-26NOV30L-T5.11` | 1,048 | 147 s | 1,832 s | 0.92 | treasury_bound |
| Will the 5-year Treasury yield dip below 4.50% in November? | `KX5YRDIRLM-26NOV30L-T4.50` | 1,031 | 61 s | 1,721 s | 0.92 | treasury_bound |
| Will the 30-year Treasury yield hit 5.65% in November? | `KX30YRDIRHM-26NOV30H-T5.64` | 954 | – | – | 0.80 | treasury_bound_quantised |
| Will the Republican Party win the CA-23 House seat? | `KXHOUSERACE-CA23-26-R` | 865 | 83 s | 471 s | 0.93 | house_race_winner |
| Will the 30-year Treasury yield hit 5.42% in November? | `KX30YRDIRHM-26NOV30H-T5.41` | 811 | 5 s | 170 s | 0.80 | treasury_bound_quantised |
| Will the 10-year Treasury yield dip below 4.76% in November? | `KX10YRDIRLM-26NOV30L-T4.76` | 543 | 100 s | 1,040 s | 0.92 | treasury_bound |
| Will the 2-year Treasury yield dip below 4.38% in November? | `KX2YRDIRLM-26NOV30L-T4.38` | 536 | 104 s | 1,951 s | 0.92 | treasury_bound |
| Will Ethereum dip to $1,000 by December 31, 2026? | `KXETHMINY-27JAN01-1000` | 411 | 210 s | 1,168 s | 0.85 | crypto_annual_bound |

Two rows carry no staleness: `KXBOND-30-CAL` and
`KX30YRDIRHM-26NOV30H-T5.64` are in the Kalshi catalog but not in the 107
tickers the collector polls, so the tape has no Kalshi book to compare against.
They are emitted because the claim is exact and the point of the artifact is to
tell the collector what to poll — but they are not counted in the staleness
distribution.

### How each rule is justified

| rule | one sentence |
|---|---|
| `house_seats_exact` | Kalshi's title is the same sentence with the same state, party and seat count ("exactly 5 seats … in Arizona"). |
| `house_race_winner` | Same district and same party; Kalshi spells the district `AZ-2` where Polymarket writes `AZ-02`. |
| `treasury_bound` | Same tenor, same boundary to the basis point, same month; Kalshi resolves "below X" and Polymarket "dip below X" over the same November window. |
| `treasury_bound_quantised` | Polymarket's "hit X%" and Kalshi's "above (X−0.01)%" are the same touch condition because Kalshi strikes are the basis point below the level (the Kalshi sub-title reads "X% or above"). |
| `crypto_annual_bound` | Same asset, same strike, same end of 2026; Kalshi's "by Jan 1, 2027 12:00 AM ET" is Polymarket's "by December 31, 2026". |
| `title_fuzzy` | Identical claim after stop-word removal (token Jaccard 1.00, identical numbers); only the resolution wording differs — Polymarket asks about the announcement, Kalshi about the outcome, which is the same casting event. |

## What could not be matched, and why

187 of the 200 active conditions have no defensible Kalshi counterpart in the
local catalog. By family:

| family | in top 200 | matched | why not |
|---|---:|---:|---|
| weather (city daily high/low temperature, river levels) | 53 | 0 | The Kalshi catalog here has no daily city-temperature series (`KXHIGHNY`, `KXHIGHT…` are absent from the markets cache, the events cache and the polled tape). |
| politics / macro | 41 | 11 | Matched where the claim shape agrees; the rest are margin *ranges* (`win by 15%–20%`), which need two Kalshi thresholds, not one. |
| crypto / private-company valuations | 33 | 1 | Kalshi lists annual max/min strikes, not the daily/weekly strikes most of these use; `Databricks`, `Stripe`, `Anthropic` IPOs have no Kalshi market here. |
| AI releases, media, sports, esports, social counts, geopolitics, equities, misc | 73 | 1 | Mostly not listed on Kalshi at all (Netflix/Steam/box office, tweet counts, `LoL` game winners); the `NFL`/`NBA` entries in the cache are a handful of moneyline games, not the spreads and totals these questions use. |

Deliberate rejects, to show the bar:

- `Will the 30-year Treasury yield dip below 5.05% in November?` — Kalshi's
  nearest 30-year strike is 5.08 %; a 3 bp gap is a different market.
- `Will Solana reach $280 by December 31, 2026?` — Kalshi lists 200/300, not 280.
- `Will Ethereum reach $3,500 in November?` — Kalshi's $3,500 strike resolves
  "by Jan 1, 2027", a strictly wider window than November.
- `Will the Republican Party candidate win the 2026 GA-09 House election by
  15%–20%?` — a range; Kalshi sells "at least X points" thresholds, and pairing
  one of them would silently bet on one side of the range.
- `Will Chuck Schumer be the next Senate Majority Leader?` — Kalshi lists the
  Senate *Democratic Leader* election, a different office.
- `Pete Hegseth out as Secretary of Defense by November 30?`, `Will there be no
  change in Fed interest rates after the December 2026 meeting?` — both match
  exactly (`KXHEGSETHOUT-26AUG-26DEC01`, `KXFEDDECISION-26DEC-H0`) but neither is
  in the top 200 by the count above (they rank on `book` snapshots, not on
  condition-level events), so they are not in the file.

## Caveats

- **This is a coverage result as much as a matching result.** The local Kalshi
  catalog is 20,344 distinct tickers (markets cache + macro cache + polled
  tape), and it is concentrated in politics, macro and annual crypto. Weather,
  sports and esports are 32 % of the active top 200, and Kalshi does list some of
  those series in production but not in this tape. A collector that walked the missing series
  would raise the match count; the matcher would pick them up unchanged.
- **`price_change` is the biggest book-event stream and is not counted.** Those
  rows carry no `condition_id`. Attributed through the per-change `asset_id`
  they total 119.7 M elements in the window, and ranking on them puts the
  top-200 cut at 111,246, keeping only 4 of these 13 matches — that measure
  ranks quote churn rather than top-of-book change, so it is reported here as a
  sensitivity check, not used.
- **Staleness is censored by the hour partition.** A Kalshi row whose last
  Polymarket update sits in the previous hour is dropped, so p90 is bounded by
  3,600 s. Hour 04 would inflate every measure because the tape has no
  `best_bid_ask` there.
- **Two of 13 have no Kalshi book** (`KXBOND-30-CAL`,
  `KX30YRDIRHM-26NOV30H-T5.64`); their staleness is undefined until the
  collector polls them.
- **All 13 are `direction = normal`** (Polymarket YES is Kalshi YES). No
  inverted pair was accepted, so the sign logic in the cross-venue module is
  exercised only through the v2 rows it still carries.

## Reproduction

```
python -m polymarketbot.data.kalshi_mapping --artifact v1
# writes data/derived/kalshi_mapping_active_v2.parquet (13 rows, 18 columns:
# 15 v2 columns + pm_events_6h + match_method + match_note)
```

The script prints the expected row count of each partition before reading it,
the two measures above, and the table in this document.
