"""Decision-logic tests for the ``micro-reward-priced-pull`` module.

Hand-built contexts only: no tape, no replay, no clock. Every case sets the two
scores, ``rate_per_day``, ``competing_score`` and the ``threat`` signal, so the
conversion

    value  = (rate_per_day / 1440) * score / (score + competing)
    risk   = PULL_MARKOUT * rewards_min_size

is proven rather than observed, together with the three brands of outcome:

* no threat           -> QUOTE / QUOTE, whatever the score;
* value >= 1.5 * risk -> QUOTE (the reward clearly pays for the markout);
* risk <= value < 1.5 * risk -> SHRINK at 0.01 (marginal, keep the score);
* value < risk        -> PULL (the forfeit is cheaper than the markout avoided);
* before ``setup``    -> QUOTE / QUOTE, because the risk cannot be priced.
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
module = importlib.import_module("ideas.micro-reward-priced-pull.module")
MINUTES_PER_DAY = module.MINUTES_PER_DAY
PULL_MARKOUT = module.PULL_MARKOUT
SHRINK_CEILING = module.SHRINK_CEILING
SHRINK_SCALE = module.SHRINK_SCALE
STRATEGY = module.STRATEGY
RewardPricedPull = module.RewardPricedPull

from polymarketbot.strategy.base import (  # noqa: E402
    BookContext,
    DecisionContext,
    MarketView,
    StrategyModule,
)

TICK = 0.01
MID = 0.50
MIN_SIZE = 100.0
#: 0.02 $/share * 100 shares: the dollar markout a pulled side avoids.
MARKOUT = PULL_MARKOUT * MIN_SIZE


@pytest.fixture()
def strategy() -> RewardPricedPull:
    instance = RewardPricedPull()
    instance.setup(_market())
    return instance


def _market(rate_per_day: float = 1440.0, min_size: float = MIN_SIZE) -> MarketView:
    return MarketView(
        condition_id="cond-1",
        question="Will the test pass?",
        rate_per_day=rate_per_day,
        rewards_min_size=min_size,
        rewards_max_spread=3.0,
        tokens=("asset-yes", "asset-no"),
    )


def _ctx(
    *,
    rate_per_day: float = 1440.0,
    bid_score: float = 0.0,
    ask_score: float = 0.0,
    competing_score: float = 0.0,
    signals: dict[str, float] | None = None,
) -> DecisionContext:
    book = BookContext(
        condition_id="cond-1",
        asset_id="asset-yes",
        outcome="YES",
        mid=MID,
        best_bid=round(MID - TICK, 4),
        best_ask=round(MID + TICK, 4),
        tick_size=TICK,
        spread_cents=TICK * 100.0,
        competing_score=competing_score,
    )
    return DecisionContext(
        book=book,
        seconds_elapsed=10.0,
        inventory_shares=0.0,
        max_inventory_shares=200.0,
        bid_queue_ratio=0.0,
        ask_queue_ratio=0.0,
        bid_size_at_level=0.0,
        ask_size_at_level=0.0,
        bid_score=bid_score,
        ask_score=ask_score,
        rate_per_day=rate_per_day,
        signals=dict(signals or {}),
    )


def test_strategy_singleton_implements_protocol() -> None:
    assert isinstance(STRATEGY, StrategyModule)
    assert STRATEGY.name == "micro-reward-priced-pull"


def test_markout_prior_matches_the_measured_back_of_queue_figure() -> None:
    """The prior is the repo's -2.01 c/share rounded, applied to the min size."""
    assert PULL_MARKOUT == pytest.approx(0.02)
    assert (PULL_MARKOUT * MIN_SIZE) == pytest.approx(2.0)
    assert MINUTES_PER_DAY == 1440.0


def test_warm_up_before_setup_quotes_because_risk_is_unpriced() -> None:
    """A fresh module has no ``rewards_min_size``, so it cannot justify a pull."""
    cold = RewardPricedPull()
    ctx = _ctx(rate_per_day=0.0, ask_score=0.0, competing_score=1_000_000.0, signals={"threat": 1.0})
    decision = cold.decide(ctx)
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0
    assert "no reward size" in decision.reason


