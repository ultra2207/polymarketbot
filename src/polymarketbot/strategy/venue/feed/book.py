"""L2 book aggregation: snapshot seeding and delta application."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from polymarketbot.logs import get_logger

from .models import (
    OrderBookLevel,
    OrderBookSnapshot,
    _parse_float,
    _pick,
    build_book_snapshot,
)

logger = get_logger("trading.feed")

@dataclass
class _TokenBook:
    """Maintained L2 depth for one token: rounded price -> aggregate size.

    Polymarket ``price_change`` deltas carry the *new aggregate size* at a price
    level (0 removes it), so the whole book can be reconstructed by applying
    them to a ``book`` snapshot. Prices are rounded to six decimals to match
    ``parse_levels`` and keep float keys stable across message encodings.
    """

    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    tick_size: float = 0.01
    timestamp: float = field(default_factory=time.time)
    hash: Optional[str] = None

    def apply_change(self, side: str, price: float, size: float) -> bool:
        """Apply one delta level. ``side`` is the ladder side ("bid"/"ask").

        Returns ``False`` when the delta is rejected. A bid at or above the best
        ask, or an ask at or below the best bid, crosses the book; publishing
        that state yields ``spread == 0``, and a zero spread makes the reward
        scorer's ``alpha = ((v-s)/v)^2`` equal to 1, i.e. a maximum score share
        on a book that does not exist. ``apply_top`` already trims levels on the
        wrong side of an asserted top; the delta path has to enforce the same
        invariant instead of corrupting the book with it.
        """
        levels = self.bids if side == "bid" else self.asks
        if size <= 0.0:
            levels.pop(price, None)
            return True
        if side == "bid" and self.asks and price >= min(self.asks):
            return False
        if side == "ask" and self.bids and price <= max(self.bids):
            return False
        levels[price] = size
        return True

    def apply_top(self, best_bid: Optional[float], best_ask: Optional[float]) -> None:
        """Move the top of book without touching deeper levels.

        A ``best_bid_ask`` frame asserts the best price but carries no size. The
        asserted price is kept as a zero-size level (explicitly "size unknown",
        never a fabricated 100) until a ``price_change`` sizes it; levels on the
        wrong side of the new top cannot exist and are trimmed.
        """
        if best_bid is not None and 0.0 < best_bid < 1.0:
            best_bid = round(best_bid, 6)
            for price in [p for p in self.bids if p > best_bid]:
                del self.bids[price]
            self.bids.setdefault(best_bid, 0.0)
        if best_ask is not None and 0.0 < best_ask < 1.0:
            best_ask = round(best_ask, 6)
            for price in [p for p in self.asks if p < best_ask]:
                del self.asks[price]
            self.asks.setdefault(best_ask, 0.0)

    def to_snapshot(
        self,
        asset_id: str,
        *,
        timestamp: Optional[float] = None,
        source: str = "ws",
    ) -> OrderBookSnapshot:
        """Build a snapshot of the maintained book.

        Zero-size levels are the ``best_bid_ask`` placeholder described above:
        they must surface as the best price, but they contribute no depth.
        """
        raw_bids = [{"price": p, "size": s} for p, s in self.bids.items()]
        raw_asks = [{"price": p, "size": s} for p, s in self.asks.items()]
        snapshot = build_book_snapshot(
            asset_id=asset_id,
            raw_bids=raw_bids,
            raw_asks=raw_asks,
            tick_size=self.tick_size,
            book_hash=self.hash,
            timestamp=timestamp if timestamp is not None else self.timestamp,
            source=source,
        )

        top_bid = max(self.bids) if self.bids else None
        top_ask = min(self.asks) if self.asks else None
        adjusted = False
        if top_bid is not None and top_bid > snapshot.best_bid:
            snapshot.bids.insert(0, OrderBookLevel(price=top_bid, size=0.0))
            snapshot.best_bid = round(top_bid, 4)
            adjusted = True
        if top_ask is not None and top_ask < snapshot.best_ask:
            snapshot.asks.insert(0, OrderBookLevel(price=top_ask, size=0.0))
            snapshot.best_ask = round(top_ask, 4)
            adjusted = True

        if adjusted:
            best_bid = snapshot.best_bid
            best_ask = snapshot.best_ask
            if best_bid > 0.0 and best_ask < 1.0 and best_ask >= best_bid:
                snapshot.mid = round((best_bid + best_ask) / 2.0, 4)
                snapshot.spread = round(best_ask - best_bid, 4)
            elif best_bid > 0.0:
                snapshot.mid = round(best_bid, 4)
                snapshot.spread = 0.0
            elif best_ask < 1.0:
                snapshot.mid = round(best_ask, 4)
                snapshot.spread = 0.0
            else:
                snapshot.mid = 0.50
                snapshot.spread = 0.0
            snapshot.spread_cents = round(snapshot.spread * 100.0, 2)

        return snapshot


class _BookAggregationMixin:
    """Maintained books shared by the WS delta path and the REST fallback."""

    # -------------------------------------------------------------------------
    # Maintained Order Books
    # -------------------------------------------------------------------------
    def _reset_book(self, asset_id: str, snapshot: OrderBookSnapshot) -> None:
        """Replace the maintained book for asset_id with a full snapshot."""
        book = _TokenBook(
            bids={level.price: level.size for level in snapshot.bids},
            asks={level.price: level.size for level in snapshot.asks},
            tick_size=snapshot.tick_size,
            timestamp=snapshot.timestamp,
            hash=snapshot.hash,
        )
        with self._book_lock:
            self._books[asset_id] = book

    def _clear_books(self) -> None:
        """Drop all maintained depth (used on WebSocket reconnect)."""
        with self._book_lock:
            self._books.clear()

    def _book_for(self, asset_id: str) -> _TokenBook:
        """Return the maintained book for asset_id, creating an empty one if needed."""
        book = self._books.get(asset_id)
        if book is None:
            book = _TokenBook()
            self._books[asset_id] = book
        return book

    def _apply_price_changes(
        self,
        default_asset_id: Optional[str],
        changes: list[Any],
    ) -> dict[str, float]:
        """Apply ``price_changes`` deltas to the maintained books.

        Returns ``asset_id -> event timestamp`` for every asset that changed so
        the caller can emit one snapshot per asset per message.
        """
        updated: dict[str, float] = {}
        with self._book_lock:
            for change in changes:
                if not isinstance(change, dict):
                    continue
                payload = change.get("payload") if isinstance(change.get("payload"), dict) else {}
                asset_id = _pick(change, payload, "asset_id", "assetId", default=default_asset_id)
                if not asset_id:
                    continue

                price = _parse_float(_pick(change, payload, "price"), default=-1.0)
                size = _parse_float(_pick(change, payload, "size"), default=-1.0)
                side = str(_pick(change, payload, "side") or "").upper()
                if not (0.0 < price < 1.0) or size < 0.0:
                    logger.debug(
                        f"Ignoring malformed price_change for {str(asset_id)[:12]}: price={price} size={size}"
                    )
                    continue
                # `side` is the ladder side: BUY rows are bids, SELL rows asks.
                if side == "BUY":
                    book_side = "bid"
                elif side == "SELL":
                    book_side = "ask"
                else:
                    logger.debug(f"Ignoring price_change with unknown side {side!r} for {str(asset_id)[:12]}")
                    continue

                ts_raw = _pick(change, payload, "timestamp")
                ts = (_parse_float(ts_raw) / 1000.0) if ts_raw else time.time()
                book = self._book_for(asset_id)
                if not book.apply_change(book_side, round(price, 6), size):
                    logger.warning(
                        f"Rejected crossing price_change for {str(asset_id)[:12]}: "
                        f"side={book_side} price={price} against best "
                        f"bid={max(book.bids) if book.bids else None} "
                        f"ask={min(book.asks) if book.asks else None}; "
                        "the book stays uncrossed and the next snapshot resynchronises"
                    )
                    continue
                book.timestamp = ts
                updated[asset_id] = ts
        return updated

    def _update_book_top(
        self,
        asset_id: str,
        best_bid: Optional[float],
        best_ask: Optional[float],
        timestamp: float,
    ) -> OrderBookSnapshot:
        """Apply a top-of-book update and return the maintained book snapshot."""
        with self._book_lock:
            book = self._book_for(asset_id)
            book.apply_top(best_bid, best_ask)
            book.timestamp = timestamp
            return book.to_snapshot(asset_id, timestamp=timestamp, source="ws")

    def _snapshot_from_book(
        self,
        asset_id: str,
        *,
        timestamp: Optional[float] = None,
        source: str = "ws",
    ) -> OrderBookSnapshot:
        """Emit the maintained book for asset_id as an OrderBookSnapshot."""
        with self._book_lock:
            return self._book_for(asset_id).to_snapshot(
                asset_id,
                timestamp=timestamp,
                source=source,
            )

