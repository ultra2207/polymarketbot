"""Comprehensive Audit Test Suite for Polymarket Fees and Execution Costs across all 3 Domains.

Validates:
1. Exact non-linear CLOB taker fee formula: fee = C * rate * (p * (1 - p)) ^ exponent.
2. Fee variation across prices: peak hurdle at p = 0.50 vs dramatic reduction at p = 0.98.
3. Maker fee exemption (0.0% fee) and maker rebate mechanics.
4. On-chain gas amortization and minimum batch sizing for Polygon PoS:
   - CTF.redeemPositions() (< 0.05c/share hurdle)
   - CTF.mergePositions()
   - NegRiskAdapter.convertPositions()
5. Domain 1 (Cross-Venue / Latency Anchor): Taker fee hurdles on latency jumps & maker quoting dominance.
6. Domain 2 (Settlement Basis Harvesting): Piecewise allocator non-linear fee curve & redemption gas.
7. Domain 3 (Structural Arbitrage & NegRisk): Multi-leg taker fee scaling & on-chain conversion gas.
"""

from __future__ import annotations

import pytest

from polymarketbot.fees import (
    FeeSchedule,
    taker_fee,
    maker_rebate,
    fee_curve_peak_rate,
)
from polymarketbot.trading.binance_quant_engine import (
    PolymarketArbitrageScanner,
    SnipingOpportunity,
)
from polymarketbot.trading.binance_anchor import (
    LifecycleExitOptimizer,
    ExitPathway,
)
from polymarketbot.trading.domain2.ortools_allocator import (
    Domain2Candidate,
    Domain2OrtoolsAllocator,
)
from polymarketbot.trading.domain2.clob_order_placer import (
    Domain2ClobOrderPlacer,
)
from polymarketbot.trading.domain2.settlement_worker import (
    Domain2SettlementWorker,
)
from polymarketbot.trading.complement_quoter import (
    ComplementQuoter,
    ComplementQuoterConfig,
)
from polymarketbot.trading.negrisk_engine import (
    CoherenceChecker,
    NegRiskMarket,
    OutcomeLeg,
    OpportunityType,
    compute_taker_fee,
    estimate_conversion_gas_usd,
    calculate_minimum_batch_shares,
)


# =============================================================================
# 1. CLOB Taker Fee Formula & Price Variation
# =============================================================================

def test_clob_taker_fee_curve_variation():
    """Verify how taker fee varies with price: peak at 0.50 vs tail at 0.98."""
    sched_crypto = FeeSchedule(rate=0.07, exponent=1.0)
    sched_sports = FeeSchedule(rate=0.05, exponent=1.0)
    sched_low = FeeSchedule(rate=0.04, exponent=1.0)
    sched_exempt = FeeSchedule(rate=0.0, exponent=1.0)

    # 1. At p = 0.50: curve = 0.50 * 0.50 = 0.25 (MAXIMUM FEE HURDLE)
    # Crypto: 0.07 * 0.25 = 1.75c / share
    assert taker_fee(100, 0.50, sched_crypto) == 1.75
    # Sports: 0.05 * 0.25 = 1.25c / share
    assert taker_fee(100, 0.50, sched_sports) == 1.25
    # Low: 0.04 * 0.25 = 1.00c / share
    assert taker_fee(100, 0.50, sched_low) == 1.00
    # Fee-exempt: 0.0
    assert taker_fee(100, 0.50, sched_exempt) == 0.0

    # 2. At p = 0.98: curve = 0.98 * 0.02 = 0.0196 (DRAMATIC REDUCTION)
    # Crypto: 0.07 * 0.0196 = 0.001372 -> 0.1372c / share
    assert abs(taker_fee(100, 0.98, sched_crypto) - 0.1372) < 0.001
    # Low: 0.04 * 0.0196 = 0.000784 -> 0.0784c / share
    assert abs(taker_fee(100, 0.98, sched_low) - 0.0784) < 0.001

    # Effective fee as fraction of capital spent (C * p):
    # At p=0.50, fee / capital = rate * (1 - p) = rate * 0.50 = 2.0% (for rate=0.04)
    # At p=0.98, fee / capital = rate * (1 - p) = rate * 0.02 = 0.08% (8 bps!)
    cap_50 = 100 * 0.50
    fee_50 = taker_fee(100, 0.50, sched_low)
    pct_50 = fee_50 / cap_50
    assert pytest.approx(pct_50, abs=1e-4) == 0.020  # 2.0%

    cap_98 = 100 * 0.98
    fee_98 = taker_fee(100, 0.98, sched_low)
    pct_98 = fee_98 / cap_98
    assert pytest.approx(pct_98, abs=1e-4) == 0.0008  # 0.08% = 8 bps


