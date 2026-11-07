"""Unit tests for Polymarket paper trading and liquidity reward harvesting."""

import json
import pytest
from pathlib import Path

from polymarketbot.paper_trading.models import (
    AntiDelusionConfig,
    BrokenBasketPolicy,
    MarketConfig,
    PendingSettlement,
    TokenConfig,
    VirtualQuote,
)
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


def test_delusion_1_logistic_queue_clearance(tmp_path: Path):
    """Delusion 1: Empirical logistic queue clearance model earns fills based on standing depth."""
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        anti_delusion=True,
        anti_delusion_config=AntiDelusionConfig(deterministic_fills=True),
        ledger_path=tmp_path / "ledger_d1.json",
    )
    engine.register_market(
        MarketConfig(
            condition_id="0xd1",
            question="D1 test",
            rate_per_day=1000.0,
            rewards_min_size=10.0,
            rewards_max_spread=3.0,
            tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
        )
    )

    # Low queue ahead ratio has higher fill probability than high queue ahead ratio
    p_front = engine.compute_fill_probability(queue_ratio=0.05, size_at_level=100.0)
    p_back = engine.compute_fill_probability(queue_ratio=0.85, size_at_level=100.0)
    assert p_front > p_back
    assert 0.0 < p_front < 1.0
    assert 0.0 < p_back < 1.0

    # Test orderbook quotes compute empirical fill probability
    engine.update_orderbook(
        "tok_yes",
        [{"price": "0.49", "size": "200"}],
        [{"price": "0.51", "size": "200"}],
        0.01,
    )
    quote = engine.virtual_quotes["tok_yes"]["bid"]
    assert quote is not None
    assert 0.0 < quote.fill_probability < 1.0

    # Trade prints at quote price with surplus: in anti-delusion deterministic mode,
    # fill is scaled by p_earn < 1.0 instead of 100% naive fill
    fill = engine.on_trade(
        "tok_yes",
        trade_price=quote.price,
        trade_size=quote.queue_ahead + 100.0,
        trade_side="SELL",
    )
    assert fill is not None
    assert 0.0 < fill.fill_probability <= 1.0
    assert fill.fill_size < 100.0  # Earned fractional fill rather than naive full fill


def test_delusion_2_adverse_selection_and_binance_alpha_gate(tmp_path: Path):
    """Delusion 2: Passive markout haircut and Binance Lead-Lag Alpha Gate intercept."""
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        anti_delusion=True,
        anti_delusion_config=AntiDelusionConfig(markout_drift_cents=0.5, deterministic_fills=True),
        binance_threshold_bps=10.0,
        ledger_path=tmp_path / "ledger_d2.json",
    )
    engine.register_market(
        MarketConfig(
            condition_id="0xd2",
            question="D2 test",
            rate_per_day=1000.0,
            rewards_min_size=10.0,
            rewards_max_spread=3.0,
            tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
        )
    )
    engine.update_orderbook(
        "tok_yes",
        [{"price": "0.49", "size": "100"}],
        [{"price": "0.51", "size": "100"}],
        0.01,
    )

    # 1. Passive fill markout haircut
    quote = engine.virtual_quotes["tok_yes"]["bid"]
    assert quote is not None
    # Trade crosses through bid (taker SELL)
    fill = engine.on_trade("tok_yes", trade_price=0.48, trade_size=50.0, trade_side="SELL")
    assert fill is not None
    # Drift is 0.5 cents = 0.005, effective fill price is 0.49 + 0.005 = 0.495
    assert pytest.approx(fill.markout_drift) == 0.005
    assert fill.markout_haircut_usd > 0.0
    assert engine.total_markout_haircut_usd > 0.0

    # 2. Binance Lead-Lag Alpha Gate: 25 bps surge pulls resting SELL quote before toxic sweep arrives
    engine.update_binance_spot("BTCUSDT", 60000.0, timestamp=100.0)
    engine.update_orderbook(
        "tok_yes",
        [{"price": "0.48", "size": "100"}],
        [{"price": "0.52", "size": "100"}],
        0.01,
    )
    assert engine.virtual_quotes["tok_yes"]["ask"] is not None

    # Binance spot jumps by +25 bps (> 10 bps threshold)
    engine.update_binance_spot("BTCUSDT", 60150.0, timestamp=102.0)
    # Aggressive taker BUY arrives to sweep the ask
    sweep_fill = engine.on_trade(
        "tok_yes",
        trade_price=0.53,
        trade_size=100.0,
        trade_side="BUY",
        timestamp=102.5,
    )
    # Threatened quote was pulled before toxic sweep could fill it
    assert sweep_fill is None
    assert engine.virtual_quotes["tok_yes"]["ask"] is None


