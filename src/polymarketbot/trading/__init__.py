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
from polymarketbot.trading.ctf_allocator import (
    ArbitrageDirection,
    ExecutionMode,
    MarginalCtfAllocator,
    MarketAllocation,
    ParityLegDepth,
    ParityTranche,
    PortfolioAllocationResult,
    SingleMarketSweepResult,
)
from polymarketbot.trading.ctf_split_arbitrage import (
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
    AllocationSlice,
    CapitalRouter,
    CashInventoryManager,
    InterDomainLiquidityRouter,
    LiquidityRouterConfig,
    MetaAllocationPlan,
    MetaOptimizerCapitalRouter,
    SimpleCashInventoryManager,
    StrategyCapacityModel,
    StrategyName,
    get_default_strategy_models,
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

__all__ = [
    "ASInventoryConfig",
    "ASQuoteDecision",
    "ActiveSettlementLoan",
    "AllocationSlice",
    "AvellanedaStoikovEngine",
    "BaseMarketMakingEngine",
    "BinanceAnchoredQuoter",
    "BinanceLatencySniperEngine",
    "BinanceQuoteDecision",
    "BinanceSpotFeed",
    "BlackScholesBinaryEngine",
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
    "estimate_conversion_gas_usd",
    "ExitCostComparison",
    "ExitPathway",
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
    "MetaAllocationPlan",
    "MetaOptimizerCapitalRouter",
    "MultiLevelOrderFlowImbalanceTracker",
    "NegRiskEngine",
    "NegRiskExecutionPlan",
    "NegRiskExecutionResult",
    "NegRiskMarket",
    "NegRiskOpportunity",
    "OpportunityType",
    "OrderPlacementResult",
    "OutcomeLeg",
    "PolygonCtfClient",
    "PolymarketTopBook",
    "QuotePair",
    "RealMoneyTradingEngine",
    "SimpleCashInventoryManager",
    "SnipeDecision",
    "SniperTelemetry",
    "StrategyCapacityModel",
    "get_default_strategy_models",
    "TakerOrder",
    "VIP_REBATE_RATES",
    # Institutional Strategy Nomenclature
    "StrategyName",
    "CapitalRouter",
    "LatencySniperEngine",
    "RewardHarvesterEngine",
    # Marginal CTF Capital Allocator
    "ArbitrageDirection",
    "ExecutionMode",
    "MarginalCtfAllocator",
    "MarketAllocation",
    "ParityLegDepth",
    "ParityTranche",
    "PortfolioAllocationResult",
    "SingleMarketSweepResult",
]

# Canonical Strategy Engine Aliases
LatencySniperEngine = BinanceLatencySniperEngine
RewardHarvesterEngine = RealMoneyTradingEngine

