"""Unit and integration tests for Polymarket Liquidity Rewards module."""

from __future__ import annotations

import pytest

from polymarketbot.rewards.engine import (
    LiquidityRewardsBotEngine,
    MarketRewardParameters,
)
from polymarketbot.rewards.inventory import AvellanedaStoikovModel
from polymarketbot.rewards.merger import (
    CompleteSetPosition,
    build_ctf_merge_transaction,
)
from polymarketbot.rewards.queue import OrderBookQueueSimulator
from polymarketbot.rewards.scoring import (
    OrderQuote,
    calculate_epoch_payout,
    compute_book_side_scores,
    compute_minimum_score,
    normalize_sample_score,
    order_spread_cents,
    quadratic_order_score,
)


def test_order_spread_cents() -> None:
    # BUY order below mid
    assert order_spread_cents(0.49, 0.50, "BUY") == pytest.approx(1.0)
    # SELL order above mid
    assert order_spread_cents(0.51, 0.50, "SELL") == pytest.approx(1.0)
    # Invalid side
    with pytest.raises(ValueError):
        order_spread_cents(0.50, 0.50, "HOLD")


def test_quadratic_order_score_basic() -> None:
    # v = 3 cents, s = 1 cent, size = 100 shares
    # ((3 - 1)/3)^2 * 100 = (4/9) * 100 = 44.4444...
    score = quadratic_order_score(max_spread_cents=3.0, spread_cents=1.0, size=100.0)
    assert score == pytest.approx(44.4444, rel=1e-4)

    # Disqualification when spread > max_spread
    assert quadratic_order_score(max_spread_cents=3.0, spread_cents=3.1, size=100.0) == 0.0

    # Disqualification when size < min_size
    assert (
        quadratic_order_score(
            max_spread_cents=3.0, spread_cents=1.0, size=50.0, min_size=100.0
        )
        == 0.0
    )

    # Disqualification when duration < 3.5 seconds
    assert (
        quadratic_order_score(
            max_spread_cents=3.0,
            spread_cents=1.0,
            size=100.0,
            duration_seconds=2.0,
            min_duration_seconds=3.5,
        )
        == 0.0
    )


def test_official_worked_example_from_docs() -> None:
    """Verify exact calculation from Polymarket official docs worked example."""
    mid = 0.50
    v = 3.0

    # Side one quotes:
    # - 100Q bid on m @ 0.49 (spread = 1c)
    # - 200Q bid on m @ 0.48 (spread = 2c)
    # - 100Q ask on m' @ 0.51 (m' mid is 0.50; ask 0.51 -> spread = 1c)
    quotes_one = [
        OrderQuote(side="BUY", outcome="YES", price=0.49, size=100.0),
        OrderQuote(side="BUY", outcome="YES", price=0.48, size=200.0),
        OrderQuote(side="SELL", outcome="NO", price=0.51, size=100.0),
    ]

    # Side two quotes (YES asks and NO bids):
    # - 100Q ask on m @ 0.515 (spread = 1.5c)
    # - 100Q bid on m' @ 0.48 (spread = 2c)
    # - 200Q ask on m @ 0.505 (spread = 0.5c)
    quotes_two = [
        OrderQuote(side="SELL", outcome="YES", price=0.515, size=100.0),
        OrderQuote(side="BUY", outcome="NO", price=0.48, size=100.0),
        OrderQuote(side="SELL", outcome="YES", price=0.505, size=200.0),
    ]

    all_quotes = quotes_one + quotes_two
    q1, q2 = compute_book_side_scores(all_quotes, mid_price_yes=mid, max_spread_cents=v)

    # Expected Q_one = ((2/3)^2 * 100) + ((1/3)^2 * 200) + ((2/3)^2 * 100) = 44.444 + 22.222 + 44.444 = 111.111
    assert q1 == pytest.approx(111.111, rel=1e-3)

    # Expected Q_two = ((1.5/3)^2 * 100) + ((1/3)^2 * 100) + ((2.5/3)^2 * 200) = 25 + 11.111 + 138.889 = 175.0
    assert q2 == pytest.approx(175.0, rel=1e-3)

    # Minimum score in [0.10, 0.90]:
    # max(min(111.111, 175.0), max(111.111/3, 175.0/3)) = max(111.111, 58.333) = 111.111
    q_min = compute_minimum_score(q1, q2, midpoint=mid, c_penalty=3.0)
    assert q_min == pytest.approx(111.111, rel=1e-3)


