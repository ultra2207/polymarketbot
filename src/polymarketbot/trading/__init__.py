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

__all__ = [
    "BaseMarketMakingEngine",
    "BinanceAnchoredQuoter",
    "BinanceQuoteDecision",
    "BinanceSpotFeed",
    "BlackScholesBinaryEngine",
    "ClobAuthCredentials",
    "ExitCostComparison",
    "ExitPathway",
    "HorizonRegime",
    "LifecycleExitOptimizer",
    "LiveClobClient",
    "MarketHorizonClassifier",
    "MarketHorizonInfo",
    "MergeExecutionResult",
    "OrderPlacementResult",
    "PolygonCtfClient",
    "RealMoneyTradingEngine",
]

