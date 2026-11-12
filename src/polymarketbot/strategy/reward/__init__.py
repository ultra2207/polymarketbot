"""Polymarket Liquidity Rewards bot and backtesting package."""

from polymarketbot.strategy.reward.deskew import (
    DeSkewDecision,
    DeSkewPathway,
    DynamicDeSkewOptimizer,
)
from polymarketbot.strategy.reward.microstructure import (
    MicrostructureEngine,
    MicrostructureState,
    MultiAssetMicrostructureSentinel,
    QuoteAction,
)
from polymarketbot.strategy.reward.scanner import (
    MarketOpportunity,
    OpportunityScanner,
    calculate_expected_yield,
    infer_market_category,
)
from polymarketbot.strategy.reward.scoring import (
    OrderQuote,
    calculate_epoch_payout,
    compute_book_side_scores,
    compute_minimum_score,
    normalize_sample_score,
    order_spread_cents,
    quadratic_order_score,
)
from polymarketbot.strategy.reward.switching import (
    OpportunisticSwitchingEngine,
    SwitchEngineState,
    SwitchEvent,
    SwitchingConfig,
    calculate_switching_cost,
    evaluate_switching_hurdle,
)

__all__ = [
    "DeSkewDecision",
    "DeSkewPathway",
    "DynamicDeSkewOptimizer",
    "MarketOpportunity",
    "MicrostructureEngine",
    "MicrostructureState",
    "MultiAssetMicrostructureSentinel",
    "OpportunisticSwitchingEngine",
    "OpportunityScanner",
    "OrderQuote",
    "QuoteAction",
    "SwitchEngineState",
    "SwitchEvent",
    "SwitchingConfig",
    "calculate_epoch_payout",
    "calculate_expected_yield",
    "calculate_switching_cost",
    "compute_book_side_scores",
    "compute_minimum_score",
    "evaluate_switching_hurdle",
    "infer_market_category",
    "normalize_sample_score",
    "order_spread_cents",
    "quadratic_order_score",
]

