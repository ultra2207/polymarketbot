"""Comprehensive test suite for the native Polymarket simulator.

Tests:
1. LivePolymarketFeed:
   - Data models & snapshot parsing.
   - Subscription management.
   - WebSocket payload dispatch (book, best_bid_ask, price_change, trade).
   - REST queries (book, midpoint, spread, rewards).
   - Resilient fallback activation.
2. SyntheticOrderBook:
   - Level parsing and sorting.
   - Queue ahead calculation for bids and asks.
   - Depth walking (VWAP, slippage, level depletion).
3. MarketImpactEngine:
   - Multipliers (0.5x, 1.0x, 2.0x).
   - Endogenous square-root impact and post-fill displacement.
4. SimulationOrderMatcher:
   - Case 1: Normal trade at price P (queue depletion and surplus fill).
   - Case 2: Aggressive cross-through (instant fill on trade_price < bid or > ask).
   - Case 3: Taker market order execution crossing the spread with depth slippage.
   - Post-only quote constraints and cancellation.
"""

from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from polymarketbot.simulator.feed import (
    LivePolymarketFeed,
    MarketTrade,
    OrderBookLevel,
    OrderBookSnapshot,
    build_book_snapshot,
    parse_levels,
)
from polymarketbot.simulator.matcher import (
    FillEvent,
    MarketImpactEngine,
    SimulationOrderMatcher,
    SyntheticOrderBook,
    VirtualQuote,
)
from polymarketbot.simulator.engine import (
    MissingBookPriceError,
    NativePaperTradingEngine,
)


# =============================================================================
# 1. Feed & Data Model Tests
# =============================================================================
def test_parse_levels():
    raw_bids = [
        {"price": "0.48", "size": "500"},
        {"price": 0.49, "size": 1000},
        ["0.47", 200],
        {"price": "invalid", "size": "100"},
        {"price": "0.0", "size": "100"},
        {"price": "1.0", "size": "100"},
    ]
    # Reverse sort = True for bids (highest first)
    bids = parse_levels(raw_bids, reverse_sort=True)
    assert len(bids) == 3
    assert bids[0].price == 0.49 and bids[0].size == 1000.0
    assert bids[1].price == 0.48 and bids[1].size == 500.0
    assert bids[2].price == 0.47 and bids[2].size == 200.0

    raw_asks = [
        {"price": "0.52", "size": "600"},
        {"price": "0.51", "size": "400"},
    ]
    # Reverse sort = False for asks (lowest first)
    asks = parse_levels(raw_asks, reverse_sort=False)
    assert len(asks) == 2
    assert asks[0].price == 0.51 and asks[0].size == 400.0
    assert asks[1].price == 0.52 and asks[1].size == 600.0


def test_build_book_snapshot():
    raw_bids = [{"price": "0.45", "size": "1000"}]
    raw_asks = [{"price": "0.49", "size": "800"}]
    snap = build_book_snapshot("tok_1", raw_bids, raw_asks, tick_size=0.01)

    assert snap.asset_id == "tok_1"
    assert snap.best_bid == 0.45
    assert snap.best_ask == 0.49
    assert snap.mid == 0.47
    assert snap.spread == 0.04
    assert snap.spread_cents == 4.0
    d = snap.to_dict()
    assert d["asset_id"] == "tok_1"
    assert d["spread_cents"] == 4.0


def test_feed_subscription_management():
    feed = LivePolymarketFeed()
    feed.subscribe(["tok_a", "tok_b"])
    assert "tok_a" in feed.subscribed_tokens
    assert "tok_b" in feed.subscribed_tokens
    assert len(feed.subscribed_tokens) == 2

    feed.unsubscribe("tok_a")
    assert "tok_a" not in feed.subscribed_tokens
    assert "tok_b" in feed.subscribed_tokens
    assert len(feed.subscribed_tokens) == 1


