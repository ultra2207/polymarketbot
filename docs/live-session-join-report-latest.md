# Live session ↔ public tape join

- Session: `20261105T100343Z-ca34a2` (dry-run), window `2026-11-05T10:03:43.673533+00:00` → `2026-11-05T10:03:52.779774+00:00`; public tape run `20261105T124829`.
- Condition: `0x0f49db97f71c68b1e42a6d16e3de93d85dbf7d4148e3f018eb79e88554be9f75`.
- Observations: **12** engine_quote=4, quote_placed=4, venue_order=4.

## Tape coverage

- `clob_events` rows for the condition: **9** (best_bid_ask=4, book=5).
- `clob_trades` rows: **2**.
- Coverage: `2026-11-05T09:12:53.584668+00:00` → `2026-11-05T09:50:32.549984+00:00`; overlap with the session window: **0.0 s**.

## Key overlap

- Session tokens: ['`54533043819946592547517511176940999955633860128497669742211153063842200957669`', '`87854174148074652060467921081181402357467303721471806610111179101805869578687`']
- Tape assets: ['`54533043819946592547517511176940999955633860128497669742211153063842200957669`', '`87854174148074652060467921081181402357467303721471806610111179101805869578687`']
- Matched asset keys: ['`54533043819946592547517511176940999955633860128497669742211153063842200957669`', '`87854174148074652060467921081181402357467303721471806610111179101805869578687`']
- Unmatched session assets: —

## Nearest-event join

- Nearest event |Δ|: n=12, p50=796317.9 ms, p95=800154.4 ms, max=800154.4 ms.
- Observations within 60 s of a tape event: **0/12**.
- Nearest trade |Δ|: n=6, p50=796321.0 ms, p95=800153.5 ms.

## Context

- Tape feed latency (recv_ts_ns − venue_ts_ms): p50=-244.1 ms, p95=3115.7 ms (n=9).

## Findings

- The condition is present in the public tape; 12 session observations carry joinable condition/asset keys.
- The session window does not overlap the tape's condition coverage; nearest-event deltas are context only.
- No fills in this session (dry-run); the fill join path is exercised once a live session records executions.
- The condition's public book/trade stream is change-driven; the nearest event is 796318 ms away. In a quiet book the last snapshot remains the state until the next change, so event proximity is context, not a validity test. This dry-run window had no public update within 60 s.

The public tape is market context, not an execution record. The join is by
`condition_id`/`asset_id` and UTC `recv_ts_ns`. The collector writes book
snapshots on change, so a long nearest-event delta can mean an unchanged
book (the last snapshot is still the state), not a missing join. The
nearest-event delta is not a clock correction.
