# DVOL jump: a vol-regime shock makes every resting binary quote cheap gamma

claim:      A >= 3 % rise in Deribit DVOL inside 10 minutes precedes a burst of adverse PM crypto-binary fills; pulling both sides for 2 minutes after the jump improves net by at least 0.3 c/share.

mechanism:  A binary gamma scales as 1/tau, so a vol-regime jump reprices every quote on the same underlying at once, and the repricing is a function of the second moment rather than the price. A realized-volatility estimate built from Polymarket mids needs the move to happen first; the option surface reprices on the shock itself, so the surface leads every PM-derived volatility estimate by construction. The shock is mean-reverting, which bounds the cost of a fixed re-entry window, and the maker is paid by the taker who buys the stale gamma rather than by any view on direction.

data:       `deribit_surface` (dvol, atm_iv, term_7d, venue_ts_ms; 10 s, 27,392 rows) + `clob_events`. DVOL is a 30-day index, so the signal is a regime, not a strike-level quantity.

module:     Per-market state: `jump_at`, measured in `ctx.seconds_elapsed` only (never wall clock). If `dvol_ret_10m >= 3` and `dvol_z_1h >= 1.0`, set `jump_at` and PULL both for 120 s of decision time, then SHRINK both to 0.5 for 300 s, then resume. An equal-size DVOL fall does nothing. A stale signal does not change state.

metric:     Paired per-condition net; secondary = markout of fills inside the 120 s window versus the same condition fills outside any window.

kill:       Kill if fewer than 10 DVOL jumps occur in the tested window (re-scope before spending replay time), or paired net improvement < +0.3 c/share, or window fills are not more adverse than non-window fills by >= 0.3 c/share (the gate selects nothing).

## Signal contract (`ctx.signals`)

`prepare.sql` writes `data/derived/ideas/cross-deribit-dvol-jump/signals.parquet`, one row per
`(condition_id, ts_ms)`; the harness left-joins it to the decision timestamp **backwards
only** (as-of, never forward). A signal whose age exceeds its max-age, or a condition
with no row, is **absent**, not zero: the module then keeps both sides.

| signal | unit | built from | max age |
|---|---|---|---|
| `dvol_ret_10m` | percent | deribit_surface.dvol, 10 min log change | 60 s |
| `dvol_z_1h` | sigma units | dvol_ret_10m / trailing 1 h std of itself | 60 s |
| `dvol_level` | vol points | deribit_surface.dvol | 60 s |

## prepare.sql

```sql
COPY (
  WITH d AS (
    SELECT index_name, venue_ts_ms, dvol,
           ln(dvol / lag(dvol, 60) OVER w) * 100 AS dvol_ret_10m,
           stddev_samp(ln(dvol / lag(dvol, 60) OVER w))
             OVER (PARTITION BY index_name ORDER BY venue_ts_ms
                   ROWS BETWEEN 359 PRECEDING AND CURRENT ROW) * 100 AS sigma_1h
    FROM read_parquet('data/tape/deribit_surface/**/*.parquet')
    WHERE dvol IS NOT NULL AND dvol > 0
    WINDOW w AS (PARTITION BY index_name ORDER BY venue_ts_ms)
  ),
  map AS (SELECT * FROM read_parquet('data/derived/ideas/cross-deribit-dvol-jump/asset_map.parquet'))
SELECT map.condition_id, d.venue_ts_ms AS ts_ms, d.dvol AS dvol_level, d.dvol_ret_10m,
       d.dvol_ret_10m / nullif(d.sigma_1h, 0) AS dvol_z_1h
FROM d JOIN map ON map.deribit_index = d.index_name
WHERE d.dvol_ret_10m IS NOT NULL
) TO 'data/derived/ideas/cross-deribit-dvol-jump/signals.parquet' (FORMAT PARQUET);

-- asset_map.parquet: condition_id -> deribit_index ('btc_usd' / 'eth_usd'),
-- built from the universe snapshot (deribit_surface.index_name carries exactly
-- those two strings, checked on the tape).
```

## Runnable now

**yes, no new collector.** `deribit_surface.dvol|index_name|venue_ts_ms` is on the tape at 10 s (27,392 rows, 11-04 00:37 -> 11-06 05:15).

## Provenance

- `docs/data-collection.md` Deribit stream (REST, 10 s).
- The gamma singularity as tau -> 0 is laid out in `docs/institutional-exogenous-market-making.md` section 1.3.

## What would change our mind

If DVOL jumps do not raise adverse markout, the PM book is not priced off the option surface at all and the Deribit branch is a level signal only (the digital-basis idea), not a timing signal.