def test_feed_ws_payload_processing():
    async def _async_test():
        feed = LivePolymarketFeed()
        received_books: list[OrderBookSnapshot] = []
        received_trades: list[MarketTrade] = []

        feed.add_book_listener(lambda b: received_books.append(b))
        feed.add_trade_listener(lambda t: received_trades.append(t))

        # 1. Test book message
        book_msg = json.dumps({
            "event_type": "book",
            "asset_id": "tok_1",
            "bids": [{"price": "0.40", "size": "500"}],
            "asks": [{"price": "0.44", "size": "600"}],
            "hash": "h123",
        })
        await feed._process_ws_payload(book_msg)
        assert len(received_books) == 1
        assert received_books[0].asset_id == "tok_1"
        assert received_books[0].best_bid == 0.40
        assert received_books[0].best_ask == 0.44

        # 2. Test trade message
        trade_msg = json.dumps({
            "event_type": "last_trade_price",
            "asset_id": "tok_1",
            "price": "0.44",
            "size": "150",
            "side": "BUY",
            "timestamp": 1700000000000,
        })
        await feed._process_ws_payload(trade_msg)
        assert len(received_trades) == 1
        assert received_trades[0].asset_id == "tok_1"
        assert received_trades[0].price == 0.44
        assert received_trades[0].size == 150.0
        assert received_trades[0].side == "BUY"

        # 3. Check event queue
        ev1 = await feed.get_next_event(timeout=0.1)
        assert ev1 is not None and ev1[0] == "book"
        ev2 = await feed.get_next_event(timeout=0.1)
        assert ev2 is not None and ev2[0] == "trade"

    asyncio.run(_async_test())


# =============================================================================
# 2. SyntheticOrderBook Tests
# =============================================================================
def test_synthetic_order_book_depth_and_queue():
    book = SyntheticOrderBook(asset_id="tok_yes", tick_size=0.01)
    bids = [
        {"price": "0.49", "size": "200"},
        {"price": "0.48", "size": "300"},
        {"price": "0.47", "size": "500"},
    ]
    asks = [
        {"price": "0.51", "size": "250"},
        {"price": "0.52", "size": "450"},
        {"price": "0.53", "size": "800"},
    ]
    book.update_levels(bids, asks)

    assert book.best_bid == 0.49
    assert book.best_ask == 0.51
    assert book.mid_price == 0.50
    assert book.spread == 0.02
    assert book.spread_cents == 2.0
    assert book.total_bid_depth == 1000.0
    assert book.total_ask_depth == 1500.0

    # Queue ahead testing for BUY:
    # A quote placed at 0.49 sits behind the existing 200 shares at 0.49
    q_49 = book.compute_queue_ahead("BUY", 0.49)
    assert q_49 == 200.0
    # A quote placed at 0.48 sits behind 200 (at 0.49) + 300 (at 0.48) = 500
    q_48 = book.compute_queue_ahead("BUY", 0.48)
    assert q_48 == 500.0

    # Queue ahead testing for SELL:
    # A quote placed at 0.51 sits behind the existing 250 shares at 0.51
    q_51 = book.compute_queue_ahead("SELL", 0.51)
    assert q_51 == 250.0
    # A quote placed at 0.52 sits behind 250 (at 0.51) + 450 (at 0.52) = 700
    q_52 = book.compute_queue_ahead("SELL", 0.52)
    assert q_52 == 700.0


def test_synthetic_order_book_walk():
    book = SyntheticOrderBook(asset_id="tok_yes")
    bids = [
        {"price": "0.49", "size": "100"},
        {"price": "0.48", "size": "200"},
    ]
    asks = [
        {"price": "0.51", "size": "100"},
        {"price": "0.52", "size": "200"},
    ]
    book.update_levels(bids, asks)

    # 1. Taker BUY for 50 shares (fits entirely in best ask level 0.51)
    vwap, filled, levels, slip = book.walk_book("BUY", 50.0)
    assert vwap == 0.51
    assert filled == 50.0
    assert len(levels) == 1
    assert slip == 0.0

    # 2. Taker BUY for 200 shares (eats 100 @ 0.51 and 100 @ 0.52)
    # VWAP = (100 * 0.51 + 100 * 0.52) / 200 = 0.515
    # Slippage = 0.515 - 0.51 = 0.005 (0.5 cents)
    vwap2, filled2, levels2, slip2 = book.walk_book("BUY", 200.0)
    assert pytest.approx(vwap2) == 0.515
    assert filled2 == 200.0
    assert len(levels2) == 2
    assert pytest.approx(slip2) == 0.005

    # 3. Deplete the levels from book
    book.deplete_levels("BUY", levels2)
    # Remaining ask at 0.52 should be 100
    assert book.best_ask == 0.52
    assert book.asks[0].size == 100.0


