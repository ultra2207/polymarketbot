"""Comprehensive Unit Tests for In-Memory L2 Delta Book and Virtual Queue Tracking.

Validates:
1. FastL2Book: snapshot ingestion, delta updates, O(1) top-of-book, cumulative depth,
   volume-weighted micro-price, and WebSocket frame parsing.
2. QueuePositionTracker: FIFO trade consumption from front, queue rank tracking,
   cancellation models (Proportional, Conservative, Optimistic), and fill simulation.
3. QueueAwareCancellationPolicy: urgency tiering (URGENT front vs THROTTLED deep),
   rate limiting, and DedicatedCancellationPool execution.
4. VirtualQueueOrderBookEngine: end-to-end integration and high-throughput benchmarks.
"""

import time
from unittest.mock import MagicMock

import pytest

from polymarketbot.trading.virtual_queue_book import (
    CancellationDecision,
    CancellationModel,
    CancellationUrgency,
    FastL2Book,
    OrderBookLevel,
    OrderSide,
    QueueAwareCancellationPolicy,
    QueueCancellationConfig,
    QueuePositionTracker,
    RateLimitTokenBucket,
    TrackedOrder,
    VirtualQueueOrderBookEngine,
)


# ============================================================================
# 1. FastL2Book Tests
# ============================================================================

def test_fast_l2_book_initial_state():
    """Verify empty book returns expected defaults."""
    book = FastL2Book(asset_id="test_token_123")
    assert book.asset_id == "test_token_123"
    assert book.best_bid is None
    assert book.best_bid_size == 0.0
    assert book.best_ask is None
    assert book.best_ask_size == 0.0
    assert book.bbo == (None, None)
    assert book.spread is None
    assert book.mid_price is None
    assert book.weighted_micro_price is None
    assert not book.is_crossed
    assert book.cumulative_depth(OrderSide.BUY) == 0.0
    assert book.cumulative_depth(OrderSide.SELL) == 0.0


def test_fast_l2_book_snapshot():
    """Verify L2 snapshot maintains descending bids and ascending asks."""
    book = FastL2Book()
    raw_bids = [
        {"price": "0.45", "size": "100"},
        {"price": "0.48", "size": "250"},
        {"price": "0.40", "size": "50"},
    ]
    raw_asks = [
        {"price": "0.55", "size": "300"},
        {"price": "0.52", "size": "150"},
        {"price": "0.60", "size": "500"},
    ]

    book.apply_snapshot(raw_bids, raw_asks)

    # Top of book
    assert book.best_bid == 0.48
    assert book.best_bid_size == 250.0
    assert book.best_ask == 0.52
    assert book.best_ask_size == 150.0

    assert book.bbo == ((0.48, 250.0), (0.52, 150.0))
    assert book.spread == 0.04
    assert book.mid_price == 0.50

    # Volume-weighted micro-price:
    # (250 * 0.52 + 150 * 0.48) / (250 + 150) = (130 + 72) / 400 = 202 / 400 = 0.505
    assert book.weighted_micro_price == 0.505

    # Check ordering
    bids = book.get_levels(OrderSide.BUY)
    assert [b.price for b in bids] == [0.48, 0.45, 0.40]

    asks = book.get_levels(OrderSide.SELL)
    assert [a.price for a in asks] == [0.52, 0.55, 0.60]


def test_fast_l2_book_delta_updates():
    """Verify incremental delta additions, updates, and removals."""
    book = FastL2Book()
    book.apply_snapshot(
        bids=[(0.48, 100.0)],
        asks=[(0.52, 100.0)],
    )

    # Add new better bid (0.49)
    old_size, new_size = book.apply_delta("BUY", 0.49, 150.0)
    assert old_size == 0.0
    assert new_size == 150.0
    assert book.best_bid == 0.49
    assert book.best_bid_size == 150.0
    assert book.spread == 0.03

    # Update size of 0.49
    old_size, new_size = book.apply_delta("BUY", 0.49, 300.0)
    assert old_size == 150.0
    assert new_size == 300.0
    assert book.best_bid_size == 300.0

    # Delete 0.49 by setting size to 0
    old_size, new_size = book.apply_delta("BUY", 0.49, 0.0)
    assert old_size == 300.0
    assert new_size == 0.0
    assert book.best_bid == 0.48
    assert book.best_bid_size == 100.0

    # Insert multiple asks and verify ascending order
    book.apply_delta("SELL", 0.58, 200.0)
    book.apply_delta("SELL", 0.51, 80.0)
    assert book.best_ask == 0.51
    assert [a.price for a in book.get_levels(OrderSide.SELL)] == [0.51, 0.52, 0.58]


