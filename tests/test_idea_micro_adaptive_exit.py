"""A market is abandoned only after it has proven itself unprofitable."""

from __future__ import annotations

import importlib

from polymarketbot.strategy.base import BookContext, DecisionContext

Module = importlib.import_module("ideas.micro-adaptive-exit.module")


def _ctx(*, pnl=0.0, fills=0) -> DecisionContext:
    return DecisionContext(
        book=BookContext("c", "a", "YES", 0.5, 0.49, 0.51, 0.01, 2.0, 0.0),
        seconds_elapsed=1.0, inventory_shares=0.0, max_inventory_shares=100.0,
        bid_queue_ratio=0.0, ask_queue_ratio=0.0, bid_size_at_level=0.0, ask_size_at_level=0.0,
        realized_pnl=pnl, fills_count=fills,
    )


def test_no_fills_quotes() -> None:
    assert Module.STRATEGY.decide(_ctx(fills=0)).bid == "QUOTE"


def test_positive_pnl_keeps_quoting() -> None:
    assert Module.STRATEGY.decide(_ctx(pnl=1.5, fills=20)).bid == "QUOTE"


def test_losing_market_is_withdrawn_after_the_minimum() -> None:
    m = Module.AdaptiveExit()
    assert m.decide(_ctx(pnl=-0.4, fills=8)).bid == "PULL"


def test_a_loser_with_too_few_fills_is_kept() -> None:
    m = Module.AdaptiveExit()
    assert m.decide(_ctx(pnl=-0.4, fills=3)).bid == "QUOTE"


def test_a_recovered_market_is_readmitted() -> None:
    """A withdrawn market is re-evaluated after the cooldown, not blacklisted."""
    m = Module.AdaptiveExit()
    assert m.decide(_ctx(pnl=-0.4, fills=9)).bid == "PULL"
    # While it keeps losing, the cooldown restarts and it stays out.
    for _ in range(30):
        assert m.decide(_ctx(pnl=-0.4, fills=9)).bid == "PULL"
    # Once the market turns positive, the next evaluation quotes again.
    out = None
    for _ in range(61):
        out = m.decide(_ctx(pnl=+0.8, fills=9))
    assert out is not None and out.bid == "QUOTE"
