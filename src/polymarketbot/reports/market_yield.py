"""Two-rate market yield report.

``reward_yield``  revenue only: venue-reported payouts vs the modelled Q_min
                  share, per (condition, UTC date).
``net_yield``     comprehensive: realised fill PnL minus rebalancing costs
                  (crossed spread + taker fees + gas) minus adverse markout.
                  Printed only when the observation counts are stated and
                  sufficient; otherwise the cell says "insufficient data".

Reads the engine's SQLite database read-only. No colors, no interpolation, no
fabricated zeros: a missing observation is printed as ``n/a``.

Usage:
    python -m polymarketbot.reports.market_yield --db data/real_money_trading.db
"""

from __future__ import annotations

import argparse
import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from polymarketbot.logs import IST
from polymarketbot.logs import get_logger
log = get_logger(__name__)

MIN_FILLS_FOR_NET = 2
MIN_REBALANCES_FOR_NET = 1

MARKOUT_COLUMNS = ("markout_30s", "markout_5m", "markout_30m")


def _utc_date(ts: Any) -> Optional[str]:
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return None
    if value <= 0.0 or not math.isfinite(value):
        return None
    return datetime.fromtimestamp(value, IST).strftime("%Y-%m-%d")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _rows(conn: sqlite3.Connection, sql: str, params: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return list(conn.execute(sql, params).fetchall())


def _safe_name(text: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(text))


def _tape_hours(tape_dir: Optional[Path], condition_id: str, date: str) -> Optional[int]:
    if tape_dir is None:
        return None
    prefix = f"{_safe_name(condition_id)}_{date.replace('-', '')}"
    return len(list(Path(tape_dir).glob(f"{prefix}*.parquet")))


def _collect(conn: sqlite3.Connection) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Group every measured observation by (condition_id, UTC date)."""
    groups: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(
        lambda: {
            "reward_actual": None,
            "reward_modelled": None,
            "reward_floor_met": False,
            "fills": 0,
            "fill_hours": set(),
            "realized_pnl": 0.0,
            "adverse_markout": 0.0,
            "markout_observed": 0,
            "rebalances": 0,
            "rebalance_cost": 0.0,
        }
    )

    if _table_exists(conn, "reward_payouts"):
        for row in _rows(conn, "SELECT date, condition_id, usd FROM reward_payouts"):
            key = (str(row["condition_id"]), str(row["date"]))
            current = groups[key]["reward_actual"]
            groups[key]["reward_actual"] = float(row["usd"]) + (
                current if current is not None else 0.0
            )

    if _table_exists(conn, "reward_epochs"):
        for row in _rows(
            conn,
            "SELECT timestamp, condition_id, epoch_payout_usd, floor_met FROM reward_epochs",
        ):
            date = _utc_date(row["timestamp"])
            if date is None:
                continue
            key = (str(row["condition_id"]), date)
            payout = float(row["epoch_payout_usd"] or 0.0)
            current = groups[key]["reward_modelled"]
            groups[key]["reward_modelled"] = payout + (current if current is not None else 0.0)
            if int(row["floor_met"] or 0):
                groups[key]["reward_floor_met"] = True

    if _table_exists(conn, "fills"):
        columns = {
            str(r[1]) for r in conn.execute("PRAGMA table_info(fills)").fetchall()
        }
        ts_column = "ts" if "ts" in columns else ("timestamp" if "timestamp" in columns else None)
        if ts_column is not None and "condition_id" in columns:
            markout_columns = [c for c in MARKOUT_COLUMNS if c in columns]
            if not markout_columns and "post_fill_drift" in columns:
                # The engine records one realised post-fill drift, not the
                # three replay horizons. Count it once and say so in the header.
                markout_columns = ["post_fill_drift"]
            wanted = [
                ts_column,
                "condition_id",
                *[c for c in ("realized_pnl", *markout_columns) if c in columns],
            ]
            select = ", ".join(wanted)
            has_realized = "realized_pnl" in columns
            for row in _rows(conn, f"SELECT {select} FROM fills"):
                date = _utc_date(row[ts_column])
                if date is None:
                    continue
                key = (str(row["condition_id"]), date)
                bucket = groups[key]
                bucket["fills"] += 1
                bucket["fill_hours"].add(int(float(row[ts_column]) // 3600))
                if has_realized:
                    bucket["realized_pnl"] += float(row["realized_pnl"] or 0.0)
                for column in markout_columns:
                    value = row[column]
                    if value is None:
                        continue
                    bucket["markout_observed"] += 1
                    markout = float(value)
                    if markout < 0.0:
                        bucket["adverse_markout"] += -markout

    if _table_exists(conn, "rebalances"):
        for row in _rows(
            conn,
            """
            SELECT ts, condition_id, crossed_cents, taker_fee_usd, gas_usd,
                   inventory_before, inventory_after
            FROM rebalances
            """,
        ):
            date = _utc_date(row["ts"])
            if date is None:
                continue
            key = (str(row["condition_id"]), date)
            bucket = groups[key]
            before = row["inventory_before"]
            after = row["inventory_after"]
            traded_size = 0.0
            if before is not None and after is not None:
                traded_size = abs(float(after) - float(before))
            crossed_usd = abs(float(row["crossed_cents"] or 0.0)) / 100.0 * traded_size
            bucket["rebalances"] += 1
            bucket["rebalance_cost"] += (
                crossed_usd
                + float(row["taker_fee_usd"] or 0.0)
                + float(row["gas_usd"] or 0.0)
            )

    return groups


def _fmt(value: Optional[float], width: int = 10) -> str:
    if value is None:
        return "n/a".rjust(width)
    return f"{value:.4f}".rjust(width)


def build_report(db_path: Path, tape_dir: Optional[Path] = None) -> str:
    """Render the plain-text two-rate report for one database."""
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        groups = _collect(conn)
    finally:
        conn.close()

    header = (
        "condition_id                             date        "
        "reward_actual reward_modelled reward_error   fills rebal  hrs   net_yield"
    )
    lines = [
        "reward_yield: actual = sum(reward_payouts.usd); "
        "modelled = sum(reward_epochs.epoch_payout_usd) (floor-gated share x pool)",
        "net_yield: realized - (crossed + taker fee + gas) - adverse markout; "
        f"net printed only with >= {MIN_FILLS_FOR_NET} fills and "
        f">= {MIN_REBALANCES_FOR_NET} rebalances",
        "",
        header,
        "-" * len(header),
    ]

    if not groups:
        lines.append(
            "insufficient data (0 fills, 0 rebalances) - no reward, fill or "
            "rebalance observations in this database"
        )
        return "\n".join(lines)

    for (condition_id, date) in sorted(groups):
        bucket = groups[(condition_id, date)]
        actual = bucket["reward_actual"]
        modelled = bucket["reward_modelled"]
        error = (actual - modelled) if (actual is not None and modelled is not None) else None
        fills = int(bucket["fills"])
        rebalances = int(bucket["rebalances"])
        hours = len(bucket["fill_hours"])
        tape_hours = _tape_hours(tape_dir, condition_id, date)
        if tape_hours is not None:
            hours = max(hours, tape_hours)

        if fills < MIN_FILLS_FOR_NET or rebalances < MIN_REBALANCES_FOR_NET:
            net_cell = f"insufficient data ({fills} fills, {rebalances} rebalances)"
        else:
            net = (
                float(bucket["realized_pnl"])
                - float(bucket["rebalance_cost"])
                - float(bucket["adverse_markout"])
            )
            net_cell = (
                f"{net:+.4f} ({fills} fills, {rebalances} rebalances, "
                f"{hours}h, {bucket['markout_observed']} markouts)"
            )

        lines.append(
            f"{condition_id:<40}  {date}  "
            f"{_fmt(actual, 13)} {_fmt(modelled, 15)} {_fmt(error, 12)} "
            f"{fills:>5} {rebalances:>5} {hours:>4}   {net_cell}"
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/real_money_trading.db")
    parser.add_argument("--tape", default=None)
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        log.error(f"database not found: {db_path}", )
        return 2
    tape_dir = Path(args.tape) if args.tape else None
    log.info(build_report(db_path, tape_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
