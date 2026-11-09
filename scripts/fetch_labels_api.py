"""Backfill settlement labels for markets the on-chain snapshot cannot cover.

Why this exists
---------------
`moose-code/condition.parquet` stops at 2026-04. Every market created from
2026-05 onward therefore has `label_source = 'missing'`, and that is the whole
forward-test window. Measured on 2026-11-01:

    created      traded markets   unlabelled    unlabelled volume
    2026-05            230,844       230,844            $6.5 B
    2026-06 -> 08-10   943,693       943,693           $15.1 B
    2026-08-11 -> 10   718,811       718,811            $7.7 B

Without these labels no PnL, Brier score or calibration number in the forward
test can be computed. This script is the fix, and it is the only gate-defining
blocker between Phase 2 and Phase 3.

How
---
`/v2/tokens?token_id=` accepts up to 50 ids per call and returns each token's
`resolved` flag and `final_price` — the settled payout per share, not a traded
price. Tokens are matched to `markets_pit` by id, so slot order is proven
rather than assumed (unlike `outcome_prices`, which is a price and is wrong for
3.4 % of markets).

Resumable: a journal of completed conditions is replayed on start, and the
destination is rewritten from it. Re-running is free; Ctrl-C loses nothing.

Outputs
-------
data/processed/labels_api.parquet
    Same schema as `labels_chain.parquet`, plus `source` and `final_price`.
data/processed/_api_labels.jsonl
    Append-only journal; delete both files to start over.

Validation
----------
`--validate` scores the API labels against on-chain labels in the 2026-04
overlap — the only period where both exist. Agreement there is what licenses
using the API as the label for 2026-05 onward.

Usage
    uv run scripts/fetch_labels_api.py --max-seconds 600 --workers 4
    uv run scripts/fetch_labels_api.py --validate
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, "src")

import duckdb

from polymarketbot.dataapi import DataAPIClient
from polymarketbot.logs import get_logger
log = get_logger(__name__)

OUT = Path("data/processed")
PIT = OUT / "markets_pit.parquet"
CHAIN = OUT / "labels_chain.parquet"
DEST = OUT / "labels_api.parquet"
JOURNAL = OUT / "_api_labels.jsonl"

TOKENS_PER_CALL = 50          # hard API cap
MARKETS_PER_CALL = TOKENS_PER_CALL // 2

_TLS = threading.local()
_LOCK = threading.Lock()


def client() -> DataAPIClient:
    """One client per thread: the throttle is per-client state."""
    c = getattr(_TLS, "client", None)
    if c is None:
        c = DataAPIClient(min_interval_s=0.15, timeout_s=45.0)
        _TLS.client = c
    return c


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET threads = 8")
    return con


def already_done() -> set[str]:
    done: set[str] = set()
    if JOURNAL.exists():
        with JOURNAL.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["condition_id"])
                except Exception:  # noqa: BLE001 - a torn final line is expected
                    continue
    return done


def candidates(con: duckdb.DuckDBPyConnection, done: set[str],
               since: str, limit: int | None) -> list[tuple[str, str, str]]:
    """(condition_id, token1, token2) for traded markets with no on-chain label."""
    rows = con.sql(f"""
        SELECT condition_id, token1, token2
        FROM read_parquet('{PIT.as_posix()}')
        WHERE NOT is_zero_volume
          AND label_source = 'missing'
          AND created_at >= TIMESTAMPTZ '{since}'
          AND token1 IS NOT NULL AND token2 IS NOT NULL
          AND token1 <> '' AND token2 <> ''
        ORDER BY created_at
        {f'LIMIT {limit}' if limit else ''}
    """).fetchall()
    return [r for r in rows if r[0] not in done]


def resolve_batch(batch: list[tuple[str, str, str]]) -> list[dict]:
    """Query one batch of markets and normalise to payout rows."""
    ids: list[str] = []
    for _, t1, t2 in batch:
        ids.extend([t1, t2])
    rows = client().token_references(ids)

    by_token = {str(r.get("token_id")): r for r in rows}
    out: list[dict] = []
    for condition_id, t1, t2 in batch:
        a, b = by_token.get(t1), by_token.get(t2)
        if a is None and b is None:
            # The API does not know this market. Leave it unlabelled rather
            # than inventing a zero; `label_source` must stay honest.
            continue
        resolved = bool((a or {}).get("resolved") or (b or {}).get("resolved"))

        def payout(row: dict | None) -> float | None:
            if row is None:
                return None
            fp = row.get("final_price")
            return None if fp is None else float(fp)

        p0, p1 = payout(a), payout(b)
        n_outcomes = int(bool(a is not None)) + int(bool(b is not None))
        void = (resolved and p0 is not None and p1 is not None
                and abs(p0 - p1) < 1e-9)
        out.append({
            "condition_id": condition_id,
            "resolved": resolved,
            "payout_0": p0,
            "payout_1": p1,
            "is_void": void,
            "n_outcomes": n_outcomes,
            "payout_numerators": None,
            "payout_denominator": None,
            "final_price": p0,
            "source": "api",
        })
    return out


def flush(con: duckdb.DuckDBPyConnection) -> int:
    if not JOURNAL.exists():
        return 0
    con.execute(f"""
        COPY (
            SELECT condition_id, resolved, payout_0, payout_1, is_void,
                   n_outcomes, payout_numerators, payout_denominator,
                   final_price, source
            FROM read_json_auto('{JOURNAL.as_posix()}', format='newline_delimited')
            QUALIFY row_number() OVER (PARTITION BY condition_id) = 1
        ) TO '{DEST.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    return con.sql(f"SELECT count(*) FROM read_parquet('{DEST.as_posix()}')").fetchall()[0][0]


