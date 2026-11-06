# Does UMA oracle state predict a losing-token discount?

Test of the proposal `cross-uma-liveness-convergence`: *when a market's resolution is
proposed, the losing token keeps trading above 0 during the liveness window, so
`YES + NO < 1` and the discount decays toward 1 as liveness expires.*

**Verdict: untestable on this tape, and mechanically refuted where it can be measured.**
Only **3 of 234** proposals land on a condition the book feed ever quoted, and **none
of the 3 has a single quote inside its liveness window**. Where a two-sided book does
exist the discount is never there: 7 conditions on the whole tape (307 observations) hold
`YES + NO` mid at **1.0000** (std 0.00045), and the wider sample of **92 quoted conditions
measured into their own resolution** (276 observations) never once offers a complete set
for less than $1.00 — 0 of 276, median pair premium 1.0 c. There is no discount state for
the oracle to predict.

---

## 1. Recon — what the dataset actually contains

`data/tape/uma_events/date=*/hour=*/*.parquet`, 18 hourly partitions,
**272 rows, 2026-11-05 05:51:04Z → 2026-11-06 04:10:31Z (22.3 h)**.

| field | value |
|---|---|
| event types | `ProposePrice` 234, `RequestPrice` 38 — **no `Settle`, no `DisputePrice`** |
| distinct questions | 272 / 272 (one event per market, no repeats, no re-proposals) |
| `condition_id` populated | **10 of 272** (all on `RequestPrice`; **null on all 234 `ProposePrice`**) |
| `market_slug` populated | 10 of 272 |
| `disputed` | `false` on all 234 `ProposePrice` (this is the state *at proposal time*) |
| `payouts` / `settlement_price` / `resolution_time` | **100 % null** |
| `proposed_price` | `0` (196) or `1e18` (38) — i.e. 84 % of proposals assert NO |
| `early_resolution` | 100 % null |
| address / contract | `0xee3afe…7c24` (UMA OptimisticOracleV2) / `optimistic_oracle_v2` |
| proposal cadence | 17 active hours, median 3 proposals/h; **111 proposals in the single 17:00Z hour** (midterm House seats), 50 at 19:00Z, 16 at 01:00Z |

Proposals arrive in **clusters inside neg-risk events** (the six Selic-rate tokens, the
30-candidate 2028 presidency event, one proposal per House seat pair), so the 234 rows
are far fewer than 234 independent bets — the NO-heavy 84 % split is that clustering,
not a directional signal.

### Mapping to tape conditions

`ProposePrice` carries no `condition_id`, so the only join is on the question text:

* source A: `data/raw/gamma/snapshot_2026-11-01.parquet` (39,957 markets, has
  `conditionId`, `question`, `clobTokenIds`, `feesEnabled`, `feeType`);
* source B: 15 `data/tape/universe/*.json` snapshots (5,809 conditions).

| step | count |
|---|---|
| events | 272 |
| mapped to a condition by exact question | **84 (31 %)** — 55 `ProposePrice`, 29 `RequestPrice` |
| unmappable (sports, short-dated crypto, non-snapshot markets) | 188 |

Attempted and failed: deriving `conditionId` from the log itself. UMA's `questionID`
is **not** `keccak256(ancillary_data)` — computed for all 84 mapped events, 0 match the
`questionId` topic of the CTF's `ConditionResolution` log, so
`keccak256(abi.encodePacked(oracle, questionID, 2))` cannot be rebuilt either (0/55
verified against known condition ids, across every oracle address that appears in
`polygon_logs`). The text join is the ceiling. That ceiling is not the binding
constraint — see §3.

---

## 2. What the tape has on those markets

`clob_events` covers the same 34 h. Per hour it holds 4 k–37 k `book` events and
0–1 M `best_bid_ask` events (`best_bid_ask` is missing entirely for 10:00–18:59Z on
Nov 5 — the collector reduced its subscription to 614–1042 conditions during that window).

Of the 84 mapped conditions, quotes exist for:

| coverage | count |
|---|---|
| any `book`/`best_bid_ask` row anywhere in the tape | **9** |
| both tokens quoted (complete set observable) | **7** |
| carrying a `ProposePrice` | **3** |
| with ≥ 1 quote inside `[proposal, proposal + 3.1 h]` | **0** |

