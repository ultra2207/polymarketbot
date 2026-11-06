"""Decision-logic tests for the competition-dilution module.

Hand-built ``DecisionContext``s only: no tape, no replay, no network.  The
module needs one prior decision to leave warm-up, so every firing case is
exercised on the second decision of the same asset.
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
_idea = importlib.import_module("ideas.micro-competition-dilution.module")
FULL_SCALE = _idea.FULL_SCALE
SHRINK_SCALE = _idea.SHRINK_SCALE
SHRINK_MULTIPLE = _idea.SHRINK_MULTIPLE
RATE_PER_DAY_AT_FULL_TARGET = _idea.RATE_PER_DAY_AT_FULL_TARGET
MIN_TARGET_SHARE = _idea.MIN_TARGET_SHARE
MAX_TARGET_SHARE = _idea.MAX_TARGET_SHARE
MicroCompetitionDilution = _idea.MicroCompetitionDilution
target_share = _idea.target_share

from polymarketbot.strategy.base import (  # noqa: E402
    BookContext,
    DecisionContext,
    StrategyModule,
)

ASSET = "asset-yes"
OTHER = "asset-no"
TICK = 0.01


def _book(asset: str = ASSET, competing_score: float = 0.0) -> BookContext:
    mid = 0.50
    return BookContext(
        condition_id="cond",
        asset_id=asset,
        outcome="YES" if asset == ASSET else "NO",
        mid=mid,
        best_bid=round(mid - TICK / 2.0, 4),
        best_ask=round(mid + TICK / 2.0, 4),
        tick_size=TICK,
        spread_cents=1.0,
        competing_score=competing_score,
    )


def _decision(
    *,
    asset: str = ASSET,
    rate_per_day: float = 0.0,
    competing_score: float = 0.0,
    bid_score: float = 0.0,
    ask_score: float = 0.0,
) -> DecisionContext:
    return DecisionContext(
        book=_book(asset, competing_score=competing_score),
        seconds_elapsed=12.0,
        inventory_shares=0.0,
        max_inventory_shares=100.0,
        # The baseline quotes inside the spread, so our own queue fields are
        # zero; the rule deliberately never reads them.
        bid_queue_ratio=0.0,
        ask_queue_ratio=0.0,
        bid_size_at_level=0.0,
        ask_size_at_level=0.0,
        ofi=None,
        bid_score=bid_score,
        ask_score=ask_score,
        rate_per_day=rate_per_day,
    )


def _fresh() -> MicroCompetitionDilution:
    module = MicroCompetitionDilution()
    module.setup(None)  # setup only resets the warm-up state
    return module


def _warmed(module: MicroCompetitionDilution, asset: str = ASSET) -> None:
    """Spend the first decision on a neutral context so the next one decides."""
    module.decide(_decision(asset=asset))


def _quotes_both(action) -> None:
    assert (action.bid, action.ask) == ("QUOTE", "QUOTE")
    assert action.bid_scale == FULL_SCALE
    assert action.ask_scale == FULL_SCALE


def _shrinks_both_half(action) -> None:
    assert (action.bid, action.ask) == ("SHRINK", "SHRINK")
    assert action.bid_scale == SHRINK_SCALE
    assert action.ask_scale == SHRINK_SCALE


def _no_pull(action) -> None:
    assert action.bid != "PULL"
    assert action.ask != "PULL"
    assert 0.0 < action.bid_scale <= 1.0
    assert 0.0 < action.ask_scale <= 1.0


def test_module_implements_the_protocol():
    assert isinstance(_fresh(), StrategyModule)


def test_defaults_pin_the_stated_prior():
    assert FULL_SCALE == 1.0
    assert SHRINK_SCALE == 0.5
    assert SHRINK_MULTIPLE == 1.5
    assert RATE_PER_DAY_AT_FULL_TARGET == 300.0
    assert MIN_TARGET_SHARE == 0.10
    assert MAX_TARGET_SHARE == 0.60


def test_target_share_hits_its_floor_endpoint():
    # A market paying nothing, and one paying a fifth of the divisor, both sit
    # at the 10 % floor rather than chasing a share nobody values.
    assert target_share(0.0) == MIN_TARGET_SHARE
    assert target_share(15.0) == MIN_TARGET_SHARE
    assert target_share(30.0) == MIN_TARGET_SHARE


def test_target_share_hits_its_cap_endpoint():
    assert target_share(180.0) == MAX_TARGET_SHARE
    assert target_share(300.0) == MAX_TARGET_SHARE
    assert target_share(100_000.0) == MAX_TARGET_SHARE


def test_target_share_is_linear_inside_the_band():
    assert target_share(75.0) == 0.25
    assert target_share(150.0) == 0.50


def test_warm_up_quotes_both_even_when_the_rule_would_shrink():
    module = _fresh()
    action = module.decide(
        _decision(rate_per_day=0.0, competing_score=1.0, bid_score=5.0, ask_score=4.0)
    )
    # share = 0.9 against a 0.10 target: the rule would dilute, but one
    # decision is not evidence that this module priced the quote.
    _quotes_both(action)
    assert "warm-up" in action.reason


def test_no_scores_yet_quotes_both():
    module = _fresh()
    _warmed(module)
    action = module.decide(
        _decision(rate_per_day=0.0, competing_score=0.0, bid_score=0.0, ask_score=0.0)
    )
    _quotes_both(action)
    assert "no scores" in action.reason


def test_no_scores_yet_is_inert_even_in_a_high_rate_market():
    module = _fresh()
    _warmed(module)
    action = module.decide(
        _decision(rate_per_day=1_000.0, competing_score=0.0, bid_score=0.0, ask_score=0.0)
    )
    _quotes_both(action)


def test_low_share_in_a_high_rate_market_quotes_both():
    # Target 0.60 at >= $180/day; share = 10 / (10 + 90) = 0.10.
    module = _fresh()
    _warmed(module)
    action = module.decide(
        _decision(rate_per_day=300.0, competing_score=90.0, bid_score=6.0, ask_score=4.0)
    )
    _quotes_both(action)
    assert "buy share" in action.reason


def test_share_far_above_target_shrinks_both_to_half():
    # Target 0.10 at $30/day; share = 9 / (9 + 1) = 0.90 > 0.15.
    module = _fresh()
    _warmed(module)
    action = module.decide(
        _decision(rate_per_day=30.0, competing_score=1.0, bid_score=5.0, ask_score=4.0)
    )
    _shrinks_both_half(action)
    _no_pull(action)


def test_share_inside_the_band_between_target_and_multiple_quotes_both():
    # Target 0.60 at $300/day, 1.5x = 0.90; share = 8 / (8 + 2) = 0.80.
    module = _fresh()
    _warmed(module)
    action = module.decide(
        _decision(rate_per_day=300.0, competing_score=2.0, bid_score=5.0, ask_score=3.0)
    )
    _quotes_both(action)
    assert "near target" in action.reason


def test_share_exactly_at_target_quotes_both():
    module = _fresh()
    _warmed(module)
    action = module.decide(
        _decision(rate_per_day=300.0, competing_score=4.0, bid_score=3.0, ask_score=3.0)
    )
    _quotes_both(action)


def test_a_high_rate_market_tolerates_a_higher_share_before_diluting():
    module = _fresh()
    _warmed(module)
    # Same book twice: at $30/day (target 0.10) a 0.60 share dilutes, at
    # $300/day (target 0.60) it does not. Only the rate changed.
    cheap = _decision(rate_per_day=30.0, competing_score=4.0, bid_score=3.0, ask_score=3.0)
    rich = _decision(rate_per_day=300.0, competing_score=4.0, bid_score=3.0, ask_score=3.0)
    _shrinks_both_half(module.decide(cheap))
    rich_module = _fresh()
    _warmed(rich_module)
    _quotes_both(rich_module.decide(rich))


def test_one_sided_score_counts_toward_our_share():
    module = _fresh()
    _warmed(module)
    # Bid only, share = 6 / (6 + 4) = 0.60; target 0.10 -> dilute.
    action = module.decide(
        _decision(rate_per_day=0.0, competing_score=4.0, bid_score=6.0, ask_score=0.0)
    )
    _shrinks_both_half(action)


def test_never_pulls_a_side():
    module = _fresh()
    cases = [
        _decision(rate_per_day=0.0, competing_score=0.0),
        _decision(rate_per_day=0.0, competing_score=1.0, bid_score=9.0),
        _decision(rate_per_day=0.0, competing_score=9.0, bid_score=1.0),
        _decision(rate_per_day=500.0, competing_score=5.0, bid_score=5.0),
        _decision(rate_per_day=30.0, competing_score=1.0, bid_score=5.0, ask_score=4.0),
    ]
    for ctx in cases:
        _no_pull(module.decide(ctx))


def test_warm_up_is_per_asset():
    module = _fresh()
    _warmed(module, ASSET)
    other = module.decide(
        _decision(asset=OTHER, rate_per_day=0.0, competing_score=1.0, bid_score=9.0)
    )
    _quotes_both(other)


def test_setup_resets_the_warm_up_guard():
    module = _fresh()
    _warmed(module)
    _shrinks_both_half(
        module.decide(_decision(rate_per_day=0.0, competing_score=1.0, bid_score=9.0))
    )
    module.setup(None)
    _quotes_both(
        module.decide(_decision(rate_per_day=0.0, competing_score=1.0, bid_score=9.0))
    )
