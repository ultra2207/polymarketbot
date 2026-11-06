"""Decision-logic tests for the ``micro-tail-price-bps`` module.

Hand-built contexts only: no tape, no replay, no clock. The rule is stateless
price geometry, so every case is a single ``decide`` call and the assertions are
about the returned scale on both sides, not about a buffered path.

Proved here:

* the firing case at a tail mid (0.90 and 0.10 -> the 0.25 floor on both sides);
* the non-firing case at the neutral mid (0.50 -> 1.0 on both sides);
* the warm-up case: a brand-new instance fires on its *first* call, because the
  rule keeps no state and has no warm-up window;
* symmetry about 0.50 and the never-``PULL`` guarantee across the whole range.
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
module = importlib.import_module("ideas.micro-tail-price-bps.module")
MIN_SCALE = module.MIN_SCALE
STRATEGY = module.STRATEGY
TailPriceSize = module.TailPriceSize

from polymarketbot.strategy.base import (  # noqa: E402
    BookContext,
    DecisionContext,
    MarketView,
    StrategyModule,
)

TICK = 0.01
NEUTRAL_MID = 0.50


@pytest.fixture()
def strategy() -> TailPriceSize:
    return TailPriceSize()


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


def _market() -> MarketView:
    return MarketView(
        condition_id="cond-1",
        question="Will it?",
        rate_per_day=10.0,
        rewards_min_size=100.0,
        rewards_max_spread=0.03,
        tokens=("asset-1",),
    )


def test_strategy_singleton_implements_protocol() -> None:
    assert isinstance(STRATEGY, StrategyModule)
    assert STRATEGY.name == "micro-tail-price-bps"


def test_non_firing_mid_quotes_full_size_both_sides(strategy: TailPriceSize) -> None:
    """mid 0.50 is the neutral point: the rule does not shrink anything."""
    decision = strategy.decide(_ctx(NEUTRAL_MID))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0
    assert "scale=1.00" in decision.reason


def test_firing_high_tail_hits_the_floor_both_sides(strategy: TailPriceSize) -> None:
    """mid 0.90: 1 - |1.8 - 1| = 0.20, clamped to the 0.25 floor on both sides."""
    decision = strategy.decide(_ctx(0.90))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == pytest.approx(MIN_SCALE)
    assert decision.ask_scale == pytest.approx(MIN_SCALE)
    assert "tail=0.80" in decision.reason


def test_firing_low_tail_hits_the_floor_both_sides(strategy: TailPriceSize) -> None:
    """mid 0.10 is the mirror image: same 0.25 floor, not a pull."""
    decision = strategy.decide(_ctx(0.10))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == pytest.approx(MIN_SCALE)
    assert decision.ask_scale == pytest.approx(MIN_SCALE)
    assert "tail=0.80" in decision.reason


def test_scale_is_symmetric_about_the_mid() -> None:
    """mid m and 1 - m are the same distance from 0.50 and must agree exactly."""
    probe = TailPriceSize()
    for mid in (0.02, 0.10, 0.20, 0.25, 0.35, 0.45, 0.50, 0.55, 0.65, 0.75, 0.80, 0.90, 0.98):
        loos = probe.decide(_ctx(mid))
        symmetric = probe.decide(_ctx(round(1.0 - mid, 4)))
        assert loos.bid_scale == pytest.approx(symmetric.bid_scale)
        assert loos.ask_scale == pytest.approx(symmetric.ask_scale)
        assert loos.bid_scale == pytest.approx(loos.ask_scale)


def test_scale_falls_monotonically_away_from_the_mid() -> None:
    """Inside 0.20-0.80 the scale is linear in |2m - 1|; outside it is the floor."""
    probe = TailPriceSize()
    previous = 1.0
    for mid in (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.98):
        scale = probe.decide(_ctx(mid)).bid_scale
        assert scale <= previous + 1e-12
        assert MIN_SCALE <= scale <= 1.0
        previous = scale


def test_equal_priced_midpoints_have_equal_share_cost() -> None:
    """Halfway out (mid 0.25) the rule halves size: 1 - |0.5 - 1| = 0.5."""
    decision = TailPriceSize().decide(_ctx(0.25))
    assert decision.bid_scale == pytest.approx(0.5)
    assert decision.ask_scale == pytest.approx(0.5)


def test_warm_up_is_irrelevant_on_a_first_call(strategy: TailPriceSize) -> None:
    """No buffer: the very first decide already fires, with no setup or on_book."""
    decision = strategy.decide(_ctx(0.90))
    assert decision.bid_scale == pytest.approx(MIN_SCALE)
    assert "warm-up" not in decision.reason


def test_setup_and_book_updates_do_not_change_the_rule(strategy: TailPriceSize) -> None:
    """The rule reads only the current mid, so state changes cannot gate it."""
    strategy.setup(_market())
    strategy.on_book(_ctx(0.50).book)
    strategy.on_book(_ctx(0.92).book)
    # ... the mid is back inside the tails: the rule follows the *current* book.
    assert strategy.decide(_ctx(0.50)).bid_scale == 1.0
    assert strategy.decide(_ctx(0.92)).bid_scale == pytest.approx(MIN_SCALE)


def test_never_pulls_and_never_returns_zero_across_the_range() -> None:
    """The claim is about size per dollar: every mid keeps both sides quoting."""
    probe = TailPriceSize()
    for mid in (0.001, 0.01, 0.05, 0.10, 0.20, 0.50, 0.80, 0.90, 0.95, 0.99, 0.999):
        decision = probe.decide(_ctx(mid))
        assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
        assert decision.bid_scale > 0.0
        assert decision.ask_scale > 0.0
        assert decision.bid_scale >= MIN_SCALE
        assert decision.ask_scale >= MIN_SCALE


def test_non_finite_mid_falls_back_to_full_size() -> None:
    """A broken book must not raise in SideActions or silently zero the quotes."""
    decision = TailPriceSize().decide(_ctx(float("nan")))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0