def test_fast_l2_book_cumulative_depth():
    """Verify depth calculation at arbitrary price distances and level cutoffs."""
    book = FastL2Book()
    book.apply_snapshot(
        bids=[(0.50, 100.0), (0.49, 200.0), (0.48, 300.0), (0.45, 400.0)],
        asks=[(0.51, 150.0), (0.52, 250.0), (0.55, 350.0)],
    )

    # Bid depth within 0.02 of best bid (0.50 to 0.48): 100 + 200 + 300 = 600
    depth_bid_2c = book.cumulative_depth(OrderSide.BUY, max_price_distance=0.02)
    assert depth_bid_2c == 600.0

    # Bid depth limited to 2 levels: 100 + 200 = 300
    depth_bid_2l = book.cumulative_depth(OrderSide.BUY, max_levels=2)
    assert depth_bid_2l == 300.0

    # Ask depth within 0.005: 150.0 (only 0.51)
    depth_ask_half_c = book.cumulative_depth(OrderSide.SELL, max_price_distance=0.005)
    assert depth_ask_half_c == 150.0

    # Ask depth within 0.01: 150.0 + 250.0 = 400.0 (0.51 and 0.52)
    depth_ask_1c = book.cumulative_depth(OrderSide.SELL, max_price_distance=0.01)
    assert depth_ask_1c == 400.0

    # Total depth without distance limit
    assert book.cumulative_depth(OrderSide.BUY) == 1000.0
    assert book.cumulative_depth(OrderSide.SELL) == 750.0


def test_fast_l2_book_clob_frame_ingestion():
    """Verify ingestion of raw Polymarket CLOB WebSocket messages."""
    book = FastL2Book(asset_id="asset_yes")

    # Ingest "book" frame
    book_frame = {
        "event_type": "book",
        "asset_id": "asset_yes",
        "bids": [{"price": "0.45", "size": "100"}, {"price": "0.44", "size": "200"}],
        "asks": [{"price": "0.55", "size": "150"}],
    }
    book.apply_clob_frame(book_frame)
    assert book.best_bid == 0.45
    assert book.best_ask == 0.55

    # Ingest "price_change" frame
    delta_frame = {
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "asset_yes", "price": "0.46", "size": "300", "side": "BUY"},
            {"asset_id": "asset_other", "price": "0.47", "size": "999", "side": "BUY"},  # Ignored (wrong asset)
            {"asset_id": "asset_yes", "price": "0.45", "size": "0", "side": "BUY"},     # Removed
        ],
    }
    book.apply_clob_frame(delta_frame)
    assert book.best_bid == 0.46
    assert book.best_bid_size == 300.0
    assert book.depth_at_price(OrderSide.BUY, 0.45) == 0.0


def test_fast_l2_book_stoikov_micro_price():
    """Verify Stoikov multi-level micro-price calculation."""
    book = FastL2Book()
    book.apply_snapshot(
        bids=[(0.48, 1000.0), (0.47, 2000.0)],  # Very heavy bids
        asks=[(0.50, 100.0), (0.51, 100.0)],    # Thin asks
    )
    mid = book.mid_price
    stoikov = book.calculate_stoikov_micro_price(decay_alpha=0.5)

    assert mid == 0.49
    # Because bids are much heavier, fair micro-price should skew higher toward ask
    assert stoikov is not None and stoikov > mid


# ============================================================================
# 2. QueuePositionTracker Tests
# ============================================================================