Quote density is the reason. The 7 two-sided conditions supply **307 usable
observations over 14–29 h**, i.e. 0.2–8 per condition-hour — the same "almost no flow" that the
replay work already found for reward markets.

The 3 proposal conditions, in full (every quote the tape ever recorded for them):

| market | proposal (UTC) | proposed | quotes exist | last quote |
|---|---|---|---|---|
| R wins NY-01 | 17:01:51 | YES | 25 | 02:34:53 (**14.4 h earlier**) |
| D wins NY-01 | 17:04:39 | NO | 2 | 02:34:47 (**14.5 h earlier**) |
| R wins FL-13 | 19:40:44 | YES | 10 | 03:37:47 (**16.0 h earlier**) |

All three books stop updating 14–16 h **before** the proposal. No quote — one-sided or
two-sided — exists anywhere in the 3.1 h liveness window, so the discount path the idea
claims to trade is simply not in the data.

### Why: the feed and the resolution set are disjoint

`polygon_logs` records the CTF's `ConditionResolution(bytes32,address,bytes32,uint256,uint256[])`
(topic0 `0xb44d84d3…5894`, 44,308 events in the 34 h, 43,460 distinct conditions). Joining
that to the set of conditions the book feed ever watched (4,832 conditions):

> **561 / 43,460 = 1.27 %** of the conditions that resolved inside the tape window were
> ever quoted by the book feed. Per hour it ranges 0.1 % – 6.6 %.

At that rate 234 proposals should yield ≈ 3 measurable conditions; 3 were measured. The
gap is structural, not a mapping artefact: markets reach a proposal because they have
stopped trading, and the collector's subscription is liquidity-ranked, so the resolving
markets are exactly the ones with no quotes left to record.

---

## 3. The discount, where it can be measured

Method: rebuild each token's best quotes from `book` (touch from the level arrays) and
`best_bid_ask`, keep only state changes, align with as-of lookup
(`searchsorted(side='right')`, max staleness 600 s) at a 60 s grid, and form the
label-free pair quantities `S_mid = mid_A + mid_B`, `S_ask = ask_A + ask_B`,
`S_bid = bid_A + bid_B`. A complete set is one of each token, so `1 − S_ask` is the
riskless profit from buying both and merging, and `S_bid − 1` the same from selling both.

**7 conditions, 307 observations, whole tape:**

| quantity | mean | median | p5 | p95 | min | max |
|---|---|---|---|---|---|---|
| `S_mid` | **1.0000** | 1.0000 | 1.0000 | 1.0000 | 0.9945 | 1.0055 |
| `S_ask` | 1.0026 | 1.0020 | 1.0010 | 1.0080 | **1.0010** | 1.0170 |
| `S_bid` | 0.9974 | 0.9980 | 0.9913 | 0.9990 | 0.9830 | 1.0020 |

* buyable complete-set discount `1 − S_ask`: mean **−0.26 c**, best ever **−0.10 c**.
  **0 of 307 observations** are below 1 by even 0.1 c; `0 %` exceed 0.5 c, 1 c or 2 c.
* sellable premium `S_bid − 1`: **0 of 307** exceed 0.5 c.
* round-trip cost of assembling and unwinding a set: mean **0.52 c**, median 0.40 c, p95 1.60 c.

Base rate for the claim: the discount is present **0 %** of the time, at a sample where a
discount of ≥ 1 c would have been visible in every one of the 307 observations. `S_mid`
sits at exactly 1 because Polymarket mirrors each binary book into both tokens (a bid on
YES *is* an ask on NO), so `YES + NO ≡ 1` by construction and the residual is the spread,
which is always paid, never received. 6 of the 7 carry `feesEnabled=true`,
`feeType=politics_fees` (the Iranian-blockade market has fees disabled), so the venue's
`fee_rate · (p(1−p))^exp` per share is an additional cost on top of a premium that is
already non-negative.

The three proposal markets, at their last quote before the proposal, show the same
picture and also why a "losing token" cannot be bought:

