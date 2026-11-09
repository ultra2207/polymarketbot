#!/usr/bin/env python3
"""Validate the raw CLOB tape: table summary and one-asset replay.

Two modes, both read-only:

``--summary``
    One aggregate per table under ``--root``: Parquet part count, row count,
    distinct assets, receive-time range, and how many ``event_type="gap"``
    rows the tape contains. The aggregate runs inside DuckDB against
    ``read_parquet(..., union_by_name=true)``; rows are never materialised.

``--asset <id> --hour YYYYMMDDTHH``
    Replay one asset's hour of ``clob_events`` in receive order. A ``book``
    event replaces the state, a ``price_change`` sets the touched level to
    its new aggregate size (size 0 removes it), then the final best bid/ask,
    level counts, snapshot count and gap count are reported.

``--rest-check`` additionally fetches ``GET /clob.polymarket.com/book`` for
the replayed token and prints the diff. It is a report, never a failure:
the tape can legitimately be older than the REST snapshot.

Usage
-----
    .venv/Scripts/python.exe scripts/validate_tape.py --summary --root data/tape
    .venv/Scripts/python.exe scripts/validate_tape.py --root data/tape \\
        --asset 1234... --hour 20261104T12 --limit 200000
    .venv/Scripts/python.exe scripts/validate_tape.py ... --rest-check
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import duckdb  # noqa: E402
import requests  # noqa: E402

from polymarketbot.logs import IST, get_logger  # noqa: E402
from polymarketbot.logs import get_logger
log = get_logger(__name__)

CLOB_BOOK_URL = "https://clob.polymarket.com/book"
DEFAULT_TABLE = "clob_events"
DEFAULT_LIMIT = 100_000


# ------------------------------------------------------------------- duckdb io
def _sql_path(path: str | Path) -> str:
    """Forward-slash the path so Windows separators are not SQL escapes."""
    return str(path).replace("\\", "/").replace("'", "''")


def parquet_parts(root: Path, table: str) -> list[Path]:
    base = root / table
    if not base.is_dir():
        return []
    return sorted(base.glob("date=*/hour=*/*.parquet"))


def table_glob(root: Path, table: str) -> str:
    return _sql_path(root / table / "date=*" / "hour=*" / "*.parquet")


def hour_parquet_parts(root: Path, table: str, hour: str) -> list[Path]:
    """``YYYYMMDDTHH`` -> the exact ``date=/hour=`` partition directory."""
    if len(hour) != 11 or hour[8] != "T" or not hour[:8].isdigit() or not hour[9:11].isdigit():
        raise ValueError(f"hour must be YYYYMMDDTHH, got {hour!r}")
    date_part = f"{hour[:4]}-{hour[4:6]}-{hour[6:8]}"
    hour_part = hour[9:11]
    directory = root / table / f"date={date_part}" / f"hour={hour_part}"
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.parquet"))


def table_columns(con: duckdb.DuckDBPyConnection, glob: str) -> set[str]:
    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{glob}', union_by_name=true)"
    ).fetchall()
    return {str(row[0]) for row in rows}


def list_tables(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(
        child.name
        for child in root.iterdir()
        if child.is_dir() and child.name != "_wal" and parquet_parts(root, child.name)
    )


def summarize_table(con: duckdb.DuckDBPyConnection, root: Path, table: str) -> dict[str, Any]:
    """Aggregate one table without pulling rows into Python."""
    parts = parquet_parts(root, table)
    if not parts:
        return {
            "table": table,
            "parts": 0,
            "rows": 0,
            "assets": None,
            "min_ts": None,
            "max_ts": None,
            "gaps": None,
        }
    glob = table_glob(root, table)
    columns = table_columns(con, glob)

    selects = ["count(*) AS rows"]
    if "asset_id" in columns:
        selects.append("count(DISTINCT asset_id) AS assets")
    else:
        selects.append("CAST(NULL AS BIGINT) AS assets")
    if "recv_ts_ns" in columns:
        selects.extend(["min(recv_ts_ns) AS min_ts", "max(recv_ts_ns) AS max_ts"])
    else:
        selects.extend(["CAST(NULL AS BIGINT) AS min_ts", "CAST(NULL AS BIGINT) AS max_ts"])
    if "event_type" in columns:
        selects.append("sum(CASE WHEN event_type = 'gap' THEN 1 ELSE 0 END) AS gaps")
    else:
        selects.append("CAST(NULL AS BIGINT) AS gaps")

    row = con.execute(
        f"SELECT {', '.join(selects)} FROM read_parquet('{glob}', union_by_name=true)"
    ).fetchone()
    return {
        "table": table,
        "parts": len(parts),
        "rows": int(row[0]),
        "assets": None if row[1] is None else int(row[1]),
        "min_ts": None if row[2] is None else int(row[2]),
        "max_ts": None if row[3] is None else int(row[3]),
        "gaps": None if row[4] is None else int(row[4]),
    }


def summarize(
    con: duckdb.DuckDBPyConnection, root: Path, table: str | None = None
) -> list[dict[str, Any]]:
    tables = [table] if table else list_tables(root)
    return [summarize_table(con, root, name) for name in tables]


# --------------------------------------------------------------------- replay
@dataclass
class BookState:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    snapshots: int = 0
    gaps: int = 0
    changes_applied: int = 0
    changes_ignored: int = 0
    changes_other_asset: int = 0
    event_types: Counter = field(default_factory=Counter)

    def apply_book(self, bids: Iterable[Any] | None, asks: Iterable[Any] | None) -> None:
        self.bids = _levels(bids)
        self.asks = _levels(asks)
        self.snapshots += 1

    def apply_price_changes(self, changes: Iterable[Any] | None, asset_id: str | None = None) -> None:
        for change in changes or []:
            if not isinstance(change, dict):
                self.changes_ignored += 1
                continue
            change_asset = change.get("asset_id")
            if asset_id and change_asset and str(change_asset) != str(asset_id):
                # A `price_change` frame can carry both outcome tokens.
                self.changes_other_asset += 1
                continue
            price = _as_float(change.get("price"))
            side = str(change.get("side") or "").upper()
            book = self.bids if side == "BUY" else self.asks if side == "SELL" else None
            if price is None or book is None:
                self.changes_ignored += 1
                continue
            size = _as_float(change.get("size"))
            if size is None or size <= 0:
                book.pop(price, None)
            else:
                book[price] = size
            self.changes_applied += 1

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _levels(values: Iterable[Any] | None) -> dict[float, float]:
    """Arrow/DuckDB level lists -> ``{price: size}`` with zero sizes dropped."""
    out: dict[float, float] = {}
    for level in values or []:
        if isinstance(level, dict):
            price, size = level.get("price"), level.get("size")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, size = level[0], level[1]
        else:
            continue
        p, s = _as_float(price), _as_float(size)
        if p is None:
            continue
        if s is None or s <= 0:
            out.pop(p, None)
        else:
            out[p] = s
    return out


def load_events(
    con: duckdb.DuckDBPyConnection,
    root: Path,
    *,
    asset: str,
    hour: str,
    table: str = DEFAULT_TABLE,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """One asset/hour, receive-ordered, bounded by ``limit``."""
    parts = hour_parquet_parts(root, table, hour)
    if not parts:
        raise FileNotFoundError(
            f"no parquet parts for table={table} hour={hour} under {root / table}"
        )
    glob = _sql_path(parts[0].parent / "*.parquet")
    columns = table_columns(con, glob)
    required = {"recv_ts_ns", "asset_id", "event_type", "bids", "asks", "price_changes"}
    missing = required - columns
    if missing:
        raise ValueError(f"table {table} is missing columns: {sorted(missing)}")

    order = "recv_ts_ns, recv_mono_ns" if "recv_mono_ns" in columns else "recv_ts_ns"
    selected = [
        name
        for name in ("recv_ts_ns", "recv_mono_ns", "asset_id", "event_type", "bids", "asks", "price_changes")
        if name in columns
    ]
    base = (
        f"SELECT {', '.join(selected)} FROM read_parquet("
        f"'{glob}', union_by_name=true) "
    )
    # A mixed `price_change` frame has a null row-level asset, so the target
    # token's changes are only reachable through the nested change list.
    sql = (
        base
        + "WHERE asset_id = ? OR EXISTS ("
        "SELECT 1 FROM unnest(price_changes) AS t(x) WHERE x.asset_id = ?) "
        + f"ORDER BY {order} ASC LIMIT ?"
    )
    try:
        result = con.execute(sql, [asset, asset, int(limit)])
    except duckdb.Error:
        # Older parts predate the per-change asset_id field.
        sql = base + "WHERE asset_id = ? " + f"ORDER BY {order} ASC LIMIT ?"
        result = con.execute(sql, [asset, int(limit)])
    names = [d[0] for d in result.description]
    return [dict(zip(names, row)) for row in result.fetchall()]


def replay(events: Iterable[dict[str, Any]], asset_id: str | None = None) -> BookState:
    """Apply book/price_change/gap events in the order given."""
    state = BookState()
    for event in events:
        event_type = str(event.get("event_type") or "")
        state.event_types[event_type] += 1
        if event_type == "book":
            state.apply_book(event.get("bids"), event.get("asks"))
        elif event_type == "price_change":
            state.apply_price_changes(event.get("price_changes"), asset_id)
        elif event_type == "gap":
            state.gaps += 1
    return state


# ---------------------------------------------------------------- rest compare
def compare_books(state: BookState, payload: dict[str, Any]) -> list[str]:
    """Return human-readable differences; never raises on a mismatch."""
    if not isinstance(payload, dict):
        return ["REST payload is not an object"]
    rest_bids = _levels(payload.get("bids"))
    rest_asks = _levels(payload.get("asks"))
    findings: list[str] = []
    rest_best_bid = max(rest_bids) if rest_bids else None
    rest_best_ask = min(rest_asks) if rest_asks else None

    if state.best_bid != rest_best_bid:
        findings.append(f"best_bid tape={state.best_bid} rest={rest_best_bid}")
    if state.best_ask != rest_best_ask:
        findings.append(f"best_ask tape={state.best_ask} rest={rest_best_ask}")
    if len(state.bids) != len(rest_bids):
        findings.append(f"bid levels tape={len(state.bids)} rest={len(rest_bids)}")
    if len(state.asks) != len(rest_asks):
        findings.append(f"ask levels tape={len(state.asks)} rest={len(rest_asks)}")
    return findings


def fetch_rest_book(asset: str, timeout_s: float = 30.0) -> dict[str, Any]:
    from polymarketbot.net import install_doh_fallback

    install_doh_fallback()
    response = requests.get(CLOB_BOOK_URL, params={"token_id": asset}, timeout=timeout_s)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("REST book response is not an object")
    return payload


# ----------------------------------------------------------------------- print
def _fmt_ts(ns: int | None) -> str:
    if ns is None:
        return "-"
    return datetime.fromtimestamp(ns / 1e9, tz=IST).strftime("%Y-%m-%d %H:%M:%S")


def print_summary(rows: list[dict[str, Any]], root: Path) -> None:
    if not rows:
        log.info(f"no parquet tables under {root}")
        return
    headers = ["table", "parts", "rows", "assets", "min recv (UTC)", "max recv (UTC)", "gaps"]
    body = [
        [
            str(row["table"]),
            f"{row['parts']:,}",
            f"{row['rows']:,}",
            "-" if row["assets"] is None else f"{row['assets']:,}",
            _fmt_ts(row["min_ts"]),
            _fmt_ts(row["max_ts"]),
            "-" if row["gaps"] is None else f"{row['gaps']:,}",
        ]
        for row in rows
    ]
    widths = [max(len(h), *(len(line[i]) for line in body)) for i, h in enumerate(headers)]
    log.info("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    log.info("  ".join("-" * width for width in widths))
    for line in body:
        log.info("  ".join(value.ljust(widths[i]) for i, value in enumerate(line)))


def print_replay(
    *,
    asset: str,
    hour: str,
    table: str,
    limit: int,
    events: list[dict[str, Any]],
    state: BookState,
) -> None:
    log.info(f"table        {table}")
    log.info(f"asset        {asset}")
    log.info(f"hour         {hour}")
    log.info(f"rows read    {len(events):,} (limit {limit:,})")
    log.info(f"snapshots    {state.snapshots:,} book events")
    log.info(
        f"changes      {state.changes_applied:,} applied, {state.changes_ignored:,} ignored, "
        f"{state.changes_other_asset:,} for the complement token"
    )
    log.info(
        "event types  "
        + ", ".join(f"{name or '<null>'}={count:,}" for name, count in state.event_types.most_common())
    )
    log.info(f"best bid     {state.best_bid if state.best_bid is not None else '-'}  ({len(state.bids)} levels)")
    log.info(f"best ask     {state.best_ask if state.best_ask is not None else '-'}  ({len(state.asks)} levels)")
    if (
        state.best_bid is not None
        and state.best_ask is not None
        and state.best_bid >= state.best_ask
    ):
        log.info(
            "warning      crossed book (best bid >= best ask); a price_change row may "
            "carry both outcome tokens' changes"
        )
    log.info(f"gaps         {state.gaps:,}")


def print_rest_check(state: BookState, payload: dict[str, Any], findings: list[str]) -> None:
    rest_bids = _levels(payload.get("bids"))
    rest_asks = _levels(payload.get("asks"))
    log.info("rest check")
    log.info(f"  rest best bid  {max(rest_bids) if rest_bids else '-'}  ({len(rest_bids)} levels)")
    log.info(f"  rest best ask  {min(rest_asks) if rest_asks else '-'}  ({len(rest_asks)} levels)")
    if findings:
        for finding in findings:
            log.info(f"  DIFF {finding}")
    else:
        log.info("  match")


# ------------------------------------------------------------------------- cli
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Summarize or replay the raw CLOB tape.")
    ap.add_argument("--root", default="data/tape", help="tape root (default: data/tape)")
    ap.add_argument(
        "--table",
        default=None,
        help=f"restrict to one table; replay defaults to {DEFAULT_TABLE}",
    )
    ap.add_argument("--asset", default=None, help="outcome token id to replay")
    ap.add_argument("--hour", default=None, help="replay hour as YYYYMMDDTHH (UTC)")
    ap.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"max replay rows (default: {DEFAULT_LIMIT:,})",
    )
    ap.add_argument("--summary", action="store_true", help="print a per-table aggregate")
    ap.add_argument(
        "--rest-check",
        action="store_true",
        help="compare the replayed book to GET /book (report only)",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    log = get_logger("validate_tape")
    root = Path(args.root)

    con = duckdb.connect()
    try:
        con.execute("SET threads=2")
        if args.summary:
            rows = summarize(con, root, args.table)
            print_summary(rows, root)
            return 0

        table = args.table or DEFAULT_TABLE
        if not args.asset or not args.hour:
            log.error("replay needs --asset and --hour (YYYYMMDDTHH), or pass --summary", )
            return 2
        if args.limit <= 0:
            log.error("--limit must be positive", )
            return 2

        events = load_events(
            con,
            root,
            asset=args.asset,
            hour=args.hour,
            table=table,
            limit=args.limit,
        )
        state = replay(events, args.asset)
        print_replay(
            asset=args.asset,
            hour=args.hour,
            table=table,
            limit=args.limit,
            events=events,
            state=state,
        )

        if args.rest_check:
            try:
                payload = fetch_rest_book(args.asset)
                print_rest_check(state, payload, compare_books(state, payload))
            except Exception as exc:  # noqa: BLE001 - report only, never fail
                log.warning(f"REST book check failed for {args.asset}: {exc}")
                log.info(f"rest check failed: {exc}")
        return 0
    except (FileNotFoundError, ValueError, duckdb.Error) as exc:
        log.error(f"tape validation failed: {exc}")
        log.error(f"error: {exc}", )
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
