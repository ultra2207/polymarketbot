# Whale activity → does large-print positioning predict a PM move?

Agent: `WhaleSignal`. Data: `data/tape/whale_activity` (3 days), `data/tape/clob_events`,
`data/tape/clob_trades`, `data/derived/replay/*/fills.parquet`.
Code: `data/derived/whale/{prep,coverage,coverage_venue,analyze,stats,stats2,robust,final}.py`.
Outputs: `data/derived/whale/{whale,prints,prints_role,base}.parquet`, `{coverage,coverage_venue,final_table}.csv`.

## Verdict

**No.** The whale feed carries no forward information a maker can use.

* **Timeliness kills it first.** Median delay from the venue timestamp of the trade
  to our copy of the row is **6 h 26 min** over the whole dataset and **63 min** on
  the 9 hours that are measurable against the book. **1.5 %** of prints arrive
  within 60 s. Measured from the arrival clock instead of the venue clock, the
  forward mid move is **−28 / −33 / −36 / −37 bps** at 5 / 30 / 60 / 300 s
  (t = −0.76 / −0.94 / −1.18 / −1.80) — i.e. nothing left to trade.
* **From the venue clock, the only effect that clears the gate is adverse
  selection of the whale, not prediction by the whale.** When a tracked wallet
  is *passively filled*, the mid subsequently moves **against that wallet**: −184 bps
  over 300 s, t = −3.25 (n = 3,200 prints, 198 assets), hit rate 35.8 % vs a base
  rate of 51.2 %. That is the same sign as the −600 bps/fill the replay already
  measures for every maker fill on liquid markets — the dataset identifies *whose*
  fill it was, but not a fill that is worse than the tape would already suggest.
* **Whale prints that are aggressive are not predictive.** +56 / +70 / +99 / +154 bps
  at 5 / 30 / 60 / 300 s (n = 1,098, 177 assets) but t ≤ 1.02 at every horizon, and
  hit rate 50.9–52.6 % against a base rate of 49.5–51.2 %: an edge of +1.4 pp.
  This is the same sweeps/participation family the replay already ruled out.
* **Size is not the informative dimension** — the stronger the size cut, the weaker
  the effect. Big vs small inside the taker role: +126 vs +69 bps at 60 s
  (t 1.55 / 0.65). Inside the maker role big prints are *less* adversely selected
  than small ones (−132 vs −231 bps at 300 s).

## 1. Recon

| field | value |
|---|---|
| files / partitions | 66 parquet, `date=2026-11-04..06`, hourly |
| rows | 165,074, no duplicate rows |
| span (venue clock) | 2026-11-04 00:37:09Z → 2026-11-06 08:21:31Z |
| span (activity timestamps found) | back to **2025-09-16** — the collector backfills history |
| `activity_type` | `TRADE` only (100 %) |
| wallets | 316, all `proxy_wallet`, all with a pseudonym (a fixed watched list) |
| conditions / assets | 8,660 / 10,085 |
| side | 144,371 BUY / 20,703 SELL |
| size | p10 5, p50 111, p90 10,000, p99 93,626 shares |
| notional | p10 \$1.9, p50 \$49.9, p90 \$4,975, p99 \$55,804 |
| \>= \$250 notional | 53,830 rows |
| \>= \$1,000 notional | 34,136 rows |

Schema: `recv_ts_ns, recv_mono_ns, wallet, condition_id, asset_id, side, size, price,
usdc_size, activity_ts_ms, tx_hash, activity_type, raw_json`. It is the Polymarket
data-api *activity* endpoint polled per wallet: `raw_json` carries
`proxy_wallet, timestamp, type, size, usdc_size, transaction_hash, price, token_id,
side, outcome, title` — the same trade the CLOB tape reports, with the wallet added.

### Delay from trade to our copy

| quantile | whole dataset | 9 analysis hours |
|---|---|---|
| p10 | 1,003 s | 275 s |
| p50 | **23,198 s (6 h 26 m)** | **3,800 s (63 m)** |
| p90 | 551,728 s (6.4 d) | 43,727 s |
| ≤ 60 s | 0.9 % (1,540 rows) | 1.5 % (82 prints) |
| ≤ 300 s | 3.9 % | 10.5 % |

The poller stamps every row of a page with one `recv_ts_ns`, so `recv_ts` is the
fetch time, not the message time. There is no streaming user channel in this feed.

