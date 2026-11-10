# Kalshi ↔ Polymarket mapping: precision audit and v2

Status: measured 2026-11-06 (IST) against the live 2,464-market Polymarket cut
(`data/tape/manifest_current.json`) and Kalshi production public REST
(`api.elections.kalshi.com/trade-api/v2`, no key). The legacy artifact is left
untouched; the new one is `data/derived/kalshi_mapping_v2.parquet`.

Everything below is a measurement unless it appears under **Inferred**.

## 1. What v1 was

`polymarketbot.data.kalshi_mapping` crawls open Kalshi *events*, prefilters them
against the Polymarket questions, fetches the markets of the survivors and
scores each Polymarket × Kalshi title pair with
`polymarketbot.data.kalshi_mapping.score_pair` (structure + token Jaccard).

Measured state of `data/derived/kalshi_mapping.parquet`:

| metric | value |
|---|---|
| rows | 233 |
| distinct Kalshi events | 136 |
| Polymarket markets mapped | 233 of 1,538 at build time (15.2%) |
| same rows present in today's 2,464-market universe | 55 (23.6% of rows) |
| methods | `structured_winner` 103, `margin_threshold` 64, `title_fuzzy` 41, `turnout_threshold` 14, `title_exact` 6, `structured_contest` 5 |
| confidence range | 0.525 – 0.970 |
| direction | not recorded |

The artifact was built on 2026-11-04 against a midterm-heavy universe. Three
days later most of those contests have settled: in the 25-pair audit sample,
11 of 25 Kalshi markets are already `finalized`. That is decay, not
mis-mapping, but it means v1 coverage is largely historical now.

## 2. Precision audit, v1

Method: `random.seed(20261106)`, `random.sample(rows, 25)` over the 233-row
artifact (reproducible). For each pair the Kalshi market and its event were
fetched live (`GET /markets?tickers=…`, 25/25 returned) and the Polymarket
question was read from the manifest. A pair is **correct** only when both
venues resolve on the same proposition (same contest, same outcome, same
claim type).

**Result: 16 / 25 correct = 0.64 point estimate; 95% Wilson interval
0.445 – 0.798** (n = 25).

By method in the sample: `structured_winner` 9/10, `title_fuzzy` 4/4,
`margin_threshold` 0/6, `turnout_threshold` 0/1, `structured_contest` 0/1.

### Named failures (all 9)

| # | Polymarket | Kalshi | Why it is wrong |
|---|---|---|---|
| 2 | Texas governor, Rep. candidate wins **by 3%–6%** | `KXMIDTERMMOV-TXGOVR-P3.5` "margin … **at least 3.5** points" | interval vs one-sided claim; Kalshi YES includes 6%+ |
| 5 | North Carolina Senate, Dem. candidate wins **by 12%–15%** | `KXMIDTERMMOV-NCSEND-P12` "**at least 12** points" | same interval/threshold error |
| 6 | WI-05 House, Rep. wins **by 15%–20%** | `KXMIDTERMMOV-WI05R-P15` "**at least 15** points" | same |
| 10 | Mississippi Senate turnout **between 800,000 and 900,000** | `KXMIDTERMVOTETURN-MSSEN-830000` "**above 830,000**" | interval vs one-sided |
| 13 | Georgia governor, Dem. wins **by 6%–9%** | `KXMIDTERMMOV-GAGOVD-P6` "**at least 6** points" | same |
| 14 | Republicans win the **Kansas governor** race | `KXKSSENGOVCOMBO-26NOV-REPREP` "Kansas **Governor** winner … **and Kansas Senate** winner …" | Kalshi is a joint (combo) claim; the sub-title is "Ty Masterson and Roger Marshall win" |
| 17 | Iowa Senate, Rep. wins **by 0%–3%** | `KXMIDTERMMOV-IASENR-P0.5` "**at least 0.5** points" | same |
| 18 | "Texas Democrat **perform best among these tossup Senate races**" | `KXSENATEWITHIN1-26NOV03-TX` "Texas Senate **decided by 1 point or less**" | different claim entirely |
| 19 | Iowa Senate, Dem. wins **by 3%–6%** | `KXMIDTERMMOV-IASEND-P3` "**at least 3** points" | same |

Two of the 16 "correct" pairs are also worth flagging as adjacent rather than
identical: `KXPRESNOMD` maps "win the nomination" to "be the nominee" (same
proposition, different wording), and the MMA `KXMMAFIGHTNEXT` markets map
"fight X next" to "next fight … : X" (same proposition).

