"""Defence is timed from the burst, not from a constant."""

from __future__ import annotations

import importlib

from polymarketbot.strategy.base import BookContext, DecisionContext, TradeContext

Module = importlib.import_module("ideas.micro-burst-decay-reentry.module")


def _ctx(seconds: float):
    return DecisionContext(
        book=BookContext("c", "a", "YES", 0.5, 0.49, 0.51, 0.01, 2.0, 0.0),
        seconds_elapsed=seconds, inventory_shares=0.0, max_inventory_shares=100.0,
        bid_queue_ratio=0.0, ask_queue_ratio=0.0, bid_size_at_level=100.0, ask_size_at_level=100.0,
    )


def test_a_burst_pulls_both_sides() -> None:
    m = Module.BurstDecayReentry()
    for i in range(Module.BURST_PRINTS):
        m.on_trade(TradeContext("a", 0.5, 10.0, "BUY", 100.0 + i * 0.5))
    out = m.decide(_ctx(103.0))
    assert (out.bid, out.ask) == ("PULL", "PULL")


def test_scattered_prints_do_not_pull() -> None:
    m = Module.BurstDecayReentry()
    for i in range(10):
        m.on_trade(TradeContext("a", 0.5, 10.0, "BUY", 100.0 + i * 30.0))
    out = m.decide(_ctx(400.0))
    assert (out.bid, out.ask) == ("QUOTE", "QUOTE")


def test_the_cooldown_scales_with_the_burst_length() -> None:
    """A burst spread over 2.9 s must defend four times longer than a 0.1 s one."""
    fast, slow = Module.BurstDecayReentry(), Module.BurstDecayReentry()
    for i in range(Module.BURST_PRINTS):
        fast.on_trade(TradeContext("a", 0.5, 1.0, "BUY", 10.0 + i * 0.02))
        slow.on_trade(TradeContext("a", 0.5, 1.0, "BUY", 10.0 + i * 0.9))
    assert slow._until > fast._until
