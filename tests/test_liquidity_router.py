"""Unit and integration tests for InterDomainLiquidityRouter.

Tests:
1. Normal borrowing: Domain 2 requests capital when ARR is high, Domain 1 provides unencumbered funds.
2. Reserve rejection: Domain 2 request rejected when ARR is below hurdle or when Domain 1 unencumbered cash is below reserve buffer.
3. Redemption repatriation: Simulates on-chain settlement redemption; verifies principal and profit return to Domain 1 and immediately increase Domain 1 usable cash.
4. Multi-market burst allocation: Multiple simultaneous UMA proposals respect individual and aggregate caps.
5. Telemetry & timeout reporting: Reports allocation ratio, active loans, lifetime capital rotated, and incremental ARR boost.
6. End-to-end integration: Domain2SettlementHarvester borrowing and CTF redemption cycle through router.
"""

from __future__ import annotations

import time
import pytest
from unittest.mock import MagicMock

from polymarketbot.trading.liquidity_router import (
    ActiveSettlementLoan,
    InterDomainLiquidityRouter,
    LiquidityRouterConfig,
    MetaAllocationPlan,
    MetaOptimizerCapitalRouter,
    SimpleCashInventoryManager,
    StrategyCapacityModel,
    StrategyName,
    get_default_strategy_models,
)
from polymarketbot.trading.domain2.types import (
    DiscountOpportunity,
    Domain2MarketState,
    UmaProposalEvent,
    ConditionResolutionEvent,
)
from polymarketbot.trading.domain2.harvester_engine import Domain2SettlementHarvester
from polymarketbot.trading.client import OrderPlacementResult


def _create_opportunity(
    condition_id: str,
    hours_left: float = 24.0,
    bid_price: float = 0.980,
    proposed_price: float = 1.0,
    size_usdc: float = 100.0,
) -> DiscountOpportunity:
    """Helper to construct a valid DiscountOpportunity."""
    disc_bps = (proposed_price - bid_price) * 10000.0
    return DiscountOpportunity(
        condition_id=condition_id,
        winning_asset_id=f"TOKEN_{condition_id[-6:]}",
        losing_asset_id="",
        proposed_price=proposed_price,
        best_bid=bid_price,
        best_ask=bid_price + 0.005,
        discount_bps=disc_bps,
        hours_to_liveness_expiry=hours_left,
        recommended_bid_price=bid_price,
        recommended_size_usdc=size_usdc,
    )


def test_normal_borrowing_high_arr():
    """Test normal borrowing: Domain 2 requests capital when ARR is high, Domain 1 provides unencumbered funds."""
    # Domain 1 has $1,000 cash, 0 locked, 0 inventory -> total equity $1,000
    d1_manager = SimpleCashInventoryManager(initial_cash=1000.0)
    config = LiquidityRouterConfig(
        min_domain1_reserve_pct=0.20,  # $200 reserve buffer
        min_settlement_arr_hurdle=150.0,  # 150% ARR hurdle
        max_single_settlement_alloc_pct=0.15,  # $150 max per market
    )
    router = InterDomainLiquidityRouter(domain1_engine=d1_manager, config=config)

    # Opportunity: $0.980 bid vs $1.00 proposed over 24 hours
    # Margin = 0.02 / 0.98 = 2.0408%
    # ARR = 2.0408% * (8760 / 24) = 744.9% ARR (>> 150% hurdle!)
    opp = _create_opportunity("0xcond_high_arr", hours_left=24.0, bid_price=0.980)

    # Request $100
    approved = router.request_settlement_capital(opp, requested_usd=100.0)

    assert approved == 100.0
    # Domain 1 cash debited from $1000 to $900
    assert d1_manager.get_cash() == 900.0
    # Active loan tracked
    assert "0xcond_high_arr" in router._active_loans
    loan = router._active_loans["0xcond_high_arr"]
    assert loan.allocated_usd == 100.0
    assert loan.expected_arr >= 700.0
    # Total portfolio equity preserved: D1 ($900) + Active Loan ($100) = $1,000
    assert router.get_total_portfolio_equity() == 1000.0