### Root causes

1. **Interval vs threshold.** The matcher scored a Polymarket bucket
   (`by 9%–12%`) against the Kalshi rung whose strike equals the bucket's
   lower bound (`>= 9%`). Those are nested, not equal, claims. 64 of the 233
   v1 rows are `margin_threshold` and 14 are `turnout_threshold`; every one of
   the 7 such rows in the sample is wrong.
2. **Composite claims.** A single-contest question matched a Kalshi market
   whose resolution requires two contests (governor *and* Senate).
3. **Ticker-suffix structure.** `KXNFLGAME-…-NYG` parses as "New York
   governor" (`NY` + `G`) inside the structural extractor. v2 hit the same
   trap on "Will Kathy Hochul win Nassau County in the New York governor
   race?" until an office-word guard was added.
4. **No direction.** Nothing in v1 says whether Kalshi YES equals
   Polymarket YES or its complement.

## 3. v2 design

Implemented in the same script (`--artifact v2`), artifact
`data/derived/kalshi_mapping_v2.parquet`.

* **Anchors, not question strings.** A Polymarket question is routed to one
  topic — `fed_decision`, `fed_count`, `fed_sequence`, `treasury_bound`,
  `crypto`, `cpi`, `pce`, `payrolls`, `unemployment`, `gdp`, `shutdown`,
  `debt_limit`, `tariff`, `mortgage`, `approval`, `election` — and only the
  anchored rule for that topic may emit a pair. Kalshi candidates are chosen
  per **series and event**, then filtered by the same topic and the same
  settlement window.
* **Normalisation.** Case/Unicode/punctuation folding is inherited from
  `kalshi_mapping.normalise`; v2 adds explicit window parsing
  (`in November`, `by Dec 31, 2026`, `before 2027`, `on Dec 12, 2026`, and
  the Jan-1 ≡ Dec-31 equivalence) and dollar/threshold parsing (`$150k`,
  `$1.2m`, `25 bps`, `between 75k and 100k`).
* **Explicit direction.** `normal` when the two claims have the same
  polarity; `inverted` only when the claim texts are identical apart from one
  polarity flip (≥0.85 claim-token overlap and equal numbers); otherwise the
  pair is rejected. No inverted pair survived the 2026-11-06 cut.
* **Confidence and reason.** Every row carries `confidence` in [0, 1] and a
  human-readable `match_reason` naming the anchor, window, threshold
  relation and (where relevant) the boundary caveat.
* **Rejections are recorded.** The JSON summary counts every refusal, e.g.
  `method:margin_threshold` 35, `window_mismatch` 96,
  `direction_ambiguous` 34, `macro:no Kalshi rung resolves the same
  asset/kind/threshold/window` 116.
* **Live filter.** Every proposed ticker is re-read with
  `GET /markets?tickers=…` (100 per request) and dropped unless its status is
  active/open.

## 4. v2 artifact

| metric | value |
|---|---|
| pairs | **107** |
| distinct Kalshi events | **44** |
| distinct Kalshi series | 31 |
| Polymarket markets covered | 107 / 2,464 = 4.3% |
| direction | `normal` 107, `inverted` 0 |
| confidence 0.7–0.8 | 15 |
| confidence 0.8–0.9 | 21 |
| confidence 0.9–1.0 | 71 |
| live requests for the last rebuild | 2 (events/macro caches reused); cold run ≈ 180 |
| artifact | `data/derived/kalshi_mapping_v2.parquet` |

### Pairs per series family (measured)

| family | series | pairs |
|---|---|---|
| crypto running max | KXBTCMAXY (BTC), KXETHMAXY (ETH), KXSOLMAXY (SOL) | 7 + 5 + 4 |
| crypto running min | KXBTCMINY, KXETHMINY | 4 + 3 |
| treasury yield running max | KX2YRDIRHM, KX5YRDIRHM, KX10YRDIRHM, KX30YRDIRHM | 6 + 5 + 2 + 5 |
| treasury yield running min | KX2YRDIRLM, KX5YRDIRLM, KX10YRDIRLM, KX30YRDIRLM | 3 + 4 + 1 + 4 |
| FOMC meeting bucket | KXFEDDECISION | 8 |
| Fed cut count | KXRATECUTCOUNT | 1 |
| election / other — structural winner | `KXHOUSERACE`, `HOUSEAZ2`, `SENATEMS`, `KXAKSENATE`, … | 19 |
| election / other — exact title | `KXFRENCHPRES`, `KXDFBPOKAL` | 6 |
| election / other — fuzzy title | `KXPRESNOMD`, `KXDPRESPRIMARY`, `KXHOUSEWINSTATE`, NASCAR, LOL, … | 20 |