def test_delusion_3_latency_taker_sniping_delusion(tmp_path: Path):
    """Delusion 3: Realistic relayer latency decay limits taker sniping probability to <= 5%."""
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        anti_delusion=True,
        anti_delusion_config=AntiDelusionConfig(max_taker_snipe_prob=0.05, latency_decay_rate=15.0),
        ledger_path=tmp_path / "ledger_d3.json",
    )
    engine.register_market(
        MarketConfig(
            condition_id="0xd3",
            question="D3 test",
            rate_per_day=1000.0,
            rewards_min_size=10.0,
            rewards_max_spread=3.0,
            tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
        )
    )
    engine.update_orderbook(
        "tok_yes",
        [{"price": "0.49", "size": "500"}],
        [{"price": "0.51", "size": "500"}],
        0.01,
    )

    # When max_taker_snipe_prob is 0.0 (or high latency), taker order is guaranteed rejected
    cfg_zero = AntiDelusionConfig(max_taker_snipe_prob=0.0)
    engine_zero = PaperTradingEngine(
        initial_cash_usd=10000.0,
        anti_delusion=True,
        anti_delusion_config=cfg_zero,
    )
    engine_zero.register_market(
        MarketConfig(
            condition_id="0xd3",
            question="D3 test",
            rate_per_day=1000.0,
            rewards_min_size=10.0,
            rewards_max_spread=3.0,
            tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
        )
    )
    engine_zero.update_orderbook("tok_yes", [{"price": "0.49", "size": "500"}], [{"price": "0.51", "size": "500"}], 0.01)
    fill_blocked = engine_zero.execute_taker_order("tok_yes", "BUY", 10.0, relayer_latency_ms=150.0)
    assert fill_blocked is None

    # Over 200 snipe attempts with 150ms latency, empirical success rate is capped <= 12%
    successes = 0
    attempts = 200
    for _ in range(attempts):
        fill = engine.execute_taker_order("tok_yes", "BUY", 1.0, relayer_latency_ms=150.0)
        if fill is not None:
            successes += 1
    assert (successes / attempts) <= 0.12  # Capped at realistic latency decay <= 5% (with binomial noise)


def test_delusion_4_broken_basket_execution_and_unwind(tmp_path: Path):
    """Delusion 4: Multi-leg basket risk and broken basket resolution via market unwind."""
    engine_base = PaperTradingEngine(
        initial_cash_usd=10000.0,
        anti_delusion=False,
        ledger_path=tmp_path / "ledger_d4.json",
    )
    engine_base.register_market(
        MarketConfig(
            condition_id="0xd4",
            question="D4 test",
            rate_per_day=1000.0,
            rewards_min_size=10.0,
            rewards_max_spread=3.0,
            tokens=[
                TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50),
                TokenConfig(token_id="tok_no", outcome="No", price=0.50),
            ],
        )
    )
    engine_base.update_orderbook("tok_yes", [{"price": "0.48", "size": "100"}], [{"price": "0.52", "size": "100"}], 0.01)
    engine_base.update_orderbook("tok_no", [{"price": "0.48", "size": "100"}], [{"price": "0.52", "size": "100"}], 0.01)

    # Leg 1 has realistic max_price, leg 2 has impossibly low max_price (simulates execution leg failure)
    legs = [
        {"asset_id": "tok_yes", "side": "BUY", "size": 10.0, "max_price": 0.60},
        {"asset_id": "tok_no", "side": "BUY", "size": 10.0, "max_price": 0.10},  # will fail: 0.52 > 0.10
    ]

    res = engine_base.execute_basket_orders(legs, condition_id="0xd4", policy=BrokenBasketPolicy.UNWIND_MARKET)
    assert res["status"] == "BROKEN_BASKET_RESOLVED"
    assert res["failed_leg_index"] == 1
    assert res["resolution"]["policy_applied"] == BrokenBasketPolicy.UNWIND_MARKET.value
    assert len(engine_base.broken_basket_events) == 1
    # After market unwind of leg 1, inventory is returned to 0
    assert engine_base.positions["tok_yes"].shares == 0.0


