"""L3 queue position reconstructed from L2 book updates and public prints.

The CLOB publishes aggregated depth, not per-order priority. A maker can still
infer its own FIFO position from three observations: the level size when it
lands, every later change to that level, and every print at or through it.

The reconstruction follows one invariant: **``ahead`` never grows**. Size that
appears at our price after we land arrived behind us, so a book update that
implies more size in front is clamped instead of trusted. Concretely, for a
tracked order at ``price`` with own size ``size``:

1. ``on_entry`` records the full resting size at ``price`` as
   ``ahead_at_entry`` (``0.0`` when the level is absent).
2. ``on_book`` reads ``observed``, the total size at ``price``; the venue
   includes our own order in it, so ``ahead = min(prev_ahead,
   max(0.0, observed - size))``. No level at ``price`` means only our order can
   be there, hence ``ahead = 0.0``.
3. ``on_trade`` at ``price`` removes the printed size from ``ahead`` and adds it
   to ``traded_at_level``. A print strictly through the price (below a bid,
   above an ask) fills the order outright, and cumulative ``traded_at_level``
   beyond ``initial_ahead`` does too. Prints between third parties at other
   prices change nothing.

All mutable state is guarded by one :class:`threading.RLock`: book updates
arrive from the websocket feed while the quote loop reads progress from its own
thread. The class holds no thresholds and no constants to fit; it is pure
bookkeeping over the :class:`~polymarketbot.risk.contracts.QueueEntry` /
:class:`~polymarketbot.risk.contracts.QueueProgress` contract.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from polymarketbot.risk.contracts import QueueEntry, QueueProgress

Levels = Sequence[tuple[float, float]]


def _finite_number(value: object) -> float | None:
    """Return ``value`` as a finite float, or ``None`` when it is not one."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _require_number(name: str, value: object) -> float:
    """Convert an entry field to a finite float, refusing garbage loudly."""
    number = _finite_number(value)
    if number is None:
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return number


def _require_side(side: object) -> str:
    """Normalise ``side`` to ``BUY`` / ``SELL``."""
    normalised = side.strip().upper() if isinstance(side, str) else ""
    if normalised not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")
    return normalised


def _level_size(levels: Levels, price: float) -> float | None:
    """Total resting size at exactly ``price``, or ``None`` when absent.

    Duplicate levels at the same price are summed. Malformed, non-finite or
    negative-size entries contribute nothing and never raise: a feed glitch
    must not take the quote path down, and NaN must not reach the state.
    """
    total = 0.0
    found = False
    for level in levels:
        try:
            level_price, size = level
        except (TypeError, ValueError):
            continue
        price_at_level = _finite_number(level_price)
        size_at_level = _finite_number(size)
        if price_at_level is None or size_at_level is None:
            continue
        if price_at_level != price:
            continue
        found = True
        if size_at_level > 0.0:
            total += size_at_level
    return total if found else None


@dataclass
class _OrderState:
    """Mutable bookkeeping for one tracked order behind an immutable entry."""

    entry: QueueEntry
    ahead: float
    level_size: float
    traded_at_level: float
    filled: bool
    updated_ts: float


def _progress(state: _OrderState) -> QueueProgress:
    """Freeze one order's current position for the caller."""
    return QueueProgress(
        order_id=state.entry.order_id,
        token_id=state.entry.token_id,
        ahead=state.ahead,
        initial_ahead=state.entry.ahead_at_entry,
        level_size=state.level_size,
        traded_at_level=state.traded_at_level,
        filled=state.filled,
        updated_ts=state.updated_ts,
    )