def test_warm_up_after_setup_prices_the_same_context() -> None:
    """Loading the market is what makes the zero-value side pullable."""
    ctx = _ctx(rate_per_day=0.0, ask_score=0.0, competing_score=1_000_000.0, signals={"threat": 1.0})
    warm = RewardPricedPull()
    warm.setup(_market())
    decision = warm.decide(ctx)
    assert (decision.bid, decision.ask) == ("PULL", "PULL")


def test_setup_with_degenerate_min_size_never_pulls() -> None:
    """A non-positive reward size cannot be priced, so no pull is justified."""
    degenerate = RewardPricedPull()
    degenerate.setup(_market(min_size=0.0))
    decision = degenerate.decide(
        _ctx(rate_per_day=0.0, competing_score=1_000_000.0, signals={"threat": 1.0})
    )
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")


def test_without_threat_signal_both_sides_quote(strategy: RewardPricedPull) -> None:
    """No threat means nothing to price: the risky-looking side still quotes."""
    decision = strategy.decide(_ctx(rate_per_day=0.0, competing_score=1_000_000.0))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")
    assert decision.bid_scale == 1.0
    assert decision.ask_scale == 1.0
    assert decision.bid_price is None
    assert decision.ask_price is None
    assert "no threat" in decision.reason


def test_zero_threat_signal_is_inactive(strategy: RewardPricedPull) -> None:
    decision = strategy.decide(_ctx(rate_per_day=0.0, signals={"threat": 0.0}))
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")


def test_threat_with_high_value_side_quotes_that_side(strategy: RewardPricedPull) -> None:
    """A reward worth more than 1.5x the markout keeps the quote.

    rate 14400/day over a 50/50 share is $5.00/min against a $2.00 markout; the
    pull would forfeit more than it saves, and the rule refuses.
    """
    high = strategy.decide(
        _ctx(rate_per_day=14_400.0, bid_score=50.0, ask_score=50.0, competing_score=50.0, signals={"threat": 1.0})
    )
    assert (high.bid, high.ask) == ("QUOTE", "QUOTE")
    assert high.bid_scale == 1.0

    # $20,000/day on a sole-occupant level is $13.89/min: far above the markout.
    very_high = strategy.decide(
        _ctx(rate_per_day=20_000.0, bid_score=10.0, ask_score=10.0, competing_score=0.0, signals={"threat": 1.0})
    )
    assert (very_high.bid, very_high.ask) == ("QUOTE", "QUOTE")


def test_threat_with_near_zero_value_side_pulls_that_side(strategy: RewardPricedPull) -> None:
    """A tiny rate behind a huge competing score forfeits ~nothing: pull it."""
    decision = strategy.decide(
        _ctx(
            rate_per_day=1.44,
            bid_score=1.0,
            ask_score=100.0,
            competing_score=1_000_000.0,
            signals={"threat": 1.0},
        )
    )
    assert (decision.bid, decision.ask) == ("PULL", "PULL")
    assert (decision.bid_scale, decision.ask_scale) == (0.0, 0.0)
    assert "pull" in decision.reason


def test_side_values_are_priced_independently(strategy: RewardPricedPull) -> None:
    """Same pool, same minute: the side that owns the score is kept, the other is not."""
    decision = strategy.decide(
        _ctx(
            rate_per_day=8640.0,
            bid_score=300.0,
            ask_score=0.0,
            competing_score=0.0,
            signals={"threat": 1.0},
        )
    )
    # bid: share 1.0 * $6.00/min = $6.00 >= 1.5 * $2.00 -> QUOTE.
    assert decision.bid == "QUOTE"
    assert decision.bid_scale == 1.0
    # ask: share 0 -> $0.00 < $2.00 -> PULL.
    assert decision.ask == "PULL"
    assert decision.ask_scale == 0.0


