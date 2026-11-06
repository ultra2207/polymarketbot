"""Unit tests for Polymarket paper trading and liquidity reward harvesting."""

import json
import pytest
from pathlib import Path

from polymarketbot.paper_trading.models import MarketConfig, TokenConfig, VirtualQuote
from polymarketbot.paper_trading.rewards import (
    compute_competing_depth_score,
    compute_epoch_payout,
    compute_quadratic_score,
)
from polymarketbot.paper_trading.engine import PaperTradingEngine


def test_compute_quadratic_score():
    # v = 3.0 cents, s = 0.0 cents, size = 100 -> score = 100.0
    assert compute_quadratic_score(3.0, 0.0, 100.0) == 100.0

    # v = 2.0 cents, s = 1.0 cent, size = 100 -> ((2-1)/2)^2 * 100 = 0.25 * 100 = 25.0
    assert pytest.approx(compute_quadratic_score(2.0, 1.0, 100.0)) == 25.0

    # v = 4.0 cents, s = 1.0 cent, size = 200 -> ((4-1)/4)^2 * 200 = (3/4)^2 * 200 = 0.5625 * 200 = 112.5
    assert pytest.approx(compute_quadratic_score(4.0, 1.0, 200.0)) == 112.5

    # Out of bounds: s > v -> 0.0
    assert compute_quadratic_score(2.0, 2.5, 100.0) == 0.0
    # Boundary: s == v -> 0.0
    assert compute_quadratic_score(2.0, 2.0, 100.0) == 0.0
    # Negative spread or size <= 0
    assert compute_quadratic_score(2.0, -0.5, 100.0) == 0.0
    assert compute_quadratic_score(2.0, 0.5, 0.0) == 0.0
    assert compute_quadratic_score(0.0, 0.5, 100.0) == 0.0


def test_compute_competing_depth_score():
    bids = [
        {"price": "0.49", "size": "100"},
        {"price": "0.48", "size": "200"},
        {"price": "0.40", "size": "500"},  # 10 cents away from 0.50 -> exceeds max_spread 3.0c
    ]
    asks = [
        {"price": "0.51", "size": "100"},
        {"price": "0.52", "size": "150"},
    ]
    mid = 0.50
    v = 3.0  # max spread in cents

    score, b_usd, a_usd = compute_competing_depth_score(bids, asks, mid, v)
    assert score > 0.0
    # Bid 0.49 is 1.0c away: ((3-1)/3)^2 * 100 = 44.44
    # Bid 0.48 is 2.0c away: ((3-2)/3)^2 * 200 = 22.22
    # Ask 0.51 is 1.0c away: ((3-1)/3)^2 * 100 = 44.44
    # Ask 0.52 is 2.0c away: ((3-2)/3)^2 * 150 = 16.67
    expected_score = (4/9 * 100) + (1/9 * 200) + (4/9 * 100) + (1/9 * 150)
    assert pytest.approx(score, rel=1e-3) == expected_score
    assert pytest.approx(b_usd) == (100 * 0.49 + 200 * 0.48)
    assert pytest.approx(a_usd) == (100 * 0.51 + 150 * 0.52)


def test_compute_epoch_payout():
    # virtual_score = 100, competing_score = 900 -> 10% share
    # daily_rate = 1440.0 -> epoch payout = (1440 / 1440) * 0.10 = 0.10
    share_pct, epoch_payout, projected_daily = compute_epoch_payout(100.0, 900.0, 1440.0)
    assert pytest.approx(share_pct) == 10.0
    assert pytest.approx(epoch_payout) == 0.10
    assert pytest.approx(projected_daily) == 144.0


