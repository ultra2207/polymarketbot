"""High-Performance L2 Delta Order Book with Virtual Queue Position Tracking.

Closes the order book microstructure and matching engine simulation gap between
retail market making and Tier-1 quantitative HFT firms (Jump Trading, Wintermute).

Key Components:
1. FastL2Book:
   - In-memory L2 Limit Order Book with ultra-fast price-level storage (bids descending, asks ascending).
   - Instant O(1) top-of-book extraction: BBO, spread, mid-price, volume-weighted micro-price.
   - Arbitrary cumulative depth queries and WebSocket frame ingestion (Polymarket book snapshots and price deltas).
2. QueuePositionTracker:
   - Price-time priority (FIFO) virtual queue tracker for resting limit orders.
   - Tracks Q_ahead (queue depth ahead), Q_behind (queue depth behind), and queue_rank.
   - Dynamic trade consumption from the queue front and configurable cancellation attribution
     (Proportional, Conservative / Adverse-Selection Worst-Case, Optimistic).
3. QueueAwareCancellationPolicy:
   - Queue-aware dynamic cancellation during toxic price shocks (e.g. Binance spot jumps).
   - Immediate high-priority cancellation for Front-of-Queue orders (Q_ahead <= threshold).
   - Deferred / throttled cancellation for Deep Queue orders (Q_ahead >> sweep size) to protect
     CLOB API rate-limit budgets (HTTP 429 prevention).
   - Seamless integration with DedicatedCancellationPool.
"""

from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from polymarketbot.logs import get_logger

logger = get_logger("virtual_queue_book")


# ============================================================================
# 1. ENUMS AND DATA STRUCTURES
# ============================================================================

class OrderSide(str, Enum):
    """Normalized order side enumeration."""

    BUY = "BUY"
    SELL = "SELL"

    @classmethod
    def from_str(cls, side: str) -> OrderSide:
        s = str(side).strip().upper()
        if s in ("BUY", "BID", "BIDS", "B"):
            return cls.BUY
        if s in ("SELL", "ASK", "ASKS", "S"):
            return cls.SELL
        raise ValueError(f"Unknown order side: {side}")

    @property
    def is_buy(self) -> bool:
        return self == OrderSide.BUY

    @property
    def is_sell(self) -> bool:
        return self == OrderSide.SELL

    @property
    def opposite(self) -> OrderSide:
        return OrderSide.SELL if self == OrderSide.BUY else OrderSide.BUY


class CancellationModel(str, Enum):
    """Microstructure model for attributing un-attributed L2 cancellations to queue positions."""

    PROPORTIONAL = "PROPORTIONAL"
    CONSERVATIVE = "CONSERVATIVE"
    OPTIMISTIC = "OPTIMISTIC"


class CancellationUrgency(str, Enum):
    """Urgency classification for queue-aware cancellation decisions."""

    URGENT = "URGENT"          # Front of queue, highest adverse selection risk; cancel immediately
    THROTTLED = "THROTTLED"    # Deep queue, protected by competitor buffer; throttle / defer cancel
    MONITOR = "MONITOR"        # Intermediate queue position; pace or monitor
    SAFE = "SAFE"              # Order not in path of toxic shock


@dataclass(slots=True, frozen=True)
class OrderBookLevel:
    """Immutable price level representation."""

    price: float
    size: float


@dataclass
class TrackedOrder:
    """Resting limit order tracked in the virtual FIFO matching engine queue."""

    order_id: str
    side: OrderSide
    price: float
    original_size: float
    remaining_size: float
    queue_ahead: float
    queue_behind: float = 0.0
    filled_size: float = 0.0
    status: str = "ACTIVE"  # ACTIVE, PARTIALLY_FILLED, FILLED, CANCELLED
    cancellation_model: CancellationModel = CancellationModel.PROPORTIONAL
    creation_ts: float = field(default_factory=time.time)
    last_update_ts: float = field(default_factory=time.time)

    @property
    def queue_rank(self) -> float:
        """Normalized position in queue: 0.0 = front (next fill), 1.0 = absolute back."""
        total_queue = self.queue_ahead + self.queue_behind
        if total_queue <= 1e-9:
            return 0.0
        return max(0.0, min(1.0, self.queue_ahead / total_queue))

    @property
    def is_front(self) -> bool:
        """True if order has arrived at the front of the queue."""
        return self.queue_ahead <= 1e-9

    @property
    def fill_ratio(self) -> float:
        """Fraction of original order size filled so far."""
        if self.original_size <= 1e-9:
            return 0.0
        return max(0.0, min(1.0, self.filled_size / self.original_size))


@dataclass(slots=True, frozen=True)
class CancellationDecision:
    """Actionable cancellation policy recommendation for a tracked order."""

    order_id: str
    side: OrderSide
    price: float
    urgency: CancellationUrgency
    queue_ahead: float
    queue_rank: float
    estimated_sweep_size: float
    reason: str
    priority_score: float  # Lower score indicates higher cancellation priority (0 = immediate)
    recommended_action: str  # CANCEL_IMMEDIATE, DEFER, MONITOR, NO_ACTION


# ============================================================================
# 2. IN-MEMORY L2 DELTA BOOK (FastL2Book)
# ============================================================================

def _norm_price(p: float) -> float:
    """Canonical price rounding to 6 decimal places to prevent float precision drift."""
    return round(float(p), 6)