def test_marginal_value_shrinks_to_the_reward_eligible_floor(strategy: RewardPricedPull) -> None:
    """$2 <= value < $3 keeps the score but cuts the notional at risk."""
    # 3600/day on a sole-occupant level is $2.50/min, between 1x and 1.5x $2.00.
    decision = strategy.decide(
        _ctx(rate_per_day=3600.0, bid_score=10.0, ask_score=10.0, competing_score=0.0, signals={"threat": 1.0})
    )
    assert (decision.bid, decision.ask) == ("SHRINK", "SHRINK")
    assert decision.bid_scale == pytest.approx(SHRINK_SCALE)
    assert decision.ask_scale == pytest.approx(SHRINK_SCALE)
    assert SHRINK_SCALE == 0.01
    assert SHRINK_CEILING == pytest.approx(1.5)


def test_pull_threshold_is_strictly_below_the_markout(strategy: RewardPricedPull) -> None:
    """Exactly at the markout the pull is a wash, so the reward wins.

    2880/day on a sole-occupant level is exactly $2.00/min = the markout; a
    score large enough that the share rounds to 1.0 keeps the arithmetic exact.
    """
    at_markout = strategy.decide(
        _ctx(rate_per_day=2880.0, bid_score=1e9, ask_score=1e9, competing_score=0.0, signals={"threat": 1.0})
    )
    assert (at_markout.bid, at_markout.ask) == ("SHRINK", "SHRINK")

    below = strategy.decide(
        _ctx(rate_per_day=2879.0, bid_score=1e9, ask_score=1e9, competing_score=0.0, signals={"threat": 1.0})
    )
    assert (below.bid, below.ask) == ("PULL", "PULL")


def test_competing_score_dilutes_the_forfeited_value(strategy: RewardPricedPull) -> None:
    """The same own score is worth less when competitors hold the band."""
    alone = strategy.decide(
        _ctx(rate_per_day=4000.0, bid_score=10.0, ask_score=10.0, competing_score=0.0, signals={"threat": 1.0})
    )
    diluted = strategy.decide(
        _ctx(rate_per_day=4000.0, bid_score=10.0, ask_score=10.0, competing_score=10.0, signals={"threat": 1.0})
    )
    # Alone: $2.78/min -> marginal band. Diluted to half: $1.39/min -> pull.
    assert (alone.bid, alone.ask) == ("SHRINK", "SHRINK")
    assert (diluted.bid, diluted.ask) == ("PULL", "PULL")


def test_context_rate_missing_falls_back_to_the_setup_rate() -> None:
    """A context without ``rate_per_day`` still prices the pull from the market."""
    rich = RewardPricedPull()
    rich.setup(_market(rate_per_day=20_000.0))
    kept = rich.decide(
        _ctx(rate_per_day=0.0, bid_score=10.0, ask_score=10.0, competing_score=0.0, signals={"threat": 1.0})
    )
    assert (kept.bid, kept.ask) == ("QUOTE", "QUOTE")

    # The same context without a stored rate forfeits nothing, so it pulls.
    flat = RewardPricedPull()
    flat.setup(_market(rate_per_day=0.0))
    pulled = flat.decide(
        _ctx(rate_per_day=0.0, bid_score=10.0, ask_score=10.0, competing_score=0.0, signals={"threat": 1.0})
    )
    assert (pulled.bid, pulled.ask) == ("PULL", "PULL")


def test_malformed_fields_are_read_as_zero_value(strategy: RewardPricedPull) -> None:
    """Junk numbers never create reward: they read as zero and cannot block a pull."""
    decision = strategy.decide(
        _ctx(rate_per_day=-1.0, bid_score=-5.0, ask_score=float("nan"), competing_score=-4.0)
    )
    assert (decision.bid, decision.ask) == ("QUOTE", "QUOTE")

    threatened = strategy.decide(
        _ctx(
            rate_per_day=-1.0,
            bid_score=-5.0,
            ask_score=float("nan"),
            competing_score=-4.0,
            signals={"threat": 1.0},
        )
    )
    assert (threatened.bid, threatened.ask) == ("PULL", "PULL")


def test_prices_are_never_pinned(strategy: RewardPricedPull) -> None:
    """This is a quantity rule; where the quotes rest stays the engine's job."""
    decision = strategy.decide(
        _ctx(rate_per_day=0.0, competing_score=1_000_000.0, signals={"threat": 1.0})
    )
    assert decision.bid_price is None
    assert decision.ask_price is None