def test_engine_post_only_and_quoting(tmp_path: Path):
    ledger_file = tmp_path / "ledger.json"
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        base_spread_offset_cents=0.75,
        default_quote_size=100.0,
        ledger_path=ledger_file,
    )

    mkt = MarketConfig(
        condition_id="0xabc123",
        question="Test Wealth Tax Election",
        rate_per_day=2000.0,
        rewards_min_size=50.0,
        rewards_max_spread=3.0,
        tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
    )
    engine.register_market(mkt)

    # Orderbook with spread from 0.48 to 0.52 (mid = 0.50)
    bids = [{"price": "0.48", "size": "1000"}]
    asks = [{"price": "0.52", "size": "1000"}]
    engine.update_orderbook("tok_yes", bids, asks, tick_size=0.01)

    quotes = engine.virtual_quotes["tok_yes"]
    bid_quote = quotes["bid"]
    ask_quote = quotes["ask"]

    assert bid_quote is not None
    assert ask_quote is not None
    # Post-only guarantee: bid < best_ask (0.52), ask > best_bid (0.48)
    assert bid_quote.price < 0.52
    assert ask_quote.price > 0.48
    assert bid_quote.post_only_valid is True
    assert ask_quote.post_only_valid is True
    assert bid_quote.score > 0.0
    assert ask_quote.score > 0.0


def test_engine_simulated_fills_and_inventory_skew(tmp_path: Path):
    ledger_file = tmp_path / "ledger.json"
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        base_spread_offset_cents=1.0,
        default_quote_size=100.0,
        max_inventory_shares=500.0,
        max_skew_cents=0.5,
        ledger_path=ledger_file,
    )

    mkt = MarketConfig(
        condition_id="0xabc123",
        question="Test CA Wealth Tax",
        rate_per_day=2000.0,
        rewards_min_size=50.0,
        rewards_max_spread=3.0,
        tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
    )
    engine.register_market(mkt)

    bids = [{"price": "0.48", "size": "1000"}]
    asks = [{"price": "0.52", "size": "1000"}]
    engine.update_orderbook("tok_yes", bids, asks, tick_size=0.01)

    # At mid=0.50, s=1.0c -> bid=0.49, ask=0.51
    initial_bid_px = engine.virtual_quotes["tok_yes"]["bid"].price
    initial_ask_px = engine.virtual_quotes["tok_yes"]["ask"].price
    assert initial_bid_px == 0.49
    assert initial_ask_px == 0.51

    # 1. Real taker SELL executes at 0.49 -> crosses our virtual limit BUY!
    fill_buy = engine.on_trade("tok_yes", trade_price=0.49, trade_size=100.0, trade_side="SELL")
    assert fill_buy is not None
    assert fill_buy.side == "BUY"
    assert fill_buy.fill_price == 0.49
    assert fill_buy.fill_size == 100.0
    assert pytest.approx(engine.cash_usd) == 10000.0 - 49.0
    assert engine.positions["tok_yes"].shares == 100.0
    assert engine.positions["tok_yes"].avg_cost == 0.49

    # Inventory is now positive (+100 shares):
    # Quote skew should widen bid (lower bid price or increase bid offset) to avoid buying more
    new_bid_quote = engine.virtual_quotes["tok_yes"]["bid"]
    # Ratio is 100/500 = 0.2. Skew = 0.2 * 0.5c = 0.1c. Bid target = 1.0 + 0.1 = 1.1c.
    # Raw bid px = 0.50 - 0.011 = 0.489 -> floors to 0.48.
    assert new_bid_quote.price <= initial_bid_px

    # 2. Real taker BUY executes at 0.51 -> crosses our virtual limit SELL!
    fill_sell = engine.on_trade("tok_yes", trade_price=0.51, trade_size=100.0, trade_side="BUY")
    assert fill_sell is not None
    assert fill_sell.side == "SELL"
    assert fill_sell.fill_price == 0.51
    assert fill_sell.fill_size == 100.0
    assert pytest.approx(engine.cash_usd) == 10000.0 - 49.0 + 51.0
    assert engine.positions["tok_yes"].shares == 0.0
    # Realized PnL: 100 * (0.51 - 0.49) = $2.00
    assert pytest.approx(engine.positions["tok_yes"].realized_pnl) == 2.00