| market | token A (bid/ask) | token B (bid/ask) | `S` | note |
|---|---|---|---|---|
| R wins NY-01 | 0.014 / 0.049 | 0.951 / 0.987 | mid 1.000, ask 1.036 | 3.5 c premium per leg |
| D wins NY-01 | 0.012 / 0.034 | 0.966 / 0.988 | mid 1.000, ask 1.022 | 2.2 c premium |
| R wins FL-13 | 0.995 / — | — / 0.005 | **unbuyable** | one-sided: bid on R, ask on D only |

At the FL-13 quote there is no ask on the winning token and no bid on the losing token,
so neither leg of a "buy the winner at 0.995" nor a "sell the loser at 0.005" trade could
be executed at that touch. That is the general shape of these end-of-life books: not a
discount, but a one-sided, 0.5–4 c wide quote on a market with no flow.

### 3b. The larger sample: every quoted condition that actually resolved (Nov 5)

Because a proposal is only observable on 3 quoted conditions, the same claim was measured
on its **conclusion** instead of its trigger: for every condition that both resolved
inside the tape and was ever quoted, take both tokens' last quote before its
`ConditionResolution` log and at −60, −30, −15, −5, −1 min and 0. One day's slice
(Nov 5, the extraction for Nov 6 was not run), **92 conditions, 276 observations**:

| pair spread `S_ask − S_bid` | n | mean `S_mid` | mean \|`S_mid`−1\| | max \|`S_mid`−1\| | mean `S_ask` | min 1−`S_ask` |
|---|---|---|---|---|---|---|
| ≤ 2 c | 127 | 1.0011 | 0.0011 | 0.0120 | 1.0059 | −1.9 c |
| 2–5 c | 64 | 1.0014 | 0.0014 | 0.0200 | 1.0163 | −4.0 c |
| 5–20 c | 34 | 1.0033 | 0.0033 | 0.0225 | 1.0434 | −6.6 c |
| 20–100 c | 26 | 0.9694 | 0.1363 | 0.4695 | 1.3225 | −93.9 c |
| > 100 c | 25 | 1.0008 | 0.0008 | 0.0200 | 1.7312 | −93.9 c |

* **`1 − S_ask > 0` in 0 of 276 observations.** Not one condition, at any distance from
  resolution, could be bought as a complete set for less than $1.00. Median pair premium
  1.0 c; on the 127 tight books it is 0.6 c.
* The mid-sum "discounts" that do exist are spread artefacts: 19 of 276 observations
  deviate > 1 c, and every one of them has a pair spread ≥ 1.4 c (median 6.1 c). On
  books tight enough to trade — pair spread ≤ 1 c, n = 69 — the largest mid-sum deviation
  ever seen is **0.45 c**.
* Median quote staleness going into resolution is **21 min** (p90 55 min), so even these
  276 observations are as-of lookups on books that had already stopped trading.


---

## 4. Can a discount be bought and held to resolution?

Partly measurable, and the answer is no on both legs:

* **Resolution outcomes do exist on the tape**, but not in `uma_events`
  (`payouts`/`settlement_price` 100 % null, no `Settle`/`DisputePrice` decoded). They are
  the CTF's `ConditionResolution` logs in `polygon_logs`: 44,308 events, payouts decodable
  from the data word (`(0,1)` 32,596, `(1,0)` 11,415, `(1,1)` 297 — the last being 50/50
  resolutions where each token pays 0.50, so a set still pays exactly $1.00).
* **Proposals match the final payout 55 / 55** for the mapped conditions that resolved
  inside the tape. Median proposal → `ConditionResolution` lag is **3.10 h** (min 3.02,
  max 3.24), consistent with a 2 h liveness plus settle time. Zero dispute reversals were
  observed — with 234 proposals and no `DisputePrice` decoded, this sample cannot bound
  the reversal tail; the on-chain dispute count for this population is unknown, not zero.
* **But there is nothing to hold.** A hold-to-resolution trade needs an ask on the
  proposed side during liveness. For 0 of 55 mapped proposals does the tape contain any
  quote in the window, and in the 7 markets where a set is priceable it costs ≥ 1.001
  and pays exactly 1.000 → expected −0.1 c to −1.7 c per set before fees.

