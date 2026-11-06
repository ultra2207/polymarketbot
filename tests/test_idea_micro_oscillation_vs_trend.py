"""Decision-logic tests for the oscillation-vs-trend module.

Hand-built contexts only: no tape, no replay, no network.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The idea directory keeps the registry slug (hyphens), so the module name is
# not a Python identifier; the harness resolves it the same way.
_idea = importlib.import_module("ideas.micro-oscillation-vs-trend.module")
MIN_NON_ZERO = _idea.MIN_NON_ZERO
SHRINK_SCALE = _idea.SHRINK_SCALE
WINDOW = _idea.WINDOW
MicroOscillationVsTrend = _idea.MicroOscillationVsTrend

from polymarketbot.strategy.base import (  # noqa: E402
    BookContext,
    DecisionContext,
)

ASSET = "asset-yes"
OTHER = "asset-no"
TICK = 0.01


def _book(asset: str = ASSET, mid: float = 0.50) -> BookContext:
    best_bid = round(mid - TICK, 4)
    best_ask = round(mid + TICK, 4)
    return BookContext(
        condition_id="cond",
        asset_id=asset,
        outcome="YES",
        mid=mid,
        best_bid=best_bid,
        best_ask=best_ask,
        tick_size=TICK,
        spread_cents=(best_ask - best_bid) * 100.0,
    )


def _decision(asset: str = ASSET) -> DecisionContext:
    return DecisionContext(
        book=_book(asset=asset),
        seconds_elapsed=0.0,
        inventory_shares=0.0,
        max_inventory_shares=100.0,
        bid_queue_ratio=0.0,
        ask_queue_ratio=0.0,
        bid_size_at_level=0.0,
        ask_size_at_level=0.0,
        ofi=None,
    )


def _fresh() -> MicroOscillationVsTrend:
    module = MicroOscillationVsTrend()
    module.setup(None)  # setup ignores the market; it only resets state
    return module


def _feed(
    module: MicroOscillationVsTrend,
    signs: list[int],
    asset: str = ASSET,
    start: float = 0.50,
) -> None:
    """Feed one book per sign: +1 = mid one tick up, -1 = down, 0 = unchanged."""
    price = start
    module.on_book(_book(asset=asset, mid=price))
    for sign in signs:
        price = round(price + sign * TICK, 4)
        module.on_book(_book(asset=asset, mid=price))


def _quotes_both(action) -> None:
    assert (action.bid, action.ask) == ("QUOTE", "QUOTE")
    assert action.bid_scale == 1.0
    assert action.ask_scale == 1.0


def test_defaults_describe_a_thirty_change_window():
    assert WINDOW == 30
    assert MIN_NON_ZERO == 10
    assert SHRINK_SCALE == 0.01


def test_warm_up_quotes_both_sides():
    module = _fresh()
    _feed(module, [+1, +1, +1, +1, +1])

    _quotes_both(module.decide(_decision()))


def test_nine_non_zero_changes_is_still_warm_up():
    module = _fresh()
    _feed(module, [+1] * (MIN_NON_ZERO - 1))

    _quotes_both(module.decide(_decision()))


def test_exactly_ten_non_zero_changes_is_enough_to_fire():
    module = _fresh()
    _feed(module, [+1] * MIN_NON_ZERO)

    action = module.decide(_decision())
    assert action.ask == "SHRINK"
    assert action.ask_scale == SHRINK_SCALE


def test_flat_book_never_fires():
    module = _fresh()
    _feed(module, [0] * 40)

    _quotes_both(module.decide(_decision()))


def test_clean_oscillation_quotes_both_sides():
    module = _fresh()
    _feed(module, [+1, -1] * (WINDOW // 2))

    _quotes_both(module.decide(_decision()))


def test_zeros_do_not_break_the_flip_statistic():
    module = _fresh()
    signs: list[int] = []
    for _ in range(15):
        signs.extend([+1, 0, -1, 0])
    _feed(module, signs)

    _quotes_both(module.decide(_decision()))


def test_up_trend_shrinks_the_ask_at_one_percent():
    module = _fresh()
    _feed(module, [+1] * WINDOW, start=0.30)

    action = module.decide(_decision())
    assert action.bid == "QUOTE"
    assert action.bid_scale == 1.0
    assert action.ask == "SHRINK"
    assert action.ask_scale == 0.01


def test_down_trend_shrinks_the_bid_at_one_percent():
    module = _fresh()
    _feed(module, [-1] * WINDOW, start=0.70)

    action = module.decide(_decision())
    assert action.bid == "SHRINK"
    assert action.bid_scale == 0.01
    assert action.ask == "QUOTE"
    assert action.ask_scale == 1.0


def test_flip_fraction_exactly_at_the_oscillation_threshold_quotes_both():
    module = _fresh()
    # 11 non-zero changes, 7 of 10 adjacent pairs flip -> exactly 0.70.
    _feed(module, [+1, +1, -1, -1, +1, -1, +1, -1, +1, -1, -1])

    _quotes_both(module.decide(_decision()))


def test_flip_fraction_exactly_at_the_trend_threshold_shrinks():
    module = _fresh()
    # 11 non-zero changes, 3 of 10 adjacent pairs flip -> exactly 0.30.
    _feed(module, [+1, +1, +1, -1, -1, -1, +1, +1, +1, -1, -1])

    action = module.decide(_decision())
    assert action.bid == "SHRINK"
    assert action.bid_scale == 0.01


def test_mid_range_flip_fraction_quotes_both():
    module = _fresh()
    # 11 non-zero changes, 5 of 10 adjacent pairs flip -> 0.50.
    _feed(module, [+1, +1, -1, -1, +1, +1, -1, -1, +1, +1, -1])

    _quotes_both(module.decide(_decision()))


def test_oscillation_that_turns_into_a_trend_becomes_a_shrink():
    module = _fresh()
    _feed(module, [+1, -1] * (WINDOW // 2))
    _quotes_both(module.decide(_decision()))

    _feed(module, [+1] * WINDOW, start=0.50)
    action = module.decide(_decision())
    assert action.ask == "SHRINK"
    assert action.ask_scale == 0.01


def test_trend_that_turns_into_oscillation_quotes_both():
    module = _fresh()
    _feed(module, [+1] * WINDOW, start=0.30)
    assert module.decide(_decision()).ask == "SHRINK"

    _feed(module, [-1, +1] * (WINDOW // 2), start=0.60)
    _quotes_both(module.decide(_decision()))


def test_state_is_per_asset():
    module = _fresh()
    _feed(module, [+1, -1] * (WINDOW // 2), asset=ASSET)
    _feed(module, [+1] * WINDOW, asset=OTHER, start=0.30)

    _quotes_both(module.decide(_decision(ASSET)))
    assert module.decide(_decision(OTHER)).ask == "SHRINK"


def test_setup_clears_history():
    module = _fresh()
    _feed(module, [+1] * WINDOW, start=0.30)
    assert module.decide(_decision()).ask == "SHRINK"

    module.setup(None)
    _quotes_both(module.decide(_decision()))


def test_decide_does_not_consume_the_window():
    module = _fresh()
    _feed(module, [+1] * WINDOW, start=0.30)

    first = module.decide(_decision())
    second = module.decide(_decision())
    assert (first.bid, first.ask, first.ask_scale) == (
        second.bid,
        second.ask,
        second.ask_scale,
    )