def test_reserve_rejection_arr_below_hurdle():
    """Test reserve rejection: Domain 2 request rejected when ARR is below hurdle (< 150%)."""
    d1_manager = SimpleCashInventoryManager(initial_cash=1000.0)
    config = LiquidityRouterConfig(min_settlement_arr_hurdle=150.0)
    router = InterDomainLiquidityRouter(domain1_engine=d1_manager, config=config)

    # Tiny discount: $0.998 bid vs $1.00 proposed over 72 hours
    # Margin = 0.002 / 0.998 = 0.2004%
    # ARR = 0.2004% * (8760 / 72) = 24.38% (< 150% hurdle)
    opp = _create_opportunity("0xcond_low_arr", hours_left=72.0, bid_price=0.998)

    approved = router.request_settlement_capital(opp, requested_usd=100.0)

    assert approved == 0.0
    # Domain 1 cash remains intact
    assert d1_manager.get_cash() == 1000.0
    assert len(router._active_loans) == 0


def test_reserve_rejection_cash_below_reserve_buffer():
    """Test reserve rejection: Domain 2 request rejected when Domain 1 unencumbered cash is below reserve buffer."""
    # Domain 1 has $1,000 total equity ($200 cash, $800 locked in resting quotes)
    # 20% reserve buffer on $1000 equity is $200
    # Unencumbered cash = max(0, $200 free cash - $200 reserve buffer) = $0.00
    d1_manager = SimpleCashInventoryManager(initial_cash=200.0, locked_cash=800.0)
    config = LiquidityRouterConfig(min_domain1_reserve_pct=0.20)
    router = InterDomainLiquidityRouter(domain1_engine=d1_manager, config=config)

    opp = _create_opportunity("0xcond_no_cash", hours_left=24.0, bid_price=0.980)

    approved = router.request_settlement_capital(opp, requested_usd=100.0)

    assert approved == 0.0
    assert d1_manager.get_cash() == 200.0
    assert len(router._active_loans) == 0


def test_partial_allocation_when_cash_nears_reserve_floor():
    """Test partial allocation when requested amount exceeds available unencumbered cash."""
    # Domain 1 has $1,000 equity ($250 free cash, $750 locked)
    # Reserve buffer is $200 (20% of $1,000)
    # Unencumbered cash = $250 - $200 = $50.00
    d1_manager = SimpleCashInventoryManager(initial_cash=250.0, locked_cash=750.0)
    config = LiquidityRouterConfig(min_domain1_reserve_pct=0.20, max_single_settlement_alloc_pct=0.15)
    router = InterDomainLiquidityRouter(domain1_engine=d1_manager, config=config)

    opp = _create_opportunity("0xcond_partial", hours_left=24.0, bid_price=0.980)

    # Requests $100, but only $50 is unencumbered
    approved = router.request_settlement_capital(opp, requested_usd=100.0)

    assert approved == 50.0
    assert d1_manager.get_cash() == 200.0  # Exactly at the 20% reserve buffer floor!
    assert router._active_loans["0xcond_partial"].allocated_usd == 50.0


def test_redemption_repatriation_principal_and_profit():
    """Test redemption repatriation: Simulates on-chain settlement redemption;

    verifies principal and profit return to Domain 1 and immediately increase Domain 1 usable cash.
    """
    d1_manager = SimpleCashInventoryManager(initial_cash=1000.0)
    router = InterDomainLiquidityRouter(domain1_engine=d1_manager)

    opp = _create_opportunity("0xcond_redeem", hours_left=24.0, bid_price=0.980)
    approved = router.request_settlement_capital(opp, requested_usd=100.0)
    assert approved == 100.0
    assert d1_manager.get_cash() == 900.0

    # Simulate on-chain CTF settlement redemption:
    # Bought at $0.980 ($100 cost basis -> ~102.04 shares)
    # Redeems at par $1.00 ($102.04 payout -> $2.04 profit)
    principal = 100.0
    profit = 2.04
    router.release_settlement_capital("0xcond_redeem", returned_usd=principal, profit_usd=profit)

    # Verified: Domain 1 usable cash is immediately updated to $1,002.04!
    assert round(d1_manager.get_cash(), 2) == 1002.04
    assert round(d1_manager.realized_pnl, 2) == 2.04
    # Loan is cleared from active loans
    assert "0xcond_redeem" not in router._active_loans
    assert len(router._completed_loans) == 1

    # Verify telemetry
    telemetry = router.get_telemetry()
    assert telemetry["active_loans_count"] == 0
    assert telemetry["lifetime_capital_rotated_usd"] == 100.0
    assert telemetry["lifetime_capital_repatriated_usd"] == 100.0
    assert telemetry["lifetime_profit_usd"] == 2.04
    assert telemetry["total_portfolio_equity"] == 1002.04


