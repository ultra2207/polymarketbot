"""Contract tests for L3 queue-position reconstruction from L2 data.

Every test is deterministic: no network, no sleeps, no wall clock. Timestamps
are synthetic floats so the assertions describe the bookkeeping rules rather
than the machine they run on.
"""

from __future__ import annotations

import json
import random

import pytest

from polymarketbot.risk.contracts import QueueProgress
from polymarketbot.risk.queue_position import QueuePositionTracker

TOKEN = "token-1"


def test_entry_snapshots_level_size_ahead() -> None:
    tracker = QueuePositionTracker()

    entry = tracker.on_entry(
        "o1",
        TOKEN,
        "BUY",
        0.50,
        40.0,
        1_000.0,
        [(0.49, 900.0), (0.50, 250.0)],
        [(0.51, 700.0)],
    )
    assert entry.order_id == "o1"
    assert entry.token_id == TOKEN
    assert entry.side == "BUY"
    assert entry.price == 0.50
    assert entry.size == 40.0
    assert entry.entered_ts == 1_000.0
    assert entry.ahead_at_entry == 250.0

    # SELL orders read the ask side of the book.
    sell = tracker.on_entry(
        "o2",
        TOKEN,
        "SELL",
        0.51,
        10.0,
        1_001.0,
        [(0.49, 900.0)],
        [(0.51, 800.0), (0.52, 100.0)],
    )
    assert sell.ahead_at_entry == 800.0

    # An absent level means nothing rests ahead of us.
    absent = tracker.on_entry(
        "o3", "token-2", "BUY", 0.40, 10.0, 1_002.0, [(0.39, 5.0)], []
    )
    assert absent.ahead_at_entry == 0.0


def test_new_size_at_level_does_not_increase_ahead() -> None:
    tracker = QueuePositionTracker()
    tracker.on_entry("o1", TOKEN, "BUY", 0.50, 40.0, 1.0, [(0.50, 250.0)], [])

    [progress] = tracker.on_book(TOKEN, [(0.50, 900.0)], [], 2.0)

    assert progress.ahead == 250.0
    assert progress.initial_ahead == 250.0
    assert progress.level_size == 900.0
    assert progress.at_front is False
    # L2 reconstruction is an inference, never a venue-reported position.
    assert progress.queue_position_inferred is True


def test_book_update_shrinking_level_reduces_ahead() -> None:
    tracker = QueuePositionTracker()
    tracker.on_entry("o1", TOKEN, "BUY", 0.50, 40.0, 1.0, [(0.50, 250.0)], [])

    [first] = tracker.on_book(TOKEN, [(0.50, 60.0)], [], 2.0)
    assert first.ahead == 20.0  # observed 60 - own 40

    # Growth beyond the previous level is clamped, never credited.
    [second] = tracker.on_book(TOKEN, [(0.50, 90.0)], [], 3.0)
    assert second.ahead == 20.0

    # A level smaller than our own order cannot imply negative size ahead.
    [third] = tracker.on_book(TOKEN, [(0.50, 10.0)], [], 4.0)
    assert third.ahead == 0.0
    assert third.at_front is True
    assert third.consumed_fraction == 1.0


def test_print_at_price_consumes_ahead_and_accumulates_traded() -> None:
    tracker = QueuePositionTracker()
    tracker.on_entry("o1", TOKEN, "BUY", 0.50, 10.0, 1.0, [(0.50, 100.0)], [])

    [first] = tracker.on_trade(TOKEN, 0.50, 35.0, "SELL", 2.0)
    assert first.ahead == 65.0
    assert first.traded_at_level == 35.0
    assert first.filled is False

    [second] = tracker.on_trade(TOKEN, 0.50, 15.0, "SELL", 3.0)
    assert second.ahead == 50.0
    assert second.traded_at_level == 50.0
    assert second.filled is False

    # A third-party print above our bid touches nothing.
    assert tracker.on_trade(TOKEN, 0.52, 10.0, "BUY", 4.0) == []
    progress = tracker.progress("o1")
    assert progress is not None
    assert progress.ahead == 50.0
    assert progress.traded_at_level == 50.0