def test_epoch_sampling_and_ledger_persistence(tmp_path: Path):
    ledger_file = tmp_path / "ledger.json"
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        base_spread_offset_cents=0.75,
        default_quote_size=100.0,
        ledger_path=ledger_file,
    )

    mkt = MarketConfig(
        condition_id="0xabc123",
        question="California Wealth Tax 2026",
        rate_per_day=1440.0,  # $1 per epoch pool
        rewards_min_size=50.0,
        rewards_max_spread=3.0,
        tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
    )
    engine.register_market(mkt)

    bids = [{"price": "0.49", "size": "200"}]
    asks = [{"price": "0.51", "size": "200"}]
    clock = [1_000_000.0]
    engine.clock = lambda: clock[0]
    engine.update_orderbook("tok_yes", bids, asks, tick_size=0.01)
    # The venue scores an order only after it has rested 3.5 s in the minute.
    clock[0] += 4.0

    # Run epoch sample
    snaps = engine.run_epoch_sample()
    assert len(snaps) == 1
    assert snaps[0].epoch_payout_usd > 0.0
    assert engine.total_rewards_earned_usd == snaps[0].epoch_payout_usd

    # Verify ledger was written
    assert ledger_file.exists()
    with open(ledger_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["system_info"]["status"] == "RUNNING"
    assert data["portfolio"]["total_rewards_earned_usd"] > 0
    assert "0xabc123" in data["markets"]
    tok_data = data["markets"]["0xabc123"]["tokens"]["tok_yes"]
    assert tok_data["virtual_quotes"]["bid"]["quadratic_score"] > 0
    assert tok_data["virtual_quotes"]["ask"]["quadratic_score"] > 0


def test_queue_depletion_and_surplus_fill(tmp_path: Path):
    """Gap F1: Verify trade volume decrements queue_ahead and surplus volume generates fills."""
    ledger_file = tmp_path / "ledger.json"
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        base_spread_offset_cents=1.0,
        default_quote_size=100.0,
        max_skew_cents=0.0,
        ledger_path=ledger_file,
    )

    mkt = MarketConfig(
        condition_id="0xqueue1",
        question="Queue Depletion Test",
        rate_per_day=1000.0,
        rewards_min_size=50.0,
        rewards_max_spread=3.0,
        tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
    )
    engine.register_market(mkt)

    # Orderbook: 300 resting at 0.49, 500 at 0.48. Asks at 0.51 (mid = 0.50)
    bids = [{"price": "0.49", "size": "300"}, {"price": "0.48", "size": "500"}]
    asks = [{"price": "0.51", "size": "500"}]
    engine.update_orderbook("tok_yes", bids, asks, tick_size=0.01)

    quote_bid = engine.virtual_quotes["tok_yes"]["bid"]
    assert quote_bid.price == 0.49
    # Queue ahead must equal competing volume at or better than quote price (300.0)
    assert quote_bid.queue_ahead == 300.0

    # 1. Trade size 100 <= queue_ahead 300 -> queue depleted to 200, NO fill
    fill1 = engine.on_trade("tok_yes", trade_price=0.49, trade_size=100.0, trade_side="SELL")
    assert fill1 is None
    assert engine.virtual_quotes["tok_yes"]["bid"].queue_ahead == 200.0
    assert engine.positions["tok_yes"].shares == 0.0

    # 2. Trade size 150 <= queue_ahead 200 -> queue depleted to 50, NO fill
    fill2 = engine.on_trade("tok_yes", trade_price=0.49, trade_size=150.0, trade_side="SELL")
    assert fill2 is None
    assert engine.virtual_quotes["tok_yes"]["bid"].queue_ahead == 50.0

    # 3. Trade size 120 > queue_ahead 50 -> surplus is 70 (120 - 50). Fills 70 shares!
    fill3 = engine.on_trade("tok_yes", trade_price=0.49, trade_size=120.0, trade_side="SELL")
    assert fill3 is not None
    assert fill3.fill_size == 70.0
    assert fill3.fill_price == 0.49
    assert engine.positions["tok_yes"].shares == 70.0
    assert engine.virtual_quotes["tok_yes"]["bid"].queue_ahead == 0.0