def test_multi_market_burst_allocation_caps():
    """Test multi-market burst allocation: Multiple simultaneous UMA proposals respect

    individual and aggregate caps.
    """
    # $1,000 portfolio:
    # 20% reserve buffer ($200) -> Max aggregate lending: $800
    # 15% single market cap -> Max per market: $150
    d1_manager = SimpleCashInventoryManager(initial_cash=1000.0)
    config = LiquidityRouterConfig(
        min_domain1_reserve_pct=0.20,
        max_single_settlement_alloc_pct=0.15,
        max_total_settlement_alloc_pct=0.80,
    )
    router = InterDomainLiquidityRouter(domain1_engine=d1_manager, config=config)

    # Market 1 requests $250 -> Capped at $150 (single market cap: 15% of $1,000)
    opp1 = _create_opportunity("0xmarket_1", hours_left=24.0, bid_price=0.980)
    alloc1 = router.request_settlement_capital(opp1, requested_usd=250.0)
    assert alloc1 == 150.0

    # Market 2 requests $150 -> Approved for $150
    opp2 = _create_opportunity("0xmarket_2", hours_left=24.0, bid_price=0.980)
    alloc2 = router.request_settlement_capital(opp2, requested_usd=150.0)
    assert alloc2 == 150.0

    # Market 3 requests $150 -> Approved for $150
    opp3 = _create_opportunity("0xmarket_3", hours_left=24.0, bid_price=0.980)
    alloc3 = router.request_settlement_capital(opp3, requested_usd=150.0)
    assert alloc3 == 150.0

    # Market 4 requests $150 -> Approved for $150
    opp4 = _create_opportunity("0xmarket_4", hours_left=24.0, bid_price=0.980)
    alloc4 = router.request_settlement_capital(opp4, requested_usd=150.0)
    assert alloc4 == 150.0

    # Market 5 requests $150 -> Approved for $150
    opp5 = _create_opportunity("0xmarket_5", hours_left=24.0, bid_price=0.980)
    alloc5 = router.request_settlement_capital(opp5, requested_usd=150.0)
    assert alloc5 == 150.0

    # Total loaned so far = 5 * $150 = $750.
    # D1 free cash remaining = $1000 - $750 = $250.
    # Unencumbered cash remaining above $200 reserve buffer = $50.00.
    assert d1_manager.get_cash() == 250.0

    # Market 6 requests $150 -> Only gets remaining unencumbered $50!
    opp6 = _create_opportunity("0xmarket_6", hours_left=24.0, bid_price=0.980)
    alloc6 = router.request_settlement_capital(opp6, requested_usd=150.0)
    assert alloc6 == 50.0
    assert d1_manager.get_cash() == 200.0  # Hit reserve buffer exactly

    # Market 7 requests $100 -> Rejected (0.0) because unencumbered cash is exhausted!
    opp7 = _create_opportunity("0xmarket_7", hours_left=24.0, bid_price=0.980)
    alloc7 = router.request_settlement_capital(opp7, requested_usd=100.0)
    assert alloc7 == 0.0

    # Telemetry reflects all active loans
    telemetry = router.get_telemetry()
    assert telemetry["active_loans_count"] == 6
    assert telemetry["active_loans_usd"] == 800.0
    assert telemetry["domain1_unencumbered_cash"] == 0.0
    assert telemetry["domain1_reserve_buffer_usd"] == 200.0


