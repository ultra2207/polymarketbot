"""Book integrity and off-loop dispatch tests for the live CLOB feed.

Covers audit items S2 (engine work on the websocket event loop) and S10
(`price_change` collapsed into a fake one-level book with size 100).
No network is used: payloads are fed straight into `_process_ws_payload`.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import MagicMock, patch

from polymarketbot.strategy.venue.feed import (
    EVENT_QUEUE_MAXSIZE,
    LivePolymarketFeed,
    MarketTrade,
    OrderBookSnapshot,
    _DispatchQueue,
    _EventQueue,
    build_book_snapshot,
)


def _levels(snapshot: OrderBookSnapshot, side: str) -> list[tuple[float, float]]:
    levels = snapshot.bids if side == "bids" else snapshot.asks
    return [(level.price, level.size) for level in levels]


def _synthetic(asset_id: str, best_bid: float) -> OrderBookSnapshot:
    return build_book_snapshot(
        asset_id,
        [{"price": best_bid, "size": 10.0}],
        [{"price": round(best_bid + 0.02, 4), "size": 10.0}],
    )


def _book_msg(asset_id: str, bids: list[dict], asks: list[dict]) -> str:
    return json.dumps(
        {
            "event_type": "book",
            "asset_id": asset_id,
            "bids": bids,
            "asks": asks,
        }
    )


# ---------------------------------------------------------------------------
# S10: real depth from book + price_change deltas
# ---------------------------------------------------------------------------
def test_price_change_updates_and_removes_levels() -> None:
    async def _test() -> None:
        feed = LivePolymarketFeed()
        received: list[OrderBookSnapshot] = []
        feed.add_book_listener(received.append)

        await feed._process_ws_payload(
            _book_msg(
                "tok",
                [{"price": "0.40", "size": "500"}, {"price": "0.39", "size": "300"}],
                [{"price": "0.44", "size": "600"}, {"price": "0.45", "size": "700"}],
            )
        )
        assert received[-1].best_bid == 0.40
        assert received[-1].best_ask == 0.44

        # size is the new aggregate size at that level; 0 removes the level.
        await feed._process_ws_payload(
            json.dumps(
                {
                    "event_type": "price_change",
                    "price_changes": [
                        {"asset_id": "tok", "price": "0.41", "size": "250", "side": "BUY"},
                        {"asset_id": "tok", "price": "0.44", "size": "0", "side": "SELL"},
                        {"asset_id": "tok", "price": "0.45", "size": "900", "side": "SELL"},
                    ],
                }
            )
        )

        snapshot = received[-1]
        assert snapshot.asset_id == "tok"
        assert snapshot.best_bid == 0.41
        assert snapshot.best_ask == 0.45
        assert _levels(snapshot, "bids") == [(0.41, 250.0), (0.40, 500.0), (0.39, 300.0)]
        assert _levels(snapshot, "asks") == [(0.45, 900.0)]
        # The old code fabricated the top level with size 100.
        assert all(size != 100.0 for _, size in _levels(snapshot, "bids") + _levels(snapshot, "asks"))

    asyncio.run(_test())


def test_price_change_reads_fields_from_item_and_payload() -> None:
    async def _test() -> None:
        feed = LivePolymarketFeed()
        received: list[OrderBookSnapshot] = []
        feed.add_book_listener(received.append)

        await feed._process_ws_payload(
            _book_msg(
                "tok",
                [{"price": "0.40", "size": "500"}],
                [{"price": "0.44", "size": "600"}],
            )
        )

        # Message and change are both wrapped in `payload`.
        await feed._process_ws_payload(
            json.dumps(
                {
                    "event_type": "price_change",
                    "payload": {
                        "price_changes": [
                            {
                                "payload": {
                                    "asset_id": "tok",
                                    "price": "0.42",
                                    "size": "321",
                                    "side": "BUY",
                                }
                            }
                        ]
                    },
                }
            )
        )

        snapshot = received[-1]
        assert snapshot.best_bid == 0.42
        assert snapshot.bids[0].price == 0.42
        assert snapshot.bids[0].size == 321.0
        # Untouched levels survive the delta.
        assert (0.40, 500.0) in _levels(snapshot, "bids")

    asyncio.run(_test())


def test_best_bid_ask_moves_top_without_erasing_depth() -> None:
    async def _test() -> None:
        feed = LivePolymarketFeed()
        received: list[OrderBookSnapshot] = []
        feed.add_book_listener(received.append)

        await feed._process_ws_payload(
            _book_msg(
                "tok",
                [
                    {"price": "0.40", "size": "500"},
                    {"price": "0.39", "size": "300"},
                    {"price": "0.38", "size": "200"},
                ],
                [
                    {"price": "0.44", "size": "600"},
                    {"price": "0.45", "size": "700"},
                    {"price": "0.46", "size": "800"},
                ],
            )
        )

        await feed._process_ws_payload(
            json.dumps(
                {
                    "event_type": "best_bid_ask",
                    "asset_id": "tok",
                    "best_bid": "0.42",
                    "best_ask": "0.43",
                }
            )
        )

        snapshot = received[-1]
        assert snapshot.best_bid == 0.42
        assert snapshot.best_ask == 0.43
        assert [price for price, _ in _levels(snapshot, "bids")] == [0.42, 0.40, 0.39, 0.38]
        assert [price for price, _ in _levels(snapshot, "asks")] == [0.43, 0.44, 0.45, 0.46]
        # A top-of-book price has no size in the frame: it is carried as unknown
        # (0.0), never as a synthetic 100-share level.
        assert snapshot.bids[0].size == 0.0
        assert snapshot.asks[0].size == 0.0
        assert snapshot.bids[1].size == 500.0
        assert snapshot.asks[1].size == 600.0

        # The next delta sizes the asserted top level for real.
        await feed._process_ws_payload(
            json.dumps(
                {
                    "event_type": "price_change",
                    "price_changes": [
                        {"asset_id": "tok", "price": "0.42", "size": "350", "side": "BUY"}
                    ],
                }
            )
        )
        snapshot = received[-1]
        assert snapshot.best_bid == 0.42
        assert snapshot.bids[0].size == 350.0

    asyncio.run(_test())


def test_reconnect_clears_maintained_book() -> None:
    async def _test() -> None:
        feed = LivePolymarketFeed()
        await feed._process_ws_payload(
            _book_msg(
                "tok",
                [{"price": "0.40", "size": "500"}],
                [{"price": "0.44", "size": "600"}],
            )
        )
        # The maintained-book reader is the only observable of a cleared base:
        # listener callbacks fire only on updates, not on reconnect clears.
        assert feed._snapshot_from_book("tok").best_bid == 0.40

        feed._clear_books()
        cleared = feed._snapshot_from_book("tok")
        assert cleared.bids == []
        assert cleared.asks == []

        # A delta after the gap is applied to an empty base, not a stale one.
        await feed._process_ws_payload(
            json.dumps(
                {
                    "event_type": "price_change",
                    "price_changes": [
                        {"asset_id": "tok", "price": "0.41", "size": "250", "side": "BUY"}
                    ],
                }
            )
        )
        snapshot = feed._snapshot_from_book("tok")
        assert _levels(snapshot, "bids") == [(0.41, 250.0)]
        assert snapshot.asks == []

    asyncio.run(_test())


def test_rest_snapshot_replaces_maintained_book() -> None:
    feed = LivePolymarketFeed()
    asyncio.run(
        feed._process_ws_payload(
            _book_msg(
                "tok",
                [{"price": "0.10", "size": "5"}],
                [{"price": "0.90", "size": "5"}],
            )
        )
    )

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "bids": [{"price": "0.45", "size": "100"}, {"price": "0.44", "size": "250"}],
        "asks": [{"price": "0.55", "size": "300"}],
        "tick_size": "0.01",
        "hash": "0xrest",
    }

    try:
        with patch.object(feed._ensure_session(), "get", return_value=response):
            snapshot = feed.get_book_sync("tok")
        assert snapshot is not None
        assert snapshot.source == "rest"
        assert snapshot.best_bid == 0.45

        # Deltas after the REST refresh apply to the REST base, not the old WS book.
        asyncio.run(
            feed._process_ws_payload(
                json.dumps(
                    {
                        "event_type": "price_change",
                        "price_changes": [
                            {"asset_id": "tok", "price": "0.46", "size": "123", "side": "BUY"}
                        ],
                    }
                )
            )
        )
        state = feed._snapshot_from_book("tok")
        assert state.best_bid == 0.46
        assert state.bids[0].size == 123.0
        assert all(price != 0.10 for price, _ in _levels(state, "bids"))
    finally:
        asyncio.run(feed.stop())


# ---------------------------------------------------------------------------
# S2: off-loop, ordered, coalesced dispatch
# ---------------------------------------------------------------------------
def test_listeners_run_off_loop_in_order() -> None:
    async def _test() -> None:
        feed = LivePolymarketFeed()
        loop_thread = threading.get_ident()
        seen: list[tuple[str, int]] = []
        delivered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_book(snapshot: OrderBookSnapshot) -> None:
            seen.append((snapshot.asset_id, threading.get_ident()))
            if len(seen) >= 3:
                loop.call_soon_threadsafe(delivered.set)

        def on_trade(trade: MarketTrade) -> None:
            seen.append((trade.asset_id, threading.get_ident()))
            if len(seen) >= 3:
                loop.call_soon_threadsafe(delivered.set)

        feed.add_book_listener(on_book)
        feed.add_trade_listener(on_trade)
        feed._ensure_dispatch_worker()

        try:
            await feed._process_ws_payload(
                _book_msg("A", [{"price": "0.40", "size": "500"}], [{"price": "0.44", "size": "600"}])
            )
            await feed._process_ws_payload(
                json.dumps(
                    {
                        "event_type": "last_trade_price",
                        "asset_id": "T",
                        "price": "0.50",
                        "size": "10",
                        "side": "BUY",
                    }
                )
            )
            await feed._process_ws_payload(
                _book_msg("C", [{"price": "0.30", "size": "500"}], [{"price": "0.34", "size": "600"}])
            )

            await asyncio.wait_for(delivered.wait(), timeout=2.0)
        finally:
            await feed.stop()

        assert [asset for asset, _ in seen] == ["A", "T", "C"]
        assert all(thread != loop_thread for _, thread in seen), "callbacks must not run on the loop thread"

    asyncio.run(_test())


def test_book_coalescing_keeps_latest_per_asset() -> None:
    async def _test() -> None:
        feed = LivePolymarketFeed()
        loop = asyncio.get_running_loop()
        gate = threading.Event()
        started = asyncio.Event()
        received: list[OrderBookSnapshot] = []

        def on_book(snapshot: OrderBookSnapshot) -> None:
            received.append(snapshot)
            if snapshot.asset_id == "blocker":
                loop.call_soon_threadsafe(started.set)
                gate.wait(2.0)

        feed.add_book_listener(on_book)
        feed._ensure_dispatch_worker()

        try:
            await feed._process_ws_payload(
                _book_msg("blocker", [{"price": "0.40", "size": "1"}], [{"price": "0.44", "size": "1"}])
            )
            await asyncio.wait_for(started.wait(), timeout=2.0)

            # The single worker is blocked inside the first callback, so these
            # three books queue up and must coalesce to the newest one.
            for price in ("0.41", "0.42", "0.43"):
                await feed._process_ws_payload(
                    _book_msg("tok", [{"price": price, "size": "10"}], [{"price": "0.99", "size": "10"}])
                )

            # Queue depth is the only direct observable of the coalescing worker;
            # the public drop counter below proves the other two were dropped.
            assert feed._dispatch_queue.qsize() == 1
            assert feed.dispatch_dropped_count == 2

            gate.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if any(snapshot.asset_id == "tok" for snapshot in received):
                    break
                await asyncio.sleep(0.01)
        finally:
            gate.set()
            await feed.stop()

        tok_books = [snapshot for snapshot in received if snapshot.asset_id == "tok"]
        assert len(tok_books) == 1
        assert tok_books[0].best_bid == 0.43

    asyncio.run(_test())


def test_dispatch_queue_bounds_and_evicts_oldest_book() -> None:
    queue = _DispatchQueue(maxsize=2)
    first = _synthetic("A", 0.40)
    second = _synthetic("A", 0.41)
    trade = MarketTrade(asset_id="T", price=0.50, size=10.0, side="BUY")
    other_trade = MarketTrade(asset_id="T", price=0.51, size=10.0, side="SELL")

    assert queue.offer("book", first) is True
    assert queue.offer("book", second) is True  # coalesces `first`
    assert queue.qsize() == 1
    assert queue.dropped == 1

    assert queue.offer("trade", trade) is True
    assert queue.qsize() == 2

    newest = _synthetic("B", 0.60)
    assert queue.offer("book", newest) is True  # evicts the queued book, keeps the trade
    assert queue.qsize() == 2
    assert queue.dropped == 2

    kind, payload = queue.get_nowait()
    assert kind == "trade" and payload is trade
    kind, payload = queue.get_nowait()
    assert kind == "book" and payload is newest

    # Full of trades: nothing to evict, so the incoming trade is refused.
    assert queue.offer("trade", trade) is True
    assert queue.offer("trade", other_trade) is True
    assert queue.offer("trade", trade) is False
    assert queue.dropped == 3


def test_dispatch_queue_stays_bounded_under_slow_listener() -> None:
    queue = _DispatchQueue(maxsize=4)
    for index in range(10):
        assert queue.offer("book", _synthetic(f"asset-{index}", 0.40)) is True

    assert queue.qsize() == 4
    assert queue.dropped == 6
    remaining = [queue.get_nowait()[1].asset_id for _ in range(4)]
    assert remaining == [f"asset-{index}" for index in range(6, 10)]


def test_event_queue_is_bounded_and_drops_oldest_books_first() -> None:
    queue = _EventQueue(maxsize=3)
    book_a = _synthetic("A", 0.40)
    book_b = _synthetic("B", 0.41)
    book_c = _synthetic("C", 0.42)
    first_trade = MarketTrade(asset_id="T1", price=0.50, size=1.0, side="BUY")
    second_trade = MarketTrade(asset_id="T2", price=0.51, size=1.0, side="SELL")

    assert queue.offer("book", book_a) is True
    assert queue.offer("trade", first_trade) is True
    assert queue.offer("book", book_b) is True
    assert queue.qsize() == 3

    # Full: the oldest book is evicted, the trade is kept.
    assert queue.offer("book", book_c) is True
    assert queue.qsize() == 3
    assert queue.dropped == 1
    remaining = [queue.get_nowait() for _ in range(3)]
    assert [(kind, payload.asset_id) for kind, payload in remaining] == [
        ("trade", "T1"),
        ("book", "B"),
        ("book", "C"),
    ]

    # Full of trades only: the oldest trade is evicted so polling consumers
    # keep seeing a recent stream instead of a stalled one.
    trade_only = _EventQueue(maxsize=2)
    assert trade_only.offer("trade", first_trade) is True
    assert trade_only.offer("trade", second_trade) is True
    assert trade_only.offer("trade", first_trade) is True
    assert trade_only.qsize() == 2
    assert trade_only.dropped == 1
    assert [trade_only.get_nowait()[1].asset_id for _ in range(2)] == ["T2", "T1"]


def test_feed_event_queue_is_bounded_and_counts_drops() -> None:
    async def _test() -> None:
        feed = LivePolymarketFeed()
        # The configured bound has no public accessor; the drop counter and the
        # drained stream below prove the queue honours it.
        assert feed._event_queue.maxsize == EVENT_QUEUE_MAXSIZE

        # Shrink the stream so the bound is reachable without thousands of frames.
        feed._event_queue = _EventQueue(maxsize=3)
        for index in range(5):
            await feed._process_ws_payload(
                json.dumps(
                    {
                        "event_type": "last_trade_price",
                        "asset_id": "T",
                        "price": "0.50",
                        "size": str(index + 1),
                        "side": "BUY",
                    }
                )
            )

        # Draining exactly three events below proves the bound held.
        assert feed.event_dropped_count == 2
        # get_next_event's signature is unchanged and yields the newest events.
        sizes = []
        for _ in range(3):
            kind, payload = await feed.get_next_event(timeout=0.5)
            assert kind == "trade"
            sizes.append(payload.size)
        assert sizes == [3.0, 4.0, 5.0]

    asyncio.run(_test())


def test_dispatch_refusal_still_flushes_event_queue() -> None:
    async def _test() -> None:
        feed = LivePolymarketFeed()
        feed._dispatch_queue = _DispatchQueue(maxsize=1)
        loop = asyncio.get_running_loop()
        gate = threading.Event()
        started = asyncio.Event()
        delivered: list[str] = []

        def on_book(snapshot: OrderBookSnapshot) -> None:
            delivered.append(snapshot.asset_id)
            if snapshot.asset_id == "blocker":
                loop.call_soon_threadsafe(started.set)
                gate.wait(2.0)

        feed.add_book_listener(on_book)
        feed._ensure_dispatch_worker()

        try:
            await feed._process_ws_payload(
                _book_msg("blocker", [{"price": "0.40", "size": "1"}], [{"price": "0.44", "size": "1"}])
            )
            await asyncio.wait_for(started.wait(), timeout=2.0)

            # Occupy the single dispatch slot with a trade: no book is queued,
            # so the next book cannot be delivered to listeners.
            await feed._process_ws_payload(
                json.dumps(
                    {
                        "event_type": "last_trade_price",
                        "asset_id": "T",
                        "price": "0.50",
                        "size": "10",
                        "side": "BUY",
                    }
                )
            )
            # The single queued snapshot is the coalesced delta; the public drop
            # counter below proves the superseded frames were discarded.
            assert feed._dispatch_queue.qsize() == 1

            await feed._process_ws_payload(
                _book_msg("tok", [{"price": "0.41", "size": "10"}], [{"price": "0.99", "size": "10"}])
            )
            assert feed.dispatch_dropped_count == 1

            # Refusal is listener-only: the event stream was flushed before the
            # dispatch attempt and still carries all three events in order.
            events = []
            for _ in range(3):
                kind, payload = await feed.get_next_event(timeout=0.5)
                events.append((kind, payload.asset_id))
            assert events == [("book", "blocker"), ("trade", "T"), ("book", "tok")]
            assert delivered == ["blocker"]
        finally:
            gate.set()
            await feed.stop()

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# Staleness signal used by the runner's disconnect check
# ---------------------------------------------------------------------------
def test_last_message_age_and_is_stale() -> None:
    feed = LivePolymarketFeed()
    assert feed.last_message_age_seconds is None
    assert feed.is_stale(30.0) is True

    asyncio.run(feed._process_ws_payload("PONG"))
    age = feed.last_message_age_seconds
    assert age is not None and age < 1.0
    assert feed.is_stale(30.0) is False

    feed._last_ws_message_time = time.time() - 60.0
    aged = feed.last_message_age_seconds
    assert aged is not None and 59.0 < aged < 61.0
    assert feed.is_stale(30.0) is True

    feed._last_ws_message_time = 0.0
    assert feed.is_stale(30.0) is True