def test_queue_priority_retention_and_price_change_reset(tmp_path: Path):
    """Gap F1: Verify queue_ahead is retained on book updates if price is unchanged, and reset on price change."""
    ledger_file = tmp_path / "ledger.json"
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        base_spread_offset_cents=1.0,
        default_quote_size=100.0,
        ledger_path=ledger_file,
    )

    mkt = MarketConfig(
        condition_id="0xqueue2",
        question="Queue Retention Test",
        rate_per_day=1000.0,
        rewards_min_size=50.0,
        rewards_max_spread=3.0,
        tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
    )
    engine.register_market(mkt)

    bids = [{"price": "0.49", "size": "500"}]
    asks = [{"price": "0.51", "size": "500"}]
    engine.update_orderbook("tok_yes", bids, asks, tick_size=0.01)

    assert engine.virtual_quotes["tok_yes"]["bid"].queue_ahead == 500.0

    # Deplete queue by 200 shares
    engine.on_trade("tok_yes", trade_price=0.49, trade_size=200.0, trade_side="SELL")
    assert engine.virtual_quotes["tok_yes"]["bid"].queue_ahead == 300.0

    # Update orderbook where depth grows at 0.49 to 1000 shares, but mid & quote price remain 0.49
    bids_deeper = [{"price": "0.49", "size": "1000"}]
    engine.update_orderbook("tok_yes", bids_deeper, asks, tick_size=0.01)

    # Queue priority MUST be retained (does NOT reset to 1000)
    assert engine.virtual_quotes["tok_yes"]["bid"].price == 0.49
    assert engine.virtual_quotes["tok_yes"]["bid"].queue_ahead == 300.0

    # Book shifts: mid moves from 0.50 to 0.52 -> quote price moves to 0.51
    bids_shifted = [{"price": "0.51", "size": "450"}]
    asks_shifted = [{"price": "0.53", "size": "450"}]
    engine.update_orderbook("tok_yes", bids_shifted, asks_shifted, tick_size=0.01)

    new_bid = engine.virtual_quotes["tok_yes"]["bid"]
    assert new_bid.price == 0.51
    # Queue priority lost upon price change -> resets to competing depth at 0.51 (450.0)
    assert new_bid.queue_ahead == 450.0


def test_crossed_through_instant_aggressive_fill(tmp_path: Path):
    """Gap F1: When real trade crosses through quote price, execute instant aggressive fill."""
    ledger_file = tmp_path / "ledger.json"
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        base_spread_offset_cents=1.0,
        default_quote_size=100.0,
        ledger_path=ledger_file,
    )

    mkt = MarketConfig(
        condition_id="0xcross1",
        question="Cross Through Test",
        rate_per_day=1000.0,
        rewards_min_size=50.0,
        rewards_max_spread=3.0,
        tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
    )
    engine.register_market(mkt)

    bids = [{"price": "0.49", "size": "1000"}]
    asks = [{"price": "0.51", "size": "1000"}]
    engine.update_orderbook("tok_yes", bids, asks, tick_size=0.01)

    assert engine.virtual_quotes["tok_yes"]["bid"].queue_ahead == 1000.0

    # Real trade sells aggressively at 0.48 (crossed through our 0.49 bid)
    fill = engine.on_trade("tok_yes", trade_price=0.48, trade_size=100.0, trade_side="SELL")
    assert fill is not None
    assert fill.fill_price == 0.49
    assert fill.fill_size == 100.0
    assert "crossed_through" in fill.trigger_reason


