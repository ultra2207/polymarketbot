# Reward is a per-minute integral of competition; the harness scores one instant

`slug:` m-competition-minute-integral
`focus:` epoch cadence — does sampling timing matter?
`market scope:` high-volume only. The signal exists only where competitive band score
`C(t)` changes inside a minute; on thin markets `C` is constant and the idea collapses
to the baseline.

## Mechanism

The venue pays `P/1440 * Q/(Q + C)` per sampled minute, and the sample is drawn once per
minute at a random offset. Two things follow that the current decision stack ignores.

1. **The statistic is `E_t[1/(Q + C_t)]`, not `1/(Q + C_now)`.** Because `1/(Q+C)` is
   convex in `C`, `E_t[Q/(Q+C_t)] >= Q/(Q+E_t[C])`. A competitor who flickers in and out
   of the band is worth less than its displayed snapshot, and a market whose band depth
   is spiky is worth *more* than a market with the same average depth and a flat book
   (Jensen). The engine's `competing_score` is an instantaneous snapshot recomputed on
   every book update (`src/polymarketbot/paper_trading/engine.py:311-313`, surfaced as
   `BookContext.competing_score`, `src/polymarketbot/strategy/base.py:79`), so a rule
   built directly on it is measuring the wrong moment.
2. **Presence is a per-minute Bernoulli, so reacting to an instantaneous spike is
   strictly dominated by waiting.** A pull in response to a `C` spike forfeits the whole
   minute with probability `1 - (spike duration / 60)`; if the spike does not survive to
   the sample the forfeiture was free. The correct test is "has the crowd *persisted*",
   not "is the crowd here now".

The harness makes this concrete and, at present, wrong in a specific way: it samples on
a fixed 60 s grid anchored at `floor(start)` (`scripts/replay_all_markets.py:471-474`),
not at a random offset. So in the replay, presence at `:00` of each minute is worth
100 % of the minute and presence at `:30` is worth nothing, whereas on the venue each is
worth 50 %. Any strategy that duty-cycles presence is measured through phase luck.

## Rule

In `decide(ctx)`, maintain a *time-weighted* competition integral using the only clock a
module has, `ctx.seconds_elapsed` (`base.py:98`):

```
dt = ctx.seconds_elapsed - last_t;            last_t = ctx.seconds_elapsed
C  = ctx.book.competing_score                  # base.py:79
S  = ctx.quote_size                            # base.py:110
A += dt / (S + C);  T += dt
share_avg = S * A / T                          # venue-consistent expected share
share_now = S / (S + C)
```

Decision: `QUOTE` both sides while `share_avg` is above the market's entry bar. Do **not**
pull on `share_now < share_avg / 2` unless the low-share state has occupied more than
50 % of the trailing 60 s (tracked as `frac_dwell`). Because `A` and `T` are
event-timestamped, the integral is independent of how often the tape updates the book.

The market-level companion (a `prepare.sql` alternative, if the in-module buffer proves
too noisy) is a per-(asset, second) `C` series with columns
`(asset_id, ts_second, comp_score)`, produced by replaying `price_change` deltas onto the
last `book` snapshot — the same reconstruction the harness already performs with
`Ladder` (`scripts/replay_all_markets.py:213-233`).

**Already implemented:** nothing of this. The engine stores a snapshot only
(`engine.py:311-313`); the reward sample reads that snapshot once per minute
(`engine.py:1240-1262`). No code averages `C` over time, and no sampler randomises the
offset. The 3.5 s minimum-rest rule (`src/polymarketbot/rewards/scoring.py:73,105`) is
*not* enforced by the replay's scoring path at all (`engine.py:710-711` calls
`compute_quadratic_score` with no duration argument), which is a separate, larger
measurement gap noted in `mINDEX.md`.

## Why not ruled out

Facts 1-8 are about our width, size, markout and venue coverage; none is about the
*temporal aggregation* of competition. The registry's `micro-competition-dilution` uses
the instantaneous whole-band `C` for a size rule, and `micro-reward-priced-pull`
prices a pull against forfeited reward using a markout prior — neither integrates `C`
over the minute, and neither distinguishes a spike from a regime. The replay's
phase-locked sampler is not documented anywhere in `ideas/registry.json`.

## Kill criterion

- Kill if the liquid cohort's within-minute `C` coefficient of variation is below 0.2:
  the integral then equals the snapshot and the rule has no degrees of freedom.
- Kill if `corr(share_now, realised minute payout)` is not materially below
  `corr(share_avg, realised minute payout)` on the same asset-minutes (the claim is that
  the average is the better predictor; if it is not, the convexity is too small to act on).
- Kill if the spike filter changes fewer than 5 % of `decide` outcomes.

## Data needed

Tape only: `data/tape/clob_events/date=*/hour=*/*.parquet` (`book` snapshots plus
`price_change` deltas; 838k price changes and 596 full books in the last hour partition
alone) and the universe snapshot for `max_spread`/`min_size`. No new feed. The in-module
version needs no `prepare.sql` at all.
