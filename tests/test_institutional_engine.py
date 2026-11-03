"""Unit tests for JaneStreetMarketMaker institutional engine."""

import pytest
from polymarketbot.simulator.institutional import (
    DualCashQuotePair,
    JaneStreetMarketMaker,
    MarketRegime,
)


def test_dual_cash_bidding_initialization():
    mm = JaneStreetMarketMaker(
        condition_id="0xabc123",
        initial_cash=150.0,
        quote_size=45.0,
        gamma=0.02,
        min_spread_cents=0.5,
        auto_merge_threshold=25.0,
    )
    assert mm.portfolio.cash == 150.0
    assert mm.total_merges == 0
    assert mm.total_fills == 0


def test_quote_generation_pure_dual_cash():
    mm = JaneStreetMarketMaker(
        condition_id="0xabc123",
        initial_cash=150.0,
        quote_size=45.0,
        min_spread_cents=0.5,
    )
    quotes = mm.generate_institutional_quotes(
        best_bid_yes=0.74,
        best_ask_yes=0.75,
        depth_bid_yes=100.0,
        depth_ask_yes=100.0,
    )
    assert isinstance(quotes, DualCashQuotePair)
    # Target bid YES should be <= 0.749
    assert quotes.bid_price_yes < 0.75
    # Synthetic ask YES is 1.00 - bid_price_no
    assert abs(quotes.synthetic_ask_yes - (1.00 - quotes.bid_price_no)) < 1e-4
    # Both bids committed under total cash
    assert quotes.collateral_committed_usd <= 150.0
    assert quotes.regime == MarketRegime.BALANCED


def test_continuous_reservation_price_skewing():
    mm = JaneStreetMarketMaker(
        condition_id="0xabc123",
        initial_cash=150.0,
        quote_size=45.0,
        gamma=0.05,
    )
    # Neutral reservation price
    r_neutral = mm.compute_continuous_reservation_price(mid_price=0.50, net_inventory=0.0)
    assert r_neutral == 0.50

    # Long YES (q = +30): reservation price should lean DOWN
    r_long_yes = mm.compute_continuous_reservation_price(mid_price=0.50, net_inventory=30.0)
    assert r_long_yes < 0.50

    # Long NO (q = -30): reservation price should lean UP
    r_long_no = mm.compute_continuous_reservation_price(mid_price=0.50, net_inventory=-30.0)
    assert r_long_no > 0.50


def test_maker_fill_and_automated_batch_merge():
    mm = JaneStreetMarketMaker(
        condition_id="0xabc123",
        initial_cash=150.0,
        quote_size=30.0,
        auto_merge_threshold=25.0,
        gas_fee_merge=0.015,
    )
    # 1. Fill on YES: BUY 30 YES @ 0.74
    res_yes = mm.on_maker_fill(token_side="YES", price=0.74, size=30.0)
    assert res_yes["shares_yes"] == 30.0
    assert res_yes["complete_sets"] == 0.0
    assert res_yes["merge_executed"] is False

    # 2. Fill on NO: BUY 30 NO @ 0.25 (which corresponds to synthetic SELL YES @ 0.75)
    res_no = mm.on_maker_fill(token_side="NO", price=0.25, size=30.0)
    # Complete sets formed = 30.0 >= 25.0 threshold -> Auto-merge triggered!
    assert res_no["merge_executed"] is True
    assert mm.total_merges == 1

    # Remaining sets should be 0.0 (all 30 merged back to USDC)
    assert res_no["complete_sets"] == 0.0
    assert res_no["shares_yes"] == 0.0
    assert res_no["shares_no"] == 0.0
    # Net profit: ($1.00 - $0.74 - $0.25) * 30 - $0.015 gas = +$0.01 * 30 - 0.015 = +$0.285
    assert res_no["cash_after"] > 150.0
    assert abs(res_no["cash_after"] - (150.0 + 0.285)) < 1e-3


def test_emergency_circuit_breaker_synthetic_cross():
    mm = JaneStreetMarketMaker(
        condition_id="0xabc123",
        initial_cash=150.0,
        quote_size=40.0,
        q_hard_threshold=50.0,
    )
    # Simulate being long 40 YES
    mm.on_maker_fill(token_side="YES", price=0.74, size=40.0)
    assert mm.portfolio.get_position("0xabc123").net_exposure == 40.0

    # Emergency cross triggered: sweeps NO at best ask (e.g. 0.26)
    merge_res = mm.execute_emergency_synthetic_cross(best_ask_yes=0.76, best_ask_no=0.26)
    assert merge_res is not None
    assert merge_res.complete_sets_merged == 40.0

    # Net exposure is now collapsed to 0
    pos = mm.portfolio.get_position("0xabc123")
    assert pos.net_exposure == 0.0
    assert pos.complete_sets == 0.0


