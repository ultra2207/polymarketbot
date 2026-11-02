"""Cross-Venue Data Models.

Defines event types and state models for Polymarket <-> Kalshi Symbiosis:
- LeadLagSignal: Latency-critical signal emitted on lead-lag dislocations.
- CrossVenueHedgeRequest: Passive maker hedge request to lock complete synthetic pairs.
- SyntheticPairState: Real-time inventory and locked complete set accounting.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Venue(str, Enum):
    """Trading venues in cross-venue symbiosis."""

    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class SignalAction(str, Enum):
    """Action requested by the Lead-Lag Sentinel."""

    PULL_QUOTE = "PULL_QUOTE"
    FADE_QUOTE = "FADE_QUOTE"
    JOIN_SPREAD = "JOIN_SPREAD"
    CROSS_BOOK = "CROSS_BOOK"


class SignalUrgency(str, Enum):
    """Urgency level of a lead-lag signal."""

    CRITICAL = "CRITICAL"  # Immediate pull before toxicity hits (<1ms)
    HIGH = "HIGH"          # High-speed quote fade
    NORMAL = "NORMAL"


class MarketCategory(str, Enum):
    """Market taxonomy defining venue precedence."""

    MACRO = "macro"              # CPI, FOMC, Jobs, Rates -> Kalshi leads
    CRYPTO = "crypto"            # BTC, ETH -> Polymarket leads
    GEOPOLITICS = "geopolitics"  # Elections, policy -> Polymarket leads
    OTHER = "other"


class HedgeExecutionMode(str, Enum):
    """Execution mode for cross-venue hedging."""

    MAKER_POST_ONLY = "MAKER_POST_ONLY"  # 0% maker fee on Kalshi
    TAKER_CROSS = "TAKER_CROSS"          # Incurs ~7% quadratic taker fee on Kalshi


class HedgeStatus(str, Enum):
    """Lifecycle status of a cross-venue hedge request."""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    RESTING = "RESTING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass
class LeadLagSignal:
    """Latency-critical signal generated when a lead venue moves before a lag venue."""

    source_venue: Venue
    target_venue: Venue
    symbol_source: str
    symbol_target: str
    action: SignalAction
    price_jump: float
    ofi: float
    source_price: float
    signal_id: str = field(default_factory=lambda: f"sig_{uuid.uuid4().hex[:10]}")
    timestamp_ns: int = field(default_factory=time.perf_counter_ns)
    category: MarketCategory = MarketCategory.OTHER
    target_price: Optional[float] = None
    urgency: SignalUrgency = SignalUrgency.HIGH
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def elapsed_ms(self, now_ns: Optional[int] = None) -> float:
        """Calculate elapsed time in milliseconds since signal creation."""
        curr = now_ns if now_ns is not None else time.perf_counter_ns()
        return (curr - self.timestamp_ns) / 1_000_000.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "timestamp_ns": self.timestamp_ns,
            "source_venue": self.source_venue.value,
            "target_venue": self.target_venue.value,
            "symbol_source": self.symbol_source,
            "symbol_target": self.symbol_target,
            "action": self.action.value,
            "price_jump": self.price_jump,
            "ofi": self.ofi,
            "source_price": self.source_price,
            "target_price": self.target_price,
            "urgency": self.urgency.value,
            "reason": self.reason,
            "metadata": self.metadata,
        }


@dataclass
class CrossVenueHedgeRequest:
    """Request to hedge an executed fill on one venue with a resting maker quote on the other."""

    originating_venue: Venue
    originating_order_id: str
    originating_token_id: str
    originating_side: str  # 'YES' or 'NO'
    fill_price: float
    fill_size: float
    target_venue: Venue
    target_ticker: str
    target_side: str  # 'NO' or 'YES'
    target_price: float
    target_size: float
    hedge_id: str = field(default_factory=lambda: f"hdg_{uuid.uuid4().hex[:10]}")
    timestamp_ns: int = field(default_factory=time.perf_counter_ns)
    execution_mode: HedgeExecutionMode = HedgeExecutionMode.MAKER_POST_ONLY
    post_only: bool = True
    order_group_id: Optional[str] = None
    dislocation_spread: float = 0.0
    status: HedgeStatus = HedgeStatus.PENDING
    rejection_reason: Optional[str] = None
    target_order_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hedge_id": self.hedge_id,
            "originating_venue": self.originating_venue.value,
            "originating_order_id": self.originating_order_id,
            "originating_token_id": self.originating_token_id,
            "originating_side": self.originating_side,
            "fill_price": self.fill_price,
            "fill_size": self.fill_size,
            "target_venue": self.target_venue.value,
            "target_ticker": self.target_ticker,
            "target_side": self.target_side,
            "target_price": self.target_price,
            "target_size": self.target_size,
            "execution_mode": self.execution_mode.value,
            "post_only": self.post_only,
            "order_group_id": self.order_group_id,
            "dislocation_spread": self.dislocation_spread,
            "status": self.status.value,
            "rejection_reason": self.rejection_reason,
            "target_order_id": self.target_order_id,
            "created_at": self.created_at,
        }


@dataclass
class SyntheticPairState:
    """Tracks inventory, complete sets, and risk across Polymarket and Kalshi for linked markets."""

    pair_id: str
    poly_token_id: str
    kalshi_ticker: str
    poly_yes_inventory: float = 0.0
    poly_no_inventory: float = 0.0
    kalshi_yes_inventory: float = 0.0
    kalshi_no_inventory: float = 0.0
    poly_yes_cost: float = 0.0
    poly_no_cost: float = 0.0
    kalshi_yes_cost: float = 0.0
    kalshi_no_cost: float = 0.0
    total_hedges_executed: int = 0

    @property
    def locked_sets_yes_poly_no_kalshi(self) -> float:
        """Complete sets formed by Long Poly YES + Long Kalshi NO."""
        return min(self.poly_yes_inventory, self.kalshi_no_inventory)

    @property
    def locked_sets_no_poly_yes_kalshi(self) -> float:
        """Complete sets formed by Long Poly NO + Long Kalshi YES."""
        return min(self.poly_no_inventory, self.kalshi_yes_inventory)

    @property
    def total_locked_sets(self) -> float:
        """Total complete synthetic pairs held across venues."""
        return self.locked_sets_yes_poly_no_kalshi + self.locked_sets_no_poly_yes_kalshi

    @property
    def net_delta(self) -> float:
        """Net directional contract delta across both venues.

        Poly YES = +1, Poly NO = -1.
        Kalshi YES = +1, Kalshi NO = -1.
        Holding Poly YES + Kalshi NO yields net_delta = 0.
        """
        poly_delta = self.poly_yes_inventory - self.poly_no_inventory
        kalshi_delta = self.kalshi_yes_inventory - self.kalshi_no_inventory
        return poly_delta + kalshi_delta

    @property
    def is_fully_hedged(self) -> bool:
        """True if net delta is virtually zero and we hold locked sets."""
        return abs(self.net_delta) < 1e-6 and self.total_locked_sets > 0

    @property
    def locked_profit(self) -> float:
        """Guaranteed locked dollar profit across resolved complete sets.

        Each complete set pays exactly $1.00 at binary settlement.
        """
        profit = 0.0
        # Combination 1: Poly YES + Kalshi NO
        sets1 = self.locked_sets_yes_poly_no_kalshi
        if sets1 > 0 and self.poly_yes_inventory > 0 and self.kalshi_no_inventory > 0:
            avg_poly_yes = self.poly_yes_cost / self.poly_yes_inventory
            avg_kalshi_no = self.kalshi_no_cost / self.kalshi_no_inventory
            cost_per_set = avg_poly_yes + avg_kalshi_no
            profit += sets1 * (1.00 - cost_per_set)

        # Combination 2: Poly NO + Kalshi YES
        sets2 = self.locked_sets_no_poly_yes_kalshi
        if sets2 > 0 and self.poly_no_inventory > 0 and self.kalshi_yes_inventory > 0:
            avg_poly_no = self.poly_no_cost / self.poly_no_inventory
            avg_kalshi_yes = self.kalshi_yes_cost / self.kalshi_yes_inventory
            cost_per_set = avg_poly_no + avg_kalshi_yes
            profit += sets2 * (1.00 - cost_per_set)

        return round(profit, 4)

    def record_fill(self, venue: Venue, side: str, price: float, size: float) -> None:
        """Update inventory and cost basis when a fill occurs on either venue."""
        side = side.upper()
        if venue == Venue.POLYMARKET:
            if side == "YES":
                self.poly_yes_inventory += size
                self.poly_yes_cost += price * size
            elif side == "NO":
                self.poly_no_inventory += size
                self.poly_no_cost += price * size
        elif venue == Venue.KALSHI:
            if side == "YES":
                self.kalshi_yes_inventory += size
                self.kalshi_yes_cost += price * size
            elif side == "NO":
                self.kalshi_no_inventory += size
                self.kalshi_no_cost += price * size
        self.total_hedges_executed += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "poly_token_id": self.poly_token_id,
            "kalshi_ticker": self.kalshi_ticker,
            "poly_yes_inventory": self.poly_yes_inventory,
            "poly_no_inventory": self.poly_no_inventory,
            "kalshi_yes_inventory": self.kalshi_yes_inventory,
            "kalshi_no_inventory": self.kalshi_no_inventory,
            "total_locked_sets": self.total_locked_sets,
            "net_delta": self.net_delta,
            "is_fully_hedged": self.is_fully_hedged,
            "locked_profit": self.locked_profit,
            "total_hedges_executed": self.total_hedges_executed,
        }