# =============================================================================
# 3. MarketImpactEngine Tests
# =============================================================================
def test_market_impact_engine_scaling():
    engine_base = MarketImpactEngine(impact_multiplier=1.0)
    engine_stress = MarketImpactEngine(impact_multiplier=2.0)
    engine_calm = MarketImpactEngine(impact_multiplier=0.5)

    book = SyntheticOrderBook(asset_id="tok_1")
    book.update_levels(
        bids=[{"price": "0.48", "size": "1000"}],
        asks=[{"price": "0.52", "size": "1000"}],
    )

    impact_base = engine_base.calculate_taker_impact(trade_size=200.0, book=book, side="BUY", walk_slippage=0.005)
    impact_stress = engine_stress.calculate_taker_impact(trade_size=200.0, book=book, side="BUY", walk_slippage=0.005)
    impact_calm = engine_calm.calculate_taker_impact(trade_size=200.0, book=book, side="BUY", walk_slippage=0.005)

    assert impact_base > 0.0
    assert pytest.approx(impact_stress, abs=1e-4) == impact_base * 2.0
    assert pytest.approx(impact_calm, abs=1e-4) == impact_base * 0.5


# =============================================================================
# 4. SimulationOrderMatcher Tests
# =============================================================================
def test_order_matcher_post_only():
    matcher = SimulationOrderMatcher()
    matcher.update_book(
        "tok_1",
        bids=[{"price": "0.48", "size": "500"}],
        asks=[{"price": "0.52", "size": "500"}],
    )

    # Valid passive quotes
    q_bid = matcher.place_quote("tok_1", "BUY", price=0.49, size=100.0, post_only=True)
    assert q_bid is not None
    assert q_bid.price == 0.49

    q_ask = matcher.place_quote("tok_1", "SELL", price=0.51, size=100.0, post_only=True)
    assert q_ask is not None
    assert q_ask.price == 0.51

    # Invalid aggressive quotes that would cross the spread
    q_bad_bid = matcher.place_quote("tok_1", "BUY", price=0.53, size=100.0, post_only=True)
    assert q_bad_bid is None  # Rejected by post_only

    q_bad_ask = matcher.place_quote("tok_1", "SELL", price=0.47, size=100.0, post_only=True)
    assert q_bad_ask is None  # Rejected by post_only


def test_order_matcher_case_1_normal_trade_queue_depletion():
    """Case 1: Normal trade at price P only fills after queue_ahead is exhausted."""
    matcher = SimulationOrderMatcher()
    matcher.update_book(
        "tok_1",
        bids=[
            {"price": "0.49", "size": "200"},  # 200 shares ahead of us
            {"price": "0.48", "size": "500"},
        ],
        asks=[{"price": "0.52", "size": "500"}],
    )

    # Place our virtual BID at 0.49 for 100 shares
    quote = matcher.place_quote("tok_1", "BUY", price=0.49, size=100.0)
    assert quote is not None
    assert quote.queue_ahead == 200.0

    # 1. Market trade at 0.49 for 150 shares (taker sell)
    # 150 <= 200 -> queue depleted to 50, NO fill!
    fills1 = matcher.on_market_trade("tok_1", trade_price=0.49, trade_size=150.0, trade_side="SELL")
    assert len(fills1) == 0
    assert quote.queue_ahead == 50.0
    assert quote.size == 100.0

    # 2. Market trade at 0.49 for 120 shares (taker sell)
    # Remaining queue = 50. Surplus = 120 - 50 = 70 shares!
    # Our quote gets filled for 70 shares, remaining quote size = 30
    fills2 = matcher.on_market_trade("tok_1", trade_price=0.49, trade_size=120.0, trade_side="SELL")
    assert len(fills2) == 1
    f = fills2[0]
    assert f.side == "BUY"
    assert f.fill_price == 0.49
    assert f.fill_size == 70.0
    assert f.liquidity_type == "MAKER"
    assert f.queue_depleted is True
    assert f.fee_usd == 0.0  # Makers pay zero
    assert quote.queue_ahead == 0.0
    assert quote.size == 30.0


