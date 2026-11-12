# On-chain CTF flow and gas — what the tape measures

Datasets: `data/tape/polygon_logs` (raw CTF logs) and `data/tape/polygon_gas`
(block headers). Everything below is from those two plus `clob_events` for the
Polymarket top of book. Scripts: `data/derived/onchain/{decode_onchain,
mid_cache,analyze,stats,coverage,gas_cost}.py`; derived tables in
`data/derived/onchain/`.

## Verdict up front

| question | answer |
|---|---|
| Does mint/merge flow **predict** a PM mid move? | **No.** After controlling for the recent trend, the coefficient is +0.0000 with t = −0.00. Hit rate on direction is the base rate. |
| Is a burst of on-chain activity informative? | It marks a market **about to move**, not a **direction**: move rate 3.2× the base rate within 5 s, up-share 51.0 % vs base 49.6 % (n = 6,477). Activity is symmetric — that is a cost, not an edge. |
| Is any of it tradeable? | **No.** The only surviving signed effect (a merge burst, +3.1 bps at 5 s, t = 2.50) is 1/25 of one tick, and the log reaches this collector a median **13.5 s** after its block. |
| Does gas data change the merge model? | **Yes, by roughly 10×.** Measured cost is **\$0.00155 per merge** (median over the tape), not the \$0.015 the replay charges. The break-even threshold is **~1 complete set**, not 100. |

---

## 1. Recon

**Span.** Blocks 96,698,107 – 96,834,634 = 2026-11-04T00:37Z – 2026-11-06T09:55Z,
580 parquet parts, 11.3 M rows, 295 MB. The 2026-11-06 partition stops at 09:55Z.

**Contracts.** 99.995 % of rows are the CTF,
`0x4d97dcd97ec945f40cf65f87097ace5ea0476045`. A second address,
`0xee3afe347d5c74317041e2618c49534daf887c24`, contributes 587 rows over three
days with three signatures I did not identify; immaterial (0.005 %).

**Signatures present** (keccak of the ABI string, all verified against decoded
payloads):

| topic0 | event | meaning for this question |
|---|---|---|
| `0x2e6bb91f…86298` | `PositionSplit(stakeholder, collateral, parentCollection, conditionId, partition[], amount)` | mint: $1 → 1 YES + 1 NO |
| `0x6f13ca62…282ca` | `PositionsMerge(…)` | burn: 1 YES + 1 NO → $1 |
| `0x2682012a…d18d` | `PayoutRedemption(…)` | post-resolution, not tradeable |
| `0xc3d58168…0f62` | ERC-1155 `TransferSingle` | wallet→wallet moves |
| `0x4a39dc06…8f7fb` | ERC-1155 `TransferBatch` | settlement batches |
| `0xab3760c3…e4177` / `0xb44d84d3…a5984` | `ConditionPreparation` / `ConditionResolution` | market lifecycle |
| `0x17307eab…96c31` | `ApprovalForAll` | no information |

`topic3` is the `conditionId` on split / merge / redeem, so the join to
`clob_events.condition_id` is direct. `amount` is the third word of `data`
(USDC, 6 dp); the second word is the offset to the partition array, which is
always `[1, 2]` — binary markets, no other partition appeared.

**Event counts, CTF only, by day**

| event | 2026-11-04 | 2026-11-05 | 2026-11-06 (to 09:55Z) |
|---|---:|---:|---:|
| TransferSingle | 12,470 | 3,450,544 | 1,298,451 |
| TransferBatch | 23,140 | 2,936,598 | 1,129,242 |
| PositionSplit | 23 | 992,780 | 370,507 |
| PositionsMerge | **0** | 183,303 | 64,451 |
| PayoutRedemption | 266 | 455,097 | 213,431 |
| ApprovalForAll | 1,039 | 126,915 | 50,465 |
| ConditionPreparation | 90 | 30,497 | 7,027 |
| ConditionResolution | 74 | 29,045 | 15,263 |

**2026-11-04 is not usable for rates**: 37,145 rows over 37,145 distinct blocks
— exactly one log per block. That is a sampling artefact of the collector, not
the chain. Use 11-05 and 11-06 only; do not compute per-hour rates by dividing
the 11-04 row count by its span.

**Tape-wide notional (all three days)**

| kind | events | notional | median | p99 |
|---|---:|---:|---:|---:|
| split | 1,366,327 | \$183.3 M | \$13.16 | \$1,815 |
| merge | 248,319 | \$43.9 M | \$17.94 | \$1,558 |
| redeem | 669,829 | \$113.9 M | \$5.94 | \$2,226 |

### Coverage of tradable conditions

Measured on 2026-11-06T06Z, one hour:

- **2,152** conditions in the current universe snapshot
  (`universe/20261106T092900.json`).
