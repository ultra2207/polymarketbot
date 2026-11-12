"""Durable ledger of signed settlement transactions.

Why this exists
---------------
A settlement transaction is signed once and broadcast once. The dangerous
window is between signing and a receipt: a timeout, a 429 or a dropped
connection says nothing about whether the node accepted the bytes. Re-deriving
a transaction in that window (a new nonce, a new signature) is how a single
intended merge turns into two on-chain merges.

So the signed payload is written here *before* it is broadcast. From then on the
hash is the identity of the settlement: rebroadcasts reuse the stored bytes and
receipt lookups use the stored hash. The row is resolved only by evidence from
the chain (status 1 or status 0); "still pending" is a status, not a failure.

The table is created by `trading/base.py::_init_sqlite` in the engine database.
`PendingSettlementStore()` without a connection keeps its own in-memory
database, which preserves the in-process guarantees for a standalone client.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from polymarketbot.logs import get_logger

logger = get_logger("pending_settlements")

# Row statuses. PENDING is the only non-terminal one.
PENDING = "PENDING"
SUCCESS = "SUCCESS"
REVERTED = "REVERTED"
FAILED = "FAILED"
TERMINAL_STATUSES = (SUCCESS, REVERTED, FAILED)

PENDING_SETTLEMENTS_DDL = """
CREATE TABLE IF NOT EXISTS pending_settlements (
    tx_hash TEXT PRIMARY KEY,
    raw_tx TEXT,
    nonce INTEGER,
    condition_id TEXT,
    kind TEXT,
    amount REAL,
    created_ts REAL,
    status TEXT,
    receipt_json TEXT
);
"""

PENDING_SETTLEMENTS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_pending_settlements_cond "
    "ON pending_settlements(condition_id, status);"
)

# At most one unresolved settlement per condition. This is the cross-process
# half of the double-merge guard: two engines sharing the database cannot both
# persist a signed transaction for the same condition, so the loser refuses
# before it broadcasts. Resolving a row frees the slot.
PENDING_SETTLEMENTS_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_settlements_one_pending "
    "ON pending_settlements(condition_id) WHERE status = 'PENDING';"
)


def _dict_rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    names = [d[0] for d in cursor.description or ()]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


class PendingSettlementStore:
    """Idempotent, hash-keyed record of signed settlement transactions."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._lock = threading.Lock()
        self._owns_conn = conn is None
        self._conn = conn or sqlite3.connect(":memory:", check_same_thread=False)
        with self._lock:
            self._conn.execute(PENDING_SETTLEMENTS_DDL)
            self._conn.execute(PENDING_SETTLEMENTS_INDEX)
            self._conn.execute(PENDING_SETTLEMENTS_UNIQUE)
            self._conn.commit()

    # -- writes ------------------------------------------------------------
    def record(
        self,
        *,
        tx_hash: str,
        raw_tx: str,
        nonce: int,
        condition_id: str,
        kind: str,
        amount: float,
    ) -> bool:
        """Persist a signed transaction. False when the hash was already stored.

        The hash is the primary key, so a retried send of identical bytes can
        never create a second row (and a re-signed transaction can never
        overwrite the original payload).

        Raises `sqlite3.IntegrityError` when another PENDING settlement already
        holds this condition: the caller must refuse to broadcast, because a
        second settlement for the same inventory is exactly the double-merge
        this table exists to prevent.
        """
        key = str(tx_hash).lower()
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM pending_settlements WHERE tx_hash = ?", (key,)
            ).fetchone():
                return False
            try:
                self._conn.execute(
                    "INSERT INTO pending_settlements "
                    "(tx_hash, raw_tx, nonce, condition_id, kind, amount, created_ts, status, receipt_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        key,
                        raw_tx,
                        int(nonce),
                        str(condition_id),
                        str(kind),
                        float(amount),
                        time.time(),
                        PENDING,
                    ),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                # A concurrent insert of these same bytes is idempotent, not a
                # conflict; anything else is a second settlement in flight.
                if self._conn.execute(
                    "SELECT 1 FROM pending_settlements WHERE tx_hash = ?", (key,)
                ).fetchone():
                    return False
                raise

    def resolve(
        self,
        tx_hash: str,
        status: str,
        receipt: dict[str, Any] | None = None,
    ) -> bool:
        """Move a row to a terminal status from chain evidence."""
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"not a terminal settlement status: {status}")
        payload = json.dumps(receipt, default=str) if receipt is not None else None
        with self._lock:
            cur = self._conn.execute(
                "UPDATE pending_settlements SET status = ?, receipt_json = COALESCE(?, receipt_json) "
                "WHERE tx_hash = ? AND status = ?",
                (status, payload, str(tx_hash).lower(), PENDING),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # -- reads -------------------------------------------------------------
    def get(self, tx_hash: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM pending_settlements WHERE tx_hash = ?",
                (str(tx_hash).lower(),),
            )
            rows = _dict_rows(cur)
        return rows[0] if rows else None

    def pending_for(self, condition_id: str) -> list[dict[str, Any]]:
        """Unresolved settlements for one condition, oldest first."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM pending_settlements WHERE condition_id = ? AND status = ? "
                "ORDER BY created_ts",
                (str(condition_id), PENDING),
            )
            return _dict_rows(cur)

    def unresolved(self) -> list[dict[str, Any]]:
        """Every unresolved settlement, oldest first."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM pending_settlements WHERE status = ? ORDER BY created_ts",
                (PENDING,),
            )
            return _dict_rows(cur)

    def count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM pending_settlements")
            return int(cur.fetchone()[0])

    def close(self) -> None:
        if self._owns_conn:
            with self._lock:
                self._conn.close()


__all__ = [
    "FAILED",
    "PENDING",
    "PENDING_SETTLEMENTS_DDL",
    "PENDING_SETTLEMENTS_INDEX",
    "PENDING_SETTLEMENTS_UNIQUE",
    "REVERTED",
    "SUCCESS",
    "TERMINAL_STATUSES",
    "PendingSettlementStore",
]
