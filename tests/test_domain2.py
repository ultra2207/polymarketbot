"""Tests for Domain 2 Settlement Basis & Resolution Discount Harvester."""

import pytest
from unittest.mock import MagicMock

from polymarketbot.trading.domain2 import (
    Domain2MarketState,
    UmaProposalEvent,
    UmaDisputeEvent,
    ConditionResolutionEvent,
    DiscountOpportunity,
    Domain2Position,
    Domain2EventListener,
    Domain2CircuitBreaker,
    Domain2ClobOrderPlacer,
    Domain2SettlementWorker,
    Domain2SettlementHarvester,
    UMA_OO_V2,
    CTF_CONTRACT,
    TOPIC_PROPOSE_PRICE,
    TOPIC_DISPUTE_PRICE,
    TOPIC_CONDITION_RESOLUTION,
)
from polymarketbot.trading.client import OrderPlacementResult
from polymarketbot.trading.ctf import RedeemExecutionResult


def test_circuit_breaker_emergency_dispute():
    """Verify that a DisputePrice event immediately halts the market and cancels quotes."""
    cb = Domain2CircuitBreaker()
    cid = "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
    
    assert not cb.is_market_halted(cid)

    dispute_event = UmaDisputeEvent(
        condition_id=cid,
        requester="0x1111111111111111111111111111111111111111",
        proposer="0x2222222222222222222222222222222222222222",
        disputer="0x3333333333333333333333333333333333333333",
        identifier=cid,
        proposed_price=1.0,
        block_number=1234567,
        tx_hash="0xabcdef",
    )

    should_cancel, msg = cb.handle_dispute_event(dispute_event)
    assert should_cancel is True
    assert cb.is_market_halted(cid)

    # Subsequent entry check must be rejected
    opp = DiscountOpportunity(
        condition_id=cid,
        winning_asset_id="tok1",
        losing_asset_id="tok2",
        proposed_price=1.0,
        best_bid=0.980,
        best_ask=0.985,
        discount_bps=150.0,
        hours_to_liveness_expiry=1.5,
        recommended_bid_price=0.985,
        recommended_size_usdc=500.0,
    )
    allowed, alloc, reason = cb.check_entry_allowed(
        opportunity=opp,
        current_equity_usdc=10000.0,
        current_market_cost_usdc=0.0,
        total_open_exposure_usdc=0.0,
        active_markets_count=0,
    )
    assert allowed is False
    assert "halted/disputed" in reason


def test_circuit_breaker_risk_boundaries():
    """Verify market allocation caps, time windows, and price bands."""
    cb = Domain2CircuitBreaker(
        max_market_allocation_pct=0.10,
        min_price=0.975,
        max_price=0.990,
        max_hours_to_expiry=2.0,
    )
    cid = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    # 1. Price too low (< 0.975)
    opp_low_p = DiscountOpportunity(
        condition_id=cid,
        winning_asset_id="tok1",
        losing_asset_id="tok2",
        proposed_price=1.0,
        best_bid=0.920,
        best_ask=0.930,
        discount_bps=700.0,
        hours_to_liveness_expiry=1.0,
        recommended_bid_price=0.930,
        recommended_size_usdc=500.0,
    )
    allowed, alloc, reason = cb.check_entry_allowed(opp_low_p, 10000.0, 0.0, 0.0, 0)
    assert allowed is False
    assert "outside safe discount band" in reason

    # 2. Time too far out (> 2.0h)
    opp_far_t = DiscountOpportunity(
        condition_id=cid,
        winning_asset_id="tok1",
        losing_asset_id="tok2",
        proposed_price=1.0,
        best_bid=0.980,
        best_ask=0.985,
        discount_bps=150.0,
        hours_to_liveness_expiry=5.0,
        recommended_bid_price=0.985,
        recommended_size_usdc=500.0,
    )
    allowed, alloc, reason = cb.check_entry_allowed(opp_far_t, 10000.0, 0.0, 0.0, 0)
    assert allowed is False
    assert "exceeds safe limit" in reason

    # 3. Valid parameters within 10% allocation cap
    opp_valid = DiscountOpportunity(
        condition_id=cid,
        winning_asset_id="tok1",
        losing_asset_id="tok2",
        proposed_price=1.0,
        best_bid=0.980,
        best_ask=0.985,
        discount_bps=150.0,
        hours_to_liveness_expiry=1.5,
        recommended_bid_price=0.985,
        recommended_size_usdc=500.0,
    )
    allowed, alloc, reason = cb.check_entry_allowed(opp_valid, 10000.0, 0.0, 0.0, 0)
    assert allowed is True
    assert alloc == 1000.0  # 10% of 10,000


