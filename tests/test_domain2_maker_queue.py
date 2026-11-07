"""Tests for Domain 2 Settlement Maker Execution & Queue Drift Engine.

Verifies:
- Option A: Inside spread pennying (first in line, Q_ahead = 0).
- Option B: Top queue joining when spread <= tick or queue clears fast.
- Option C: Dynamic EV-maximizing regime selection.
- Queue drift & adverse non-selection decay mechanics.
- Acute taker sweep fallback when expiry is imminent and edge > fees.
- Full integration with Domain2ClobOrderPlacer order placement and tracking.
"""

import math
import time
import pytest
from unittest.mock import MagicMock

from polymarketbot.trading.domain2.types import (
    MakerExecutionRegime,
    MakerPricingDecision,
    DiscountOpportunity,
)
from polymarketbot.trading.domain2.maker_pricing_engine import (
    SettlementMakerPricingEngine,
    MakerEngineConfig,
)
from polymarketbot.trading.domain2.clob_order_placer import Domain2ClobOrderPlacer
from polymarketbot.trading.client import OrderPlacementResult


def test_option_a_improve_inside_spread():
    """Option A: When spread > tick and standing depth is large, penny inside spread."""
    engine = SettlementMakerPricingEngine(
        config=MakerEngineConfig(
            default_tick_size=0.001,
            queue_clearance_fraction_limit=0.50,
        )
    )

    # Wide spread (2.5 cents), large queue ahead (3,000 shares)
    # Clearance time: 3000 / 148 = 20.27 hours > 0.5 * 2.0h -> Option B rejected
    decision = engine.evaluate_decision(
        best_bid=0.960,
        best_ask=0.985,
        allocated_capital_usdc=500.0,
        hours_to_expiry=2.0,
        standing_depth_ahead=3000.0,
        taker_sell_velocity_shares_hr=148.0,
        tick_size=0.001,
    )

    assert decision.regime == MakerExecutionRegime.MAKER_IMPROVE_INSIDE_SPREAD
    assert decision.quote_price == 0.961  # 0.960 + 0.001 tick
    assert decision.standing_depth_ahead == 0.0  # First in line!
    assert decision.is_taker is False
    assert decision.p_fill >= 0.70  # Calibrated first-priority fill probability
    assert decision.expected_arr > 0.0
    assert "Option A chosen" in decision.reason


def test_option_b_mandatory_on_one_tick_spread():
    """Option B: When spread == tick, improving would cross spread; mandatory queue join."""
    engine = SettlementMakerPricingEngine(
        config=MakerEngineConfig(default_tick_size=0.001)
    )

    # Spread is strictly 1 tick (0.001)
    decision = engine.evaluate_decision(
        best_bid=0.980,
        best_ask=0.981,
        allocated_capital_usdc=500.0,
        hours_to_expiry=2.0,
        standing_depth_ahead=100.0,
        taker_sell_velocity_shares_hr=148.0,
        tick_size=0.001,
    )

    assert decision.regime == MakerExecutionRegime.MAKER_JOIN_TOP_QUEUE
    assert decision.quote_price == 0.980
    assert decision.standing_depth_ahead == 100.0
    assert decision.is_taker is False
    assert decision.t_clear_hours == pytest.approx(100.0 / 148.0, abs=1e-3)
    assert "Spread is 1 tick" in decision.reason


def test_option_c_dynamic_ev_maximizer_picks_option_b():
    """Option C: Wide spread, but small queue clears fast and extra margin gives higher ARR."""
    engine = SettlementMakerPricingEngine(
        config=MakerEngineConfig(
            default_tick_size=0.001,
            queue_clearance_fraction_limit=0.50,
        )
    )

    # Spread is 2.0 cents (0.970 to 0.990), small queue ahead (30 shares)
    # Clearance time: 30 / 150 = 0.2 hours (12 minutes) << 0.5 * 2.0h (60 minutes)
    decision = engine.evaluate_decision(
        best_bid=0.970,
        best_ask=0.990,
        allocated_capital_usdc=500.0,
        hours_to_expiry=2.0,
        standing_depth_ahead=30.0,
        taker_sell_velocity_shares_hr=150.0,
        tick_size=0.001,
    )

    assert decision.regime == MakerExecutionRegime.MAKER_JOIN_TOP_QUEUE
    assert decision.quote_price == 0.970
    assert decision.standing_depth_ahead == 30.0
    assert decision.t_clear_hours == pytest.approx(30.0 / 150.0, abs=1e-3)
    assert decision.is_taker is False
    assert "Option C chosen" in decision.reason


