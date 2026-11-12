# Sweep: infrastructure, data, venue client, UI, docs (2026-11-11)

Independent sweep for defects that escaped the 2026-11-11 fix pass. Scope:
`src/polymarketbot/{data,tools,ui}/`, `storage.py`, `logs.py`, `config.py`,
`gamma.py`, `pyproject.toml`, `docs/`, `archive/`, and the infra tests.
Already-fixed items from `docs/codebase-quality-report.md` are not repeated.

## Findings

| # | Severity | Location | Defect | Resolution |
|---|---|---|---|---|
| I1 | P1 | `tools/tape_buffer.py:74-101` (`flush_frame`), `tools/crypto_updown_books.py` (`BookBuffer.flush`, `TradeBuffer.flush`) | A failed or interrupted part write lost the whole batch: rows were cleared from memory *before* `to_parquet`, and the file was written in place, so a kill left a truncated part that the next flush quarantined (destroying the rows it held) and a write error dropped the batch silently. | Rows are restored on failure and the exception re-raised; every part is written to a hidden temporary and `os.replace`d. The concat/quarantine logic is now one helper (`flush_frame`) used by all three buffers instead of three divergent copies. |
| I2 | P1 | `tools/tape_buffer.py:45` (`_quarantine`) | A recovered-from part was renamed `minute=NN.corrupt-<ts>.parquet`. Every reader in the repo globs `*.parquet`, so the corrupt evidence was still read and broke the whole table, not just that minute. | Quarantined to `<stem>.corrupt-<ts>.bad` (outside every `*.parquet` glob), warning names the file. |
| I3 | P1 | `tools/crypto_updown_study.py` (all commands) | The pipeline resumes on "destination exists", but every output was written in place (`markets.parquet`, `paths/batch=*.parquet`, `SEEN/date=/hour=.parquet` via DuckDB `COPY`, the four `summary*.json`/`bets.json`/`maker.json`). A kill mid-write leaves a truncated file that the resume check accepts **forever**. | Added `_write_parquet` / `_write_json_text` (temp + `os.replace`) and routed all 13 write sites through them, including DuckDB `COPY` to a temporary. |
| I4 | P1 | `tools/reward_tape.py:216-231` | `while len(shards) < len(chunks): await add_shard(...)` never terminated once the universe needed more chunks than `--max-shards` (600+ tokens at the default 60/shard): `add_shard` refused and returned, the loop spun at 100 % CPU with no output. | Chunks are capped at `--max-shards`, the unrecorded token count is logged, and the CLI rejects `--max-shards`/`--tokens-per-shard` < 1. |
| I5 | P2 | `tools/reward_tape.py:152-162` | `shard` was the literal `0` on every row, so a per-shard silent-WS failure (the documented failure mode) could not be located in the tape. Measured on `data/tape/clob_trades`, 2026-11-11: one distinct value, `0`. | The consumer passes its shard index; the column records it. |
| I6 | P2 | `gamma.py:118-151` | Every non-429 4xx was retried five times with backoff (a 404 cost ~22 s per call), a 429-only exhaustion raised `... failed after 5 attempts: None` (the 429 branch never set `last`), and an unparseable 2xx body was indistinguishable from a transport failure. | Retries are classified: `GammaTransportError` (retried), `GammaHTTPError` (retried for 429/5xx with `Retry-After` clamped to 60 s; raised immediately for other 4xx), `GammaUnparseable` (retried, then raised as itself). All remain `RuntimeError` subclasses so existing callers are unaffected. |
| I7 | P2 | `gamma.py:97-105` | `_throttle` read and wrote `_last_call` without a lock, and one client is shared by the universe walker and metadata refresh; two threads could both see an expired interval and fire together — the burst the venue resets. | Throttle is serialised under a `threading.Lock`. |
| I8 | P2 | `data/universe_metadata.py:209-220` | `refresh()` promised to keep rows for conditions that left the manifest, but `write_rows` replaces the file, so any condition Gamma no longer returns was silently dropped; and the manifest's original spelling plus the parquet's lowercased key made every such condition be fetched twice. | Existing rows are merged into the written table (`setdefault`), and ids are de-duplicated case-insensitively. |
| I9 | P2 | `logs.py:55-66, 124-146, 193-230` | `LOG_DIR` defaulted to a **relative** `data/logs`, so every tool launched from another directory wrote its logs there; and both the aggregate and per-file sinks were created unguarded at import/`get_logger`, so a read-only checkout (or a `PermissionError` from a file another process holds mid-rotation) made the *import* fatal. | A relative `LOG_DIR` is anchored to the repository root; both sink creations catch `OSError`, warn on stderr, and degrade to stderr-only instead of raising. |
| I10 | P2 | `ui/app.py:56-141` | `read_table` swallowed every `sqlite3.Error` (a locked or corrupt journal rendered as "no trades yet"), and `read_lots` inferred "legacy schema" from *any* failed query, so a lock relabelled every live mark as unmeasured. | Reads warn with the file, table and error; `read_lots` asks `PRAGMA table_info` whether `mark_live` exists instead of inferring it from a failure. |
| I11 | P2 | `ui/app.py:165-205, 279` | The headline "Trades"/"Fees paid" were aggregated from the 500-row display window, so they silently undercounted on any journal with more than 500 trades. | `read_totals()` computes journal-wide `COUNT(*)`/`SUM(fee)`; `summarise(..., totals=...)` uses it and keeps the window numbers separately, disclosed in the strategies tab. |
| I12 | P3 | `ui/app.py:26-31` | The default journal path was relative to the Streamlit launcher's cwd. | Anchored to the checkout; `POLYMARKETBOT_PAPER_DB` still overrides. |
| I13 | P2 | `config.py:106-129` | `to_dict(mask_secrets=True)` printed `POLYGON_RPC_URL` verbatim — for a hosted provider that URL *is* the API key — and showed the last four characters of `PRIVATE_KEY`. | The RPC URL's last path segment is redacted; `PRIVATE_KEY` is masked completely. Also logs (debug) when no `.env` is found instead of silently resolving everything from defaults. |
| I14 | P2 | `pyproject.toml:14-38` | `numpy` and `pandas` were imported directly by the tape buffers, the study tools and the dashboard but were not declared; they arrived transitively through `streamlit`/`scipy`, so a resolved environment without either failed at `import polymarketbot.tools.tape_buffer`. | Declared. `uv.lock` updated (also rewrote `revision = 5` → `3`, the revision this machine's uv supports). |
| I15 | P2 | `tools/latency_probe.py:62-96, 149-175` | A failed socket stage was recorded as `0.0 ms` and entered the sample, pulling the reported DNS/TCP/TLS medians toward zero exactly in the runs with the most loss; the socket also leaked on a failed connect/TLS. | Unfinished stages are `None` and excluded from the percentiles (`*_samples` counts what was measured); sockets are closed in a `finally`. |
| I16 | P3 | `tools/latency_probe.py:226-243, 285-291` | The RPC endpoint probed `netloc` (which carries `:port`) as a hostname, so `getaddrinfo` failed and that endpoint's socket stages were always zero; the report was written in place, so a kill left truncated JSON that `load_persisted_rpc_rtt_s` reads. | Host and port are split from the URL; the report is written to a temporary and replaced. |
| I17 | P2 | `storage.py:69-101` | `tune_sqlite_connection` set `auto_vacuum` then ran `VACUUM` unconditionally — inside the caller's open transaction that raises, aborting the tuning halfway with a warning that reads like a transient fault; there was no `busy_timeout`, so a second connection failed instantly instead of waiting. | `busy_timeout` is set; `VACUUM` is skipped (with an explicit warning) when the connection has an open transaction. |
| I18 | P3 | `storage.py:107-120` | `prune_sqlite_table` interpolated caller-supplied table/column names into SQL. | Identifiers are validated against `^[A-Za-z_][A-Za-z0-9_]*$` and a bad one raises `ValueError`. |
| I19 | P3 | `storage.py:166-170` | Pruning ordered by mtime alone, so entries written inside one filesystem timestamp tick (and on coarse-mtime volumes) were deleted in directory-enumeration order — the root cause of the flaky `time.sleep`-based test. | Sort key is `(mtime, name)`; the test sets explicit mtimes instead of sleeping. |
| I20 | P3 | `storage.py:1-13` (docstring) | The module described "production-grade … automated rolling partition pruning" for the whole module, but only `prune_directory` is called by the code (`live/recorder.py`); the other four are operator utilities and the paper ledger never tunes its connection. | Docstring now states which function is on the live path and which are operator calls. |
| I21 | P3 | `data/__init__.py` | Claimed the package provided UMA log decoding, which was archived. | Corrected. |
| I22 | P2 | `docs/*.md` (7 files) | Thirteen documented commands still target modules that do not exist, and three runbooks read as live instructions: `kalshi-runbook` (§1 table + §3 commands for a subsystem deleted from `src/`), `phase-2-universe` (§1 imports `polymarketbot.universe`, gone), `data-inventory`, `kalshi-mapping-active`, `kalshi-mapping-audit`, `live-trading-data-architecture` (§1 lists the deleted collector and monitor as running), `pmxt-runbook` (Step 1's downloader), plus `fees.md` quoting a deleted validator as reproducible. | Each now carries a dated status banner naming the deleted entry points and the real ones (`tools.reward_tape`, `tools.crypto_updown_books`, `paper.main`, `live.runner`); the PMXT step keeps the URL pattern and one honest `curl` example instead of a command that raises. |
| I23 | P3 | `tools/fill_calibration.py` (docstring) | Presented `data/derived/l3_queue` as reproducible from the tape, but its producer was deleted, so the tool can only re-fit an existing tree. | Documented; an empty `--root` still raises rather than writing a zero-observation model. |
| I24 | P3 | `tests/test_storage.py:62-88` | Sleep-based ordering: `time.sleep(0.01)` between 8 mkdirs, which still ties on a coarse-mtime volume. | Explicit `os.utime` stamps. |
## Follow-up round: the six deferred items

| # | Item | Status |
|---|---|---|
| R1 | Journal WAL/busy_timeout (money path) | helpers added in `storage.py`; **call sites are outside this edit set - exact patch below** |
| R2 | Markout staleness | fixed behind an explicit parameter and **re-measured** on the same 4 partitions |
| R3 | `pyarrow.dataset` on `clob_trades` | fixed: `tools/tape_reader.py` unifies per part and names the conflict |
| R4 | `app.log` rotation loss | fixed: rotation-safe sink, a deferred rotation keeps every record and says so once |
| R5 | UI full snapshot read per rerun | fixed: bounded per-strategy series + journal-wide edges |
| R6 | Reward-tape re-scan | fixed: persisted keyset cursor, resumable cycle |

### R1 - journal WAL + busy timeout

`storage.py` now provides the contract, so the call-site change is one line:

* `open_journal_connection(path, *, check_same_thread, busy_timeout_ms=5000, attempts=6)`
  opens with WAL + `synchronous=NORMAL` + `busy_timeout`, verifies `journal_mode`
  really switched (a silent downgrade raises instead of warning), and retries
  **only** a busy/locked open, a bounded number of times.
* `run_with_busy_retry(fn, *, attempts=6, base_delay_s=0.05)` retries only
  `SQLITE_BUSY`/`SQLITE_LOCKED` with capped exponential backoff; every other
  `sqlite3.Error` propagates immediately. `fn` must own its transaction.
* `tune_sqlite_connection(..., strict=True)` re-raises instead of logging.

`WIRING:` (outside this agent's file set - apply exactly):

```diff
--- a/src/polymarketbot/strategy/ledger.py
+++ b/src/polymarketbot/strategy/ledger.py
@@
-        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
+        # WAL + busy timeout: the dashboard reads this file while the engine
+        # writes it, and in rollback-journal mode a reader blocks the writer.
+        self._conn = open_journal_connection(
+            self._db_path, check_same_thread=False
+        )
@@  # imports
+from polymarketbot.storage import open_journal_connection, run_with_busy_retry

--- a/src/polymarketbot/strategy/ledger.py  (def _row, the journal insert)
-        self._conn.execute(
-            "INSERT INTO trades (...) VALUES (?,?,?,?,?,?,?,?,?,?)",
-            (...),
-        )
-        self._conn.commit()
+        def write() -> None:
+            self._conn.execute(
+                "INSERT INTO trades (...) VALUES (?,?,?,?,?,?,?,?,?,?)",
+                (...),
+            )
+            self._conn.commit()
+
+        # A retry is safe: the insert and its commit are one unit, and the
+        # in-memory book is only mutated by the caller after this returns.
+        run_with_busy_retry(write)

--- a/src/polymarketbot/live/store.py  (_init_sqlite)
         cur = self._conn.cursor()
         cur.execute("PRAGMA journal_mode = WAL;")
         cur.execute("PRAGMA synchronous = NORMAL;")
+        cur.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS};")
```

`live/store.py` already sets WAL; it is missing only the explicit
`busy_timeout` (Python's `connect` default is 5 s, so the observable gain there
is small). The paper ledger is the one that needs the change: it opens in
rollback-journal mode, and `_row` commits per insert while the dashboard reads
the same file.

Verified locally: `open_journal_connection` yields `journal_mode=wal` and
`busy_timeout>0`, a read-only connection sees a row committed after it opened
(no reader/writer blocking), a held `BEGIN EXCLUSIVE` makes the open give up in
bounded time, and the retry helper retries only locked errors.

### R2 - markout staleness, re-measured

`measure_markout(..., stale_after_s=DECAY_MAX_QUOTE_AGE_S)` (60 s, the inter-update
gap this module measured for a still-quoting market) replaces the horizon as the
bound on how old the newest book update may be at the fill and at the exit. Each
cell now carries `stale_after_s`, and `definition.stale_after_s` is a number plus
a `stale_after_basis` string. `--stale-after` overrides it; there is no default
that invents a bound.

Re-measured with the same input set as the published artifact
(`--markout --hours 07 --merge`: 4 hour-partitions, 44,068 fills, **158 s**):

| horizon | tick | fills before → after | markout before → after | Δ |
|---:|---:|---:|---:|---:|
| 5 s | 0.001 | 76 → 77 | 0.00080 → 0.00079 | -0.00001 |
| 60 s | 0.01 | 1,606 → 1,606 | 0.00051 → 0.00051 | 0 |
| 225 s | 0.001 | 2,290 → 2,089 | 0.00208 → 0.00228 | +0.00020 |
| 225 s | 0.01 | 5,719 → 5,454 | 0.00125 → 0.00119 | -0.00006 |
| 244 s | 0.01 | 6,133 → 5,710 | 0.00022 → -0.00003 | **-0.00025 (sign change)** |
| 300 s | 0.01 | 7,249 → 6,729 | 0.00069 → 0.00012 | **-0.00057** |
| 400 s | 0.01 | 9,273 → 8,147 | 0.00199 → 0.00121 | **-0.00078 (-39 %)** |

Short horizons (<= 60 s) are unchanged, as expected (the old bound was the
horizon itself, which is <= 60 s there); every horizon above 60 s now drops the
5-12 % of fills whose mark was staler than a live book, and the two longest
horizons move by more than 2 standard errors of their own reported spread. The
long-horizon markout the maker gate prices was therefore **overstated** by the
old bound. Artifact written: `data/derived/ctf_maker_markout.json` (backup of the
previous file kept outside the repo during the run).

### R3 - `clob_trades` cannot be read with `pyarrow.dataset`

Measured on all 638 parts (2026-11-11): `shard` int16/int64, `fee_rate_bps`
double/null, `transaction_hash` large_string/null. Without `partitioning=None`
pyarrow fails on the hive `date` column (`date32` vs `dictionary<string>`); with
it, on `Unsupported cast from double to null`. DuckDB coerces all of it, which is
why the harvester never noticed.

`tools/tape_reader.py` now lists the parts itself, reads each part's footer to
build a unified schema (null yields to the observed type, integer widths widen to
int64, string/large_string to large_string, anything else raises), reads one part
at a time and conforms it, and raises `TapeSchemaError` naming the column, both
types and a sample part. `read_tape` refuses an empty tape instead of returning
zero rows. Wired into the two multi-file reads in `ctf_quote_fill_horizon`
(`_load_sell_prints`, `_condition_legs`).

### R4 - rotation that cannot lose a record

loguru's file sink renames the live file; on Windows that raises
`PermissionError` whenever another process holds it (two bot processes always
hold `app.log`), and the queue thread then discards the record. `logs.py` now
uses `_RotatingFileSink` for both the aggregate and the per-file sinks:

* it owns its handle and rotates on size (`10 MB`, or `LOG_ROTATION` in
  `B/KB/MB/GB`) or on a daily `HH:MM` boundary;
* a rotation closes, retries the rename `ROTATION_ATTEMPTS` (5) times, and if it
  is still refused **keeps appending** to the current file and says so once on
  stderr - the file grows past the threshold until the other process lets go,
  which is visible, but no line is lost;
* the next write retries the deferred rotation, so the file converges;
* rotated parts are compressed (gz, best effort) and pruned to
  `LOG_RETENTION`; an unsupported `LOG_COMPRESSION` is reported and left
  uncompressed rather than silently skipped.

Covered by five tests including the blocked-rename case (all records present in
the directory union, exactly one warning) and retention/gzip.

### R5 - the dashboard no longer reads the whole snapshot table

`read_snapshots` is bounded to `CHART_MAX_POINTS` (320) **per strategy** with a
`ROW_NUMBER() OVER (PARTITION BY strategy ...)` query, so the cost of a rerun is
constant in paper trading run length and adding a pillar does not shorten the history of the
others. The headline comes from `read_snapshot_edges`, two indexed
MIN/MAX-per-strategy joins, so PnL still starts at each strategy's first
snapshot; `summarise(..., edges=...)` uses them. The chart captions the truncation.
`max_points=None` still reads everything for a caller that needs it.

### R6 - reward-tape discovery is resumable

`RewardUniverse` takes `cursor_path` (default
`data/derived/reward_tape_cursor.json`, `--cursor` to move it, `--no-cursor` to
walk from page 1 every cycle). A cycle starts at the saved keyset cursor, runs at
most `--max-pages`, and merges what it found into the persisted set; the set is
replaced only when a cycle reaches the end of the walk, so a market that lost its
campaign is dropped then and not before. Expired markets are dropped on load;
an unreadable cursor falls back to a fresh walk with a warning; the file is
written atomically. `--probe` passes `resume=False` so a one-shot probe neither
consumes nor advances the recorder's cursor.

Measured context: ~6,000 markets per full walk at ~18 s of network per
rediscovery, against the recorder's own request budget.
## Tests added

- `tests/test_tape_buffer.py` (new, 6): per-minute part, appends within a minute,
  unknown assets dropped, a failed write keeps the batch and the retry persists
  it, a corrupt part is quarantined as non-`.parquet` without overwriting, no
  temporary survives.
- `tests/test_tape_reader.py` (new, 7): null-vs-double unification, integer and
  string width widening, partition columns come from the files (not the hive
  directory), a missing column is typed null, an irreconcilable type names the
  column and a part, an empty tape is refused, hour filtering.
- `tests/test_reward_tape.py` (new, 7): partial cycle persists and resumes the
  cursor, a found market survives until a full pass, no cursor means a full walk
  every cycle, `resume=False` ignores the cursor, expired markets are dropped on
  load, an unreadable cursor falls back, filtering.
- `tests/test_logs.py` (new, 8): `LOG_DIR` is absolute and under the checkout,
  repeated `get_logger` does not duplicate sinks, the record carries its `name`,
  rotation splits at the threshold, **a blocked rename loses no record and warns
  once**, retention keeps the newest, gz compression, rotation-spec parsing.
- `tests/test_config.py` (new, 5): full credential masking incl. the RPC key and
  `PRIVATE_KEY`, unmasked dump is literal, `.env` reading does not mutate
  `os.environ`, process env wins, a missing file yields defaults.
- `tests/test_clients.py` (+5): 4xx not retried (one request), 429 exhaustion
  names the status, 5xx retried once, unparseable body raises
  `GammaUnparseable`, transport error retried and raised without the HTTP class.
- `tests/test_storage.py` (+4): WAL + busy timeout and a read-only reader that
  does not block the writer, a held `BEGIN EXCLUSIVE` gives up in bounded time,
  `run_with_busy_retry` retries only locked errors, plus the mtime-stamped
  pruning test.
- `tests/test_ui.py` (+3): journal-wide trade count and fees vs the display
  window, the chart series bounded per strategy, the headline using journal edges
  (and the window-first bug it replaces).
- `tests/test_universe_metadata.py` (+1): a delisted condition's row survives
  `refresh()`, and ids are not fetched twice.

## Verification

- `uv run pytest tests/test_storage.py tests/test_logs.py tests/test_tape_buffer.py
  tests/test_tape_reader.py tests/test_reward_tape.py tests/test_ui.py
  tests/test_universe_metadata.py tests/test_clients.py tests/test_config.py -q`
  -> **all passed** (`-x`, one process).
- `uv run ruff check` on every touched file -> clean.
- `import polymarketbot.{logs,storage,gamma,config,ui.app}` and every
  `polymarketbot.tools.*` module import without pulling in
  `polymarketbot.strategy.reward` (that package is mid-refactor under another
  agent, so it is deliberately not on this sweep's import path); the six tool
  CLIs answer `--help`.
- Bounded data job: the markout re-measure above (158 s, one process, the same
  4 partitions as the published artifact).
- No network calls, no full-suite run, no long data job.

## Remaining work

Only R1's call sites: `strategy/ledger.py` (two hunks) and `live/store.py` (one
pragma) are outside this agent's edit set; the exact patch is in R1 above. No
item is BLOCKED, and no fabricated constant was introduced: the markout bound is
the module's own measured 60 s inter-update gap, the UI bound is a per-strategy
chart-render limit with the journal-wide totals taken from SQL edges, and the
rotation retry is a bounded operational timeout.

## Files changed

`src/polymarketbot/logs.py`, `src/polymarketbot/gamma.py`, `src/polymarketbot/config.py`,
`src/polymarketbot/storage.py`, `src/polymarketbot/ui/app.py`,
`src/polymarketbot/data/__init__.py`, `src/polymarketbot/data/universe_metadata.py`,
`src/polymarketbot/tools/tape_buffer.py`, `src/polymarketbot/tools/crypto_updown_books.py`,
`src/polymarketbot/tools/crypto_updown_study.py`, `src/polymarketbot/tools/reward_tape.py`,
`src/polymarketbot/tools/latency_probe.py`, `src/polymarketbot/tools/fill_calibration.py`,
`src/polymarketbot/tools/ctf_quote_fill_horizon.py`,
`src/polymarketbot/tools/tape_reader.py` (new),
`pyproject.toml`, `uv.lock`, `tests/test_storage.py`, `tests/test_ui.py`,
`tests/test_clients.py`, `tests/test_universe_metadata.py`, `tests/test_tape_buffer.py` (new),
`tests/test_tape_reader.py` (new), `tests/test_reward_tape.py` (new),
`tests/test_logs.py` (new), `tests/test_config.py` (new),
`docs/kalshi-runbook.md`, `docs/kalshi-mapping-active.md`, `docs/kalshi-mapping-audit.md`,
`docs/data-inventory.md`, `docs/phase-2-universe.md`, `docs/pmxt-runbook.md`, `docs/fees.md`,
`docs/live-trading-data-architecture.md`, plus this file.

Data artifact re-measured (not source): `data/derived/ctf_maker_markout.json`.
A new runtime artifact appears on the next recording run:
`data/derived/reward_tape_cursor.json`.
