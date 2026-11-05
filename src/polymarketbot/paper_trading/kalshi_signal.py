"""Cross-venue Kalshi basis monitor for the paper maker.

What it is
----------
The paper maker quotes Polymarket. Kalshi lists the same event on a public,
CFTC-regulated book, so its YES mid is an independent estimate of the same
probability. This module polls ``GET /markets/orderbooks`` for the tickers in
the audited Polymarket -> Kalshi mapping and exposes, per mapped Polymarket
condition,

    basis = logit(kalshi_yes_mid) - logit(pm_yes_mid)

A positive basis means Kalshi prices YES above Polymarket: Polymarket's YES
mid has to reprice up, so the resting **ask** is the side at risk. A negative
basis threatens the **bid**. ``direction="inverted"`` rows in the mapping flip
Kalshi's YES probability (``1 - mid``) before the logit.

The threshold that turns this signal into a gate is **not yet measured**. The
orchestrator defaults to disabled with ``signal="ofi"``, so production
behaviour is unchanged; once the kalshi signal is selected, a threshold of
``0.0`` only rejects an exactly zero basis.

Transport
---------
The Kalshi edge is fetched over the same pinned, rotating HTTP client as the
tape collector (``polymarketbot.data.streams.kalshi.KalshiClient``); the
``/markets/orderbooks`` endpoint accepts at most 100 ``tickers=`` values per
request. Network I/O runs in a daemon thread so the paper engine's event loop
and quote generation never block. Nothing here writes the tape, and no API key
is used: the endpoint is public.
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from polymarketbot.data.streams.kalshi import KalshiClient
from polymarketbot.logs import get_logger

logger = get_logger("paper_trading.kalshi")

DEFAULT_MAPPING = Path("data/derived/kalshi_mapping_v2.parquet")
DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_STALE_AFTER_S = 20.0
#: Kalshi rejects more than 100 repeated ``tickers=`` values with HTTP 400.
MAX_TICKERS_PER_REQUEST = 100
#: Every Kalshi price below this margin is inside a valid quote; 0 and 1 are
#: not probabilities and their logit is undefined.
EPSILON = 1e-9


def _price(value: Any) -> Optional[float]:
    """Normalise a Kalshi price to 0..1.

    Current payloads use dollar strings (``"0.5500"``); legacy payloads use
    integer cents (``55``). Divide by 100 only when the magnitude exceeds one.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number > 1.0:
        number /= 100.0
    if number < 0.0 or number > 1.0:
        return None
    return number


def _best_bid(levels: Any) -> Optional[float]:
    """Highest price in a Kalshi bid ladder, or ``None`` when it is empty."""
    if not isinstance(levels, list):
        return None
    best: Optional[float] = None
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 1:
            continue
        price = _price(level[0])
        if price is None:
            continue
        if best is None or price > best:
            best = price
    return best


def kalshi_yes_mid(orderbook: dict[str, Any]) -> Optional[float]:
    """Kalshi YES mid from an order book snapshot.

    ``yes_dollars``/``no_dollars`` are bid ladders (the API renamed the legacy
    ``yes``/``no`` cent ladders). The best NO bid is the YES ask at
    ``1 - best_no_bid``. A one-sided or crossed book is ``None``: a missing
    quote is not a 0c or 100c quote.
    """
    fp = orderbook.get("orderbook_fp")
    book = fp if isinstance(fp, dict) else orderbook
    yes_bid = _best_bid(book.get("yes_dollars"))
    if yes_bid is None:
        yes_bid = _best_bid(book.get("yes"))
    no_bid = _best_bid(book.get("no_dollars"))
    if no_bid is None:
        no_bid = _best_bid(book.get("no"))
    if yes_bid is None or no_bid is None:
        return None
    yes_ask = 1.0 - no_bid
    if yes_ask < yes_bid - 1e-9:
        return None
    return (yes_bid + yes_ask) / 2.0


def logit(probability: float) -> Optional[float]:
    """``log(p / (1 - p))`` with ``p`` clamped inside (0, 1)."""
    p = float(probability)
    if not math.isfinite(p):
        return None
    p = min(1.0 - EPSILON, max(EPSILON, p))
    return math.log(p / (1.0 - p))