def test_queue_drift_adverse_selection_decay():
    """Drift Risk: When queue clearance time exceeds remaining expiry, fill probability decays to 0."""
    engine = SettlementMakerPricingEngine(
        config=MakerEngineConfig(default_tick_size=0.001)
    )

    # 1. Normal clearance (within window)
    dec_fast = engine.evaluate_decision(
        best_bid=0.980,
        best_ask=0.981,
        allocated_capital_usdc=500.0,
        hours_to_expiry=2.0,
        standing_depth_ahead=150.0,
        taker_sell_velocity_shares_hr=150.0,  # t_clear = 1.0h < 2.0h
    )
    assert dec_fast.p_fill > 0.30

    # 2. Impossible clearance (exceeds window by 2x -> t_clear = 5.0h vs tau = 1.0h)
    dec_slow = engine.evaluate_decision(
        best_bid=0.980,
        best_ask=0.981,
        allocated_capital_usdc=500.0,
        hours_to_expiry=1.0,
        standing_depth_ahead=750.0,
        taker_sell_velocity_shares_hr=150.0,  # t_clear = 5.0h > 1.0h
    )
    # Upward price drift decay factor: 1.0 - (5.0 - 1.0) / 1.0 = -3.0 -> clamped to 0.0
    assert dec_slow.p_fill == 0.0
    assert dec_slow.expected_arr == 0.0


def test_taker_fallback_acute_window():
    """Taker Fallback: When remaining time is acute (<= 15m) and net margin > fees, sweep ask."""
    engine = SettlementMakerPricingEngine(
        config=MakerEngineConfig(
            default_tick_size=0.001,
            acute_resolution_hours=0.25,  # 15 mins
            min_taker_net_margin_bps=30.0,
            taker_fee_param_c=0.0,
        )
    )

    # 1-cent spread, 6 minutes left (0.10h <= 0.25h), ask = 0.985 (150 bps discount)
    decision = engine.evaluate_decision(
        best_bid=0.984,
        best_ask=0.985,
        allocated_capital_usdc=500.0,
        hours_to_expiry=0.10,
        standing_depth_ahead=200.0,
        taker_sell_velocity_shares_hr=100.0,
        tick_size=0.001,
    )

    assert decision.regime == MakerExecutionRegime.TAKER_SWEEP_IMMEDIATE
    assert decision.is_taker is True
    assert decision.quote_price == 0.985
    assert decision.p_fill == 1.0
    assert decision.t_clear_hours == 0.0
    assert "taking ask" in decision.reason


def test_nonlinear_taker_fee_modeling():
    """Verify non-linear taker fee calculation C * 0.04 * P * (1 - P)."""
    engine = SettlementMakerPricingEngine(
        config=MakerEngineConfig(taker_fee_param_c=1.0)
    )
    # At P = 0.90, fee = 1.0 * 0.04 * 0.90 * 0.10 = 0.0036 (36 bps)
    fee_90 = engine.calculate_taker_fee_rate(0.90)
    assert pytest.approx(fee_90, abs=1e-5) == 0.0036

    # At P = 0.50, fee = 1.0 * 0.04 * 0.50 * 0.50 = 0.0100 (100 bps)
    fee_50 = engine.calculate_taker_fee_rate(0.50)
    assert pytest.approx(fee_50, abs=1e-5) == 0.0100


