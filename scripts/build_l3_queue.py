"""Replay the CLOB tape into synthetic L3 queue telemetry.

The tape is L2 + trades: a ``book`` snapshot gives the resting size at every
price level, and ``price_change`` frames carry the new aggregate size at a
level, so the queue is observable. ``polymarketbot.data.l3`` replays that into
one row per level update for a hypothetical maker order resting at that price:
``size_at_level``, ``q_ahead``, ``q_behind``, ``queue_ratio``, ``cancel_size``,
``fill_size``, ``replenishment_latency`` and ``survival``. See
``docs/data-collection.md`` for the model and the attribution rule.

Output
------
``data/derived/l3_queue/date=YYYY-MM-DD/hour=HH/asset=<asset_id>.parquet``

One file per asset-hour. An existing file is skipped, so the build is
resumable and an expansion never rewrites finished work.

Usage
-----
    # First run: the latest complete hour, its busiest asset. Prints counts.
    .venv/Scripts/python.exe scripts/build_l3_queue.py

    # A named asset/hour slice.
    .venv/Scripts/python.exe scripts/build_l3_queue.py \
        --date 2026-11-04 --hour 01 --asset 49718783305730493924656122694200034294465263658396511315074782258923543877714

    # Expand: every asset with a book in the hour, in batches.
    .venv/Scripts/python.exe scripts/build_l3_queue.py --date 2026-11-04 --hour 01 --all-assets

A tape hour is one DuckDB scan per batch of assets. Keep ``--assets-per-batch``
modest; the counters show what each pass produced.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "src")

import duckdb

from polymarketbot.data.l3 import L3Session, write_rows
from polymarketbot.logs import IST, get_logger

log = get_logger("build_l3_queue")

EVENTS_TABLE = "clob_events"
TRADES_TABLE = "clob_trades"
DEFAULT_ROOT = Path("data/tape")
DEFAULT_OUT = Path("data/derived/l3_queue")
DEFAULT_ASSETS_PER_BATCH = 25
MAX_SECONDS_DEFAULT = 280.0


# ---------------------------------------------------------------- resolution
def hour_parts(root: Path, table: str) -> dict[str, Path]:
    """``{YYYYMMDDTHH: partition dir}`` for one table of the tape."""
    out: dict[str, Path] = {}
    for day_dir in sorted((root / table).glob("date=*")):
        date = day_dir.name.split("=", 1)[1]
        for hour_dir in sorted(day_dir.glob("hour=*")):
            hour = hour_dir.name.split("=", 1)[1]
            out[f"{date.replace('-', '')}T{hour}"] = hour_dir
    return out


def hour_glob(part: Path) -> str:
    return (part / "*.parquet").as_posix()


def previous_hour(hour: str, back: int = 1) -> str:
    stamp = datetime.strptime(hour, "%Y%m%dT%H").replace(tzinfo=timezone.utc)
    return (stamp - timedelta(hours=back)).strftime("%Y%m%dT%H")


def hour_of(date: str, hour: str) -> str:
    return f"{date.replace('-', '')}T{int(hour):02d}"


def connect(max_threads: int = 2) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET threads = {max_threads}")
    con.execute("SET memory_limit = '4GB'")
    con.execute("SET TimeZone = 'UTC'")
    con.execute("SET preserve_insertion_order = false")
    return con


def sql_list(values: list[str]) -> str:
    for value in values:
        if not value or not all(ch.isalnum() or ch in "-_" for ch in value):
            raise ValueError(f"unsafe literal for SQL: {value!r}")
    return "[" + ", ".join(f"'{value}'" for value in values) + "]"


# ------------------------------------------------------------------- discovery
def discover_assets(con: duckdb.DuckDBPyConnection, events_glob: str) -> list[str]:
    """Assets ranked by how many level updates they saw in the hour."""
    rows = con.execute(
        f"""
        SELECT ch.asset_id, count(*) AS n
        FROM read_parquet('{events_glob}') e, unnest(e.price_changes) AS t(ch)
        WHERE e.event_type = 'price_change'
        GROUP BY 1
        ORDER BY n DESC
        """
    ).fetchall()
    return [row[0] for row in rows if row[0]]


def pick_default_asset(
    con: duckdb.DuckDBPyConnection, events_glob: str, trades_glob: str | None
) -> str | None:
    """The hour's most-traded asset, else its busiest one.

    One asset on the first run should show fills as well as cancels, and an
    asset with no prints in the hour can only ever produce cancels.
    """
    if trades_glob:
        row = con.execute(
            f"SELECT asset_id FROM read_parquet('{trades_glob}') "
            "WHERE asset_id IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
        ).fetchone()
        if row and row[0]:
            return row[0]
    assets = discover_assets(con, events_glob)
    return assets[0] if assets else None


def load_gaps(con: duckdb.DuckDBPyConnection, session: L3Session, events_glob: str) -> int:
    rows = con.execute(
        f"SELECT shard, recv_ts_ns FROM read_parquet('{events_glob}') WHERE event_type = 'gap'"
    ).fetchall()
    for shard, ts in rows:
        session.replayer.add_gap(shard=shard, recv_ts_ns=ts)
    return len(rows)


def seed_books(
    con: duckdb.DuckDBPyConnection,
    session: L3Session,
    roots: dict[str, Path],
    hour: str,
    assets: list[str],
    lookback_hours: int,
) -> dict[str, dict]:
    """Latest book at or before the hour for assets that start mid-stream.

    An asset whose first in-hour event is a ``price_change`` still had a book
    before the hour; without that anchor its deltas are unusable. Any ``gap``
    in the hours walked is registered too, so a reconnect that happened just
    before the hour still unanchors the asset.
    """
    wanted = set(assets)
    seeded: dict[str, dict] = {}
    stamp = int(
        datetime.strptime(hour, "%Y%m%dT%H").replace(tzinfo=timezone.utc).timestamp() * 1e9
    )
    for back in range(1, lookback_hours + 1):
        prev = previous_hour(hour, back)
        part = roots.get(prev)
        targets = [asset for asset in assets if asset in wanted]
        if part is None or not targets:
            break
        rows = con.execute(
            f"""
            SELECT asset_id, recv_ts_ns, bids, asks, shard
            FROM read_parquet('{hour_glob(part)}')
            WHERE event_type = 'book'
              AND list_contains({sql_list(targets)}, asset_id)
              AND recv_ts_ns < {stamp}
            QUALIFY row_number() OVER (PARTITION BY asset_id ORDER BY recv_ts_ns DESC) = 1
            """
        ).fetchall()
        for asset_id, ts, bids, asks, shard in rows:
            if asset_id in wanted:
                seeded[asset_id] = {"recv_ts_ns": ts, "bids": bids, "asks": asks, "shard": shard}
                wanted.discard(asset_id)
        gaps = load_gaps(con, session, hour_glob(part))
        if gaps:
            log.info("registered {} gap(s) from {}", gaps, prev)
    log.info("seeded {} of {} assets from before the hour", len(seeded), len(assets))
    return seeded


# ---------------------------------------------------------------------- replay
def build_batch(
    con: duckdb.DuckDBPyConnection,
    *,
    events_glob: str,
    trades_glob: str | None,
    assets: list[str],
    out_dir: Path,
    session: L3Session,
    force: bool,
) -> dict[str, int]:
    """Replay one batch of assets, writing one Parquet per asset-hour."""
    todo = [
        asset
        for asset in assets
        if force or not (out_dir / f"asset={asset}.parquet").exists()
    ]
    stats = {"assets": 0, "skipped": len(assets) - len(todo), "rows": 0}
    if not todo:
        return stats

    asset_list = sql_list(todo)
    trades_branch = ""
    if trades_glob:
        trades_branch = f"""
        UNION ALL
        SELECT asset_id, recv_ts_ns, recv_mono_ns, 'trade', NULL, NULL, NULL, NULL,
               side, price, size
        FROM read_parquet('{trades_glob}')
        WHERE list_contains({asset_list}, asset_id)
        """
    query = f"""
        WITH kept AS (
            SELECT recv_ts_ns, recv_mono_ns, event_type, asset_id, shard,
                   bids, asks, price_changes, side, price, size
            FROM read_parquet('{events_glob}')
            WHERE (event_type = 'book' AND list_contains({asset_list}, asset_id))
               OR (event_type = 'price_change'
                   AND len(list_filter(price_changes, lambda c: list_contains({asset_list}, c.asset_id))) > 0)
        )
        SELECT asset_id, recv_ts_ns, recv_mono_ns, 'book' AS kind, shard,
               bids, asks,
               CAST(NULL AS STRUCT(asset_id VARCHAR, price DOUBLE, size DOUBLE,
                                   side VARCHAR, best_bid DOUBLE, best_ask DOUBLE)[]) AS changes,
               CAST(NULL AS VARCHAR) AS side, CAST(NULL AS DOUBLE) AS price,
               CAST(NULL AS DOUBLE) AS size
        FROM kept WHERE event_type = 'book'
        UNION ALL
        SELECT c.asset_id, recv_ts_ns, recv_mono_ns, 'price_change', shard, NULL, NULL,
               [c], NULL, NULL, NULL
        FROM kept, unnest(list_filter(price_changes, lambda c: list_contains({asset_list}, c.asset_id))) AS t(c)
        WHERE event_type = 'price_change'
        {trades_branch}
        ORDER BY 1, 2, 3
    """
    cursor = con.execute(query)

    def rows():
        while True:
            chunk = cursor.fetchmany(20_000)
            if not chunk:
                return
            for asset_id, ts, mono, kind, shard, bids, asks, changes, side, price, size in chunk:
                yield {
                    "kind": kind,
                    "asset_id": asset_id,
                    "recv_ts_ns": ts,
                    "recv_mono_ns": mono,
                    "shard": shard,
                    "bids": bids,
                    "asks": asks,
                    "changes": changes,
                    "side": side,
                    "price": price,
                    "size": size,
                }

    out_dir.mkdir(parents=True, exist_ok=True)
    current: str | None = None
    buffered: list[dict] = []
    for row in session.feed(rows()):
        asset_id = row["asset_id"]
        if asset_id != current:
            if current is not None:
                stats["rows"] += write_rows(out_dir / f"asset={current}.parquet", buffered)
                stats["assets"] += 1
            current, buffered = asset_id, []
        buffered.append(row)
    if current is not None:
        stats["rows"] += write_rows(out_dir / f"asset={current}.parquet", buffered)
        stats["assets"] += 1
    return stats


# ------------------------------------------------------------------------ main
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="tape root (read-only)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="derived output root")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: latest hour on disk)")
    ap.add_argument("--hour", default=None, help="HH of the UTC hour to replay")
    ap.add_argument("--asset", action="append", default=[], help="token id; repeatable")
    ap.add_argument("--all-assets", action="store_true", help="every asset with a book in the hour")
    ap.add_argument("--assets-per-batch", type=int, default=DEFAULT_ASSETS_PER_BATCH)
    ap.add_argument("--max-assets", type=int, default=0, help="cap for a staged expansion")
    ap.add_argument("--lookback-hours", type=int, default=3, help="hours searched for an anchor book")
    ap.add_argument("--match-window-ms", type=float, default=500.0)
    ap.add_argument("--force", action="store_true", help="overwrite existing asset-hour files")
    ap.add_argument("--max-seconds", type=float, default=MAX_SECONDS_DEFAULT)
    ap.add_argument("--threads", type=int, default=2)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.time()
    root = Path(args.root)
    out_root = Path(args.out)

    events_parts = hour_parts(root, EVENTS_TABLE)
    trades_parts = hour_parts(root, TRADES_TABLE)
    if not events_parts:
        log.error("no {} partitions under {}", EVENTS_TABLE, root)
        return 2

    if args.date and args.hour:
        hour = hour_of(args.date, args.hour)
    elif args.hour:
        hour = max(
            (stamp for stamp in events_parts if stamp.endswith(f"T{int(args.hour):02d}")),
            default=None,
        )
    else:
        # The live collector is still writing the current hour; take the last
        # complete one so the slice is stable.
        now = datetime.now(IST).strftime("%Y%m%dT%H")
        complete = [stamp for stamp in events_parts if stamp < now]
        hour = max(complete or events_parts)
    if hour not in events_parts:
        log.error("hour {} has no clob_events partition (have: {})", hour, sorted(events_parts)[-3:])
        return 2

    part = events_parts[hour]
    events_glob = hour_glob(part)
    trades_glob = hour_glob(trades_parts[hour]) if hour in trades_parts else None
    if trades_glob is None:
        log.warning("no clob_trades partition for {}; every decrease will read as a cancel", hour)
    date = hour[:4] + "-" + hour[4:6] + "-" + hour[6:8]
    out_dir = out_root / f"date={date}" / f"hour={hour[9:11]}"
    log.info("replaying {} from {} -> {}", hour, part, out_dir)

    con = connect(args.threads)
    session = L3Session()
    session.replayer.match_window_ns = int(args.match_window_ms * 1e6)

    if args.asset:
        assets = args.asset
    elif args.all_assets:
        assets = discover_assets(con, events_glob)
    else:
        picked = pick_default_asset(con, events_glob, trades_glob)
        if picked is None:
            log.error("no level updates or trades in {}; nothing to replay", hour)
            return 2
        assets = [picked]
    if args.max_assets > 0:
        assets = assets[: args.max_assets]
    log.info("targets: {} asset(s)", len(assets))

    seeds = seed_books(con, session, events_parts, hour, assets, args.lookback_hours)
    for asset, seed in seeds.items():
        # Anchor state only: rows for the earlier hour belong to its own file.
        session.replayer.on_book(
            asset_id=asset,
            recv_ts_ns=int(seed["recv_ts_ns"]),
            bids=seed["bids"],
            asks=seed["asks"],
            shard=seed["shard"],
        )
    # Seeding produced no output rows; start the reported counters at zero.
    session.replayer.reset_counters()

    gaps = load_gaps(con, session, events_glob)
    log.info("registered {} gap(s) in-hour", gaps)

    totals = {"assets": 0, "skipped": 0, "rows": 0, "batches": 0}
    batch_size = max(1, args.assets_per_batch)
    index = 0
    while index < len(assets):
        if time.time() - started > args.max_seconds:
            log.warning(
                "stopping after {:.0f}s with {} asset(s) left; rerun to continue",
                time.time() - started,
                len(assets) - index,
            )
            break
        batch = assets[index : index + batch_size]
        index += batch_size
        stats = build_batch(
            con,
            events_glob=events_glob,
            trades_glob=trades_glob,
            assets=batch,
            out_dir=out_dir,
            session=session,
            force=args.force,
        )
        totals["batches"] += 1
        for key in ("assets", "skipped", "rows"):
            totals[key] += stats[key]
        log.info(
            "batch {}: assets {} rows {} (skipped {}) | {}",
            totals["batches"],
            stats["assets"],
            stats["rows"],
            stats["skipped"],
            json.dumps(session.replayer.counters.as_dict()),
        )

    con.close()
    counters = session.replayer.counters.as_dict()
    summary = {
        "hour": hour,
        "output": str(out_dir),
        "elapsed_s": round(time.time() - started, 1),
        **totals,
        **counters,
        "ambiguity_flags": counters["ambiguous"],
        "rows_per_asset": round(totals["rows"] / totals["assets"], 1) if totals["assets"] else 0.0,
    }
    log.info("summary {}", json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