def test_batch_merging_and_gas_deduction(tmp_path: Path):
    """Gap F5: Complete-set merges only execute when min(YES, NO) >= auto_merge_threshold, deducting $0.015 gas."""
    ledger_file = tmp_path / "ledger.json"
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        base_spread_offset_cents=1.0,
        default_quote_size=100.0,
        max_skew_cents=0.0,
        auto_merge_threshold=100.0,
        merge_gas_fee_usd=0.015,
        ledger_path=ledger_file,
    )

    mkt = MarketConfig(
        condition_id="0xmerge1",
        question="Batch Merge Test Market",
        rate_per_day=1000.0,
        rewards_min_size=50.0,
        rewards_max_spread=3.0,
        tokens=[
            TokenConfig(token_id="tok_yes", outcome="YES", price=0.50),
            TokenConfig(token_id="tok_no", outcome="NO", price=0.50),
        ],
    )
    engine.register_market(mkt)

    # Initial books
    engine.update_orderbook("tok_yes", [{"price": "0.49", "size": "0"}], [{"price": "0.51", "size": "1000"}])
    engine.update_orderbook("tok_no", [{"price": "0.49", "size": "0"}], [{"price": "0.51", "size": "1000"}])

    # 1. Fill 60 shares of YES and 60 shares of NO (min(YES, NO) = 60 < threshold 100)
    engine.on_trade("tok_yes", trade_price=0.49, trade_size=60.0, trade_side="SELL")
    engine.on_trade("tok_no", trade_price=0.49, trade_size=60.0, trade_side="SELL")

    assert engine.positions["tok_yes"].shares == 60.0
    assert engine.positions["tok_no"].shares == 60.0
    # No merge executed yet
    assert len(engine.merges) == 0
    assert engine.total_merges_count == 0
    assert engine.total_gas_fees_usd == 0.0
    # Cash: 10000 - (60 * 0.49 + 60 * 0.49) = 10000 - 58.8 = 9941.20
    assert pytest.approx(engine.cash_usd) == 9941.20

    # 2. Fill the remaining resting 40 shares (from 100 quote size) of YES and NO -> inventory reaches 100 YES and 100 NO
    # min(YES, NO) = 100 >= threshold 100 -> triggers automated merge!
    engine.on_trade("tok_yes", trade_price=0.49, trade_size=50.0, trade_side="SELL")
    engine.on_trade("tok_no", trade_price=0.49, trade_size=50.0, trade_side="SELL")

    # 100 sets merged into 100 * 1.00 = $100.00 USDC, minus $0.015 gas fee = +$99.985
    # Previous cash: 9941.20 - (40 * 0.49 + 40 * 0.49) = 9941.20 - 39.20 = 9902.00
    # After merge: 9902.00 + 100.00 - 0.015 = 10001.985
    assert len(engine.merges) == 1
    assert engine.total_merges_count == 1
    assert pytest.approx(engine.total_gas_fees_usd) == 0.015
    assert pytest.approx(engine.cash_usd) == 10001.985
    # Both positions cleared
    assert engine.positions["tok_yes"].shares == 0.0
    assert engine.positions["tok_no"].shares == 0.0

    # Verify merge logged in ledger dict
    ledger_data = engine.to_ledger_dict()
    assert len(ledger_data["recent_merges"]) == 1
    assert ledger_data["recent_merges"][0]["shares_merged"] == 100.0
    assert ledger_data["recent_merges"][0]["gas_fee_usd"] == 0.015


def test_price_only_ofi_gate_pulls_threatened_ask(tmp_path: Path):
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        base_spread_offset_cents=0.75,
        default_quote_size=100.0,
        ledger_path=tmp_path / "ledger_gate.json",
        microstructure_gate=True,
    )
    engine.register_market(
        MarketConfig(
            condition_id="0xgate",
            question="Gate test",
            rate_per_day=2000.0,
            rewards_min_size=50.0,
            rewards_max_spread=3.0,
            tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
        )
    )

    # First top of book: no previous state, so both sides quote.
    engine.update_orderbook(
        "tok_yes", [{"price": "0.48", "size": "100"}], [{"price": "0.52", "size": "100"}], 0.01
    )
    first = engine.virtual_quotes["tok_yes"]
    assert first["bid"] is not None
    assert first["ask"] is not None

    # Bid rises, ask unchanged: trailing OFI is +1 (buy pressure), so the ask is
    # the threatened side and is pulled. The bid is safe and stays quoted.
    engine.update_orderbook(
        "tok_yes", [{"price": "0.49", "size": "100"}], [{"price": "0.52", "size": "100"}], 0.01
    )
    gated = engine.virtual_quotes["tok_yes"]
    assert gated["bid"] is not None
    assert gated["ask"] is None

