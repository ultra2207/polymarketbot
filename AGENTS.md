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

