# Working agreement

Rules for agents working in this repository. They exist because long, unverified
runs have already wasted hours here.

## Process discipline

- **One compute process at a time.** This machine has one CPU. Two 12-thread
  DuckDB jobs run roughly 4× slower each than the same jobs run in series.
  Downloads are the one exception: they are I/O bound and may overlap.
- **First run of any script gets a 300-second timeout.** Never launch a script
  for the first time with a longer timeout. A script earns a long timeout only
  after it has been observed to work and to be optimised.
- **If a run exceeds 300 seconds: kill it, then inspect what it actually
  produced.** Delete the assumption that it was "almost done".
  - Produced nothing (no output file, no row count growth, no journal growth)
    → the script is wrong. Change it before running again.
  - Was progressing (journal lines growing, output partition growing, printed
    per-iteration progress) → relaunch with a longer timeout.
- **Never kill blind.** Every long job must emit incremental, observable
  progress — a row count, a partition file, a journal line. A job whose progress
  cannot be observed cannot be trusted.
- **Never pipe a long job through `tail`, `head` or any pager.** The pipe buffers
  until the process exits, so a run killed at its timeout produces *no output at
  all* — the one moment you most need to see how far it got. This has already
  cost a full 300-second run here.
- **Scripts log with `loguru` via `polymarketbot.logs.get_logger(name)`, not
  `print`.** Every script writes `data/logs/<name>.log` in addition to stderr.
  The consequence: when a task longer than a minute dies, is killed, or its
  terminal output is lost, **read the log instead of rerunning**. Rerunning to
  recover an answer is always the more expensive mistake.
- **A long job must persist its work as it goes**, one file per unit of work, and
  skip units already present. Then a 300-second cap is not a failure — it is
  progress, and the next bounded run continues where the last one stopped.
  `scripts/screen_calibration.py` writes one parquet per quarter for this reason.
- **Subagent / background waiting cadence.** Never poll or set rapid sub-minute
  timers (e.g. 15–30s) when waiting on background subagents or long tasks. Wait
  and check on a ~3-minute (180s) cadence to prevent wasting LLM context tokens,
  relying on reactive notifications to wake execution when subagents finish.


## Where code lives

- **`scripts/` is for temporary, throwaway tooling only.** One-off probes, migration
  helpers, and experiments live there and are deleted when their question is answered.
- **`tmp_python/` is for scratch Python too short-lived to earn a `scripts/` entry.**
  Any throwaway probe longer than a few lines is written to a file there and run with
  `uv run python tmp_python/<name>.py` — never pasted into the shell as a heredoc. The
  reasons are practical: a multi-line heredoc mangles quoting and indentation, is
  unreviewable after the fact, cannot be re-run or amended without rewriting it, and
  buries the result in a wall of echoed source. `tmp_python/` is gitignored.
- **Anything run repeatedly lives in `src/polymarketbot/` and is invoked with
  `python -m`.** The live paper soak is `polymarketbot.paper.main`, not a script:
  `uv run python -m polymarketbot.paper.main --capital 3000 --duration 1800 --interval 10`.
- A long-lived entry point that grows past a few hundred lines is a design error: split
  it into modules under its package, where each decision rule is testable without I/O.

## Time budget — hard rule

- **Nothing runs longer than 60 seconds until it has been proven at 60 seconds or
  less.** Before any long run, the same code path must have been executed on a
  small slice (one month, one partition, a `LIMIT`ed subquery) and returned a
  correct result. A query that has never completed quickly does not get a long
  timeout — it gets a smaller input.
- **Any single invocation is capped at 600 seconds.** Not 900, not 1800, not
  "it should be nearly done". If the work genuinely needs longer, it is a
  different job: split it into units that persist their output and skip units
  already done.
- **Work longer than 600 seconds is an extreme special case** and must be run as
  a sequence of ≤600-second blocks: run a block, inspect what it actually
  produced, then run the next block. Never launch one long call and hope.
  A 12-hour task that produced nothing usable is the failure mode this rule
  exists to prevent.
- **Every long run states its expected cost up front** — tables scanned, row
  counts entering each stage, where the reduction happens — and is checked
  against that estimate when it returns. A run that took 10× longer than
  predicted is a bug report, not a success.

- **Every operation that can run long saves incrementally at a reasonable cadence**, not
  just "long jobs". The unit of persistable work should be small enough that a timeout
  costs one unit, and the next run resumes by skipping units already written. A 10-minute
  build that writes one 14 GB file at the end loses everything to a 600-second cap — which
  has already happened here. Partition the work (by month, by hash bucket, by wallet range)
  and write one file per partition.