class KalshiBasisMonitor:
    """Poll mapped Kalshi books in a background thread and answer ``basis``.

    ``poll_seconds`` is the request cadence, not a freshness guarantee: a
    snapshot older than ``stale_after_s`` is reported as ``None`` so the
    orchestrator keeps both sides instead of acting on a dead venue.

    Tests inject a ``client`` exposing ``get_json(path, params)``; production
    leaves it as ``None`` and gets the pinned :class:`KalshiClient`.
    """

    def __init__(
        self,
        mapping_path: Path | str = DEFAULT_MAPPING,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        base_url: str = DEFAULT_BASE_URL,
        client: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.mapping_path = Path(mapping_path)
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.stale_after_s = max(0.0, float(stale_after_s))
        self.base_url = base_url
        self._clock = clock
        self._client = client or KalshiClient(
            base_url,
            timeout=min(5.0, self.stale_after_s or 5.0),
            retry_total=1,
        )
        self._owns_client = client is None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._allowed: Optional[set[str]] = None

        # ticker -> (condition_id, direction); condition -> last mid/observation.
        self._ticker_rows: dict[str, tuple[str, str]] = {}
        self._condition_direction: dict[str, str] = {}
        self._mids: dict[str, tuple[float, float]] = {}
        self._condition_tickers: dict[str, list[str]] = {}
        self._load_mapping()

    # -- mapping -----------------------------------------------------------
    def _load_mapping(self) -> None:
        from polymarketbot.data.kalshi_mapping import read_mapping

        rows = read_mapping(self.mapping_path)
        tickers: dict[str, tuple[str, str]] = {}
        conditions: dict[str, list[str]] = {}
        directions: dict[str, str] = {}
        for row in rows:
            ticker = row.get("kalshi_ticker")
            condition = row.get("polymarket_condition_id")
            if not ticker or not condition:
                continue
            ticker, condition = str(ticker), str(condition)
            direction = str(row.get("direction") or "normal").lower()
            if ticker in tickers and tickers[ticker][0] != condition:
                continue  # one Kalshi market, one PM condition: first wins
            tickers[ticker] = (condition, direction)
            conditions.setdefault(condition, []).append(ticker)
            directions.setdefault(condition, direction)
        with self._lock:
            self._ticker_rows = tickers
            self._condition_tickers = conditions
            self._condition_direction = directions
            self._mids = {}
        logger.info(
            f"Kalshi basis mapping: {len(conditions)} conditions / "
            f"{len(tickers)} tickers from {self.mapping_path}"
        )

    def set_conditions(self, conditions: Iterable[str]) -> None:
        """Restrict polling to the conditions the engine actually quotes."""
        self._allowed = {str(condition) for condition in conditions}
        logger.info(
            f"Kalshi basis monitor restricted to {len(self._allowed)} registered "
            f"conditions ({len(self.mapped_tickers())} mapped tickers)"
        )

    def mapped_tickers(self) -> tuple[str, ...]:
        with self._lock:
            tickers = [
                ticker
                for ticker, (condition, _) in self._ticker_rows.items()
                if self._allowed is None or condition in self._allowed
            ]
        return tuple(sorted(tickers))

    def mapped_conditions(self) -> tuple[str, ...]:
        with self._lock:
            conditions = [
                condition
                for condition in self._condition_tickers
                if self._allowed is None or condition in self._allowed
            ]
        return tuple(sorted(conditions))

    # -- polling -----------------------------------------------------------
    def poll_once(self) -> int:
        """Fetch every mapped book once. Returns the number of fresh mids."""
        tickers = self.mapped_tickers()
        if not tickers:
            return 0
        fresh = 0
        for start in range(0, len(tickers), MAX_TICKERS_PER_REQUEST):
            batch = list(tickers[start : start + MAX_TICKERS_PER_REQUEST])
            payload = self._client.get_json(
                "/markets/orderbooks", {"tickers": batch}
            )
            rows = payload.get("orderbooks") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                logger.warning("Kalshi orderbooks payload had no orderbooks list")
                continue
            observed_at = self._clock()
            updates: dict[str, tuple[float, float]] = {}
            for item in rows:
                if not isinstance(item, dict):
                    continue
                ticker = item.get("ticker")
                if selector := self._ticker_rows.get(str(ticker)):
                    mid = kalshi_yes_mid(item)
                    if mid is not None:
                        updates[selector[0]] = (mid, observed_at)
                        fresh += 1
            with self._lock:
                self._mids.update(updates)
        return fresh

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - a poll must not kill the run
                logger.warning(f"Kalshi basis poll failed: {exc}")
            self._stop.wait(self.poll_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self.mapped_tickers():
            logger.warning(
                "Kalshi basis monitor has no mapped tickers; no polls will run"
            )
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="kalshi-basis", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None

    def close(self) -> None:
        self.stop()
        if self._owns_client:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass

    # -- answers -----------------------------------------------------------
    def latest(self, condition_id: str) -> Optional[float]:
        """Freshest Kalshi probability mapped onto Polymarket YES, else ``None``."""
        with self._lock:
            entry = self._mids.get(str(condition_id))
            direction = self._condition_direction.get(str(condition_id), "normal")
        if entry is None:
            return None
        mid, observed_at = entry
        age = self._clock() - observed_at
        if age > self.stale_after_s:
            return None
        if direction == "inverted":
            return 1.0 - mid
        return mid

    def fresh_count(self) -> int:
        """Conditions with a snapshot inside ``stale_after_s``."""
        return sum(
            1
            for condition in self.mapped_conditions()
            if self.latest(condition) is not None
        )

    def basis(self, condition_id: str, pm_yes_mid: Optional[float]) -> Optional[float]:
        """``logit(kalshi) - logit(pm)``; ``None`` when either mid is unusable."""
        if pm_yes_mid is None:
            return None
        kalshi_mid = self.latest(condition_id)
        if kalshi_mid is None:
            return None
        kalshi_logit = logit(kalshi_mid)
        pm_logit = logit(float(pm_yes_mid))
        if kalshi_logit is None or pm_logit is None:
            return None
        return kalshi_logit - pm_logit
