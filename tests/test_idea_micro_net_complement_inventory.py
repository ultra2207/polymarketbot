"""Decision-logic tests for the ``micro-net-complement-inventory`` module.

Hand-built contexts only: no tape, no replay, no clock. Each case sets this
token's inventory, its cap and ``signals["sibling_inventory_frac"]`` and
asserts the per-side instruction, so the rule

    u = inventory / max(cap, 1);  sib = signals["sibling_inventory_frac"]
    sign(u) == sign(sib) != 0 -> the leg that adds to that direction is 0.5
    otherwise                 -> QUOTE / QUOTE at 1.0

is proven rather than observed. A missing signal must leave both sides alone:
the module never guesses sibling exposure.
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
module = importlib.import_module("ideas.micro-net-complement-inventory.module")
FULL_SCALE = module.FULL_SCALE
SHRINK_SCALE = module.SHRINK_SCALE
SIBLING_SIGNAL = module.SIBLING_SIGNAL
STRATEGY = module.STRATEGY
NetComplementInventory = module.NetComplementInventory

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
def strategy() -> NetComplementInventory:
    return NetComplementInventory()


def _market() -> MarketView:
    return MarketView(
        condition_id="cond-1",
        question="Will it happen?",
        rate_per_day=20.0,
        rewards_min_size=50.0,
        rewards_max_spread=3.0,
        tokens=("asset-yes", "asset-no"),
    )


def _ctx(
    inventory: float,
    sibling: float | None = None,
    cap: float = CAP,
    signals: dict[str, float] | None = None,
) -> DecisionContext:
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
    if signals is None:
        signals = {} if sibling is None else {SIBLING_SIGNAL: sibling}
    return DecisionContext(
        book=book,
        seconds_elapsed=10.0,
        inventory_shares=inventory,
        max_inventory_shares=cap,
        bid_queue_ratio=0.0,
        ask_queue_ratio=0.0,
        bid_size_at_level=0.0,
        ask_size_at_level=0.0,
        signals=signals,
    )


def test_strategy_singleton_implements_protocol() -> None:
    assert isinstance(STRATEGY, StrategyModule)
    assert STRATEGY.name == "micro-net-complement-inventory"


def test_no_signal_is_inert_and_quotes_both_sides(strategy: NetComplementInventory) -> None:
    """Without ``sibling_inventory_frac`` there is no net to price: quote both."""
    decision = strategy.decide(_ctx(inventory=90.0))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0
    assert decision.bid_price is None
    assert decision.ask_price is None
    assert "no sibling_inventory_frac" in decision.reason


def test_same_direction_long_shrinks_the_bid(strategy: NetComplementInventory) -> None:
    """u = +0.5 and sibling +0.4: both long, so the bid grows the shared risk."""
    decision = strategy.decide(_ctx(inventory=50.0, sibling=0.4))
    assert decision.bid == "SHRINK"
    assert decision.bid_scale == pytest.approx(SHRINK_SCALE)
    assert decision.ask == "QUOTE"
    assert decision.ask_scale == 1.0
    assert "bid grows shared risk" in decision.reason


def test_same_direction_short_shrinks_the_ask(strategy: NetComplementInventory) -> None:
    """u = -0.5 and sibling -0.4: both short, so the ask grows the shared risk."""
    decision = strategy.decide(_ctx(inventory=-50.0, sibling=-0.4))
    assert decision.ask == "SHRINK"
    assert decision.ask_scale == pytest.approx(SHRINK_SCALE)
    assert decision.bid == "QUOTE"
    assert decision.bid_scale == 1.0
    assert "ask grows shared risk" in decision.reason


def test_opposite_direction_is_a_completed_pair_and_quotes_both(
    strategy: NetComplementInventory,
) -> None:
    """Long this leg, short the sibling: the pair is netted, so full size both."""
    decision = strategy.decide(_ctx(inventory=50.0, sibling=-0.4))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0
    assert "completed pair" in decision.reason

    mirrored = strategy.decide(_ctx(inventory=-50.0, sibling=0.4))
    assert (mirrored.bid, mirrored.ask) == ("QUOTE", "QUOTE")
    assert mirrored.bid_scale == 1.0
    assert mirrored.ask_scale == 1.0


def test_warm_up_after_setup_has_no_blind_period(
    strategy: NetComplementInventory,
) -> None:
    """The rule is stateless: the first decision after setup already nets."""
    strategy.setup(_market())
    flat = strategy.decide(_ctx(inventory=0.0, sibling=0.4))
    assert (flat.bid, flat.ask) == ("QUOTE", "QUOTE")
    assert flat.bid_scale == 1.0
    assert flat.ask_scale == 1.0

    stacked = strategy.decide(_ctx(inventory=50.0, sibling=0.4))
    assert (stacked.bid, stacked.ask) == ("SHRINK", "QUOTE")
    assert stacked.bid_scale == pytest.approx(SHRINK_SCALE)


def test_flat_own_inventory_has_no_direction_to_net(strategy: NetComplementInventory) -> None:
    """A zero fraction on either side is not a shared direction."""
    for inventory, sibling in ((0.0, 0.4), (50.0, 0.0), (0.0, 0.0)):
        decision = strategy.decide(_ctx(inventory=inventory, sibling=sibling))
        assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
        assert decision.bid_scale == 1.0
        assert decision.ask_scale == 1.0


def test_missing_or_unusable_signal_never_shrinks(strategy: NetComplementInventory) -> None:
    """A non-finite or unparsable signal is absence, not a guessed direction."""
    for signals in (
        {SIBLING_SIGNAL: float("nan")},
        {SIBLING_SIGNAL: float("inf")},
        {SIBLING_SIGNAL: "not-a-number"},  # type: ignore[dict-item]
        {"other_signal": 0.9},
    ):
        decision = strategy.decide(_ctx(inventory=50.0, signals=signals))
        assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
        assert decision.bid_scale == 1.0
        assert decision.ask_scale == 1.0


def test_degenerate_cap_keeps_the_sign_with_a_share_floor(
    strategy: NetComplementInventory,
) -> None:
    """``max(cap, 1)`` means a zero cap cannot erase the direction of a position."""
    decision = strategy.decide(_ctx(inventory=5.0, sibling=0.2, cap=0.0))
    assert (decision.bid, decision.ask) == ("SHRINK", "QUOTE")
    assert decision.bid_scale == pytest.approx(SHRINK_SCALE)


def test_never_pulls_never_prices_and_shrinks_at_most_one_side(
    strategy: NetComplementInventory,
) -> None:
    inventories = [-1000.0, -50.0, -0.01, 0.0, 0.01, 50.0, 1000.0]
    siblings = [-1.0, -0.4, 0.0, 0.4, 1.0]
    for inventory in inventories:
        for sibling in siblings:
            decision = strategy.decide(_ctx(inventory=inventory, sibling=sibling))
            assert decision.bid in ("QUOTE", "SHRINK")
            assert decision.ask in ("QUOTE", "SHRINK")
            assert decision.bid_price is None
            assert decision.ask_price is None
            assert SHRINK_SCALE <= decision.bid_scale <= FULL_SCALE
            assert SHRINK_SCALE <= decision.ask_scale <= FULL_SCALE
            shrunk = (decision.bid == "SHRINK") + (decision.ask == "SHRINK")
            assert shrunk <= 1
            same_way = inventory * sibling > 0.0
            if same_way:
                expected = "SHRINK" if inventory > 0.0 else "QUOTE"
                assert decision.bid == expected
                expected_ask = "SHRINK" if inventory < 0.0 else "QUOTE"
                assert decision.ask == expected_ask
            else:
                assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