Crypto is the strongest overlap: 23 pairs (21.5% of the v2 rows); treasury ladders add 30 (28%). The yearly
BTC/ETH/SOL running-max/min ladders align strike-for-strike with Polymarket's
"What price will X hit in 2026?" ladder.

The election side is intentionally smaller than v1: 64 margin/turnout bucket
rows were dropped as unsound, 35 `structured_contest` candidates and 96
window mismatches were rejected. 23 tickers are shared with v1.

## 5. Kalshi series with no Polymarket counterpart (measured)

`kalshi_mapping_v2_summary.json → macro_series_without_pairs` lists 62 series
that had a topic+window match but produced no pair. Grouped by reason:

| family | series (examples) | measured reason |
|---|---|---|
| payrolls | `KXPAYROLLS`, `KXJOBREVISIONS` | PM publishes **intervals** (`50k–75k`, `75k–100k`) and one inclusive `at least 100k`; Kalshi publishes one-sided **strict** `above N`. 3 interval refusals + 1 boundary refusal. |
| core PCE | `KXPCECORE`, `KXCPICOMBO` | PM asks **exact** `0.2%`/`0.3%` and **YoY** levels; the Kalshi event is **MoM `above N`**. 9 refusals. |
| CPI | `KXCPICOREHEADMOM`, (US CPI events with no year in the title were not fetched) | PM's live CPI questions are China / UK / South Africa annual inflation; Kalshi's matching series are US CPI components. 2 refusals. |
| GDP | `KXGDP`/`KXGDPYEAR`/`KXNOMGDPGROWTH` | PM's live GDP questions are Japan Q3 brackets; Kalshi has US quarterly/annual and other countries' nominal levels. 4 refusals. |
| government shutdown | `KXGOVTSHUTDOWN`, `KXGOVTSHUTLENGTH`, `KXNUMSHUTDOWNS` | PM asks "shutdown **by** Dec 12/31"; Kalshi asks "shutdown **on** Dec 12" (point in time) or counts shutdowns in **2026** while the PM count question is for **2027**. 6 refusals. |
| Trump approval | `KXAPRPOTUS` (`… according to RealClearPolitics`) | Kalshi names RCP; the Polymarket manifest carries no resolution source/description, so two different polling averages cannot be ruled out. 6 refusals. |
| Fed sequence / hike count | `KXFEDCOMBO`, `KXFEDHIKE`, `KXFEDCUT` | PM asks a 3-meeting sequence (Pause-Hike-Pause) or "1 hike in 2026"; Kalshi sells single-meeting rate+dissent combos and by-date binaries. 4 refusals. |
| crypto, other constructions | `KXBTCD`, `KXETHD`, `KX*15M`, `KXBTCY`, `KXETHY`, `KXSOLD26`, `KX*ABOVEBY`, `KX*MAXMON`/`MINMON` | point-in-time price or 15-minute direction (PM does not state the observation time), end-of-year **level** ladders (PM asks running max/min), and "trimmed mean" ladders (a different statistic). 63 + 116 refusals. |
| yields, other tenors/windows | `KX7YRDIRHM/LM`, `KX*RATE15M`, `KXNOTE10Y` | no PM question for 7Y, no 15-minute PM yield question, and year-end **level** ≠ running max/min. 23 refusals. |
| misc | `KXBTCVSGOLD`, `KXINXVSBTC`, `KXBTCRESERVE`, `KXSATOSHIBTCYEAR` | different claim type (relative performance, reserve, "Satoshi moves coins"). |

`KXCPI`, `KXCPIYOY`, `KXCPICORE`, `KXCPICOREYOY` were **not** selected in this
cut because their event titles omit the year (`CPI in October`) and no
Polymarket question asks US CPI this week, so there was no window to anchor on.
That is a coverage gap, not a rejection.

## 6. Measured vs inferred

**Measured** (from API responses or artifact rows):

* endpoint shapes, field names, `yes_sub_title`, `floor_strike`/`cap_strike`,
  statuses, the 106 output rows, request counts, method/confidence/direction
  distributions, the 25-pair audit and its CI;
