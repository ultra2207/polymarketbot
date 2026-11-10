"""Measured round trips for every action that moves cash or inventory.

The venue's ``GET /time`` is the measurement. An action that cannot be given a
measured round trip is refused, never executed at zero latency.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from polymarketbot.logs import get_logger

log = get_logger(__name__)


class RttProbe:
    """Rolling p50 of ``GET /time``; persisted so a restart is not zero-latency."""

    def __init__(
        self,
        base_url: str,
        session: requests.Session,
        persisted: str | Path = "data/derived/clob_rtt.json",
        legacy: str | Path = "data/derived/local_latency.json",
        samples: int = 9,
    ) -> None:
        self._url = base_url.rstrip("/") + "/time"
        self._session = session
        # The latency probe's own report lives in `local_latency.json`, and
        # `trading.ctf.load_persisted_rpc_rtt_s` reads it for the RPC round
        # trip. A CLOB p50 written into that file destroyed the report, which
        # is why no measured RPC RTT ever existed. The probe now keeps its
        # sample in its own file and only *reads* the legacy one.
        self._persisted = Path(persisted)
        self._legacy = Path(legacy)
        self._samples = max(1, int(samples))
        self._rtts: list[float] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        for path in (self._persisted, self._legacy):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            p50 = payload.get("p50_ms")
            if isinstance(p50, (int, float)) and p50 > 0:
                self._rtts = [float(p50) / 1000.0]
                return

    def measure(self) -> Optional[float]:
        try:
            started = time.perf_counter()
            response = self._session.get(self._url, timeout=5.0)
            elapsed = time.perf_counter() - started
            if response.status_code != 200:
                return self.p50()
        except Exception as exc:  # noqa: BLE001 - measurement failure is the signal
            log.warning(f"RTT probe failed: {exc}")
            return self.p50()
        with self._lock:
            self._rtts.append(elapsed)
            self._rtts = self._rtts[-self._samples:]
        self._persist()
        return self.p50()

    def p50(self) -> Optional[float]:
        with self._lock:
            if not self._rtts:
                return None
            return float(statistics.median(self._rtts))

    def _persist(self) -> None:
        p50 = self.p50()
        if p50 is None:
            return
        try:
            self._persisted.parent.mkdir(parents=True, exist_ok=True)
            self._persisted.write_text(
                json.dumps({"p50_ms": round(p50 * 1000.0, 3), "ts": time.time()}),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning(f"Could not persist latency probe: {exc}")


@dataclass
class Action:
    kind: str
    strategy: str
    payload: dict[str, Any]
    submitted: float
    lands: float


class ActionBook:
    """Queue actions; they become executable only once the round trip has landed.

    A cancel is complete only when it lands. A fill is decided against the book
    that exists at landing time because the caller re-reads the book in
    ``due()`` rather than reusing the decision snapshot.
    """

    def __init__(
        self,
        rtt_source: Callable[[], Optional[float]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rtt_source = rtt_source
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: list[Action] = []
        self._landed: list[Action] = []

    def submit(
        self,
        kind: str,
        strategy: str,
        payload: dict[str, Any],
        rtt: Optional[float] = None,
    ) -> Optional[Action]:
        """Queue an action that lands once its own flight has elapsed.

        ``rtt`` overrides the CLOB round trip for actions that do not travel
        over the CLOB: a merge/split/redeem lands after the Polygon RPC round
        trip plus one block, and pricing that flight with the CLOB p50 credits
        par for a transaction that was never broadcast.
        """
        measured = self._rtt_source() if rtt is None else float(rtt)
        if measured is None or measured <= 0.0:
            log.warning(f"Refused {kind} for {strategy}: no measured round trip")
            return None
        now = self._clock()
        action = Action(kind, strategy, payload, now, now + measured)
        with self._lock:
            self._pending.append(action)
        return action

    def due(self, now: Optional[float] = None) -> list[Action]:
        current = self._clock() if now is None else float(now)
        with self._lock:
            ready = [action for action in self._pending if action.lands <= current]
            if ready:
                self._pending = [action for action in self._pending if action.lands > current]
                self._landed.extend(ready)
                self._landed = self._landed[-500:]
        return ready

    def pending(self) -> list[Action]:
        with self._lock:
            return list(self._pending)

    def cancel(self, predicate: Callable[[Action], bool]) -> int:
        """Remove pending actions (e.g. a quote superseded before it landed)."""
        with self._lock:
            keep = [action for action in self._pending if not predicate(action)]
            removed = len(self._pending) - len(keep)
            self._pending = keep
        return removed
