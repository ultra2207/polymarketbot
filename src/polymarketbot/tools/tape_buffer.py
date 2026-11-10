"""Minute-partitioned parquet buffers shared by the tape recorders.

A recorder must persist as it goes: the working agreement caps any single run and
a kill has to cost one minute, not the session. Both the crypto up/down recorder
and the reward-universe recorder need the same thing, so it lives here once.

Rows carry the market metadata (condition id, slug, which token side) plus the
observed fields; the buffer stamps every row with the destination minute at flush
time so a long run produces ``date=/hour=/minute=`` parts.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd


class MinuteParquetBuffer:
    """Buffer dict rows and write one parquet per wall-clock minute."""

    def __init__(self, out: Path, columns: list[str],
                 dtypes: dict[str, str] | None = None) -> None:
        self.out = out
        self.columns = columns
        # All-null columns infer as parquet "NULL", and a reader that starts on
        # such a part cannot cast a sibling part's DOUBLE into it. Pin the types
        # so a part is self-describing whatever order the glob resolves in.
        self.dtypes = dtypes or {}
        self.rows: list[dict[str, Any]] = []
        self.written = 0
        self.flushes = 0
        self.meta: dict[str, dict[str, Any]] = {}

    def set_meta(self, meta: dict[str, dict[str, Any]]) -> None:
        self.meta.update(meta)

    def add(self, asset_id: str, row: dict[str, Any]) -> bool:
        """Append a row for a subscribed asset; unknown assets are dropped.

        Dropping is deliberate: only tokens we asked for belong in the tape, and
        a market that expired between discovery and the frame would otherwise
        arrive with no metadata to attribute it to.
        """
        info = self.meta.get(asset_id)
        if info is None:
            return False
        self.rows.append({**info, **row})
        return True

    def flush(self, now: float | None = None) -> int:
        if not self.rows:
            return 0
        now = now or time.time()
        dest = (self.out
                / f"date={datetime.fromtimestamp(now, UTC).strftime('%Y-%m-%d')}"
                / f"hour={datetime.fromtimestamp(now, UTC).strftime('%H')}")
        dest.mkdir(parents=True, exist_ok=True)
        minute = datetime.fromtimestamp(now, UTC).strftime("%M")
        rows = self.rows
        self.rows = []
        df = pd.DataFrame(rows)
        for col in self.columns:
            if col not in df.columns:
                df[col] = None
        df = df[self.columns]
        for col, dtype in self.dtypes.items():
            if col in df.columns:
                df[col] = df[col].astype(dtype)
        path = dest / f"minute={minute}.parquet"
        if path.exists():
            try:
                df = pd.concat([pd.read_parquet(path), df], ignore_index=True)
            except Exception:  # noqa: BLE001 - a corrupt part must not kill the run
                pass
        df.to_parquet(path, index=False)
        self.written += len(rows)
        self.flushes += 1
        return len(rows)