def test_telemetry_and_timeout_monitoring():
    """Test telemetry metrics, allocation ratio, and timeout detection."""
    d1_manager = SimpleCashInventoryManager(initial_cash=1000.0)
    config = LiquidityRouterConfig(settlement_repatriation_timeout_hours=72.0)
    router = InterDomainLiquidityRouter(domain1_engine=d1_manager, config=config)

    opp = _create_opportunity("0xtimeout_cond", hours_left=24.0, bid_price=0.980)
    router.request_settlement_capital(opp, requested_usd=100.0)

    # Fresh loan is not timed out
    assert len(router.get_timed_out_loans()) == 0

    # Simulate time elapse past 72 hours
    router._active_loans["0xtimeout_cond"].timestamp = time.time() - (73.0 * 3600.0)

    # Now detected as timed out
    timed_out = router.get_timed_out_loans()
    assert len(timed_out) == 1
    assert timed_out[0].condition_id == "0xtimeout_cond"

    telemetry = router.get_telemetry()
    assert telemetry["timed_out_loans_count"] == 1
    assert telemetry["active_loans"][0]["is_timed_out"] is True
    assert telemetry["incremental_arr_boost_pct"] > 0.0


def test_end_to_end_domain2_harvester_integration():
    """Test full integration between Domain2SettlementHarvester, InterDomainLiquidityRouter,

    and Domain 1 Cash Manager.
    """
    mock_clob = MagicMock()
    mock_clob.place_limit_order.return_value = OrderPlacementResult(
        success=True,
        order_id="ord_123",
        status="OPEN",
        side="BUY",
        price=0.980,
        size=100.0,
    )
    mock_ctf = MagicMock()
    mock_ctf.redeem_positions.return_value = MagicMock(success=True, tx_hash="0xredeem_tx")

    # Domain 1 begins with $2,000 cash
    d1_manager = SimpleCashInventoryManager(initial_cash=2000.0)
    router = InterDomainLiquidityRouter(
        domain1_engine=d1_manager,
        config=LiquidityRouterConfig(
            min_domain1_reserve_pct=0.20,  # $400 reserve buffer
            min_settlement_arr_hurdle=150.0,
            max_single_settlement_alloc_pct=0.15,  # $300 max per market
        ),
    )

    # Domain 2 begins with $0 idle cash (hard-gated $0 to avoid cash drag!)
    harvester = Domain2SettlementHarvester(
        clob_client=mock_clob,
        ctf_client=mock_ctf,
        initial_capital_usdc=0.0,
        dry_run=True,
        router=router,
    )

    assert harvester.available_cash == 0.0

    # UMA price proposal arrives
    cid = "0xuma_market_alpha"
    event = UmaProposalEvent(
        condition_id=cid,
        requester="0xreq",
        proposer="0xprop",
        identifier="TOKEN_ALPHA",
        proposed_price=1.0,
        request_timestamp=int(time.time()),
        expiration_timestamp=int(time.time()) + 7200,  # 2 hours left
        ancillary_data="hex",
        block_number=1000,
        tx_hash="0xuma_tx",
    )

    # Harvester processes proposal:
    # Available cash was $0, so it requests unencumbered capital from router.
    # Router approves loan up to single market cap ($300).
    # Harvester places passive limit bid!
    harvester.handle_proposal(event)

    assert cid in router._active_loans
    loan_amount = router._active_loans[cid].allocated_usd
    assert loan_amount > 0.0
    # Domain 1 cash decreased by loan amount
    assert d1_manager.get_cash() == 2000.0 - loan_amount
    assert harvester._market_states[cid] == Domain2MarketState.QUOTING

    # Simulate trade fill: 100 shares filled at $0.980 ($98 cost)
    pos = harvester.record_fill(
        condition_id=cid,
        asset_id="TOKEN_ALPHA",
        shares=100.0,
        price=0.980,
        liveness_expiry_ts=int(time.time()) + 1,
    )
    assert pos.cost_basis_usdc == 98.0

    # Simulate on-chain ConditionResolution event
    res_event = ConditionResolutionEvent(
        condition_id=cid,
        oracle="0xoracle",
        question_id=1,
        outcome_slot_count=2,
        payout_numerators=[1, 0],
        winning_outcome_index=0,
        is_void=False,
        block_number=1050,
        tx_hash="0xres_tx",
    )

    # Mock settlement worker to return redemption success
    harvester.settlement_worker.check_and_settle_ready_positions = MagicMock(
        return_value=[MagicMock(success=True, condition_id=cid, tx_hash="0xsettled")]
    )

    harvester.handle_resolution(res_event)

    # Verified:
    # 1. Domain 2 market state transitioned to SETTLED
    assert harvester._market_states[cid] == Domain2MarketState.SETTLED
    # 2. Capital was repatriated to Domain 1!
    # Proceeds = $100 (100 shares * $1.00), Cost basis = $98, Profit = $2.00
    # Total loan of $300 is fully repatriated ($98 filled principal + $202 unspent + $2 profit = $302)!
    # Domain 1 cash becomes initial $2000 + $2 profit = $2002!
    # Domain 2 available cash returns to $0 (zero cash drag)!
    assert d1_manager.get_cash() == 2002.0
    assert d1_manager.realized_pnl == 2.0
    assert harvester.available_cash == 0.0
    # 3. Router cleared active loan
    assert cid not in router._active_loans
    assert len(router._completed_loans) == 1


