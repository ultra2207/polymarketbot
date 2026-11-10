"""Reconcile the venue's reward and rebate payouts against the ledger.

This is the command that can prove or refute the accrual model with real money:
for one UTC epoch day it reads

* ``GET /rewards/user/total?date=`` - the venue's own earnings per asset,
* ``GET /rebates/current?date=&maker_address=`` - the wallet's rebated fees per
  condition,
* ``GET /rewards/user?date=`` - the venue's earnings per condition, and
* the paper ledger's ``reward_accrual`` rows and ``maker_rebate`` credits,

then prints the two sides and their ratio. Every number is either the venue's
own, the ledger's own, or the literal ``unmeasured`` with the reason - the
command never fills a missing side with zero, because "the venue paid nothing"
and "we could not ask the venue" are different facts and only one of them
refutes the model.

Usage::

    python -m polymarketbot.rewards.reconcile --date 2026-11-10
    python -m polymarketbot.rewards.reconcile --date 2026-11-10 --db data/paper_engine.db

The client reads ``POLY_ADDRESS`` (or ``PROXY_WALLET_ADDRESS``) from the
environment or ``.env``; without a maker address both venue reads are
``unmeasured`` and the report says so.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any, Optional

from polymarketbot.logs import get_logger

log = get_logger(__name__)

DEFAULT_DB = "data/paper_engine.db"
UNMEASURED = "unmeasured"


def _ratio(numerator: Optional[float], denominator: Optional[float]) -> str:
    """Ratio string, or ``unmeasured``/``n/a`` when a side is missing."""
    if numerator is None or denominator is None:
        return UNMEASURED
    if denominator <= 0.0:
        return "n/a (ledger side is zero)"
    return f"{numerator / denominator:,.3f}x"


def _read_ledger(db_path: str | Path, day: str) -> dict[str, Any]:
    """Read the ledger's accrual and rebate rows for ``day``, read-only.

    Never creates the database: a missing journal is reported as
    ``unmeasured``, not as an empty (zero) day.
    """
    path = Path(db_path)
    if not path.exists():
        return {
            "source": "missing",
            "error": f"ledger journal {path} does not exist",
            "accrual_by_condition": {},
            "rebates_by_condition": {},
        }
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        accrual: dict[str, float] = {}
        accrual_note = ""
        if "reward_accrual" in tables:
            accrual = {
                str(condition_id): float(total or 0.0)
                for condition_id, total in conn.execute(
                    "SELECT condition_id, SUM(accrued_usd) FROM reward_accrual"
                    " WHERE day = ? GROUP BY condition_id",
                    (day,),
                )
            }
        else:
            accrual_note = (
                "this journal predates the reward_accrual table, so the ledger "
                "side of the earnings comparison cannot be measured"
            )
        rebates: dict[str, float] = {}
        rebate_by_detail: dict[str, float] = {}
        if "trades" in tables:
            for condition_id, detail, total in conn.execute(
                "SELECT condition_id, detail, SUM(cash_delta) FROM trades"
                " WHERE kind = 'credit' AND detail LIKE 'maker_rebate%'"
                " AND date(ts, 'unixepoch') = ? GROUP BY condition_id, detail",
                (day,),
            ):
                rebates[str(condition_id)] = rebates.get(str(condition_id), 0.0) + float(
                    total or 0.0
                )
                rebate_by_detail[str(detail)] = rebate_by_detail.get(
                    str(detail), 0.0
                ) + float(total or 0.0)
        return {
            "source": "measured",
            "error": "",
            "accrual_note": accrual_note,
            "accrual_by_condition": accrual,
            "rebates_by_condition": rebates,
            "rebate_by_detail": rebate_by_detail,
        }
    finally:
        conn.close()


def reconcile_day(
    day: str,
    *,
    client: Any = None,
    db_path: str | Path = DEFAULT_DB,
) -> dict[str, Any]:
    """Gather both sides of one UTC day. Nothing is defaulted to zero."""
    if client is None:
        from polymarketbot.trading.client import LiveClobClient

        client = LiveClobClient()

    earnings = client.get_rewards_for_date(day)
    rebates = client.get_current_rebates(day)
    per_condition = client.get_daily_rewards(day)
    ledger = _read_ledger(db_path, day)

    venue_earnings_total = (
        float(earnings["total_usd"]) if earnings["source"] == "clob_api" else None
    )
    venue_rebates_total = (
        float(rebates["total_usd"]) if rebates["source"] == "clob_api" else None
    )
    venue_by_condition = (
        {str(item["condition_id"]): float(item["earned_usd"])
         for item in per_condition.get("market_breakdown", [])
         if item.get("condition_id")}
        if per_condition.get("source") == "clob_api"
        else None
    )
    ledger_accrual = ledger["accrual_by_condition"]
    ledger_accrual_total = (
        sum(ledger_accrual.values())
        if ledger["source"] == "measured" and not ledger.get("accrual_note")
        else None
    )
    ledger_rebate_total = (
        sum(ledger["rebates_by_condition"].values())
        if ledger["source"] == "measured"
        else None
    )

    return {
        "day": day,
        "db_path": str(db_path),
        "client": {
            "dry_run": bool(getattr(client, "dry_run", False)),
            "maker_address": str(getattr(client, "maker_address", "") or ""),
        },
        "venue_earnings": {
            "source": earnings["source"],
            "error": earnings.get("error", ""),
            "by_asset": earnings.get("by_asset") if earnings["source"] == "clob_api" else None,
            "rows": earnings.get("rows") if earnings["source"] == "clob_api" else None,
            "total": venue_earnings_total,
            "by_condition": venue_by_condition,
            "condition_source": per_condition.get("source"),
            "condition_error": per_condition.get("error", ""),
        },
        "venue_rebates": {
            "source": rebates["source"],
            "error": rebates.get("error", ""),
            "by_condition": rebates.get("by_condition")
            if rebates["source"] == "clob_api"
            else None,
            "total": venue_rebates_total,
        },
        "ledger": {
            "source": ledger["source"],
            "error": ledger["error"],
            "accrual_note": ledger.get("accrual_note", ""),
            "accrual_by_condition": ledger_accrual,
            "accrual_total": ledger_accrual_total,
            "rebates_by_condition": ledger["rebates_by_condition"],
            "rebate_by_detail": ledger.get("rebate_by_detail", {}),
            "rebate_total": ledger_rebate_total,
        },
        "ratios": {
            "earnings": _ratio(venue_earnings_total, ledger_accrual_total),
            "rebates": _ratio(venue_rebates_total, ledger_rebate_total),
        },
    }


def _money(value: Optional[float]) -> str:
    return UNMEASURED if value is None else f"${value:,.6f}"


def _reason(source: str, error: str) -> str:
    if source == "clob_api":
        return ""
    detail = error or source
    return f"{UNMEASURED} ({detail})"


def format_report(data: dict[str, Any]) -> str:
    """Human-readable table for :func:`reconcile_day`'s result."""
    day = data["day"]
    venue_e = data["venue_earnings"]
    venue_r = data["venue_rebates"]
    ledger = data["ledger"]
    lines = [f"Reward reconciliation for {day} (UTC epoch day)"]
    client = data.get("client") or {}
    if client.get("maker_address"):
        lines.append(f"  maker: {client['maker_address']}")
    if client.get("dry_run"):
        lines.append(
            "  note: the client is in dry-run (no L2 credentials configured), so "
            "every venue read is unmeasured - that is a missing measurement, "
            "not a zero payout"
        )

    lines.append("")
    lines.append("[earnings] venue GET /rewards/user/total vs ledger reward_accrual")
    if venue_e["total"] is None:
        lines.append(f"  venue: {_reason(venue_e['source'], venue_e['error'])}")
    else:
        for asset, usd in sorted((venue_e["by_asset"] or {}).items()):
            lines.append(f"  venue asset {asset}: {_money(usd)}")
        lines.append(f"  venue total: {_money(venue_e['total'])}")
    if ledger["accrual_note"]:
        lines.append(f"  ledger: {UNMEASURED} ({ledger['accrual_note']})")
    elif ledger["source"] != "measured":
        lines.append(f"  ledger: {UNMEASURED} ({ledger['error']})")
    else:
        lines.append(
            f"  ledger total: {_money(ledger['accrual_total'])} over "
            f"{len(ledger['accrual_by_condition'])} condition(s)"
        )
    lines.append(f"  ratio venue/ledger: {data['ratios']['earnings']}")

    lines.append("")
    lines.append("[per-condition earnings] venue GET /rewards/user vs ledger reward_accrual")
    venue_by_condition = venue_e["by_condition"]
    if venue_by_condition is None:
        lines.append(
            f"  venue: {_reason(venue_e['condition_source'] or '', venue_e['condition_error'])}"
        )
    ledger_by_condition = ledger["accrual_by_condition"] if ledger["source"] == "measured" else {}
    ledger_measured = ledger["source"] == "measured" and not ledger.get("accrual_note")
    conditions = sorted(set(venue_by_condition or {}) | set(ledger_by_condition))
    if conditions:
        lines.append(f"  {'condition':<20} {'venue':>14} {'ledger':>14} {'ratio':>10}")
        for condition_id in conditions:
            venue_usd = (
                None
                if venue_by_condition is None
                else venue_by_condition.get(condition_id, 0.0)
            )
            ledger_usd = (
                None
                if not ledger_measured
                else ledger_by_condition.get(condition_id, 0.0)
            )
            lines.append(
                f"  {condition_id[:20]:<20} {_money(venue_usd):>14} "
                f"{_money(ledger_usd):>14} {_ratio(venue_usd, ledger_usd):>10}"
            )
    else:
        lines.append("  no condition appears on either side")

    lines.append("")
    lines.append("[rebates] venue GET /rebates/current vs ledger maker_rebate credits")
    if venue_r["total"] is None:
        lines.append(f"  venue: {_reason(venue_r['source'], venue_r['error'])}")
    else:
        lines.append(f"  venue total: {_money(venue_r['total'])}")
    if ledger["source"] != "measured":
        lines.append(f"  ledger: {UNMEASURED} ({ledger['error']})")
    else:
        detail = ledger["rebate_by_detail"]
        split = ", ".join(f"{name}={_money(usd)}" for name, usd in sorted(detail.items()))
        lines.append(
            f"  ledger total: {_money(ledger['rebate_total'])}"
            + (f" ({split})" if split else "")
        )
    lines.append(f"  ratio venue/ledger: {data['ratios']['rebates']}")

    venue_rebates_by_condition = venue_r["by_condition"]
    if venue_rebates_by_condition is not None and ledger["source"] == "measured":
        conditions = sorted(
            set(venue_rebates_by_condition) | set(ledger["rebates_by_condition"])
        )
        if conditions:
            lines.append("")
            lines.append(
                f"  {'condition':<20} {'venue':>14} {'ledger':>14} {'ratio':>10}"
            )
            for condition_id in conditions:
                venue_usd = venue_rebates_by_condition.get(condition_id, 0.0)
                ledger_usd = ledger["rebates_by_condition"].get(condition_id, 0.0)
                lines.append(
                    f"  {condition_id[:20]:<20} {_money(venue_usd):>14} "
                    f"{_money(ledger_usd):>14} {_ratio(venue_usd, ledger_usd):>10}"
                )

    lines.append("")
    lines.append(
        "A ratio near 1.0x with both sides measured means the model priced the "
        "pool correctly; a measured venue value against an unmeasured ledger (or "
        "vice versa) is a coverage gap, not a refutation."
    )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    from datetime import datetime, timezone

    parser = argparse.ArgumentParser(
        description="Reconcile venue reward/rebate payouts against the paper ledger"
    )
    parser.add_argument(
        "--date",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        help="UTC epoch day to reconcile (YYYY-MM-DD); defaults to today",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite ledger journal")
    args = parser.parse_args(argv)

    data = reconcile_day(args.date, db_path=args.db)
    print(format_report(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
