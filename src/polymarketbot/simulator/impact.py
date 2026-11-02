"""Microstructure Market Impact Engine for Polymarket paper trading.

Implements:
1. Exact FIFO Queue Priority: Resting volume at or better than quote price P.
2. Queue Depletion: Trade size consumption against queue_ahead and fillable surplus calculation.
3. Queue Penalty on Price Revision: Forfeits queue priority and resets queue_ahead upon price shift.
4. Kyle's Lambda Post-Fill Price Impact: Post-fill adverse price drift:
   delta_P = -sign(side) * lambda_kyle * sqrt(fill_size) * volatility * (1 - exp(-kappa * tau)).
5. Level Depletion & Level Clearing: Wipes price level and pushes the order book forward.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

from polymarketbot.logs import get_logger
from polymarketbot.simulator.book import (
    PRICE_EPSILON,
    PRICE_PRECISION,
    DepthInfo,
    SyntheticOrderBook,
    _norm_px,
)

logger = get_logger("simulator")


class QuoteSide(str, Enum):
    """Side of the resting virtual maker quote."""

    BUY = "BUY"
    SELL = "SELL"

    @classmethod
    def from_str(cls, side: str) -> QuoteSide:
        s = side.strip().upper()
        if s in ("BUY", "BID"):
            return cls.BUY
        if s in ("SELL", "ASK"):
            return cls.SELL
        raise ValueError(f"Unknown side: '{side}'. Expected 'BUY' or 'SELL'.")

    @property
    def sign(self) -> int:
        """Sign convention: +1 for BUY/BID, -1 for SELL/ASK."""
        return 1 if self == QuoteSide.BUY else -1


def compute_kyles_price_impact(
    side: Union[str, int, QuoteSide],
    fill_size: float,
    volatility: float,
    tau: float,
    lambda_kyle: float = 0.001,
    kappa: float = 0.5,
) -> float:
    """Compute post-fill adverse price drift using Kyle's Lambda square-root formulation.

    Formula:
        delta_P = -sign(side) * lambda_kyle * sqrt(fill_size) * volatility * (1 - exp(-kappa * tau))

    Adverse Selection Interpretation:
        - When our BUY maker quote fills, aggressive taker sells dominate, driving the
          post-fill price downwards (delta_P < 0).
        - When our SELL maker quote fills, aggressive taker buys dominate, driving the
          post-fill price upwards (delta_P > 0).

    Args:
        side: "BUY" / "SELL", +1 / -1, or QuoteSide.
        fill_size: Number of shares filled (> 0).
        volatility: Asset volatility (e.g. annualized or intraday standard deviation).
        tau: Elapsed time or impact horizon (> 0).
        lambda_kyle: Kyle's lambda market depth parameter (> 0).
        kappa: Exponential impact decay / relaxation rate (> 0).

    Returns:
        delta_P: Post-fill adverse price drift.
    """
    if fill_size <= 0.0 or volatility <= 0.0 or tau <= 0.0 or kappa <= 0.0 or lambda_kyle <= 0.0:
        return 0.0

    if isinstance(side, int):
        s_val = 1 if side > 0 else -1
    elif isinstance(side, QuoteSide):
        s_val = side.sign
    else:
        s_val = QuoteSide.from_str(str(side)).sign

    time_factor = 1.0 - math.exp(-kappa * tau)
    impact = -s_val * lambda_kyle * math.sqrt(fill_size) * volatility * time_factor
    return round(impact, PRICE_PRECISION)


def deplete_queue_step(
    queue_ahead: float,
    trade_size: float,
) -> Tuple[float, float]:
    """Execute standard FIFO queue depletion for a trade printing at the quote level.

    Rules:
        - If trade_size <= queue_ahead:
            queue_ahead -= trade_size
            fillable_surplus = 0.0
        - If trade_size > queue_ahead:
            fillable_surplus = trade_size - queue_ahead
            queue_ahead = 0.0

    Returns:
        Tuple of (new_queue_ahead, fillable_surplus).
    """
    q_ahead = max(0.0, float(queue_ahead))
    t_size = max(0.0, float(trade_size))

    if t_size <= q_ahead:
        return round(q_ahead - t_size, PRICE_PRECISION), 0.0
    else:
        surplus = round(t_size - q_ahead, PRICE_PRECISION)
        return 0.0, surplus


@dataclass
class QuoteQueueTracker:
    """State of an active resting virtual quote tracked for queue priority and fills."""

    quote_id: str
    side: QuoteSide
    price: float
    size: float
    queue_ahead: float
    created_at: float = 0.0
    revisions_count: int = 0
    fills_count: int = 0
    total_filled_size: float = 0.0
    last_fill_drift: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote_id": self.quote_id,
            "side": self.side.value,
            "price": self.price,
            "size": round(self.size, 4),
            "queue_ahead": round(self.queue_ahead, 4),
            "created_at": self.created_at,
            "revisions_count": self.revisions_count,
            "fills_count": self.fills_count,
            "total_filled_size": round(self.total_filled_size, 4),
            "last_fill_drift": self.last_fill_drift,
        }


@dataclass
class TradeImpactResult:
    """Result of processing a trade against resting virtual maker quotes."""

    quote_id: str
    side: str
    quote_price: float
    trade_price: float
    trade_size: float
    queue_ahead_before: float
    queue_ahead_after: float
    fillable_surplus: float
    fill_size: float
    post_fill_drift: float
    level_cleared: bool
    remaining_quote_size: float
    effective_mid_after: Optional[float] = None
    effective_micro_after: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote_id": self.quote_id,
            "side": self.side,
            "quote_price": self.quote_price,
            "trade_price": self.trade_price,
            "trade_size": round(self.trade_size, 4),
            "queue_ahead_before": round(self.queue_ahead_before, 4),
            "queue_ahead_after": round(self.queue_ahead_after, 4),
            "fillable_surplus": round(self.fillable_surplus, 4),
            "fill_size": round(self.fill_size, 4),
            "post_fill_drift": self.post_fill_drift,
            "level_cleared": self.level_cleared,
            "remaining_quote_size": round(self.remaining_quote_size, 4),
            "effective_mid_after": self.effective_mid_after,
            "effective_micro_after": self.effective_micro_after,
        }


class MarketImpactEngine:
    """Microstructure and Market Impact Engine.

    Manages:
    - Queue position tracking under strict FIFO priority rules.
    - Queue penalty on price revisions (forfeits priority, resets to total competing depth).
    - Queue depletion upon real trade prints.
    - Post-fill adverse selection price drift via Kyle's lambda square-root model.
    - Price level clearing and advancing the synthetic order book forward.
    """

    def __init__(
        self,
        book: Optional[SyntheticOrderBook] = None,
        lambda_kyle: float = 0.001,
        kappa: float = 0.5,
        default_volatility: float = 0.02,
        default_tau: float = 1.0,
    ) -> None:
        self.book = book or SyntheticOrderBook()
        self.lambda_kyle = float(lambda_kyle)
        self.kappa = float(kappa)
        self.default_volatility = float(default_volatility)
        self.default_tau = float(default_tau)

        # Tracked quotes: quote_id -> QuoteQueueTracker
        self.quotes: dict[str, QuoteQueueTracker] = {}

    # -------------------------------------------------------------------------
    # FIFO Queue Priority
    # -------------------------------------------------------------------------

    def compute_queue_ahead(
        self,
        side: Union[str, QuoteSide],
        price: float,
        book: Optional[SyntheticOrderBook] = None,
    ) -> float:
        """Compute exact queue_ahead as the real resting volume at or better than P.

        For BUY quotes:
            Higher prices are better. Competing volume = sum(real_bids at p >= P).
        For SELL quotes:
            Lower prices are better. Competing volume = sum(real_asks at p <= P).

        Args:
            side: "BUY" or "SELL".
            price: Proposed limit order price P.
            book: Optional book instance (defaults to self.book).

        Returns:
            queue_ahead: Real volume ahead in the FIFO queue.
        """
        ob = book or self.book
        s = QuoteSide.from_str(str(side)) if not isinstance(side, QuoteSide) else side
        norm_px = _norm_px(price)

        if s == QuoteSide.BUY:
            # All real bids at or better (>= P)
            q_ahead = sum(
                sz for p, sz in ob._real_bids.items()
                if p >= norm_px - PRICE_EPSILON and sz > PRICE_EPSILON
            )
        else:
            # All real asks at or better (<= P)
            q_ahead = sum(
                sz for p, sz in ob._real_asks.items()
                if p <= norm_px + PRICE_EPSILON and sz > PRICE_EPSILON
            )

        return round(q_ahead, PRICE_PRECISION)

    # -------------------------------------------------------------------------
    # Quote Lifecycle & Queue Penalties
    # -------------------------------------------------------------------------

    def place_quote(
        self,
        side: Union[str, QuoteSide],
        price: float,
        size: float,
        quote_id: Optional[str] = None,
        timestamp: float = 0.0,
    ) -> QuoteQueueTracker:
        """Place a virtual maker quote into the engine with initial FIFO queue priority.

        Args:
            side: "BUY" or "SELL".
            price: Quote price P.
            size: Quote size.
            quote_id: Optional unique identifier.
            timestamp: Quote placement epoch timestamp.

        Returns:
            QuoteQueueTracker instance with calculated queue_ahead.
        """
        s = QuoteSide.from_str(str(side)) if not isinstance(side, QuoteSide) else side
        norm_px = _norm_px(price)
        norm_sz = max(0.0, float(size))
        qid = quote_id or str(uuid.uuid4())

        # Exact FIFO priority: real resting volume at or better than P
        queue_ahead = self.compute_queue_ahead(s, norm_px)

        # Inject into synthetic book
        self.book.inject_quote(
            side=s.value,
            price=norm_px,
            size=norm_sz,
            quote_id=qid,
            timestamp=timestamp,
        )

        tracker = QuoteQueueTracker(
            quote_id=qid,
            side=s,
            price=norm_px,
            size=norm_sz,
            queue_ahead=queue_ahead,
            created_at=timestamp,
        )
        self.quotes[qid] = tracker

        logger.debug(
            f"[QUOTE PLACED] {s.value} {norm_sz:.1f} @ {norm_px:.3f} | "
            f"queue_ahead: {queue_ahead:.1f}"
        )
        return tracker

    def revise_quote(
        self,
        quote_id: str,
        new_price: float,
        new_size: Optional[float] = None,
        timestamp: float = 0.0,
    ) -> QuoteQueueTracker:
        """Revise an existing quote.

        Queue Penalty on Price Revision:
            If quote price shifts, forfeits priority and resets queue_ahead to total
            competing volume at the new price level.
            If quote price is unchanged, retains existing queue_ahead.
        """
        tracker = self.quotes.get(quote_id)
        if tracker is None:
            raise KeyError(f"Quote {quote_id} not found in MarketImpactEngine.")

        norm_new_px = _norm_px(new_price)
        price_shifted = abs(norm_new_px - tracker.price) > PRICE_EPSILON
        norm_size = max(0.0, float(new_size)) if new_size is not None else tracker.size

        if price_shifted:
            # Forfeits priority! Reset queue_ahead to competing volume at new price level
            old_px = tracker.price
            tracker.price = norm_new_px
            tracker.queue_ahead = self.compute_queue_ahead(tracker.side, norm_new_px)
            tracker.revisions_count += 1

            logger.info(
                f"[QUEUE PENALTY] Quote {quote_id} price shifted {old_px:.3f} -> {norm_new_px:.3f}. "
                f"Priority forfeited. Reset queue_ahead: {tracker.queue_ahead:.1f}"
            )
        else:
            # Same price level: retains queue priority
            pass

        tracker.size = norm_size

        # Update synthetic book
        self.book.inject_quote(
            side=tracker.side.value,
            price=tracker.price,
            size=tracker.size,
            quote_id=quote_id,
            timestamp=timestamp,
        )

        return tracker

    def cancel_quote(self, quote_id: str) -> bool:
        """Cancel tracked quote and remove from synthetic book."""
        tracker = self.quotes.pop(quote_id, None)
        if tracker is None:
            return False
        self.book.cancel_quote(quote_id)
        return True

    # -------------------------------------------------------------------------
    # Queue Depletion & Trade Processing
    # -------------------------------------------------------------------------

    def deplete_queue_at_price(
        self,
        price: float,
        trade_size: float,
        quote_id: Optional[str] = None,
    ) -> Tuple[float, float]:
        """Deplete queue_ahead for quote(s) at price P following exact FIFO rules.

        Rules:
            - If trade_size <= queue_ahead: decrements queue_ahead -= trade_size.
            - If trade_size > queue_ahead: returns fillable surplus = trade_size - queue_ahead, sets queue_ahead = 0.

        Returns:
            Tuple of (new_queue_ahead, fillable_surplus).
        """
        norm_px = _norm_px(price)

        if quote_id is not None:
            tracker = self.quotes.get(quote_id)
            if tracker is None or abs(tracker.price - norm_px) > PRICE_EPSILON:
                return 0.0, 0.0
            new_q, surplus = deplete_queue_step(tracker.queue_ahead, trade_size)
            tracker.queue_ahead = new_q
            return new_q, surplus

        # If no specific quote_id provided, deplete first matching quote at this price
        for q in self.quotes.values():
            if abs(q.price - norm_px) <= PRICE_EPSILON:
                new_q, surplus = deplete_queue_step(q.queue_ahead, trade_size)
                q.queue_ahead = new_q
                return new_q, surplus

        return 0.0, trade_size

    def on_trade(
        self,
        trade_price: float,
        trade_size: float,
        trade_side: str,
        volatility: Optional[float] = None,
        tau: Optional[float] = None,
        quote_id: Optional[str] = None,
    ) -> List[TradeImpactResult]:
        """Process incoming trade against resting virtual maker quotes.

        Taker trade sides:
            - Taker SELL trades execute against resting BUY limit quotes.
            - Taker BUY trades execute against resting SELL limit quotes.

        Executes:
            1. Queue Depletion at or through the quote price.
            2. Fill execution from available surplus.
            3. Kyle's Lambda post-fill adverse price drift:
               delta_P = -sign(side) * lambda_kyle * sqrt(fill_size) * volatility * (1 - exp(-kappa * tau)).
            4. Level Depletion & Level Clearing: wipes price level if remaining volume reaches 0,
               pushing the synthetic order book forward.

        Returns:
            List of TradeImpactResult for affected quotes.
        """
        t_side = trade_side.upper()
        norm_trade_px = _norm_px(trade_price)
        t_sz = max(0.0, float(trade_size))
        vol = self.default_volatility if volatility is None else float(volatility)
        impact_tau = self.default_tau if tau is None else float(tau)

        target_quotes: List[QuoteQueueTracker] = []
        if quote_id is not None:
            q = self.quotes.get(quote_id)
            if q is not None:
                target_quotes.append(q)
        else:
            target_quotes = list(self.quotes.values())

        results: List[TradeImpactResult] = []

        for q in target_quotes:
            if q.size <= PRICE_EPSILON:
                continue

            # Check if taker trade interacts with this quote
            interacts = False
            is_crossed = False
            is_at_level = False

            if t_side == "SELL" and q.side == QuoteSide.BUY:
                if norm_trade_px < q.price - PRICE_EPSILON:
                    # Taker sold through our bid price: instant cross
                    interacts = True
                    is_crossed = True
                elif abs(norm_trade_px - q.price) <= PRICE_EPSILON:
                    # Printed exactly at our bid price
                    interacts = True
                    is_at_level = True
                elif norm_trade_px > q.price + PRICE_EPSILON:
                    # Printed at a higher bid: consumes bids ahead of us
                    q_before = q.queue_ahead
                    q.queue_ahead = max(0.0, round(q.queue_ahead - t_sz, PRICE_PRECISION))
                    results.append(
                        TradeImpactResult(
                            quote_id=q.quote_id,
                            side=q.side.value,
                            quote_price=q.price,
                            trade_price=norm_trade_px,
                            trade_size=t_sz,
                            queue_ahead_before=q_before,
                            queue_ahead_after=q.queue_ahead,
                            fillable_surplus=0.0,
                            fill_size=0.0,
                            post_fill_drift=0.0,
                            level_cleared=False,
                            remaining_quote_size=q.size,
                            effective_mid_after=self.book.mid_price,
                            effective_micro_after=self.book.micro_price,
                        )
                    )

            elif t_side == "BUY" and q.side == QuoteSide.SELL:
                if norm_trade_px > q.price + PRICE_EPSILON:
                    # Taker bought through our ask price: instant cross
                    interacts = True
                    is_crossed = True
                elif abs(norm_trade_px - q.price) <= PRICE_EPSILON:
                    # Printed exactly at our ask price
                    interacts = True
                    is_at_level = True
                elif norm_trade_px < q.price - PRICE_EPSILON:
                    # Printed at a lower ask: consumes asks ahead of us
                    q_before = q.queue_ahead
                    q.queue_ahead = max(0.0, round(q.queue_ahead - t_sz, PRICE_PRECISION))
                    results.append(
                        TradeImpactResult(
                            quote_id=q.quote_id,
                            side=q.side.value,
                            quote_price=q.price,
                            trade_price=norm_trade_px,
                            trade_size=t_sz,
                            queue_ahead_before=q_before,
                            queue_ahead_after=q.queue_ahead,
                            fillable_surplus=0.0,
                            fill_size=0.0,
                            post_fill_drift=0.0,
                            level_cleared=False,
                            remaining_quote_size=q.size,
                            effective_mid_after=self.book.mid_price,
                            effective_micro_after=self.book.micro_price,
                        )
                    )

            if not interacts:
                continue

            q_before = q.queue_ahead
            if is_crossed:
                # Crossed through: all queue ahead cleared, full trade size is fillable
                fillable_surplus = t_sz
                q_after = 0.0
            else:
                # Printed at level: standard depletion
                q_after, fillable_surplus = deplete_queue_step(q_before, t_sz)

            q.queue_ahead = q_after

            fill_size = 0.0
            post_fill_drift = 0.0
            level_cleared = False

            if is_crossed:
                # Crossed through: level was completely swept
                fill_size = min(q.size, fillable_surplus)
                q.size = round(max(0.0, q.size - fill_size), PRICE_PRECISION)
                q.fills_count += 1
                q.total_filled_size += fill_size

                post_fill_drift = compute_kyles_price_impact(
                    side=q.side,
                    fill_size=fill_size,
                    volatility=vol,
                    tau=impact_tau,
                    lambda_kyle=self.lambda_kyle,
                    kappa=self.kappa,
                )
                q.last_fill_drift = post_fill_drift

                self.book.wipe_level(q.side.value, q.price)
                level_cleared = True
                if q.size <= PRICE_EPSILON:
                    self.book.cancel_quote(q.quote_id)

            elif is_at_level:
                # Printed at level: deplete trade_size from resting book liquidity
                depleted, wiped = self.book.deplete_level(q.side.value, q.price, t_sz)

                if fillable_surplus > PRICE_EPSILON:
                    fill_size = min(q.size, fillable_surplus)
                    q.size = round(max(0.0, q.size - fill_size), PRICE_PRECISION)
                    q.fills_count += 1
                    q.total_filled_size += fill_size

                    # Kyle's lambda post-fill adverse price drift
                    post_fill_drift = compute_kyles_price_impact(
                        side=q.side,
                        fill_size=fill_size,
                        volatility=vol,
                        tau=impact_tau,
                        lambda_kyle=self.lambda_kyle,
                        kappa=self.kappa,
                    )
                    q.last_fill_drift = post_fill_drift

                # Check if level is completely cleared
                rem_depth = self.book.get_depth(q.price, side=q.side.value).total
                if rem_depth <= PRICE_EPSILON or wiped:
                    self.book.wipe_level(q.side.value, q.price)
                    level_cleared = True

                if q.size <= PRICE_EPSILON:
                    self.book.cancel_quote(q.quote_id)

            results.append(
                TradeImpactResult(
                    quote_id=q.quote_id,
                    side=q.side.value,
                    quote_price=q.price,
                    trade_price=norm_trade_px,
                    trade_size=t_sz,
                    queue_ahead_before=q_before,
                    queue_ahead_after=q_after,
                    fillable_surplus=fillable_surplus,
                    fill_size=fill_size,
                    post_fill_drift=post_fill_drift,
                    level_cleared=level_cleared,
                    remaining_quote_size=q.size,
                    effective_mid_after=self.book.mid_price,
                    effective_micro_after=self.book.micro_price,
                )
            )

        return results

    # -------------------------------------------------------------------------
    # Level Depletion & Level Clearing
    # -------------------------------------------------------------------------

    def deplete_and_clear_level(
        self,
        side: str,
        price: float,
        fill_size: float,
        book: Optional[SyntheticOrderBook] = None,
    ) -> bool:
        """Deplete fill_size from the price level in the order book.

        If the fill consumes the remaining volume at that price level,
        wipes the level and pushes the book forward.

        Returns:
            level_cleared: True if the price level was wiped.
        """
        ob = book or self.book
        norm_px = _norm_px(price)

        # First deplete resting volume in the synthetic book
        depleted, wiped = ob.deplete_level(side, norm_px, fill_size)

        # Check remaining depth
        rem_depth = ob.get_depth(norm_px, side=side).total
        if rem_depth <= PRICE_EPSILON:
            ob.wipe_level(side, norm_px)
            logger.info(
                f"[LEVEL CLEARED] Wiped {side} @ {norm_px:.3f}. "
                f"Book pushed forward -> New effective best: "
                f"Bid={ob.best_bid}, Ask={ob.best_ask}, Mid={ob.mid_price}"
            )
            return True

        return wiped
