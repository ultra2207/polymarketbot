"""Live real-time paper trading system for Polymarket liquidity reward harvesting."""

from polymarketbot.paper_trading.models import (
    VirtualQuote,
    Position,
    SimulatedFill,
    MergeEvent,
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
from polymarketbot.paper_trading.polysim_client import (
    PolySimulatorClient,
    PolySimBalance,
    PolySimOrderResult,
)
from polymarketbot.paper_trading.polysim_db import PolySimDatabase
from polymarketbot.paper_trading.polysim_trader import PolySimulatorTrader
from polymarketbot.paper_trading.autonomous_daemon import AutonomousHarvesterDaemon

__all__ = [
    "VirtualQuote",
    "Position",
    "SimulatedFill",
    "MergeEvent",
    "EpochSnapshot",
    "MarketConfig",
    "compute_quadratic_score",
    "compute_competing_depth_score",
    "compute_epoch_payout",
    "PaperTradingEngine",
    "PolymarketLiveClient",
    "PolySimulatorClient",
    "PolySimBalance",
    "PolySimOrderResult",
    "PolySimulatorTrader",
    "PolySimDatabase",
    "AutonomousHarvesterDaemon",
]



