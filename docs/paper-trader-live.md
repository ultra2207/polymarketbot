# The paper trader as a live data stream

Run 2026-11-06, 5 markets, **$1,000,000** of virtual capital, 10 minutes, live
CLOB WebSocket. Session `paper-20261106T113119Z-a8496f`, 1,054 rows.

## What it produced

| table | rows | what it is |
|---|---|---|
| `quotes` | 240 | our resting virtual quotes, 10 tokens x 2 sides x 12 snapshots |
| `positions` | 120 | per-token inventory and cost basis |
| `reward_epochs` | 670 | **one row per token per epoch**: `virtual_score`, `competing_score`, `pool_share_pct`, `mid_price`, payout |
| `portfolio_snapshots` | 12 | cash, equity, projected daily reward |
| `fills` | **0** | no taker crossed a 1 c quote in ten minutes |

## The one thing that is genuinely new

`reward_epochs` is a **live competition feed**. For every market, every minute:
our score, the competitors' score, and the pool share those imply. Nothing else
in this repo measures `competing_score` on live data — the replay can only see it
through the tape, and the tape has no competitors on most markets.

The session shows why that matters:

| condition | epochs | payout | mean our score | mean competing score |
|---|---|---|---|---|
| `0xa09ec3a3…` | 11 | **$1.506** | 563 | **2,406** |
| `0x857398c4…` | 11 | $0.055 | 536 | 73,394 |
| `0xf0364816…` | 11 | $0.015 | 74 | 38,330 |
| `0xbc74ddca…` | 11 | $0.003 | 58 | 132,114 |
| `0x48a3e95c…` | 11 | $0.000 | 0 | 120,166 |

**95 % of the income came from one market where the competitions' score was 2,400
instead of 40,000-130,000.** Same pool size, same quote, 30-500x the payout. That
is the reward formula doing exactly what it says, and it is invisible from a
static universe file.

The startup projection said **$4.10/day**. The session earned **$1.58 in 11
minutes** (~$206/day). An instantaneous projection is not a forecast: it samples
one minute of competition and the share moves by two orders of magnitude between
minutes.

## What it cannot do

- **No fills in 10-25 minutes across two sessions.** These markets trade a few
  times an hour, so a paper session yields no fill outcomes at any duration this
  machine can wait for. Fill-markout data still has to come from the replay.
- **Capital is not the constraint.** $1,000,000 bought 500-share quotes; the
  venue minimum is 200-500 shares, so the book was `$25-50` per market and the
  rest sat idle. The ROI of a paper session is meaningless as a percentage of
  capital - the real figures are `$` per market per day.
- **It is not a latency source.** Our host clock sits 699 ms off the CLOB (median
  `clock_offset`, RTT 193 ms), drifting to 1,318 ms on 11-06 from 384 ms on
  11-05. Any latency claim has to correct for that first.

## The wide run: 40 markets, 15 minutes

`--markets 40` (80 tokens) is the configuration that produces a dataset. Final
epoch across the 40 markets:

| statistic | value |
|---|---|
| competing score, min / median / max | 4,551 / 233,159 / **3,043,284** |
| spread | **666x** |
| markets giving us a non-zero share | 17 of 40 in the final minute, 24-48 tokens per epoch out of 80 |
| our score in the busiest market | 1,058 against 3,043,284 - a 0.069 % share |
| simulated fills | 1 (SELL 11 @ $0.12) |

The competition is not merely skewed across markets, it moves: the count of
tokens earning a non-zero share swings between 24 and 48 across fourteen epochs.
That is the panel a selection rule needs, and it exists nowhere else in the repo.

## What it is worth doing

Run it **wide, not deep**: `--markets 40-80 --duration 3600 --snapshot-interval 60`
produces a competition panel across the reward universe. One caveat from the wide
run: with 40 markets the session took 23 s to rank and subscribe, and the graceful
shutdown needs headroom - a 900 s session under a 960 s `timeout` was killed mid
stop, which no longer loses data now that snapshots are periodic, but leaves the
manifest unfinalised.

## Defect found and fixed

The paper recorder wrote **only** at `start()` and `stop()`. The first attempt was
killed by its own `timeout` exactly when `stop()` would have run, and the session
directory was left with a manifest and five empty folders - 25 minutes of data
lost to the same write-at-the-end pattern the repo's data rules exist to prevent.
`PaperSessionRecorder.snapshot()` is now public and the runner calls it every
`--snapshot-interval` seconds; a kill costs one interval.