def test_session_end_liquidation_to_cash(tmp_path):
    from polymarketbot.simulator.engine import NativePaperTradingEngine

    db_path = str(tmp_path / "test_liq.db")
    ledger_path = str(tmp_path / "test_liq.json")

    engine = NativePaperTradingEngine(
        condition_id="0xliqtest",
        asset_id_yes="0xliq_YES",
        asset_id_no="0xliq_NO",
        initial_cash=150.0,
        quote_size=45.0,
        db_path=db_path,
        ledger_path=ledger_path,
        institutional_mode=True,
    )

    # Ingest real order book depth
    engine.update_orderbook_snapshot(
        token_side="YES",
        bids=[{"price": 0.76, "size": 100.0}, {"price": 0.75, "size": 100.0}],
        asks=[{"price": 0.77, "size": 100.0}],
    )
    engine.update_orderbook_snapshot(
        token_side="NO",
        bids=[{"price": 0.23, "size": 100.0}],
        asks=[{"price": 0.24, "size": 100.0}],
    )
    engine.evaluate_quotes()
    assert len(engine.active_quotes) > 0

    # Simulate an open position of 45 YES shares bought at $0.76
    engine.portfolio.process_fill(
        condition_id="0xliqtest",
        side="BUY",
        outcome="YES",
        price=0.76,
        size=45.0,
        is_maker=True,
    )
    pos = engine.portfolio.get_position("0xliqtest")
    assert pos.shares_yes == 45.0
    assert engine.portfolio.cash == round(150.0 - 45.0 * 0.76, 4)

    # Call session-end liquidation
    liq_res = engine.liquidate_all_positions()

    # All quotes cancelled
    assert len(engine.active_quotes) == 0

    # All inventory liquidated to 100% cash
    pos_after = engine.portfolio.get_position("0xliqtest")
    assert pos_after.shares_yes == 0.0
    assert pos_after.shares_no == 0.0
    assert pos_after.net_exposure == 0.0
    assert liq_res["final_open_positions_count"] == 0

    # 45 shares sold at best bid 0.76 -> cash restored to 150.0
    assert abs(liq_res["final_cash_settlement"] - 150.0) < 1e-3
    assert engine.portfolio.cash == liq_res["final_cash_settlement"]
    assert liq_res["shares_yes_liquidated"] == 45.0


def test_microstructure_integration_and_deskew_sizing(tmp_path):
    from polymarketbot.simulator.engine import NativePaperTradingEngine

    db_path = str(tmp_path / "test_micro.db")
    ledger_path = str(tmp_path / "test_micro.json")

    engine = NativePaperTradingEngine(
        condition_id="0xmicrotest",
        asset_id_yes="0xmicro_YES",
        asset_id_no="0xmicro_NO",
        initial_cash=200.0,
        quote_size=20.0,
        db_path=db_path,
        ledger_path=ledger_path,
        institutional_mode=True,
    )

    # 1. First snapshot
    engine.update_orderbook_snapshot(
        token_side="YES",
        bids=[{"price": 0.50, "size": 1000.0}],
        asks=[{"price": 0.52, "size": 1000.0}],
    )
    # 2. Second snapshot with mild benign flow (OFI = -100 < 850 threshold)
    engine.update_orderbook_snapshot(
        token_side="YES",
        bids=[{"price": 0.50, "size": 950.0}],
        asks=[{"price": 0.52, "size": 1050.0}],
    )

    # Verify microstructure tracking
    state = engine.microstructure_yes.latest_state
    assert state is not None
    assert state.best_bid == 0.50
    assert state.best_ask == 0.52
    assert state.bid_size == 950.0
    assert state.ask_size == 1050.0
    # Negative OFI from bid dropping (-50) and ask growing (+50)
    assert state.ofi_10_events == -100.0
    assert state.is_toxic is False

    # 3. Test de-skew sizing optimization: Simulate long 45 YES under benign flow
    engine.portfolio.process_fill(
        condition_id="0xmicrotest",
        side="BUY",
        outcome="YES",
        price=0.50,
        size=45.0,
        is_maker=True,
    )
    engine.evaluate_quotes()

    # In Tier 1 Passive Fade, the BUY NO de-skew quote should be sized to exactly 45.0 (net_q)
    assert "BUY_NO" in engine.active_quotes
    quote_no = engine.active_quotes["BUY_NO"]
    assert quote_no["size"] == 45.0

    # 4. Now simulate extreme toxic flow (|OFI| > 850) and verify Tier 2 trigger
    engine.update_orderbook_snapshot(
        token_side="YES",
        bids=[{"price": 0.50, "size": 50.0}],
        asks=[{"price": 0.52, "size": 3000.0}],
    )
    state_toxic = engine.microstructure_yes.latest_state
    assert state_toxic.is_toxic is True
    assert abs(state_toxic.ofi_10_events) > 850.0


