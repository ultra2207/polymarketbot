"""A stack may only tighten a quote, never add exposure.

That is what makes a combination result interpretable: if the composite beats its
best member, the interaction is real rather than leverage.
"""

from __future__ import annotations

from polymarketbot.strategy.base import SideActions
from polymarketbot.strategy.composite import combine


def test_most_conservative_action_wins_per_side() -> None:
    out = combine(
        [
            SideActions(bid="QUOTE", ask="QUOTE"),
            SideActions(bid="SHRINK", ask="QUOTE", bid_scale=0.4),
            SideActions(bid="QUOTE", ask="PULL"),
        ]
    )
    assert (out.bid, out.ask) == ("SHRINK", "PULL")
    assert out.bid_scale == 0.4
    assert out.ask_scale == 0.0


def test_smallest_surviving_scale_is_used() -> None:
    out = combine(
        [
            SideActions(bid="SHRINK", bid_scale=0.5),
            SideActions(bid="SHRINK", bid_scale=0.2),
        ]
    )
    assert out.bid_scale == 0.2


def test_pull_beats_shrink_and_zeroes_the_scale() -> None:
    out = combine([SideActions(ask="SHRINK", ask_scale=0.9), SideActions(ask="PULL")])
    assert out.ask == "PULL" and out.ask_scale == 0.0


def test_a_composite_can_never_increase_scale() -> None:
    out = combine([SideActions(bid="QUOTE", bid_scale=1.0), SideActions(bid="QUOTE", bid_scale=0.3)])
    assert out.bid == "QUOTE" and out.bid_scale == 0.3


def test_first_explicit_price_wins_and_is_kept() -> None:
    out = combine(
        [
            SideActions(bid_price=None, ask_price=None),
            SideActions(bid_price=0.41, ask_price=0.59),
            SideActions(bid_price=0.40, ask_price=0.60),
        ]
    )
    assert (out.bid_price, out.ask_price) == (0.41, 0.59)


def test_empty_stack_quotes_both() -> None:
    out = combine([])
    assert (out.bid, out.ask) == ("QUOTE", "QUOTE")
