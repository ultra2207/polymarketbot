# PMXT download and reduce runbook

The raw order-book archive is **~1.1 TB and cannot live on this machine** (295 GB free).
Downloading it is an operator task, not something the assistant should launch.

Everything below is free, public, and needs no API key.

## What it is

`https://r2v2.pmxt.dev/polymarket_orderbook_<YYYY-MM-DDTHH>.parquet` — one file per hour,
~371 MB each, full L2 (`book` snapshots, `price_change` deltas, `last_trade_price`,
`tick_size_change`).

| Window | Hours | Size | Role |
|---|---|---|---|
| `2026-04-13T19` → `2026-05-31T23` | 1,158 | ~430 GB | fill validation |
| `2026-06-01T00` → `2026-08-10T23` | 1,703 | ~630 GB | **forward test** |
| **total** | **2,861** | **~1.06 TB** | the only full-L2 history that exists |

Everything after `2026-08-10T23` returns 404. There is no free replacement, and no paid
option is on the table.

## Step 1 — download (you, on a drive with ≥ 1.2 TB free)

> **Status (2026-11-11).** `polymarketbot.data.builders.download` was deleted with the
> `data/builders` tree, so the two commands that used to drive this step no longer resolve.
> What remains is the URL pattern and the measured sizes. A manual pull has to redo the
> parts that command did for you: hour enumeration (absent hours 404 — skip and log them,
> do not count them), ETag-based resume, and the manifest/byte-count probe. Files must still
> land at `<dest>/pmxt/polymarket_orderbook_<stamp>.parquet` for the reducer's glob.

The windows to pull, forward-test first — this is the part that gates the go/no-go:

| Window | Start | End (inclusive) | Files |
|---|---|---|---|
| Forward test | `2026-06-01T00` | `2026-08-10T23` | 1,703 |
| Validation | `2026-04-13T19` | `2026-05-31T23` | 1,158 |

One hour, as the equivalent of the old command's unit of work:

```bash
mkdir -p E:/polymarket_pmxt/pmxt
curl -fL --retry 3 --retry-delay 5 \
  -o E:/polymarket_pmxt/pmxt/polymarket_orderbook_2026-06-01T00.parquet \
  https://r2v2.pmxt.dev/polymarket_orderbook_2026-06-01T00.parquet
```

Notes:

- Resumable by construction: re-run an hour whose file is shorter than 371 MB.
- Measured throughput here: **~15 MB/s**, so the forward-test window is ~12 h and the full
  archive ~20 h. Run it overnight.
- Keep the layout — the reducer globs for it.

Expected result: **1,703 files** for the forward-test window, **1,158** for validation.

## Step 2 — reduce (me, or you)

The reducer streams each hour, reconstructs book state, and writes ~1/25th the bytes. It
never deletes the raw archive in `--dir` mode, and skips hours already reduced, so it is
safe to stop and resume.

```bash
# retired: scripts/reduce_pmxt.py was deleted in the 2026-11-10 scripts cleanup
# and has no `python -m` replacement, so Step 2 can no longer be run from this
# tree. The raw archive is untouched; only the reduced table is unavailable.
```

Output: `data/processed/pmxt_reduced/dt=YYYY-MM-DD/hour=HH/books.parquet`, one row per
`(token, 15-minute bucket)` with `best_bid`, `best_ask`, depth inside 1¢ and 2¢, and the top
5 levels per side.

`--bucket-seconds 300` gives 3× the rows and 3× the compute. 900 s matches a decision cadence
that makes sense at $250/order; raise it if disk is tight.

## Decision: a contiguous block, not the full archive and not a sparse sample

Two candidate plans were on the table. Both were solving the wrong constraint, so both
are rejected — for different reasons.

### Measurement first (2026-11-01)

| Quantity | Measured |
|---|---|
| Free disk | **297 GB** |
| Raw archive, full window | 1.06 TB (2,861 hours x 371 MB) |
| Reduced output, 15-min cadence | **43 GB** |
| Reduced output, 1-hour cadence | **13 GB** |
| Reducer cost, 1-hour buckets | **43.6 s/hour** |
| Reducer cost, 15-min buckets | **~150 s/hour** |
| Download throughput, 1 connection | **0.33 MB/s** |
| Download throughput, 64 range requests | **7.6-9.0 MB/s** |

**Storage was never the blocker.** The full reduced archive fits with room to spare. The
real costs are bandwidth (20 h for 1.06 TB) and compute (35 h at 1-hour cadence, ~110 h at
15-minute cadence).