def test_real_money_trading_engine_integration(tmp_path):
    """Test RealMoneyTradingEngine integration with InterDomainLiquidityRouter."""
    from polymarketbot.trading.client import LiveClobClient
    from polymarketbot.trading.ctf import PolygonCtfClient
    from polymarketbot.trading.engine import RealMoneyTradingEngine
    from polymarketbot.trading.liquidity_router import CashInventoryManager

    clob = LiveClobClient(dry_run=True)
    ctf = PolygonCtfClient(dry_run=True)
    router = InterDomainLiquidityRouter()

    engine = RealMoneyTradingEngine(
        condition_id="0x" + "a" * 64,
        asset_id_yes="TOKEN_YES_TEST",
        asset_id_no="TOKEN_NO_TEST",
        clob_client=clob,
        ctf_client=ctf,
        initial_cash=1000.0,
        db_path=tmp_path / "engine.db",
        ledger_path=tmp_path / "ledger.json",
        dry_run=True,
        router=router,
    )

    try:
        # Check bidirectional wiring
        assert engine.router is router
        assert router.domain1_engine is engine
        assert isinstance(engine, CashInventoryManager)

        # Check unencumbered cash calculation (20% reserve on $1000 equity = $200 reserve buffer)
        unencumbered = engine.get_unencumbered_cash(min_reserve_pct=0.20)
        assert unencumbered == 800.0

        # Borrow $120 for Domain 2
        opp = _create_opportunity("0xreal_engine_market", hours_left=24.0, bid_price=0.980)
        approved = router.request_settlement_capital(opp, requested_usd=120.0)
        assert approved == 120.0

        # Engine cash reduced to $880
        assert engine.cash == 880.0
        assert engine.risk_manager.portfolio_state.current_cash_usd == 880.0

        # Repatriate $120 principal + $4.80 profit
        router.release_settlement_capital("0xreal_engine_market", returned_usd=120.0, profit_usd=4.80)

        assert engine.cash == 1004.80
        assert engine.realized_pnl == 4.80
        assert engine.risk_manager.portfolio_state.current_cash_usd == 1004.80
    finally:
        engine._conn.close()


def test_arr_calculation_edge_cases():
    """Test compute_opportunity_arr edge cases and explicit expected_arr parsing."""
    router = InterDomainLiquidityRouter()

    # Normal 2.0% discount over 24 hours -> ~745% ARR
    opp = _create_opportunity("0xnormal", hours_left=24.0, bid_price=0.980, proposed_price=1.0)
    arr = router.compute_opportunity_arr(opp)
    assert 740.0 < arr < 750.0

    # Explicit expected_arr as decimal ratio (e.g. 1.85 -> 185.0%)
    opp_dec = _create_opportunity("0xdec", hours_left=24.0)
    opp_dec.expected_arr = 1.85
    assert router.compute_opportunity_arr(opp_dec) == 185.0

    # Explicit expected_arr as percent (e.g. 240.0 -> 240.0%)
    opp_pct = _create_opportunity("0xpct", hours_left=24.0)
    opp_pct.expected_arr = 240.0
    assert router.compute_opportunity_arr(opp_pct) == 240.0

    # Zero hours left -> safe fallback without ZeroDivisionError
    opp_zero_hr = _create_opportunity("0xzero_hr", hours_left=0.0)
    assert router.compute_opportunity_arr(opp_zero_hr) >= 0.0

    # Inverted price (bid >= proposed) -> 0.0% ARR
    opp_inv = _create_opportunity("0xinv", hours_left=24.0, bid_price=1.005, proposed_price=1.0)
    assert router.compute_opportunity_arr(opp_inv) == 0.0


