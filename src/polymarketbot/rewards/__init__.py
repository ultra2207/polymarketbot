"""Polymarket Liquidity Rewards bot and backtesting package."""

from polymarketbot.rewards.engine import (
    CATEGORY_REGIMES,
    EpochReport,
    LiquidityRewardsBotEngine,
    MarketRewardParameters,
)
from polymarketbot.rewards.inventory import (
    AvellanedaStoikovModel,
    SkewedQuote,
)
from polymarketbot.rewards.merger import (
    CompleteSetPosition,
    MergeEvent,
    build_ctf_merge_transaction,
)
from polymarketbot.rewards.queue import (
    FillExecution,
    OrderBookQueueSimulator,
    RestingOrder,
)
from polymarketbot.rewards.scoring import (
    OrderQuote,
    calculate_epoch_payout,
    compute_book_side_scores,
    compute_minimum_score,
    normalize_sample_score,
    order_spread_cents,
    quadratic_order_score,
)

__all__ = [
    "CATEGORY_REGIMES",
    "EpochReport",
    "LiquidityRewardsBotEngine",
    "MarketRewardParameters",
    "AvellanedaStoikovModel",
    "SkewedQuote",
    "CompleteSetPosition",
    "MergeEvent",
    "build_ctf_merge_transaction",
    "FillExecution",
    "OrderBookQueueSimulator",
    "RestingOrder",
    "OrderQuote",
    "calculate_epoch_payout",
    "compute_book_side_scores",
    "compute_minimum_score",
    "normalize_sample_score",
    "order_spread_cents",
    "quadratic_order_score",
]