def test_queue_tracker_registration():
    """Verify order tracking computes correct initial Q_ahead and Q_behind."""
    book = FastL2Book()
    book.apply_snapshot(bids=[(0.48, 500.0)], asks=[(0.52, 300.0)])
    tracker = QueuePositionTracker(book=book)

    # Case 1: Order is placed and already confirmed in the book snapshot
    order1 = tracker.track_order(
        order_id="ord_1",
        side="BUY",
        price=0.48,
        size=100.0,
        already_in_book=True,
    )
    # Q_ahead = 500 - 100 = 400
    assert order1.queue_ahead == 400.0
    assert order1.queue_behind == 0.0
    assert order1.queue_rank == 1.0  # 400 / (400 + 0) = 1.0 (at the back of existing queue)

    # Case 2: Order placed before book includes it
    order2 = tracker.track_order(
        order_id="ord_2",
        side="BUY",
        price=0.48,
        size=50.0,
        already_in_book=False,
    )
    assert order2.queue_ahead == 500.0
    assert order2.queue_behind == 0.0


def test_queue_tracker_trade_fifo_consumption():
    """Verify incoming trades consume queue ahead and simulate partial/full fills."""
    tracker = QueuePositionTracker()
    order = tracker.track_order(
        order_id="ord_test",
        side="BUY",
        price=0.48,
        size=100.0,
        initial_depth_ahead=300.0,
    )
    assert order.queue_ahead == 300.0
    assert order.remaining_size == 100.0
    assert order.status == "ACTIVE"

    # Trade 1: 150 shares trade at 0.48. Should consume half of Q_ahead, 0 fill
    fills1 = tracker.on_trade(side="BUY", price=0.48, size=150.0)
    assert len(fills1) == 0
    assert order.queue_ahead == 150.0
    assert order.remaining_size == 100.0
    assert order.status == "ACTIVE"

    # Trade 2: 200 shares trade at 0.48. Consumes remaining 150 of Q_ahead + fills 50 of our order!
    fills2 = tracker.on_trade(side="BUY", price=0.48, size=200.0)
    assert len(fills2) == 1
    assert fills2[0][0].order_id == "ord_test"
    assert fills2[0][1] == 50.0
    assert order.queue_ahead == 0.0
    assert order.remaining_size == 50.0
    assert order.filled_size == 50.0
    assert order.status == "PARTIALLY_FILLED"
    assert order.is_front

    # Trade 3: 50 shares trade. Fills remaining 50 shares -> FILLED
    fills3 = tracker.on_trade(side="BUY", price=0.48, size=50.0)
    assert len(fills3) == 1
    assert fills3[0][1] == 50.0
    assert order.remaining_size == 0.0
    assert order.filled_size == 100.0
    assert order.status == "FILLED"


def test_queue_tracker_expansion_enters_behind():
    """Verify new liquidity additions enter at queue tail (Q_ahead untouched)."""
    tracker = QueuePositionTracker()
    order = tracker.track_order(
        order_id="ord_tail",
        side="BUY",
        price=0.48,
        size=100.0,
        initial_depth_ahead=200.0,
    )
    assert order.queue_ahead == 200.0
    assert order.queue_behind == 0.0

    # Level size expands from 300 to 500 (+200)
    tracker.on_level_delta(side="BUY", price=0.48, old_size=300.0, new_size=500.0)

    # Q_ahead must remain exactly 200.0; Q_behind must absorb the +200.0
    assert order.queue_ahead == 200.0
    assert order.queue_behind == 200.0

    # Queue rank is now: 200 / (200 + 200) = 0.50 (middle of the queue)
    assert order.queue_rank == 0.50


def test_queue_tracker_cancellation_proportional():
    """Verify proportional cancellation attribution."""
    tracker = QueuePositionTracker()
    order = tracker.track_order(
        order_id="ord_prop",
        side="BUY",
        price=0.48,
        size=100.0,
        initial_depth_ahead=300.0,
        cancellation_model=CancellationModel.PROPORTIONAL,
    )
    # Expand tail by 300
    tracker.on_level_delta("BUY", 0.48, 400.0, 700.0)
    assert order.queue_ahead == 300.0
    assert order.queue_behind == 300.0

    # 200 shares cancelled. Ratio ahead = 300 / 600 = 0.5
    # Should reduce Q_ahead by 100 and Q_behind by 100
    tracker.on_level_delta("BUY", 0.48, 700.0, 500.0)
    assert order.queue_ahead == 200.0
    assert order.queue_behind == 200.0