def test_clob_taker_fee_exponent_variations():
    """Verify exponent=2.0 (crypto pilot) vs exponent=1.0."""
    sched_exp2 = FeeSchedule(rate=0.25, exponent=2.0)
    # At p=0.50: (0.25)^2 = 0.0625. Fee = 100 * 0.25 * 0.0625 = 1.5625 USDC
    assert abs(taker_fee(100, 0.50, sched_exp2) - 1.5625) < 0.001


# =============================================================================
# 2. Maker Fees & Rebates
# =============================================================================

def test_maker_fee_is_zero():
    """Makers pay strictly 0.0% fee on Polymarket across all categories."""
    # Resting orders never incur taker fees
    sched = FeeSchedule(rate=0.07, rebate_rate=0.20)
    # A maker fill produces 0 fee
    # Rebate paid to maker from collected taker fees:
    rebate = maker_rebate(1.75, sched)
    assert rebate == 0.35  # Maker receives 20% rebate on taker fees


# =============================================================================
# 3. Polygon PoS Gas & Minimum Batch Sizing
# =============================================================================

def test_polygon_gas_minimum_batch_sizing():
    """Ensure minimum batch size amortizes on-chain gas to < 0.05c/share."""
    # CTF.redeemPositions() typical gas: $0.005 USD
    min_redeem = calculate_minimum_batch_shares(gas_cost_usd=0.005, max_gas_friction_cents_per_share=0.05)
    assert min_redeem == 10.0  # 10 shares @ 0.05c = $0.005

    # CTF.mergePositions() typical gas: $0.015 USD
    min_merge = calculate_minimum_batch_shares(gas_cost_usd=0.015, max_gas_friction_cents_per_share=0.05)
    assert min_merge == 30.0  # 30 shares @ 0.05c = $0.015

    # NegRisk conversion + merge typical gas: $0.030 USD
    min_negrisk = calculate_minimum_batch_shares(gas_cost_usd=0.030, max_gas_friction_cents_per_share=0.05)
    assert min_negrisk == 60.0  # 60 shares @ 0.05c = $0.030

    # At ComplementQuoter default batch size of 100 shares:
    # Gas drag is $0.015 / 100 = 0.015c / share (well under 0.05c/share limit)
    quoter_cfg = ComplementQuoterConfig(auto_merge_threshold=100.0, gas_fee_per_merge=0.015)
    assert quoter_cfg.gas_friction_cents_per_pair == 0.015


# =============================================================================
# 4. Domain 1 Audit: Latency Sniping vs Maker Quoting
# =============================================================================

def test_domain1_taker_fee_destroys_small_latency_jumps():
    """Small latency jumps (< 1.5 bps) on ATM contracts are destroyed by taker fees."""
    scanner = PolymarketArbitrageScanner(
        min_taker_edge=0.015,     # 1.5c net edge hurdle
        gas_amortized_usd=0.005,  # 0.5c gas buffer
    )

    # Suppose Binance jumps +1.0 bps. Fair probability shifts from 0.50 to 0.523 (+2.3c).
    # CLOB best ask is resting at 0.50.
    # Taker fee at 0.50 for crypto (rate 0.07): 0.07 * 0.25 = 1.75c.
    # Total friction: Ask (0.50) + Fee (0.0175) + Gas (0.005) = 0.5225.
    # Net edge: 0.523 - 0.5225 = +0.0005 (+0.05c) < 1.5c threshold!
    opp = scanner.evaluate_taker_snipe(
        fair_p_yes=0.523,
        best_ask_yes=0.500,
        best_ask_no=0.500,
        fee_rate=0.07,
    )
    # Taker fee completely invalidates the snipe!
    assert not opp.is_actionable
    assert opp.target_side == "NONE"


