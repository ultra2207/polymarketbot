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

Run from the repo root. `--dest` must be the external drive.

```bash
# forward-test window first - this is the part that gates the go/no-go
uv run scripts/download.py --only pmxt \
  --pmxt-start 2026-06-01T00 --pmxt-end 2026-08-10T23 \
  --dest E:/polymarket_pmxt --yes
```

Then the earlier validation window:

```bash
uv run scripts/download.py --only pmxt \
  --pmxt-start 2026-04-13T19 --pmxt-end 2026-05-31T23 \
  --dest E:/polymarket_pmxt --yes
```

Notes:

- Resumable and ETag-pinned. Ctrl-C is safe; re-run the same command.
- It probes every hour first and writes a manifest, so you see the true byte count before
  committing. Absent hours are logged, not counted.
- Measured throughput here: **~15 MB/s**, so the forward-test window is ~12 h and the full
  archive ~20 h. Run it overnight.
- Files land at `<dest>/pmxt/polymarket_orderbook_<stamp>.parquet`. Keep that layout — the
  reducer globs for it.

Expected result: **1,703 files** for the forward-test window, **1,158** for validation.

## Step 2 — reduce (me, or you)

The reducer streams each hour, reconstructs book state, and writes ~1/25th the bytes. It
never deletes the raw archive in `--dir` mode, and skips hours already reduced, so it is
safe to stop and resume.

```bash
uv run scripts/reduce_pmxt.py --dir E:/polymarket_pmxt --bucket-seconds 900
```

Output: `data/processed/pmxt_reduced/dt=YYYY-MM-DD/hour=HH/books.parquet`, one row per
`(token, 15-minute bucket)` with `best_bid`, `best_ask`, depth inside 1¢ and 2¢, and the top
5 levels per side.

`--bucket-seconds 300` gives 3× the rows and 3× the compute. 900 s matches a decision cadence
that makes sense at $250/order; raise it if disk is tight.

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
