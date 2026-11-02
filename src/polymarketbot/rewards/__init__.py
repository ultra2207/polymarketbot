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
from polymarketbot.rewards.microstructure import (
    MicrostructureEngine,
    MicrostructureState,
    MultiAssetMicrostructureSentinel,
    QuoteAction,
)
from polymarketbot.rewards.scoring import (
    DEFAULT_COMPETITOR_LADDER,
    OrderQuote,
    calculate_epoch_payout,
    compute_book_side_scores,
    compute_competitor_ladder_score,
    compute_minimum_score,
    normalize_sample_score,
    order_spread_cents,
    quadratic_order_score,
)
from polymarketbot.rewards.scanner import (
    MarketOpportunity,
    OpportunityScanner,
    calculate_expected_yield,
    infer_market_category,
)
from polymarketbot.rewards.switching import (
    OpportunisticSwitchingEngine,
    SwitchEngineState,
    SwitchEvent,
    SwitchingConfig,
    calculate_switching_cost,
    evaluate_switching_hurdle,
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
    "DEFAULT_COMPETITOR_LADDER",
    "OrderQuote",
    "calculate_epoch_payout",
    "compute_book_side_scores",
    "compute_competitor_ladder_score",
    "compute_minimum_score",
    "normalize_sample_score",
    "order_spread_cents",
    "quadratic_order_score",
    "MicrostructureEngine",
    "MicrostructureState",
    "MultiAssetMicrostructureSentinel",
    "QuoteAction",
    "MarketOpportunity",
    "OpportunityScanner",
    "calculate_expected_yield",
    "infer_market_category",
    "OpportunisticSwitchingEngine",
    "SwitchEngineState",
    "SwitchEvent",
    "SwitchingConfig",
    "calculate_switching_cost",
    "evaluate_switching_hurdle",
]