def test_queue_tracker_cancellation_conservative():
    """Verify conservative cancellation attribution (orders behind cancel first)."""
    tracker = QueuePositionTracker()
    order = tracker.track_order(
        order_id="ord_cons",
        side="SELL",
        price=0.52,
        size=100.0,
        initial_depth_ahead=300.0,
        cancellation_model=CancellationModel.CONSERVATIVE,
    )
    # Expand tail by 200
    tracker.on_level_delta("SELL", 0.52, 400.0, 600.0)
    assert order.queue_ahead == 300.0
    assert order.queue_behind == 200.0

    # Cancellation of 150 shares: should come entirely from Q_behind
    tracker.on_level_delta("SELL", 0.52, 600.0, 450.0)
    assert order.queue_behind == 50.0
    assert order.queue_ahead == 300.0  # Ahead is untouched!

    # Further cancellation of 100 shares: exhausts 50 in Q_behind and reduces Q_ahead by 50
    tracker.on_level_delta("SELL", 0.52, 450.0, 350.0)
    assert order.queue_behind == 0.0
    assert order.queue_ahead == 250.0


# ============================================================================
# 3. QueueAwareCancellationPolicy Tests
# ============================================================================

def test_cancellation_policy_urgency_tiering():
    """Verify policy partitions orders into URGENT front vs THROTTLED deep."""
    tracker = QueuePositionTracker()

    # Order 1: Front of queue (Q_ahead = 50 <= 300) -> URGENT
    o_front = tracker.track_order(
        order_id="ord_front",
        side="BUY",
        price=0.48,
        size=100.0,
        initial_depth_ahead=50.0,
    )

    # Order 2: Deep queue (Q_ahead = 2500 >= 1000 * 2.0) -> THROTTLED
    o_deep = tracker.track_order(
        order_id="ord_deep",
        side="BUY",
        price=0.45,
        size=100.0,
        initial_depth_ahead=2500.0,
    )

    # Order 3: Intermediate queue (Q_ahead = 800) -> MONITOR
    o_mid = tracker.track_order(
        order_id="ord_mid",
        side="BUY",
        price=0.47,
        size=100.0,
        initial_depth_ahead=800.0,
    )

    policy = QueueAwareCancellationPolicy(
        tracker=tracker,
        config=QueueCancellationConfig(
            front_queue_threshold=300.0,
            sweep_protection_multiplier=2.0,
            min_toxic_shock_threshold=3.0,
        ),
    )

    # Evaluate against a 5.0 bps toxic shock with 1,000 share estimated sweep
    decisions = policy.evaluate_threatened_orders(
        threatened_side="BUY",
        toxic_shock_magnitude=5.0,
        estimated_sweep_size=1000.0,
    )

    assert len(decisions) == 3
    # Sorted by priority: front first!
    assert decisions[0].order_id == "ord_front"
    assert decisions[0].urgency == CancellationUrgency.URGENT
    assert decisions[0].recommended_action == "CANCEL_IMMEDIATE"

    assert decisions[1].order_id == "ord_mid"
    assert decisions[1].urgency == CancellationUrgency.MONITOR

    assert decisions[2].order_id == "ord_deep"
    assert decisions[2].urgency == CancellationUrgency.THROTTLED
    assert decisions[2].recommended_action == "DEFER"


def test_cancellation_policy_safe_sub_threshold():
    """Verify that sub-threshold price movements are classified as SAFE."""
    tracker = QueuePositionTracker()
    o = tracker.track_order("o1", "BUY", 0.48, 100.0, initial_depth_ahead=10.0)

    policy = QueueAwareCancellationPolicy(
        tracker=tracker,
        config=QueueCancellationConfig(min_toxic_shock_threshold=5.0),
    )
    decision = policy.evaluate_order(o, toxic_shock_magnitude=1.5)
    assert decision.urgency == CancellationUrgency.SAFE
    assert decision.recommended_action == "NO_ACTION"


