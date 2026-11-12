"""The measured per-order cap: paper's plan is the sizer.

Live's aggregate engine sets ``RiskConfig.max_quote_size_shares`` from the
sizer's measured quote size. These tests fix the paper equivalent: the reward
planner's planned leg size is the measurement, it is applied to the same shared
``RiskConfig`` object before the gate validates the plan, an empty plan invents
nothing, the cap follows the current plan in both directions, and the shared
per-token cap still clamps the approved size. The engine is the real one; only
the venue I/O and the planner are stubbed, so the wiring under test is the
wiring that runs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from loguru import logger

from polymarketbot.fees import FeeSchedule
from polymarketbot.paper import engine as engine_mod
from polymarketbot.paper import sizing as paper_sizing
from polymarketbot.paper.deskew import DeskewState
from polymarketbot.paper.pillars import reward_runner
from polymarketbot.strategy.reward.quote_plan import RewardQuote
from polymarketbot.strategy.venue.reads import Market

STRATEGY = "RewardHarvester"


# ------------------------------------------------------------------- fakes
class _Http:
    def get(self, *args, **kwargs):
        raise AssertionError("fake venue: no HTTP expected")

    def post(self, *args, **kwargs):
        raise AssertionError("fake venue: no HTTP expected")


class _Gamma:
    def markets(self, ids):
        return []


class _Feed:
    def __init__(self, books):
        self._books = books

    def books(self, token_ids, force=False):
        return {
            token: book for token, book in self._books.items() if token in token_ids
        }

    def snapshot(self):
        return dict(self._books)


class _Walker:
    def __init__(self, markets):
        self._markets = markets
        self.complete = True

    def step(self):
        return list(self._markets), True

    def snapshot(self):
        return list(self._markets)


class _Rtt:
    def __init__(self, seconds: float = 0.001):
        self._seconds = seconds

    def measure(self):
        return self._seconds

    def p50(self):
        return self._seconds


def _book(bid: float, ask: float, size: float = 1000.0) -> dict:
    return {
        "bids": [{"price": str(bid), "size": str(size)}],
        "asks": [{"price": str(ask), "size": str(size)}],
    }


def _market(
    condition_id: str = "c1",
    yes: str = "yes",
    no: str = "no",
    *,
    min_size: float = 20.0,
) -> Market:
    return Market(
        condition_id=condition_id,
        question="Q",
        outcomes=("Yes", "No"),
        token_ids=(yes, no),
        tick=0.01,
        min_order=5.0,
        daily_reward=0.0,
        reward_max_spread_cents=3.5,
        reward_min_size=min_size,
        fee_schedule=FeeSchedule.zero(),
        neg_risk=False,
        closed=False,
        uma_status="",
        outcome_prices=(0.5, 0.5),
    )


def _engine(tmp_path, books, markets, *, capital: float = 1000.0):
    engine = engine_mod.PaperEngine(
        {STRATEGY: capital},
        http=_Http(),
        gamma=_Gamma(),
        feed=_Feed(books),
        rtt=_Rtt(0.001),
        db_path=str(tmp_path / "sizing.db"),
        interval=1.0,
        deskew=DeskewState(),
    )
    engine.walker = _Walker(markets)
    return engine


def _wanted(
    condition_id: str,
    token_id: str,
    *,
    price: float = 0.48,
    shares: float,
    min_size: float = 20.0,
) -> RewardQuote:
    return RewardQuote(
        condition_id=condition_id,
        question="Q",
        token_id=token_id,
        price=price,
        shares=shares,
        daily_payout=0.0,
        competing_score=100.0,
        max_spread_cents=3.5,
        min_size=min_size,
    )


def _plan(monkeypatch, quotes: list[RewardQuote]) -> None:
    """Stub the planner; the engine still runs the real risk gate around it."""
    monkeypatch.setattr(
        reward_runner.reward, "reward_candidates", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        reward_runner.reward, "plan_quotes", lambda *args, **kwargs: list(quotes)
    )


def _no_network(monkeypatch) -> None:
    monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: [])
    monkeypatch.setattr(engine_mod.venue, "condition_tape", lambda http, *a, **k: [])


def _pending(engine, kind: str) -> list:
    return [action for action in engine.actions.pending() if action.kind == kind]


@pytest.fixture()
def stable_books():
    return {"yes": _book(0.48, 0.52), "no": _book(0.48, 0.52)}


# --------------------------------------------------------------- observer
def test_an_empty_plan_is_not_an_observation():
    observer = paper_sizing.QuoteSizer()
    # No quotes at all, and only zero-share legs: neither is a size.
    assert observer.observe([], now=1.0) is None
    assert observer.observe([_wanted("c1", "yes", shares=0.0)], now=2.0) is None
    assert observer.last is None
    assert observer.observations == 0


def test_the_statistic_is_the_largest_planned_leg_with_its_count():
    observer = paper_sizing.QuoteSizer()
    plan = observer.observe(
        [
            _wanted("c1", "yes", shares=37.5),
            _wanted("c1", "no", shares=37.5),
            _wanted("c2", "y2", shares=12.0),
        ],
        now=100.0,
    )
    assert plan is not None
    assert plan.shares == pytest.approx(37.5)
    assert plan.legs == 3
    assert plan.conditions == 2
    assert plan.ts == pytest.approx(100.0)
    assert plan.observations == 1
    assert plan.source == paper_sizing.SOURCE
    assert "max of 3 planned leg(s)" in plan.describe()


# ------------------------------------------------------- measured plan cap
def test_measured_plan_sets_the_shared_per_order_cap(
    tmp_path, stable_books, monkeypatch,
):
    _no_network(monkeypatch)
    engine = _engine(tmp_path, stable_books, [_market()])
    config = engine.risk_manager.config
    # Before any plan: the documented live fallback ($1,000 -> $200 cost cap).
    assert config.max_quote_size_shares == pytest.approx(200.0)
    per_token = config.max_position_shares_per_token
    per_market = config.max_position_cost_per_market

    _plan(monkeypatch, [_wanted("c1", "yes", shares=30.0)])
    engine._tick()

    # Same shared RiskConfig object, now carrying the plan's measured size.
    assert engine.risk_manager.config is config
    assert config.max_quote_size_shares == pytest.approx(30.0)
    # The position/cost caps are untouched: this is the per-order cap only.
    assert config.max_position_shares_per_token == per_token
    assert config.max_position_cost_per_market == per_market
    placed = _pending(engine, "reward_quote")
    assert len(placed) == 1
    assert placed[0].payload["resting"].shares == pytest.approx(30.0)
    observation = paper_sizing.sizer(engine).last
    assert observation is not None
    assert observation.shares == pytest.approx(30.0)
    assert observation.legs == 1
    assert observation.observations == 1
    engine.ledger.close()


def test_empty_plan_leaves_the_cap_alone(tmp_path, stable_books, monkeypatch):
    _no_network(monkeypatch)
    engine = _engine(tmp_path, stable_books, [_market()])
    _plan(monkeypatch, [_wanted("c1", "yes", shares=40.0)])
    engine._tick()
    config = engine.risk_manager.config
    assert config.max_quote_size_shares == pytest.approx(40.0)
    observed = paper_sizing.sizer(engine).last

    _plan(monkeypatch, [])
    engine._tick()

    # A tick with nothing planned measured nothing: the cap and the last
    # observation stand, and no size was invented from the empty list.
    assert config.max_quote_size_shares == pytest.approx(40.0)
    assert paper_sizing.sizer(engine).last is observed
    assert paper_sizing.sizer(engine).observations == 1
    engine.ledger.close()


def test_the_cap_follows_the_current_plan_both_ways(
    tmp_path, stable_books, monkeypatch,
):
    """Chosen policy: non-monotonic - the gate prices the plan it validates."""
    _no_network(monkeypatch)
    engine = _engine(tmp_path, stable_books, [_market()])
    config = engine.risk_manager.config

    _plan(monkeypatch, [_wanted("c1", "yes", shares=60.0)])
    engine._tick()
    assert config.max_quote_size_shares == pytest.approx(60.0)

    # A smaller later plan lowers the cap: a stale larger cap would price the
    # gate against a size this tick's planner did not ask for.
    _plan(monkeypatch, [_wanted("c1", "yes", shares=20.0)])
    engine._tick()
    assert config.max_quote_size_shares == pytest.approx(20.0)

    # And a larger plan raises it back, so the planner can scale up again.
    _plan(monkeypatch, [_wanted("c1", "yes", shares=45.0)])
    engine._tick()
    assert config.max_quote_size_shares == pytest.approx(45.0)
    assert paper_sizing.sizer(engine).observations == 3
    engine.ledger.close()


def test_the_shared_per_token_cap_still_clamps_the_plan(
    tmp_path, stable_books, monkeypatch,
):
    _no_network(monkeypatch)
    engine = _engine(tmp_path, stable_books, [_market()], capital=1000.0)
    config = engine.risk_manager.config
    # $1,000 bankroll derives a 150-share per-token cap.
    assert config.max_position_shares_per_token == pytest.approx(150.0)

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="INFO")
    try:
        _plan(monkeypatch, [_wanted("c1", "yes", shares=500.0)])
        engine._tick()
    finally:
        logger.remove(sink_id)

    # The measured plan set the per-order cap to 500; the shared per-token cap
    # still clamped the approved size to 150 shares.
    assert config.max_quote_size_shares == pytest.approx(500.0)
    placed = _pending(engine, "reward_quote")
    assert len(placed) == 1
    resting = placed[0].payload["resting"]
    assert resting.shares == pytest.approx(150.0)
    assert resting.initial_shares == pytest.approx(150.0)
    assert any("clamped by risk" in message for message in messages)
    engine.ledger.close()


def test_the_cap_mutation_logs_old_new_count_and_source(
    tmp_path, stable_books, monkeypatch,
):
    _no_network(monkeypatch)
    engine = _engine(tmp_path, stable_books, [_market()])
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="INFO")
    try:
        _plan(
            monkeypatch,
            [
                _wanted("c1", "yes", shares=33.3),
                _wanted("c1", "no", shares=33.3),
            ],
        )
        engine._tick()
    finally:
        logger.remove(sink_id)

    sizing_logs = [
        message
        for message in messages
        if "paper sizer: max_quote_size_shares" in message
    ]
    assert sizing_logs, messages
    line = sizing_logs[0]
    assert "200.00 -> 33.30" in line
    assert "max of 2 planned leg(s)" in line
    assert "sample 1" in line
    assert paper_sizing.SOURCE in line
    engine.ledger.close()


# ------------------------------------------------------- non-engine caller
def test_a_caller_without_a_risk_manager_still_records_the_observation():
    engine = SimpleNamespace()
    plan = paper_sizing.record(
        engine, [_wanted("c1", "yes", shares=17.0)], now=5.0
    )
    assert plan is not None
    assert plan.shares == pytest.approx(17.0)
    # Nothing was published, but the measurement exists for the next reader.
    assert engine._quote_sizer.last is plan