- **515 (23.9 %)** of them emitted a mint or merge in that one hour — 418 with a
  split, 369 with a merge.
- Merge notional on those universe conditions: **\$736 k** of **\$1.14 M** total
  (65 %). Splits: \$1.06 M of \$3.90 M.
- Network-wide, 4,699 distinct conditions had flow in that hour. Only 710 map to
  the universe file and only **513** have a Polymarket top-of-book series in the
  same hour — **the on-chain firehose is mostly markets we cannot trade.**

So: coverage is real but partial. About a quarter of the tradable universe
produces on-chain mint/burn flow per hour, and the flow that lands on those
markets is 65 % of all merge volume.

Size distribution on the analysed markets: median merge \$19.12, p90 \$190;
999 merges ≥ \$100 and 84 ≥ \$1,000 in the hour — i.e. the bulk is retail-sized,
and the tail is what a maker would care about.

---

## 2. Does the flow lead the Polymarket mid?

### Method

1-second grid per condition, **as-of alignment** (`searchsorted(..., side='right')`,
last observation held — never `np.interp`). The mid is the last
`(best_bid+best_ask)/2` at or before each second, built from the nested
`price_changes` of `clob_events` (the `best_bid_ask` stream covers only a small
part of the universe).

Flow is aggregated into **30 s buckets** per condition; the forward change is
measured from the bucket end. The comparison base rate is computed on **the same
conditions**, sampled every 30th grid point, so "active market" and "this
condition" are held fixed and only the flow varies.

Slice: 2026-11-06 06:00–08:59Z (three hours).

- **n = 6,477** flow buckets over **874 conditions**
- base sample **n = 193,475** grid points on the same conditions
- events behind the buckets: **11,074 logs** — 6,852 splits, 4,222 merges (the other
  174,616 on-chain events in those hours belong to conditions with no PM top of book)

### Result

| bucket class | n | move% 5 s | move% 30 s | move% 60 s | mean Δ 5 s (bps) | t | mean Δ 30 s (bps) | t | mean Δ 60 s (bps) | t |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **base rate** | 193,475 | 5.2 | 12.7 | 16.9 | −0.02 | −0.19 | +0.11 | 0.53 | +0.22 | 0.82 |
| any mint/merge | 6,477 | **16.7** | **34.0** | **41.5** | +0.71 | 0.83 | +2.24 | 1.10 | +2.62 | 0.96 |
| split > 0 | 4,219 | 18.4 | 37.1 | 44.3 | −0.37 | −0.36 | +1.54 | 0.61 | +2.82 | 0.77 |
| merge > 0 | 3,174 | 17.7 | 35.7 | 43.4 | **+3.10** | **2.50** | **+6.19** | **2.06** | +4.84 | 1.26 |
| split ≥ p90 | 648 | 19.3 | 35.7 | 43.4 | +1.03 | 0.55 | +2.06 | 0.46 | +1.98 | 0.33 |
| merge ≥ p90 | 320 | 10.3 | 23.1 | 28.8 | 0.00 | 0.00 | −0.72 | −0.18 | −7.05 | −1.16 |
| net mint ≥ p90 | 648 | 18.8 | 35.0 | 42.4 | +0.97 | 0.53 | −0.28 | −0.06 | +1.12 | 0.19 |

Up-share **among movers** (the direction question, isolated from the
move/no-move question):

| bucket class | 5 s | 30 s | 60 s |
|---|---:|---:|---:|
| base rate | 49.6 % | 48.5 % | 49.0 % |
| any mint/merge | 51.0 % | 50.1 % | 49.7 % |
| split > 0 | 51.1 % | 50.1 % | 50.2 % |
| merge > 0 | 52.2 % | 50.8 % | 50.2 % |
| merge ≥ p90 | 51.5 % | 44.6 % | 40.2 % |

Mean absolute move shows the activity effect plainly — 13.4 / 43.7 / 65.0 bps
for flow buckets against 3.7 / 13.4 / 21.1 bps for the base — but it is
**symmetric**: the 5 s up-share is 51.0 % where the base is 49.6 %, a 1.4 pp
difference with a standard error of ~1.5 pp on 1,082 movers.

**Continuous test.** Spearman(net mint USD over the bucket, forward change):

| horizon | ρ | p | n |
|---|---:|---:|---:|
| past 30 s | +0.0115 | 0.35 | 6,477 |
| forward 5 s | −0.0043 | 0.73 | 6,477 |
| forward 30 s | −0.0012 | 0.92 | 6,477 |
| forward 60 s | +0.0094 | 0.45 | 6,477 |

**With the recent trend controlled.** OLS, `Δ30 bps ~ const + past30 bps + net
mint USD`, n = 6,477:

| term | coef | se | t |
|---|---:|---:|---:|
| const | +2.240 | 2.028 | +1.10 |
| past30 bps | −0.0155 | 0.0082 | −1.89 |
| **net mint USD** | **−0.0000** | 0.0001 | **−0.00** |