def test_cancellation_policy_execution_with_mock_pool():
    """Verify execution triggers DedicatedCancellationPool for urgent orders and throttles deep orders."""
    tracker = QueuePositionTracker()
    o_front = tracker.track_order("o_front", "SELL", 0.52, 100.0, initial_depth_ahead=50.0)
    o_deep = tracker.track_order("o_deep", "SELL", 0.55, 100.0, initial_depth_ahead=3000.0)

    mock_pool = MagicMock()
    mock_pool.cancel_order.return_value = MagicMock(success=True, latency_ms=1.2)

    policy = QueueAwareCancellationPolicy(tracker=tracker)
    decisions = policy.evaluate_threatened_orders(
        threatened_side="SELL",
        toxic_shock_magnitude=10.0,
        estimated_sweep_size=1000.0,
    )

    results = policy.execute_cancellations(decisions, cancellation_pool=mock_pool)

    # Urgent order must call pool.cancel_order
    mock_pool.cancel_order.assert_called_once_with(order_id="o_front", headers=None)
    assert results["o_front"]["action"] == "CANCEL_SENT"
    assert results["o_front"]["urgency"] == "URGENT"

    # Deep order was throttled and avoided burning rate limit
    assert results["o_deep"]["action"] == "THROTTLED_BUFFER_PROTECTED"
    assert results["o_deep"]["urgency"] == "THROTTLED"

    # Urgent order removed from tracker; deep order preserved
    assert tracker.get_order("o_front") is None
    assert tracker.get_order("o_deep") is not None


def test_rate_limit_token_bucket():
    """Verify rate limiter prevents API rate exhaustion."""
    bucket = RateLimitTokenBucket(rate=2.0, capacity=2.0)
    assert bucket.consume(1.0) is True
    assert bucket.consume(1.0) is True
    assert bucket.consume(1.0) is False  # Exhausted


# ============================================================================
# 4. VirtualQueueOrderBookEngine Integration & Performance Benchmark
# ============================================================================

def test_engine_end_to_end_integration():
    """Verify full workflow: book updates -> queue tracking -> toxic shock defense."""
    engine = VirtualQueueOrderBookEngine(asset_id="btc_updown_5m")

    # Ingest book snapshot
    engine.process_clob_frame({
        "event_type": "book",
        "bids": [{"price": "0.48", "size": "1000"}, {"price": "0.47", "size": "2000"}],
        "asks": [{"price": "0.52", "size": "800"}, {"price": "0.53", "size": "1500"}],
    })
    assert engine.book.best_bid == 0.48
    assert engine.book.best_ask == 0.52

    # Place bot orders
    ord_touch = engine.tracker.track_order("touch_bid", "BUY", 0.48, 100.0, already_in_book=True)
    ord_deep = engine.tracker.track_order("deep_bid", "BUY", 0.47, 100.0, already_in_book=True)

    assert ord_touch.queue_ahead == 900.0
    assert ord_deep.queue_ahead == 1900.0

    # Simulate trade hitting 0.48 bids for 850 shares
    engine.process_clob_frame({
        "event_type": "trade",
        "side": "SELL",  # Taker SELL hits maker bids
        "price": "0.48",
        "size": "850",
    })
    # Touch order queue ahead reduced to 50 shares! (Direct front of queue)
    assert ord_touch.queue_ahead == 50.0

    # Toxic shock occurs on Binance: spot price drops sharply
    # Asks are safe, bids are threatened by adverse selection!
    mock_pool = MagicMock()
    mock_pool.cancel_order.return_value = MagicMock(success=True)

    results = engine.handle_toxic_price_shock(
        threatened_side="BUY",
        toxic_shock_magnitude=8.0,
        estimated_sweep_size=500.0,
        cancellation_pool=mock_pool,
    )

    # Touch order is in front of queue -> Cancelled immediately
    assert results["touch_bid"]["action"] == "CANCEL_SENT"
    # Deep order is protected by 1,900 shares buffer -> Throttled
    assert results["deep_bid"]["action"] == "THROTTLED_BUFFER_PROTECTED"


