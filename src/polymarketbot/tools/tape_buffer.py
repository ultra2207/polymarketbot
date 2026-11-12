"""Minute-partitioned parquet buffers shared by the tape recorders.

A recorder must persist as it goes: the working agreement caps any single run and
a kill has to cost one minute, not the session. Both the crypto up/down recorder
and the reward-universe recorder need the same thing, so it lives here once.

Rows carry the market metadata (condition id, slug, which token side) plus the
observed fields; the buffer stamps every row with the destination minute at flush
time so a long run produces ``date=/hour=/minute=`` parts.

Durability contract
-------------------
* A part is written to a temporary file beside it and ``os.replace``d into
  place, so a kill during the write leaves the previous part intact instead of
  a half-parquet that the next flush has to quarantine.
* A batch is only dropped from memory after its part is on disk. A failed write
  puts the rows back and re-raises, so the caller loses a flush, not the data.
* A part that cannot be read is renamed to ``minute=NN.corrupt-<epoch>.parquet``
  before a fresh part is written; the rows in it are not silently overwritten.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from polymarketbot.logs import get_logger

log = get_logger("tape_buffer")


def minute_part(out: Path, now: float) -> Path:
    stamp = datetime.fromtimestamp(now, UTC)
    dest = out / f"date={stamp.strftime('%Y-%m-%d')}" / f"hour={stamp.strftime('%H')}"
    dest.mkdir(parents=True, exist_ok=True)
    return dest / f"minute={stamp.strftime('%M')}.parquet"


def _quarantine(path: Path, exc: Exception) -> None:
    # The new name must not end in .parquet: every reader in this repo globs
    # `*.parquet`, so a quarantined part kept that suffix would still be read
    # and would fail the whole table instead of just this minute.
    bad = path.with_name(f"{path.stem}.corrupt-{int(time.time())}.bad")
    try:
        path.rename(bad)
        detail = f"moved to {bad.name}"
    except OSError:
        # Could not rename (an open reader on Windows); dropping the unreadable
        # part is the only way forward and the loss is logged by name.
        path.unlink(missing_ok=True)
        detail = "deleted (rename refused)"
    log.warning(
        f"corrupt parquet part {path.name} ({exc}); {detail} and writing a fresh part"
    )


def flush_frame(frame: pd.DataFrame, path: Path) -> int:
    """Append ``frame`` to the minute part at ``path`` atomically.

    Returns the number of rows the part holds afterwards. The temporary file is
    hidden and has no ``.parquet`` suffix, so a crash mid-write cannot leave
    something a reader's glob would pick up.
    """
    if path.exists():
        try:
            frame = pd.concat([pd.read_parquet(path), frame], ignore_index=True)
        except Exception as exc:  # noqa: BLE001 - a corrupt part must not kill the run
            _quarantine(path, exc)
    tmp = path.with_name(f".{path.stem}.tmp")
    frame.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return len(frame)


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
        # `add` runs on the event-loop thread, `flush` in a worker thread.
        self._lock = threading.Lock()

    def set_meta(self, meta: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            self.meta.update(meta)

    def add(self, asset_id: str, row: dict[str, Any]) -> bool:
        """Append a row for a subscribed asset; unknown assets are dropped.

        Dropping is deliberate: only tokens we asked for belong in the tape, and
        a market that expired between discovery and the frame would otherwise
        arrive with no metadata to attribute it to.
        """
        with self._lock:
            info = self.meta.get(asset_id)
            if info is None:
                return False
            self.rows.append({**info, **row})
        return True

    def flush(self, now: float | None = None) -> int:
        with self._lock:
            if not self.rows:
                return 0
            rows = self.rows
            self.rows = []
        now = now or time.time()
        frame = pd.DataFrame(rows)
        for col in self.columns:
            if col not in frame.columns:
                frame[col] = None
        frame = frame[self.columns]
        for col, dtype in self.dtypes.items():
            if col in frame.columns:
                frame[col] = frame[col].astype(dtype)
        try:
            flush_frame(frame, minute_part(self.out, now))
        except BaseException:
            # The part is not on disk, so the batch goes back in front of the
            # rows that arrived while it was being written.
            with self._lock:
                self.rows = rows + self.rows
            raise
        with self._lock:
            self.written += len(rows)
            self.flushes += 1
        return len(rows)
