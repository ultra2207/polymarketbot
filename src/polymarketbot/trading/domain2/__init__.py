"""Domain 2: UMA Settlement Basis & Resolution Discount Harvester.

Provides:
- Event Listener for UMA Optimistic Oracle and Polygon CTF resolution logs.
- Emergency Circuit Breakers triggered by UMA DisputePrice events.
- Passive CLOB Order Placer targeting 50-250 bps settlement discounts.
- Smart Settlement Worker executing on-chain `CTF.redeemPositions()`.
- Production Harvester Engine orchestrating the entire lifecycle.
"""

from __future__ import annotations

from polymarketbot.trading.domain2.types import (
    Domain2MarketState,
    UmaProposalEvent,
    UmaDisputeEvent,
    ConditionResolutionEvent,
    DiscountOpportunity,
    Domain2Position,
    MakerExecutionRegime,
    MakerPricingDecision,
    LiveBookMetrics,
)
from polymarketbot.trading.domain2.maker_pricing_engine import (
    SettlementMakerPricingEngine,
    MakerEngineConfig,
    RollingTradeBuffer,
    parse_l2_book,
    calculate_upward_drift_rate,
)
from polymarketbot.trading.domain2.event_listener import (
    Domain2EventListener,
    UMA_OO_V2,
    CTF_CONTRACT,
    TOPIC_PROPOSE_PRICE,
    TOPIC_DISPUTE_PRICE,
    TOPIC_CONDITION_RESOLUTION,
)
from polymarketbot.trading.domain2.circuit_breaker import Domain2CircuitBreaker
from polymarketbot.trading.domain2.clob_order_placer import Domain2ClobOrderPlacer
from polymarketbot.trading.domain2.settlement_worker import Domain2SettlementWorker
from polymarketbot.trading.domain2.harvester_engine import Domain2SettlementHarvester
from polymarketbot.trading.domain2.ortools_allocator import (
    Domain2OrtoolsAllocator,
    Domain2Candidate,
    Domain2Allocation,
    Domain2OptimizationResult,
)
from polymarketbot.trading.domain2.dynamic_sizer import (
    DynamicPositionSizer,
    DynamicSizerConfig,
    StrikeDistanceMetrics,
)

__all__ = [
    "Domain2MarketState",
    "UmaProposalEvent",
    "UmaDisputeEvent",
    "ConditionResolutionEvent",
    "DiscountOpportunity",
    "Domain2Position",
    "MakerExecutionRegime",
    "MakerPricingDecision",
    "LiveBookMetrics",
    "SettlementMakerPricingEngine",
    "MakerEngineConfig",
    "RollingTradeBuffer",
    "parse_l2_book",
    "calculate_upward_drift_rate",
    "Domain2EventListener",
    "Domain2CircuitBreaker",
    "Domain2ClobOrderPlacer",
    "Domain2SettlementWorker",
    "Domain2SettlementHarvester",
    "Domain2OrtoolsAllocator",
    "Domain2Candidate",
    "Domain2Allocation",
    "Domain2OptimizationResult",
    "DynamicPositionSizer",
    "DynamicSizerConfig",
    "StrikeDistanceMetrics",
    "UMA_OO_V2",
    "CTF_CONTRACT",
    "TOPIC_PROPOSE_PRICE",
    "TOPIC_DISPUTE_PRICE",
    "TOPIC_CONDITION_RESOLUTION",
]
