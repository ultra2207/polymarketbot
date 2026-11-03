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

