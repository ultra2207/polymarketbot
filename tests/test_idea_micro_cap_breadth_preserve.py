"""Decision-logic tests for the ``micro-cap-breadth-preserve`` module.

Hand-built contexts only: no tape, no replay, no clock. Each case sets an
inventory and a cap and asserts the returned per-side scale, so the rule

    u = min(|inventory| / max_inventory, 1)
    if u < 0.6:  bid_scale = ask_scale = 1.0
    else:        add_scale = clamp((1 - u) / 0.4, 0.25, 1.0)
                 long  -> bid_scale = add_scale, ask_scale = 1.0
                 short -> ask_scale = add_scale, bid_scale = 1.0

is proven rather than observed. The module must never pull a side and never set
a price: the engine floors any positive size at ``rewards_min_size`` and
re-quotes on every book update, so a 0.25 scale is already the smallest quote
that still scores.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The directory is hyphenated, so it is resolved the way the harness resolves it.
module = importlib.import_module("ideas.micro-cap-breadth-preserve.module")
MAX_SCALE = module.MAX_SCALE
MIN_SCALE = module.MIN_SCALE
RAMP_START = module.RAMP_START
STRATEGY = module.STRATEGY
CapBreadthPreserve = module.CapBreadthPreserve

from polymarketbot.strategy.base import (  # noqa: E402
    BookContext,
    DecisionContext,
    MarketView,
    StrategyModule,
)

TICK = 0.01
MID = 0.50
CAP = 100.0


@pytest.fixture()
def strategy() -> CapBreadthPreserve:
    return CapBreadthPreserve()


def _market() -> MarketView:
    return MarketView(
        condition_id="cond-1",
        question="Will it happen?",
        rate_per_day=20.0,
        rewards_min_size=50.0,
        rewards_max_spread=3.0,
        tokens=("asset-yes", "asset-no"),
    )


def _ctx(inventory: float, cap: float = CAP) -> DecisionContext:
    book = BookContext(
        condition_id="cond-1",
        asset_id="asset-yes",
        outcome="YES",
        mid=MID,
        best_bid=round(MID - TICK, 4),
        best_ask=round(MID + TICK, 4),
        tick_size=TICK,
        spread_cents=TICK * 100.0,
    )
    return DecisionContext(
        book=book,
        seconds_elapsed=10.0,
        inventory_shares=inventory,
        max_inventory_shares=cap,
        bid_queue_ratio=0.0,
        ask_queue_ratio=0.0,
        bid_size_at_level=0.0,
        ask_size_at_level=0.0,
    )


def test_strategy_singleton_implements_protocol() -> None:
    assert isinstance(STRATEGY, StrategyModule)
    assert STRATEGY.name == "micro-cap-breadth-preserve"


def test_warm_up_after_setup_quotes_full_size(strategy: CapBreadthPreserve) -> None:
    """No stored state to warm up: the first decision after setup is full size."""
    strategy.setup(_market())
    decision = strategy.decide(_ctx(inventory=0.0))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0
    assert decision.bid_price is None
    assert decision.ask_price is None


def test_ramp_does_not_latch(strategy: CapBreadthPreserve) -> None:
    """A ramped decision leaves no state: returning to flat restores full size."""
    ramped = strategy.decide(_ctx(inventory=0.9 * CAP))
    assert ramped.bid_scale < MAX_SCALE
    recovered = strategy.decide(_ctx(inventory=0.1 * CAP))
    assert (recovered.bid_scale, recovered.ask_scale) == (1.0, 1.0)
    assert (recovered.bid, recovered.ask) == ("QUOTE", "QUOTE")


def test_below_the_ramp_quotes_both_sides_at_full_size(strategy: CapBreadthPreserve) -> None:
    """u = 0.2 < 0.6: no shrink, both sides QUOTE at 1.0."""
    decision = strategy.decide(_ctx(inventory=20.0))
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")


def test_ramp_boundary_is_continuous_at_sixty_percent(strategy: CapBreadthPreserve) -> None:
    """u = 0.6 gives add_scale = (1 - 0.6) / 0.4 = 1.0, so nothing is cut."""
    decision = strategy.decide(_ctx(inventory=0.6 * CAP))
    assert decision.bid_scale == pytest.approx(MAX_SCALE)
    assert decision.ask_scale == pytest.approx(MAX_SCALE)
    assert decision.bid == "QUOTE"


def test_long_inventory_shrinks_the_bid_only(strategy: CapBreadthPreserve) -> None:
    """u = 0.8 long: bid clamped to 0.5, ask stays full; the bid never vanishes."""
    decision = strategy.decide(_ctx(inventory=80.0))
    assert decision.bid_scale < MAX_SCALE
    assert decision.bid_scale >= MIN_SCALE
    assert decision.bid_scale == pytest.approx(0.5)
    assert decision.ask_scale == 1.0
    assert decision.bid == "SHRINK"
    assert decision.ask == "QUOTE"
    assert "shrink bid" in decision.reason


def test_long_at_ninety_nine_percent_sits_on_the_floor(strategy: CapBreadthPreserve) -> None:
    """u = 0.99: (1 - 0.99) / 0.4 = 0.025 -> floor 0.25, still not a pull."""
    decision = strategy.decide(_ctx(inventory=99.0))
    assert decision.bid_scale == pytest.approx(MIN_SCALE)
    assert MIN_SCALE == 0.25
    assert decision.bid == "SHRINK"
    assert decision.ask_scale == 1.0
    assert decision.ask == "QUOTE"


def test_short_inventory_shrinks_the_ask_only(strategy: CapBreadthPreserve) -> None:
    """u = 0.8 short: ask clamped to 0.5, bid stays full; mirrors the long case."""
    decision = strategy.decide(_ctx(inventory=-80.0))
    assert decision.ask_scale < MAX_SCALE
    assert decision.ask_scale >= MIN_SCALE
    assert decision.ask_scale == pytest.approx(0.5)
    assert decision.bid_scale == 1.0
    assert decision.ask == "SHRINK"
    assert decision.bid == "QUOTE"
    assert "shrink ask" in decision.reason


def test_short_at_ninety_nine_percent_sits_on_the_floor(strategy: CapBreadthPreserve) -> None:
    decision = strategy.decide(_ctx(inventory=-99.0))
    assert decision.ask_scale == pytest.approx(MIN_SCALE)
    assert decision.bid_scale == 1.0
    assert decision.bid == "QUOTE"


def test_inventory_beyond_the_cap_is_clamped_not_extrapolated(strategy: CapBreadthPreserve) -> None:
    """|u| > 1 clamps to 1, so the floor holds instead of crossing it."""
    long = strategy.decide(_ctx(inventory=10 * CAP))
    assert long.bid_scale == pytest.approx(MIN_SCALE)
    assert long.ask_scale == 1.0
    short = strategy.decide(_ctx(inventory=-10 * CAP))
    assert short.ask_scale == pytest.approx(MIN_SCALE)
    assert short.bid_scale == 1.0


def test_degenerate_cap_is_read_as_flat_inventory(strategy: CapBreadthPreserve) -> None:
    """A zero or negative cap cannot normalise inventory, so we do not guess."""
    for cap in (0.0, -100.0):
        decision = strategy.decide(_ctx(inventory=500.0, cap=cap))
        assert decision.bid_scale == 1.0
        assert decision.ask_scale == 1.0
        assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")


def test_never_pulls_never_prices_and_never_zeroes_the_resting_side(
    strategy: CapBreadthPreserve,
) -> None:
    inventories = [-1000.0, -100.0, -99.0, -80.0, -60.0, -20.0, -0.01, 0.0, 0.01, 20.0, 60.0, 80.0, 99.0, 100.0, 1000.0]
    for inventory in inventories:
        decision = strategy.decide(_ctx(inventory=inventory))
        assert decision.bid in ("QUOTE", "SHRINK")
        assert decision.ask in ("QUOTE", "SHRINK")
        assert decision.bid_price is None
        assert decision.ask_price is None
        assert MIN_SCALE <= decision.bid_scale <= MAX_SCALE
        assert MIN_SCALE <= decision.ask_scale <= MAX_SCALE
        # Both sides always carry positive size: the floor keeps the book two-sided.
        assert decision.bid_scale > 0.0
        assert decision.ask_scale > 0.0
        # Only the side that would grow |inventory| is ever cut.
        u = min(abs(inventory) / CAP, 1.0)
        if inventory > 0.0 and u > RAMP_START:
            assert decision.ask_scale == MAX_SCALE
            assert decision.bid_scale < MAX_SCALE
        elif inventory < 0.0 and u > RAMP_START:
            assert decision.bid_scale == MAX_SCALE
            assert decision.ask_scale < MAX_SCALE
        else:
            assert (decision.bid_scale, decision.ask_scale) == (MAX_SCALE, MAX_SCALE)