def test_clob_order_placer():
    """Verify passive limit bid price calculation and cancellation."""
    mock_clob = MagicMock()
    mock_clob.place_order.return_value = OrderPlacementResult(
        success=True,
        order_id="0xorder123",
        status="PLACED",
        side="BUY",
        price=0.985,
        size=1000.0,
        error_message=None,
    )

    placer = Domain2ClobOrderPlacer(clob_client=mock_clob, target_discount_bps=150.0)
    cid = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    # Test passive price logic
    bid_p = placer.calculate_passive_bid_price(best_bid=0.982, best_ask=0.988, proposed_price=1.0)
    assert 0.975 <= bid_p <= 0.990
    assert bid_p >= 0.982

    opp = DiscountOpportunity(
        condition_id=cid,
        winning_asset_id="tok_yes",
        losing_asset_id="tok_no",
        proposed_price=1.0,
        best_bid=0.982,
        best_ask=0.988,
        discount_bps=150.0,
        hours_to_liveness_expiry=1.0,
        recommended_bid_price=0.985,
        recommended_size_usdc=1000.0,
    )

    res = placer.place_discount_bid(opp, allocated_capital_usdc=985.0)
    assert res is not None
    assert res.success is True
    assert res.order_id == "0xorder123"
    mock_clob.place_order.assert_called_once()

    # Test cancellation on market
    cancelled = placer.cancel_market_orders(cid)
    assert cancelled == ["0xorder123"]
    mock_clob.cancel_order.assert_called_once_with("0xorder123")


def test_settlement_worker_redemption():
    """Verify settlement worker only redeems when expiry has passed and resolution confirmed."""
    mock_ctf = MagicMock()
    mock_ctf.redeem_positions.return_value = RedeemExecutionResult(
        success=True,
        tx_hash="0xtxredeem",
        condition_id="0xcccc",
        index_sets=[1, 2],
        gas_fee_usd=0.015,
        gas_units_used=45000,
        error=None,
        is_dry_run=True,
        status="confirmed",
    )

    worker = Domain2SettlementWorker(ctf_client=mock_ctf, dry_run=True)
    cid = "0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

    pos = Domain2Position(
        condition_id=cid,
        asset_id="tok_win",
        shares=1000.0,
        cost_basis_usdc=985.0,
        entry_price=0.985,
        liveness_expiry_ts=1000,
        proposed_price=1.0,
    )
    worker.register_position(pos)

    # 1. Check before expiry and before resolution: should not redeem
    res1 = worker.check_and_settle_ready_positions(now_ts=500)
    assert len(res1) == 0
    mock_ctf.redeem_positions.assert_not_called()

    # 2. Check after expiry: triggers redemption
    res2 = worker.check_and_settle_ready_positions(now_ts=1500)
    assert len(res2) == 1
    assert res2[0].success is True
    assert pos.is_redeemed is True
    assert pos.redeem_tx_hash == "0xtxredeem"
    mock_ctf.redeem_positions.assert_called_once()


def test_harvester_engine_emergency_dispute_flow():
    """Verify end-to-end integration: dispute halts quoting and cancels orders."""
    mock_clob = MagicMock()
    mock_clob.place_order.return_value = OrderPlacementResult(
        success=True,
        order_id="ord_active_1",
        status="PLACED",
        side="BUY",
        price=0.985,
        size=1000.0,
        error_message=None,
    )
    mock_ctf = MagicMock()

    engine = Domain2SettlementHarvester(
        clob_client=mock_clob,
        ctf_client=mock_ctf,
        initial_capital_usdc=10000.0,
        dry_run=True,
    )
    cid = "0xdddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"

    import time
    now = time.time()
    # 1. Propose price -> enters QUOTING
    prop = UmaProposalEvent(
        condition_id=cid,
        requester="0xreq",
        proposer="0xprop",
        identifier="tok_win",
        proposed_price=1.0,
        request_timestamp=int(now - 1800),
        expiration_timestamp=int(now + 3600),
        ancillary_data="",
        block_number=100,
        tx_hash="0xprop_tx",
    )
    engine.handle_proposal(prop)
    assert engine._market_states[cid] == Domain2MarketState.QUOTING
    assert mock_clob.place_order.called

    # 2. Dispute occurs -> cancels order immediately and marks DISPUTED_HALTED
    dispute = UmaDisputeEvent(
        condition_id=cid,
        requester="0xreq",
        proposer="0xprop",
        disputer="0xevil",
        identifier="tok_win",
        proposed_price=1.0,
        block_number=105,
        tx_hash="0xdispute_tx",
    )
    engine.handle_dispute(dispute)
    assert engine._market_states[cid] == Domain2MarketState.DISPUTED_HALTED
    mock_clob.cancel_order.assert_called_once_with("ord_active_1")