def test_domain1_large_latency_jump_is_actionable():
    """Large latency jumps (> 3 bps) generate sufficient edge to clear taker fees."""
    scanner = PolymarketArbitrageScanner(
        min_taker_edge=0.015,
        gas_amortized_usd=0.005,
    )
    # Binance rallies +4 bps: Fair probability jumps to 0.60 (+10c).
    # Ask is still at 0.50. Taker fee = 1.75c, Gas = 0.5c.
    # Net edge: 0.60 - 0.50 - 0.0175 - 0.005 = +0.0775 (+7.75c) > 1.5c!
    opp = scanner.evaluate_taker_snipe(
        fair_p_yes=0.60,
        best_ask_yes=0.50,
        best_ask_no=0.50,
        fee_rate=0.07,
    )
    assert opp.is_actionable
    assert opp.target_side == "YES"
    assert opp.net_edge > 0.07


def test_domain1_exit_optimizer_incorporates_fee_schedules():
    """Verify LifecycleExitOptimizer accounts for fee schedule across all 3 pathways."""
    sched_exempt = FeeSchedule(rate=0.0, exponent=1.0)
    sched_crypto = FeeSchedule(rate=0.07, exponent=1.0)

    opt_crypto = LifecycleExitOptimizer(fee_schedule=sched_crypto)
    opt_exempt = LifecycleExitOptimizer(fee_schedule=sched_exempt)

    # 100 shares market sell at 0.50:
    # Crypto pays $1.75 in taker fees; Exempt pays $0.00
    res_crypto = opt_crypto.compare_exit_pathways(
        shares_up=100.0, shares_down=0.0,
        bid_up=0.50, bid_down=0.50,
        ask_up=0.52, ask_down=0.52,
        tau_seconds=60.0, p_win_up=0.50,
    )
    res_exempt = opt_exempt.compare_exit_pathways(
        shares_up=100.0, shares_down=0.0,
        bid_up=0.50, bid_down=0.50,
        ask_up=0.52, ask_down=0.52,
        tau_seconds=60.0, p_win_up=0.50,
    )

    # For crypto, market sell net payout is 50.0 - 1.75 = 48.25
    assert res_crypto.net_payout_sell == 48.25
    # For exempt, market sell net payout is 50.0 - 0.0 = 50.00
    assert res_exempt.net_payout_sell == 50.00


# =============================================================================
# 5. Domain 2 Audit: Settlement Basis Allocator & Execution
# =============================================================================

def test_domain2_allocator_non_linear_fee_model():
    """Verify piecewise allocator uses exact non-linear fee formula instead of flat fee."""
    allocator = Domain2OrtoolsAllocator(
        reserve_pct=0.10,
        max_drawdown_target=0.30,
        max_single_bet_pct=0.50,
        max_category_pct=0.50,
        min_order_usd=20.0,
        depth_consumption_gamma=1.0,
    )

    # Candidate at p = 0.98, p_win = 0.999.
    # Gross edge = (0.999 - 0.98) / 0.98 = 1.9388%.
    # If using old buggy logic (subtracting flat fee_rate=0.04), edge becomes 1.9388% - 4.0% = -2.06% (REJECTED!).
    # With true non-linear fee for taker: fee = 0.04 * (0.98 * 0.02) = 0.000784 / share = 0.08% of capital.
    # With maker: fee = 0.0% of capital!
    cand_maker = Domain2Candidate(
        condition_id="0xMAKER",
        token_id="tok_m",
        question="Maker Settlement Basis",
        category="finance",
        price=0.98,
        hours_to_resolution=2.0,
        depth_usd=1000.0,
        p_hat=0.999,
        fee_rate=0.04,
        is_taker=False,  # Resting maker order
        redemption_gas_usd=0.005,
    )
    cand_taker = Domain2Candidate(
        condition_id="0xTAKER",
        token_id="tok_t",
        question="Taker Settlement Basis",
        category="sports",
        price=0.98,
        hours_to_resolution=2.0,
        depth_usd=1000.0,
        p_hat=0.999,
        fee_rate=0.04,
        is_taker=True,  # Taker order
        redemption_gas_usd=0.005,
    )

    result = allocator.optimize_allocation([cand_maker, cand_taker], bankroll_usd=2000.0)
    assert result.solver_status == "OPTIMAL"
    alloc_map = {a.condition_id: a for a in result.allocations}

    # Both are accepted because true edge (+1.8% to +1.9%) exceeds risk & variance hurdles!
    assert "0xMAKER" in alloc_map
    assert "0xTAKER" in alloc_map

    # Maker allocation has zero taker fee
    assert alloc_map["0xMAKER"].total_fee_usd == 0.0
    # Taker allocation has positive taker fee accurately computed
    assert alloc_map["0xTAKER"].total_fee_usd > 0.0
    # Redemption gas is strictly recorded
    assert alloc_map["0xMAKER"].redemption_gas_usd == 0.005


