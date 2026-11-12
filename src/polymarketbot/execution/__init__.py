"""Execution support that live trading actually uses.

The dedicated-cancel pool, the HFT connection pool and the pre-signed order
cache were removed: no caller in ``src/`` enabled them, their documented
latency claims were untrue (the fast-cancel HMAC was computed over a different
body than it sent, so every cancel would 401), and a dormant money path that
looks wired is worse than no path at all.
"""

from polymarketbot.execution.self_trade import (
    MakerOrderRecord,
    SelfTradeGuard,
    STPAction,
    STPCheckResult,
)

__all__ = [
    "MakerOrderRecord",
    "STPAction",
    "STPCheckResult",
    "SelfTradeGuard",
]
