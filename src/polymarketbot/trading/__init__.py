"""Real-Money Polymarket Trading Package.

Provides production-grade live CLOB order management, EIP-712 order signing,
on-chain Polygon CTF settlement, shared market-making core architecture,
and the real-money trading engine.
"""

from __future__ import annotations

from polymarketbot.trading.base import BaseMarketMakingEngine
from polymarketbot.trading.client import (
    ClobAuthCredentials,
    LiveClobClient,
    OrderPlacementResult,
)
from polymarketbot.trading.complement_quoter import (
    ComplementMergeRecord,
    ComplementQuoteDecision,
    ComplementQuoter,
    ComplementQuoterConfig,
    LegQuote,
)
from polymarketbot.trading.as_inventory_engine import (
    ASInventoryConfig,
    ASQuoteDecision,
    AvellanedaStoikovEngine,
    InventoryRegime,
    QuotePair,
)
from polymarketbot.trading.binance_anchor import (
    BinanceAnchoredQuoter,
    BinanceQuoteDecision,
    BinanceSpotFeed,
    BlackScholesBinaryEngine,
    ExitCostComparison,
    ExitPathway,
    HorizonRegime,
    LifecycleExitOptimizer,
    MarketHorizonClassifier,
    MarketHorizonInfo,
)
from polymarketbot.trading.ctf import MergeExecutionResult, PolygonCtfClient
from polymarketbot.trading.engine import RealMoneyTradingEngine
from polymarketbot.trading.negrisk_engine import (
    CoherenceChecker,
    ConversionPlanner,
    NegRiskEngine,
    NegRiskExecutionPlan,
    NegRiskExecutionResult,
    NegRiskMarket,
    NegRiskOpportunity,
    OpportunityType,
    OutcomeLeg,
    compute_taker_fee,
    estimate_conversion_gas_usd,
)
from polymarketbot.trading.virtual_queue_book import (
    CancellationDecision,
    CancellationModel,
    CancellationUrgency,
    FastL2Book,
    OrderBookLevel,
    OrderSide,
    QueueAwareCancellationPolicy,
    QueueCancellationConfig,
    QueuePositionTracker,
    RateLimitTokenBucket,
    TrackedOrder,
    VirtualQueueOrderBookEngine,
)

__all__ = [
    "ASInventoryConfig",
    "ASQuoteDecision",
    "AvellanedaStoikovEngine",
    "BaseMarketMakingEngine",
    "BinanceAnchoredQuoter",
    "BinanceQuoteDecision",
    "BinanceSpotFeed",
    "BlackScholesBinaryEngine",
    "CancellationDecision",
    "CancellationModel",
    "CancellationUrgency",
    "ClobAuthCredentials",
    "CoherenceChecker",
    "ComplementMergeRecord",
    "ComplementQuoteDecision",
    "ComplementQuoter",
    "ComplementQuoterConfig",
    "ConversionPlanner",
    "compute_taker_fee",
    "estimate_conversion_gas_usd",
    "ExitCostComparison",
    "ExitPathway",
    "FastL2Book",
    "HorizonRegime",
    "InventoryRegime",
    "LegQuote",
    "LifecycleExitOptimizer",
    "LiveClobClient",
    "MarketHorizonClassifier",
    "MarketHorizonInfo",
    "MergeExecutionResult",
    "NegRiskEngine",
    "NegRiskExecutionPlan",
    "NegRiskExecutionResult",
    "NegRiskMarket",
    "NegRiskOpportunity",
    "OpportunityType",
    "OrderBookLevel",
    "OrderPlacementResult",
    "OrderSide",
    "OutcomeLeg",
    "PolygonCtfClient",
    "QueueAwareCancellationPolicy",
    "QueueCancellationConfig",
    "QueuePositionTracker",
    "QuotePair",
    "RateLimitTokenBucket",
    "RealMoneyTradingEngine",
    "TrackedOrder",
    "VirtualQueueOrderBookEngine",
]

