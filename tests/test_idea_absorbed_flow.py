"""A level that refills was absorbed; a level that only shrinks is informed."""

from __future__ import annotations

import importlib

from polymarketbot.strategy.base import BookContext, DecisionContext

Module = importlib.import_module("ideas.micro-absorbed-flow.module")


def _ctx(bid: float, ask: float):
    return DecisionContext(
        book=BookContext("c", "a", "YES", 0.5, 0.49, 0.51, 0.01, 2.0, 0.0),
        seconds_elapsed=1.0, inventory_shares=0.0, max_inventory_shares=100.0,
        bid_queue_ratio=0.0, ask_queue_ratio=0.0,
        bid_size_at_level=bid, ask_size_at_level=ask,
    )


def test_an_emptying_bid_is_pulled() -> None:
    m = Module.AbsorbedFlow()
    for size in (100.0, 60.0, 30.0, 10.0):
        out = m.decide(_ctx(size, 100.0))
    assert out.bid == "PULL"
    assert out.ask == "QUOTE"


def test_a_refilled_bid_stays_quoted() -> None:
    m = Module.AbsorbedFlow()
    m.decide(_ctx(100.0, 100.0))
    m.decide(_ctx(40.0, 100.0))   # eaten
    out = m.decide(_ctx(95.0, 100.0))  # came back
    assert out.bid == "QUOTE"


def test_a_thin_level_that_never_moves_is_not_a_signal() -> None:
    m = Module.AbsorbedFlow()
    for _ in range(5):
        out = m.decide(_ctx(20.0, 20.0))
    assert (out.bid, out.ask) == ("QUOTE", "QUOTE")
