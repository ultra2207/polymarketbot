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
from polymarketbot.trading.ctf_split_arbitrage import (
    CtfCrossSplitArbitrageEngine,
    CtfSplitArbConfig,
    CtfSplitExecutionPlan,
    CtfSplitExecutionResult,
    CtfSplitLeg,
    CtfSplitOpportunity,
    OrderBatchLeg,
    SplitTransactionPayload,
    encode_split_positions_calldata,
    estimate_polygon_eip1559_gas_usd,
)
from polymarketbot.trading.engine import RealMoneyTradingEngine
from polymarketbot.trading.latency_sniper import (
    BinanceLatencySniperEngine,
    LatencySniperConfig,
    PolymarketTopBook,
    SnipeDecision,
    SniperTelemetry,
    TakerOrder,
    VIP_REBATE_RATES,
    calculate_taker_fee as calculate_latency_sniper_taker_fee,
    get_vip_rebate_rate,
)
from polymarketbot.trading.liquidity_router import (
    ActiveSettlementLoan,
    CapitalRouter,
    CashInventoryManager,
    InterDomainLiquidityRouter,
    LiquidityRouterConfig,
    SimpleCashInventoryManager,
    StrategyName,
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
    "BinanceLatencySniperEngine",
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
    "CtfCrossSplitArbitrageEngine",
    "CtfSplitArbConfig",
    "CtfSplitExecutionPlan",
    "CtfSplitExecutionResult",
    "CtfSplitLeg",
    "CtfSplitOpportunity",
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
    "LatencySniperConfig",
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
    "PolymarketTopBook",
    "QueueAwareCancellationPolicy",
    "QueueCancellationConfig",
    "QueuePositionTracker",
    "QuotePair",
    "RateLimitTokenBucket",
    "RealMoneyTradingEngine",
    "SimpleCashInventoryManager",
    "SnipeDecision",
    "SniperTelemetry",
    "TakerOrder",
    "TrackedOrder",
    "VIP_REBATE_RATES",
    "VirtualQueueOrderBookEngine",
    # Institutional Strategy Nomenclature
    "StrategyName",
    "CapitalRouter",
    "LatencySniperEngine",
    "CtfSplitterEngine",
    "RewardHarvesterEngine",
]

# Canonical Strategy Engine Aliases
LatencySniperEngine = BinanceLatencySniperEngine
CtfSplitterEngine = CtfCrossSplitArbitrageEngine
RewardHarvesterEngine = RealMoneyTradingEngine