def test_through_print_marks_filled() -> None:
    tracker = QueuePositionTracker()
    tracker.on_entry("buy", TOKEN, "BUY", 0.50, 10.0, 1.0, [(0.50, 100.0)], [])

    # Above the bid: not through, not at our level.
    assert tracker.on_trade(TOKEN, 0.51, 5.0, "BUY", 2.0) == []
    buy = tracker.progress("buy")
    assert buy is not None and buy.filled is False

    # At the bid: consumes queue but is not a through-print.
    [at_level] = tracker.on_trade(TOKEN, 0.50, 5.0, "SELL", 3.0)
    assert at_level.filled is False
    assert at_level.ahead == 95.0

    # Strictly below the bid: the order traded through.
    [through] = tracker.on_trade(TOKEN, 0.49, 5.0, "SELL", 4.0)
    assert through.filled is True
    assert through.ahead == 95.0

    # Mirror image for a resting ask: filled only strictly above.
    tracker.on_entry("sell", TOKEN, "SELL", 0.60, 10.0, 5.0, [], [(0.60, 50.0)])
    assert tracker.on_trade(TOKEN, 0.59, 5.0, "BUY", 6.0) == []
    sell = tracker.progress("sell")
    assert sell is not None and sell.filled is False
    [sell_through] = tracker.on_trade(TOKEN, 0.61, 5.0, "BUY", 7.0)
    assert sell_through.filled is True


def test_cumulative_prints_beyond_initial_ahead_mark_filled() -> None:
    tracker = QueuePositionTracker()
    tracker.on_entry("o1", TOKEN, "BUY", 0.50, 10.0, 1.0, [(0.50, 50.0)], [])

    [first] = tracker.on_trade(TOKEN, 0.50, 20.0, "SELL", 2.0)
    assert first.traded_at_level == 20.0
    assert first.filled is False

    [second] = tracker.on_trade(TOKEN, 0.50, 20.0, "SELL", 3.0)
    assert second.traded_at_level == 40.0
    assert second.filled is False

    [third] = tracker.on_trade(TOKEN, 0.50, 20.0, "SELL", 4.0)
    assert third.traded_at_level == 60.0 > third.initial_ahead
    assert third.filled is True
    assert third.at_front is True


def test_level_disappearing_yields_at_front() -> None:
    tracker = QueuePositionTracker()
    tracker.on_entry("o1", TOKEN, "BUY", 0.50, 40.0, 1.0, [(0.50, 200.0)], [])

    [progress] = tracker.on_book(TOKEN, [], [], 2.0)

    assert progress.ahead == 0.0
    assert progress.at_front is True
    assert progress.consumed_fraction == 1.0
    # Only our own order can still rest at that price.
    assert progress.level_size == 40.0


def test_orders_at_different_prices_tracked_independently() -> None:
    tracker = QueuePositionTracker()
    entry_a = tracker.on_entry(
        "a", TOKEN, "BUY", 0.50, 10.0, 1.0, [(0.50, 100.0)], []
    )
    entry_b = tracker.on_entry(
        "b", TOKEN, "BUY", 0.48, 10.0, 1.0, [(0.48, 300.0)], []
    )
    assert entry_a.ahead_at_entry == 100.0
    assert entry_b.ahead_at_entry == 300.0

    book = {
        progress.order_id: progress
        for progress in tracker.on_book(
            TOKEN, [(0.50, 60.0), (0.48, 800.0)], [], 2.0
        )
    }
    assert set(book) == {"a", "b"}
    assert book["a"].ahead == 50.0
    assert book["b"].ahead == 300.0  # growth at 0.48 is behind b

    # 0.49 is through a's bid but above b's bid.
    trades = {
        progress.order_id: progress
        for progress in tracker.on_trade(TOKEN, 0.49, 5.0, "SELL", 3.0)
    }
    assert set(trades) == {"a"}
    assert trades["a"].filled is True
    assert trades["a"].ahead == 50.0

    b = tracker.progress("b")
    assert b is not None
    assert b.filled is False
    assert b.ahead == 300.0
    assert b.traded_at_level == 0.0

    # 0.47 is through both bids.
    [b_through] = [
        progress
        for progress in tracker.on_trade(TOKEN, 0.47, 5.0, "SELL", 4.0)
        if progress.order_id == "b"
    ]
    assert b_through.filled is True

    assert {progress.order_id for progress in tracker.for_token(TOKEN)} == {"a", "b"}


def test_drop_stops_tracking() -> None:
    tracker = QueuePositionTracker()
    tracker.on_entry("o1", TOKEN, "BUY", 0.50, 10.0, 1.0, [(0.50, 10.0)], [])
    tracker.on_entry("o2", "token-2", "SELL", 0.60, 10.0, 1.0, [], [(0.60, 10.0)])

    assert isinstance(tracker.progress("o1"), QueueProgress)

    tracker.drop(TOKEN, "o1")
    assert tracker.progress("o1") is None
    assert tracker.for_token(TOKEN) == []
    assert tracker.snapshot()["queue_tokens"] == {"token-2": ["o2"]}

    # A drop against the wrong token is a no-op.
    tracker.drop(TOKEN, "o2")
    assert tracker.progress("o2") is not None

    tracker.drop("token-2", "o2")
    assert tracker.progress("o2") is None
    assert tracker.for_token("token-2") == []

    tracker.clear()
    assert tracker.snapshot() == {"queue_orders": [], "queue_tokens": {}}