def test_strategy_capacity_model_decay():
    """Test StrategyCapacityModel diminishing returns and capacity limits."""
    model = StrategyCapacityModel(
        name="TestStrat",
        base_marginal_apy=1000.0,
        capacity_scale_usd=50_000.0,
        decay_exponent=1.0,
        max_capacity_usd=200_000.0,
        is_active=True,
    )

    # At $0 capital: marginal APY equals base APY
    assert model.marginal_apy(0.0) == 1000.0

    # At K ($50k): marginal APY decays to exactly half (500%)
    assert abs(model.marginal_apy(50_000.0) - 500.0) < 1e-4

    # At $150k: decay = 1 + 3 = 4 -> marginal APY = 250%
    assert abs(model.marginal_apy(150_000.0) - 250.0) < 1e-4

    # At max capacity ($200k): marginal APY drops to 0.0
    assert model.marginal_apy(200_000.0) == 0.0
    assert model.marginal_apy(250_000.0) == 0.0

    # When deactivated/benched: marginal APY drops to 0.0
    model.is_active = False
    assert model.marginal_apy(0.0) == 0.0
    assert model.effective_apy(10_000.0) == 0.0


def test_meta_optimizer_greedy_equimarginal_allocation():
    """Test greedy equi-marginal allocation across small to mid capital tiers."""
    optimizer = MetaOptimizerCapitalRouter()

    # Tier 1: $1,000 deployment -> 100% to #1 (RewardHarvester)
    plan_1k = optimizer.optimize(1_000.0)
    assert plan_1k.allocations["RewardHarvester"].allocated_usd == 1000.0
    assert plan_1k.allocations["RewardHarvester"].allocation_pct == 100.0
    assert plan_1k.allocations["SettlementHarvester"].allocated_usd == 0.0
    assert plan_1k.allocations["CtfSplitter"].allocated_usd == 0.0
    assert plan_1k.allocations["LatencySniper"].allocated_usd == 0.0
    assert plan_1k.blended_effective_apy > 15_000.0

    # Tier 2: $100,000 deployment -> RewardHarvester marginal yield still beats #2
    plan_100k = optimizer.optimize(100_000.0)
    assert plan_100k.allocations["RewardHarvester"].allocated_usd == 100_000.0
    assert plan_100k.allocations["RewardHarvester"].allocation_pct == 100.0
    assert plan_100k.allocations["SettlementHarvester"].allocated_usd == 0.0

    # Tier 3: $1,000,000 deployment -> Equi-marginal condition reached!
    # RewardHarvester decays until its marginal APY matches SettlementHarvester (~1,041%)
    plan_1m = optimizer.optimize(1_000_000.0)
    rh_slice = plan_1m.allocations["RewardHarvester"]
    sh_slice = plan_1m.allocations["SettlementHarvester"]
    cs_slice = plan_1m.allocations["CtfSplitter"]

    # Both RH and SH receive substantial capital
    assert 400_000.0 < rh_slice.allocated_usd < 550_000.0
    assert 450_000.0 < sh_slice.allocated_usd < 600_000.0
    assert cs_slice.allocated_usd == 0.0  # CS base APY (581%) < 1,041%, receives $0

    # The marginal APYs of RH and SH must be virtually identical (within 2% tolerance)
    assert abs(rh_slice.marginal_apy - sh_slice.marginal_apy) < 5.0
    assert 1030.0 < rh_slice.marginal_apy < 1055.0


