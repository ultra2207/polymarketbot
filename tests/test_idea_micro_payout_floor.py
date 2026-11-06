"""Pull a market whose projected daily payout cannot clear the $1 venue floor.

Below the floor the reward term is exactly zero, so quoting earns nothing and
only adverse selection remains. The hysteresis band stops flapping at the line.
"""

from __future__ import annotations

import importlib

from polymarketbot.strategy.base import BookContext, DecisionContext, MarketView

PayoutFloor = importlib.import_module("ideas.micro-payout-floor.module").PayoutFloor


def _ctx(*, rate: float, bid_score: float, ask_score: float, competing: float) -> DecisionContext:
    return DecisionContext(
        book=BookContext(
            condition_id="c", asset_id="a", outcome="YES", mid=0.5, best_bid=0.49,
            best_ask=0.51, tick_size=0.01, spread_cents=2.0, competing_score=competing,
        ),
        seconds_elapsed=1.0,
        inventory_shares=0.0,
        max_inventory_shares=100.0,
        bid_queue_ratio=0.0,
        ask_queue_ratio=0.0,
        bid_size_at_level=0.0,
        ask_size_at_level=0.0,
        ofi=0.0,
        bid_score=bid_score,
        ask_score=ask_score,
        rate_per_day=rate,
    )


def _module() -> PayoutFloor:
    m = PayoutFloor()
    m.setup(MarketView("c", "q", 100.0, 20.0, 5.0, ("a", "b")))
    return m


def test_no_resting_score_quotes() -> None:
    out = _module().decide(_ctx(rate=100.0, bid_score=0.0, ask_score=0.0, competing=10.0))
    assert (out.bid, out.ask) == ("QUOTE", "QUOTE")


def test_high_projection_keeps_quoting() -> None:
    # 100/day * (20 / (20 + 10)) = $66.7 -> far above the floor.
    out = _module().decide(_ctx(rate=100.0, bid_score=10.0, ask_score=10.0, competing=10.0))
    assert (out.bid, out.ask) == ("QUOTE", "QUOTE")


def test_below_floor_pulls_both() -> None:
    # 2/day * (1 / (1 + 1000)) = $0.002 -> dead reward.
    out = _module().decide(_ctx(rate=2.0, bid_score=0.5, ask_score=0.5, competing=1000.0))
    assert (out.bid, out.ask) == ("PULL", "PULL")
    assert out.bid_scale == 0.0


def test_hysteresis_keeps_it_withdrawn_between_floor_and_resume() -> None:
    m = _module()
    m.decide(_ctx(rate=2.0, bid_score=0.5, ask_score=0.5, competing=1000.0))  # withdraw
    # $1.10/day is above the floor but below the resume band: stay out.
    out = m.decide(_ctx(rate=2.0, bid_score=5.5, ask_score=5.5, competing=1000.0))
    assert (out.bid, out.ask) == ("PULL", "PULL")
    # Above the band: resume.
    out = m.decide(_ctx(rate=20.0, bid_score=60.0, ask_score=60.0, competing=1000.0))
    assert (out.bid, out.ask) == ("QUOTE", "QUOTE")
