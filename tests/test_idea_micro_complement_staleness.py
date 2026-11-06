"""A gap between our mid and the sibling-implied mid pulls both sides of ours."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from polymarketbot.strategy.base import BookContext, DecisionContext

Module = importlib.import_module("ideas.micro-complement-staleness.module")


def _book(asset: str, mid: float):
    return BookContext("c", asset, "YES", mid, mid - 0.01, mid + 0.01, 0.01, 2.0, 0.0)


def _ctx(asset: str, mid: float):
    return DecisionContext(
        book=_book(asset, mid), seconds_elapsed=1.0, inventory_shares=0.0,
        max_inventory_shares=100.0, bid_queue_ratio=0.0, ask_queue_ratio=0.0,
        bid_size_at_level=100.0, ask_size_at_level=100.0,
    )


def test_a_consistent_pair_quotes() -> None:
    m = Module.ComplementStaleness()
    m.setup(SimpleNamespace(tokens=("yes", "no")))
    m.on_book(_book("no", 0.50))
    out = m.decide(_ctx("yes", 0.50))
    assert (out.bid, out.ask) == ("QUOTE", "QUOTE")


def test_a_dislocated_pair_pulls_the_lagging_token() -> None:
    m = Module.ComplementStaleness()
    m.setup(SimpleNamespace(tokens=("yes", "no")))
    m.on_book(_book("no", 0.44))          # sibling repriced
    out = m.decide(_ctx("yes", 0.50))     # we are still at the old fair value
    assert (out.bid, out.ask) == ("PULL", "PULL")
    assert "ticks" in out.reason


def test_the_leading_token_keeps_quoting() -> None:
    """The pull follows the stale token, not the whole condition."""
    m = Module.ComplementStaleness()
    m.setup(SimpleNamespace(tokens=("yes", "no")))
    m.on_book(_book("yes", 0.44))
    m.on_book(_book("no", 0.56))
    assert m.decide(_ctx("no", 0.56)).bid == "QUOTE"