The flow adds exactly nothing over the recent trend. A dollar of net minting
moves the forward 60 s mid by less than 1/1,000 of a basis point.

### The one effect that survives significance

`merge > 0` gives **+3.10 bps at 5 s (t = 2.50)** and **+6.19 bps at 30 s
(t = 2.06)** over the base rate. The mechanism is plausible — a merge is an
arbitrageur extracting \$1 from a pair quoted below par, so the pressure on the
pair ends when the merge lands — but the size is decisive:

- 3.1 bps = **0.031 c** of probability. One tick at p = 0.5 is 1 c ≈ 200 bps.
- The quote offset the production baseline uses is 0.75 c.
- Fading or joining on it costs a spread 25× wider than the effect.

I do not believe it is real either: the up-share test on the same bucket
(52.2 % vs 49.6 %) has z ≈ 1.2, and the mean is carried by magnitude, not
direction. It is recorded so nobody re-derives it.

### Is any of it tradeable? Two separate reasons no

1. **Transport.** The block timestamp of the merge block versus the timestamp at
   which this collector *received* the log: **median 13.5 s, p90 25.8 s**
   (n = 2,108 merge logs, 06Z). For calibration the CLOB feed in the same hour
   shows `recv_ts_ns − venue_ts_ms` = **−1.57 s**, i.e. the host clock runs about
   1.6 s behind the venue clock, so the on-chain transport delay is at least
   15 s. A 5 s horizon has already elapsed before the event exists in our
   process. This is a plumbing property, not a market one — a direct
   `eth_subscribe` would cost ~1–2 s — but it is the property of *this tape*.
2. **The book is already moving.** The Polymarket top of book for these
   conditions updates every ~1 s: 99.9 % of on-chain mint/merge events have a
   price update within ±2 s. There is no lag structure to read off — the mint
   and the quote change are the same event at this resolution, and the base-rate
   table above is the only way to separate them.

The block interval measured from `polygon_gas` is 1 s median, 2 s p90, so the
chain itself offers no window to act in either.

---

## 3. Gas: the measured cost of a merge

`polygon_gas` gives block headers: `base_fee_gwei`, `gas_used`, `gas_limit`,
`tx_count`, `timestamp_ms`. It does **not** give per-transaction receipts, so
the gas *units* a merge consumes cannot be read from this tape — among the
106,885 blocks that carry a `tx_count` (95,160 rows have it NULL, because
`newHeads` omits the transaction list) the sparsest contains 24 transactions
(block 96,703,048) and none contains a single one. Gas units therefore stay at
the CTF profile
constant the repo already uses, **78,000** (`queue_valuation.CTFGasProfile`,
`estimate_merge_cost_usd`). What the tape measures is the **price**, and that is
the part the model got wrong.

**Base fee, measured**

| window | blocks | p10 | median | p90 |
|---|---:|---:|---:|---:|
| whole tape | 106,885 | — | **242.7 gwei** | 250.9 |
| blocks in the gas tape containing ≥1 merge | 62,874 | 71.4 | **216.4 gwei** | 250.4 |
| 2026-11-04 | 35,700 | 227.6 | 247.9 | 251.7 |
| 2026-11-05 | 48,458 | 77.9 | 243.1 | 251.0 |
| 2026-11-06 (to 09:55Z) | 23,128 | 62.2 | 151.2 | 242.2 |
| 06Z merge blocks only | 2,108 | 69.7 | **142.4 gwei** | 154.9 |

Blocks are not congested — `gas_limit` is 160 M and utilisation runs 15–20 %
(mean 70–73 txs per block) — the fee level is simply the chain's regime for
these days.

**Cost per merge in USD**

    $/merge = 78,000 gas x base_fee x 1e-9 x POL/USD

POL/USD is taken at **\$0.092**, the repo's own fallback in
`trading/ctf.py::get_dynamic_pol_usd_price` (there is no POL price series on the
tape; the number is linear in the price, so any other assumption scales it).

| basis | base fee | \$/merge |
|---|---:|---:|
| p10 of merge blocks | 71.4 gwei | \$0.00051 |
| **median of merge blocks (tape)** | 216.4 gwei | **\$0.00155** |
| 06Z merge blocks | 142.4 gwei | \$0.00102 |
| p90 of merge blocks | 250.4 gwei | \$0.00180 |

