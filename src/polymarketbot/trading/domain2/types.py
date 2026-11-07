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