def test_order_matcher_case_2_aggressive_cross_through():
    """Case 2: Trade crossing through our quote price triggers instant aggressive fill."""
    matcher = SimulationOrderMatcher()
    matcher.update_book(
        "tok_1",
        bids=[{"price": "0.49", "size": "500"}],
        asks=[{"price": "0.52", "size": "500"}],
    )

    # Place our virtual BID at 0.49 for 100 shares with 500 queue ahead
    quote = matcher.place_quote("tok_1", "BUY", price=0.49, size=100.0)
    assert quote is not None
    assert quote.queue_ahead == 500.0

    # An aggressive market sell prints at 0.48 (below our bid of 0.49)
    # This crosses through our bid and executes immediately regardless of queue!
    fills = matcher.on_market_trade("tok_1", trade_price=0.48, trade_size=60.0, trade_side="SELL")
    assert len(fills) == 1
    f = fills[0]
    assert f.side == "BUY"
    assert f.fill_price == 0.49  # Filled at our resting quote price
    assert f.fill_size == 60.0
    assert f.liquidity_type == "MAKER"
    assert f.queue_depleted is True
    assert quote.size == 40.0


def test_order_matcher_case_3_taker_market_order():
    """Case 3: Taker market order execution crosses the spread with realistic depth slippage."""
    matcher = SimulationOrderMatcher(taker_fee_rate=0.03)  # 3% fee rate
    matcher.update_book(
        "tok_1",
        bids=[
            {"price": "0.48", "size": "200"},
            {"price": "0.47", "size": "300"},
        ],
        asks=[
            {"price": "0.52", "size": "100"},
            {"price": "0.53", "size": "200"},
        ],
    )

    # Bot executes taker BUY for 150 shares
    # Walks asks: 100 @ 0.52, 50 @ 0.53 -> VWAP = (52 + 26.5)/150 = 78.5/150 = 0.523333
    fill = matcher.execute_taker_order("tok_1", side="BUY", size=150.0)

    assert fill.side == "BUY"
    assert fill.liquidity_type == "TAKER"
    assert fill.fill_size == 150.0
    assert pytest.approx(fill.fill_price, rel=1e-4) == 0.523333
    assert fill.slippage > 0.0  # Slippage against best ask 0.52
    assert fill.post_fill_impact > 0.0
    assert fill.fee_usd > 0.0  # Taker pays fee

    # Verify book depth was depleted
    book = matcher.books["tok_1"]
    # 0.52 ask is fully depleted; new best ask should be >= 0.53
    assert book.best_ask >= 0.53


def test_cancellation_and_audit_history():
    matcher = SimulationOrderMatcher()
    matcher.update_book("tok_1", bids=[{"price": "0.48", "size": "100"}], asks=[{"price": "0.52", "size": "100"}])

    q1 = matcher.place_quote("tok_1", "BUY", price=0.48, size=50.0)
    q2 = matcher.place_quote("tok_1", "SELL", price=0.52, size=50.0)
    assert len(matcher.get_active_quotes()) == 2

    cancelled = matcher.cancel_quote("tok_1", q1.order_id)
    assert cancelled is not None and cancelled.is_cancelled is True
    assert len(matcher.get_active_quotes()) == 1

    matcher.cancel_all_quotes()
    assert len(matcher.get_active_quotes()) == 0