Adding a priority tip (not in the tape; Polygon's floor is ~25–30 gwei) scales
these by ~1.14: **\$0.0018** at the median.

**Against the model in the repo**

| source | value | vs measured |
|---|---:|---|
| `engine.merge_gas_fee_usd`, used by every replay | \$0.015 | **9.7× too high** |
| `queue_valuation.PolygonPoSEnvironment` assumed fee | 35 + 45 = 80 gwei | 2.7× below the measured 216 |
| `estimate_merge_cost_usd` at its own defaults (80 gwei, \$0.092) | \$0.00057 | consistent with the tape |
| the fee that would make \$0.015 true at \$0.092/POL | 2,090 gwei | 8.3× the measured p90 |

\$0.015 cannot be reconciled with the tape at any observed fee level. Total gas
spent on all 248,319 merges in the tape is **≈ \$385**, against \$43.9 M of
merged notional — **0.88 bps**.

**Resolved (2026-11-10).** The constants this section indicted are gone, and the
tape median above is confirmed by a live measurement. `gas.py` now measures and
refuses; there is no default to fall through to.

| Fabrication (before) | Measured now | Error that was live |
|---|---:|---:|
| EIP-1559 tip `30.0` Gwei | `maxPriorityFeePerGas` = **25.0** Gwei | +20 % |
| all-in fallback `40.0` Gwei | **236.58** Gwei (median of 4 RPC samples, spread 0.28 %) | **−83 %** |
| `POL/USD` `0.092` | **0.087172** (median of 4 venues, spread 0.138 %) | +5.8 % |
| `PolygonPoSEnvironment(35, 45, 0.35)` | same measured values, no defaults | 0.35 was **4×** the real POL price |
| `switching.py` `$0.015` merge gas | measured, or `+inf` (never switch on a guess) | 9.3× |

The all-in fee at 236.58 Gwei is the number that matters, and at 78,000 gas it
makes the merge cost **\$0.001609** against the \$0.000287 the old constants
produced — the fabricated fee understated merge gas by **5.6×**, so the merge
threshold sat 5.6× too low and sub-one-set merges that were gas-negative passed
the hurdle. The oracle's `fast.maxFee` (323.09 Gwei) is a *cap recommendation*
with 36 % headroom; it is deliberately not averaged into the price median, only
used when no RPC answers and logged as a drift check.

**Does the merge threshold move? Yes, by ~100×.**

`calculate_optimal_merge_threshold` is `gas_cost / 0.0015` (a 15 bps friction
hurdle on the \$1 per complete set that a merge frees). Fed the measured cost:

    threshold = 0.00155 / 0.0015 = 1.03  ->  1 complete set

The replay runs `auto_merge_threshold = 100.0`, and the comment at
`scripts/replay_all_markets.py:385` (the harness was deleted in the 2026-11-10
scripts cleanup; this is quoted from its last revision) already admits why that
is wrong: *"the
default (100) is above the quote size on most markets, so the channel never
fires and the gas model is untested."* With pool-scaled cash of \$10–25 per market
(pool-scaled), 100 complete sets = \$100 of dead inventory that the account can
never even reach: **the merge channel is dead, not expensive.**

Per-share gas drag if the channel is switched on:

| merge size | gas/share | as % of a 0.75 c quote offset |
|---:|---:|---:|
| 1 set | 0.155 c | 21 % |
| 10 sets | 0.0155 c | 2.1 % |
| 100 sets | 0.0016 c | 0.2 % |

Recommendation: `merge_gas_fee_usd = 0.0016` and
`auto_merge_threshold = 10` (gas ≤ 2 % of the offset, capital recycled 10×
sooner than 100). Below 10 the gas starts to matter relative to the spread; above
40 it is noise either way.

**What this does and does not change.** The correction is worth \$0.0134 per
merge. On a 500-market slice that merges once per market it is ~\$6.7 of phantom
cost against reported nets in the tens of dollars — material at that scale, and
currently *unexercised* because the threshold never fires. It cannot change any
of the seventeen negative results already recorded, because none of them merged.

---

## 4. Limits of this measurement

- **Two days of rate data**, one day (11-04) discarded as sampled, and the
  lead/lag test runs on three hours of 11-06. That is 6,477 buckets over 874
  conditions — enough to call a null on a 3 bps effect, not enough to rule out a
  conditional effect (e.g. flow in the top decile of notional *and* a wide
  spread) that this cut does not isolate.
- **Gas units per merge are a constant, not a measurement.** Per-transaction
  receipts are not in the tape. If someone adds `eth_getTransactionReceipt`
  capture, the 78,000 should be replaced with the observed distribution (the
  profile's own spread is 68 k–142 k), which would move \$/merge by −13 % to
  +82 % — the threshold would move from 1 to between 0.9 and 1.9 sets, so the
  conclusion is insensitive to it.
- **POL/USD is not on the tape.** Every dollar figure above scales linearly with
  it; the ratio to the \$0.015 model constant does not.
- Merge flow is not the only on-chain information: the `TransferBatch` stream
  (2.9 M rows on 11-05, 735 distinct conditions) is the exchange's settlement
  batching and carries wallet-level trade detail. That is the whale-activity
  dataset, not this one; I did not use it here.
