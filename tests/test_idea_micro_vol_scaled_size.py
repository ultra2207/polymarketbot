"""Decision-logic tests for the ``micro-vol-scaled-size`` module.

Hand-built contexts only: no tape, no replay, no clock. Each case feeds a known
mid path into ``decide`` and asserts the returned scale, so the rule
``scale = clamp(1 tick / sigma, 0.25, 1)`` is proven rather than observed.
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
module = importlib.import_module("ideas.micro-vol-scaled-size.module")
MIN_OBSERVATIONS = module.MIN_OBSERVATIONS
MIN_SCALE = module.MIN_SCALE
STRATEGY = module.STRATEGY
TARGET_SIGMA = module.TARGET_SIGMA
WINDOW = module.WINDOW
VolScaledSize = module.VolScaledSize

from polymarketbot.strategy.base import (  # noqa: E402
    BookContext,
    DecisionContext,
    MarketView,
    StrategyModule,
)

TICK = 0.01
MID = 0.50


@pytest.fixture()
def strategy() -> VolScaledSize:
    return VolScaledSize()


def _ctx(mid: float, tick: float = TICK) -> DecisionContext:
    book = BookContext(
        condition_id="cond-1",
        asset_id="asset-1",
        outcome="YES",
        mid=mid,
        best_bid=round(mid - TICK / 2.0, 4),
        best_ask=round(mid + TICK / 2.0, 4),
        tick_size=tick,
        spread_cents=TICK * 100.0,
    )
    return DecisionContext(
        book=book,
        seconds_elapsed=0.0,
        inventory_shares=0.0,
        max_inventory_shares=100.0,
        bid_queue_ratio=0.0,
        ask_queue_ratio=0.0,
        bid_size_at_level=0.0,
        ask_size_at_level=0.0,
    )


def _walk(start: float, deltas_ticks: list[float], tick: float = TICK) -> list[float]:
    """Mid path from a list of tick-to-tick changes, in ticks."""
    mids = [round(start, 4)]
    for delta in deltas_ticks:
        mids.append(round(mids[-1] + delta * tick, 4))
    return mids


def _feed(strategy: VolScaledSize, mids: list[float], tick: float = TICK):
    decision = None
    for mid in mids:
        decision = strategy.decide(_ctx(mid, tick))
    assert decision is not None
    return decision


def _alternating(step_ticks: float, count: int) -> list[float]:
    """Changes of +step, -step, +step, ... (``count`` distinct mids)."""
    return _walk(MID, [step_ticks if i % 2 == 0 else -step_ticks for i in range(count - 1)])


def test_strategy_singleton_implements_protocol() -> None:
    assert isinstance(STRATEGY, StrategyModule)
    assert STRATEGY.name == "micro-vol-scaled-size"


def test_warm_up_quotes_at_full_scale(strategy: VolScaledSize) -> None:
    """Fewer than 30 observations: both sides at full size, even if volatile."""
    assert MIN_OBSERVATIONS == 30
    mids = _alternating(5.0, MIN_OBSERVATIONS - 1)
    decision = _feed(strategy, mids)
    assert decision.bid == "QUOTE"
    assert decision.ask == "QUOTE"
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0
    assert "warm-up" in decision.reason


def test_first_observation_is_warm_up(strategy: VolScaledSize) -> None:
    decision = strategy.decide(_ctx(MID))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert (decision.bid_scale, decision.ask_scale) == (1.0, 1.0)


def test_idle_book_stays_at_full_scale(strategy: VolScaledSize) -> None:
    """An unchanged mid is not a new observation and is not a zero change."""
    decision = _feed(strategy, [MID] * 200)
    assert (decision.bid_scale, decision.ask_scale) == (1.0, 1.0)
    assert "warm-up" in decision.reason


def test_quiet_series_yields_full_scale(strategy: VolScaledSize) -> None:
    """Half-tick jitter: sigma < 1 tick, so the ratio clamps to 1."""
    mids = _alternating(0.5, MIN_OBSERVATIONS)
    decision = _feed(strategy, mids)
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0
    assert "sigma=0.50t" in decision.reason


def test_one_tick_series_is_neutral(strategy: VolScaledSize) -> None:
    """sigma == 1 tick is the neutral point: target / sigma == 1."""
    decision = _feed(strategy, _alternating(TARGET_SIGMA, MIN_OBSERVATIONS))
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0


def test_volatile_series_shrinks_below_one(strategy: VolScaledSize) -> None:
    """Three-tick moves: scale is 1/3, strictly below 1 and above the floor."""
    decision = _feed(strategy, _alternating(3.0, MIN_OBSERVATIONS))
    assert decision.bid == "QUOTE"
    assert decision.ask == "QUOTE"
    assert decision.bid_scale < 1.0
    assert decision.bid_scale >= MIN_SCALE
    assert decision.bid_scale == pytest.approx(1.0 / 3.0, abs=1e-3)


def test_extreme_volatility_hits_the_floor_and_never_below(strategy: VolScaledSize) -> None:
    decision = _feed(strategy, _alternating(40.0, MIN_OBSERVATIONS))
    assert decision.bid_scale == MIN_SCALE
    assert decision.ask_scale == MIN_SCALE
    assert decision.bid_scale >= 0.25


def test_both_sides_always_share_one_scale(strategy: VolScaledSize) -> None:
    for step in (0.25, 0.5, 1.0, 2.0, 7.0, 30.0):
        decision = _feed(VolScaledSize(), _alternating(step, MIN_OBSERVATIONS))
        assert decision.bid_scale == decision.ask_scale
        assert 0.0 < decision.bid_scale <= 1.0


def test_module_never_pulls_across_regimes() -> None:
    for step in (0.0, 0.5, 1.0, 3.0, 40.0):
        decision = _feed(VolScaledSize(), _alternating(step, MIN_OBSERVATIONS))
        assert decision.bid == "QUOTE"
        assert decision.ask == "QUOTE"
        assert decision.bid_scale > 0.0
        assert decision.ask_scale > 0.0


def test_on_book_and_decide_do_not_double_count(strategy: VolScaledSize) -> None:
    """The engine calls ``on_book`` then ``decide`` on the same update."""
    mids = _alternating(3.0, MIN_OBSERVATIONS)
    for mid in mids:
        strategy.on_book(_ctx(mid).book)
        decision = strategy.decide(_ctx(mid))
    expected = _feed(VolScaledSize(), mids)
    assert decision.bid_scale == expected.bid_scale
    assert decision.ask_scale == expected.ask_scale
    assert decision.bid_scale == pytest.approx(1.0 / 3.0, abs=1e-3)


def test_window_is_bounded(strategy: VolScaledSize) -> None:
    """Only the recent path matters: a violent past ages out of the window."""
    viol = _alternating(30.0, MIN_OBSERVATIONS)
    shrunk = _feed(VolScaledSize(), viol)
    assert shrunk.bid_scale < 1.0

    calm = _alternating(0.5, WINDOW + 30)
    decision = _feed(strategy, viol + calm)
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0


def test_setup_resets_state(strategy: VolScaledSize) -> None:
    _feed(strategy, _alternating(3.0, MIN_OBSERVATIONS))
    market = MarketView(
        condition_id="cond-1",
        question="will it?",
        rate_per_day=20.0,
        rewards_min_size=10.0,
        rewards_max_spread=3.0,
        tokens=("asset-1",),
    )
    strategy.setup(market)
    decision = strategy.decide(_ctx(MID))
    assert (decision.bid_scale, decision.ask_scale) == (1.0, 1.0)
    assert "warm-up" in decision.reason