def test_feed_rest_queries_mocked():
    feed = LivePolymarketFeed()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "bids": [{"price": "0.45", "size": "100"}],
        "asks": [{"price": "0.55", "size": "200"}],
        "tick_size": "0.01",
        "hash": "0x123",
    }

    with patch.object(feed._ensure_session(), "get", return_value=mock_resp):
        snap = feed.get_book_sync("tok_test")
        assert snap is not None
        assert snap.asset_id == "tok_test"
        assert snap.best_bid == 0.45
        assert snap.best_ask == 0.55
        assert snap.source == "rest"

        # Test midpoint
        mock_resp.json.return_value = {"mid": "0.50"}
        mid = feed.get_midpoint_sync("tok_test")
        assert mid == 0.50

        # Test spread
        mock_resp.json.return_value = {"spread": "0.10"}
        sp = feed.get_spread_sync("tok_test")
        assert sp == 0.10

        # Test rewards
        mock_resp.json.return_value = {"data": [{"condition_id": "0xabc", "rate_per_day": 500.0}]}
        mkts = feed.get_rewards_markets_sync(limit=1)
        assert len(mkts) == 1
        assert mkts[0]["condition_id"] == "0xabc"


def test_feed_fallback_polling():
    async def _test():
        feed = LivePolymarketFeed(fallback_poll_interval=0.1)
        feed.subscribe("tok_poll")
        dispatched_snaps: list[OrderBookSnapshot] = []
        feed.add_book_listener(lambda s: dispatched_snaps.append(s))

        # Mock fetch_book
        dummy_snap = build_book_snapshot("tok_poll", [{"price": 0.48, "size": 100}], [{"price": 0.52, "size": 100}], source="rest")
        with patch.object(feed, "fetch_book", AsyncMock(return_value=dummy_snap)):
            feed._running = True
            feed._ws_connected = False  # WS is down, fallback should poll
            task = asyncio.create_task(feed._fallback_poll_loop())

            await asyncio.sleep(0.4)
            feed._running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert len(dispatched_snaps) >= 1
        assert dispatched_snaps[0].asset_id == "tok_poll"
        assert dispatched_snaps[0].source == "rest"

    asyncio.run(_test())


def test_order_matcher_multi_quote_priority():
    """Verify that multiple resting quotes fill in strict price priority."""
    matcher = SimulationOrderMatcher()
    matcher.update_book(
        "tok_1",
        bids=[{"price": "0.49", "size": "100"}],
        asks=[{"price": "0.53", "size": "100"}],
    )

    # Place two bids: one at 0.49 (higher) and one at 0.48 (lower)
    q1 = matcher.place_quote("tok_1", "BUY", price=0.49, size=50.0)
    q2 = matcher.place_quote("tok_1", "BUY", price=0.48, size=50.0)

    # Aggressive sell sweeps through both levels with 80 shares @ 0.47
    fills = matcher.on_market_trade("tok_1", trade_price=0.47, trade_size=80.0, trade_side="SELL")

    assert len(fills) == 2
    # First fill must be the higher bid @ 0.49 for 50 shares
    assert fills[0].fill_price == 0.49
    assert fills[0].fill_size == 50.0
    assert q1.is_filled is True

    # Second fill must be the lower bid @ 0.48 for remaining 30 shares
    assert fills[1].fill_price == 0.48
    assert fills[1].fill_size == 30.0
    assert q2.size == 20.0



# =============================================================================
# 5. NativePaperTradingEngine price honesty (no fabricated midpoints)
# =============================================================================
CONDITION_ID = "0xPRICE_HONESTY"


def _honesty_engine(tmp_path, **overrides) -> NativePaperTradingEngine:
    kwargs = {
        "condition_id": CONDITION_ID,
        "initial_cash": 100.0,
        "quote_size": 10.0,
        "db_path": tmp_path / "honesty.db",
        "ledger_path": tmp_path / "honesty.json",
    }
    kwargs.update(overrides)
    return NativePaperTradingEngine(**kwargs)


def _buy_yes(engine: NativePaperTradingEngine, price: float, size: float) -> None:
    engine.portfolio.process_fill(
        condition_id=CONDITION_ID,
        side="BUY",
        outcome="YES",
        price=price,
        size=size,
        is_maker=True,
    )


