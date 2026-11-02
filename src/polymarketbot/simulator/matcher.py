"""Native Self-Hosted Polymarket Order Matcher and Execution Simulator.

Implements high-fidelity virtual trade matching against resting quotes and taker
market order book-walking with endogenous market impact.

Features:
- SyntheticOrderBook: L2 depth state tracking, queue ahead calculation, and depth walking.
- MarketImpactEngine: Slippage, square-root law impact, and post-fill price displacement.
- Fill execution cases:
  1. Normal trade at price P: fills only after queue_ahead is exhausted.
  2. Aggressive cross-through (trade_price < bid_price or trade_price > ask_price):
     triggers instant aggressive maker fill.
  3. Taker market order execution: immediately crosses spread at best ask/bid with
     depth-walked VWAP slippage and book depletion.
- Generates deterministic FillEvent audit records.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from polymarketbot.logs import get_logger
from polymarketbot.simulator.feed import OrderBookLevel, OrderBookSnapshot

logger = get_logger("simulator.matcher")


@dataclass
class FillEvent:
    """Deterministic record of an executed simulated trade fill."""

    fill_id: str
    timestamp: float
    asset_id: str
    side: str  # "BUY" or "SELL"
    fill_price: float
    fill_size: float
    notional_usd: float
    slippage: float  # Absolute slippage in price units (cents = slippage * 100)
    liquidity_type: str  # "MAKER" or "TAKER"
    queue_depleted: bool  # True if queue_ahead was exhausted or crossed through
    post_fill_impact: float  # Price displacement caused by this execution
    fee_usd: float = 0.0
    trigger_reason: str = ""
    order_id: Optional[str] = None
    remaining_quote_size: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "timestamp": self.timestamp,
            "asset_id": self.asset_id,
            "side": self.side.upper(),
            "fill_price": round(self.fill_price, 4),
            "fill_size": round(self.fill_size, 4),
            "notional_usd": round(self.notional_usd, 4),
            "slippage": round(self.slippage, 6),
            "slippage_cents": round(self.slippage * 100.0, 4),
            "liquidity_type": self.liquidity_type.upper(),
            "queue_depleted": self.queue_depleted,
            "post_fill_impact": round(self.post_fill_impact, 6),
            "post_fill_impact_cents": round(self.post_fill_impact * 100.0, 4),
            "fee_usd": round(self.fee_usd, 5),
            "trigger_reason": self.trigger_reason,
            "order_id": self.order_id,
            "remaining_quote_size": round(self.remaining_quote_size, 4),
        }


@dataclass
class VirtualQuote:
    """Represents a virtual resting limit order in the simulated book."""

    order_id: str
    asset_id: str
    side: str  # "BUY" or "SELL"
    price: float
    size: float
    initial_size: float
    queue_ahead: float
    initial_queue_ahead: float
    created_at: float = field(default_factory=time.time)
    post_only: bool = True
    is_filled: bool = False
    is_cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "asset_id": self.asset_id,
            "side": self.side.upper(),
            "price": round(self.price, 4),
            "size": round(self.size, 4),
            "initial_size": round(self.initial_size, 4),
            "queue_ahead": round(self.queue_ahead, 4),
            "initial_queue_ahead": round(self.initial_queue_ahead, 4),
            "created_at": self.created_at,
            "post_only": self.post_only,
            "is_filled": self.is_filled,
            "is_cancelled": self.is_cancelled,
        }


class SyntheticOrderBook:
    """Maintains L2 order book depth, calculates queue priority, and supports depth-walking."""

    def __init__(self, asset_id: str, tick_size: float = 0.01) -> None:
        self.asset_id = asset_id
        self.tick_size = tick_size
        self.bids: list[OrderBookLevel] = []  # Sorted descending (highest price first)
        self.asks: list[OrderBookLevel] = []  # Sorted ascending (lowest price first)
        self.last_updated: float = time.time()
        self.book_hash: Optional[str] = None

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def mid_price(self) -> float:
        bb = self.best_bid
        ba = self.best_ask
        if bb > 0.0 and ba < 1.0 and ba >= bb:
            return round((bb + ba) / 2.0, 4)
        if bb > 0.0:
            return round(bb, 4)
        if ba < 1.0:
            return round(ba, 4)
        return 0.50

    @property
    def spread(self) -> float:
        return max(0.0, round(self.best_ask - self.best_bid, 4))

    @property
    def spread_cents(self) -> float:
        return round(self.spread * 100.0, 2)

    @property
    def total_bid_depth(self) -> float:
        return sum(lvl.size for lvl in self.bids)

    @property
    def total_ask_depth(self) -> float:
        return sum(lvl.size for lvl in self.asks)

    def update_from_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        """Update full depth from an incoming OrderBookSnapshot."""
        self.bids = list(snapshot.bids)
        self.asks = list(snapshot.asks)
        self.tick_size = snapshot.tick_size
        self.last_updated = snapshot.timestamp
        self.book_hash = snapshot.hash

    def update_levels(
        self,
        bids: list[Any],
        asks: list[Any],
        tick_size: Optional[float] = None,
        timestamp: Optional[float] = None,
    ) -> None:
        """Update book from raw bid and ask lists."""
        from polymarketbot.simulator.feed import parse_levels

        self.bids = parse_levels(bids, reverse_sort=True)
        self.asks = parse_levels(asks, reverse_sort=False)
        if tick_size is not None:
            self.tick_size = tick_size
        self.last_updated = timestamp if timestamp is not None else time.time()

    def compute_queue_ahead(self, side: str, price: float) -> float:
        """Calculate market volume resting ahead of our quote at given price.

        For BUY quote:
        - Volume of all market bids at higher prices (strictly better priority).
        - Volume of all market bids at identical price (FIFO queue arrival behind existing).

        For SELL quote:
        - Volume of all market asks at lower prices (strictly better priority).
        - Volume of all market asks at identical price (FIFO queue arrival behind existing).
        """
        side_upper = side.upper()
        queue: float = 0.0

        if side_upper == "BUY":
            for lvl in self.bids:
                if lvl.price >= price - 1e-6:
                    queue += lvl.size
                else:
                    break
        elif side_upper == "SELL":
            for lvl in self.asks:
                if lvl.price <= price + 1e-6:
                    queue += lvl.size
                else:
                    break

        return round(queue, 4)

    def walk_book(
        self, side: str, target_size: float
    ) -> tuple[float, float, list[tuple[float, float]], float]:
        """Simulate taker order crossing the spread and walking book depth.

        Args:
            side: Aggressor side ("BUY" or "SELL").
                  "BUY" crosses spread eating asks from lowest to highest.
                  "SELL" crosses spread eating bids from highest to lowest.
            target_size: Desired execution size in shares.

        Returns:
            Tuple of:
            - vwap_price: Volume-weighted average execution price.
            - total_filled: Total shares filled (may be capped by available book depth).
            - executed_levels: List of (level_price, filled_size_at_level).
            - slippage: Difference between VWAP and best initial price (in price units).
        """
        side_upper = side.upper()
        rem_size = float(target_size)
        total_filled = 0.0
        total_cost = 0.0
        executed_levels: list[tuple[float, float]] = []

        if rem_size <= 0.0:
            return 0.0, 0.0, [], 0.0

        if side_upper == "BUY":
            ref_best = self.best_ask
            for lvl in self.asks:
                if rem_size <= 0.0:
                    break
                fill = min(rem_size, lvl.size)
                executed_levels.append((lvl.price, fill))
                total_cost += fill * lvl.price
                total_filled += fill
                rem_size -= fill

            if total_filled > 0.0:
                vwap = total_cost / total_filled
                slippage = max(0.0, vwap - ref_best)
            else:
                vwap = ref_best
                slippage = 0.0

        else:  # SELL
            ref_best = self.best_bid
            for lvl in self.bids:
                if rem_size <= 0.0:
                    break
                fill = min(rem_size, lvl.size)
                executed_levels.append((lvl.price, fill))
                total_cost += fill * lvl.price
                total_filled += fill
                rem_size -= fill

            if total_filled > 0.0:
                vwap = total_cost / total_filled
                slippage = max(0.0, ref_best - vwap)
            else:
                vwap = ref_best
                slippage = 0.0

        return round(vwap, 6), round(total_filled, 4), executed_levels, round(slippage, 6)

    def deplete_levels(self, side: str, executed_levels: list[tuple[float, float]]) -> None:
        """Remove executed taker liquidity from the resting book levels."""
        if not executed_levels:
            return

        side_upper = side.upper()
        # levels to deplete are asks if taker BUY, bids if taker SELL
        source_levels = self.asks if side_upper == "BUY" else self.bids
        level_map = {lvl.price: lvl.size for lvl in source_levels}

        for px, sz in executed_levels:
            if px in level_map:
                level_map[px] = max(0.0, level_map[px] - sz)

        new_levels = [
            OrderBookLevel(price=px, size=round(sz, 6))
            for px, sz in level_map.items()
            if sz > 1e-4
        ]
        reverse = (side_upper == "SELL")  # Bids must be sorted descending
        new_levels.sort(key=lambda lvl: lvl.price, reverse=reverse)

        if side_upper == "BUY":
            self.asks = new_levels
        else:
            self.bids = new_levels


class MarketImpactEngine:
    """Models endogenous market impact and realistic book displacement.

    Implements:
    - Multi-regime impact scaling (0.5x conservative, 1.0x baseline, 2.0x stressed).
    - Square-root law depth impact: Delta P = multiplier * spread * sqrt(size / depth).
    - Post-fill book displacement shifting subsequent midpoints.
    """

    def __init__(
        self,
        impact_multiplier: float = 1.0,
        depth_elasticity: float = 0.5,
        permanent_ratio: float = 0.35,
    ) -> None:
        self.impact_multiplier = max(0.1, float(impact_multiplier))
        self.depth_elasticity = max(0.1, float(depth_elasticity))
        self.permanent_ratio = max(0.0, min(1.0, float(permanent_ratio)))

    def calculate_taker_impact(
        self,
        trade_size: float,
        book: SyntheticOrderBook,
        side: str,
        walk_slippage: float = 0.0,
    ) -> float:
        """Calculate total price impact for a taker market order crossing the spread.

        Includes book-walk microstructure slippage plus residual liquidity displacement.
        """
        if trade_size <= 0:
            return 0.0

        depth = book.total_ask_depth if side.upper() == "BUY" else book.total_bid_depth
        effective_depth = max(depth, 100.0)
        spread = max(book.spread, book.tick_size)

        # Square-root liquidity impact
        participation = min(2.0, trade_size / effective_depth)
        sqrt_impact = spread * math.pow(participation, self.depth_elasticity)

        total_impact = (walk_slippage + sqrt_impact) * self.impact_multiplier
        return round(max(0.0, total_impact), 6)

    def calculate_maker_impact(
        self,
        fill_size: float,
        fill_price: float,
        side: str,
        book: SyntheticOrderBook,
    ) -> float:
        """Calculate price impact generated by a filled resting maker quote."""
        if fill_size <= 0:
            return 0.0

        depth = book.total_bid_depth if side.upper() == "BUY" else book.total_ask_depth
        effective_depth = max(depth, 100.0)
        spread = max(book.spread, book.tick_size)

        participation = min(1.0, fill_size / effective_depth)
        maker_impact = spread * 0.25 * math.pow(participation, self.depth_elasticity)
        return round(maker_impact * self.impact_multiplier, 6)

    def apply_impact_to_book(
        self,
        book: SyntheticOrderBook,
        impact: float,
        side: str,
    ) -> None:
        """Displace synthetic book levels following an aggressive fill."""
        if impact <= 1e-6:
            return

        perm_impact = impact * self.permanent_ratio
        side_upper = side.upper()

        if side_upper == "BUY":
            # Taker buy pushed price UP: shift bids and asks upwards
            book.bids = [
                OrderBookLevel(price=min(0.999, round(lvl.price + perm_impact, 4)), size=lvl.size)
                for lvl in book.bids
            ]
            book.asks = [
                OrderBookLevel(price=min(0.999, round(lvl.price + perm_impact, 4)), size=lvl.size)
                for lvl in book.asks
            ]
        else:
            # Taker sell pushed price DOWN: shift bids and asks downwards
            book.bids = [
                OrderBookLevel(price=max(0.001, round(lvl.price - perm_impact, 4)), size=lvl.size)
                for lvl in book.bids
            ]
            book.asks = [
                OrderBookLevel(price=max(0.001, round(lvl.price - perm_impact, 4)), size=lvl.size)
                for lvl in book.asks
            ]


class SimulationOrderMatcher:
    """Coordinates virtual quote matching, queue exhaustion, and simulated taker execution."""

    def __init__(
        self,
        impact_multiplier: float = 1.0,
        taker_fee_rate: float = 0.0,
    ) -> None:
        self.impact_engine = MarketImpactEngine(impact_multiplier=impact_multiplier)
        self.taker_fee_rate = float(taker_fee_rate)

        # Asset state
        self.books: dict[str, SyntheticOrderBook] = {}
        # asset_id -> {order_id: VirtualQuote}
        self.resting_quotes: dict[str, dict[str, VirtualQuote]] = {}
        self.fill_history: list[FillEvent] = []

    def get_or_create_book(self, asset_id: str, tick_size: float = 0.01) -> SyntheticOrderBook:
        """Retrieve or initialize SyntheticOrderBook for asset_id."""
        if asset_id not in self.books:
            self.books[asset_id] = SyntheticOrderBook(asset_id=asset_id, tick_size=tick_size)
        return self.books[asset_id]

    def update_book(
        self,
        asset_id: str,
        bids: list[Any],
        asks: list[Any],
        tick_size: float = 0.01,
        timestamp: Optional[float] = None,
    ) -> SyntheticOrderBook:
        """Update L2 order book depth for an asset."""
        book = self.get_or_create_book(asset_id, tick_size)
        book.update_levels(bids, asks, tick_size, timestamp)
        return book

    def update_from_snapshot(self, snapshot: OrderBookSnapshot) -> SyntheticOrderBook:
        """Update order book directly from an OrderBookSnapshot."""
        book = self.get_or_create_book(snapshot.asset_id, snapshot.tick_size)
        book.update_from_snapshot(snapshot)
        return book

    # -------------------------------------------------------------------------
    # Quote Lifecycle Management
    # -------------------------------------------------------------------------
    def place_quote(
        self,
        asset_id: str,
        side: str,
        price: float,
        size: float,
        order_id: Optional[str] = None,
        post_only: bool = True,
    ) -> Optional[VirtualQuote]:
        """Place a virtual resting limit quote with strict post-only validation and queue tracking.

        Returns:
            VirtualQuote instance if successfully placed, or None if rejected by post-only rule.
        """
        book = self.get_or_create_book(asset_id)
        side_upper = side.upper()
        p = round(float(price), 4)
        s = round(float(size), 4)

        if s <= 0.0 or not (0.001 <= p <= 0.999):
            logger.warning(f"Rejected invalid quote price {p} or size {s}")
            return None

        # Post-only validation: limit BUY must be < best_ask; limit SELL must be > best_bid
        if post_only:
            if side_upper == "BUY" and p >= book.best_ask and book.best_ask < 1.0:
                logger.warning(
                    f"Post-only BUY rejected: price {p:.4f} >= best_ask {book.best_ask:.4f}"
                )
                return None
            if side_upper == "SELL" and p <= book.best_bid and book.best_bid > 0.0:
                logger.warning(
                    f"Post-only SELL rejected: price {p:.4f} <= best_bid {book.best_bid:.4f}"
                )
                return None

        oid = order_id or f"vq_{uuid.uuid4().hex[:10]}"
        queue = book.compute_queue_ahead(side_upper, p)

        quote = VirtualQuote(
            order_id=oid,
            asset_id=asset_id,
            side=side_upper,
            price=p,
            size=s,
            initial_size=s,
            queue_ahead=queue,
            initial_queue_ahead=queue,
            created_at=time.time(),
            post_only=post_only,
        )

        if asset_id not in self.resting_quotes:
            self.resting_quotes[asset_id] = {}
        self.resting_quotes[asset_id][oid] = quote

        logger.debug(
            f"Placed {side_upper} quote {oid} for {asset_id[:10]}: {s} @ {p:.4f} (queue_ahead={queue:.1f})"
        )
        return quote

    def cancel_quote(self, asset_id: str, order_id: str) -> Optional[VirtualQuote]:
        """Cancel a specific resting virtual quote."""
        quotes = self.resting_quotes.get(asset_id, {})
        if order_id in quotes:
            q = quotes.pop(order_id)
            q.is_cancelled = True
            return q
        return None

    def cancel_all_quotes(self, asset_id: Optional[str] = None) -> list[VirtualQuote]:
        """Cancel all resting virtual quotes across one or all assets."""
        cancelled: list[VirtualQuote] = []
        target_assets = [asset_id] if asset_id else list(self.resting_quotes.keys())

        for aid in target_assets:
            if aid in self.resting_quotes:
                for q in self.resting_quotes[aid].values():
                    q.is_cancelled = True
                    cancelled.append(q)
                self.resting_quotes[aid].clear()

        return cancelled

    def get_active_quotes(
        self, asset_id: Optional[str] = None, side: Optional[str] = None
    ) -> list[VirtualQuote]:
        """Retrieve currently active (unfilled, uncancelled) virtual quotes."""
        active: list[VirtualQuote] = []
        target_assets = [asset_id] if asset_id else list(self.resting_quotes.keys())

        for aid in target_assets:
            for q in self.resting_quotes.get(aid, {}).values():
                if not q.is_filled and not q.is_cancelled and q.size > 1e-4:
                    if side is None or q.side.upper() == side.upper():
                        active.append(q)
        return active

    # -------------------------------------------------------------------------
    # Case 1 & 2: Market Trade Matching (Maker Fills)
    # -------------------------------------------------------------------------
    def on_market_trade(
        self,
        asset_id: str,
        trade_price: float,
        trade_size: float,
        trade_side: str,
        timestamp: Optional[float] = None,
    ) -> list[FillEvent]:
        """Process incoming market trade and execute resting virtual quotes adhering to FIFO queue priority.

        Execution cases handled:
        - Normal trade at price P: fills only after queue_ahead is exhausted.
        - Crossed through price (e.g. taker sell < our bid): triggers instant aggressive fill.

        Args:
            asset_id: Target token ID.
            trade_price: Execution price of public trade.
            trade_size: Execution size of public trade.
            trade_side: Aggressor taker side ("BUY" or "SELL").
            timestamp: Trade timestamp.

        Returns:
            List of generated FillEvents.
        """
        quotes_map = self.resting_quotes.get(asset_id, {})
        if not quotes_map:
            return []

        book = self.get_or_create_book(asset_id)
        ts = timestamp if timestamp is not None else time.time()
        aggressor_side = trade_side.upper()
        rem_trade_size = float(trade_size)
        fills: list[FillEvent] = []

        # Taker SELL aggressor matches against our resting BUY quotes
        if aggressor_side == "SELL":
            candidate_quotes = [
                q for q in quotes_map.values()
                if q.side == "BUY" and not q.is_filled and not q.is_cancelled and q.size > 1e-4
            ]
            # Prioritize higher bid prices first
            candidate_quotes.sort(key=lambda q: q.price, reverse=True)

            for q in candidate_quotes:
                if rem_trade_size <= 0.0:
                    break

                # Case 2: Crossed through our quote price (trade_price < our_bid_price)
                if trade_price < q.price - 1e-6:
                    fill_sz = min(rem_trade_size, q.size)
                    q.queue_ahead = 0.0
                    q.size = round(q.size - fill_sz, 4)
                    if q.size <= 1e-4:
                        q.is_filled = True

                    post_impact = self.impact_engine.calculate_maker_impact(fill_sz, q.price, "BUY", book)
                    self.impact_engine.apply_impact_to_book(book, post_impact, "BUY")

                    fill = FillEvent(
                        fill_id=f"fill_{uuid.uuid4().hex[:12]}",
                        timestamp=ts,
                        asset_id=asset_id,
                        side="BUY",
                        fill_price=q.price,
                        fill_size=fill_sz,
                        notional_usd=round(fill_sz * q.price, 4),
                        slippage=0.0,
                        liquidity_type="MAKER",
                        queue_depleted=True,
                        post_fill_impact=post_impact,
                        fee_usd=0.0,  # Makers pay zero fees on Polymarket
                        trigger_reason=(
                            f"taker_sell_crossed_through_bid (trade_px={trade_price:.4f} < quote_px={q.price:.4f})"
                        ),
                        order_id=q.order_id,
                        remaining_quote_size=q.size,
                    )
                    fills.append(fill)
                    self.fill_history.append(fill)
                    rem_trade_size -= fill_sz

                # Case 1: Printed at our quote price level
                elif abs(trade_price - q.price) <= 1e-6:
                    if rem_trade_size <= q.queue_ahead:
                        q.queue_ahead = round(q.queue_ahead - rem_trade_size, 4)
                        logger.debug(
                            f"Queue depleted at {q.price:.4f}: reduced by {rem_trade_size:.2f} to {q.queue_ahead:.2f}"
                        )
                        rem_trade_size = 0.0
                    else:
                        surplus = rem_trade_size - q.queue_ahead
                        fill_sz = min(q.size, surplus)
                        q.queue_ahead = 0.0
                        q.size = round(q.size - fill_sz, 4)
                        if q.size <= 1e-4:
                            q.is_filled = True

                        post_impact = self.impact_engine.calculate_maker_impact(fill_sz, q.price, "BUY", book)
                        self.impact_engine.apply_impact_to_book(book, post_impact, "BUY")

                        fill = FillEvent(
                            fill_id=f"fill_{uuid.uuid4().hex[:12]}",
                            timestamp=ts,
                            asset_id=asset_id,
                            side="BUY",
                            fill_price=q.price,
                            fill_size=fill_sz,
                            notional_usd=round(fill_sz * q.price, 4),
                            slippage=0.0,
                            liquidity_type="MAKER",
                            queue_depleted=True,
                            post_fill_impact=post_impact,
                            fee_usd=0.0,
                            trigger_reason=(
                                f"taker_sell_depleted_queue_bid (px={trade_price:.4f}, surplus={surplus:.2f})"
                            ),
                            order_id=q.order_id,
                            remaining_quote_size=q.size,
                        )
                        fills.append(fill)
                        self.fill_history.append(fill)
                        rem_trade_size -= (q.queue_ahead + fill_sz)

        # Taker BUY aggressor matches against our resting SELL quotes
        elif aggressor_side == "BUY":
            candidate_quotes = [
                q for q in quotes_map.values()
                if q.side == "SELL" and not q.is_filled and not q.is_cancelled and q.size > 1e-4
            ]
            # Prioritize lower ask prices first
            candidate_quotes.sort(key=lambda q: q.price, reverse=False)

            for q in candidate_quotes:
                if rem_trade_size <= 0.0:
                    break

                # Case 2: Crossed through our quote price (trade_price > our_ask_price)
                if trade_price > q.price + 1e-6:
                    fill_sz = min(rem_trade_size, q.size)
                    q.queue_ahead = 0.0
                    q.size = round(q.size - fill_sz, 4)
                    if q.size <= 1e-4:
                        q.is_filled = True

                    post_impact = self.impact_engine.calculate_maker_impact(fill_sz, q.price, "SELL", book)
                    self.impact_engine.apply_impact_to_book(book, post_impact, "SELL")

                    fill = FillEvent(
                        fill_id=f"fill_{uuid.uuid4().hex[:12]}",
                        timestamp=ts,
                        asset_id=asset_id,
                        side="SELL",
                        fill_price=q.price,
                        fill_size=fill_sz,
                        notional_usd=round(fill_sz * q.price, 4),
                        slippage=0.0,
                        liquidity_type="MAKER",
                        queue_depleted=True,
                        post_fill_impact=post_impact,
                        fee_usd=0.0,
                        trigger_reason=(
                            f"taker_buy_crossed_through_ask (trade_px={trade_price:.4f} > quote_px={q.price:.4f})"
                        ),
                        order_id=q.order_id,
                        remaining_quote_size=q.size,
                    )
                    fills.append(fill)
                    self.fill_history.append(fill)
                    rem_trade_size -= fill_sz

                # Case 1: Printed at our quote price level
                elif abs(trade_price - q.price) <= 1e-6:
                    if rem_trade_size <= q.queue_ahead:
                        q.queue_ahead = round(q.queue_ahead - rem_trade_size, 4)
                        logger.debug(
                            f"Queue depleted at {q.price:.4f}: reduced by {rem_trade_size:.2f} to {q.queue_ahead:.2f}"
                        )
                        rem_trade_size = 0.0
                    else:
                        surplus = rem_trade_size - q.queue_ahead
                        fill_sz = min(q.size, surplus)
                        q.queue_ahead = 0.0
                        q.size = round(q.size - fill_sz, 4)
                        if q.size <= 1e-4:
                            q.is_filled = True

                        post_impact = self.impact_engine.calculate_maker_impact(fill_sz, q.price, "SELL", book)
                        self.impact_engine.apply_impact_to_book(book, post_impact, "SELL")

                        fill = FillEvent(
                            fill_id=f"fill_{uuid.uuid4().hex[:12]}",
                            timestamp=ts,
                            asset_id=asset_id,
                            side="SELL",
                            fill_price=q.price,
                            fill_size=fill_sz,
                            notional_usd=round(fill_sz * q.price, 4),
                            slippage=0.0,
                            liquidity_type="MAKER",
                            queue_depleted=True,
                            post_fill_impact=post_impact,
                            fee_usd=0.0,
                            trigger_reason=(
                                f"taker_buy_depleted_queue_ask (px={trade_price:.4f}, surplus={surplus:.2f})"
                            ),
                            order_id=q.order_id,
                            remaining_quote_size=q.size,
                        )
                        fills.append(fill)
                        self.fill_history.append(fill)
                        rem_trade_size -= (q.queue_ahead + fill_sz)

        # Cleanup filled quotes
        for q in list(quotes_map.values()):
            if q.is_filled:
                quotes_map.pop(q.order_id, None)

        return fills

    # -------------------------------------------------------------------------
    # Case 3: Taker Market Order Execution
    # -------------------------------------------------------------------------
    def execute_taker_order(
        self,
        asset_id: str,
        side: str,
        size: float,
        timestamp: Optional[float] = None,
        custom_fee_rate: Optional[float] = None,
    ) -> FillEvent:
        """Execute an aggressive taker market order crossing the spread immediately.

        Walks the synthetic order book depth to compute realistic VWAP execution price,
        slippage against best opposite quote, depletes book levels, applies endogenous
        market impact, and calculates taker fee.

        Args:
            asset_id: Target token ID.
            side: Aggressor side ("BUY" or "SELL").
            size: Size in shares.
            timestamp: Execution timestamp.
            custom_fee_rate: Optional explicit taker fee rate (default uses self.taker_fee_rate).

        Returns:
            FillEvent capturing complete execution metrics.
        """
        book = self.get_or_create_book(asset_id)
        side_upper = side.upper()
        ts = timestamp if timestamp is not None else time.time()
        s = float(size)

        if s <= 0.0:
            raise ValueError(f"Taker order size must be positive, got {s}")

        # Walk synthetic book depth
        vwap_price, filled_size, levels_walked, walk_slippage = book.walk_book(side_upper, s)

        # Deplete executed levels from the order book
        book.deplete_levels(side_upper, levels_walked)

        # Compute endogenous market impact
        post_impact = self.impact_engine.calculate_taker_impact(
            trade_size=filled_size,
            book=book,
            side=side_upper,
            walk_slippage=walk_slippage,
        )
        self.impact_engine.apply_impact_to_book(book, post_impact, side_upper)

        # Calculate taker fee (taker pays fee based on Polymarket formula)
        fee_rate = custom_fee_rate if custom_fee_rate is not None else self.taker_fee_rate
        # fee = C * rate * (p * (1 - p))
        curve = vwap_price * (1.0 - vwap_price)
        fee_usd = round(filled_size * fee_rate * curve, 5) if fee_rate > 0.0 else 0.0

        notional_usd = round(filled_size * vwap_price, 4)

        fill = FillEvent(
            fill_id=f"taker_{uuid.uuid4().hex[:12]}",
            timestamp=ts,
            asset_id=asset_id,
            side=side_upper,
            fill_price=vwap_price,
            fill_size=filled_size,
            notional_usd=notional_usd,
            slippage=walk_slippage,
            liquidity_type="TAKER",
            queue_depleted=False,
            post_fill_impact=post_impact,
            fee_usd=fee_usd,
            trigger_reason="taker_market_order_cross_spread",
            remaining_quote_size=0.0,
        )

        self.fill_history.append(fill)
        logger.info(
            f"Executed TAKER {side_upper} {filled_size:.2f} @ {vwap_price:.4f} "
            f"(slippage={walk_slippage * 100:.2f}c, impact={post_impact * 100:.2f}c, fee=${fee_usd:.4f})"
        )
        return fill

    def get_fills(self, asset_id: Optional[str] = None) -> list[FillEvent]:
        """Return audit trail of simulated fills."""
        if asset_id is None:
            return list(self.fill_history)
        return [f for f in self.fill_history if f.asset_id == asset_id]

    def clear_fills(self) -> None:
        """Clear fill history."""
        self.fill_history.clear()