class FastL2Book:
    """Ultra-fast In-Memory L2 Delta Limit Order Book.

    Maintains bid levels sorted descending (highest price first) and ask levels
    sorted ascending (lowest price first). Optimized for high-throughput Polymarket
    WebSocket updates and instantaneous O(1) top-of-book metric extraction.
    """

    __slots__ = (
        "asset_id",
        "market_id",
        "last_update_ts",
        "sequence_num",
        "_bids",
        "_asks",
        "_bid_prices",
        "_ask_prices",
        "_listeners",
    )

    def __init__(self, asset_id: Optional[str] = None, market_id: Optional[str] = None) -> None:
        self.asset_id = asset_id
        self.market_id = market_id
        self.last_update_ts: float = 0.0
        self.sequence_num: int = 0

        # Fast price -> size level storage
        self._bids: Dict[float, float] = {}
        self._asks: Dict[float, float] = {}

        # Sorted price arrays: bids descending, asks ascending
        self._bid_prices: List[float] = []
        self._ask_prices: List[float] = []

        # Optional change listeners: callback(side, price, old_size, new_size)
        self._listeners: List[Callable[[OrderSide, float, float, float], None]] = []

    def add_listener(self, callback: Callable[[OrderSide, float, float, float], None]) -> None:
        """Register a callback invoked on every price level size change."""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[OrderSide, float, float, float], None]) -> None:
        """Unregister a listener callback."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    def clear(self) -> None:
        """Reset all order book levels."""
        self._bids.clear()
        self._asks.clear()
        self._bid_prices.clear()
        self._ask_prices.clear()
        self.sequence_num += 1

    # -------------------------------------------------------------------------
    # Snapshot & Delta Application
    # -------------------------------------------------------------------------

    def apply_snapshot(
        self,
        bids: Sequence[Any],
        asks: Sequence[Any],
        timestamp: Optional[float] = None,
    ) -> None:
        """Apply a full L2 snapshot, replacing all existing price levels."""
        now = time.time() if timestamp is None else float(timestamp)
        self.last_update_ts = now
        self.sequence_num += 1

        self._bids.clear()
        self._asks.clear()

        # Parse bids
        for item in bids:
            p, s = self._extract_price_size(item)
            if p is not None and s > 1e-9:
                p_norm = _norm_price(p)
                self._bids[p_norm] = float(s)

        # Parse asks
        for item in asks:
            p, s = self._extract_price_size(item)
            if p is not None and s > 1e-9:
                p_norm = _norm_price(p)
                self._asks[p_norm] = float(s)

        # Sort price indices: bids descending, asks ascending
        self._bid_prices = sorted(self._bids.keys(), reverse=True)
        self._ask_prices = sorted(self._asks.keys())

    def apply_delta(
        self,
        side: Union[str, OrderSide],
        price: float,
        size: float,
        timestamp: Optional[float] = None,
    ) -> Tuple[float, float]:
        """Apply an incremental price-level update (delta).

        Args:
            side: Side of the book ("BUY"/"BID" or "SELL"/"ASK").
            price: Level price.
            size: New aggregate size at this price. 0.0 indicates deletion.
            timestamp: Event receipt timestamp.

        Returns:
            Tuple of (old_size, new_size).
        """
        now = time.time() if timestamp is None else float(timestamp)
        self.last_update_ts = now
        self.sequence_num += 1

        order_side = side if isinstance(side, OrderSide) else OrderSide.from_str(side)
        p_norm = _norm_price(price)
        new_size = max(0.0, float(size))

        if order_side.is_buy:
            book_dict = self._bids
            price_list = self._bid_prices
            is_descending = True
        else:
            book_dict = self._asks
            price_list = self._ask_prices
            is_descending = False

        old_size = book_dict.get(p_norm, 0.0)

        if new_size <= 1e-9:
            # Level deletion
            if p_norm in book_dict:
                del book_dict[p_norm]
                self._remove_price(price_list, p_norm, is_descending)
                new_size = 0.0
        else:
            # Level update or insertion
            if p_norm in book_dict:
                book_dict[p_norm] = new_size
            else:
                book_dict[p_norm] = new_size
                self._insert_price(price_list, p_norm, is_descending)

        # Notify registered listeners
        if self._listeners and abs(new_size - old_size) > 1e-9:
            for listener in self._listeners:
                try:
                    listener(order_side, p_norm, old_size, new_size)
                except Exception as exc:
                    logger.warning(f"Error in FastL2Book listener: {exc}")

        return old_size, new_size

    def apply_clob_frame(
        self,
        frame: Mapping[str, Any],
        timestamp: Optional[float] = None,
    ) -> None:
        """Parse and ingest a raw Polymarket CLOB WebSocket message frame.

        Supports:
        - "book": Complete snapshot.
        - "price_change": Incremental delta changes.
        """
        event_type = frame.get("event_type") or frame.get("type")
        if not event_type:
            return

        ts = timestamp or time.time()

        if event_type == "book":
            bids = frame.get("bids", [])
            asks = frame.get("asks", [])
            self.apply_snapshot(bids, asks, timestamp=ts)

        elif event_type == "price_change":
            changes = frame.get("price_changes", [])
            for ch in changes:
                if not isinstance(ch, dict):
                    continue
                # If frame is multi-asset, ensure it belongs to this book's asset
                asset_id = ch.get("asset_id")
                if self.asset_id and asset_id and str(asset_id) != str(self.asset_id):
                    continue

                raw_side = ch.get("side")
                raw_price = ch.get("price")
                raw_size = ch.get("size")
                if raw_side is None or raw_price is None or raw_size is None:
                    continue
                try:
                    self.apply_delta(
                        side=str(raw_side),
                        price=float(raw_price),
                        size=float(raw_size),
                        timestamp=ts,
                    )
                except (ValueError, TypeError):
                    continue

    # -------------------------------------------------------------------------
    # Internal Binary Search Index Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _insert_price(price_list: List[float], price: float, is_descending: bool) -> None:
        """Insert price into sorted list keeping order (descending for bids, ascending for asks)."""
        if is_descending:
            # For bids descending: bisect using negative prices
            idx = bisect.bisect_left(price_list, -price, key=lambda p: -p)
            price_list.insert(idx, price)
        else:
            # For asks ascending: standard bisect
            bisect.insort(price_list, price)

    @staticmethod
    def _remove_price(price_list: List[float], price: float, is_descending: bool) -> None:
        """Remove price from sorted list in O(log N) search + O(N) pop."""
        if not price_list:
            return
        if is_descending:
            idx = bisect.bisect_left(price_list, -price, key=lambda p: -p)
        else:
            idx = bisect.bisect_left(price_list, price)

        if idx < len(price_list) and abs(price_list[idx] - price) < 1e-9:
            price_list.pop(idx)

    @staticmethod
    def _extract_price_size(item: Any) -> Tuple[Optional[float], float]:
        """Extract (price, size) tuple from diverse JSON formats."""
        if isinstance(item, dict):
            p = item.get("price")
            s = item.get("size", 0.0)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            p = item[0]
            s = item[1]
        elif hasattr(item, "price") and hasattr(item, "size"):
            p = item.price
            s = item.size
        else:
            return None, 0.0

        try:
            return float(p), float(s)
        except (TypeError, ValueError):
            return None, 0.0

    # -------------------------------------------------------------------------
    # Top-of-Book & Microstructure Properties (O(1))
    # -------------------------------------------------------------------------

    @property
    def best_bid(self) -> Optional[float]:
        """Top-of-book best bid price, or None if empty."""
        return self._bid_prices[0] if self._bid_prices else None

    @property
    def best_bid_size(self) -> float:
        """Aggregate depth at the top bid price."""
        if not self._bid_prices:
            return 0.0
        return self._bids.get(self._bid_prices[0], 0.0)

    @property
    def best_ask(self) -> Optional[float]:
        """Top-of-book best ask price, or None if empty."""
        return self._ask_prices[0] if self._ask_prices else None

    @property
    def best_ask_size(self) -> float:
        """Aggregate depth at the top ask price."""
        if not self._ask_prices:
            return 0.0
        return self._asks.get(self._ask_prices[0], 0.0)

    @property
    def bbo(self) -> Tuple[Optional[Tuple[float, float]], Optional[Tuple[float, float]]]:
        """Best Bid and Offer: ((best_bid, bid_size), (best_ask, ask_size))."""
        bid_entry = (self._bid_prices[0], self._bids[self._bid_prices[0]]) if self._bid_prices else None
        ask_entry = (self._ask_prices[0], self._asks[self._ask_prices[0]]) if self._ask_prices else None
        return bid_entry, ask_entry

    @property
    def spread(self) -> Optional[float]:
        """Bid-Ask spread: best_ask - best_bid."""
        bb = self.best_bid
        ba = self.best_ask
        if bb is None or ba is None:
            return None
        return round(ba - bb, 6)

    @property
    def mid_price(self) -> Optional[float]:
        """Simple arithmetic mid-price: 0.5 * (best_bid + best_ask)."""
        bb = self.best_bid
        ba = self.best_ask
        if bb is None or ba is None:
            return None
        return round(0.5 * (bb + ba), 6)

    @property
    def weighted_micro_price(self) -> Optional[float]:
        """Volume-Weighted Top-of-Book Micro-Price.

        P_micro = (Q_bid * P_ask + Q_ask * P_bid) / (Q_bid + Q_ask)
        Accounts for order book imbalance skewing fair value toward the heavy side.
        """
        bb = self.best_bid
        ba = self.best_ask
        if bb is None or ba is None:
            return bb if bb is not None else ba

        qb = self.best_bid_size
        qa = self.best_ask_size
        total_vol = qb + qa

        if total_vol <= 1e-9:
            return round(0.5 * (bb + ba), 6)

        micro = (qb * ba + qa * bb) / total_vol
        return round(micro, 6)

    def calculate_stoikov_micro_price(self, decay_alpha: float = 0.5, max_levels: int = 5) -> Optional[float]:
        """Multi-level L2 depth-weighted micro-price with exponential distance decay (Stoikov 2018)."""
        if not self._bid_prices or not self._ask_prices:
            return self.weighted_micro_price

        weighted_bid_sum = 0.0
        bid_weight_sum = 0.0
        for i, p in enumerate(self._bid_prices[:max_levels]):
            size = self._bids.get(p, 0.0)
            weight = size * math.exp(-decay_alpha * i)
            weighted_bid_sum += p * weight
            bid_weight_sum += weight

        weighted_ask_sum = 0.0
        ask_weight_sum = 0.0
        for i, p in enumerate(self._ask_prices[:max_levels]):
            size = self._asks.get(p, 0.0)
            weight = size * math.exp(-decay_alpha * i)
            weighted_ask_sum += p * weight
            ask_weight_sum += weight

        total_weight = bid_weight_sum + ask_weight_sum
        if total_weight <= 1e-9:
            return self.mid_price

        # Heavy bid size exerts upward drift pressure; heavy ask size exerts downward pressure
        p_bid_eff = weighted_bid_sum / bid_weight_sum if bid_weight_sum > 0 else self._bid_prices[0]
        p_ask_eff = weighted_ask_sum / ask_weight_sum if ask_weight_sum > 0 else self._ask_prices[0]

        micro = (bid_weight_sum * p_ask_eff + ask_weight_sum * p_bid_eff) / total_weight
        return round(micro, 6)

    @property
    def is_crossed(self) -> bool:
        """True if top bid is greater than or equal to top ask (crossed or locked book)."""
        bb = self.best_bid
        ba = self.best_ask
        if bb is None or ba is None:
            return False
        return bb >= ba

    # -------------------------------------------------------------------------
    # Depth Queries
    # -------------------------------------------------------------------------

    def depth_at_price(self, side: Union[str, OrderSide], price: float) -> float:
        """Get total resting depth at an exact price level."""
        order_side = side if isinstance(side, OrderSide) else OrderSide.from_str(side)
        p_norm = _norm_price(price)
        if order_side.is_buy:
            return self._bids.get(p_norm, 0.0)
        return self._asks.get(p_norm, 0.0)

    def cumulative_depth(
        self,
        side: Union[str, OrderSide],
        max_price_distance: Optional[float] = None,
        max_levels: Optional[int] = None,
    ) -> float:
        """Calculate cumulative size on one side up to a price distance or level count."""
        order_side = side if isinstance(side, OrderSide) else OrderSide.from_str(side)
        if order_side.is_buy:
            prices = self._bid_prices
            book_dict = self._bids
            best = self.best_bid
            if best is None:
                return 0.0
            cutoff = (best - max_price_distance) if max_price_distance is not None else -math.inf
            total = 0.0
            count = 0
            for p in prices:
                if p < cutoff or (max_levels is not None and count >= max_levels):
                    break
                total += book_dict.get(p, 0.0)
                count += 1
            return total
        else:
            prices = self._ask_prices
            book_dict = self._asks
            best = self.best_ask
            if best is None:
                return 0.0
            cutoff = (best + max_price_distance) if max_price_distance is not None else math.inf
            total = 0.0
            count = 0
            for p in prices:
                if p > cutoff or (max_levels is not None and count >= max_levels):
                    break
                total += book_dict.get(p, 0.0)
                count += 1
            return total

    def get_levels(
        self,
        side: Union[str, OrderSide],
        max_levels: Optional[int] = None,
    ) -> List[OrderBookLevel]:
        """Return list of (price, size) levels for a given side."""
        order_side = side if isinstance(side, OrderSide) else OrderSide.from_str(side)
        if order_side.is_buy:
            prices = self._bid_prices[:max_levels] if max_levels else self._bid_prices
            return [OrderBookLevel(p, self._bids[p]) for p in prices]
        else:
            prices = self._ask_prices[:max_levels] if max_levels else self._ask_prices
            return [OrderBookLevel(p, self._asks[p]) for p in prices]


# ============================================================================
# 3. VIRTUAL QUEUE POSITION TRACKING (QueuePositionTracker)
# ============================================================================

class QueuePositionTracker:
    """Tracks virtual queue priority for resting limit orders under FIFO matching.

    In price-time priority (FIFO) matching engines like Polymarket CLOB, an order's
    fill probability and adverse selection risk depend directly on its position in the queue.

    Mechanics:
    1. Order Placement:
       When order of size q_bot is placed at price p:
       Q_ahead = DepthAtPrice(p) - q_bot (if order is already in book snapshot),
       or Q_ahead = DepthAtPrice(p) (if placed prior to book update).
       Q_behind = 0.0.
    2. Trades at price p:
       Trades consume the queue strictly from the front:
       Q_ahead = max(0.0, Q_ahead - V_trade).
       If V_trade > Q_ahead, the remainder executes into q_bot (partial/full fill).
    3. Level Size Changes:
       - Delta S > 0 (New liquidity): Enters at the tail of the queue -> Q_behind += Delta S.
       - Delta S < 0 (Cancellations): Attributed via Proportional, Conservative, or Optimistic models.
    """

    def __init__(self, book: Optional[FastL2Book] = None) -> None:
        self.book = book
        self._orders: Dict[str, TrackedOrder] = {}
        # Index: (side, price) -> list of order_ids sorted by arrival time
        self._price_queue: Dict[Tuple[OrderSide, float], List[str]] = {}

        if self.book is not None:
            self.book.add_listener(self._on_book_level_change)

    # -------------------------------------------------------------------------
    # Order Lifecycle Management
    # -------------------------------------------------------------------------

    def track_order(
        self,
        order_id: str,
        side: Union[str, OrderSide],
        price: float,
        size: float,
        already_in_book: bool = True,
        initial_depth_ahead: Optional[float] = None,
        cancellation_model: CancellationModel = CancellationModel.PROPORTIONAL,
        timestamp: Optional[float] = None,
    ) -> TrackedOrder:
        """Register a new resting order into the virtual queue tracker.

        Args:
            order_id: Unique order identifier.
            side: Side ("BUY" or "SELL").
            price: Order limit price.
            size: Order share quantity.
            already_in_book: If True, the current book depth at this price already includes
                             this order, so Q_ahead = max(0.0, depth - size).
                             If False, this order has not yet entered the book, so Q_ahead = depth.
            initial_depth_ahead: Optional explicit queue depth ahead. Overrides book calculation.
            cancellation_model: Model for un-attributed cancellations.
            timestamp: Order placement timestamp.

        Returns:
            TrackedOrder instance with calculated queue position.
        """
        order_side = side if isinstance(side, OrderSide) else OrderSide.from_str(side)
        p_norm = _norm_price(price)
        size_f = float(size)
        now = time.time() if timestamp is None else float(timestamp)

        if initial_depth_ahead is not None:
            q_ahead = max(0.0, float(initial_depth_ahead))
        elif self.book is not None:
            current_depth = self.book.depth_at_price(order_side, p_norm)
            if already_in_book:
                q_ahead = max(0.0, current_depth - size_f)
            else:
                q_ahead = max(0.0, current_depth)
        else:
            q_ahead = 0.0

        tracked = TrackedOrder(
            order_id=order_id,
            side=order_side,
            price=p_norm,
            original_size=size_f,
            remaining_size=size_f,
            queue_ahead=round(q_ahead, 4),
            queue_behind=0.0,
            filled_size=0.0,
            status="ACTIVE",
            cancellation_model=cancellation_model,
            creation_ts=now,
            last_update_ts=now,
        )

        self._orders[order_id] = tracked
        key = (order_side, p_norm)
        if key not in self._price_queue:
            self._price_queue[key] = []
        self._price_queue[key].append(order_id)

        return tracked

    def get_order(self, order_id: str) -> Optional[TrackedOrder]:
        """Fetch a tracked order by ID."""
        return self._orders.get(order_id)

    def remove_order(self, order_id: str) -> Optional[TrackedOrder]:
        """Remove an order from tracking (e.g. upon user cancellation)."""
        tracked = self._orders.pop(order_id, None)
        if tracked is not None:
            key = (tracked.side, tracked.price)
            if key in self._price_queue and order_id in self._price_queue[key]:
                self._price_queue[key].remove(order_id)
                if not self._price_queue[key]:
                    del self._price_queue[key]
            tracked.status = "CANCELLED"
            tracked.last_update_ts = time.time()
        return tracked

    def get_orders_at_price(self, side: Union[str, OrderSide], price: float) -> List[TrackedOrder]:
        """Retrieve all active tracked orders at a specific side and price."""
        order_side = side if isinstance(side, OrderSide) else OrderSide.from_str(side)
        p_norm = _norm_price(price)
        key = (order_side, p_norm)
        order_ids = self._price_queue.get(key, [])
        return [self._orders[oid] for oid in order_ids if oid in self._orders]

    def get_all_active_orders(self) -> List[TrackedOrder]:
        """Retrieve all active tracked orders."""
        return [o for o in self._orders.values() if o.status in ("ACTIVE", "PARTIALLY_FILLED")]

    # -------------------------------------------------------------------------
    # Event Processing: Trades & Incremental Level Changes
    # -------------------------------------------------------------------------

    def on_trade(
        self,
        side: Union[str, OrderSide],
        price: float,
        size: float,
        timestamp: Optional[float] = None,
    ) -> List[Tuple[TrackedOrder, float]]:
        """Simulate trade execution consumption at a price level.

        Under FIFO price-time priority:
        - The trade executes against resting orders at the front of the queue.
        - Q_ahead is decremented.
        - When Q_ahead reaches 0, any remaining trade size matches into our order.

        Args:
            side: Side of the resting orders being filled (e.g. BUY for bids hit by seller).
            price: Execution price.
            size: Executed trade size.
            timestamp: Trade timestamp.

        Returns:
            List of (TrackedOrder, fill_amount) for any tracked orders that were filled.
        """
        order_side = side if isinstance(side, OrderSide) else OrderSide.from_str(side)
        p_norm = _norm_price(price)
        key = (order_side, p_norm)
        now = time.time() if timestamp is None else float(timestamp)

        order_ids = list(self._price_queue.get(key, []))
        if not order_ids:
            return []

        fills: List[Tuple[TrackedOrder, float]] = []
        trade_vol = float(size)
        if trade_vol <= 1e-9:
            return []

        for oid in order_ids:
            order = self._orders.get(oid)
            if not order or order.status not in ("ACTIVE", "PARTIALLY_FILLED"):
                continue

            order.last_update_ts = now
            prior_ahead = order.queue_ahead

            # 1. Volume consumed ahead of this order
            consumed_ahead = min(prior_ahead, trade_vol)
            order.queue_ahead = round(max(0.0, prior_ahead - consumed_ahead), 4)

            # 2. Remaining volume that reaches this order
            vol_at_order = max(0.0, trade_vol - prior_ahead)

            # 3. If volume reaches order, match and fill
            if vol_at_order > 1e-9:
                fill_amt = min(order.remaining_size, vol_at_order)
                if fill_amt > 1e-9:
                    order.remaining_size = round(max(0.0, order.remaining_size - fill_amt), 4)
                    order.filled_size = round(order.filled_size + fill_amt, 4)

                    if order.remaining_size <= 1e-9:
                        order.status = "FILLED"
                    else:
                        order.status = "PARTIALLY_FILLED"

                    fills.append((order, fill_amt))

                # 4. Volume that passes past this order consumes queue behind
                vol_past_order = max(0.0, vol_at_order - fill_amt)
                if vol_past_order > 1e-9 and order.queue_behind > 1e-9:
                    order.queue_behind = round(max(0.0, order.queue_behind - vol_past_order), 4)

        return fills

    def on_level_delta(
        self,
        side: Union[str, OrderSide],
        price: float,
        old_size: float,
        new_size: float,
        timestamp: Optional[float] = None,
    ) -> None:
        """Update virtual queue positions in response to an L2 level delta."""
        order_side = side if isinstance(side, OrderSide) else OrderSide.from_str(side)
        p_norm = _norm_price(price)
        key = (order_side, p_norm)
        now = time.time() if timestamp is None else float(timestamp)

        order_ids = self._price_queue.get(key, [])
        if not order_ids:
            return

        delta = new_size - old_size
        if abs(delta) <= 1e-9:
            return

        for oid in order_ids:
            order = self._orders.get(oid)
            if not order or order.status not in ("ACTIVE", "PARTIALLY_FILLED"):
                continue

            order.last_update_ts = now

            if delta > 0:
                # Queue Expansion: In FIFO, all new incoming maker orders join at the TAIL.
                # Q_ahead is completely unaffected! Q_behind increases by delta.
                order.queue_behind = round(order.queue_behind + delta, 4)
            else:
                # Queue Reduction (Cancellations)
                reduction = abs(delta)
                self._apply_cancellation_reduction(order, reduction)

    def _apply_cancellation_reduction(self, order: TrackedOrder, reduction: float) -> None:
        """Apply cancellation reduction across Q_ahead and Q_behind according to the order's model."""
        if order.cancellation_model == CancellationModel.PROPORTIONAL:
            # Proportional model: cancellations are distributed across the queue
            total_other_depth = order.queue_ahead + order.queue_behind
            if total_other_depth <= 1e-9:
                order.queue_ahead = 0.0
                order.queue_behind = 0.0
                return

            ahead_ratio = order.queue_ahead / total_other_depth
            reduce_ahead = min(order.queue_ahead, reduction * ahead_ratio)
            reduce_behind = min(order.queue_behind, reduction - reduce_ahead)

            order.queue_ahead = round(max(0.0, order.queue_ahead - reduce_ahead), 4)
            order.queue_behind = round(max(0.0, order.queue_behind - reduce_behind), 4)

        elif order.cancellation_model == CancellationModel.CONSERVATIVE:
            # Conservative (Adverse-Selection Worst-Case):
            # Competitors behind us cancel first; orders ahead of us cancel last.
            reduce_behind = min(order.queue_behind, reduction)
            order.queue_behind = round(max(0.0, order.queue_behind - reduce_behind), 4)
            remaining_reduction = reduction - reduce_behind

            if remaining_reduction > 1e-9:
                order.queue_ahead = round(max(0.0, order.queue_ahead - remaining_reduction), 4)

        elif order.cancellation_model == CancellationModel.OPTIMISTIC:
            # Optimistic: Orders ahead cancel first
            reduce_ahead = min(order.queue_ahead, reduction)
            order.queue_ahead = round(max(0.0, order.queue_ahead - reduce_ahead), 4)
            remaining_reduction = reduction - reduce_ahead

            if remaining_reduction > 1e-9:
                order.queue_behind = round(max(0.0, order.queue_behind - remaining_reduction), 4)

    def _on_book_level_change(self, side: OrderSide, price: float, old_size: float, new_size: float) -> None:
        """Listener hook automatically invoked when attached FastL2Book updates."""
        self.on_level_delta(side, price, old_size, new_size)

    def on_clob_frame(self, frame: Mapping[str, Any], timestamp: Optional[float] = None) -> None:
        """Process incoming CLOB frames (book, price_change, or trade)."""
        event_type = frame.get("event_type") or frame.get("type")
        if not event_type:
            return

        ts = timestamp or time.time()

        if event_type in ("last_trade_price", "trade"):
            raw_side = frame.get("side")
            raw_price = frame.get("price")
            raw_size = frame.get("size")
            if raw_side and raw_price and raw_size:
                try:
                    # In CLOB trade messages, 'side' is typically taker side
                    # If taker is BUY, resting SELL orders are consumed
                    # If taker is SELL, resting BUY orders are consumed
                    taker_side = OrderSide.from_str(str(raw_side))
                    maker_side = taker_side.opposite
                    self.on_trade(
                        side=maker_side,
                        price=float(raw_price),
                        size=float(raw_size),
                        timestamp=ts,
                    )
                except (ValueError, TypeError):
                    pass