### Overlap with the PM tape — the structural constraint

Per-hour, against `clob_events` (book data), keyed on the **venue** hour:

| hour (UTC) | whale rows | book assets | whale assets with book data | whale rows measurable |
|---|---|---|---|---|
| 2026-11-04 00 | 7,127 | 3,745 | 135 | 4,546 |
| 2026-11-04 01 | 4,340 | 3,927 | 124 | 3,246 |
| 2026-11-04 02 | 4,601 | 3,795 | 106 | 2,982 |
| 2026-11-04 06 | 2,225 | 3,900 | 41 | 1,744 |
| 2026-11-04 05 | 2,332 | 4,446 | 62 | 1,568 |
| 2026-11-05 03 | 6,916 | 7,541 | 37 | 554 |
| 2026-11-05 20 | 1,336 | 6,413 | 28 | 274 |

Typical hour: only a few percent of the assets a tracked wallet traded have any book
data in the same hour — 46 of 1,027 traded assets (4.5 %) at 2026-11-05 03, 156 of
1,096 (14 %) at 2026-11-05 20. Over all 35 venue hours with ≥ 800 whale rows the
measurable overlap is **28,176 rows (35 % of the whale rows in those hours)** across
a mean of 48 assets per hour. The book collector subscribes to ~5k assets/hour and
the watched wallets trade in ~1,000/hour, and those two sets barely intersect.

Against `clob_trades` (the print tape), whole dataset:

* 285,634 prints, 285,634 unique tx, 7,048 assets;
* **14.6 %** of whale rows (24,094) have their `tx_hash` on the print tape;
* 1,135 of 10,085 whale assets ever appear on the print tape;
* where both exist, `size` agrees (88 % within 1 %) and **`side` agrees only 46 %**.

That 46 % is the single most important structural fact after the lag: `side` in the
whale feed is **the wallet's own side**, while `side` on the tape is the taker's.
So roughly 60 % of these prints are the tracked wallet acting as *maker*, and a
"whale BUY" is usually a taker selling into the whale's resting bid. Every
measurement below therefore splits prints by the wallet's role, read off our own
book snapshot: price at the far touch = taker, at the near touch = maker.

## 2. Method

1. Nine hours with the largest overlap (2026-11-04 00,01,02,04,05,06,07;
   2026-11-05 03; 2026-11-06 00).
2. Per asset, a mid series from `price_change` (the `best_bid`/`best_ask` pair
   carried by each changed level), `best_bid_ask`, and `book` events. 16.7 M mid updates →
   **5,443 deduplicated prints** (fills of one `tx_hash` collapsed),
   **322 assets, 242 conditions, 115 wallets**.
3. 1-second grid, **as-of alignment only** (`searchsorted(..., side='right')`,
   never `interp`). `mid0` is the last quote at or before the print's venue ms.
4. `x = sign(wallet side) × (mid(t+H) − mid0)/mid0` in bps. `x > 0` means the mid
   moved the way the wallet traded.
5. **Base rate on every 1 s grid second of the same 322 asset-hours**
   (n = 3,247,043 seconds per horizon), aggregated per asset-hour so a busy asset
   cannot dominate.
6. Drift adjustment: subtract the same asset-hour's unconditional signed drift, so
   an asset-wide trend cannot masquerade as a signal.
7. `t` is clustered by asset (prints of one asset are not independent).

Base rate, unconditional, and on "active" seconds (the mid had already moved within
the previous 5 s — a print can only happen when someone is trading):

| H | P(up) | P(flat) | P(down) | mean | active P(up) | active P(down) |
|---|---|---|---|---|---|---|
| 5 s | 0.090 | 0.819 | 0.091 | +3.2 bps | 0.215 | 0.217 |
| 30 s | 0.180 | 0.643 | 0.178 | +11.2 | 0.315 | 0.314 |
| 60 s | 0.225 | 0.555 | 0.220 | +17.5 | 0.356 | 0.347 |
| 300 s | 0.328 | 0.359 | 0.313 | +46.1 | 0.418 | 0.404 |

Among seconds where the mid moved at all, the base hit rate is **49.5 / 50.2 /
50.5 / 51.2 %** at 5/30/60/300 s. That is the number the hit rates below must beat.

## 3. Results

`hit` = share of prints where the mid moved the whale's way; `hit_move` = same,
restricted to prints where the mid moved at all; `base` = the same base rate among
moving seconds. `mean` is drift-adjusted bps. All horizons: n and clusters in the
columns.