def test_clob_order_placer_integration():
    """Verify Domain2ClobOrderPlacer integrates engine decisions and logs order metadata."""
    mock_clob = MagicMock()
    mock_clob.place_order.return_value = OrderPlacementResult(
        success=True,
        order_id="0xmk_order_123",
        status="PLACED",
        side="BUY",
        price=0.961,
        size=520.29,
        error_message=None,
    )

    placer = Domain2ClobOrderPlacer(
        clob_client=mock_clob,
        tick_size=0.001,
    )

    opp = DiscountOpportunity(
        condition_id="0x1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
        winning_asset_id="tok_gamma_yes",
        losing_asset_id="tok_gamma_no",
        proposed_price=1.0,
        best_bid=0.960,
        best_ask=0.985,
        discount_bps=150.0,
        hours_to_liveness_expiry=2.0,
        recommended_bid_price=0.985,
        recommended_size_usdc=500.0,
        standing_depth_ahead=2500.0,  # Large queue -> should trigger Option A
        taker_sell_velocity_shares_hr=150.0,
        tick_size=0.001,
    )

    res = placer.place_discount_bid(opp, allocated_capital_usdc=500.0)
    assert res is not None
    assert res.success is True
    assert res.order_id == "0xmk_order_123"

    # Verify order details recorded decision correctly
    details = placer._order_details["0xmk_order_123"]
    assert details["regime"] == MakerExecutionRegime.MAKER_IMPROVE_INSIDE_SPREAD.value
    assert details["is_taker"] is False
    assert details["price"] == 0.961
    assert details["p_fill"] > 0.70
    assert details["expected_arr"] > 0.0

    # Verify mock call
    mock_clob.place_order.assert_called_once_with(
        token_id="tok_gamma_yes",
        price=0.961,
        size=520.29,
        side="BUY",
    )


def test_parse_l2_book_depth_and_spread():
    """Verify live L2 order book parsing: best_bid, best_ask, standing depth sum, and spread."""
    from polymarketbot.trading.domain2 import parse_l2_book

    raw_book = {
        "bids": [
            {"price": "0.980", "size": "1500"},
            {"price": "0.980", "size": "500"},   # Second order at top of book
            {"price": "0.975", "size": "3000"},  # Deeper level
        ],
        "asks": [
            {"price": "0.985", "size": "800"},
            {"price": "0.990", "size": "2000"},
        ],
    }

    best_bid, best_ask, q_ahead, spread = parse_l2_book(raw_book, tick_size=0.001)

    assert best_bid == 0.980
    assert best_ask == 0.985
    assert q_ahead == 2000.0  # Exactly 1500 + 500 at best_bid
    assert spread == 0.005


def test_rolling_trade_buffer_and_empirical_floor():
    """Verify RollingTradeBuffer trade velocity tracking and dynamic empirical floor."""
    from polymarketbot.trading.domain2 import RollingTradeBuffer

    buffer = RollingTradeBuffer(window_seconds=3600.0)
    now = 10000.0

    # 1. Zero trades: dynamic empirical floor adapts to queue depth and expiry (no flat 148 static fallback)
    floor_large = buffer.calculate_taker_sell_velocity(
        token_id="tok_x",
        standing_depth_ahead=2000.0,
        hours_to_expiry=2.0,
        current_time=now,
    )
    # Floor: 2000 / (2.0 * 2.0) = 500.0 shares/hr
    assert floor_large == 500.0

    floor_small = buffer.calculate_taker_sell_velocity(
        token_id="tok_y",
        standing_depth_ahead=40.0,
        hours_to_expiry=2.0,
        current_time=now,
    )
    # Floor: 40 / (2.0 * 2.0) = 10.0 shares/hr
    assert floor_small == 10.0

    # 2. Live trades recorded: calculates true trailing sell velocity
    # Record two taker sell trades within last 30 minutes
    buffer.record_trade("tok_active", shares=300.0, price=0.980, side="SELL", timestamp=now - 1200.0)
    buffer.record_trade("tok_active", shares=500.0, price=0.980, side="SELL", timestamp=now - 600.0)
    # Buy trade should not count toward taker sell velocity
    buffer.record_trade("tok_active", shares=1000.0, price=0.985, side="BUY", timestamp=now - 300.0)

    # 800 sell shares over 1200s (0.333h) -> ~2400 shares/hr
    velocity = buffer.calculate_taker_sell_velocity(
        token_id="tok_active",
        standing_depth_ahead=100.0,
        hours_to_expiry=1.0,
        current_time=now,
    )
    assert velocity == pytest.approx(2400.0, abs=10.0)


