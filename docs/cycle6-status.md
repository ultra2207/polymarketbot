# Cycle 6 Status and Next Actions — 2026-11-09

## Done

- [x] Logging port: `polymarketbot.logs` writes `data/logs/app.log` plus one rotated
  per-source-file log; `MALLOC_ARENA_MAX=2` documented for 1 GB hosts.
- [x] `AGENTS.md` updated with the 30-minute soak + 3-auditor loop, no-PTY launch,
  and the corrected host-memory requirement.
- [x] 30-minute soak loop run (cycles through the earlier audits).
- [x] Cycle 6 integration: settlement S2/S5/S6/S8/S9/S10/S12; CTF
  C3/C4/C5/C7/C9/C10/C11/C12; shared-file conflicts resolved and tree compiles.
- [x] Bounded smoke runs completed; 178-test targeted suite passed; focused
  28-test reward/settlement suite passed after the memory changes.
- [x] Current host diagnosis: t3.micro, 930 MB RAM, 2 GB swap; soak RSS 441–558 MB
  plus 320–570 MB agent harness; journald reported memory pressure and the
  30-minute run restarted the instance.
- [x] Memory fixes in `scripts/run_live_paper_soak.py`:
  - bounded 500-token `LazyBookCache` (two chunks resident),
  - exact spread trimming of reward books,
  - compact reward pool rows,
  - `malloc_trim` after chunk churn,
  - RSS/available-memory dashboard line,
  - dynamic memory guard,
  - fixed a stale-allocation `KeyError` in `MarginalRewardAllocator._improve`,
  - 60 s startup grace to stop reward/settlement/CTF scans overlapping,
  - normal `uv run python` launch, never PTY.
- [x] Local India analysis: `docs/local-pc-india-analysis.md`.
- [x] Local latency probe: `scripts/measure_local_latency.py` (validated from
  London: CLOB p50 37–39 ms, Gamma 18 ms, Binance 225 ms, `/books` 37 ms).

## Blocked

- [ ] **Cycle 6 30-minute soak** — blocked on host. t3.micro is not sufficient;
  the 30-minute run restarted the instance. Do not relaunch on 1 GB.
- [ ] **Loop repeat** — blocked on a >=2 GB instance or the local 16 GB PC.
- [ ] **Reward R2/R6/R7–R10/R11/R12/R14–R15** — code changes exist but remain
  unverified against a clean 30-minute run because the host cannot host one.

## Next actions on the local India PC

1. Pull `master` and run `uv sync --upgrade`.
2. Measure the real path (peak and off-peak, p95 not p50):

   ```bash
   uv run python scripts/measure_local_latency.py --samples 100
   ```

   If p95 > 250 ms or loss > 1%, keep CtfMinter live in London.
3. Run the 30-minute soak (16 GB local PC; no memory guard pressure):

   ```bash
   MALLOC_ARENA_MAX=2 uv run python scripts/run_live_paper_soak.py \
     --capital 3000 --duration 1800 --interval 10 --fresh
   ```

4. Record the UTC window, per-pillar start/end equity, fills, and errors from
   `data/logs/scripts.run_live_paper_soak.log`.
5. Dispatch the three strategy auditors against that exact 30-minute window.
6. Treat local CtfMinter paper PnL as latency-optimistic: the current model uses
   the fixed `MEASURED_MAKER_MARKOUT = 0.0063`, derives `p_leg` from seller flow
   with no cancel-race term, and only records RTT in telemetry. Add latency
   stress or fit a local markout before believing CTF live.

## Key artifacts

- `docs/local-pc-india-analysis.md` — full local-India strategy analysis.
- `docs/audit-cycle1.md`, `docs/audit-cycle4.md`, `docs/audit-cycle5.md` — prior
  audit records.
- `scripts/measure_local_latency.py` — DNS/TCP/TLS/TTFB probe for CLOB, Gamma,
  Binance.
- `scripts/run_live_paper_soak.py` — 3-pillar paper soak with the memory-safe
  full-universe scanners.