| definition | H | n | assets | hit % | flat % | hit_move % | base % | edge pp | mean bps | med | t |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **taker** (crossed) | 5 | 1,098 | 177 | 50.2 | 1.4 | 50.9 | 49.5 | +1.4 | +55.9 | 0.0 | −0.04 |
| | 30 | | | 47.9 | 1.4 | 48.6 | 50.2 | −1.7 | +69.6 | −0.5 | 0.58 |
| | 60 | | | 51.2 | 1.4 | 51.9 | 50.5 | +1.4 | +99.2 | 1.3 | 1.02 |
| | 300 | | | 51.9 | 1.4 | 52.6 | 51.2 | +1.4 | +153.9 | 10.8 | 0.77 |
| **maker** (resting, filled) | 5 | 3,200 | 198 | 37.2 | 1.1 | 37.6 | 49.5 | −11.8 | −18.9 | −0.5 | 0.34 |
| | 30 | | | 35.5 | 1.1 | 35.9 | 50.2 | −14.3 | −103.8 | −9.9 | −1.85 |
| | 60 | | | 35.4 | 1.1 | 35.8 | 50.5 | −14.7 | −128.8 | −17.9 | −1.05 |
| | 300 | | | 35.4 | 1.1 | 35.8 | 51.2 | −15.5 | **−184.2** | −58.6 | **−3.25** |
| inside (unclear role) | 300 | 1,145 | 221 | 47.8 | 3.3 | 49.4 | 51.2 | −1.8 | +12.4 | 0.0 | −1.59 |
| all prints | 60 | 5,443 | 322 | 39.6 | 1.6 | 40.3 | 50.5 | −10.2 | −59.4 | −14.0 | −0.78 |
| all prints | 300 | | | 41.3 | 1.6 | 42.0 | 51.2 | −9.2 | −74.6 | −27.5 | −1.97 |
| **abs notional ≥ \$1,000** | 5 | 1,081 | 126 | 39.0 | 1.5 | 39.6 | 49.5 | −9.8 | +2.0 | −0.4 | −0.31 |
| | 30 | | | 40.1 | 1.5 | 40.7 | 50.2 | −9.6 | +3.3 | −4.7 | 0.02 |
| | 60 | | | 42.6 | 1.5 | 43.2 | 50.5 | −7.3 | +0.1 | −3.7 | 0.71 |
| | 300 | | | 43.2 | 1.5 | 43.8 | 51.2 | −7.4 | +183.4 | −7.6 | 1.03 |
| **abs shares ≥ 1,000** | 60 | 1,486 | 159 | 41.9 | 1.5 | 42.5 | 50.5 | −8.0 | −51.3 | −9.8 | −0.38 |
| | 300 | | | 42.6 | 1.5 | 43.3 | 51.2 | −8.0 | +43.4 | −14.1 | 0.11 |
| **size > own p90** (assets with ≥10 prints) | 5 | 504 | 96 | 42.7 | 1.0 | 43.1 | 49.5 | −6.4 | +8.2 | −0.2 | 1.51 |
| | 30 | | | 43.8 | 1.0 | 44.3 | 50.2 | −5.9 | −11.5 | −1.2 | 0.31 |
| | 60 | | | 44.2 | 1.0 | 44.7 | 50.5 | −5.8 | −1.3 | −5.5 | 0.65 |
| | 300 | | | 47.8 | 1.0 | 48.3 | 51.2 | −2.9 | +265.0 | −3.6 | 0.47 |
| **size > 5× own median** | 60 | 1,171 | 134 | 42.2 | 1.3 | 42.7 | 50.5 | −7.8 | −30.8 | −11.0 | 0.74 |
| | 300 | | | 43.9 | 1.3 | 44.5 | 51.2 | −6.8 | +127.1 | −17.1 | 0.55 |

Reading it: **every size-based definition sits below the base rate** once the
wallet's role is not controlled for, because the "all prints" population is 59 %
maker fills and maker fills move against the wallet. Once the role is split, the
image is clean — takers ~+1.4 pp and t ≈ 1, makers −15 pp and t = −3.25 — and
**size cuts move the effect in the wrong direction** (the tighter the size filter,
the smaller the deviation).

### Big vs small inside a role