### Why "download only what is needed" does not work

A PMXT hour is 68,840,573 rows in **66 row groups of ~1.04 M rows** (5.65 bytes/row
compressed). The file is ordered by time, so each row group spans roughly 52 seconds of
the hour and contains **every asset active in that slice**. There is therefore no
row-group pruning that isolates a market: any query for a specific token still reads
essentially every row group. Filtering can only happen after the bytes arrive, so it saves
storage — which we do not need — and saves no bandwidth at all.

### Why sparse sampling does not work

A sparse sample (2 hours per day) saves bandwidth but destroys **continuity**, and
continuity is the one thing the exit-policy comparison requires: the book must exist for
the whole holding period, not at isolated points. A gap in the middle of a position makes
the exit unfalsifiable.

### The decision

**Reduce a contiguous block inside the calibration window, then extend only if the method
earns it.**

| Phase | Window | Hours | Download | Compute @ 15-min | Stored | Gate |
|---|---|---|---|---|---|---|
| **1** | 2026-04-20 → 04-27 | 168 | 63 GB | **3.1 h wall** | ~2.5 GB | build replayer + exit comparison |
| 2 | extend to +21 days | 504 | 187 GB | **9.4 h wall** | ~8 GB | only if phase 1 produces a signal |
| 3 | 2026-06-01 → 08-10 | 1,703 | 630 GB | **31.7 h wall** | ~26 GB | **only after Gate 7**, forward test stays untouched |

Three reasons this is the right shape:

1. **A 7-day block contains complete market lives.** The median market lifetime is 24 h
   (crypto 24.1 h, sports 24.8 h, weather 49 h), so a week holds the entire life of the
   large majority of markets. That is what makes exit evaluation possible without the full
   archive.
2. **It stays inside the calibration window.** Developing on 2026-04-13 → 05-31 is exactly
   what that window is for. Using the forward test for development would make the eventual
   forward test meaningless.
3. **Each phase is independently useful and independently abandonable.** Phase 1 answers
   "does the replayer work and does any exit beat holding". If the answer is no, we stop
   after 63 GB instead of 1.06 TB.

### Download bandwidth (measured 2026-11-01)

The assumption that download was fast was wrong. Measured with curl against
`r2v2.pmxt.dev`:

| Concurrent range requests | Aggregate throughput |
|---|---|
| 1 | 0.33 MB/s |
| 6 | 1.12 MB/s |
| 12 | 2.10 MB/s |
| 24 | 3.15 MB/s |
| 32 | 3.74 MB/s |
| **64** | **7.6-9.0 MB/s** |
| 128 | 2.12 MB/s (throttled) |

The limit is **per connection**, roughly 0.13 MB/s each, and it scales close to
linearly until the host starts throttling somewhere between 64 and 128. At one
connection an hour file takes 18.7 minutes — a 7-day block would have taken two
days. At 64 it takes about 45 seconds. The initial attempt also stalled at
exactly 60 MiB, which is why `fetch_hour` now verifies that every range returned
its exact byte count and retries, rather than trusting a single stream.

`requests` alone cannot exploit this; `fetch_hour` splits each file into
contiguous ranges and fetches them concurrently.

### Reducer performance (measured 2026-11-01, one hour = 68,840,573 rows)

The first reducer took 43.6 s/hour at 1-hour buckets and ~150 s/hour at 15-minute
buckets. Profiling each statement found one dominant cost and one dominant cause.

| Statement | Time | Share |
|---|---|---|
| parse to 80.9 M events | 3.2 s | 8 % |
| DISTINCT keys + levels | 5.3 s | 14 % |
| **ASOF JOIN state** | **29.8 s** | **76 %** |
| top-N ranking | 0.7 s | 2 % |

Running the same statement twice in one session made it **5x slower** (29.8 s ->
161 s): accumulated temp tables crossed the 8 GB limit and DuckDB spilled. With
15.6 GB of RAM the limit, not the algorithm, was setting the pace.

Four changes, in order of what they bought:

1. `preserve_insertion_order = false` plus an explicit `temp_directory`.
2. **State via hash aggregate + forward fill instead of ASOF.** `arg_max(size, ts)`
   collapses each (level, bucket) cell, leaving a 10.8 M-row grid to forward-fill
   with `last_value(... IGNORE NULLS)`. Spill fell from 8.5 GB to 2.2 GB.
