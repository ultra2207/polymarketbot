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
    DynamicASCalibrator,
    InventoryRegime,
    QuotePair,
)
from polymarketbot.trading.binance_quant_engine import (
    CrossVenueLeadLagKalmanFilter,
    HighFrequencyEWMAVolEstimator,
    HighFrequencyVolEstimator,
    MultiLevelOrderFlowImbalanceTracker,
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
from polymarketbot.trading.liquidity_router import (
    ActiveSettlementLoan,
    CashInventoryManager,
    InterDomainLiquidityRouter,
    LiquidityRouterConfig,
    SimpleCashInventoryManager,
)
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
    DynamicQueueDepletionTracker,
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
    "ActiveSettlementLoan",
    "AvellanedaStoikovEngine",
    "BaseMarketMakingEngine",
    "BinanceAnchoredQuoter",
    "BinanceQuoteDecision",
    "BinanceSpotFeed",
    "BlackScholesBinaryEngine",
    "CancellationDecision",
    "CancellationModel",
    "CancellationUrgency",
    "CashInventoryManager",
    "ClobAuthCredentials",
    "CoherenceChecker",
    "ComplementMergeRecord",
    "ComplementQuoteDecision",
    "ComplementQuoter",
    "ComplementQuoterConfig",
    "ConversionPlanner",
    "compute_taker_fee",
    "CrossVenueLeadLagKalmanFilter",
    "DynamicASCalibrator",
    "DynamicQueueDepletionTracker",
    "estimate_conversion_gas_usd",
    "ExitCostComparison",
    "ExitPathway",
    "FastL2Book",
    "HighFrequencyEWMAVolEstimator",
    "HighFrequencyVolEstimator",
    "HorizonRegime",
    "InterDomainLiquidityRouter",
    "InventoryRegime",
    "LegQuote",
    "LifecycleExitOptimizer",
    "LiquidityRouterConfig",
    "LiveClobClient",
    "MarketHorizonClassifier",
    "MarketHorizonInfo",
    "MergeExecutionResult",
    "MultiLevelOrderFlowImbalanceTracker",
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
    "SimpleCashInventoryManager",
    "TrackedOrder",
    "VirtualQueueOrderBookEngine",
]

