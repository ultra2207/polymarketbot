"""Decision-logic tests for the cross-token substitution module.

Hand-built ``DecisionContext``s cover the three states the rule distinguishes:
no prepared signal (warm-up, must be the baseline), a pulled sibling (the other
token is not carrying the pair), and a threatened sibling (mirror the
protection without ever putting both ends of a Q pair at the floor).
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
_idea = importlib.import_module("ideas.micro-cross-token-substitution.module")
SHRINK_SCALE = _idea.SHRINK_SCALE
CrossTokenSubstitution = _idea.CrossTokenSubstitution

from polymarketbot.strategy.base import (  # noqa: E402
    BookContext,
    DecisionContext,
    MarketView,
    StrategyModule,
    TradeContext,
)

ASSET = "asset-yes"
OTHER = "asset-no"
MID = 0.45
TICK = 0.01


def _book(asset: str = ASSET) -> BookContext:
    return BookContext(
        condition_id="cond",
        asset_id=asset,
        outcome="YES" if asset == ASSET else "NO",
        mid=MID,
        best_bid=MID - TICK / 2.0,
        best_ask=MID + TICK / 2.0,
        tick_size=TICK,
        spread_cents=1.0,
    )


def _decision(
    *,
    asset: str = ASSET,
    ofi: float | None = None,
    bid_score: float = 0.0,
    ask_score: float = 0.0,
    bid_ratio: float = 0.0,
    ask_ratio: float = 0.0,
    signals: dict[str, float] | None = None,
) -> DecisionContext:
    return DecisionContext(
        book=_book(asset),
        seconds_elapsed=12.0,
        inventory_shares=0.0,
        max_inventory_shares=100.0,
        bid_queue_ratio=bid_ratio,
        ask_queue_ratio=ask_ratio,
        bid_size_at_level=0.0,
        ask_size_at_level=0.0,
        ofi=ofi,
        bid_score=bid_score,
        ask_score=ask_score,
        rate_per_day=100.0,
        signals=dict(signals or {}),
    )


def _fresh() -> CrossTokenSubstitution:
    module = CrossTokenSubstitution()
    module.setup(None)  # stateless: setup is only a protocol hook
    return module


def _assert_full(action) -> None:
    assert action.bid == "QUOTE" and action.ask == "QUOTE"
    assert action.bid_scale == 1.0
    assert action.ask_scale == 1.0


def _assert_shrunk(side: str, action) -> None:
    """Exactly ``side`` is at the reward floor; the other side is untouched."""
    other = "ask" if side == "bid" else "bid"
    assert getattr(action, side) == "SHRINK"
    assert getattr(action, f"{side}_scale") == SHRINK_SCALE
    assert getattr(action, other) == "QUOTE"
    assert getattr(action, f"{other}_scale") == 1.0


def test_warm_up_without_signals_quotes_both_sides():
    """No prepared signal: degrade to the baseline, do not guess a sibling."""
    action = _fresh().decide(_decision())
    _assert_full(action)


def test_warm_up_ignores_a_missing_threat_key():
    action = _fresh().decide(_decision(signals={"sibling_scale": 1.0}))
    _assert_full(action)


def test_sibling_pulled_and_unscored_side_quotes_at_full_scale():
    """The sibling is out; with no own score there is nothing to protect."""
    action = _fresh().decide(_decision(signals={"sibling_scale": 0.0}))
    _assert_full(action)


def test_sibling_pulled_substitutes_only_the_unscored_side():
    """Scored side mirrors the threat; the unscored side carries the pair."""
    action = _fresh().decide(
        _decision(
            ofi=1.5,  # the ask is this token's threatened side
            bid_score=0.0,
            ask_score=0.4,
            signals={"sibling_scale": 0.0, "sibling_threat": 1.0},
        )
    )
    # Bid: pulled sibling + own score 0 -> rule 1 beats rule 3.
    assert action.bid == "QUOTE" and action.bid_scale == 1.0
    # Ask: own threat + sibling threat -> rule 2 keeps it quoted too.
    assert action.ask == "QUOTE" and action.ask_scale == 1.0


def test_sibling_threat_without_own_threat_shrinks_to_the_reward_floor():
    action = _fresh().decide(_decision(signals={"sibling_threat": 1.0}))
    assert action.bid == "SHRINK" and action.bid_scale == SHRINK_SCALE
    assert action.ask == "SHRINK" and action.ask_scale == SHRINK_SCALE
    assert action.bid_scale > 0.0 and action.ask_scale > 0.0  # never a pull


@pytest.mark.parametrize("ofi, threatened", [(1.5, "ask"), (-1.5, "bid")])
def test_both_ends_of_the_pair_are_never_at_the_floor(ofi, threatened):
    """Sibling threatened *and* this side threatened -> this side stays full."""
    action = _fresh().decide(_decision(ofi=ofi, signals={"sibling_threat": 1.0}))
    assert getattr(action, threatened) == "QUOTE"
    assert getattr(action, f"{threatened}_scale") == 1.0
    _assert_shrunk("bid" if threatened == "ask" else "ask", action)
    # The pair-level invariant the rule exists for.
    assert not (action.bid_scale == 0.0 and action.ask_scale == 0.0)
    assert "QUOTE" in (action.bid, action.ask)


def test_both_sides_threatened_keeps_both_quoted():
    action = _fresh().decide(
        _decision(
            bid_ratio=0.9,
            ask_ratio=0.9,
            signals={"sibling_threat": 1.0},
        )
    )
    _assert_full(action)


def test_own_threat_alone_never_shrinks():
    """Nothing is mirrored when the sibling is not threatened."""
    action = _fresh().decide(
        _decision(ofi=2.0, bid_ratio=0.9, ask_ratio=0.9, signals={"sibling_threat": 0.0})
    )
    _assert_full(action)


def test_queue_ratio_is_enough_to_name_the_threatened_side():
    """The rule does not depend on OFI: a deep queue names the side too."""
    action = _fresh().decide(
        _decision(ofi=None, ask_ratio=0.85, signals={"sibling_threat": 1.0})
    )
    assert action.ask == "QUOTE" and action.ask_scale == 1.0
    _assert_shrunk("bid", action)


def test_pulled_sibling_wins_over_the_threat_when_this_side_is_unscored():
    action = _fresh().decide(
        _decision(
            ofi=-1.5,
            bid_score=0.0,
            ask_score=0.0,
            signals={"sibling_scale": 0.0, "sibling_threat": 1.0},
        )
    )
    _assert_full(action)


def test_a_zero_scale_is_never_returned():
    """Every state keeps both quotes alive; only the scale moves."""
    cases = [
        _decision(),
        _decision(signals={"sibling_scale": 0.0}),
        _decision(signals={"sibling_threat": 1.0}),
        _decision(ofi=1.0, signals={"sibling_threat": 1.0}),
        _decision(ofi=-1.0, bid_score=0.3, ask_score=0.3, signals={"sibling_threat": 1.0}),
    ]
    module = _fresh()
    for ctx in cases:
        action = module.decide(ctx)
        assert action.bid in ("QUOTE", "SHRINK")
        assert action.ask in ("QUOTE", "SHRINK")
        assert action.bid_scale > 0.0
        assert action.ask_scale > 0.0


def test_the_floor_scale_is_used_only_under_a_sibling_threat():
    """0.01 appears for mirrored protection and nowhere else."""
    module = _fresh()
    quiet = module.decide(_decision(ofi=1.0))
    assert (quiet.bid_scale, quiet.ask_scale) == (1.0, 1.0)

    pulled = module.decide(_decision(ofi=1.0, signals={"sibling_scale": 0.0}))
    assert (pulled.bid_scale, pulled.ask_scale) == (1.0, 1.0)

    mirrored = module.decide(_decision(signals={"sibling_threat": 1.0}))
    assert (mirrored.bid_scale, mirrored.ask_scale) == (SHRINK_SCALE, SHRINK_SCALE)

    guarded = module.decide(_decision(ofi=1.0, signals={"sibling_threat": 1.0}))
    assert (guarded.bid_scale, guarded.ask_scale) == (SHRINK_SCALE, 1.0)


def test_the_sibling_does_not_leak_across_assets():
    """One module instance serves both tokens; the decision is per context."""
    module = _fresh()
    yes = module.decide(_decision(asset=ASSET, signals={"sibling_threat": 1.0}))
    no = module.decide(
        _decision(asset=OTHER, ofi=-1.0, signals={"sibling_threat": 1.0})
    )
    assert yes.bid_scale == SHRINK_SCALE and yes.ask_scale == SHRINK_SCALE
    assert no.bid_scale == 1.0 and no.ask_scale == SHRINK_SCALE


def test_setup_only_clears_its_own_market():
    module = _fresh()
    market = MarketView(
        condition_id="cond",
        question="Will it?",
        rate_per_day=100.0,
        rewards_min_size=50.0,
        rewards_max_spread=3.0,
        tokens=(ASSET, OTHER),
    )
    module.setup(market)
    _assert_full(module.decide(_decision()))
    module.on_book(_book())
    module.on_trade(
        TradeContext(
            asset_id=ASSET, price=MID, size=10.0, side="buy", seconds_elapsed=1.0
        )
    )
    _assert_full(module.decide(_decision()))


def test_module_satisfies_the_strategy_protocol():
    assert isinstance(_fresh(), StrategyModule)
    assert _idea.STRATEGY.name == "micro-cross-token-substitution"
