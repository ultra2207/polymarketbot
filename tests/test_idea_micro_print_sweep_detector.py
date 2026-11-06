"""Sweeps are detected relative to displayed depth, never in absolute shares."""

from __future__ import annotations

import importlib

from polymarketbot.strategy.base import BookContext, DecisionContext, TradeContext

Module = importlib.import_module("ideas.micro-print-sweep-detector.module")


def _ctx(*, bid_size=100.0, ask_size=100.0, mid=0.5):
    return DecisionContext(
        book=BookContext("c", "a", "YES", mid, mid - 0.01, mid + 0.01, 0.01, 2.0, 0.0),
        seconds_elapsed=1.0,
        inventory_shares=0.0,
        max_inventory_shares=100.0,
        bid_queue_ratio=0.0,
        ask_queue_ratio=0.0,
        bid_size_at_level=bid_size,
        ask_size_at_level=ask_size,
    )


def _module():
    return Module.PrintSweepDetector()


def test_a_small_print_on_a_deep_level_changes_nothing() -> None:
    m = _module()
    m.decide(_ctx(bid_size=1000.0))
    m.on_trade(TradeContext("a", 0.49, 50.0, "SELL", 1.0))
    out = m.decide(_ctx())
    assert (out.bid, out.ask) == ("QUOTE", "QUOTE")


def test_the_same_print_is_a_sweep_on_a_shallow_level() -> None:
    """50 shares is noise against 1,000 and a sweep against 60 - the point of
    measuring consumption as a fraction rather than an absolute size."""
    m = _module()
    m.decide(_ctx(bid_size=60.0))
    m.on_trade(TradeContext("a", 0.49, 50.0, "SELL", 1.0))
    out = m.decide(_ctx())
    assert out.bid == "SHRINK" and out.ask == "SHRINK"
    assert out.bid_scale == 0.01


def test_the_defence_expires() -> None:
    m = _module()
    m.decide(_ctx(bid_size=60.0))
    m.on_trade(TradeContext("a", 0.49, 50.0, "SELL", 1.0))
    for _ in range(Module.COOLDOWN):
        m.decide(_ctx())
    out = m.decide(_ctx())
    assert (out.bid, out.ask) == ("QUOTE", "QUOTE")


def test_a_print_without_a_known_level_is_ignored() -> None:
    m = _module()
    m.decide(_ctx(bid_size=0.0))
    m.on_trade(TradeContext("a", 0.49, 500.0, "SELL", 1.0))
    out = m.decide(_ctx())
    assert (out.bid, out.ask) == ("QUOTE", "QUOTE")


def test_pull_replaces_shrink_when_the_mid_follows_through() -> None:
    m = _module()
    m.decide(_ctx(bid_size=60.0))
    m.on_trade(TradeContext("a", 0.49, 50.0, "SELL", 1.0))
    out = m.decide(_ctx(mid=0.48))  # mid fell 2c after the bid was swept
    assert out.bid == "PULL" and out.ask == "SHRINK"