* Kalshi strike grids: BTC max strikes end in `.99` (e.g. `$149,999.99`),
  ETH/SOL strikes are round (`$5,000.00`, `$200.00`); treasury ladders step by
  one basis point and state the effective threshold in `yes_sub_title`
  (`4.92% or above`, `4.75% or below`).

**Inferred** (stated so they can be challenged):

* Kalshi `above F` resolves strictly (`> F`) and `below C` strictly
  (`< C`); we therefore compare Polymarket thresholds against the *sub-title*
  number (`5.06% or above`) for treasuries, and mark the one-cent
  strict/inclusive boundary as confidence 0.80 (round crypto strikes) rather
  than 0.90 (BTC `.99` strikes, where the thresholds coincide exactly).
* `by Jan 1, 2027 12:00am` and `by Dec 31, 2026 11:59pm` are the same tape
  boundary; windows are canonicalised to 2026-12-31.
* Polymarket "hit/reach X" means running max ≥ X and "dip to X" means running
  min ≤ X; Kalshi `KX*MAXY`/`MINY` are running max/min ladders.
* Poll-average source for Polymarket approval markets is unknown; Kalshi
  states RealClearPolitics. This is why approval pairs are refused, not
  emitted.
* Election-year equivalence within ±1 year follows the legacy extractor.

## 7. Artifact schema and loader compatibility

`streams/kalshi.py` reads the mapping through
`polymarketbot.data.kalshi_mapping.load_mapping`, which touches **only
`kalshi_ticker`** (sorted, de-duplicated). `read_mapping` returns row dicts.
`scripts/report_kalshi_basis.py` additionally selects
`polymarket_condition_id`, `kalshi_ticker`, `confidence`, `method`.

v2 keeps all of those names, plus the new fields:

```
kalshi_ticker, kalshi_event, polymarket_condition_id,
polymarket_yes_token, polymarket_no_token, direction, confidence,
method, match_reason, kalshi_title, kalshi_yes_sub_title,
series_ticker, window, polymarket_question, kalshi_event_ticker
```

* `polymarket_yes_token` / `polymarket_no_token` are `clob_tokens[0]` and
  `clob_tokens[1]` from the manifest.
* `direction = normal` means Kalshi YES == Polymarket YES token;
  `inverted` would mean Kalshi YES == Polymarket NO token. No inverted row
  exists today, so no consumer needs the flip yet, but the column is present
  for when one appears.
* `kalshi_event_ticker` is an alias of `kalshi_event` for the v1 column order.
* **Loader update note:** no code change is required to consume v2. To use it,
  point `kalshi.mapping_path` in `configs/data_collection.yaml` at
  `data/derived/kalshi_mapping_v2.parquet` (or pass the path to
  `report_kalshi_basis.py --mapping`). Until then the collector keeps polling
  the v1 tickers, which is deliberate: v1 still has 55 live rows that v2 does
  not cover (mostly non-macro elections), and v2 adds 84 rows v1 lacked.
  A merged view is the obvious next step; it is not written here because the
  ownership of the config is outside this change.

## 8. Reproduce

```bash
# audit numbers, v2 artifact (caches reused; ~20 s, 2 requests)
.venv/Scripts/python.exe -m polymarketbot.data.kalshi_mapping --artifact v2 --extra-series 0

# cold rebuild: re-crawl open events, re-fetch macro/crypto series, ~180 requests
.venv/Scripts/python.exe -m polymarketbot.data.kalshi_mapping --artifact v2 --refresh --refresh-markets

# v1 (legacy behaviour) is unchanged
.venv/Scripts/python.exe -m polymarketbot.data.kalshi_mapping
```

The 25-pair sample is reproducible with `random.seed(20261106)` over the v1
parquet; the fetched evidence for each pair is stored in
`data/tmp/kalshi_audit_sample.json`.

## 9. Limits

* v1 precision is a 25-pair estimate; the method breakdown suggests the true
  figure is lower than 0.64 if the 78 threshold rows all fail like the 7
  sampled ones. v2 removes that failure mode by construction but has not been
  audited with a fresh 25-pair sample yet.
* v2 covers 4.3% of the current universe. The remaining 2,357 markets are
  mostly sports, "what will Trump say", and non-US races without a Kalshi
  counterpart in this cut.
* Crypto/treasury ladders are high-confidence but their resolution sources
  (Kalshi's index vs Polymarket's) are not compared here; that belongs to the
  basis report.
* The macro crawl depends on event titles carrying a year or month. Events
  titled "CPI in October" without a year are skipped until a later crawl sees
  a dated sibling.
