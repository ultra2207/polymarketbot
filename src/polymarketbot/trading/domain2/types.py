"""Domain 2 Types and State Definitions.

Defines data structures for the UMA Settlement Basis & Resolution Discount Harvester.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any


class Domain2MarketState(str, Enum):
    """Lifecycle state of a condition within Domain 2."""
    DISCOVERED = "DISCOVERED"
    PROPOSED = "PROPOSED"
    QUOTING = "QUOTING"
    FILLED = "FILLED"
    LIVENESS_ACTIVE = "LIVENESS_ACTIVE"
    DISPUTED_HALTED = "DISPUTED_HALTED"
    RESOLVED = "RESOLVED"
    REDEEMING = "REDEEMING"
    SETTLED = "SETTLED"


@dataclass
class UmaProposalEvent:
    """Event emitted when UMA Optimistic Oracle receives a price proposal."""
    condition_id: str
    requester: str
    proposer: str
    identifier: str
    proposed_price: float
    request_timestamp: int
    expiration_timestamp: int
    ancillary_data: str
    block_number: int
    tx_hash: str
    raw_event: Optional[Dict[str, Any]] = None


@dataclass
class UmaDisputeEvent:
    """Event emitted when a proposed price is disputed during the liveness window."""
    condition_id: str
    requester: str
    proposer: str
    disputer: str
    identifier: str
    proposed_price: float
    block_number: int
    tx_hash: str
    raw_event: Optional[Dict[str, Any]] = None


@dataclass
class ConditionResolutionEvent:
    """Event emitted by the CTF contract upon on-chain resolution."""
    condition_id: str
    oracle: str
    question_id: int
    outcome_slot_count: int
    payout_numerators: List[int]
    winning_outcome_index: Optional[int]
    is_void: bool
    block_number: int
    tx_hash: str


class MakerExecutionRegime(str, Enum):
    """Maker quote execution regime for settlement discount harvesting."""
    MAKER_IMPROVE_INSIDE_SPREAD = "MAKER_IMPROVE_INSIDE_SPREAD"  # Option A: Penny inside spread (Q_ahead = 0, first in line)
    MAKER_JOIN_TOP_QUEUE = "MAKER_JOIN_TOP_QUEUE"                # Option B: Join top queue at best_bid (wider profit, queue delay)
    TAKER_SWEEP_IMMEDIATE = "TAKER_SWEEP_IMMEDIATE"              # Taker Fallback: Immediate sweep when expiry acute & edge > fee


@dataclass
class LiveBookMetrics:
    """Real-time market depth, queue ahead, trade velocity, and drift metrics."""
    best_bid: float
    best_ask: float
    spread: float
    standing_depth_ahead: float
    taker_sell_velocity_shares_hr: float
    drift_rate_usd_hr: float
    discount_bps: float
    is_live_book: bool = True


@dataclass
class MakerPricingDecision:
    """Detailed decision output from SettlementMakerPricingEngine."""
    regime: MakerExecutionRegime
    quote_price: float
    quote_size: float
    side: str = "BUY"
    p_fill: float = 0.0
    t_clear_hours: float = 0.0
    expected_arr: float = 0.0
    spread: float = 0.0
    standing_depth_ahead: float = 0.0
    taker_sell_velocity_shares_hr: float = 0.0
    is_taker: bool = False
    fee_rate_bps: float = 0.0
    reason: str = ""
    drift_rate_usd_hr: float = 0.0


@dataclass
class DiscountOpportunity:
    """Evaluated discount opportunity on a proposed condition."""
    condition_id: str
    winning_asset_id: str
    losing_asset_id: str
    proposed_price: float
    best_bid: float
    best_ask: float
    discount_bps: float
    hours_to_liveness_expiry: float
    recommended_bid_price: float
    recommended_size_usdc: float
    standing_depth_ahead: float = 0.0
    taker_sell_velocity_shares_hr: float = 0.0
    tick_size: float = 0.001
    drift_rate_usd_hr: float = 0.0


@dataclass
class Domain2Position:
    """Tracked inventory position awaiting dispute expiry and CTF redemption."""
    condition_id: str
    asset_id: str
    shares: float
    cost_basis_usdc: float
    entry_price: float
    liveness_expiry_ts: int
    proposed_price: float
    is_disputed: bool = False
    is_resolved: bool = False
    is_redeemed: bool = False
    redeem_tx_hash: Optional[str] = None
    realized_pnl: Optional[float] = None


@dataclass
class BinaryMarketVolatilityMetrics:
    """Institutional empirical volatility and entropy metrics for binary contracts."""
    realized_qv: Optional[float] = None
    implied_vol: Optional[float] = None
    entropy: float = 0.0
    effective_annual_vol: float = 0.0
    method: str = "entropy"


@dataclass
class DynamicCashReserveMetrics:
    """Institutional cash inventory and queuing metrics (Miller-Orr / Baumol-Tobin)."""
    target_reserve_pct: float
    target_reserve_usd: float
    miller_orr_z_usd: float
    var_buffer_usd: float
    arrival_intensity: float
    lockup_hours: float
    stockout_prob_pct: float
    min_reserve_pct: float
    max_reserve_pct: float


@dataclass
class OrderBookElasticityMetrics:
    """Continuous price impact elasticity and order book profile metrics."""
    elasticity: float
    concave_alpha: float
    dynamic_depth_gamma: float
    continuous_kyle_lambda: float
    optimal_capacity_usd: float


@dataclass
class DynamicSettlementDelayMetrics:
    """Dynamic UMA liveness, relayer queue latency, and on-chain gas metrics."""
    liveness_remaining_hours: float
    relayer_queue_delay_hours: float
    dispute_contingency_hours: float
    effective_turnover_latency_hours: float
    redemption_gas_usd: float