class QueuePositionTracker:
    """Track every resting order's inferred FIFO position, per token, from L2."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._orders: dict[str, _OrderState] = {}
        # token_id -> order_ids in insertion order (dict used as an ordered set)
        self._by_token: dict[str, dict[str, None]] = {}

    # ------------------------------------------------------------------ entry

    def on_entry(
        self,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        ts: float,
        bids: Levels,
        asks: Levels,
    ) -> QueueEntry:
        """Snapshot the queue in front of a newly submitted order and track it."""
        normalised_side = _require_side(side)
        price = _require_number("price", price)
        size = _require_number("size", size)
        entered_ts = _require_number("ts", ts)

        levels = bids if normalised_side == "BUY" else asks
        observed = _level_size(levels, price)
        ahead_at_entry = 0.0 if observed is None else observed

        entry = QueueEntry(
            order_id=order_id,
            token_id=token_id,
            side=normalised_side,
            price=price,
            size=size,
            entered_ts=entered_ts,
            ahead_at_entry=ahead_at_entry,
        )
        state = _OrderState(
            entry=entry,
            ahead=ahead_at_entry,
            # Our order is included in the venue's level size once resting.
            level_size=ahead_at_entry + size,
            traded_at_level=0.0,
            filled=False,
            updated_ts=entered_ts,
        )
        with self._lock:
            previous = self._orders.get(order_id)
            if previous is not None and previous.entry.token_id != token_id:
                stale_ids = self._by_token.get(previous.entry.token_id)
                if stale_ids is not None:
                    stale_ids.pop(order_id, None)
                    if not stale_ids:
                        del self._by_token[previous.entry.token_id]
            self._orders[order_id] = state
            self._by_token.setdefault(token_id, {})[order_id] = None
        return entry

    # ------------------------------------------------------------------- book

    def on_book(
        self,
        token_id: str,
        bids: Levels,
        asks: Levels,
        ts: float,
    ) -> list[QueueProgress]:
        """Recompute every tracked order at ``token_id`` from an L2 snapshot."""
        updated_ts = _finite_number(ts)
        with self._lock:
            states = self._states_for(token_id)
            results: list[QueueProgress] = []
            for state in states:
                entry = state.entry
                levels = bids if entry.side == "BUY" else asks
                observed = _level_size(levels, entry.price)
                if observed is None:
                    # Only our own size can be at this price.
                    state.ahead = 0.0
                    state.level_size = entry.size
                else:
                    state.level_size = observed
                    not_ahead = max(0.0, observed - entry.size)
                    # FIFO: size that arrives later is behind us.
                    state.ahead = min(state.ahead, not_ahead)
                if updated_ts is not None:
                    state.updated_ts = updated_ts
                results.append(_progress(state))
            return results

    # ------------------------------------------------------------------ trade

    def on_trade(
        self,
        token_id: str,
        price: float,
        size: float,
        taker_side: str,
        ts: float,
    ) -> list[QueueProgress]:
        """Attribute one public print to the orders it can actually touch.

        Prints at a tracked order's price consume the size in front of it.
        Prints strictly through a bid (below) or an ask (above) fill it.
        Prints at other prices are third-party trades and leave the queue
        untouched. Returns progress only for the orders this print changed.
        """
        print_price = _finite_number(price)
        print_size = _finite_number(size)
        if print_price is None or print_size is None or print_size <= 0.0:
            return []
        updated_ts = _finite_number(ts)
        del taker_side  # Direction is already implied by the print price.

        with self._lock:
            results: list[QueueProgress] = []
            for state in self._states_for(token_id):
                entry = state.entry
                at_level = print_price == entry.price
                through = (
                    print_price < entry.price
                    if entry.side == "BUY"
                    else print_price > entry.price
                )
                if not (at_level or through):
                    continue
                if at_level:
                    state.ahead = max(0.0, state.ahead - print_size)
                    state.traded_at_level += print_size
                    if state.traded_at_level > entry.ahead_at_entry:
                        state.filled = True
                if through:
                    state.filled = True
                if updated_ts is not None:
                    state.updated_ts = updated_ts
                results.append(_progress(state))
            return results

    # ------------------------------------------------------------------ reads

    def progress(self, order_id: str) -> QueueProgress | None:
        """Current position of one order, or ``None`` when it is not tracked."""
        with self._lock:
            state = self._orders.get(order_id)
            return None if state is None else _progress(state)

    def for_token(self, token_id: str) -> list[QueueProgress]:
        """Current positions of every order tracked on ``token_id``."""
        with self._lock:
            return [_progress(state) for state in self._states_for(token_id)]

    def snapshot(self) -> dict:
        """Plain, JSON-safe state for the live recorder."""
        with self._lock:
            return {
                "queue_orders": [self._record(state) for state in self._orders.values()],
                "queue_tokens": {
                    token_id: list(order_ids)
                    for token_id, order_ids in self._by_token.items()
                },
            }

    # ------------------------------------------------------------------ drops

    def drop(self, token_id: str, order_id: str) -> None:
        """Stop tracking ``order_id`` once it is no longer live on ``token_id``."""
        with self._lock:
            state = self._orders.get(order_id)
            if state is None or state.entry.token_id != token_id:
                return
            del self._orders[order_id]
            order_ids = self._by_token.get(token_id)
            if order_ids is not None:
                order_ids.pop(order_id, None)
                if not order_ids:
                    del self._by_token[token_id]

    def clear(self) -> None:
        """Forget every tracked order."""
        with self._lock:
            self._orders.clear()
            self._by_token.clear()

    # ---------------------------------------------------------------- private

    def _states_for(self, token_id: str) -> list[_OrderState]:
        """Tracked states for one token, in registration order (lock held)."""
        order_ids = self._by_token.get(token_id)
        if not order_ids:
            return []
        orders = self._orders
        return [orders[order_id] for order_id in order_ids if order_id in orders]

    @staticmethod
    def _record(state: _OrderState) -> dict:
        """One order as plain JSON-safe data."""
        progress = _progress(state)
        entry = state.entry
        return {
            "order_id": progress.order_id,
            "token_id": progress.token_id,
            "side": entry.side,
            "price": entry.price,
            "size": entry.size,
            "entered_ts": entry.entered_ts,
            "ahead": progress.ahead,
            "initial_ahead": progress.initial_ahead,
            "level_size": progress.level_size,
            "traded_at_level": progress.traded_at_level,
            "filled": progress.filled,
            "updated_ts": progress.updated_ts,
            "consumed_fraction": progress.consumed_fraction,
            "at_front": progress.at_front,
            "queue_position_inferred": progress.queue_position_inferred,
        }
