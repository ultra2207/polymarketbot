"""Fill ledger records and post-trade markout measurement."""

from __future__ import annotations

import heapq
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, List, Optional, Tuple

from polymarketbot.logs import get_logger

logger = get_logger("real_money_engine")


# Horizon (seconds) -> measured fills column.
MARKOUT_HORIZONS: Tuple[Tuple[float, str], ...] = (
    (30.0, "markout_30s"),
    (300.0, "markout_5m"),
    (1800.0, "markout_30m"),
)


@dataclass
class RealMoneyFillRecord:
    """Record of a trade fill executed on CLOB."""

    fill_id: str
    timestamp: float
    condition_id: str
    token_side: str
    side: str
    fill_price: float
    fill_size: float
    is_maker: bool
    order_id: str = ""
    notes: str = ""
    maker_rebate_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MarkoutTracker:
    """Daemon worker that fills post-trade markouts once a horizon matures.

    One instance per engine. Each booked fill queues three horizons; when a
    horizon is due the fresh mid is read under the engine lock and only a NULL
    markout column is written. Nothing is backfilled on restart, so a horizon
    that could not be measured at its due time stays NULL forever.

    The thread starts on the first recorded fill and is joined from
    ``RealMoneyTradingEngine.close()``. Tests inject ``clock`` and disable
    ``autostart`` to drive ``process_due`` deterministically.
    """

    def __init__(
        self,
        conn: Any,
        mid_getter: Callable[[str], Optional[float]],
        lock: Optional[threading.RLock] = None,
        clock: Optional[Callable[[], float]] = None,
        autostart: bool = True,
    ) -> None:
        self._conn = conn
        self._mid_getter = mid_getter
        self._lock = lock or threading.RLock()
        self._clock = clock or time.time
        self._autostart = bool(autostart)
        self._heap: List[tuple[float, int, str, str, float, str, str]] = []
        self._seq = 0
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._written = 0
        self._unmeasured = 0

    # -- public API ---------------------------------------------------------
    def record(
        self,
        fill_key: str,
        token_side: str,
        fill_price: float,
        side: str,
        now: Optional[float] = None,
    ) -> None:
        """Queue the three markout horizons for one booked fill."""
        if not fill_key:
            return
        base_ts = self._clock() if now is None else float(now)
        side_up = str(side).upper()
        if side_up not in ("BUY", "SELL") or not (0.0 < float(fill_price) < 1.0):
            return
        with self._cv:
            for horizon, column in MARKOUT_HORIZONS:
                self._seq += 1
                self._heap.append(
                    (
                        base_ts + horizon,
                        self._seq,
                        str(fill_key),
                        str(token_side).upper(),
                        float(fill_price),
                        side_up,
                        column,
                    )
                )
            heapq.heapify(self._heap)
            self._cv.notify_all()
        if self._autostart:
            self.start()

    def start(self) -> None:
        """Start the daemon thread when it is not already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="markout-tracker", daemon=True
        )
        self._thread.start()

    def pending(self) -> int:
        with self._cv:
            return len(self._heap)

    def process_due(self, now: Optional[float] = None) -> int:
        """Write every markout whose horizon has matured (returns rows attempted)."""
        current = self._clock() if now is None else float(now)
        due: List[tuple[float, int, str, str, float, str, str]] = []
        with self._cv:
            while self._heap and self._heap[0][0] <= current:
                due.append(heapq.heappop(self._heap))
        for item in due:
            self._apply(*item[2:])
        return len(due)

    def stop(self, timeout: float = 2.0) -> int:
        """Stop the worker; returns the number of horizons still pending."""
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                with self._cv:
                    leftover = len(self._heap)
                logger.warning(
                    f"MarkoutTracker did not stop within {timeout:.1f}s; "
                    f"{leftover} pending markout(s) left NULL"
                )
        self._thread = None
        with self._cv:
            return len(self._heap)

    # -- internals ----------------------------------------------------------
    def _apply(
        self,
        fill_key: str,
        token_side: str,
        fill_price: float,
        side: str,
        column: str,
    ) -> None:
        if self._conn is None:
            return
        with self._lock:
            if self._conn is None:
                return
            try:
                mid = self._mid_getter(token_side)
            except Exception as e:  # measurement only, never propagate
                logger.debug(f"Markout mid read failed for {fill_key}: {e}")
                mid = None
            if mid is None:
                self._unmeasured += 1
                return
            markout = (float(mid) - fill_price) if side == "BUY" else (fill_price - float(mid))
            try:
                cur = self._conn.cursor()
                cur.execute(
                    f"UPDATE fills SET {column} = ? "
                    f"WHERE fill_key = ? AND {column} IS NULL",
                    (float(markout), fill_key),
                )
                if cur.rowcount and cur.rowcount > 0:
                    self._written += 1
                self._conn.commit()
            except Exception as e:
                logger.warning(f"Markout write failed for {fill_key} {column}: {e}")

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                if not self._heap:
                    self._cv.wait(1.0)
                    continue
                due_ts = self._heap[0][0]
                now = self._clock()
                if due_ts > now:
                    self._cv.wait(min(max(due_ts - now, 0.01), 5.0))
                    continue
            self.process_due()
