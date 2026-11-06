# Scheduled releases: pull before the print, not after the headline

claim:      Sixty seconds before a scheduled FOMC decision, every resting quote in the affected conditions should be pulled; doing so is net positive over at least 10 releases, because the release instant is public and a resting quote at that instant is a free option written to armed takers.

mechanism:  The arrival time is public and nothing about the outcome is, so a resting quote in that second is a free option written to armed takers. Unlike a news shock, this risk is scheduled, which makes the pull a pure calendar rule with a computable reward cost: six minutes out of a day, at a known time.

data:       `kalshi_markets` (ticker, close_time, status, recv_ts_ns; 2 s, 1.94 M rows) + mapping artifact + `data/tape/universe/*.json` (question) + `clob_events`. Anchor, measured on the tape: `KXFEDDECISION-26DEC-H0` closes at `2026-12-09T18:59:00Z`, one minute before the 19:00 UTC (14:00 ET) statement, so the Kalshi close timestamp is the release clock. Population, stated honestly: the tape holds **8 distinct KXFEDDECISION tickers and zero CPI or payrolls tickers**, and no FOMC decision occurs inside the current 46-hour window, so this is a forward-armed monitor, not a runnable test today.

module:     `secs_to_release` signed and `release_confidence` (0..1) from how the timestamp was derived. If `release_confidence >= 0.8` and `-60 <= secs_to_release <= +300`: PULL both sides. Outside: QUOTE. Because the rule only ever pulls for six minutes at a known time, the reward cost is bounded and reported per release.

metric:     Paired per-condition net inside the window versus the same condition non-window minutes; secondary = the count of releases covered and |mid move| in the first 60 s versus baseline.

kill:       Kill if fewer than 10 FOMC releases fall inside the tested window (no power; keep it armed), or if window markout is no worse than the same condition non-window minutes, or if the pull window net after reward foregone is <= 0 on two decisions.

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-macro-release-arm/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `secs_to_release` | seconds | kalshi_markets.close_time - now, signed (close = release - 60 s) | 60 s |
| `release_confidence` | 0..1 | 0.9 for the KXFEDDECISION series, 0 when no series anchor exists | static |
| `release_kind` | enum | FOMC only on this tape | static |

## prepare.sql

```sql
-- Release clock from the venue's own close timestamp.
-- Observed on the tape: KXFEDDECISION-26DEC-H0 close_time = 2026-12-09T18:59:00Z,
-- one minute before the 19:00 UTC (14:00 ET) FOMC statement. The rule therefore
-- arms at close_time + 0 s and holds through release + 300 s.
COPY (
  WITH k AS (
    SELECT ticker, close_time, status, recv_ts_ns,
           CAST(replace(close_time, ' ', 'T') AS TIMESTAMPTZ) AS close_ts
    FROM read_parquet('data/tape/kalshi_markets/**/*.parquet')
    WHERE ticker LIKE 'KXFEDDECISION%'
      AND close_time IS NOT NULL
  ),
  m AS (SELECT * FROM read_parquet('data/derived/ideas/cross-macro-release-arm/kalshi_map.parquet'))
  SELECT DISTINCT m.condition_id,
         epoch(k.close_ts) * 1000 AS release_ms,
         0.9 AS release_confidence,       -- series-derived, not a news observation
         'FOMC' AS release_kind
  FROM k JOIN m ON m.kalshi_ticker = k.ticker
) TO 'data/derived/ideas/cross-macro-release-arm/signals.parquet' (FORMAT PARQUET);

-- CPI and payrolls have no Kalshi tickers on this tape, so they get no rows and
-- the rule never fires for them: the module must not act on a guessed calendar.
```

## Runnable now

**no, not today: forward-armed.** The inputs are on the tape (`kalshi_markets.close_time|ticker`, mapping artifact, `clob_events`) and no new collector is needed, but the tape holds 8 FOMC tickers and no FOMC decision inside its 46 hours, so there is nothing to test yet. The scheduler should queue it with `status: proposed` and run it the week of the December 2026 decision.

## Provenance

- `docs/kalshi-hypotheses.md` H4 (scheduled releases as a quote-pull calendar) and E4 in the experiment order.
- Pre-scheduled arming with a hard cancel seconds before the print: `docs/institutional-exogenous-market-making.md` section 2.1.
- BLS/FOMC release clocks (08:30 ET for CPI and payrolls, 14:00 ET for the FOMC statement) are fixed and public, which is what makes a calendar rule possible without a news wire.

## What would change our mind

If PM prices the print before Kalshi settles and before our window closes, the scheduled pull is pure reward loss; the rule then only survives as a spread-widening input on the pull window edge.
