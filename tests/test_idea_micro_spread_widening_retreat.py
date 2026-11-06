"""Decision-logic tests for the spread-widening retreat module.

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
_idea = importlib.import_module("ideas.micro-spread-widening-retreat.module")
COOLDOWN_DECISIONS = _idea.COOLDOWN_DECISIONS
MIN_OBSERVATIONS = _idea.MIN_OBSERVATIONS
SpreadWideningRetreat = _idea.SpreadWideningRetreat

from polymarketbot.strategy.base import (  # noqa: E402
    BookContext,
    DecisionContext,
)

ASSET = "asset-yes"
OTHER = "asset-no"
QUIET_MID = 0.45
QUIET_SPREAD = 2.0
TICK = 0.01


def _book(
    *,
    asset: str = ASSET,
    mid: float = QUIET_MID,
    spread_cents: float = QUIET_SPREAD,
    tick: float = TICK,
) -> BookContext:
    half = (spread_cents / 100.0) / 2.0
    return BookContext(
        condition_id="cond",
        asset_id=asset,
        outcome="YES",
        mid=mid,
        best_bid=mid - half,
        best_ask=mid + half,
        tick_size=tick,
        spread_cents=spread_cents,
    )


def _decision(**book) -> DecisionContext:
    return DecisionContext(
        book=_book(**book),
        seconds_elapsed=0.0,
        inventory_shares=0.0,
        max_inventory_shares=100.0,
        bid_queue_ratio=0.0,
        ask_queue_ratio=0.0,
        bid_size_at_level=0.0,
        ask_size_at_level=0.0,
        ofi=None,
        signals={},
    )


def _fresh() -> SpreadWideningRetreat:
    module = SpreadWideningRetreat()
    module.setup(None)  # setup only clears per-market state
    return module


def _assert_quotes(action) -> None:
    assert (action.bid, action.ask) == ("QUOTE", "QUOTE")
    assert action.bid_scale == 1.0
    assert action.ask_scale == 1.0


def _assert_pulls(action) -> None:
    assert (action.bid, action.ask) == ("PULL", "PULL")
    assert action.bid_scale == 0.0
    assert action.ask_scale == 0.0


def _warm(module: SpreadWideningRetreat, calls: int = MIN_OBSERVATIONS) -> None:
    for _ in range(calls):
        _assert_quotes(module.decide(_decision()))


def test_warm_up_quotes_both_sides():
    module = _fresh()
    for _ in range(MIN_OBSERVATIONS - 1):
        _assert_quotes(module.decide(_decision()))

    # Still one observation short of a reference, so even a 20c book is a quote.
    _assert_quotes(module.decide(_decision(spread_cents=20.0)))


def test_flat_mid_widening_opens_a_five_call_pull_window():
    module = _fresh()
    _warm(module)

    for _ in range(COOLDOWN_DECISIONS):
        _assert_pulls(module.decide(_decision(spread_cents=4.0)))


@pytest.mark.parametrize("spread", [3.0, 3.5, 3.99])
def test_widening_shy_of_two_ticks_does_not_pull(spread):
    module = _fresh()
    _warm(module)

    _assert_quotes(module.decide(_decision(spread_cents=spread)))


@pytest.mark.parametrize(
    "moved_mid", [QUIET_MID + TICK, QUIET_MID - TICK, QUIET_MID + 3 * TICK]
)
def test_widening_with_a_moved_mid_does_not_pull(moved_mid):
    module = _fresh()
    _warm(module)

    for _ in range(COOLDOWN_DECISIONS + 1):
        _assert_quotes(module.decide(_decision(spread_cents=4.0, mid=moved_mid)))


def test_half_tick_drift_is_still_flat():
    module = _fresh()
    _warm(module)

    _assert_pulls(module.decide(_decision(spread_cents=4.0, mid=QUIET_MID + TICK / 2)))


def test_window_closes_after_exactly_five_pulls():
    module = _fresh()
    _warm(module)

    for _ in range(COOLDOWN_DECISIONS):
        _assert_pulls(module.decide(_decision(spread_cents=4.0)))

    # Still wide, still flat: the episode was already acted on, so quote again.
    _assert_quotes(module.decide(_decision(spread_cents=4.0)))
    _assert_quotes(module.decide(_decision(spread_cents=4.0)))


def test_return_to_median_ends_the_window_early_and_rearms():
    module = _fresh()
    _warm(module)

    _assert_pulls(module.decide(_decision(spread_cents=4.0)))
    # Back at the median before the window expires: resume quoting now.
    _assert_quotes(module.decide(_decision()))

    # A later widening is a new episode and gets its own full window.
    for _ in range(COOLDOWN_DECISIONS):
        _assert_pulls(module.decide(_decision(spread_cents=4.0)))


@pytest.mark.parametrize(
    ("tick", "spread", "pulls"),
    [
        (0.01, 4.0, True),
        (0.01, 3.9, False),
        (0.001, 2.3, True),
        (0.001, 2.1, False),
    ],
)
def test_threshold_scales_with_the_tick(tick, spread, pulls):
    module = _fresh()
    for _ in range(MIN_OBSERVATIONS):
        _assert_quotes(module.decide(_decision(tick=tick)))

    action = module.decide(_decision(tick=tick, spread_cents=spread))
    if pulls:
        _assert_pulls(action)
    else:
        _assert_quotes(action)


def test_the_window_is_per_asset():
    module = _fresh()
    _warm(module)  # only ASSET has a spread reference

    # OTHER has no history, so its wide book is still warm-up.
    _assert_quotes(module.decide(_decision(asset=OTHER, spread_cents=8.0)))
    _assert_pulls(module.decide(_decision(spread_cents=4.0)))
