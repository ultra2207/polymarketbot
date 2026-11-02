"""Data models for Polymarket paper trading and liquidity reward harvesting."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class VirtualQuote:
    """Represents a virtual resting limit order."""

    side: str  # "BUY" or "SELL"
    price: float
    size: float
    spread_from_mid_cents: float
    score: float = 0.0
    post_only_valid: bool = True
    queue_ahead: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "price": round(self.price, 4),
            "size": round(self.size, 2),
            "spread_from_mid_cents": round(self.spread_from_mid_cents, 2),
            "quadratic_score": round(self.score, 4),
            "post_only_valid": self.post_only_valid,
            "queue_ahead": round(self.queue_ahead, 2),
            "timestamp": self.timestamp,
        }


@dataclass
class Position:
    """Tracks position inventory, average cost, and PnL for an asset."""

    asset_id: str
    outcome: str
    shares: float = 0.0
    avg_cost: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    market_value: float = 0.0
    fills_count: int = 0

    def update_mark(self, mark_price: float) -> None:
        """Update mark-to-market valuations."""
        self.market_value = self.shares * mark_price
        if self.shares > 0:
            self.unrealized_pnl = self.shares * (mark_price - self.avg_cost)
        elif self.shares < 0:
            self.unrealized_pnl = abs(self.shares) * (self.avg_cost - mark_price)
        else:
            self.unrealized_pnl = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "outcome": self.outcome,
            "shares": round(self.shares, 4),
            "avg_cost": round(self.avg_cost, 4),
            "market_value_usd": round(self.market_value, 4),
            "unrealized_pnl_usd": round(self.unrealized_pnl, 4),
            "realized_pnl_usd": round(self.realized_pnl, 4),
            "fills_count": self.fills_count,
        }


@dataclass
class SimulatedFill:
    """Records an executed simulated trade fill."""

    fill_id: str
    timestamp: float
    condition_id: str
    asset_id: str
    outcome: str
    side: str  # "BUY" or "SELL"
    fill_price: float
    fill_size: float
    fill_value_usd: float
    cash_delta: float
    position_shares_after: float
    avg_cost_after: float
    realized_pnl: float
    trigger_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "timestamp": self.timestamp,
            "condition_id": self.condition_id,
            "asset_id": self.asset_id,
            "outcome": self.outcome,
            "side": self.side,
            "fill_price": round(self.fill_price, 4),
            "fill_size": round(self.fill_size, 4),
            "fill_value_usd": round(self.fill_value_usd, 4),
            "cash_delta": round(self.cash_delta, 4),
            "position_shares_after": round(self.position_shares_after, 4),
            "avg_cost_after": round(self.avg_cost_after, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "trigger_reason": self.trigger_reason,
        }


@dataclass
class MergeEvent:
    """Record of a complete-set CTF merge execution."""

    timestamp: float
    condition_id: str
    shares_merged: float
    usdc_unlocked: float
    gas_fee_usd: float
    remaining_yes: float
    remaining_no: float
    cash_after: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "condition_id": self.condition_id,
            "shares_merged": round(self.shares_merged, 4),
            "usdc_unlocked": round(self.usdc_unlocked, 4),
            "gas_fee_usd": round(self.gas_fee_usd, 4),
            "remaining_yes": round(self.remaining_yes, 4),
            "remaining_no": round(self.remaining_no, 4),
            "cash_after": round(self.cash_after, 2),
        }


@dataclass
class EpochSnapshot:
    """Summary of a 1-minute reward sampling epoch."""

    epoch_index: int
    timestamp: float
    condition_id: str
    asset_id: str
    outcome: str
    mid_price: float
    virtual_score: float
    competing_score: float
    pool_share_pct: float
    epoch_payout_usd: float
    projected_daily_payout_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "epoch_index": self.epoch_index,
            "timestamp": self.timestamp,
            "condition_id": self.condition_id,
            "asset_id": self.asset_id,
            "outcome": self.outcome,
            "mid_price": round(self.mid_price, 4),
            "virtual_score": round(self.virtual_score, 4),
            "competing_score": round(self.competing_score, 4),
            "pool_share_pct": round(self.pool_share_pct, 4),
            "epoch_payout_usd": round(self.epoch_payout_usd, 4),
            "projected_daily_payout_usd": round(self.projected_daily_payout_usd, 2),
        }


@dataclass
class TokenConfig:
    token_id: str
    outcome: str
    price: float = 0.5


@dataclass
class MarketConfig:
    condition_id: str
    question: str
    rate_per_day: float
    rewards_min_size: float
    rewards_max_spread: float  # in cents
    tokens: list[TokenConfig] = field(default_factory=list)