- **Check the write cadence against the timeout**: if a run is expected to take N seconds
  and the cap is 600, there must be at least N/300 completed, durable outputs along the
  way, so a kill is always inspectable progress rather than a total loss.

## SQL discipline

Every query is written against a 45 GB tape and a 250 M-row cache. A query that
would be fine on a toy database is a 10-minute run here. **Optimise before
executing, not after it times out.**

- **Rank and aggregate small tables first, join second.** Never apply a window
  function on the output of a large join: `ntile(10) OVER (ORDER BY vol DESC)`
  placed after `pr JOIN w` sorts every position row (250 M), not every wallet
  (500 k). Compute the decile on the small table, then join the label onto the
  large one.
- **A window function's output cannot be pushed down.** `WHERE dec = 1` applied
  outside a window produces no filter during the join — the full join still
  runs. Materialise the filtered set once into a temp table and reuse it.
- **Project only the columns used.** `SELECT pr.*` forces every column through
  the join and sort pipelines. Three columns in, three columns out.
- **Group on integers, format strings at the end.** Concatenating a label per
  row (`b*10||'-'||…`) evaluates the expression repeatedly and allocates a
  string for each of 250 M rows. Group by the raw integer and format the 10
  result rows in Python.
- **Always filter on the partition/decile key before aggregating**, and check the
  row count that survives the filter — if a step is meant to reduce to 25 M rows
  and it does not, stop and fix the query.
- **Validate the plan on a small slice first.** Run the query against one month
  or with a `LIMIT`-ed subquery before the full pass; a wrong join key or an
  ambiguous column costs minutes when it fails on 250 M rows.
- **Statement of intent:** before running anything longer than ~60 s, state which
  table is scanned, roughly how many rows enter each stage, and where the
  reduction happens.

## Data discipline

- `data/raw` is immutable. Derived files are rebuilt, never edited in place.
- Long jobs write incrementally so a kill loses at most one interval, and are
  resumable by replaying what was already written.
- Never coerce an unknown into a zero. A missing label is `missing`, not 0 and
  not 0.5.
- A number that came from a query is evidence. A number that came from an
  assumption is a guess and must be labelled as one.

## Verification discipline

- Any screen or backtest result must state its window, its sample size, and
  whether its standard errors account for correlated observations. Two tokens of
  the same market are perfectly anti-correlated, not independent.
- Optimisations are measured, not assumed. `GROUP BY ALL` over list columns made
  the PMXT reducer 65 % slower than the four subqueries it replaced.

## Ground-Truth Protocol Re-baselining & Multi-Agent Audit Workflow

- **No Simulated Market Assumptions (Option B Mandate).** All strategies must execute against
  physical protocol realities:
  - **Exact Smart Contract Interfaces:** ERC-1155 `ConditionalTokens` rules (`splitPosition`,
    `mergePositions`, `redeemPositions`), exact UMA oracle resolution states (`umaResolutionStatus`,
    `closed`, not ambiguous `resolvedBy` factory addresses).
  - **Strict Fee & Rebate Modeling:** Taker fees must be tracked at exact entry ticks ($0.0022/share
    at $0.940). Maker fee rebates must be credited upon fill.
  - **Unified Mark-to-Par Accounting:** Net Equity = $\text{Cash} + \sum (\text{Shares} \times \text{ParValue}) - \text{FeesPaid}$.
    Never record an unfinalized discounted win ($0.940$) as an equity drawdown simply because par redemption
    is waiting on oracle finalization.
  - **First-Principles Solvers:** Zero greedy heuristics or arbitrary caps. LP rewards must use KKT
    water-filling convex optimization; CTF must use maker parity bundles; Settlement must use
    hazard-rate / Bayesian survival horizon scans across the full active universe.

- **Production Soak & 30-Minute Continuous Audit Cycle:**
  1. The live paper trader daemon runs continuously with all 3 pillars active.
  2. Every 30 minutes, perform a rigorous system audit by dispatching **3 dedicated subagents**:
     - **Subagent 1 (Reward Harvester Auditor):** Inspects liquidity reward allocations, queue
       priority decay, adverse selection fills, and marginal yield optimization.
     - **Subagent 2 (Settlement Harvester Auditor):** Audits active horizons, pending par redemptions,
       taker fee leakage, and capital turnover velocity across the 10,000+ market radar.
     - **Subagent 3 (CTF Minter Auditor):** Checks maker parity quoting spreads ($\text{Bid}_A + \text{Bid}_B < 1.0$),
       fill balance across legs, inventory skew, and merge efficiency.
  3. All findings, detachments from reality, non-optimal code, and money left on the table are
     logged, reported, and remediated immediately.