---

## 5. Verdict

1. **The channel is untestable with this sample.** The acceptance gate is 20 conditions
   reaching a proposal *and* being observable. Measured: **234 proposals, 55 mapped to a
   condition (179 unmappable), 9 of the 84 mapped conditions ever quoted, 3 with a proposal
   and a quote, 0 with a quote inside the liveness window.** Nothing about the discount
   path — its level, its decay, its relation to remaining liveness — can be estimated.
2. **Where measurement is possible, the premise is false.** `YES + NO` mid = 1.0000
   (std 0.00045, n=307, 7 conditions); the buyable set is always ≥ 1.001 and never below
   1. Taking the conclusion instead of the trigger — 92 quoted conditions measured into
   their own resolution, 276 observations, Nov 5 — the executable discount is below zero
   in **every** observation (`1 − S_ask > 0` in 0 of 276; median premium 1.0 c), and the
   only large mid-sum deviations (> 1 c, 19 of 276) all sit on pair spreads ≥ 1.4 c, where
   nothing is executable. The "losing-token discount" is not a state that waxes and wanes
   with oracle liveness; on this venue it is a mirrored book and a spread.
3. **The mechanism explains the emptiness**: only 1.27 % of the 43,460 conditions that
   resolved inside the tape were ever quoted, and the leg that would carry the discount
   is one-sided or absent exactly when a proposal arrives.

### What would make this testable

* Subscribe the book feed to the **resolution pipeline**, not the liquidity ranking:
  poll/subscribe any condition whose market end-date is within 24 h, or better, mirror
  the addresses in `polygon_logs` (the CTF + the adapter) back into a quote subscription.
  At the observed 1.27 % overlap, no amount of extra tape length fixes this — 234
  proposals yielded 3 observable conditions, so ~1,500 proposals are needed per 20
  observable ones unless the subscription changes.
* Decode the OO address `0xee3afe…7c24` properly: `polygon_logs` holds its logs (277 +
  272 + 38 over the tape) but the standard `Settle` and `DisputePrice` topic0s occur
  **zero** times in the whole 34 h, so no settlement or dispute is captured by any
  collector path — the dispute-reversal rate is unmeasurable rather than zero.
* Record the **ask side with depth** for resolved markets; the touch alone cannot
  distinguish "1 c premium" from "no ask at all", which is what half of these books are.

### Reproduce

```
.venv/Scripts/python.exe data/derived/uma/extract_pc.py 2026-11-05
.venv/Scripts/python.exe data/derived/uma/extract_pc.py 2026-11-06
.venv/Scripts/python.exe data/derived/uma/cov.py date=2026-11-05
.venv/Scripts/python.exe data/derived/uma/cov.py date=2026-11-06
.venv/Scripts/python.exe data/derived/uma/final.py
.venv/Scripts/python.exe data/derived/uma/res_quotes.py 2026-11-05
.venv/Scripts/python.exe data/derived/uma/res_pair.py
```

`res_quotes.py 2026-11-05` is the one query that broke the 60 s rule — 177 s, because it
scans every `book`/`best_bid_ask` row of 29 hourly partitions to filter the 561
resolved-and-watched conditions (474 of them came back with quotes).
The Nov 6 pass was not run; §3b is one day's slice.

Artifacts: `data/derived/uma/uma_raw.parquet` (272 events),
`uma_mapped_union.parquet` (84 mapped), `res_nov5/6.parquet` (44,308 resolutions),
`watched_*.parquet` (per-hour watched conditions), `basket_all.parquet` (307 pair
observations), `res_watch_by_hour.parquet`, `prop_vs_resolution.parquet` (55/55),
`rq_2026-11-05.parquet` + `res_pair_path.parquet` (92 conditions, 276 observations).

Limits, stated: 179 of the 234 proposals cannot be mapped at all (no `condition_id` in
the log, question text only, no network to Gamma); the 3.1 h proposal→resolution lag is
measured on the 55 that resolved, not from a liveness field; and every number here is
one 34 h window with one collector configuration.