def cross_check(con: duckdb.DuckDBPyConnection, n: int) -> int:
    """Does `/v2/tokens` `final_price` equal the on-chain payout?

    The 2026-04 overlap cannot answer this: the markets chain is *missing* in
    April are exactly the unlabelled ones, so a join finds no rows. The only
    honest test is to fetch API labels for markets chain already knows and
    compare the numbers directly.
    """
    rows = con.sql(f"""
        SELECT p.condition_id, p.token1, p.token2, c.payout_0, c.payout_1,
               c.is_void, c.resolved
        FROM read_parquet('{PIT.as_posix()}') p
        JOIN read_parquet('{CHAIN.as_posix()}') c USING (condition_id)
        WHERE c.resolved AND c.n_outcomes = 2
          AND p.token1 IS NOT NULL AND p.token2 IS NOT NULL
          AND p.token1 <> '' AND p.token2 <> ''
        USING SAMPLE {n} ROWS (reservoir, 42)
    """).fetchall()
    if not rows:
        log.info("no chain-labelled markets to cross-check")
        return 1

    truth = {r[0]: r for r in rows}
    batch = [(r[0], r[1], r[2]) for r in rows]
    got = []
    for i in range(0, len(batch), MARKETS_PER_CALL):
        got.extend(resolve_batch(batch[i:i + MARKETS_PER_CALL]))

    compared = p0_ok = p1_ok = v_ok = missing = 0
    worst = []
    for g in got:
        t = truth.get(g["condition_id"])
        if t is None:
            continue
        if g["payout_0"] is None and g["payout_1"] is None:
            missing += 1
            continue
        compared += 1
        d0 = abs((g["payout_0"] or 0.0) - (t[3] or 0.0))
        d1 = abs((g["payout_1"] or 0.0) - (t[4] or 0.0))
        p0_ok += d0 < 1e-9
        p1_ok += d1 < 1e-9
        v_ok += bool(g["is_void"]) == bool(t[5])
        if max(d0, d1) > 1e-9:
            worst.append((max(d0, d1), g["condition_id"], g["payout_0"], t[3]))

    log.info(f"sampled {len(rows):,} chain-labelled conditions, api answered {compared:,}")
    log.info(f"  api returned no data for {missing:,}")
    if not compared:
        log.info("FAIL: api returned nothing for the sample")
        return 1
    log.info(f"  payout_0 agree  {p0_ok:>6,}  ({100*p0_ok/compared:.2f} %)")
    log.info(f"  payout_1 agree  {p1_ok:>6,}  ({100*p1_ok/compared:.2f} %)")
    log.info(f"  is_void agree   {v_ok:>6,}  ({100*v_ok/compared:.2f} %)")

    worst.sort(reverse=True)
    for err, cid, api, chain in worst[:10]:
        log.info(f"  MISMATCH {err:.4f}  {cid[:24]}… api={api} chain={chain}")

    if p0_ok / compared < 0.99:
        log.info("\nFAIL: below 99 % agreement; api labels cannot stand in for payouts")
        return 1
    log.info("\nPASS: api final_price reproduces on-chain payout")
    return 0