## Optimizer, not hand filters

- **No hardcoded values, at all.** A value must be fetched dynamically from the venue/API,
  measured from historical data, or derived from live observations. The only exceptions
  are documented protocol constants (tick size, minimum order size, fee schedule, gas
  units). Anything else — a cap, multiplier, threshold, hurdle, reserve, or fallback — is
  a bug. Do not replace one guessed constant with another; fit it or measure it.
- **Never shrink the tradable universe to make a scan faster.** Limiting the available
  market space to improve universe-walk speed or latency is not acceptable: it can produce
  a non-global-optimum allocation. Accept more API usage, parallelism, caching, indexing,
  or a better algorithm instead. Every filter must be applied **inside the optimizer**,
  with its economic reason and measured basis logged; never silently pre-filter candidates.
- **If tractability needs a bound, make it a measured compute/time budget** with a logged
  error bound; never silently truncate the candidate set to make the solver easy.
- **The optimizer decides allocation, switching, slot count, and size.** Hand ranking
  followed by a small subset is not an optimizer. If the solver cannot handle the full
  set, iterate on the solver until it can; do not restore caps.

## Continuous 30-minute paper-trader audit loop (latest operating instructions)

This is the standing refinement loop for the three live paper strategies:
**RewardHarvester**, **SettlementHarvester**, and **CtfMinter** (LatencySniper stays benched).

### Logging contract (do not regress)

- Every Python file that produces output calls
  `from polymarketbot.logs import get_logger; log = get_logger(__name__)` and uses
  `log.info` / `log.warning` / `log.error`; no bare `print()` in application or script code.
- `polymarketbot.logs` writes every record to `data/logs/app.log` **and** to a
  per-source-file log (for example `data/logs/polymarketbot.paper_trading.engine.log`,
  `data/logs/polymarketbot.paper.main.log`). Timestamps are IST.
- All file sinks rotate at `LOG_ROTATION` (default `10 MB`), retain
  `LOG_RETENTION` rotations (default `3`), and compress rotated files. Do not add an
  unrotated log file.
- A long run must remain diagnosable from the logs alone: state, fills, allocations,
  errors, and the periodic equity snapshot go through loguru, not stdout.

### Latency contract (no zero-latency money)

Every action that changes cash, inventory, or order state on the venue pays one measured
round trip before it takes effect. This is not optional:

- `place`, `cancel`, `amend/rebalance`, taker `buy`/`sell` are queued through
  `PendingVenueAction` / `_submit_venue_action` and applied only once
  `now >= submitted_ts + measured RTT`. The RTT is the live `GET /time` measurement
  (`last_clob_rtt_ms`), falling back to the persisted probe p50 in
  `data/derived/local_latency.json`; if neither exists, the action is refused and logged —
  never executed at 0 ms.
- An action lands against the book/state that exists at landing time. Using the snapshot
  that triggered the decision is forbidden.
- A cancel is only complete when it lands: the resting order stays live and fillable for
  the duration of its flight. Fills in that window are real.
- On-chain actions (`merge`, `split`, `redeem`) pay the measured RPC round trip plus the
  measured block interval. If the block interval is not measured, the action is not
  assumed instantaneous.
- Each landed action is logged with submitted→landing seconds and the price/quantity it
  actually executed at.

### The loop

