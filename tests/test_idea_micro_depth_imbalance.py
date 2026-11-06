"""Decision-logic tests for the ``micro-depth-imbalance`` module.

Hand-built contexts only: no tape, no replay, no clock. The rule under test is

    di = signals.get("depth_imbalance")
    di >= +0.5  -> SHRINK ask at 0.01, QUOTE bid
    di <= -0.5  -> SHRINK bid at 0.01, QUOTE ask
    otherwise   -> QUOTE both

with a missing or non-finite signal returning QUOTE/QUOTE. That is the module's
inert state: the baseline quotes sit inside the spread, so the level sizes in
the context are zero and the harness supplies the touch-depth ratio as a
prepared signal; without ``prepare.sql`` there is no signal and no trade.

The tests also pin the reward-preserving property: no branch may return
``PULL`` or set a price. A shrink is floored by the engine at
``rewards_min_size`` and keeps scoring; a pull costs two-thirds of the side's
score under the venue normalisation and is never justified by this rule.
"""

from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The directory is hyphenated, so it is resolved the way the harness resolves it.
module = importlib.import_module("ideas.micro-depth-imbalance.module")
IMBALANCE_THRESHOLD = module.IMBALANCE_THRESHOLD
SHRINK_SCALE = module.SHRINK_SCALE
SIGNAL_NAME = module.SIGNAL_NAME
STRATEGY = module.STRATEGY
MicroDepthImbalance = module.MicroDepthImbalance

from polymarketbot.strategy.base import (  # noqa: E402
    BookContext,
    DecisionContext,
    MarketView,
    StrategyModule,
)

TICK = 0.01
MID = 0.50


@pytest.fixture()
def strategy() -> MicroDepthImbalance:
    return MicroDepthImbalance()


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
    *,
    depth_imbalance: object = None,
    include_signal: bool = True,
    extra_signals: dict[str, float] | None = None,
) -> DecisionContext:
    """One balanced top-of-book step with an optional prepared signal."""
    signals: dict[str, float] = {}
    if include_signal and depth_imbalance is not None:
        signals[SIGNAL_NAME] = depth_imbalance  # type: ignore[assignment]
    if extra_signals:
        signals.update(extra_signals)
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
        inventory_shares=0.0,
        max_inventory_shares=100.0,
        # Inside-spread baseline quotes: level sizes are genuinely zero.
        bid_queue_ratio=0.0,
        ask_queue_ratio=0.0,
        bid_size_at_level=0.0,
        ask_size_at_level=0.0,
        signals=signals,
    )


def test_strategy_singleton_implements_protocol() -> None:
    assert isinstance(STRATEGY, StrategyModule)
    assert STRATEGY.name == "micro-depth-imbalance"


def test_warm_up_after_setup_is_inert(strategy: MicroDepthImbalance) -> None:
    """No signal table yet: the first decision quotes both sides at full size.

    The module holds no warm-up state and no history: this is the deliberate
    inert state until ``prepare.sql`` materialises ``depth_imbalance``.
    """
    strategy.setup(_market())
    decision = strategy.decide(_ctx())
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0
    assert decision.bid_price is None
    assert decision.ask_price is None


def test_no_signal_at_all_quotes_both_sides(strategy: MicroDepthImbalance) -> None:
    decision = strategy.decide(_ctx())
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0


def test_signals_without_the_key_are_inert(strategy: MicroDepthImbalance) -> None:
    """An unrelated prepared column must not be read as a depth ratio."""
    decision = strategy.decide(_ctx(extra_signals={"ofi_60s": 0.9}))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0


def test_threshold_high_shrinks_the_ask(strategy: MicroDepthImbalance) -> None:
    """di = +0.5 is the boundary: the ask is thin, the bid keeps its reward."""
    decision = strategy.decide(_ctx(depth_imbalance=IMBALANCE_THRESHOLD))
    assert decision.ask == "SHRINK"
    assert decision.ask_scale == SHRINK_SCALE
    assert decision.bid == "QUOTE"
    assert decision.bid_scale == 1.0
    assert "ask thin" in decision.reason


def test_threshold_low_shrinks_the_bid(strategy: MicroDepthImbalance) -> None:
    """di = -0.5 is the boundary: the bid is thin, the ask keeps its reward."""
    decision = strategy.decide(_ctx(depth_imbalance=-IMBALANCE_THRESHOLD))
    assert decision.bid == "SHRINK"
    assert decision.bid_scale == SHRINK_SCALE
    assert decision.ask == "QUOTE"
    assert decision.ask_scale == 1.0
    assert "bid thin" in decision.reason


def test_weak_imbalance_keeps_both_sides(strategy: MicroDepthImbalance) -> None:
    """di = 0.2 is below the bar: no measured large risk, so no reward is lost."""
    decision = strategy.decide(_ctx(depth_imbalance=0.2))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0


def test_just_below_threshold_does_not_fire(strategy: MicroDepthImbalance) -> None:
    decision = strategy.decide(_ctx(depth_imbalance=0.4999))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    decision = strategy.decide(_ctx(depth_imbalance=-0.4999))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")


def test_strong_imbalance_still_shrinks_only_the_thin_side(
    strategy: MicroDepthImbalance,
) -> None:
    ask_side = strategy.decide(_ctx(depth_imbalance=0.95))
    assert (ask_side.bid, ask_side.ask) == ("QUOTE", "SHRINK")
    assert ask_side.ask_scale == SHRINK_SCALE

    bid_side = strategy.decide(_ctx(depth_imbalance=-0.95))
    assert (bid_side.bid, bid_side.ask) == ("SHRINK", "QUOTE")
    assert bid_side.bid_scale == SHRINK_SCALE


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf, "junk", None])
def test_unusable_signal_value_is_inert(
    strategy: MicroDepthImbalance, bad_value: object
) -> None:
    """A NaN/Inf/garbage signal names no thin side, so both sides are quoted."""
    decision = strategy.decide(_ctx(depth_imbalance=bad_value))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0


def test_never_pulls_never_prices_across_the_signal_range(
    strategy: MicroDepthImbalance,
) -> None:
    """Reward preservation: the worst branch is a floored shrink, never a pull."""
    for di in (-1.0, -0.75, -0.5, -0.2, 0.0, 0.2, 0.5, 0.75, 1.0):
        decision = strategy.decide(_ctx(depth_imbalance=di))
        assert decision.bid in ("QUOTE", "SHRINK")
        assert decision.ask in ("QUOTE", "SHRINK")
        assert decision.bid_price is None
        assert decision.ask_price is None
        for scale in (decision.bid_scale, decision.ask_scale):
            assert scale == 1.0 or scale == SHRINK_SCALE


def test_decisions_do_not_carry_state_between_steps(
    strategy: MicroDepthImbalance,
) -> None:
    """A fired step must not make the next signal-less step defensive."""
    fired = strategy.decide(_ctx(depth_imbalance=1.0))
    assert fired.ask == "SHRINK"
    after = strategy.decide(_ctx())
    assert (after.bid, after.ask) == ("QUOTE", "QUOTE")
    assert after.bid_scale == 1.0
    assert after.ask_scale == 1.0
