"""Gamma metadata enrichment for the tape universe.

The CLOB reward walk returns ``event_id``/``event_slug`` but no fee schedule,
and the 2026-11-01 Gamma snapshot predates most of the current universe
(509 of the 647 conditions traded in a recent two-hour window were created
after it). Event grouping and fee netting therefore need a refresh keyed by
the live manifest, not the old snapshot.

The Gamma ``/markets?condition_ids=`` endpoint accepts 100 ids per request and
returns the event object plus ``feeType``/``feeSchedule``. For the 2,496-market
universe that is 25 requests. This module fetches those rows, writes
``data/derived/universe_metadata.parquet`` for the analysis scripts, and
backfills the manifest so the risk code can group by ``event_id`` and net fees
by ``fee_type``.

Usage:
    uv run python -m polymarketbot.data.refresh_universe_metadata
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from collections.abc import Iterable
from typing import Any

from polymarketbot.gamma import GammaClient
from polymarketbot.logs import IST, get_logger

log = get_logger("data.universe_metadata")

META_PATH = Path("data/derived/universe_metadata.parquet")
MANIFEST_DEFAULT = Path("data/tape/manifest_current.json")

# Manifest fields the enrichment owns. Existing non-null values are preserved
# only for fields the walk already owns (question/slug/pool); these are filled.
FIELDS = (
    "event_id",
    "event_slug",
    "event_title",
    "fee_type",
    "fees_enabled",
    "fee_rate",
    "fee_exponent",
    "fee_rebate",
    "tick_size",
    "min_order_size",
    "resolution_source",
)


def _s(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def to_row(market: dict[str, Any]) -> dict[str, Any]:
    """Gamma market object -> one metadata row (snake_case, JSON-safe)."""
    events = market.get("events") or []
    event = events[0] if events and isinstance(events[0], dict) else {}
    sched = GammaClient.fee_schedule(market)
    fee_type = _s(market.get("feeType"))
    if fee_type is None and market.get("feesEnabled") is False:
        # Gamma leaves feeType null when fees are disabled; that is a measured
        # zero (the venue's ``zero_fees`` schedule), not an unknown fee.
        fee_type = "zero_fees"
    return {
        "condition_id": _s(market.get("conditionId")),
        "event_id": _s(event.get("id") or market.get("eventId")),
        "event_slug": _s(event.get("slug")),
        "event_title": _s(event.get("title")),
        "event_description": _s(event.get("description")),
        "resolution_source": _s(
            event.get("resolutionSource") or market.get("resolutionSource")
        ),
        "fee_type": fee_type,
        "fees_enabled": market.get("feesEnabled"),
        "fee_rate": float(sched.rate) if sched else None,
        "fee_exponent": float(sched.exponent) if sched else None,
        "fee_rebate": float(sched.rebate_rate) if sched else None,
        "tick_size": GammaClient.tick_size(market),
        "min_order_size": GammaClient.min_order_size(market),
        "neg_risk": bool(market.get("negRisk")),
        "tags": GammaClient.tag_labels(market),
        "gamma_end_date": _s(market.get("endDate")),
        "fetched_at": datetime.now(IST).isoformat(),
    }


def fetch_rows(
    condition_ids: Iterable[str], *, client: GammaClient | None = None
) -> list[dict[str, Any]]:
    """Fetch metadata for the ids in 100-condition Gamma batches."""
    ids = [str(cid) for cid in dict.fromkeys(condition_ids) if cid]
    client = client or GammaClient()
    rows: list[dict[str, Any]] = []
    for market in client.markets(ids):
        row = to_row(market)
        if row["condition_id"]:
            rows.append(row)
    return rows


def write_rows(rows: list[dict[str, Any]], path: str | Path = META_PATH) -> int:
    """Write the metadata table, de-duplicated by condition id."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    by_id = {row["condition_id"].lower(): row for row in rows if row.get("condition_id")}
    if not by_id:
        log.warning("no metadata rows to write")
        return 0
    table = pa.Table.from_pylist(list(by_id.values()))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, target)
    return len(by_id)


def load_rows(path: str | Path = META_PATH) -> dict[str, dict[str, Any]]:
    """condition_id (lower) -> metadata row; empty when the file is absent."""
    import pyarrow.parquet as pq

    target = Path(path)
    if not target.exists():
        return {}
    table = pq.read_table(target)
    return {
        str(row["condition_id"]).lower(): row
        for row in table.to_pylist()
        if row.get("condition_id")
    }


def apply_to_manifest(
    rows: list[dict[str, Any]] | dict[str, dict[str, Any]],
    manifest_path: str | Path = MANIFEST_DEFAULT,
) -> int:
    """Fill event/fee fields on every manifest market; atomic replace.

    ``rows`` may be a list of metadata rows or a condition -> row mapping. Only
    non-null values overwrite, so a missing fee on one Gamma row does not erase
    a fee the manifest already carries.
    """
    path = Path(manifest_path)
    if not path.exists():
        log.warning(f"manifest {path} missing; nothing to enrich")
        return 0
    if isinstance(rows, dict):
        meta = {str(k).lower(): v for k, v in rows.items()}
    else:
        meta = {
            str(row["condition_id"]).lower(): row
            for row in rows
            if row.get("condition_id")
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    updated = 0
    for market in payload.get("markets") or []:
        cid = str(market.get("condition_id") or "").lower()
        row = meta.get(cid)
        if not row:
            continue
        for field in FIELDS:
            value = row.get(field)
            if value is not None:
                market[field] = value
        if row.get("tags"):
            market["tags"] = list(row["tags"])
        updated += 1
    payload["metadata_updated_at"] = datetime.now(IST).isoformat()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return updated


def refresh(
    manifest_path: str | Path = MANIFEST_DEFAULT,
    out_path: str | Path = META_PATH,
    *,
    limit: int | None = None,
    client: GammaClient | None = None,
) -> dict[str, int]:
    """Fetch the manifest's condition ids, write the table, backfill the manifest."""
    path = Path(manifest_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    ids = [
        str(m["condition_id"])
        for m in payload.get("markets") or []
        if m.get("condition_id")
    ]
    # Keep rows for conditions that left the manifest: the scorecard still
    # needs their fees for trades executed while they were listed.
    ids = list(dict.fromkeys(ids + list(load_rows(out_path))))
    if limit:
        ids = ids[:limit]
    rows = fetch_rows(ids, client=client)
    written = write_rows(rows, out_path)
    applied = apply_to_manifest(rows, manifest_path)
    log.info(
        f"metadata: {len(ids)} ids -> {len(rows)} rows, {written} written, "
        f"{applied} manifest markets updated"
    )
    return {"ids": len(ids), "rows": len(rows), "written": written, "applied": applied}