1. **Soak.** Run the live paper trader for exactly 30 minutes. The canonical command,
   used verbatim every cycle (the operator watches its output live):

   ```
   uv run python -m polymarketbot.paper.main --capital 3000 --duration 1800 --interval 10 --fresh --db data/paper_soak_telemetry.db
   ```

   Run this as a **normal foreground command** with a **2000-second timeout** — not as a
   background job, not as a named service, and **not through a PTY**. Use plain
   `uv run python ...`; the harness PTY path is buggy and has hung runs. The dashboard
   streams to the terminal, so the
   operator can check progress directly instead of asking the assistant. Wait for the
   command to return; do not poll with repeated DB/log checks or short sleeps. The script's
   own `--duration 1800` is the timer. This is the one explicitly authorised exception to
   the 600-second invocation cap: it is a daemon with 10-second SQLite snapshots, per-file
   logs, and graceful SIGINT/SIGTERM, so a kill still leaves inspectable progress. Never
   launch it silently; the dashboard is logged every interval and
   `data/paper_soak_telemetry.db` grows every interval.
   - Gas is **measured at startup** from the live Polygon gas oracle (`fast.maxFee`) and
     the live POL/USD ticker; both must resolve or CtfMinter and on-chain settlement refuse
     to run. `--gas-usd` is an override, never a substitute for a measurement.
   - Inspect the live dashboard output directly while it runs. Do not wait 30 minutes to
     discover it produced nothing.
   - On completion, record the window (start/end UTC), starting/ending equity per pillar,
     fills, and any errors from `data/logs/polymarketbot.paper.main.log`.

2. **Audit + fix.** Dispatch **3 subagents in parallel**, one per strategy, each reading the
   **last 30-minute run only** (the exact window from step 1) plus the code path that
   produced it. Do not include older runs; they predate the current fixes and are not
   evidence about the current code.
   - **Reward Harvester Auditor** — liquidity reward allocations, queue priority/decay,
     adverse-selection fills, marginal yield (KKT water-filling), maker rebate credit,
     capital utilisation, banned/hardcoded caps.
   - **Settlement Harvester Auditor** — active horizons, pending par redemptions, taker-fee
     leakage, capital turnover velocity, resolution/hazard-rate logic, mark-to-par equity.
   - **CTF Minter Auditor** — maker parity bundles (`Bid_A + Bid_B < 1.0` after fees/gas),
     fill balance across legs, inventory skew, merge efficiency, sequential vs atomic legs.
   Each auditor must report, with file:line evidence:
   - bugs and unexpected behaviour,
   - detachment from protocol reality (fees, ERC-1155 CTF, UMA resolution states),
   - money left on the table,
   - unoptimised code and unnecessary allocations on hot paths,
   - hardcoded/non-dynamic parameters that should be solved or measured,
   - haphazard design that a top HFT desk (Jump/Citadel) would reject.

   **Auditors own their strategy's implementation.** They are not read-only and must not
   dump findings on the orchestrator. Each auditor implements every fix it finds in its
   own strategy files, runs the narrowest verifying test or bounded smoke, and reports the
   diff plus evidence. There is no rush: the launch gate is Dec 1, and every found issue
   is to be fixed, not deferred. The orchestrator only mediates shared-file/cross-strategy
   conflicts, verifies the integrated result, and restarts the soak.

   **Hard 30-minute limit per auditor.** An auditor that has not returned a complete,
   compiling fix within 30 minutes is treated as irrecoverably stuck (observed: one
   circling a single gate for 43 minutes without applying an edit). The orchestrator
   must then **kill it and finish the slice itself**: read its transcript for whatever
   diagnosis it produced, verify the tree compiles, apply the missing fix, and continue
   the loop. Do not wait for a stalled auditor and do not spawn a replacement for the
   same slice.

3. **Integrate.** Orchestrator checks the three diffs for conflicts, compiles the tree,
   runs a bounded smoke if the changes touch a shared path, records what changed, and
   returns to step 1. Every audit cycle ends with the loop restarted; do not stop after
   the report.

### Operating constraints for the loop

- **Host memory.** A `t3.micro` (930 MB) is **not sufficient** for the full-universe
  3-pillar soak. Even with chunked book loading, spread trimming, compact reward rows and
  allocator trimming, the soak peaks at 441–558 MB while the agent harness holds another
  320–570 MB; a 30-minute run restarted the instance despite the memory guard. Use a
  **>= 2 GB instance** (or the 16 GB local PC) and run the local-India assessment in
  `docs/local-pc-india-analysis.md` before using a high-RTT host for live CTF.
- Keep one compute process at a time; the soak is that process. Auditors read files and the
  SQLite/log outputs, they do not launch duplicate soak runs.
- Never kill the soak blind: check `data/logs/polymarketbot.paper.main.log` and the row
  growth in `data/paper_engine.db` first. If it produced nothing after 300 seconds, stop and
  fix the code.
- State the expected cost of the soak up front: one process, one incremental universe walk
  (20 keyset pages per tick), one rotating 12k-token book window per tick, one trade-tape
  walk, UMA log windows per settlement pass, and one SQLite snapshot per interval. A
  30-minute run at `--interval 10` writes ~180 snapshot rows per pillar.

