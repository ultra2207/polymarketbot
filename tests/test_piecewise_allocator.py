"""Unit tests for Domain 2 Piecewise-Linear Order Book Depth Ladder Optimization."""

import pytest
from polymarketbot.trading.domain2.ortools_allocator import (
    Domain2Candidate,
    Domain2OrtoolsAllocator,
)


def test_piecewise_linear_ladder_allocation():
    """Verify that Google OR-Tools SCIP fills cheaper order book tiers before expensive ones."""
    allocator = Domain2OrtoolsAllocator(
        reserve_pct=0.10,          # 90% usable capital = $900
        max_drawdown_target=0.40,
        max_single_bet_pct=0.50,   # Max $500 per single market
        max_category_pct=0.60,
        max_active_markets=5,
        min_order_usd=20.0,
        depth_consumption_gamma=1.0,  # Test full capacity
        variance_penalty_lambda=2.0,
    )

    # Market A: Multi-level ladder
    # Tier 0: 100 shares @ $0.95 = $95 cap
    # Tier 1: 200 shares @ $0.96 = $192 cap
    # Tier 2: 500 shares @ $0.98 = $490 cap
    cand_a = Domain2Candidate(
        condition_id="0xAAAA",
        token_id="tokA",
        question="Market A with Orderbook Ladder",
        category="crypto",
        price=0.95,
        hours_to_resolution=1.0,
        depth_usd=777.0,
        p_hat=0.999,
        fee_rate=0.0,
        order_book_levels=[
            (0.95, 100.0),  # $95.00
            (0.96, 200.0),  # $192.00
            (0.98, 500.0),  # $490.00
        ],
    )

    # Market B: High price, single tier
    cand_b = Domain2Candidate(
        condition_id="0xBBBB",
        token_id="tokB",
        question="Market B Flat",
        category="politics",
        price=0.97,
        hours_to_resolution=2.0,
        depth_usd=500.0,
        p_hat=0.998,
        fee_rate=0.0,
        order_book_levels=[(0.97, 500.0)],
    )

    result = allocator.optimize_allocation([cand_a, cand_b], bankroll_usd=1000.0)

    assert result.solver_status == "OPTIMAL"
    assert result.total_allocated_usd > 0.0

    alloc_map = {a.condition_id: a for a in result.allocations}
    assert "0xAAAA" in alloc_map

    alloc_a = alloc_map["0xAAAA"]
    # Cand A has higher edge (0.95 and 0.96 with 1.0h vs 0.97 with 2.0h)
    # It should consume Tier 0 ($95) and Tier 1 ($192) = at least $287
    assert alloc_a.allocated_usd >= 287.0
    assert alloc_a.levels_consumed >= 2
    # Effective price must reflect volume-weighted average (between 0.95 and 0.98)
    assert 0.95 <= alloc_a.effective_avg_price <= 0.98
    assert alloc_a.shares > 0.0