3. **Tokens as `int32`, sides as booleans.** `asset_id` is a 77-character decimal
   string; hashing and sorting 81 M of them was pure waste.
4. Explicit `DROP TABLE` after each step so peak memory is one table, not six.

| Cadence | Before | After | Speedup | 2,861 h extrapolation |
|---|---|---|---|---|
| 3600 s | 55.6 s | **18.2 s** | 3.1x | 14.5 h |
| 900 s | ~161 s | **20.9 s** | **7.7x** | **16.6 h** |

Threads were left at 12: 24 threads made the parse step *slower* (8.4 s vs 3.2 s)
by overrunning the memory limit.

**GPU: not used, and now not needed.** DuckDB has no GPU backend. The only route
is cuDF/RAPIDS, which has no native Windows build (Linux/WSL2 only) — a day of
infrastructure. After the memory fix the full window is 16.6 h of compute, so the
payoff does not justify the new failure surface. Revisit only if the window grows.

### Correctness: `book` events are full ladders, not deltas (fixed 2026-11-01)

The first reducer treated every row as an incremental level change. A `book` event is
**the complete ladder at that instant**, so any level absent from it has size 0. Treated as
a delta, every level that ever existed stayed alive forever, and the reconstructed book
slowly filled with stale orders on both sides of the true touch.

Measured against the 145,308 authoritative `book` snapshots inside a single hour:

| | mean spread | |bid error| | |ask error| |
|---|---|---|---|---|
| true snapshots | **+0.0238** | — | — |
| reconstruction, anchors as deltas | **−0.4766** | 0.2526 | 0.2526 |
| reconstruction, anchors reset the book | **+0.0524** | 0.0303 | 0.0523 |

A *negative* mean spread means the book was crossed — bid above ask — which no real book
ever is. The bug is invisible per market (individual assets matched exactly) and obvious
across the population, which is why the check has to be aggregate.

The fix is a generation counter: each asset increments a generation at every anchor, and a
level is only valid while its last update belongs to the current generation.

**How it was found:** not by reading code, but by comparing a reconstruction against the
snapshots that produced it. Every correctness check in this file that mattered came from
that comparison rather than from inspection.

**Consequence:** any partition reduced before this fix is invalid and must be re-run. The
carry file (`_carry/`) exists so hour state survives a boundary; it does not repair a
wrong rule.

### Correctness: the rewrite also removed a lookahead bug

The ASOF version used `ts <= bucket_end`, so an event timestamped exactly at the
snapshot instant contributed to that snapshot. Comparing both outputs against a
direct reconstruction from the raw file, on the 4 rows where they disagreed:

| | Matches ground truth |
|---|---|
| original ASOF | **0 of 4** |
| hash + forward-fill | **4 of 4** |

The bucket is now `[bucket_end - bucket_s, bucket_end)`: an event on the boundary
belongs to the following bucket and cannot appear in a snapshot taken before it.

### Disk safety

The pipeline streams: download one hour → reduce → delete the raw file. Peak raw usage is
therefore one hour (~400 MB), not 63 GB. That is why the block size can be large while
disk stays comfortable.

## Why the reducer exists at all

Three properties of the raw data, each verified against a real hour (`2026-06-01T00`), that
make naive replay wrong:

1. **`best_bid` / `best_ask` on `price_change` rows are stale.** They held `0.50`/`0.51`
   across 15 consecutive events while the ladder moved from `0.01`→`0.11`. Ignore these
   columns entirely; rebuild the touch from levels.
2. **`side` is the ladder side, not the aggressor.** `BUY` = bids (observed 0.01–0.47,
   never above the ask), `SELL` = asks (observed 0.53–0.99, never below the bid).
3. **Anchors are sparse.** Only 8,238 of 73,367 assets have a `book` snapshot in an hour, and
   the busiest asset had 8 anchors against 200,075 events. State must be reconstructed, and
   the reconstruction must carry across hour boundaries.

Also: `market` is the condition id as ASCII inside a BLOB — `CAST(market AS VARCHAR)`, not
`encode(market,'hex')`. All 73,367 probe assets joined to `markets_pit` with zero misses
that way.

## Disk budget

| Item | Size |
|---|---|
| raw archive (external) | ~1.06 TB |
| reduced output at 900 s cadence | ~40–70 GB (see the reducer's own measurement) |
| everything else already held | ~62 GB |

The reduced set is what the backtester reads. The raw archive is only ever touched by the
reducer.