def validate(con: duckdb.DuckDBPyConnection) -> int:
    """Score API labels against on-chain labels where both exist.

    This is the licence for using `final_price` after 2026-04. Without it the
    forward-test labels are an assumption wearing a column name.
    """
    if not DEST.exists():
        log.info(f"missing {DEST}; run the backfill first")
        return 1
    row = con.sql(f"""
        SELECT
            count(*)                                        AS overlap,
            count(*) FILTER (WHERE a.resolved = c.resolved)  AS resolved_agree,
            count(*) FILTER (WHERE abs(a.payout_0 - c.payout_0) < 1e-9) AS p0_agree,
            count(*) FILTER (WHERE abs(a.payout_1 - c.payout_1) < 1e-9) AS p1_agree,
            count(*) FILTER (WHERE a.is_void = c.is_void)    AS void_agree,
            count(*) FILTER (WHERE c.payout_0 IS NOT NULL
                               AND a.payout_0 IS NULL)       AS api_null_chain_known,
            max(abs(a.payout_0 - c.payout_0))                AS max_p0_err
        FROM read_parquet('{DEST.as_posix()}') a
        JOIN read_parquet('{CHAIN.as_posix()}') c USING (condition_id)
        WHERE c.resolved
    """).fetchall()[0]
    overlap, r_ok, p0_ok, p1_ok, v_ok, nulls, max_err = row
    if not overlap:
        log.info("no overlap with on-chain labels — nothing to validate")
        return 0
    log.info(f"overlap with on-chain labels: {overlap:,} resolved conditions")
    log.info(f"  resolved flag   {r_ok:>9,}  ({100*r_ok/overlap:.2f} %)")
    log.info(f"  payout_0        {p0_ok:>9,}  ({100*p0_ok/overlap:.2f} %)   max |err| {max_err}")
    log.info(f"  payout_1        {p1_ok:>9,}  ({100*p1_ok/overlap:.2f} %)")
    log.info(f"  is_void         {v_ok:>9,}  ({100*v_ok/overlap:.2f} %)")
    log.info(f"  api null, chain known: {nulls:,}")

    if p0_ok / overlap < 0.99:
        log.info("\nFAIL: payout agreement below 99 %; do not use api labels for 2026-05+")
        for r in con.sql(f"""
            SELECT condition_id, a.payout_0, c.payout_0, a.payout_1, c.payout_1
            FROM read_parquet('{DEST.as_posix()}') a
            JOIN read_parquet('{CHAIN.as_posix()}') c USING (condition_id)
            WHERE c.resolved AND abs(a.payout_0 - c.payout_0) >= 1e-9
            ORDER BY abs(a.payout_0 - c.payout_0) DESC LIMIT 15
        """).fetchall():
            log.info(f"  {r[0][:20]}… api=({r[1]}, {r[3]}) chain=({r[2]}, {r[4]})")
        return 1
    log.info("\nPASS: api payouts agree with on-chain for >= 99 % of the overlap")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-seconds", type=float, default=0,
                    help="stop after this long (0 = run to completion)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="cap markets this run (0 = all)")
    ap.add_argument("--since", default="2026-04-01",
                    help="only markets created on/after this date")
    ap.add_argument("--flush-every", type=int, default=200,
                    help="rewrite the parquet every N completed markets")
    ap.add_argument("--validate", action="store_true",
                    help="score api labels against on-chain, then exit")
    ap.add_argument("--cross-check", type=int, default=0, metavar="N",
                    help="fetch api labels for N chain-labelled markets and compare payouts")
    args = ap.parse_args()

    if not PIT.exists():
        log.info(f"missing {PIT}; run scripts/build_universe.py first")
        return 1

    con = connect()
    if args.validate:
        return validate(con)
    if args.cross_check:
        return cross_check(con, args.cross_check)

    done = already_done()
    log.info(f"journal: {len(done):,} conditions already done")
    todo = candidates(con, done, args.since, args.limit or None)
    log.info(f"to fetch: {len(todo):,} markets "
          f"({(len(todo) + MARKETS_PER_CALL - 1) // MARKETS_PER_CALL:,} requests)")
    if not todo:
        log.info("nothing to do")
        return flush(con) and 0

    batches = [todo[i:i + MARKETS_PER_CALL] for i in range(0, len(todo), MARKETS_PER_CALL)]
    t0 = time.time()
    written = rows_out = failed = 0

    with JOURNAL.open("a", encoding="utf-8") as jf, \
            ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(resolve_batch, b): b for b in batches}
        try:
            for fut in as_completed(futs):
                try:
                    rows = fut.result()
                except Exception as exc:  # noqa: BLE001 - one bad batch must not stop the run
                    failed += 1
                    if failed <= 5:
                        log.info(f"  batch failed: {exc}")
                    continue
                with _LOCK:
                    for r in rows:
                        jf.write(json.dumps(r) + "\n")
                    rows_out += len(rows)
                written += 1
                if written % args.flush_every == 0:
                    jf.flush()
                    # Rewrite the parquet too: the journal is the source of
                    # truth, but a stale destination file made a 24-minute run
                    # look like it had produced 1,000 rows.
                    flush(con)
                    elapsed = time.time() - t0
                    rate = written / elapsed if elapsed else 0
                    eta = (len(batches) - written) / rate / 60 if rate else 0
                    log.info(f"  {written:,}/{len(batches):,} batches  "
                          f"{rows_out:,} labels  {rate:.1f} batch/s  eta {eta:.1f} min")
                if args.max_seconds and time.time() - t0 >= args.max_seconds:
                    log.info("  time limit reached; cancelling outstanding batches")
                    for f in futs:
                        f.cancel()
                    break
        except KeyboardInterrupt:
            log.info("  interrupted; flushing what completed")
            for f in futs:
                f.cancel()

    jf_total = flush(con)
    log.info(f"\njournal rows in {DEST.name}: {jf_total:,}")
    log.info(f"failed batches: {failed}")
    return validate(con)


if __name__ == "__main__":
    raise SystemExit(main())
