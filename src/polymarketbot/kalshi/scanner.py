"""Kalshi Dynamic Opportunity Scanner.

Identifies unpegged, high-yield niche markets on Kalshi:
1. Microstructure filter:
   - Filters OUT tight 1c pegged books with massive HFT queue depth (Jane Street / Susquehanna books).
   - Filters IN unpegged niche markets with wide spreads (3c <= spread <= 15c).
   - Filters IN markets with manageable competing depth (< 500 contracts).
2. Computes Net Expected Yield per Dollar on allocated bankroll:
   - Expected spread capture at 25-50 contract quote size.
   - Eligible incentive program rewards with strict $1.00 daily payout floor enforcement.
3. Ranks candidates by net expected return on capital.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from polymarketbot.kalshi.client import KalshiClient
from polymarketbot.logs import get_logger

logger = get_logger("kalshi_scanner")

# Filter thresholds
MIN_SPREAD_CENTS = 3
MAX_SPREAD_CENTS = 15
MAX_COMPETING_DEPTH = 500
MIN_QUOTE_SIZE = 25
MAX_QUOTE_SIZE = 50
INCENTIVE_DAILY_FLOOR = 1.00  # Kalshi requires >= $1.00 daily payout floor


@dataclass
class KalshiMarketCandidate:
    """Evaluated niche market opportunity on Kalshi."""

    ticker: str
    title: str
    yes_bid: int
    yes_ask: int
    spread_cents: int
    competing_depth: int
    quote_size: int
    capital_required_dollars: float
    expected_spread_capture_dollars: float
    eligible_incentive_reward_dollars: float
    net_expected_daily_return_dollars: float
    net_expected_yield_per_dollar: float
    is_hft_dominated: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class KalshiOpportunityScanner:
    """Dynamic opportunity scanner for Kalshi markets."""

    def __init__(
        self,
        client: Optional[KalshiClient] = None,
        min_spread_cents: int = MIN_SPREAD_CENTS,
        max_spread_cents: int = MAX_SPREAD_CENTS,
        max_competing_depth: int = MAX_COMPETING_DEPTH,
        default_quote_size: int = 35,
    ) -> None:
        self.client = client or KalshiClient(mock_mode=True)
        self.min_spread_cents = min_spread_cents
        self.max_spread_cents = max_spread_cents
        self.max_competing_depth = max_competing_depth
        self.default_quote_size = max(MIN_QUOTE_SIZE, min(default_quote_size, MAX_QUOTE_SIZE))

    def evaluate_market(
        self,
        market: Dict[str, Any],
        bankroll: float = 300.0,
        quote_size: Optional[int] = None,
    ) -> Optional[KalshiMarketCandidate]:
        """Evaluate a single market against microstructure and yield criteria.

        Returns None if market fails microstructure filters.
        """
        ticker = market.get("ticker", "")
        title = market.get("title", ticker)

        # Extract bid/ask prices
        yes_bid = market.get("yes_bid")
        yes_ask = market.get("yes_ask")

        # Fallbacks for nested or alternate keys
        if yes_bid is None:
            yes_bid = market.get("best_bid", 0)
        if yes_ask is None:
            yes_ask = market.get("best_ask", 0)

        # Must have valid positive quotes
        if not (1 <= yes_bid < yes_ask <= 99):
            return None

        spread_cents = yes_ask - yes_bid

        # Competing depth at best bid/ask
        bid_depth = market.get("bid_depth", market.get("yes_bid_size", 0))
        ask_depth = market.get("ask_depth", market.get("yes_ask_size", 0))
        competing_depth = max(bid_depth, ask_depth)

        # Microstructure filter 1: Reject tight 1c or 2c pegged books (HFT-dominated Jane Street/Susquehanna)
        if spread_cents < self.min_spread_cents:
            logger.debug(f"Market {ticker} rejected: spread {spread_cents}c < {self.min_spread_cents}c (HFT pegged)")
            return None

        # Filter out completely illiquid / dead books with excessive spread
        if spread_cents > self.max_spread_cents:
            logger.debug(f"Market {ticker} rejected: spread {spread_cents}c > {self.max_spread_cents}c")
            return None

        # Microstructure filter 2: Reject massive competing queue depth (> 500 contracts)
        if competing_depth >= self.max_competing_depth:
            logger.debug(f"Market {ticker} rejected: competing depth {competing_depth} >= {self.max_competing_depth}")
            return None

        # Quote sizing (25 to 50 contracts)
        q_size = quote_size or self.default_quote_size
        q_size = max(MIN_QUOTE_SIZE, min(q_size, MAX_QUOTE_SIZE))

        # Capital required in dollars: each contract costs yes_bid / 100
        cost_per_contract = yes_bid / 100.0
        capital_required = round(q_size * cost_per_contract, 2)
        if capital_required > bankroll:
            q_size = max(MIN_QUOTE_SIZE, int(bankroll / cost_per_contract))
            capital_required = round(q_size * cost_per_contract, 2)

        if capital_required <= 0:
            return None

        # Spread capture estimation:
        # Assuming maker earns half the spread on round trips with fill probability ~0.4 per day
        half_spread_dollars = (spread_cents / 2.0) / 100.0
        expected_spread_capture = round(q_size * half_spread_dollars * 0.4 * 2.0, 4)

        # Incentive rewards: check if market has eligible volume / liquidity incentive
        raw_incentive = market.get("incentive_daily_estimate", 0.0)
        # Strict enforcement of Kalshi $1.00 daily payout floor:
        # If expected incentive is below $1.00, it pays out $0.00
        eligible_incentive = raw_incentive if raw_incentive >= INCENTIVE_DAILY_FLOOR else 0.0

        net_expected_daily = expected_spread_capture + eligible_incentive
        yield_per_dollar = round(net_expected_daily / capital_required, 6)

        return KalshiMarketCandidate(
            ticker=ticker,
            title=title,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            spread_cents=spread_cents,
            competing_depth=competing_depth,
            quote_size=q_size,
            capital_required_dollars=capital_required,
            expected_spread_capture_dollars=expected_spread_capture,
            eligible_incentive_reward_dollars=eligible_incentive,
            net_expected_daily_return_dollars=round(net_expected_daily, 4),
            net_expected_yield_per_dollar=yield_per_dollar,
            is_hft_dominated=False,
            metadata=market,
        )

    def scan_markets(
        self,
        bankroll: float = 300.0,
        quote_size: Optional[int] = None,
        limit: int = 100,
    ) -> List[KalshiMarketCandidate]:
        """Fetch open markets from Kalshi API and evaluate candidates."""
        res = self.client.get_markets(status="open", limit=limit)
        markets = res.get("markets", [])
        return self.rank_markets(markets=markets, bankroll=bankroll, quote_size=quote_size)

    def rank_markets(
        self,
        markets: List[Dict[str, Any]],
        bankroll: float = 300.0,
        quote_size: Optional[int] = None,
    ) -> List[KalshiMarketCandidate]:
        """Evaluate a list of markets and return candidates sorted descending by yield per dollar."""
        candidates: List[KalshiMarketCandidate] = []
        for m in markets:
            evaluated = self.evaluate_market(m, bankroll=bankroll, quote_size=quote_size)
            if evaluated:
                candidates.append(evaluated)

        # Sort descending by net expected yield per dollar
        candidates.sort(key=lambda c: c.net_expected_yield_per_dollar, reverse=True)
        return candidates