def test_high_throughput_microbenchmark():
    """Verify FastL2Book processes at least 20,000 updates/sec in pure Python."""
    book = FastL2Book()
    book.apply_snapshot(
        bids=[(0.48 - i * 0.001, 100.0) for i in range(20)],
        asks=[(0.52 + i * 0.001, 100.0) for i in range(20)],
    )

    t0 = time.perf_counter()
    n_updates = 10_000
    for i in range(n_updates):
        p = 0.48 if i % 2 == 0 else 0.52
        side = "BUY" if i % 2 == 0 else "SELL"
        book.apply_delta(side, p, 100.0 + (i % 50))

    elapsed = time.perf_counter() - t0
    rate = n_updates / elapsed
    # Pure Python should comfortably process > 50,000 updates/sec
    assert rate > 20_000, f"Expected >20k updates/sec, achieved {rate:.0f}"
    assert elapsed < 1.0  # Runs well under 1 second


def test_multiple_orders_at_same_price_fifo():
    """Verify FIFO ordering when multiple bot orders rest at the same price."""
    tracker = QueuePositionTracker()
    o1 = tracker.track_order("o1", "BUY", 0.50, 100.0, initial_depth_ahead=200.0)
    o2 = tracker.track_order("o2", "BUY", 0.50, 100.0, initial_depth_ahead=300.0)

    # Trade 1: 250 shares.
    # Consumes 200 ahead of o1 + fills 50 of o1.
    # For o2: consumed 250 of its 300 ahead -> 50 ahead remains.
    fills = tracker.on_trade("BUY", 0.50, 250.0)
    assert len(fills) == 1
    assert fills[0][0].order_id == "o1"
    assert fills[0][1] == 50.0

    assert o1.remaining_size == 50.0
    assert o1.queue_ahead == 0.0

    assert o2.remaining_size == 100.0
    assert o2.queue_ahead == 50.0


def test_queue_tracker_cancellation_optimistic():
    """Verify optimistic cancellation attribution (orders ahead cancel first)."""
    tracker = QueuePositionTracker()
    order = tracker.track_order(
        order_id="ord_opt",
        side="BUY",
        price=0.50,
        size=100.0,
        initial_depth_ahead=300.0,
        cancellation_model=CancellationModel.OPTIMISTIC,
    )
    # Expand tail by 200
    tracker.on_level_delta("BUY", 0.50, 400.0, 600.0)
    assert order.queue_ahead == 300.0
    assert order.queue_behind == 200.0

    # 150 shares cancelled: should come entirely from Q_ahead in optimistic mode
    tracker.on_level_delta("BUY", 0.50, 600.0, 450.0)
    assert order.queue_ahead == 150.0
    assert order.queue_behind == 200.0


def test_fast_l2_book_crossed_detection():
    """Verify crossed book detection."""
    book = FastL2Book()
    book.apply_snapshot(bids=[(0.48, 100.0)], asks=[(0.52, 100.0)])
    assert not book.is_crossed

    # Bid crosses ask
    book.apply_delta("BUY", 0.53, 50.0)
    assert book.is_crossed


def test_rate_limited_monitor_execution():
    """Verify intermediate monitor orders get deferred when rate limiter is exhausted."""
    tracker = QueuePositionTracker()
    o1 = tracker.track_order("m1", "BUY", 0.48, 100.0, initial_depth_ahead=500.0)
    o2 = tracker.track_order("m2", "BUY", 0.47, 100.0, initial_depth_ahead=600.0)

    # Limiter only has 1 token
    limiter = RateLimitTokenBucket(rate=0.1, capacity=1.0)
    policy = QueueAwareCancellationPolicy(
        tracker=tracker,
        rate_limiter=limiter,
        config=QueueCancellationConfig(front_queue_threshold=100.0),
    )

    decisions = policy.evaluate_threatened_orders("BUY", toxic_shock_magnitude=5.0)
    assert len(decisions) == 2
    assert decisions[0].urgency == CancellationUrgency.MONITOR
    assert decisions[1].urgency == CancellationUrgency.MONITOR

    mock_pool = MagicMock()
    mock_pool.cancel_order.return_value = MagicMock(success=True)

    results = policy.execute_cancellations(decisions, cancellation_pool=mock_pool)
    # First consumed the 1 token
    assert results["m1"]["action"] == "CANCEL_SENT"
    # Second was rate limited
    assert results["m2"]["action"] == "RATE_LIMITED_DEFERRED"

