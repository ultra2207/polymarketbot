# Live session ↔ public tape join

- Session: `20261105T084909Z-abbb5f` (dry-run), window `2026-11-05T08:49:09.789451+00:00` → `2026-11-05T08:49:18.910701+00:00`; public tape run `20261105T124829`.
- Condition: `0x0f49db97f71c68b1e42a6d16e3de93d85dbf7d4148e3f018eb79e88554be9f75`.
- Observations: **14** engine_quote=12, quote_placed=1, venue_order=1.

## Tape coverage

- `clob_events` rows for the condition: **37** (best_bid_ask=18, book=19).
- `clob_trades` rows: **9**.
- Coverage: `2026-11-05T07:57:10.523883+00:00` → `2026-11-05T09:44:02.061527+00:00`; overlap with the session window: **9.1 s**.

## Key overlap

- Session tokens: ['`54533043819946592547517511176940999955633860128497669742211153063842200957669`', '`87854174148074652060467921081181402357467303721411487145860486612047910751019`']
- Tape assets: ['`54533043819946592547517511176940999955633860128497669742211153063842200957669`', '`87854174148074652060467921081181402357467303721471806610111179101805869578687`']
- Matched asset keys: ['`54533043819946592547517511176940999955633860128497669742211153063842200957669`']
- Unmatched session assets: ['`87854174148074652060467921081181402357467303721411487145860486612047910751019`']

## Nearest-event join

- Nearest event |Δ|: n=10, p50=248608.6 ms, p95=399886.4 ms, max=399886.4 ms.
- Observations within 60 s of a tape event: **0/14**.
- Nearest trade |Δ|: n=10, p50=346807.8 ms, p95=399860.3 ms.

## Context

- Tape feed latency (recv_ts_ns − venue_ts_ms): p50=-130.6 ms, p95=8055.3 ms (n=37).

## Findings

- The condition is present in the public tape; 14 session observations carry joinable condition/asset keys.
- Session token(s) absent from the tape: `87854174148074652060467921081181402357467303721411487145860486612047910751019`. The CLOB market detail is authoritative; the runner now refuses a `--token-*` override that disagrees with it. Re-run with the CLOB token or omit the flag.
- No fills in this session (dry-run); the fill join path is exercised once a live session records executions.
- The condition's public book/trade stream is change-driven; the nearest event is 248609 ms away. In a quiet book the last snapshot remains the state until the next change, so event proximity is context, not a validity test. This dry-run window had no public update within 60 s.

The public tape is market context, not an execution record. The join is by
`condition_id`/`asset_id` and UTC `recv_ts_ns`. The collector writes book
snapshots on change, so a long nearest-event delta can mean an unchanged
book (the last snapshot is still the state), not a missing join. The
nearest-event delta is not a clock correction.
