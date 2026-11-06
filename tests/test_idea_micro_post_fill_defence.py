"""Decision-logic tests for the post-fill defence module.

Hand-built contexts only: no tape, no replay, no network.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The idea directory keeps the registry slug (hyphens), so the module name is
# not a Python identifier; the harness resolves it the same way.
_idea = importlib.import_module("ideas.micro-post-fill-defence.module")
DEFENCE_CALLS = _idea.DEFENCE_CALLS
DEFENCE_SCALE = _idea.DEFENCE_SCALE
PostFillDefence = _idea.PostFillDefence

from polymarketbot.strategy.base import (  # noqa: E402
    BookContext,
    DecisionContext,
    TradeContext,
)

ASSET = "asset-yes"
OTHER = "asset-no"
BID = 0.45
ASK = 0.47
TICK = 0.01


def _book(
    asset: str = ASSET,
    best_bid: float = BID,
    best_ask: float = ASK,
    tick: float = TICK,
) -> BookContext:
    return BookContext(
        condition_id="cond",
        asset_id=asset,
        outcome="YES",
        mid=(best_bid + best_ask) / 2.0,
        best_bid=best_bid,
        best_ask=best_ask,
        tick_size=tick,
        spread_cents=(best_ask - best_bid) * 100.0,
    )


def _decision(book: BookContext | None = None) -> DecisionContext:
    return DecisionContext(
        book=book or _book(),
        seconds_elapsed=0.0,
        inventory_shares=0.0,
        max_inventory_shares=100.0,
        bid_queue_ratio=0.0,
        ask_queue_ratio=0.0,
        bid_size_at_level=0.0,
        ask_size_at_level=0.0,
        ofi=None,
    )


def _trade(price: float, side: str, asset: str = ASSET) -> TradeContext:
    return TradeContext(
        asset_id=asset,
        price=price,
        size=100.0,
        side=side,
        seconds_elapsed=0.0,
    )


def _fresh() -> PostFillDefence:
    module = PostFillDefence()
    module.setup(None)  # setup ignores the market; it only resets state
    return module


def _armed(price: float, side: str) -> PostFillDefence:
    module = _fresh()
    module.on_book(_book())
    module.on_trade(_trade(price, side))
    return module


def _assert_quotes(action) -> None:
    assert (action.bid, action.ask) == ("QUOTE", "QUOTE")


def test_no_trade_quotes_both_sides():
    module = _fresh()
    module.on_book(_book())

    action = module.decide(_decision())

    _assert_quotes(action)
    assert action.bid_scale == 1.0
    assert action.ask_scale == 1.0


@pytest.mark.parametrize("price", [0.44, 0.45, 0.46])
def test_sell_print_at_the_bid_shrinks_the_bid_for_five_calls(price):
    module = _armed(price, "SELL")

    for call in range(1, DEFENCE_CALLS + 1):
        action = module.decide(_decision())
        assert action.bid == "SHRINK", f"call {call} should still defend the bid"
        assert action.bid_scale == DEFENCE_SCALE
        assert action.ask == "QUOTE", "the ask must never be shrunk by a bid hit"
        assert action.ask_scale == 1.0

    _assert_quotes(module.decide(_decision()))


@pytest.mark.parametrize("price", [0.46, 0.47, 0.48])
def test_buy_print_at_the_ask_shrinks_the_ask_for_five_calls(price):
    module = _armed(price, "BUY")

    for call in range(1, DEFENCE_CALLS + 1):
        action = module.decide(_decision())
        assert action.ask == "SHRINK", f"call {call} should still defend the ask"
        assert action.ask_scale == DEFENCE_SCALE
        assert action.bid == "QUOTE", "the bid must never be shrunk by an ask hit"
        assert action.bid_scale == 1.0

    _assert_quotes(module.decide(_decision()))


@pytest.mark.parametrize(("price", "side"), [(0.47, "SELL"), (0.50, "SELL"), (0.45, "BUY")])
def test_print_away_from_our_side_does_not_arm(price, side):
    module = _armed(price, side)

    _assert_quotes(module.decide(_decision()))


def test_trade_before_any_book_is_a_no_op():
    module = _fresh()
    module.on_trade(_trade(0.45, "SELL"))

    _assert_quotes(module.decide(_decision()))


def test_second_hit_restarts_the_window_on_the_new_side():
    module = _armed(0.45, "SELL")
    for _ in range(2):
        assert module.decide(_decision()).bid == "SHRINK"

    module.on_trade(_trade(0.47, "BUY"))
    for call in range(1, DEFENCE_CALLS + 1):
        action = module.decide(_decision())
        assert action.ask == "SHRINK", f"call {call} should defend the ask"
        assert action.bid == "QUOTE"
    _assert_quotes(module.decide(_decision()))


def test_window_is_per_asset():
    module = _armed(0.45, "SELL")

    other = module.decide(_decision(_book(asset=OTHER)))

    _assert_quotes(other)
    assert module.decide(_decision()).bid == "SHRINK"