def test_ortools_allocator_mathematical_optimization():
    """Verify Google OR-Tools mathematical program optimization, category capping, and hazard filtering."""
    from polymarketbot.trading.domain2 import (
        Domain2OrtoolsAllocator,
        Domain2Candidate,
    )

    allocator = Domain2OrtoolsAllocator(
        reserve_pct=0.25,
        max_drawdown_target=0.10,
        max_single_bet_pct=0.025,
        max_category_pct=0.150,
        min_order_usd=20.0,
    )

    candidates = [
        # 1. High-velocity, safe settlement candidate: edge +1.4%, hazard 0.1% -> MUST ACCEPT
        Domain2Candidate(
            condition_id="0xsafe1",
            token_id="tok1",
            question="Will Team A win Game 3?",
            category="sports",
            price=0.985,
            hours_to_resolution=1.0,
            depth_usd=5000.0,
            p_hat=0.999,
        ),
        # 2. CPI print candidate: edge +1.6%, hazard 0.2% -> MUST ACCEPT
        Domain2Candidate(
            condition_id="0xsafe2",
            token_id="tok2",
            question="US CPI Print?",
            category="economics",
            price=0.982,
            hours_to_resolution=0.8,
            depth_usd=10000.0,
            p_hat=0.998,
        ),
        # 3. High-hazard, long lockup market (e.g. Tokyo temp 10h out, hazard 5.5% > edge 0.5%) -> MUST REJECT
        Domain2Candidate(
            condition_id="0xtoxic_weather",
            token_id="tok3",
            question="Tokyo Temp 18C?",
            category="weather",
            price=0.940,
            hours_to_resolution=10.0,
            depth_usd=3000.0,
            p_hat=0.945,
        ),
        # 4. BTC volatile market (hazard 5.0% > edge) -> MUST REJECT
        Domain2Candidate(
            condition_id="0xtoxic_btc",
            token_id="tok4",
            question="BTC > $78k?",
            category="crypto",
            price=0.970,
            hours_to_resolution=8.0,
            depth_usd=20000.0,
            p_hat=0.950,
        ),
    ]

    res = allocator.optimize_allocation(candidates, bankroll_usd=10000.0)

    assert res.solver_status == "OPTIMAL"
    assert res.active_markets_count == 2  # Only the 2 positive risk-adjusted edge markets!
    allocated_cids = {a.condition_id for a in res.allocations}
    assert "0xsafe1" in allocated_cids
    assert "0xsafe2" in allocated_cids
    assert "0xtoxic_weather" not in allocated_cids  # Algorithm rejected toxic weather reversal!
    assert "0xtoxic_btc" not in allocated_cids      # Algorithm rejected toxic BTC reversal!

    # Sizing constraints check
    for a in res.allocations:
        assert a.allocated_usd <= 250.001  # 2.5% max single bet
        assert a.allocated_usd >= 20.0     # min order size

    assert res.total_allocated_usd <= 7500.0  # Usable capital budget respected


def test_harvester_universe_scan_and_optimal_execution():
    """Verify that harvester scans universe candidates and dispatches optimal portfolio."""
    clob_mock = MagicMock()
    clob_mock.place_order.return_value = OrderPlacementResult(
        success=True,
        order_id="ord_test_123",
        status="PLACED",
        side="BUY",
        price=0.985,
        size=20.0,
    )
    ctf_mock = MagicMock()

    harvester = Domain2SettlementHarvester(
        clob_client=clob_mock,
        ctf_client=ctf_mock,
        initial_capital_usdc=5000.0,
        dry_run=True,
    )

    cands = harvester.scan_universe_candidates(
        manifest_path="data/tape/manifest_current.json",
        max_hours_ahead=72.0,
        min_price=0.90,
    )

    assert len(cands) > 100  # Broad coverage across universe
    # Run optimization on first 25 candidates
    res = harvester.execute_optimal_portfolio(cands[:25])
    assert res.solver_status in ("OPTIMAL", "FEASIBLE")
    assert res.total_allocated_usd > 0.0
    assert clob_mock.place_order.called