def test_meta_optimizer_macro_multi_million_allocation():
    """Test $10,000,000 macro capital allocation across all strategies."""
    optimizer = MetaOptimizerCapitalRouter()
    plan_10m = optimizer.optimize(10_000_000.0)

    rh_slice = plan_10m.allocations["RewardHarvester"]
    sh_slice = plan_10m.allocations["SettlementHarvester"]
    cs_slice = plan_10m.allocations["CtfSplitter"]
    ls_slice = plan_10m.allocations["LatencySniper"]

    # LatencySniper is benched -> receives exactly $0
    assert ls_slice.allocated_usd == 0.0

    # SettlementHarvester hits its hard capacity ceiling of $6,000,000
    assert sh_slice.allocated_usd == 6_000_000.0
    assert sh_slice.allocation_pct == 60.0
    assert sh_slice.marginal_apy == 0.0

    # Remaining $4,000,000 is distributed between RH and CS along the equi-marginal frontier
    assert 1_000_000.0 < rh_slice.allocated_usd < 1_400_000.0
    assert 2_600_000.0 < cs_slice.allocated_usd < 3_000_000.0

    # RH and CS marginal APY must be equilibrated (within 2%)
    assert abs(rh_slice.marginal_apy - cs_slice.marginal_apy) < 5.0
    assert 375.0 < rh_slice.marginal_apy < 385.0

    # Total deployed matches bankroll
    total_deployed = sum(s.allocated_usd for s in plan_10m.allocations.values())
    assert abs(total_deployed - 10_000_000.0) < 1.0

    # Total profit exceeds $50M/year
    assert plan_10m.total_annual_profit_usd > 50_000_000.0


def test_meta_optimizer_optimality_vs_naive_equal_split():
    """Prove mathematically that the meta-optimizer achieves strictly higher profit than naive equal splitting."""
    optimizer = MetaOptimizerCapitalRouter()
    total_cap = 1_000_000.0

    # 1. Optimal equi-marginal plan
    optimal_plan = optimizer.optimize(total_cap)

    # 2. Naive equal 3-way split: $333,333.33 each
    models = {m.name: m for m in get_default_strategy_models() if m.is_active}
    naive_alloc = total_cap / len(models)
    naive_profit = sum(m.cumulative_annual_profit_usd(naive_alloc) for m in models.values())

    # The optimal allocation must generate strictly higher annual profit
    assert optimal_plan.total_annual_profit_usd > naive_profit
    profit_advantage = optimal_plan.total_annual_profit_usd - naive_profit
    assert profit_advantage > 1_000_000.0  # Over $1M/year higher return from meta-optimization!


def test_meta_optimizer_rebalance_delta():
    """Test capital rebalance delta calculation with threshold gating."""
    optimizer = MetaOptimizerCapitalRouter()

    # Suppose current portfolio has $500k in RH and $500k in SH at $1M total
    current = {
        "RewardHarvester": 500_000.0,
        "SettlementHarvester": 500_000.0,
        "CtfSplitter": 0.0,
    }
    deltas = optimizer.rebalance_delta(current, total_equity=1_000_000.0, min_rebalance_pct=0.01)

    # Optimal target is ~$478.5k RH and ~$521.5k SH
    # Delta for RH: ~$478.5k - $500k = -$21.5k
    # Delta for SH: ~$521.5k - $500k = +$21.5k
    assert -23_000.0 < deltas["RewardHarvester"] < -20_000.0
    assert 20_000.0 < deltas["SettlementHarvester"] < 23_000.0
    assert deltas.get("CtfSplitter", 0.0) == 0.0

    # With higher 5% threshold ($50k), small deltas are suppressed to prevent churn
    suppressed_deltas = optimizer.rebalance_delta(current, total_equity=1_000_000.0, min_rebalance_pct=0.05)
    assert suppressed_deltas["RewardHarvester"] == 0.0
    assert suppressed_deltas["SettlementHarvester"] == 0.0


def test_interdomain_router_meta_integration():
    """Test InterDomainLiquidityRouter integrated with MetaOptimizerCapitalRouter."""
    d1_manager = SimpleCashInventoryManager(initial_cash=5000.0)
    router = InterDomainLiquidityRouter(domain1_engine=d1_manager)

    # Router automatically initializes meta_optimizer
    assert router.meta_optimizer is not None

    # Compute optimal allocation from router
    plan = router.compute_meta_optimal_allocation()
    assert plan.total_capital_usd == 5000.0
    assert plan.allocations["RewardHarvester"].allocated_usd == 5000.0

    # Verify telemetry contains meta_optimal_plan
    telemetry = router.get_telemetry()
    assert "meta_optimal_plan" in telemetry
    plan_dict = telemetry["meta_optimal_plan"]
    assert plan_dict["total_capital_usd"] == 5000.0
    assert "allocations" in plan_dict
    assert "RewardHarvester" in plan_dict["allocations"]