def test_upward_drift_rate_calculation():
    """Verify live measured upward drift rate dP/dt = (1.00 - P_bid) / tau."""
    from polymarketbot.trading.domain2 import calculate_upward_drift_rate

    # Case 1: Bid 0.980, tau 2.0 hours -> dP/dt = (1.00 - 0.98) / 2.0 = 0.010 USD/share/hr
    drift_1 = calculate_upward_drift_rate(best_bid=0.980, hours_to_expiry=2.0, proposed_price=1.0)
    assert pytest.approx(drift_1, abs=1e-5) == 0.010

    # Case 2: Bid 0.980, tau 0.25 hours (15m acute) -> dP/dt = (1.00 - 0.98) / 0.25 = 0.080 USD/share/hr
    drift_2 = calculate_upward_drift_rate(best_bid=0.980, hours_to_expiry=0.25, proposed_price=1.0)
    assert pytest.approx(drift_2, abs=1e-5) == 0.080

    # Case 3: Deep discount (bid 0.950), tau 1.0 hour -> dP/dt = 0.050 USD/share/hr
    drift_3 = calculate_upward_drift_rate(best_bid=0.950, hours_to_expiry=1.0, proposed_price=1.0)
    assert pytest.approx(drift_3, abs=1e-5) == 0.050


def test_dynamic_regime_switching_thin_book_vs_heavy_wall():
    """Verify dynamic deterministic switching between Option A and Option B driven by book depth."""
    engine = SettlementMakerPricingEngine(
        config=MakerEngineConfig(
            default_tick_size=0.001,
            queue_clearance_fraction_limit=0.50,
        )
    )

    # State 1: Thin book (Q_ahead = 15 shares, v_sell = 150 shares/hr, tau = 1.0h)
    # Clearance time: 15 / 150 = 0.10h (6m) <= 0.50 * 1.0h (30m)
    # Extra margin at 0.975 vs 0.976 yields higher ARR -> Deterministically picks Option B
    dec_thin = engine.evaluate_decision(
        best_bid=0.975,
        best_ask=0.985,
        allocated_capital_usdc=300.0,
        hours_to_expiry=1.0,
        standing_depth_ahead=15.0,
        taker_sell_velocity_shares_hr=150.0,
        tick_size=0.001,
    )
    assert dec_thin.regime == MakerExecutionRegime.MAKER_JOIN_TOP_QUEUE
    assert dec_thin.quote_price == 0.975
    assert dec_thin.standing_depth_ahead == 15.0
    assert dec_thin.t_clear_hours == pytest.approx(0.10, abs=1e-3)
    assert "Option C chosen" in dec_thin.reason

    # State 2: Heavy wall (Q_ahead = 3,000 shares, v_sell = 150 shares/hr, tau = 1.0h)
    # Clearance time: 3000 / 150 = 20.0h > 0.50 * 1.0h
    # Standing in queue would suffer drift abandonment -> Deterministically picks Option A (Pennying)
    dec_wall = engine.evaluate_decision(
        best_bid=0.975,
        best_ask=0.985,
        allocated_capital_usdc=300.0,
        hours_to_expiry=1.0,
        standing_depth_ahead=3000.0,
        taker_sell_velocity_shares_hr=150.0,
        tick_size=0.001,
    )
    assert dec_wall.regime == MakerExecutionRegime.MAKER_IMPROVE_INSIDE_SPREAD
    assert dec_wall.quote_price == 0.976  # 0.975 + 1 tick
    assert dec_wall.standing_depth_ahead == 0.0
    assert "Option A chosen" in dec_wall.reason