def test_delusion_5_uma_settlement_capital_lockup(tmp_path: Path):
    """Delusion 5: Enforce mandatory 3.11-hour UMA settlement delay and capital lockup."""
    engine = PaperTradingEngine(
        initial_cash_usd=1000.0,
        anti_delusion=True,
        ledger_path=tmp_path / "ledger_d5.json",
    )
    engine.register_market(
        MarketConfig(
            condition_id="0xd5",
            question="D5 test",
            rate_per_day=1000.0,
            rewards_min_size=10.0,
            rewards_max_spread=3.0,
            tokens=[
                TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50),
                TokenConfig(token_id="tok_no", outcome="No", price=0.50),
            ],
        )
    )
    # Acquire 100 shares of YES at 0.50
    engine.positions["tok_yes"].shares = 100.0
    engine.positions["tok_yes"].avg_cost = 0.50
    engine.cash_usd = 950.0

    res_time = 1000.0
    settlement = engine.settle_market("0xd5", payout_numerators={"tok_yes": 1.0, "tok_no": 0.0}, resolution_timestamp=res_time)
    assert isinstance(settlement, PendingSettlement)
    assert settlement.unlock_timestamp == res_time + 11196.0  # 3.11 hours = 11196 seconds
    assert settlement.is_unlocked is False
    assert settlement.payout_usd == 100.0
    assert settlement.net_payout_usd > 99.0  # after dynamic redemption gas

    # Capital is locked and NOT available for trading
    assert pytest.approx(engine.locked_settlement_cash_usd) == settlement.net_payout_usd
    util = engine.get_capital_utilization()
    assert util["locked_settlement_cash_usd"] == round(settlement.net_payout_usd, 2)
    assert util["available_cash_usd"] == 950.0

    # 1. Advance clock to 1000 + 7200s (2.0 hours, within challenge window): still locked
    unlocked = engine.process_pending_settlements(current_timestamp=res_time + 7200.0)
    assert len(unlocked) == 0
    assert settlement.is_unlocked is False
    assert pytest.approx(engine.locked_settlement_cash_usd) == settlement.net_payout_usd

    # 2. Advance clock past 3.11 hours (1000 + 11200s): matures and unlocks!
    unlocked = engine.process_pending_settlements(current_timestamp=res_time + 11200.0)
    assert len(unlocked) == 1
    assert settlement.is_unlocked is True
    assert engine.locked_settlement_cash_usd == 0.0
    assert pytest.approx(engine.cash_usd) == 950.0 + settlement.net_payout_usd