| role | H | big (size > asset median) | small |
|---|---|---|---|
| taker | 60 | n=578, +126 bps, hit 53.1 %, t=1.55 | n=520, +69 bps, hit 49.0 %, t=0.65 |
| taker | 300 | n=578, +218 bps, hit 52.9 %, t=1.76 | n=520, +82 bps, hit 50.8 %, t=0.28 |
| maker | 60 | n=1,513, −129 bps, hit 36.0 %, t=−0.91 | n=1,687, −128 bps, hit 34.8 %, t=−0.94 |
| maker | 300 | n=1,513, −132 bps, hit 36.2 %, t=−1.92 | n=1,687, −231 bps, hit 34.6 %, t=−2.41 |

Size does nothing for the maker side and at most a weak, sub-gate (\|t\| < 1.8) lift
for takers.

### Robustness of the one significant effect (maker, 300 s)

Leave-one-asset-out, dropping each of the six most influential assets in turn:
mean stays in **−136 to −216 bps** and t in **−3.10 to −3.35** (197 assets each).
Not one asset. The top-5 assets carry 39 % of the total sum, so the effect is
broad but heavy-tailed.

### Positioning (the dataset's actual selling point)

Does accumulated whale exposure predict the next move? For each asset-hour with
≥ 2 prints before the midpoint, net signed notional in the first 30 min versus the
mid change in the second 30 min: **n = 103 asset-hours, corr = −0.13**. The
sign-matched return is −817 bps mean but +30 bps median with 52 % up — two or three
large losers, no usable relationship. And the sign is *against* the whale, the same
direction as the maker result.

### Wallets

Of 115 wallets with prints in the analysis window, 13 have ≥ 100. Their drift-adjusted
60 s means span −1,289 bps to +1,821 bps with p(up) from 0.12 to 0.84 — pure
dispersion at these counts (median 175 prints), no rankable skill. A per-wallet
whitelist cannot be built from 3 days of this feed.

## 4. Does it survive at maker latency?

| | value |
|---|---|
| prints arriving ≤ 60 s after the trade | 82 / 5,443 = **1.5 %** |
| prints arriving ≤ 300 s | 571 / 5,443 = 10.5 % |
| median delay | 3,800 s (63 min) on these hours, 23,198 s overall |
| forward drift from the **arrival** clock, drift-adjusted | −27.9 / −33.2 / −35.6 / −37.0 bps at 5/30/60/300 s (t = −0.76/−0.94/−1.18/−1.80) |
| forward drift from the **venue** clock, taker prints | +56/+70/+99/+154 bps, t ≤ 1.02 |

A maker would have to act on the venue clock to see anything, and the venue-clock
effect is not significant anyway. On the clock he can actually observe, the sign is
negative at every horizon.

## 5. Reverse hazard: does a whale print precede worse fill markout?

Two answers, both negative in the sense that matters.

**(a) Against the whale.** When the tracked wallet is the *maker* and gets filled,
its own markout is −184 bps at 300 s (t = −3.25). So a maker resting on the same
side as a whale that has just been filled has a worse markout — 15.5 pp worse hit
rate than the base. But that is just adverse selection on a fill that the tape
already reports instantaneously: clob_trades carries the same `tx_hash` at the same
venue ms, without the wallet. The only extra content is the identity, and the
identity costs hours.

**(b) On the replay fills.** `data/derived/replay/*/fills.parquet` holds **92,478
fills over 2026-11-06 02:10–06:09 on 301 assets / 186 conditions**. Inside that
window the whale feed contributes **2,700 prints, of which only 38 are on one of
the 301 fill assets, spread over 7 assets**. That is 38 prints against 92k fills:
no power, and no honest way to condition a fill's markout on a same-asset whale
print. The timestamps would align (both are venue ms) — the events simply do not
co-occur.

## What would have to be true for this dataset to pay

1. The feed would have to be a **user-channel websocket** (per-wallet) instead of a
   polled activity endpoint: the information must arrive in < 1 s to matter, and
   today it arrives in hours.
2. The book collector's subscription would have to include the assets these
   wallets trade: 85 % of whale prints have no book at all on our side, so even a
   perfect signal would be untradeable on two thirds of the sample.
3. The signal itself is the wrong sign. Whales in this list are **liquidity
   providers who get picked off** (−184 bps per passive fill, t = −3.25), not
   leakers of information. Following them loses; fading them is the same trade as
   the `micro-print-sweep-detector` family, which the replay already scored as
   exposure-only.
