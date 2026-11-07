"""Data models for Polymarket paper trading and liquidity reward harvesting."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class BrokenBasketPolicy(str, Enum):
    """Execution policy when a multi-leg basket suffers execution failure (broken leg)."""

    UNWIND_MARKET = "unwind_market"         # Cross spread immediately to market-sell filled partial legs into CLOB bids
    SYNTHETIC_CONVERT = "synthetic_convert" # Convert filled legs via NegRiskAdapter or complete set merge
    ABORT_HOLD = "abort_hold"               # Halt and leave partial inventory intact


@dataclass
class AntiDelusionConfig:
    """Institutional configuration eliminating the 8 backtest delusions in paper trading."""

    enabled: bool = True

    # 1. Fill Rate Delusion (Empirical Logistic Model)
    use_logistic_fill_model: bool = True
    fill_model_path: str = "data/derived/fill_model.json"
    fill_model_coef: tuple[float, float, float] = (1.9969502915862272, -3.966170297458595, -0.21375437197755692)
    require_earned_fill: bool = True
    deterministic_fills: bool = False

    # 2. Adverse Selection & Binance Lead-Lag Alpha Gate
    enable_markout_haircut: bool = True
    markout_drift_cents: float = 0.5         # 0.5c / share adverse drift on passive sweep fills
    binance_surge_threshold_bps: float = 10.0 # > 10 bps Binance jump pulls threatened resting quotes

    # 3. Latency Taker Sniping Delusion
    max_taker_snipe_prob: float = 0.05       # P(snipe) <= 5% against co-located bots
    taker_latency_ms: float = 120.0
    latency_decay_rate: float = 15.0

    # 4. Broken Basket & Sequential Leg Risk
    broken_basket_policy: BrokenBasketPolicy = BrokenBasketPolicy.UNWIND_MARKET

    # 5. UMA Settlement Delay & Capital Lockup
    uma_liveness_hours: float = 2.0
    relayer_queue_delay_hours: float = 1.11  # Total 3.11 hours
    lockup_duration_seconds: float = 11196.0  # 3.11 * 3600 seconds

    # 6. Polygon PoS Gas & Fee Friction
    dynamic_polygon_gas: bool = True
    taker_fee_rate: float = 0.04             # Polymarket exact taker fee rate: C * 0.04 * (p * (1 - p))

    # 7. Order Book Depth & Non-Linear Market Impact (Kyle's Lambda)
    kyle_lambda: float = 0.004               # 0.004 * (Size / Depth)^1.2
    kyle_exponent: float = 1.2

    # 8. Realistic Capital Utilization & Opportunity Gating
    max_capital_utilization: float = 0.95
    prevent_infinite_compounding: bool = True
    max_market_capacity_usd: float = 5000.0


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
    fill_probability: float = 1.0

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
    markout_drift: float = 0.0
    markout_haircut_usd: float = 0.0
    taker_fee_usd: float = 0.0
    slippage_usd: float = 0.0
    fill_probability: float = 1.0

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
            "markout_drift": round(self.markout_drift, 4),
            "markout_haircut_usd": round(self.markout_haircut_usd, 4),
            "taker_fee_usd": round(self.taker_fee_usd, 4),
            "slippage_usd": round(self.slippage_usd, 4),
            "fill_probability": round(self.fill_probability, 4),
        }


@dataclass
class PendingSettlement:
    """Record of a market resolution pending the mandatory 3.11-hour UMA settlement lockup."""

    settlement_id: str
    condition_id: str
    resolution_timestamp: float
    unlock_timestamp: float  # resolution_timestamp + 3.11 * 3600
    payout_usd: float
    redemption_gas_usd: float
    net_payout_usd: float
    payout_numerators: dict[str, float]
    shares_redeemed: dict[str, float]
    is_unlocked: bool = False
    unlocked_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "settlement_id": self.settlement_id,
            "condition_id": self.condition_id,
            "resolution_timestamp": self.resolution_timestamp,
            "unlock_timestamp": self.unlock_timestamp,
            "payout_usd": round(self.payout_usd, 4),
            "redemption_gas_usd": round(self.redemption_gas_usd, 4),
            "net_payout_usd": round(self.net_payout_usd, 4),
            "payout_numerators": self.payout_numerators,
            "shares_redeemed": {k: round(v, 4) for k, v in self.shares_redeemed.items()},
            "is_unlocked": self.is_unlocked,
            "unlocked_at": self.unlocked_at,
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
    event_id: Optional[str] = None  # Gamma event grouping for correlated-event caps