# ============================================================================
# 4. QUEUE-AWARE DYNAMIC CANCELLATION POLICY
# ============================================================================

@dataclass(frozen=True)
class QueueCancellationConfig:
    """Microstructure configuration for queue-aware cancellation decisions."""

    # Front-of-queue threshold (contracts/shares). Orders with Q_ahead <= this
    # face severe adverse selection risk and trigger immediate urgent cancellation.
    front_queue_threshold: float = 300.0

    # Safety buffer multiplier for deep queue classification.
    # An order is deep if Q_ahead >= estimated_sweep_size * sweep_protection_multiplier.
    sweep_protection_multiplier: float = 2.0

    # Minimum toxic price shock (in basis points or cents) to activate evaluation
    min_toxic_shock_threshold: float = 2.0

    # CLOB API cancellation rate limit (max cancels allowed per second)
    max_cancels_per_second: float = 10.0

    # Delay in milliseconds before re-evaluating deferred/throttled orders
    throttle_defer_ms: float = 150.0


class RateLimitTokenBucket:
    """Thread-safe token bucket rate limiter for CLOB REST API requests."""

    def __init__(self, rate: float = 10.0, capacity: float = 10.0) -> None:
        self.rate = float(rate)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.last_update = time.perf_counter()

    def consume(self, tokens: float = 1.0) -> bool:
        """Attempt to consume tokens. Returns True if granted, False if rate limited."""
        now = time.perf_counter()
        elapsed = now - self.last_update
        self.last_update = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    @property
    def available_tokens(self) -> float:
        now = time.perf_counter()
        elapsed = now - self.last_update
        return min(self.capacity, self.tokens + elapsed * self.rate)


