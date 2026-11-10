"""Polymarket Liquidity Rewards bot and backtesting package."""

from polymarketbot.strategy.reward.microstructure import (
    MicrostructureEngine,
    MicrostructureState,
    MultiAssetMicrostructureSentinel,
    QuoteAction,
)
from polymarketbot.strategy.reward.scoring import (
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
from polymarketbot.strategy.reward.scanner import (
    MarketOpportunity,
    OpportunityScanner,
    calculate_expected_yield,
    infer_market_category,
)
from polymarketbot.strategy.reward.switching import (
    OpportunisticSwitchingEngine,
    SwitchEngineState,
    SwitchEvent,
    SwitchingConfig,
    calculate_switching_cost,
    evaluate_switching_hurdle,
)
from polymarketbot.strategy.reward.deskew import (
    DeSkewDecision,
    DeSkewPathway,
    DynamicDeSkewOptimizer,
)

__all__ = [
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
    "DeSkewDecision",
    "DeSkewPathway",
    "DynamicDeSkewOptimizer",
]