def test_engine_equity_excludes_position_without_live_book(tmp_path):
    engine = _honesty_engine(tmp_path)
    try:
        _buy_yes(engine, price=0.40, size=10.0)

        # No book: the position is not marked at its 0.40 purchase price.
        assert engine.get_equity() == pytest.approx(engine.portfolio.cash)
        state = engine._marked_portfolio_state()
        assert state["mark_basis"] == "book_mid_unpriced_positions_excluded"
        assert state["unmarked_positions"] == [CONDITION_ID]
        assert state["unrealized_pnl"] == pytest.approx(0.0)

        telemetry = engine.get_telemetry()
        assert telemetry["mid_price_yes"] is None
        assert telemetry["best_bid_yes"] is None
        assert telemetry["best_ask_yes"] is None
        assert telemetry["unmarked_positions"] == [CONDITION_ID]
    finally:
        engine.close()


def test_engine_marks_position_from_live_book(tmp_path):
    engine = _honesty_engine(tmp_path)
    try:
        engine.update_orderbook_snapshot(
            "YES",
            bids=[{"price": 0.49, "size": 100.0}],
            asks=[{"price": 0.51, "size": 100.0}],
        )
        _buy_yes(engine, price=0.40, size=10.0)

        mid = engine._mid_price("YES")
        assert mid is not None
        assert engine.get_equity() == pytest.approx(engine.portfolio.cash + 10.0 * mid)
        state = engine._marked_portfolio_state()
        assert state["mark_basis"] == "book_mid"
        assert state["unmarked_positions"] == []
        assert engine.get_telemetry()["mid_price_yes"] == pytest.approx(mid)
    finally:
        engine.close()


def test_engine_get_best_bid_ask_raises_without_live_quote(tmp_path):
    engine = _honesty_engine(tmp_path)
    try:
        with pytest.raises(MissingBookPriceError):
            engine.get_best_bid_ask("YES")

        engine.update_orderbook_snapshot(
            "YES",
            bids=[{"price": 0.49, "size": 100.0}],
            asks=[{"price": 0.51, "size": 100.0}],
        )
        assert engine.get_best_bid_ask("YES") == pytest.approx((0.49, 0.51))
        # The NO side is the binary complement of the live YES book.
        assert engine.get_best_bid_ask("NO") == pytest.approx((0.49, 0.51))
    finally:
        engine.close()


def test_engine_skips_quotes_and_switching_cost_without_two_sided_book(tmp_path):
    engine = _honesty_engine(tmp_path)
    try:
        engine.update_orderbook_snapshot("YES", bids=[{"price": 0.49, "size": 100.0}], asks=[])

        assert engine.active_quotes == {}
        with pytest.raises(MissingBookPriceError):
            engine.compute_switching_cost(daily_yield=20.0)
    finally:
        engine.close()


def test_engine_liquidation_never_invents_a_clearing_price(tmp_path):
    engine = _honesty_engine(tmp_path)
    try:
        _buy_yes(engine, price=0.40, size=10.0)
        res = engine.liquidate_all_positions()

        # No live bids: the lot stays in inventory instead of being cleared at 0.50.
        assert res["unliquidated_shares"] == {"YES": pytest.approx(10.0)}
        assert res["fills_executed"] == 0
        assert res["final_open_positions_count"] == 1
        assert res["pre_liquidation_equity_basis"] == "cost_basis"
        pos = engine.portfolio.get_position(CONDITION_ID)
        assert pos.shares_yes == pytest.approx(10.0)
        assert engine.portfolio.cash == pytest.approx(res["initial_cash"] - 10.0 * 0.40)
    finally:
        engine.close()


def test_engine_liquidation_sells_only_visible_bid_depth(tmp_path):
    engine = _honesty_engine(tmp_path)
    try:
        engine.update_orderbook_snapshot(
            "YES",
            bids=[{"price": 0.49, "size": 4.0}],
            asks=[{"price": 0.51, "size": 100.0}],
        )
        _buy_yes(engine, price=0.40, size=10.0)
        res = engine.liquidate_all_positions()

        assert res["shares_yes_liquidated"] == pytest.approx(4.0)
        assert res["unliquidated_shares"] == {"YES": pytest.approx(6.0)}
        assert res["final_open_positions_count"] == 1
        pos = engine.portfolio.get_position(CONDITION_ID)
        assert pos.shares_yes == pytest.approx(6.0)
    finally:
        engine.close()
