"""Polymarket Native Simulation Engine.

Provides:
- SimulatedPortfolio: CTF portfolio accounting (split, merge, cash, average cost basis).
- NativePaperTradingEngine: Native simulator orchestrator with 2-tier deskewing & quadratic rewards.
- SyntheticOrderBook: L2 depth state tracker with queue position modeling.
- MarketImpactEngine: Endogenous market impact and slippage model.
- LivePolymarketFeed: Zero-API-key direct public CLOB WebSocket and REST feed (when available).
- SimulationOrderMatcher: High-fidelity order matching against resting quotes (when available).
"""

from __future__ import annotations

from polymarketbot.simulator.book import (
    DepthInfo,
    SyntheticOrderBook,
    VirtualMakerQuote,
)
from polymarketbot.simulator.impact import (
    MarketImpactEngine,
    QuoteQueueTracker,
    QuoteSide,
    TradeImpactResult,
    compute_kyles_price_impact,
    deplete_queue_step,
)
from polymarketbot.simulator.portfolio import (
    MergeResult,
    PortfolioTransaction,
    Position,
    SimulatedPortfolio,
    SplitResult,
    TransactionType,
)
from polymarketbot.simulator.engine import (
    FillRecord,
    NativePaperTradingEngine,
    OrderMatcherBridge,
)
from polymarketbot.simulator.optimizer import (
    derive_quote_size,
    optimize_spread_offset,
)

# Optional exports from feed & matcher modules
try:
    from polymarketbot.simulator.feed import (
        LivePolymarketFeed,
        MarketTrade,
        OrderBookLevel,
        OrderBookSnapshot,
    )
except ImportError:
    LivePolymarketFeed = None  # type: ignore
    MarketTrade = None  # type: ignore
    OrderBookLevel = None  # type: ignore
    OrderBookSnapshot = None  # type: ignore

try:
    from polymarketbot.simulator.matcher import (
        FillEvent,
        SimulationOrderMatcher,
    )
except ImportError:
    FillEvent = None  # type: ignore
    SimulationOrderMatcher = None  # type: ignore

__all__ = [
    "SimulatedPortfolio",
    "Position",
    "PortfolioTransaction",
    "TransactionType",
    "MergeResult",
    "SplitResult",
    "NativePaperTradingEngine",
    "FillRecord",
    "OrderMatcherBridge",
    "SyntheticOrderBook",
    "DepthInfo",
    "VirtualMakerQuote",
    "MarketImpactEngine",
    "QuoteQueueTracker",
    "QuoteSide",
    "TradeImpactResult",
    "compute_kyles_price_impact",
    "deplete_queue_step",
    "LivePolymarketFeed",
    "MarketTrade",
    "OrderBookLevel",
    "OrderBookSnapshot",
    "FillEvent",
    "SimulationOrderMatcher",
    "derive_quote_size",
    "optimize_spread_offset",
]
