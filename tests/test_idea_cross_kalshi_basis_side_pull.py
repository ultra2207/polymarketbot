"""The basis threatens exactly one side, and a NO quote flips it."""

from __future__ import annotations

import importlib

from polymarketbot.strategy.base import BookContext, DecisionContext

Module = importlib.import_module("ideas.cross-kalshi-basis-side-pull.module")


def _ctx(*, basis=None, age=None, outcome="YES", mid=0.5):
    signals = {}
    if basis is not None:
        signals["basis"] = basis
    if age is not None:
        signals["pm_age_s"] = age
    return DecisionContext(
        book=BookContext("c", "a", outcome, mid, mid - 0.01, mid + 0.01, 0.01, 2.0, 0.0),
        seconds_elapsed=1.0, inventory_shares=0.0, max_inventory_shares=100.0,
        bid_queue_ratio=0.0, ask_queue_ratio=0.0, bid_size_at_level=0.0, ask_size_at_level=0.0,
        signals=signals,
    )


def test_no_signal_quotes_both() -> None:
    out = Module.STRATEGY.decide(_ctx())
    assert (out.bid, out.ask) == ("QUOTE", "QUOTE")


def test_inside_threshold_quotes_both() -> None:
    out = Module.STRATEGY.decide(_ctx(basis=1.5, age=1.0))
    assert (out.bid, out.ask) == ("QUOTE", "QUOTE")


def test_positive_basis_shrinks_the_ask() -> None:
    out = Module.STRATEGY.decide(_ctx(basis=4.0, age=1.0))
    assert out.ask == "SHRINK" and out.bid == "QUOTE"
    assert out.ask_scale == 0.01


def test_negative_basis_shrinks_the_bid() -> None:
    out = Module.STRATEGY.decide(_ctx(basis=-4.0, age=1.0))
    assert out.bid == "SHRINK" and out.ask == "QUOTE"


def test_a_no_quote_flips_the_threat() -> None:
    out = Module.STRATEGY.decide(_ctx(basis=4.0, age=1.0, outcome="NO"))
    assert out.bid == "SHRINK" and out.ask == "QUOTE"


def test_stale_signal_is_ignored() -> None:
    out = Module.STRATEGY.decide(_ctx(basis=9.0, age=120.0))
    assert (out.bid, out.ask) == ("QUOTE", "QUOTE")
