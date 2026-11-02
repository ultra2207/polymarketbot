"""Live real-time paper trading system for Polymarket liquidity reward harvesting."""

from polymarketbot.paper_trading.models import (
    VirtualQuote,
    Position,
    SimulatedFill,
    EpochSnapshot,
    MarketConfig,
)
from polymarketbot.paper_trading.rewards import (
    compute_quadratic_score,
    compute_competing_depth_score,
    compute_epoch_payout,
)
from polymarketbot.paper_trading.engine import PaperTradingEngine
from polymarketbot.paper_trading.client import PolymarketLiveClient

__all__ = [
    "VirtualQuote",
    "Position",
    "SimulatedFill",
    "EpochSnapshot",
    "MarketConfig",
    "compute_quadratic_score",
    "compute_competing_depth_score",
    "compute_epoch_payout",
    "PaperTradingEngine",
    "PolymarketLiveClient",
]
