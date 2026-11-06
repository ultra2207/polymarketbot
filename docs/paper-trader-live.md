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

## What it is worth doing

Run it **wide, not deep**: `--markets 50 --duration 3600 --snapshot-interval 60`
produces a competition panel across the reward universe, which is the missing
input to every market-selection rule (the ones that died on the replay did so for
lack of exactly this column). Ten minutes on five markets is a smoke test, not a
dataset.

## Defect found and fixed

The paper recorder wrote **only** at `start()` and `stop()`. The first attempt was
killed by its own `timeout` exactly when `stop()` would have run, and the session
directory was left with a manifest and five empty folders - 25 minutes of data
lost to the same write-at-the-end pattern the repo's data rules exist to prevent.
`PaperSessionRecorder.snapshot()` is now public and the runner calls it every
`--snapshot-interval` seconds; a kill costs one interval.
