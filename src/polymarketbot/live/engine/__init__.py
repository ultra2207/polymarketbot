"""Production Real-Money Polymarket Trading Engine.

Operates symmetrically to NativePaperTradingEngine using BaseMarketMakingEngine,
but sends real signed EIP-712 orders to Polymarket CLOB and executes real CTF
contract merges on Polygon PoS.

Features:
1. Live EIP-712 Order Management via LiveClobClient (Chain ID 137).
2. Live On-Chain CTF Complete-Set Settlement via PolygonCtfClient.
3. Real Wallet & Spender Allowance Auditing (USDC & POL).
4. Full Integration with LivePolymarketFeed for real-time L2 order books & trades.
5. Master RiskManager Circuit Breakers: halts trading if mid-price jumps > 2.0c in 60s,
   daily loss limit ($15/5%), or drawdown limit ($30/10%).
6. Asymmetric Toxicity Quote Fading (Cont-Kukanov-Stoikov OFI & Stoikov micro-price drift).
7. Continuous Capital Compounding via ContinuousOptimizationGovernor & calculate_endogenous_reserve.
8. Opportunistic 300s Market Switching Hurdle via CapitalMigrationEngine.
9. Comprehensive Pre-flight Credential, RPC, and Balance Verification with Safe Dry-Run fallback.
"""

from polymarketbot.live.engine.markout import (
    MARKOUT_HORIZONS,
    MarkoutTracker,
    RealMoneyFillRecord,
)
from polymarketbot.live.engine.preflight import PreflightReport
from polymarketbot.live.engine.trading import RealMoneyTradingEngine

__all__ = [
    "MARKOUT_HORIZONS",
    "MarkoutTracker",
    "PreflightReport",
    "RealMoneyFillRecord",
    "RealMoneyTradingEngine",
]