def test_delusion_6_dynamic_gas_and_exact_taker_fee(tmp_path: Path):
    """Delusion 6: Dynamic Polygon PoS EIP-1559 gas fee and exact non-linear Polymarket taker fee."""
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        anti_delusion=True,
        ledger_path=tmp_path / "ledger_d6.json",
    )
    # Dynamic gas calculation: 40 Gwei base + 30 Gwei tip = 70 Gwei total @ $0.40/POL
    engine.update_gas_environment(base_fee_gwei=40.0, priority_fee_gwei=30.0, pol_usd_rate=0.40)
    # Merge: 78,000 gas * 70e-9 * 0.40 = 0.002184 USD
    fee_merge = engine.get_dynamic_gas_fee_usd("merge")
    assert pytest.approx(fee_merge, 1e-4) == 0.002184
    # Redeem: 82,000 gas * 70e-9 * 0.40 = 0.002296 USD
    fee_redeem = engine.get_dynamic_gas_fee_usd("redeem")
    assert pytest.approx(fee_redeem, 1e-4) == 0.002296

    # Exact non-linear Polymarket taker fee: C * 0.04 * p * (1 - p)
    # At p = 0.50 (max fee), size = 100: 100 * 0.04 * 0.25 = $1.00
    fee_50 = engine.compute_polymarket_taker_fee(100.0, 0.50)
    assert pytest.approx(fee_50) == 1.00
    # At p = 0.80, size = 100: 100 * 0.04 * 0.16 = $0.64
    fee_80 = engine.compute_polymarket_taker_fee(100.0, 0.80)
    assert pytest.approx(fee_80) == 0.64
    # At p = 0.95, size = 100: 100 * 0.04 * (0.95 * 0.05) = $0.19
    fee_95 = engine.compute_polymarket_taker_fee(100.0, 0.95)
    assert pytest.approx(fee_95) == 0.19


def test_delusion_7_order_book_depth_market_impact(tmp_path: Path):
    """Delusion 7: Concave Kyle's Lambda price impact walks the book rather than naive BBO fills."""
    engine = PaperTradingEngine(
        initial_cash_usd=10000.0,
        anti_delusion=True,
        ledger_path=tmp_path / "ledger_d7.json",
    )
    # Impact formula: P_entry = P0 + 0.004 * (Size / Depth)^1.2 for BUY
    p0 = 0.50
    depth = 100.0
    p_small = engine.compute_market_impact(p0, 10.0, depth, "BUY")
    p_unit = engine.compute_market_impact(p0, 100.0, depth, "BUY")
    p_large = engine.compute_market_impact(p0, 300.0, depth, "BUY")

    assert p0 < p_small < p_unit < p_large
    assert pytest.approx(p_unit, 1e-4) == 0.5040  # 0.50 + 0.004 * (1.0)^1.2 = 0.5040

    # SELL impact pushes price downward
    p_sell = engine.compute_market_impact(p0, 100.0, depth, "SELL")
    assert pytest.approx(p_sell, 1e-4) == 0.4960  # 0.50 - 0.004 * (1.0)^1.2 = 0.4960


def test_delusion_8_capital_utilization_and_opportunity_gating(tmp_path: Path):
    """Delusion 8: Realistic capital utilization tracking and compounding gating."""
    engine = PaperTradingEngine(
        initial_cash_usd=1000.0,
        anti_delusion=True,
        ledger_path=tmp_path / "ledger_d8.json",
    )
    engine.register_market(
        MarketConfig(
            condition_id="0xd8",
            question="D8 test",
            rate_per_day=1000.0,
            rewards_min_size=10.0,
            rewards_max_spread=3.0,
            tokens=[TokenConfig(token_id="tok_yes", outcome="Yes", price=0.50)],
        )
    )
    engine.update_orderbook("tok_yes", [{"price": "0.49", "size": "100"}], [{"price": "0.51", "size": "100"}], 0.01)

    # Utilization tracks unencumbered cash vs resting bids
    util = engine.get_capital_utilization()
    assert util["total_equity_usd"] == 1000.0
    assert util["cash_usd"] == 1000.0
    assert util["open_bids_notional_usd"] == 49.0
    assert util["available_cash_usd"] == 951.0  # 1000.0 - 49.0 open bid notional
    assert 0.0 < util["utilization_rate"] < 1.0

    # Lock capital via pending settlement
    engine.locked_settlement_cash_usd = 800.0
    util2 = engine.get_capital_utilization()
    assert util2["locked_settlement_cash_usd"] == 800.0
    assert util2["total_encumbered_capital_usd"] > util["total_encumbered_capital_usd"]
    assert util2["utilization_rate"] > util["utilization_rate"]