def test_minimum_score_single_sided_penalties() -> None:
    # When mid is inside [0.10, 0.90], single-sided order gets score / 3.0
    q1 = 300.0
    q2 = 0.0
    mid_center = 0.50
    q_min_center = compute_minimum_score(q1, q2, midpoint=mid_center, c_penalty=3.0)
    assert q_min_center == pytest.approx(100.0)

    # When mid is outside [0.10, 0.90] (e.g. 0.08), liquidity must be double-sided
    mid_tail = 0.08
    q_min_tail = compute_minimum_score(q1, q2, midpoint=mid_tail, c_penalty=3.0)
    assert q_min_tail == 0.0


def test_epoch_payout_and_floor() -> None:
    rate = 100.0

    # Case 1: Gross payout is $5.00 >= $1.00 -> Net payout is $5.00
    share, gross, net = calculate_epoch_payout(
        maker_epoch_score=50.0,
        total_epoch_score=1000.0,
        rate_per_day=rate,
        payout_floor_usd=1.00,
    )
    assert share == pytest.approx(0.05)
    assert gross == pytest.approx(5.00)
    assert net == pytest.approx(5.00)

    # Case 2: Gross payout is $0.50 < $1.00 floor -> Net payout is $0.00
    share, gross, net = calculate_epoch_payout(
        maker_epoch_score=5.0,
        total_epoch_score=1000.0,
        rate_per_day=rate,
        payout_floor_usd=1.00,
    )
    assert share == pytest.approx(0.005)
    assert gross == pytest.approx(0.50)
    assert net == 0.0


def test_avellaneda_stoikov_inventory_skew() -> None:
    as_model = AvellanedaStoikovModel(
        gamma=0.02,
        volatility=0.25,
        max_inventory=1000.0,
        touch_spread_cents=1.0,
        tick_size=0.01,
    )

    # When q = 0: quote symmetrically at 1.0c touch (mid - 0.5c, mid + 0.5c)
    mid = 0.50
    q0 = as_model.compute_quotes(
        mid_price=mid,
        inventory_q=0.0,
        base_quote_size=200.0,
        max_spread_cents=3.5,
    )
    assert q0.bid_price < mid < q0.ask_price
    assert q0.bid_size == 200.0
    assert q0.ask_size == 200.0
    assert q0.is_bid_active is True
    assert q0.is_ask_active is True

    # When q > 0 (long YES): skew to flatten
    # - Widen accumulating side (bid): bid drops lower
    # - Tighten reducing side (ask): ask drops closer to mid
    q_long = as_model.compute_quotes(
        mid_price=mid,
        inventory_q=600.0,
        base_quote_size=200.0,
        max_spread_cents=3.5,
    )
    assert q_long.bid_price <= q0.bid_price  # Widen/drop bid
    assert q_long.ask_price <= q0.ask_price  # Tighten ask closer to mid
    assert q_long.ask_size > q_long.bid_size  # Offer more on reducing side

    # When q reaches max_inventory: bid deactivated
    q_max = as_model.compute_quotes(
        mid_price=mid,
        inventory_q=1000.0,
        base_quote_size=200.0,
        max_spread_cents=3.5,
    )
    assert q_max.is_bid_active is False
    assert q_max.bid_size == 0.0
    assert q_max.is_ask_active is True


def test_complete_set_merge_logic() -> None:
    pos = CompleteSetPosition(auto_merge_threshold=10.0)

    # Buy 500 YES shares @ $0.40
    pos.record_fill(outcome="YES", side="BUY", price=0.40, size=500.0)
    assert pos.shares_yes == 500.0
    assert pos.shares_no == 0.0
    assert pos.usdc_cash == pytest.approx(-200.0)
    assert pos.net_inventory == 500.0
    assert pos.total_merged_shares == 0.0

    # Buy 300 NO shares @ $0.58
    # Holding 500 YES and 300 NO -> 300 complete sets can merge into $300 USDC!
    pos.record_fill(outcome="NO", side="BUY", price=0.58, size=300.0)

    # Auto-merge should have executed
    assert pos.total_merged_shares == 300.0
    assert pos.total_unlocked_usd == 300.0
    assert pos.shares_yes == 200.0
    assert pos.shares_no == 0.0
    # Net inventory before was 500 - 300 = 200; after merge it is still 200 - 0 = 200!
    assert pos.net_inventory == 200.0
    # Cash: -200 (YES buy) - 174 (NO buy) + 300 (Merge unlock) = -74 USDC
    assert pos.usdc_cash == pytest.approx(-74.0)

    # Test CTF transaction builder
    tx = build_ctf_merge_transaction(condition_id="0xabc123", amount_shares=100.0)
    assert tx["method"] == "mergePositions"
    assert tx["params"]["conditionId"] == "0xabc123"
    assert tx["params"]["amount"] == 100_000_000  # 6 decimals