def test_dynamic_regime_switching_velocity():
    """Verify dynamic deterministic switching driven by live rolling trade velocity."""
    engine = SettlementMakerPricingEngine(
        config=MakerEngineConfig(
            default_tick_size=0.001,
            queue_clearance_fraction_limit=0.50,
        )
    )

    # Identical queue of 30 shares, tau = 2.0h (clearance threshold = 0.50 * 2.0h = 1.0h)
    # Case A: Low velocity (15 shares/hr) -> t_clear = 2.0h > 1.0h -> Option A (Penny inside spread)
    dec_low_v = engine.evaluate_decision(
        best_bid=0.970,
        best_ask=0.990,
        allocated_capital_usdc=500.0,
        hours_to_expiry=2.0,
        standing_depth_ahead=30.0,
        taker_sell_velocity_shares_hr=15.0,
        tick_size=0.001,
    )
    assert dec_low_v.regime == MakerExecutionRegime.MAKER_IMPROVE_INSIDE_SPREAD
    assert dec_low_v.quote_price == 0.971
    assert "queue clearance slow" in dec_low_v.reason

    # Case B: High velocity (150 shares/hr) -> t_clear = 0.20h <= 1.0h -> Option B (Join top queue)
    dec_high_v = engine.evaluate_decision(
        best_bid=0.970,
        best_ask=0.990,
        allocated_capital_usdc=500.0,
        hours_to_expiry=2.0,
        standing_depth_ahead=30.0,
        taker_sell_velocity_shares_hr=150.0,
        tick_size=0.001,
    )
    assert dec_high_v.regime == MakerExecutionRegime.MAKER_JOIN_TOP_QUEUE
    assert dec_high_v.quote_price == 0.970
    assert "Option C chosen" in dec_high_v.reason


def test_harvester_live_book_extraction_end_to_end():
    """Verify Domain2SettlementHarvester extracts live L2 book depth and dynamically prices orders."""
    from polymarketbot.trading.domain2 import Domain2SettlementHarvester, UmaProposalEvent

    mock_clob = MagicMock()
    # Mock live order book on CLOB
    mock_clob.get_order_book.return_value = {
        "bids": [
            {"price": "0.972", "size": "2500"},  # Wall at best_bid
            {"price": "0.970", "size": "4000"},
        ],
        "asks": [
            {"price": "0.985", "size": "1000"},
        ],
    }
    mock_clob.place_order.return_value = OrderPlacementResult(
        success=True,
        order_id="ord_live_test",
        status="PLACED",
        side="BUY",
        price=0.973,
        size=500.0,
    )

    mock_ctf = MagicMock()

    harvester = Domain2SettlementHarvester(
        clob_client=mock_clob,
        ctf_client=mock_ctf,
        initial_capital_usdc=5000.0,
        dry_run=True,
    )

    # Ingest a live trade to establish real velocity
    harvester.trade_buffer.record_trade(
        token_id="tok_live_uma",
        shares=100.0,
        price=0.972,
        side="SELL",
        timestamp=time.time() - 600,
    )

    now = time.time()
    event = UmaProposalEvent(
        condition_id="0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        requester="0xreq",
        proposer="0xprop",
        identifier="tok_live_uma",
        proposed_price=1.0,
        request_timestamp=int(now - 1800),
        expiration_timestamp=int(now + 7200),  # 2.0 hours left
        ancillary_data="",
        block_number=123,
        tx_hash="0xprop",
    )

    harvester.handle_proposal(event)

    # Verify get_order_book was called for the live token
    mock_clob.get_order_book.assert_called_with("tok_live_uma")

    # Due to heavy wall (2500 shares) and moderate velocity (100 shares / 0.16h ~ 600/hr -> t_clear > 0.50 tau),
    # engine dynamically selects Option A (Pennying to 0.973) rather than sitting behind 2500 shares
    mock_clob.place_order.assert_called_once()
    called_args = mock_clob.place_order.call_args[1]
    assert called_args["token_id"] == "tok_live_uma"
    assert called_args["price"] == 0.973  # 0.972 + 0.001 tick
    assert called_args["side"] == "BUY"