def test_dynamic_threshold_initialization_defaults():
    """Verify JaneStreetMarketMaker defaults to dynamic threshold calculators when params are None."""
    from polymarketbot.rewards.deskew import DynamicThresholdCalculator
    from polymarketbot.rewards.microstructure import (
        compute_dynamic_drift_threshold_cents,
        compute_dynamic_ofi_threshold,
    )
    from polymarketbot.trading.ctf import estimate_merge_cost_usd

    mm = JaneStreetMarketMaker(
        condition_id="0xdyn_init",
        initial_cash=200.0,
        quote_size=50.0,
    )

    # 1. Dynamic gas fee
    assert mm._dynamic_gas_fee is True
    assert mm.gas_fee_merge == pytest.approx(estimate_merge_cost_usd(), 1e-6)
    assert mm.portfolio.gas_fee_merge == mm.gas_fee_merge

    # 2. Dynamic threshold calculator instantiated
    assert isinstance(mm.threshold_calculator, DynamicThresholdCalculator)
    assert mm._dynamic_q_hard is True
    assert mm._dynamic_auto_merge is True
    assert mm.q_hard > 0.0
    assert mm.auto_merge_threshold > 0.0

    # 3. Dynamic OFI threshold
    assert mm._dynamic_ofi is True
    expected_ofi = compute_dynamic_ofi_threshold(100.0, 100.0)
    assert mm.ofi_thresh == pytest.approx(expected_ofi, 1e-4)

    # 4. Dynamic drift threshold
    assert mm._dynamic_drift is True
    expected_drift = compute_dynamic_drift_threshold_cents(0.50, 1.0)
    assert mm.drift_thresh_cents == pytest.approx(expected_drift, 1e-4)


def test_dynamic_threshold_updates_on_depth_and_mid_price_changes():
    """Verify thresholds update dynamically when depth or mid-price changes in generate_institutional_quotes."""
    from polymarketbot.rewards.microstructure import (
        compute_dynamic_drift_threshold_cents,
        compute_dynamic_ofi_threshold,
    )

    mm = JaneStreetMarketMaker(
        condition_id="0xdyn_updates",
        initial_cash=200.0,
        quote_size=50.0,
    )

    # Initial quote call with depth 100/100 and mid 0.50
    mm.generate_institutional_quotes(
        best_bid_yes=0.495,
        best_ask_yes=0.505,
        depth_bid_yes=100.0,
        depth_ask_yes=100.0,
    )
    init_ofi = mm.ofi_thresh
    init_drift = mm.drift_thresh_cents
    init_q_hard = mm.q_hard

    # 1. Change order book depth: increase to 2000.0/2000.0
    mm.generate_institutional_quotes(
        best_bid_yes=0.495,
        best_ask_yes=0.505,
        depth_bid_yes=2000.0,
        depth_ask_yes=2000.0,
    )
    expected_deep_ofi = compute_dynamic_ofi_threshold(2000.0, 2000.0)
    assert mm.ofi_thresh == pytest.approx(expected_deep_ofi, 1e-4)
    assert mm.ofi_thresh > init_ofi  # OFI capacity increases with deep book

    # 2. Change mid-price and spread: shift to 0.85/0.88 (mid 0.865, spread 3.0c)
    mm.generate_institutional_quotes(
        best_bid_yes=0.85,
        best_ask_yes=0.88,
        depth_bid_yes=2000.0,
        depth_ask_yes=2000.0,
    )
    expected_new_drift = compute_dynamic_drift_threshold_cents(0.865, 3.0)
    assert mm.drift_thresh_cents == pytest.approx(expected_new_drift, 1e-4)
    assert mm.drift_thresh_cents != init_drift


def test_explicit_threshold_overrides_preserved():
    """Verify that explicitly provided thresholds are preserved and not overwritten dynamically."""
    mm = JaneStreetMarketMaker(
        condition_id="0xexplicit",
        initial_cash=150.0,
        quote_size=40.0,
        auto_merge_threshold=33.0,
        q_hard_threshold=111.0,
        gas_fee_merge=0.025,
        ofi_toxicity_threshold=950.0,
        drift_toxicity_cents=2.2,
    )

    assert mm._dynamic_gas_fee is False
    assert mm._dynamic_q_hard is False
    assert mm._dynamic_auto_merge is False
    assert mm._dynamic_ofi is False
    assert mm._dynamic_drift is False

    assert mm.gas_fee_merge == 0.025
    assert mm.q_hard == 111.0
    assert mm.auto_merge_threshold == 33.0
    assert mm.ofi_thresh == 950.0
    assert mm.drift_thresh_cents == 2.2

    # Call generate_institutional_quotes with varied depth and mid-price
    mm.generate_institutional_quotes(
        best_bid_yes=0.80,
        best_ask_yes=0.85,
        depth_bid_yes=5000.0,
        depth_ask_yes=5000.0,
    )

    # Values must remain unchanged
    assert mm.gas_fee_merge == 0.025
    assert mm.q_hard == 111.0
    assert mm.auto_merge_threshold == 33.0
    assert mm.ofi_thresh == 950.0
    assert mm.drift_thresh_cents == 2.2



