"""Comprehensive mathematical and behavioral unit tests for MarginalCtfAllocator.

Verifies:
1. Multi-level L2 depth sweeping synthesis across multi-outcome books.
2. Monotonically decreasing marginal profit dPi/dC and strictly concave net profit Pi(C).
3. Exact global maximum C* where dPi/dC = 0 (and verification that C > C* reduces profit).
4. Fixed on-chain gas amortization and breakeven threshold C_breakeven.
5. Non-linear Polymarket taker fee deduction with VIP tier rebates.
6. Multi-market portfolio water-filling equalizing marginal return across funded markets.
7. Iterative active-set pruning for underfunded markets failing gas hurdles.
8. Merge arbitrage depth sweeping (buying asks under par and merging).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest

from polymarketbot.fees import FeeSchedule
from polymarketbot.trading.ctf_allocator import (
    ArbitrageDirection,
    ExecutionMode,
    MarginalCtfAllocator,
    MarketAllocation,
    OrderBookLevel,
    ParityLegDepth,
    ParityTranche,
    PortfolioAllocationResult,
    SingleMarketSweepResult,
)


# ==============================================================================
# 1. OrderBookLevel & ParityLegDepth Input Parsing
# ==============================================================================
def test_orderbook_level_validation():
    # Valid level
    lvl = OrderBookLevel(price=0.55, size=500.0)
    assert lvl.price == 0.55
    assert lvl.size == 500.0

    # Invalid price (< 0 or > 1)
    with pytest.raises(ValueError, match="Price out of range"):
        OrderBookLevel(price=-0.01, size=100.0)

    with pytest.raises(ValueError, match="Price out of range"):
        OrderBookLevel(price=1.05, size=100.0)

    # Invalid negative size
    with pytest.raises(ValueError, match="Size cannot be negative"):
        OrderBookLevel(price=0.50, size=-10.0)


def test_parity_leg_depth_sorting_and_consolidation():
    # Bids must be sorted descending by price, with duplicates consolidated
    raw_bids = [
        {"price": 0.52, "size": 100.0},
        {"price": 0.54, "size": 200.0},
        {"price": 0.52, "size": 50.0},  # duplicate price 0.52 -> should sum to 150.0
        {"price": 0.49, "size": 300.0},
    ]
    leg = ParityLegDepth.from_raw("tok_yes", "cond_1", "BID", raw_bids, title="YES")
    sorted_lvls = leg.sorted_levels()

    assert len(sorted_lvls) == 3
    assert sorted_lvls[0].price == 0.54
    assert sorted_lvls[0].size == 200.0
    assert sorted_lvls[1].price == 0.52
    assert sorted_lvls[1].size == 150.0
    assert sorted_lvls[2].price == 0.49
    assert sorted_lvls[2].size == 300.0

    # Asks must be sorted ascending by price
    raw_asks = [
        (0.48, 100.0),
        (0.45, 200.0),
        (0.51, 150.0),
    ]
    leg_ask = ParityLegDepth.from_raw("tok_no", "cond_1", "ASK", raw_asks, title="NO")
    sorted_asks = leg_ask.sorted_levels()

    assert len(sorted_asks) == 3
    assert sorted_asks[0].price == 0.45
    assert sorted_asks[0].size == 200.0
    assert sorted_asks[1].price == 0.48
    assert sorted_asks[1].size == 100.0
    assert sorted_asks[2].price == 0.51
    assert sorted_asks[2].size == 150.0


# ==============================================================================
# 2. Strict Concavity & Monotonically Decreasing Marginal Profit
# ==============================================================================
def test_monotonic_decreasing_marginal_profit_and_concavity():
    """Verify that as depth S increases:
    1. Marginal profit dPi/dS is strictly non-increasing across all tranches.
    2. Gross net proceeds R_net(S) is strictly concave.
    """
    allocator = MarginalCtfAllocator(vip_tier="bronze", hurdle_rate=0.0)

    # Leg 0 bids: 100 @ 0.54, 200 @ 0.52, 300 @ 0.49
    leg0 = ParityLegDepth.from_raw(
        "tok_0", "cond_1", "BID",
        [(0.54, 100.0), (0.52, 200.0), (0.49, 300.0)],
        title="YES",
    )
    # Leg 1 bids: 150 @ 0.51, 250 @ 0.49, 200 @ 0.47
    leg1 = ParityLegDepth.from_raw(
        "tok_1", "cond_1", "BID",
        [(0.51, 150.0), (0.49, 250.0), (0.47, 200.0)],
        title="NO",
    )

    res = allocator.compute_sweep_curve(
        market_id="mkt_1",
        condition_id="cond_1",
        legs=[leg0, leg1],
        direction=ArbitrageDirection.MINT_SPLIT,
        execution_mode=ExecutionMode.ALL_TAKER,
    )

    assert len(res.tranches) >= 4

    # Check monotonicity of marginal return
    for i in range(len(res.tranches) - 1):
        curr_m = res.tranches[i].marginal_return_per_dollar
        next_m = res.tranches[i + 1].marginal_return_per_dollar
        assert curr_m >= next_m, f"Tranche {i} margin ({curr_m}) < Tranche {i+1} margin ({next_m})"

    # Check concavity of profit function: second difference <= 0
    profits = [t.cumulative_net_profit_before_gas for t in res.tranches]
    shares = [t.cumulative_shares for t in res.tranches]
    for i in range(1, len(profits) - 1):
        slope_left = (profits[i] - profits[i-1]) / (shares[i] - shares[i-1])
        slope_right = (profits[i+1] - profits[i]) / (shares[i+1] - shares[i])
        assert slope_left >= slope_right - 1e-6, "Profit function is not concave!"


# ==============================================================================
# 3. Exact Optimal Batch Size C* and Proof of Negative Marginal Profit
# ==============================================================================
def test_exact_optimal_batch_size_and_oversweep_penalty():
    """Construct orderbooks where:
    - Tranche 1 (0-100 shs): sum(bids) = 0.55 + 0.51 = 1.06 (Strong positive edge)
    - Tranche 2 (100-200 shs): sum(bids) = 0.52 + 0.50 = 1.02 (Moderate positive edge)
    - Tranche 3 (200-400 shs): sum(bids) = 0.48 + 0.47 = 0.95 (Negative edge!)

    Assert:
    1. Optimal shares S* is exactly 200.0 shares.
    2. Beyond 200 shares, marginal profit is negative (dPi/dS < 0).
    3. Total net profit Pi(250) is strictly LESS than Pi(200)!
    """
    allocator = MarginalCtfAllocator(vip_tier="none", hurdle_rate=0.0)

    leg0 = ParityLegDepth.from_raw(
        "tok_0", "cond_1", "BID",
        [(0.55, 100.0), (0.52, 100.0), (0.48, 200.0)],
        title="YES",
    )
    leg1 = ParityLegDepth.from_raw(
        "tok_1", "cond_1", "BID",
        [(0.51, 100.0), (0.50, 100.0), (0.47, 200.0)],
        title="NO",
    )

    res = allocator.compute_sweep_curve(
        market_id="mkt_test",
        condition_id="cond_1",
        legs=[leg0, leg1],
        direction=ArbitrageDirection.MINT_SPLIT,
        execution_mode=ExecutionMode.ALL_TAKER,
    )

    assert res.is_viable is True
    # Exactly 200 shares should be optimal!
    assert res.optimal_shares == 200.0
    assert res.optimal_capital == 200.0

    # Net profit at S*
    peak_profit = res.optimal_net_profit
    assert peak_profit > 0.0

    # Evaluate at S = 200 shares
    c_200, pnl_200, margin_200 = res.evaluate_at_shares(200.0)
    assert pnl_200 == peak_profit

    # Evaluate at S = 300 shares (oversweep by 100 shares into negative edge)
    c_300, pnl_300, margin_300 = res.evaluate_at_shares(300.0)
    assert margin_300 < 0.0, "Marginal profit beyond S* must be negative!"
    assert pnl_300 < pnl_200, f"Oversweeping must reduce profit! pnl_300={pnl_300} >= pnl_200={pnl_200}"

    # Specifically, evaluate profit at 250 shares
    c_250, pnl_250, margin_250 = res.evaluate_at_shares(250.0)
    assert pnl_250 < pnl_200, "Even 50 shares of oversweep must reduce profit!"


# ==============================================================================
# 4. Fixed Gas Friction Amortization & Breakeven Calculation
# ==============================================================================
def test_gas_amortization_and_rejection_of_sub_breakeven():
    """Verify that if orderbook depth cannot cover Polygon PoS gas ($0.00136),
    the market is marked non-viable (S* = 0).
    """
    allocator = MarginalCtfAllocator(vip_tier="bronze", min_net_edge_usd=0.05)

    # Edge is tiny: sum bids = 0.5005 + 0.5005 = 1.0010 (10 bps edge)
    # Available size: only 5 shares
    # Gross edge before gas = 5 * (1.001 - 1.000) = $0.005
    # Taker fees = ~0.005 * 0.02 * 0.25 * 5 = ~$0.0025
    # Gas cost = $0.00136
    # Net edge is < min_net_edge_usd ($0.05)
    leg0 = ParityLegDepth.from_raw("t0", "c0", "BID", [(0.5005, 5.0)])
    leg1 = ParityLegDepth.from_raw("t1", "c0", "BID", [(0.5005, 5.0)])

    res = allocator.compute_sweep_curve(
        market_id="mkt_tiny",
        condition_id="c0",
        legs=[leg0, leg1],
        direction=ArbitrageDirection.MINT_SPLIT,
    )
    assert res.is_viable is False

    # Now provide sufficient depth so that edge covers gas and clears $0.05 threshold
    leg0_large = ParityLegDepth.from_raw("t0", "c0", "BID", [(0.51, 500.0)])
    leg1_large = ParityLegDepth.from_raw("t1", "c0", "BID", [(0.51, 500.0)])

    res_large = allocator.compute_sweep_curve(
        market_id="mkt_large",
        condition_id="c0",
        legs=[leg0_large, leg1_large],
        direction=ArbitrageDirection.MINT_SPLIT,
    )
    assert res_large.is_viable is True
    assert res_large.optimal_net_profit > 1.00
    assert res_large.breakeven_shares > 0.0
    assert res_large.breakeven_shares < res_large.optimal_shares


# ==============================================================================
# 5. VIP Rebate Tiers Impact on S* and Net Profit
# ==============================================================================
def test_vip_tier_impact_on_depth_sweep():
    """Verify that higher VIP tiers (lower net taker fees) increase realized profit
    and allow deeper sweeps into the book.
    """
    legs = [
        ParityLegDepth.from_raw("t0", "c", "BID", [(0.510, 100.0), (0.502, 200.0)]),
        ParityLegDepth.from_raw("t1", "c", "BID", [(0.505, 100.0), (0.501, 200.0)]),
    ]

    alloc_none = MarginalCtfAllocator(vip_tier="none")
    alloc_obs = MarginalCtfAllocator(vip_tier="obsidian")  # 50% rebate

    res_none = alloc_none.compute_sweep_curve("m", "c", legs)
    res_obs = alloc_obs.compute_sweep_curve("m", "c", legs)

    assert res_obs.optimal_net_profit > res_none.optimal_net_profit
    assert res_obs.optimal_net_fees < res_none.optimal_net_fees
    assert res_obs.optimal_shares >= res_none.optimal_shares


# ==============================================================================
# 6. CTF Merge Arbitrage (Buying asks under par)
# ==============================================================================
def test_ctf_merge_unwind_sweep():
    """Verify CTF Merge Arbitrage depth sweeping:
    - Buying YES @ 0.48 (size 100) and NO @ 0.49 (size 150) -> sum = 0.97 (300 bps discount to $1.00)
    - Next level: YES @ 0.51 and NO @ 0.50 -> sum = 1.01 (over par -> negative edge!)
    """
    allocator = MarginalCtfAllocator(vip_tier="bronze", hurdle_rate=0.0)

    leg0_ask = ParityLegDepth.from_raw("t0", "c", "ASK", [(0.48, 100.0), (0.51, 200.0)], title="YES")
    leg1_ask = ParityLegDepth.from_raw("t1", "c", "ASK", [(0.49, 150.0), (0.50, 200.0)], title="NO")

    res = allocator.compute_sweep_curve(
        market_id="mkt_merge",
        condition_id="c",
        legs=[leg0_ask, leg1_ask],
        direction=ArbitrageDirection.MERGE_UNWIND,
    )

    assert res.is_viable is True
    # Optimal depth should stop after level 1 (100 shares, constrained by leg0 depth 100)
    assert res.optimal_shares == 100.0
    assert res.optimal_net_profit > 0.0

    # Ensure oversweeping to 150 shares produces lower profit because ask sum exceeds 1.00
    _, pnl_100, _ = res.evaluate_at_shares(100.0)
    _, pnl_150, margin_150 = res.evaluate_at_shares(150.0)
    assert margin_150 < 0.0
    assert pnl_150 < pnl_100


# ==============================================================================
# 7. Multi-Market Portfolio Water-Filling Equalizing Marginal Utility
# ==============================================================================
def test_multi_market_equal_marginal_water_filling():
    """Construct 2 dislocated markets:
    - Market A: high initial marginal edge (+3.0%), but shallow depth (50 shares).
                Then drops to +1.0% for 100 shares.
    - Market B: moderate initial marginal edge (+2.0%), but deep (300 shares).

    Budget = $150.
    Optimal allocation must:
    1. Fully fund Market A tranche 1 ($50 @ +3.0%).
    2. Then fund Market B ($100 @ +2.0%), because 2.0% > Market A's tranche 2 (1.0%).
    3. Market A tranche 2 (1.0%) receives $0 because budget is exhausted on higher-marginal tranches!
    """
    allocator = MarginalCtfAllocator(vip_tier="none", hurdle_rate=0.0)

    # Market A: 50 shs @ 0.54 + 0.50 = 1.04 (~3% net), then 100 shs @ 0.51 + 0.505 = 1.015 (~1% net)
    leg_a0 = ParityLegDepth.from_raw("ta0", "ca", "BID", [(0.54, 50.0), (0.51, 100.0)])
    leg_a1 = ParityLegDepth.from_raw("ta1", "ca", "BID", [(0.50, 50.0), (0.505, 100.0)])
    sweep_a = allocator.compute_sweep_curve("MKT_A", "ca", [leg_a0, leg_a1])

    # Market B: 300 shs @ 0.52 + 0.51 = 1.03 (~2% net)
    leg_b0 = ParityLegDepth.from_raw("tb0", "cb", "BID", [(0.52, 300.0)])
    leg_b1 = ParityLegDepth.from_raw("tb1", "cb", "BID", [(0.51, 300.0)])
    sweep_b = allocator.compute_sweep_curve("MKT_B", "cb", [leg_b0, leg_b1])

    assert sweep_a.is_viable and sweep_b.is_viable

    # Run portfolio allocation with $150 budget
    port_res = allocator.allocate_portfolio(
        candidates=[sweep_a, sweep_b],
        total_budget=150.0,
    )

    assert port_res.total_allocated_capital == 150.0
    assert "MKT_A" in port_res.allocations
    assert "MKT_B" in port_res.allocations

    alloc_a = port_res.allocations["MKT_A"]
    alloc_b = port_res.allocations["MKT_B"]

    # Market A receives exactly its 50 shares of high-marginal depth ($50)
    assert alloc_a.allocated_shares == 50.0
    assert alloc_a.allocated_capital == 50.0

    # Market B receives the remaining $100 ($100 / 1.00 = 100 shares)
    assert alloc_b.allocated_shares == 100.0
    assert alloc_b.allocated_capital == 100.0

    # Total net profit must exceed allocating all $150 to Market A or all $150 to Market B
    _, pnl_a_only, _ = sweep_a.evaluate_at_capital(150.0)
    _, pnl_b_only, _ = sweep_b.evaluate_at_capital(150.0)
    assert port_res.total_expected_net_profit > pnl_a_only
    assert port_res.total_expected_net_profit > pnl_b_only


# ==============================================================================
# 8. Multi-Market Gas Pruning for Underfunded Opportunities
# ==============================================================================
def test_multi_market_gas_pruning():
    """Verify that if a candidate market is allocated too little capital to clear
    the fixed Polygon gas cost ($0.00136 + min_net_edge_usd), it is pruned and
    capital is recycled to the remaining viable markets.
    """
    allocator = MarginalCtfAllocator(vip_tier="bronze", min_net_edge_usd=0.25)

    # Market A: viable and large
    leg_a0 = ParityLegDepth.from_raw("ta0", "ca", "BID", [(0.53, 500.0)])
    leg_a1 = ParityLegDepth.from_raw("ta1", "ca", "BID", [(0.51, 500.0)])
    sweep_a = allocator.compute_sweep_curve("MKT_LARGE", "ca", [leg_a0, leg_a1])

    # Market B: has edge, but tiny depth (only 5 shares = $5 max capital)
    # Net profit at $5 is ~$0.15, which is < min_net_edge_usd ($0.25)
    leg_b0 = ParityLegDepth.from_raw("tb0", "cb", "BID", [(0.54, 5.0)])
    leg_b1 = ParityLegDepth.from_raw("tb1", "cb", "BID", [(0.51, 5.0)])
    sweep_b = allocator.compute_sweep_curve("MKT_TINY", "cb", [leg_b0, leg_b1])

    # sweep_b alone has optimal_net_profit < 0.25, so it is filtered out
    assert sweep_b.is_viable is False

    port = allocator.allocate_portfolio([sweep_a, sweep_b], total_budget=100.0)
    assert "MKT_TINY" not in port.allocations
    assert "MKT_LARGE" in port.allocations
    assert port.allocations["MKT_LARGE"].allocated_capital == 100.0


# ==============================================================================
# 9. Selective Maker Execution Mode
# ==============================================================================
def test_selective_maker_mode():
    """Verify Selective Maker mode:
    Leg with lower fee is chosen as Taker, while Leg with higher fee is executed as Maker (0% fee).
    """
    allocator = MarginalCtfAllocator(vip_tier="silver")

    # Leg 0 @ 0.50: curve = 0.50 * 0.50 = 0.25 (maximum taker fee rate)
    # Leg 1 @ 0.20: curve = 0.20 * 0.80 = 0.16 (lower taker fee rate)
    leg0 = ParityLegDepth.from_raw("t0", "c", "BID", [(0.50, 100.0)])
    leg1 = ParityLegDepth.from_raw("t1", "c", "BID", [(0.55, 100.0)])

    res_all_taker = allocator.compute_sweep_curve("m", "c", [leg0, leg1], execution_mode=ExecutionMode.ALL_TAKER)
    res_selective = allocator.compute_sweep_curve("m", "c", [leg0, leg1], execution_mode=ExecutionMode.SELECTIVE_MAKER)

    # Selective maker pays fees on only 1 leg -> higher net profit
    assert res_selective.optimal_net_fees < res_all_taker.optimal_net_fees
    assert res_selective.optimal_net_profit > res_all_taker.optimal_net_profit


# ==============================================================================
# 10. Live Paper Soak Daemon Integration Test
# ==============================================================================
def test_live_paper_soak_daemon_integration():
    """Verify that LivePaperSoakDaemon initializes MarginalCtfAllocator and executes sweeps."""
    from scripts.run_live_paper_soak import LivePaperSoakDaemon

    daemon = LivePaperSoakDaemon(capital_per_strategy=1000.0, vip_tier="bronze")
    assert hasattr(daemon, "ctf_allocator")
    assert isinstance(daemon.ctf_allocator, MarginalCtfAllocator)
    assert daemon.ctf_allocator.vip_tier == "bronze"

    # Simulate parity candidate books
    raw_bids_0 = [{"price": 0.54, "size": 100.0}, {"price": 0.52, "size": 100.0}]
    raw_bids_1 = [{"price": 0.51, "size": 100.0}, {"price": 0.50, "size": 100.0}]
    leg0 = ParityLegDepth.from_raw("tok_0", "cond_test", "BID", raw_bids_0, title="YES")
    leg1 = ParityLegDepth.from_raw("tok_1", "cond_test", "BID", raw_bids_1, title="NO")

    res = daemon.ctf_allocator.compute_sweep_curve("m_soak", "cond_test", [leg0, leg1])
    assert res.is_viable is True

    port = daemon.ctf_allocator.allocate_portfolio([res], total_budget=400.0)
    assert port.funded_count == 1
    assert "m_soak" in port.allocations
    assert port.allocations["m_soak"].allocated_shares == 200.0

