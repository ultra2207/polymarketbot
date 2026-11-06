"""Enter where competition is thin, leave where it is thick, never churn."""

from __future__ import annotations

import importlib

from polymarketbot.strategy.base import BookContext, DecisionContext

Module = importlib.import_module("ideas.micro-maker-vacancy.module")


def _ctx(comp: float):
    return DecisionContext(
        book=BookContext("c", "a", "YES", 0.5, 0.49, 0.51, 0.01, 2.0, comp),
        seconds_elapsed=1.0, inventory_shares=0.0, max_inventory_shares=100.0,
        bid_queue_ratio=0.0, ask_queue_ratio=0.0, bid_size_at_level=100.0, ask_size_at_level=100.0,
    )


def test_warmup_quotes() -> None:
    m = Module.MakerVacancy()
    for _ in range(Module.WARMUP - 1):
        assert m.decide(_ctx(50.0)).bid == "QUOTE"


def test_a_crowded_market_is_left_and_a_vacant_one_re_entered() -> None:
    m = Module.MakerVacancy()
    for i in range(Module.WARMUP):
        m.decide(_ctx(float(i)))
    assert m.decide(_ctx(10_000.0)).bid == "PULL"      # above the 75th percentile
    assert m.decide(_ctx(0.0)).bid == "QUOTE"          # back below the 25th
