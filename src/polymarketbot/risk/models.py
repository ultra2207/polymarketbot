"""Data models and configuration for the Polymarket Risk Management System.

Tailored for small capital ($300 initial bankroll) and multi-market scaling.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Tuple


class RiskStatus(str, Enum):
    """Lifecycle risk state for individual markets or entire portfolio."""

    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    COOLDOWN = "COOLDOWN"
    HALTED = "HALTED"
    KILLED = "KILLED"


class CircuitBreakerReason(str, Enum):
    """Reasons triggering risk halts and emergency cancellations."""

    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    MAX_DRAWDOWN_LIMIT = "MAX_DRAWDOWN_LIMIT"
    PRICE_JUMP_VOLATILITY = "PRICE_JUMP_VOLATILITY"
    ASYMMETRIC_TOXICITY = "ASYMMETRIC_TOXICITY"
    DISCONNECT_TIMEOUT = "DISCONNECT_TIMEOUT"
    POSITION_LIMIT_EXCEEDED = "POSITION_LIMIT_EXCEEDED"
    PORTFOLIO_EXPOSURE_EXCEEDED = "PORTFOLIO_EXPOSURE_EXCEEDED"
    CAPITAL_CAP_EXCEEDED = "CAPITAL_CAP_EXCEEDED"
    GAS_SPEND_CAP_EXCEEDED = "GAS_SPEND_CAP_EXCEEDED"
    MANUAL_KILL = "MANUAL_KILL"


@dataclass
class CircuitBreakerEvent:
    """Audit record when a circuit breaker or kill switch trips."""

    timestamp: float
    reason: CircuitBreakerReason
    details: str
    condition_id: Optional[str] = None
    asset_id: Optional[str] = None
    action_taken: str = "CANCEL_QUOTES"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "reason": self.reason.value if isinstance(self.reason, Enum) else str(self.reason),
            "details": self.details,
            "condition_id": self.condition_id,
            "asset_id": self.asset_id,
            "action_taken": self.action_taken,
        }


@dataclass
class RiskConfig:
    """Risk configuration calibrated for small bankroll ($300) and multi-market scaling."""

    # 1. Hard Capital & Position Limits
    total_bankroll_usd: float = 300.0  # Max allocated bankroll ($300)
    max_position_cost_per_market: Optional[float] = None  # None = dynamically derived from concentration pct
    max_position_shares_per_token: Optional[float] = None  # None = dynamically derived
    max_portfolio_inventory_usd: Optional[float] = None  # None = dynamically derived
    max_portfolio_inventory_shares: float = 400.0  # Max total inventory shares
    max_quote_size_shares: float = 50.0  # Max single quote size in shares
    max_active_markets: int = 5  # Multi-market scaling cap

    # Micro-capital concentration model (for total_bankroll_usd <= $500)
    concentrated_micro_capital: bool = True
    micro_capital_concentration_pct: float = 0.75  # 70%–85% capital concentration ($225 on $300)
    micro_capital_cash_reserve_pct: float = 0.25   # 15%–30% liquid cash reserve for synthetic hedging ($75 on $300)

    def __post_init__(self) -> None:
        if self.max_position_cost_per_market is None:
            if self.total_bankroll_usd <= 500.0 and self.concentrated_micro_capital:
                self.max_position_cost_per_market = round(
                    self.total_bankroll_usd * self.micro_capital_concentration_pct, 2
                )
            else:
                self.max_position_cost_per_market = round(self.total_bankroll_usd * 0.20, 2)

        if self.max_position_shares_per_token is None:
            if self.total_bankroll_usd <= 500.0 and self.concentrated_micro_capital:
                self.max_position_shares_per_token = round(
                    self.max_position_cost_per_market / 0.50, 1
                )
            else:
                self.max_position_shares_per_token = 150.0

        if self.max_portfolio_inventory_usd is None:
            if self.total_bankroll_usd <= 500.0 and self.concentrated_micro_capital:
                self.max_portfolio_inventory_usd = round(
                    self.total_bankroll_usd * self.micro_capital_concentration_pct, 2
                )
            else:
                self.max_portfolio_inventory_usd = 200.0

    # 2. Max Drawdown Circuit Breakers
    daily_loss_limit_usd: float = 15.0  # Stop quoting if daily loss > $15 (5% of $300)
    daily_loss_limit_pct: float = 0.05  # 5% daily loss limit
    max_drawdown_usd: float = 30.0  # Stop engine if cumulative loss > $30 (10% of $300)
    max_drawdown_pct: float = 0.10  # 10% max drawdown limit

    # 3. Price Jump & Volatility Circuit Breaker
    price_jump_threshold_cents: float = 2.0  # Cancel quotes if mid moves > 2.0c
    price_jump_window_seconds: float = 60.0  # Rolling 60s monitor window
    cooldown_duration_seconds: float = 300.0  # 5 minutes cooldown (300s)

    # 4. Asymmetric Toxicity / Inventory Skew Limiter
    consecutive_fills_threshold: int = 3  # 2-3 consecutive fills in same direction
    toxicity_action: str = "widen"  # "widen" or "suppress"
    toxicity_spread_penalty_cents: float = 1.5  # Extra spread penalty if widening
    toxicity_size_reduction_pct: float = 0.5  # Reduce quote size by 50% when widened
    toxicity_cooldown_seconds: float = 120.0  # Reset after 2 minutes of no toxic fills

    # 5. Gas & Complete-Set Merge Manager
    min_merge_threshold_shares: float = 30.0  # Small capital merge threshold (25-50 shares, default 30)
    gas_fee_usd: float = 0.015  # Polygon CTF mergePositions gas fee ($0.015)
    max_gas_spread_pct: float = 0.05  # Gas fee cannot exceed 5% of captured spread
    assumed_spread_cents: float = 1.0  # Baseline 1.0c spread per share for gas viability
    daily_gas_cap_usd: float = 1.50  # Hard daily gas cap ($1.50)

    # 6. Emergency Kill-Switch & Disconnect Monitor
    heartbeat_timeout_seconds: float = 15.0  # Trip on WS/REST heartbeat loss > 15s
    auto_unquote_on_disconnect: bool = True

    # 7. Institutional Hybrid Rebalancing & Active Synthetic Hedging (Regime B)
    enable_synthetic_hedging: bool = True
    q_urgency_threshold_shares: float = 60.0  # Switch from passive skew to active hedge
    max_synthetic_cross_spread_cents: float = 0.5  # Max spread crossing slippage to lock pair
    toxic_markout_threshold_cents: float = 1.5  # 30s markout loss > 1.5c triggers emergency cross
    markout_window_seconds: float = 30.0  # Post-fill markout window


@dataclass
class MarketRiskState:
    """State tracking for a single market / token."""

    condition_id: str
    asset_id: str
    price_history: Deque[Tuple[float, float]] = field(default_factory=deque)  # (timestamp, mid_price)
    consecutive_buys: int = 0
    consecutive_sells: int = 0
    last_fill_side: Optional[str] = None
    last_fill_timestamp: float = 0.0
    cooldown_until: float = 0.0
    is_toxic_widened: bool = False
    is_toxic_suppressed: bool = False
    toxic_side: Optional[str] = None
    last_jump_detected_time: float = 0.0
    status: RiskStatus = RiskStatus.HEALTHY


@dataclass
class PortfolioRiskState:
    """Global portfolio risk metrics and circuit breaker status."""

    bankroll_usd: float = 300.0
    peak_equity_usd: float = 300.0
    day_start_equity_usd: float = 300.0
    current_cash_usd: float = 300.0
    current_positions_value_usd: float = 0.0
    current_equity_usd: float = 300.0
    daily_realized_pnl: float = 0.0
    daily_unrealized_pnl: float = 0.0
    daily_loss_usd: float = 0.0
    cumulative_drawdown_usd: float = 0.0
    cumulative_drawdown_pct: float = 0.0
    daily_gas_spent_usd: float = 0.0
    total_gas_spent_usd: float = 0.0
    total_merges_count: int = 0
    status: RiskStatus = RiskStatus.HEALTHY
    kill_switch_active: bool = False
    is_halted: bool = False
    last_heartbeat_timestamp: float = field(default_factory=time.time)
    circuit_breaker_events: List[CircuitBreakerEvent] = field(default_factory=list)

    @property
    def total_pnl(self) -> float:
        return self.daily_realized_pnl + self.daily_unrealized_pnl

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "bankroll_usd": round(self.bankroll_usd, 2),
            "current_equity_usd": round(self.current_equity_usd, 2),
            "peak_equity_usd": round(self.peak_equity_usd, 2),
            "daily_loss_usd": round(self.daily_loss_usd, 2),
            "cumulative_drawdown_usd": round(self.cumulative_drawdown_usd, 2),
            "cumulative_drawdown_pct": round(self.cumulative_drawdown_pct, 4),
            "daily_gas_spent_usd": round(self.daily_gas_spent_usd, 4),
            "total_merges_count": self.total_merges_count,
            "kill_switch_active": self.kill_switch_active,
            "is_halted": self.is_halted,
            "events_count": len(self.circuit_breaker_events),
        }
