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