def test_short_sale_split_and_merge() -> None:
    pos = CompleteSetPosition(auto_merge_threshold=10.0)
    # Sell 100 YES without holding any: splits $100 USDC -> holds 100 NO and receives $51
    pos.record_fill(outcome="YES", side="SELL", price=0.51, size=100.0)
    assert pos.shares_yes == 0.0
    assert pos.shares_no == 100.0
    assert pos.usdc_cash == pytest.approx(-49.0)
    assert pos.net_inventory == -100.0

    # Buy 100 YES @ 0.49: now holds 100 YES and 100 NO -> auto-merges into $100 USDC!
    pos.record_fill(outcome="YES", side="BUY", price=0.49, size=100.0)
    assert pos.shares_yes == 0.0
    assert pos.shares_no == 0.0
    assert pos.net_inventory == 0.0
    assert pos.total_merged_shares == 100.0
    assert pos.total_unlocked_usd == 100.0
    assert pos.usdc_cash == pytest.approx(2.0)  # Captured full 2c spread ($2.00)



def test_order_book_queue_and_fills() -> None:
    sim = OrderBookQueueSimulator(cancellation_rate_per_min=0.10)

    # Place bid with 500 shares competing depth ahead
    order = sim.sync_quote(
        order_id="bid_1",
        side="BUY",
        outcome="YES",
        price=0.49,
        size=100.0,
        competing_depth_at_price=500.0,
        current_epoch=1,
    )
    assert order.queue_ahead == 500.0

    # Small trade of 200 shares sells at 0.49: doesn't reach us
    fills1 = sim.process_trade(
        trade_side="SELL",
        trade_price=0.49,
        trade_volume=200.0,
        current_mid=0.50,
        next_mid_60s=0.50,
        current_epoch=1,
    )
    assert len(fills1) == 0
    assert order.queue_ahead == 300.0

    # Aggressive trade sells through our price at 0.48: crosses our quote!
    fills2 = sim.process_trade(
        trade_side="SELL",
        trade_price=0.48,
        trade_volume=150.0,
        current_mid=0.50,
        next_mid_60s=0.485,
        current_epoch=2,
    )
    assert len(fills2) == 1
    f = fills2[0]
    assert f.is_cross is True
    assert f.fill_price == 0.49
    assert f.fill_size == 100.0
    # Spread captured: (0.50 mid - 0.49 fill) * 100 = 1.00 USD
    assert f.spread_captured == pytest.approx(1.0)
    # Adverse selection markout: fill 0.49 vs next_mid 0.485 = (0.49 - 0.485) * 100 = 0.50 USD
    assert f.adverse_selection_loss == pytest.approx(0.5)


def test_engine_epoch_simulation() -> None:
    mkt = MarketRewardParameters(
        condition_id="0x7586a96520578acaaaa4ea84a2582f197f84255da1f3392a7aa300386c187b37",
        question="Test Market",
        rate_per_day=500.0,
        rewards_min_size=100.0,
        rewards_max_spread=3.0,
        initial_mid=0.50,
        category="politics",
        competing_qualifying_depth=5000.0,
        competing_touch_depth=1000.0,
    )

    engine = LiquidityRewardsBotEngine(
        market=mkt,
        quote_size=100.0,
        risk_gamma=0.01,
        max_inventory=1000.0,
        seed=123,
    )

    # Run 1 epoch (1440 minutes)
    report = engine.simulate_epoch(epoch_idx=1, n_minutes=1440)

    assert report.epoch_index == 1
    assert report.daily_rate == 500.0
    assert report.allocated_capital > 0.0
    assert report.gross_rewards >= 0.0
    assert report.net_pnl == pytest.approx(
        report.net_rewards
        + report.spread_captured
        - report.adverse_selection
        - report.inventory_decay,
        rel=1e-2,
    )
    assert isinstance(report.passed_payout_floor, bool)