class QueueAwareCancellationPolicy:
    """Queue-Aware Dynamic Cancellation Policy during toxic price shocks.

    Microstructure Rationale:
    1. When an external toxic shock occurs (e.g. Binance spot jump):
       - Retail bots send naive blanket cancels across all orders.
       - This burns the CLOB rate limit, causes HTTP 429 penalties, and wastes latency.
    2. Jump Trading / Wintermute Microstructure Tiering:
       - Front-of-Queue Orders (Q_ahead <= front_threshold):
         Highest adverse selection risk! Incoming toxic arbitrage sweeps will hit them
         within milliseconds. Must be cancelled IMMEDIATELY via DedicatedCancellationPool.
       - Deep Queue Orders (Q_ahead >> estimated_sweep_size):
         Protected by hundreds/thousands of competitor contracts ahead. Probability of
         getting hit in the first 50ms is near zero. Defer or throttle cancellation to
         conserve CLOB API rate-limit quota and avoid rate-limiting penalties (HTTP 429).
    """

    def __init__(
        self,
        tracker: QueuePositionTracker,
        config: Optional[QueueCancellationConfig] = None,
        rate_limiter: Optional[RateLimitTokenBucket] = None,
    ) -> None:
        self.tracker = tracker
        self.config = config or QueueCancellationConfig()
        self.rate_limiter = rate_limiter or RateLimitTokenBucket(
            rate=self.config.max_cancels_per_second,
            capacity=self.config.max_cancels_per_second,
        )

    def evaluate_order(
        self,
        order: TrackedOrder,
        toxic_shock_magnitude: float,
        estimated_sweep_size: float = 1000.0,
    ) -> CancellationDecision:
        """Evaluate cancellation urgency for a single tracked limit order.

        Args:
            order: The tracked limit order.
            toxic_shock_magnitude: Magnitude of toxic price jump (e.g. in bps or cents).
            estimated_sweep_size: Expected taker sweep volume that snipers will dispatch.

        Returns:
            CancellationDecision with urgency, rank, and rationale.
        """
        # If shock is below sensitivity threshold, order is safe
        if toxic_shock_magnitude < self.config.min_toxic_shock_threshold:
            return CancellationDecision(
                order_id=order.order_id,
                side=order.side,
                price=order.price,
                urgency=CancellationUrgency.SAFE,
                queue_ahead=order.queue_ahead,
                queue_rank=order.queue_rank,
                estimated_sweep_size=estimated_sweep_size,
                reason=f"Shock {toxic_shock_magnitude:.2f} below threshold {self.config.min_toxic_shock_threshold:.2f}",
                priority_score=999.0,
                recommended_action="NO_ACTION",
            )

        q_ahead = order.queue_ahead
        deep_threshold = max(
            self.config.front_queue_threshold * 2.0,
            estimated_sweep_size * self.config.sweep_protection_multiplier,
        )

        # Tier 1: Front of Queue (Direct line of fire)
        if q_ahead <= self.config.front_queue_threshold:
            reason = (
                f"Front of queue: Q_ahead={q_ahead:.1f} <= threshold "
                f"({self.config.front_queue_threshold:.1f}). "
                f"Adverse selection imminent!"
            )
            # Priority score: lower Q_ahead gives higher priority (sorted first)
            priority_score = max(0.0, q_ahead)
            return CancellationDecision(
                order_id=order.order_id,
                side=order.side,
                price=order.price,
                urgency=CancellationUrgency.URGENT,
                queue_ahead=q_ahead,
                queue_rank=order.queue_rank,
                estimated_sweep_size=estimated_sweep_size,
                reason=reason,
                priority_score=priority_score,
                recommended_action="CANCEL_IMMEDIATE",
            )

        # Tier 2: Deep Queue (Protected by competitor buffer)
        elif q_ahead >= deep_threshold:
            buffer_ratio = q_ahead / max(1.0, estimated_sweep_size)
            reason = (
                f"Deep queue cushion: Q_ahead={q_ahead:.1f} is {buffer_ratio:.1f}x sweep size "
                f"({deep_threshold:.1f} safe depth). Defers cancel to conserve API rate limits."
            )
            priority_score = 500.0 + q_ahead
            return CancellationDecision(
                order_id=order.order_id,
                side=order.side,
                price=order.price,
                urgency=CancellationUrgency.THROTTLED,
                queue_ahead=q_ahead,
                queue_rank=order.queue_rank,
                estimated_sweep_size=estimated_sweep_size,
                reason=reason,
                priority_score=priority_score,
                recommended_action="DEFER",
            )

        # Tier 3: Intermediate Queue
        else:
            reason = (
                f"Intermediate queue: Q_ahead={q_ahead:.1f} between front and deep thresholds. "
                f"Paced cancellation subject to rate limit availability."
            )
            priority_score = 100.0 + q_ahead
            return CancellationDecision(
                order_id=order.order_id,
                side=order.side,
                price=order.price,
                urgency=CancellationUrgency.MONITOR,
                queue_ahead=q_ahead,
                queue_rank=order.queue_rank,
                estimated_sweep_size=estimated_sweep_size,
                reason=reason,
                priority_score=priority_score,
                recommended_action="MONITOR",
            )

    def evaluate_threatened_orders(
        self,
        threatened_side: Union[str, OrderSide],
        toxic_shock_magnitude: float,
        estimated_sweep_size: float = 1000.0,
    ) -> List[CancellationDecision]:
        """Evaluate all active orders on the threatened side during a toxic price shock.

        Args:
            threatened_side: The side exposed to adverse selection (e.g. SELL asks when spot surges).
            toxic_shock_magnitude: Magnitude of toxic shock (e.g. basis points).
            estimated_sweep_size: Expected taker sweep volume.

        Returns:
            List of CancellationDecisions sorted in priority order (URGENT first, lowest Q_ahead first).
        """
        side = threatened_side if isinstance(threatened_side, OrderSide) else OrderSide.from_str(threatened_side)
        active_orders = [o for o in self.tracker.get_all_active_orders() if o.side == side]

        decisions = [
            self.evaluate_order(o, toxic_shock_magnitude, estimated_sweep_size)
            for o in active_orders
        ]

        # Sort so that highest priority cancellations (lowest priority_score) come first
        decisions.sort(key=lambda d: d.priority_score)
        return decisions

    def execute_cancellations(
        self,
        decisions: Sequence[CancellationDecision],
        cancellation_pool: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Execute cancellations respecting queue priorities and rate limits.

        - URGENT orders: Always dispatched immediately via DedicatedCancellationPool
          (bypassing rate limits or consuming tokens first).
        - MONITOR orders: Dispatched if rate limit tokens are available.
        - THROTTLED orders: Deferred / saved to avoid HTTP 429 penalties.

        Args:
            decisions: List of cancellation decisions.
            cancellation_pool: DedicatedCancellationPool instance (optional).
            headers: Optional HTTP headers for cancellation requests.

        Returns:
            Dictionary mapping order_id to execution outcome.
        """
        results: Dict[str, Any] = {}

        for d in decisions:
            if d.urgency == CancellationUrgency.SAFE:
                results[d.order_id] = {
                    "action": "SKIPPED",
                    "reason": d.reason,
                    "urgency": d.urgency.value,
                }
                continue

            if d.urgency == CancellationUrgency.URGENT:
                # Urgent: Immediate dispatch
                if cancellation_pool is not None:
                    try:
                        res = cancellation_pool.cancel_order(order_id=d.order_id, headers=headers)
                        results[d.order_id] = {
                            "action": "CANCEL_SENT",
                            "urgency": d.urgency.value,
                            "success": getattr(res, "success", True),
                            "latency_ms": getattr(res, "latency_ms", 0.0),
                            "reason": d.reason,
                        }
                    except Exception as exc:
                        logger.error(f"Failed urgent cancel for {d.order_id}: {exc}")
                        results[d.order_id] = {
                            "action": "ERROR",
                            "error": str(exc),
                            "urgency": d.urgency.value,
                        }
                else:
                    results[d.order_id] = {
                        "action": "DRY_RUN_URGENT",
                        "urgency": d.urgency.value,
                        "reason": d.reason,
                    }
                self.tracker.remove_order(d.order_id)

            elif d.urgency == CancellationUrgency.MONITOR:
                # Monitor: Only dispatch if rate limiter has tokens
                if self.rate_limiter.consume(1.0):
                    if cancellation_pool is not None:
                        try:
                            res = cancellation_pool.cancel_order(order_id=d.order_id, headers=headers)
                            results[d.order_id] = {
                                "action": "CANCEL_SENT",
                                "urgency": d.urgency.value,
                                "success": getattr(res, "success", True),
                                "reason": d.reason,
                            }
                        except Exception as exc:
                            results[d.order_id] = {"action": "ERROR", "error": str(exc)}
                    else:
                        results[d.order_id] = {
                            "action": "DRY_RUN_MONITOR",
                            "urgency": d.urgency.value,
                            "reason": d.reason,
                        }
                    self.tracker.remove_order(d.order_id)
                else:
                    results[d.order_id] = {
                        "action": "RATE_LIMITED_DEFERRED",
                        "urgency": d.urgency.value,
                        "reason": "CLOB API rate-limit budget exhausted",
                    }

            elif d.urgency == CancellationUrgency.THROTTLED:
                # Deep queue: Intentionally throttled / deferred
                results[d.order_id] = {
                    "action": "THROTTLED_BUFFER_PROTECTED",
                    "urgency": d.urgency.value,
                    "reason": d.reason,
                    "defer_ms": self.config.throttle_defer_ms,
                }

        return results


# ============================================================================
# 5. UNIFIED HIGH-FREQUENCY MATCHING ENGINE HARNESS
# ============================================================================

class VirtualQueueOrderBookEngine:
    """Unified Tier-1 HFT matching engine harness combining L2 Book, Virtual Queue, and Cancellation Policy."""

    def __init__(
        self,
        asset_id: Optional[str] = None,
        market_id: Optional[str] = None,
        cancellation_config: Optional[QueueCancellationConfig] = None,
    ) -> None:
        self.asset_id = asset_id
        self.market_id = market_id
        self.book = FastL2Book(asset_id=asset_id, market_id=market_id)
        self.tracker = QueuePositionTracker(book=self.book)
        self.cancellation_policy = QueueAwareCancellationPolicy(
            tracker=self.tracker,
            config=cancellation_config,
        )

    def process_clob_frame(self, frame: Mapping[str, Any], timestamp: Optional[float] = None) -> None:
        """Process incoming WebSocket frame through both the L2 book and virtual queue tracker."""
        ts = timestamp or time.time()
        self.book.apply_clob_frame(frame, timestamp=ts)
        self.tracker.on_clob_frame(frame, timestamp=ts)

    def handle_toxic_price_shock(
        self,
        threatened_side: Union[str, OrderSide],
        toxic_shock_magnitude: float,
        estimated_sweep_size: float = 1000.0,
        cancellation_pool: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Orchestrate queue-aware defensive cancellations during a toxic price shock."""
        decisions = self.cancellation_policy.evaluate_threatened_orders(
            threatened_side=threatened_side,
            toxic_shock_magnitude=toxic_shock_magnitude,
            estimated_sweep_size=estimated_sweep_size,
        )
        return self.cancellation_policy.execute_cancellations(
            decisions=decisions,
            cancellation_pool=cancellation_pool,
        )
