"""Persistence of live-polled UMA verdicts.

A verdict is the oracle's own statement and does not expire because the
process restarted; the deadline check happens at decision time in the pricing
rule, so a stale row here can never fund a trade.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict

from polymarketbot.logs import get_logger
from polymarketbot.strategy.settlement.verdict_index import (
    TapeProposal,
    normalize_question,
)

log = get_logger("domain2_settlement_fair_value")

# Live-polled verdicts outlive the process. The overlay is memory state built
# from the chain, and a restart used to erase it: the parquet tape is only as
# fresh as the last collector run (measured 2026-11-09: 5,035 min stale while
# the chain carried 74 ``ProposePrice`` events in the previous 25 h), so every
# restart began with an empty verdict index and the radar reported "0
# oracle-priced" for its whole run. Persisting the overlay is what makes the
# on-chain proposals the ever-updating input instead of a per-process one-shot.
UMA_LIVE_VERDICTS_PATH: str = "data/derived/uma/uma_live_verdicts.json"


def load_overlay(path: str) -> Dict[str, TapeProposal]:
    """Restore the verdicts a previous process polled off the chain."""
    overlay: Dict[str, TapeProposal] = {}
    if not path:
        return overlay
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return overlay
    rows = raw.get("proposals") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return overlay
    restored = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            proposal = TapeProposal(
                question=str(row.get("question") or ""),
                condition_id=str(row.get("condition_id") or "").lower(),
                proposed_price=float(row["proposed_price"]),
                ts=float(row["ts"]),
                liveness_sec=float(row.get("liveness_sec") or 0.0),
                expiry_ts=float(row.get("expiry_ts") or 0.0),
            )
        except (KeyError, TypeError, ValueError):
            continue
        key = normalize_question(proposal.question)
        if not key or proposal.ts <= 0.0:
            continue
        overlay[key] = proposal
        restored += 1
    if restored:
        log.info(
            f"UMA verdict overlay: {restored} live-polled proposal(s) restored "
            f"from {path}"
        )
    return overlay


def save_overlay(path: str, overlay: Dict[str, TapeProposal]) -> None:
    """Atomically persist the live overlay. Best effort by design."""
    if not path:
        return
    path_obj = Path(path)
    try:
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        tmp = path_obj.with_name(path_obj.name + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "updated_ts": time.time(),
                    "proposals": [
                        {
                            "question": p.question,
                            "condition_id": p.condition_id,
                            "proposed_price": p.proposed_price,
                            "ts": p.ts,
                            "liveness_sec": p.liveness_sec,
                            "expiry_ts": p.expiry_ts,
                        }
                        for p in overlay.values()
                    ],
                }
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path_obj)
    except OSError as exc:  # noqa: BLE001 - an unwritable path is a degraded cache
        log.warning(f"UMA verdict overlay unwritable ({exc}); memory only")