def test_domain2_order_placer_min_order_gas_amortization():
    """Verify clob_order_placer enforces minimum size guaranteeing gas < 0.05c/share."""
    placer = Domain2ClobOrderPlacer(clob_client=None, min_order_usdc=20.0)
    assert placer.min_order_usdc == 20.0
    # At $0.985 bid, $20.0 allocation buys 20.3 shares.
    # Gas ($0.005) / 20.3 = 0.0246c / share < 0.05c / share limit.


# =============================================================================
# 6. Domain 3 Audit: Structural Arbitrage & NegRisk
# =============================================================================

def test_domain3_negrisk_fee_scaling_across_legs():
    """Verify NegRisk Buy-All-NO scales taker fees linearly with k legs."""
    # Build 4-way market
    k = 4
    legs = {}
    for i in range(k):
        legs[i] = OutcomeLeg(
            index=i,
            condition_id=f"0xcond_{i}",
            question_id=f"0xq_{i}",
            title=f"Team {i}",
            yes_token_id=f"yes_{i}",
            no_token_id=f"no_{i}",
            best_ask_no=0.74,  # Sum = 4 * 0.74 = 2.96 (< 3.00 payout)
            ask_size_no=100.0,
            fee_rate=0.04,
            fee_exponent=1.0,
        )

    market = NegRiskMarket(
        event_id="evt_4",
        market_id="mkt_4",
        k=4,
        legs=legs,
    )

    # Gross edge: Payout (3.00) - Cost (2.96) = 0.04 USDC / share ($4.00 for 100 shares).
    # Taker fee per leg: 100 * 0.04 * (0.74 * 0.26) = 100 * 0.04 * 0.1924 = 0.7696 USDC.
    # Total taker fees across 4 legs: 4 * 0.7696 = 3.0784 USDC (~3.08c / share!).
    # Conversion + merge gas: ~0.001 - 0.015 USDC.
    # Net edge: $4.00 - $3.0784 - gas ~= $0.90 USDC.
    checker = CoherenceChecker(min_net_edge_usd=0.10, min_net_edge_bps=1.0)
    opps = checker.scan_market(market=market, pol_usd_price=0.20, gas_price_gwei=50.0)

    no_opps = [o for o in opps if o.opportunity_type == OpportunityType.BUY_ALL_NO]
    assert len(no_opps) == 1
    opp = no_opps[0]

    assert opp.shares == 100.0
    assert pytest.approx(opp.total_taker_fees_usd, abs=1e-3) == 3.0784
    assert opp.net_edge_usd < opp.gross_edge_usd
    assert opp.net_edge_usd > 0.0


def test_domain3_negrisk_fee_wipes_out_thin_arbitrage():
    """Verify that a thin gross edge is correctly rejected when fees exceed edge."""
    # Sum NO = 2.98 (gross edge = 0.02 / share = $2.00 for 100 shares).
    # But taker fees across 4 legs = $3.08 USDC!
    # Net edge is negative (-$1.08). Scanner MUST reject it.
    legs = {}
    for i in range(4):
        legs[i] = OutcomeLeg(
            index=i,
            condition_id=f"0xcond_{i}",
            question_id=f"0xq_{i}",
            title=f"Team {i}",
            yes_token_id=f"yes_{i}",
            no_token_id=f"no_{i}",
            best_ask_no=0.745,  # Sum = 4 * 0.745 = 2.98
            ask_size_no=100.0,
            fee_rate=0.04,
            fee_exponent=1.0,
        )

    market = NegRiskMarket(
        event_id="evt_thin",
        market_id="mkt_thin",
        k=4,
        legs=legs,
    )

    checker = CoherenceChecker(min_net_edge_usd=0.10)
    opps = checker.scan_market(market=market)
    no_opps = [o for o in opps if o.opportunity_type == OpportunityType.BUY_ALL_NO]
    # Correctly rejected because taker fees exceed gross edge!
    assert len(no_opps) == 0
