"""Decision-logic tests for the ``micro-inventory-skew-size`` module.

Hand-built contexts only: no tape, no replay, no clock. Each case sets an
inventory and a cap and asserts the returned per-side scale, so the rule

    u = clamp(inventory / max_inventory, -1, 1)
    bid_scale = clamp(1 - u, 0.25, 1)
    ask_scale = clamp(1 + u, 0.25, 1)

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
module = importlib.import_module("ideas.micro-inventory-skew-size.module")
MAX_SCALE = module.MAX_SCALE
MIN_SCALE = module.MIN_SCALE
STRATEGY = module.STRATEGY
InventorySkewSize = module.InventorySkewSize

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
def strategy() -> InventorySkewSize:
    return InventorySkewSize()


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
    assert STRATEGY.name == "micro-inventory-skew-size"


def test_warm_up_after_setup_quotes_full_size(strategy: InventorySkewSize) -> None:
    """No stored state to warm up: the first decision after setup is full size."""
    strategy.setup(_market())
    decision = strategy.decide(_ctx(inventory=0.0))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0
    assert decision.bid_price is None
    assert decision.ask_price is None


def test_flat_inventory_quotes_both_sides_at_full_size(strategy: InventorySkewSize) -> None:
    decision = strategy.decide(_ctx(inventory=0.0))
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0
    assert decision.bid == "QUOTE"
    assert decision.ask == "QUOTE"


def test_long_inventory_shrinks_the_bid_only(strategy: InventorySkewSize) -> None:
    """u = 0.5 -> bid clamp(0.5) = 0.5, ask stays at 1.0."""
    decision = strategy.decide(_ctx(inventory=50.0))
    assert decision.bid_scale == pytest.approx(0.5)
    assert decision.ask_scale == 1.0
    assert decision.bid == "SHRINK"
    assert decision.ask == "QUOTE"
    assert "long" in decision.reason


def test_short_inventory_shrinks_the_ask_only(strategy: InventorySkewSize) -> None:
    """u = -0.5 -> ask clamp(0.5) = 0.5, bid stays at 1.0."""
    decision = strategy.decide(_ctx(inventory=-50.0))
    assert decision.ask_scale == pytest.approx(0.5)
    assert decision.bid_scale == 1.0
    assert decision.ask == "SHRINK"
    assert decision.bid == "QUOTE"
    assert "short" in decision.reason


def test_skew_is_linear_between_flat_and_the_floor(strategy: InventorySkewSize) -> None:
    """u = 0.25 -> 0.75, u = 0.6 -> 0.4; the reducing side never moves."""
    assert strategy.decide(_ctx(inventory=25.0)).bid_scale == pytest.approx(0.75)
    decision = strategy.decide(_ctx(inventory=60.0))
    assert decision.bid_scale == pytest.approx(0.4)
    assert decision.ask_scale == 1.0


def test_long_floor_is_a_quarter_at_three_quarters_of_the_cap(strategy: InventorySkewSize) -> None:
    """u = 0.75 is where 1 - u reaches the floor; above it the scale sticks."""
    at_floor = strategy.decide(_ctx(inventory=75.0))
    assert at_floor.bid_scale == pytest.approx(MIN_SCALE)
    assert MIN_SCALE == 0.25
    above_floor = strategy.decide(_ctx(inventory=90.0))
    assert above_floor.bid_scale == pytest.approx(MIN_SCALE)
    assert above_floor.ask_scale == 1.0


def test_short_floor_is_a_quarter_at_three_quarters_of_the_cap(strategy: InventorySkewSize) -> None:
    at_floor = strategy.decide(_ctx(inventory=-75.0))
    assert at_floor.ask_scale == pytest.approx(MIN_SCALE)
    above_floor = strategy.decide(_ctx(inventory=-90.0))
    assert above_floor.ask_scale == pytest.approx(MIN_SCALE)
    assert above_floor.bid_scale == 1.0


def test_inventory_beyond_the_cap_is_clamped_not_extrapolated(strategy: InventorySkewSize) -> None:
    """|u| > 1 clamps to 1, so the floor holds instead of crossing it."""
    long = strategy.decide(_ctx(inventory=10 * CAP))
    assert long.bid_scale == pytest.approx(MIN_SCALE)
    assert long.ask_scale == 1.0
    short = strategy.decide(_ctx(inventory=-10 * CAP))
    assert short.ask_scale == pytest.approx(MIN_SCALE)
    assert short.bid_scale == 1.0


def test_degenerate_cap_is_read_as_flat_inventory(strategy: InventorySkewSize) -> None:
    """A zero or negative cap cannot normalise inventory, so we do not guess."""
    for cap in (0.0, -100.0):
        decision = strategy.decide(_ctx(inventory=500.0, cap=cap))
        assert decision.bid_scale == 1.0
        assert decision.ask_scale == 1.0


def test_never_pulls_never_prices_and_shrinks_at_most_one_side(
    strategy: InventorySkewSize,
) -> None:
    inventories = [-1000.0, -100.0, -75.0, -50.0, -0.01, 0.0, 0.01, 50.0, 75.0, 100.0, 1000.0]
    for inventory in inventories:
        decision = strategy.decide(_ctx(inventory=inventory))
        assert decision.bid in ("QUOTE", "SHRINK")
        assert decision.ask in ("QUOTE", "SHRINK")
        assert decision.bid_price is None
        assert decision.ask_price is None
        assert MIN_SCALE <= decision.bid_scale <= MAX_SCALE
        assert MIN_SCALE <= decision.ask_scale <= MAX_SCALE
        # Only the side that would grow |inventory| is ever cut.
        if inventory > 0.0:
            assert decision.ask_scale == 1.0
        elif inventory < 0.0:
            assert decision.bid_scale == 1.0
        else:
            assert (decision.bid_scale, decision.ask_scale) == (1.0, 1.0)