def test_reregistration_moves_order_to_its_new_token() -> None:
    tracker = QueuePositionTracker()
    tracker.on_entry("o1", "token-a", "BUY", 0.50, 10.0, 1.0, [(0.50, 100.0)], [])
    tracker.on_entry("o1", "token-b", "BUY", 0.40, 10.0, 2.0, [(0.40, 20.0)], [])

    assert tracker.for_token("token-a") == []
    [progress] = tracker.for_token("token-b")
    assert progress.initial_ahead == 20.0
    assert progress.ahead == 20.0

    # The old token's book no longer touches the order.
    assert tracker.on_book("token-a", [(0.40, 900.0)], [], 3.0) == []
    still = tracker.progress("o1")
    assert still is not None and still.ahead == 20.0


def test_seeded_book_updates_never_increase_ahead() -> None:
    rng = random.Random(20261111)
    tracker = QueuePositionTracker()
    prices = [round(0.50 - index * 0.01, 2) for index in range(6)]

    previous: dict[str, float] = {}
    for index, price in enumerate(prices):
        order_id = f"o{index}"
        entry = tracker.on_entry(
            order_id,
            TOKEN,
            "BUY",
            price,
            10.0,
            float(index),
            [(price, 500.0)],
            [],
        )
        previous[order_id] = entry.ahead_at_entry

    for step in range(400):
        levels: list[tuple[float, float]] = []
        for price in prices:
            if rng.random() < 0.15:
                continue  # level vanishes from this snapshot
            levels.append((price, rng.uniform(0.0, 1_200.0)))
        rng.shuffle(levels)

        results = tracker.on_book(TOKEN, levels, [], float(step))
        assert len(results) == len(prices)
        for progress in results:
            assert progress.ahead >= 0.0
            assert progress.ahead <= previous[progress.order_id]
            previous[progress.order_id] = progress.ahead

    assert len(previous) == len(prices)
    for index in range(len(prices)):
        assert tracker.progress(f"o{index}") is not None


def test_zero_size_and_nan_levels_do_not_raise() -> None:
    tracker = QueuePositionTracker()
    tracker.on_entry("o1", TOKEN, "BUY", 0.50, 10.0, 1.0, [(0.50, 100.0)], [])

    [zero] = tracker.on_book(TOKEN, [(0.50, 0.0)], [], 2.0)
    assert zero.ahead == 0.0
    assert zero.level_size == 0.0

    [nan] = tracker.on_book(
        TOKEN,
        [(0.50, float("nan")), (float("nan"), 500.0), ("bad", 1.0)],
        [],
        3.0,
    )
    # Every malformed level is skipped, so the level reads as absent.
    assert nan.ahead == 0.0
    assert nan.level_size == 10.0
    assert nan.updated_ts == 3.0

    [empty] = tracker.on_book(TOKEN, [], [], 4.0)
    assert empty.ahead == 0.0
    assert empty.level_size == 10.0

    assert tracker.on_trade(TOKEN, float("nan"), 5.0, "SELL", 5.0) == []
    assert tracker.on_trade(TOKEN, 0.50, float("nan"), "SELL", 6.0) == []
    progress = tracker.progress("o1")
    assert progress is not None
    assert progress.traded_at_level == 0.0


def test_snapshot_is_json_safe() -> None:
    tracker = QueuePositionTracker()
    tracker.on_entry("o1", TOKEN, "BUY", 0.50, 10.0, 1.0, [(0.50, 100.0)], [])
    tracker.on_trade(TOKEN, 0.50, 5.0, "SELL", 2.0)

    payload = tracker.snapshot()
    encoded = json.dumps(payload, allow_nan=False)
    decoded = json.loads(encoded)

    assert decoded["queue_tokens"] == {TOKEN: ["o1"]}
    [record] = decoded["queue_orders"]
    assert record["order_id"] == "o1"
    assert record["side"] == "BUY"
    assert record["ahead"] == 95.0
    assert record["traded_at_level"] == 5.0
    assert record["filled"] is False
    assert record["at_front"] is False
    assert record["queue_position_inferred"] is True
    assert record["consumed_fraction"] == pytest.approx(0.05)


def test_invalid_entry_inputs_raise() -> None:
    tracker = QueuePositionTracker()

    with pytest.raises(ValueError):
        tracker.on_entry("o1", TOKEN, "HOLD", 0.50, 10.0, 1.0, [], [])
    with pytest.raises(ValueError):
        tracker.on_entry("o1", TOKEN, "BUY", float("nan"), 10.0, 1.0, [], [])
    with pytest.raises(ValueError):
        tracker.on_entry("o1", TOKEN, "BUY", 0.50, float("inf"), 1.0, [], [])
    with pytest.raises(ValueError):
        tracker.on_entry("o1", TOKEN, "BUY", 0.50, 10.0, float("nan"), [], [])

    assert tracker.progress("o1") is None
