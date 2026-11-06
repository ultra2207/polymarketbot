"""Decision-logic tests for the two-sided-coupling module.

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
_idea = importlib.import_module("ideas.micro-two-sided-coupling.module")
FULL_SCALE = _idea.FULL_SCALE
SHRINK_SCALE = _idea.SHRINK_SCALE
WINDOW = _idea.WINDOW
MicroTwoSidedCoupling = _idea.MicroTwoSidedCoupling

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


def _decision(
    asset: str = ASSET,
    bid_score: float = 0.0,
    ask_score: float = 0.0,
) -> DecisionContext:
    return DecisionContext(
        book=_book(asset=asset),
        seconds_elapsed=0.0,
        inventory_shares=0.0,
        max_inventory_shares=100.0,
        # The baseline quotes inside the spread, so our own queue fields are
        # zero; a rule that needed them would never fire.
        bid_queue_ratio=0.0,
        ask_queue_ratio=0.0,
        bid_size_at_level=0.0,
        ask_size_at_level=0.0,
        ofi=None,
        bid_score=bid_score,
        ask_score=ask_score,
    )


def _fresh() -> MicroTwoSidedCoupling:
    module = MicroTwoSidedCoupling()
    module.setup(None)  # setup ignores the market; it only resets state
    return module


def _quotes_both(action) -> None:
    assert (action.bid, action.ask) == ("QUOTE", "QUOTE")
    assert action.bid_scale == FULL_SCALE
    assert action.ask_scale == FULL_SCALE


def _no_pull(action) -> None:
    assert action.bid != "PULL"
    assert action.ask != "PULL"
    assert 0.0 < action.bid_scale <= 1.0
    assert 0.0 < action.ask_scale <= 1.0


def test_defaults_describe_a_twenty_decision_window():
    assert WINDOW == 20
    assert SHRINK_SCALE == 0.01
    assert FULL_SCALE == 1.0


def test_warm_up_with_no_history_quotes_both_sides():
    module = _fresh()

    _quotes_both(module.decide(_decision()))


def test_warm_up_quotes_both_even_when_one_side_is_already_missing():
    # One observation is not evidence that the missing side is gone; do not
    # shrink the only side standing on the first decision.
    module = _fresh()

    action = module.decide(_decision(bid_score=1.0, ask_score=0.0))
    _quotes_both(action)


def test_both_sides_resting_quotes_both():
    module = _fresh()
    module.decide(_decision(bid_score=1.0, ask_score=1.0))  # warm-up

    _quotes_both(module.decide(_decision(bid_score=1.0, ask_score=1.0)))


def test_neither_side_resting_quotes_both_to_restore_the_pair():
    module = _fresh()
    module.decide(_decision(bid_score=1.0, ask_score=1.0))  # warm-up

    _quotes_both(module.decide(_decision(bid_score=0.0, ask_score=0.0)))


def test_missing_ask_with_score_history_restores_both_at_full_scale():
    module = _fresh()
    module.decide(_decision(bid_score=1.0, ask_score=1.0))  # ask scored

    action = module.decide(_decision(bid_score=1.0, ask_score=0.0))
    _quotes_both(action)
    _no_pull(action)


def test_missing_bid_with_score_history_restores_both_at_full_scale():
    module = _fresh()
    module.decide(_decision(bid_score=1.0, ask_score=1.0))  # bid scored

    action = module.decide(_decision(bid_score=0.0, ask_score=1.0))
    _quotes_both(action)
    _no_pull(action)


def test_missing_side_never_scored_shrinks_the_unpaired_bid():
    module = _fresh()
    module.decide(_decision(bid_score=1.0, ask_score=0.0))  # warm-up

    action = module.decide(_decision(bid_score=1.0, ask_score=0.0))
    assert action.bid == "SHRINK"
    assert action.bid_scale == SHRINK_SCALE
    assert action.ask == "QUOTE"
    assert action.ask_scale == FULL_SCALE
    _no_pull(action)


def test_missing_side_never_scored_shrinks_the_unpaired_ask():
    module = _fresh()
    module.decide(_decision(bid_score=0.0, ask_score=1.0))  # warm-up

    action = module.decide(_decision(bid_score=0.0, ask_score=1.0))
    assert action.ask == "SHRINK"
    assert action.ask_scale == SHRINK_SCALE
    assert action.bid == "QUOTE"
    assert action.bid_scale == FULL_SCALE
    _no_pull(action)


def test_a_side_scored_exactly_one_window_ago_is_still_restorable():
    module = _fresh()
    module.decide(_decision(bid_score=1.0, ask_score=1.0))  # decision 1: bid scored

    action = None
    for _ in range(WINDOW):  # decisions 2..21: bid missing
        action = module.decide(_decision(bid_score=0.0, ask_score=1.0))
    _quotes_both(action)  # the previous 20 decisions still include decision 1


def test_a_side_unscored_for_the_whole_window_cannot_be_restored():
    module = _fresh()
    module.decide(_decision(bid_score=1.0, ask_score=1.0))  # decision 1: bid scored
    for _ in range(WINDOW):
        module.decide(_decision(bid_score=0.0, ask_score=1.0))

    # Decision 22: the previous 20 decisions (2..21) all have bid_score == 0.
    action = module.decide(_decision(bid_score=0.0, ask_score=1.0))
    assert action.ask == "SHRINK"
    assert action.ask_scale == SHRINK_SCALE
    assert action.bid == "QUOTE"
    assert action.bid_scale == FULL_SCALE
    _no_pull(action)


def test_the_missing_side_is_always_quoted_at_full_scale():
    module = _fresh()
    module.decide(_decision(bid_score=1.0, ask_score=1.0))
    for _ in range(5):
        action = module.decide(_decision(bid_score=1.0, ask_score=0.0))
        assert action.ask == "QUOTE"
        assert action.ask_scale == FULL_SCALE
    for _ in range(5):
        action = module.decide(_decision(bid_score=0.0, ask_score=1.0))
        assert action.bid == "QUOTE"
        assert action.bid_scale == FULL_SCALE


def test_never_pulls_a_side():
    module = _fresh()
    for bid_score, ask_score in (
        (0.0, 0.0),
        (1.0, 1.0),
        (1.0, 0.0),
        (1.0, 0.0),
        (0.0, 1.0),
        (1.0, 1.0),
        (0.0, 0.0),
    ):
        action = module.decide(_decision(bid_score=bid_score, ask_score=ask_score))
        _no_pull(action)


def test_state_is_per_asset():
    module = _fresh()
    module.decide(_decision(asset=ASSET, bid_score=1.0, ask_score=0.0))
    unpaired = module.decide(_decision(asset=ASSET, bid_score=1.0, ask_score=0.0))
    assert unpaired.bid == "SHRINK"

    # OTHER has its own empty history, so the first decision there is warm-up.
    fresh_asset = module.decide(_decision(asset=OTHER, bid_score=1.0, ask_score=0.0))
    _quotes_both(fresh_asset)


def test_setup_clears_history():
    module = _fresh()
    module.decide(_decision(bid_score=1.0, ask_score=0.0))
    module.decide(_decision(bid_score=1.0, ask_score=0.0))

    module.setup(None)
    _quotes_both(module.decide(_decision(bid_score=1.0, ask_score=0.0)))
