"""Paper risk parity: the live hard gates over the paper ledger.

Each test fixes one gate the paper engine used to lack: mark/equity reaching
``RiskManager.update_portfolio_pnl`` before planning, a daily-loss or drawdown
halt stopping quotes and replay fills, a price-jump cooldown blocking only the
affected market, a kill switch and a stale book stopping quoting, and the
shared per-order / position caps clamping or refusing a planned quote. The
engine is the real one; only the venue I/O and the reward planner are stubbed,
so the wiring under test is the wiring that runs.
"""

from __future__ import annotations

import time

import pytest
from loguru import logger

from polymarketbot.fees import FeeSchedule
from polymarketbot.paper import engine as engine_mod
from polymarketbot.paper import risk as paper_risk
from polymarketbot.paper.actions import Resting
from polymarketbot.paper.deskew import DeskewState
from polymarketbot.paper.pillars import reward_runner
from polymarketbot.risk.manager import RiskManager
from polymarketbot.risk.models import CircuitBreakerReason, RiskConfig, RiskStatus
from polymarketbot.strategy.reward.quote_plan import RewardQuote
from polymarketbot.strategy.venue.reads import Market, Print

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
        return {token: book for token, book in self._books.items() if token in token_ids}

    def snapshot(self, max_age=None):
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


def _book(bid: float | None, ask: float | None, size: float = 1000.0) -> dict:
    return {
        "bids": [] if bid is None else [{"price": str(bid), "size": str(size)}],
        "asks": [] if ask is None else [{"price": str(ask), "size": str(size)}],
    }


def _market(
    condition_id: str = "c1",
    yes: str = "yes",
    no: str = "no",
    *,
    reward: float = 0.0,
    min_order: float = 5.0,
    min_size: float = 20.0,
) -> Market:
    return Market(
        condition_id=condition_id,
        question="Q",
        outcomes=("Yes", "No"),
        token_ids=(yes, no),
        tick=0.01,
        min_order=min_order,
        daily_reward=reward,
        reward_max_spread_cents=3.5,
        reward_min_size=min_size,
        fee_schedule=FeeSchedule.zero(),
        neg_risk=False,
        closed=False,
        uma_status="",
        outcome_prices=(0.5, 0.5),
    )


def _engine(
    tmp_path,
    books,
    markets,
    *,
    capital: float = 1000.0,
    risk_manager: RiskManager | None = None,
    db: str = "risk.db",
):
    engine = engine_mod.PaperEngine(
        {STRATEGY: capital},
        http=_Http(),
        gamma=_Gamma(),
        feed=_Feed(books),
        rtt=_Rtt(0.001),
        db_path=str(tmp_path / db),
        interval=1.0,
        # Merges/de-skew are not part of this file: the state is disabled so
        # only the risk wiring runs.
        deskew=DeskewState(),
        risk_manager=risk_manager,
    )
    engine.walker = _Walker(markets)
    return engine


def _wanted(
    condition_id: str,
    token_id: str,
    *,
    price: float = 0.48,
    shares: float = 20.0,
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


# --------------------------------------------------------- measured inputs
def test_tick_feeds_measured_mids_and_marked_equity_to_the_risk_manager(
    tmp_path, stable_books, monkeypatch,
):
    _no_network(monkeypatch)
    market = _market()
    engine = _engine(tmp_path, stable_books, [market])
    # A lot that is not part of this tick's market: its mark moves equity
    # without moving any mid the volatility monitor sees.
    assert engine.ledger.buy(
        STRATEGY, condition_id="c9", token_id="tok9", shares=100.0, price=0.40
    )
    stable_books["tok9"] = _book(0.55, 0.56)

    heartbeat_before = engine.risk_manager.portfolio_state.last_heartbeat_timestamp
    engine._tick()

    rm = engine.risk_manager
    assert rm.portfolio_state.current_equity_usd == pytest.approx(
        sum(engine.ledger.equity().values())
    )
    assert rm.portfolio_state.current_cash_usd == pytest.approx(
        engine.ledger.cash(STRATEGY)
    )
    # 100 shares bought at $0.40, marked at a $0.55 bid.
    assert rm.portfolio_state.daily_unrealized_pnl == pytest.approx(15.0)
    assert rm.portfolio_state.current_equity_usd == pytest.approx(1015.0)
    assert rm.portfolio_state.last_heartbeat_timestamp >= heartbeat_before
    # Both outcome mids of the market were observed; the 2 c breaker sees them.
    assert rm.volatility_monitor.last_mid["yes"] == pytest.approx(0.50)
    assert rm.volatility_monitor.last_mid["no"] == pytest.approx(0.50)
    assert rm.portfolio_state.is_halted is False
    engine.ledger.close()


def test_risk_config_uses_the_shared_live_source(tmp_path, stable_books, monkeypatch):
    _no_network(monkeypatch)
    engine = _engine(tmp_path, stable_books, [_market()], capital=1000.0)
    rm = engine.risk_manager
    assert isinstance(rm, RiskManager)
    # RiskConfig derives these from the bankroll; the live engine spells the
    # same derived values out. Nothing here is a paper-specific threshold.
    assert rm.config.total_bankroll_usd == pytest.approx(1000.0)
    assert rm.config.daily_loss_limit_usd == pytest.approx(50.0)
    assert rm.config.max_drawdown_usd == pytest.approx(100.0)
    assert rm.config.price_jump_threshold_cents == pytest.approx(2.0)
    assert rm.config.price_jump_window_seconds == pytest.approx(60.0)
    assert rm.config.cooldown_duration_seconds == pytest.approx(300.0)
    assert rm.config.allow_short is False
    # The shared per-order cap, not a paper constant (live's aggregate engine
    # substitutes its own measured sizer size; paper has no such input).
    assert rm.config.max_quote_size_shares == pytest.approx(200.0)
    # A fresh ledger opens with its own equity, not the $300 RiskConfig default.
    state = rm.portfolio_state
    assert state.day_start_equity_usd == pytest.approx(1000.0)
    assert state.peak_equity_usd == pytest.approx(1000.0)
    engine.ledger.close()


def test_an_injected_risk_manager_is_adopted(tmp_path, stable_books, monkeypatch):
    _no_network(monkeypatch)
    injected = RiskManager(RiskConfig(total_bankroll_usd=1000.0))
    engine = _engine(tmp_path, stable_books, [_market()], risk_manager=injected)
    assert engine.risk_manager is injected
    # The measured opening equity still seeds the adopted manager.
    assert injected.portfolio_state.day_start_equity_usd == pytest.approx(1000.0)
    engine.ledger.close()


# ---------------------------------------------------------- permanent halts
def test_daily_loss_breaker_halts_and_books_no_fill(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    market = _market()
    books = {"yes": _book(0.42, 0.43), "no": _book(0.57, 0.58)}
    engine = _engine(tmp_path, books, [market], capital=1000.0)
    assert engine.ledger.buy(
        STRATEGY, condition_id="c1", token_id="yes", shares=1000.0, price=0.50
    )
    cash_before = engine.ledger.cash(STRATEGY)
    # A resting bid the tape would fill: the halt must stop the replay.
    engine.reward_quotes["yes"] = Resting(
        STRATEGY, "c1", "yes", 0.44, 100.0, 5.0, 0.0,
        order_id="seeded", initial_shares=100.0, seen_ts=0.0,
    )
    prints = [Print("yes", "c1", "SELL", 0.44, 200.0, time.time())]
    monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: prints)

    # Equity is $500 cash + 1000 x $0.42 = $920: an $80 daily loss (limit $50).
    engine._tick()

    rm = engine.risk_manager
    assert rm.portfolio_state.is_halted is True
    assert rm.portfolio_state.status is RiskStatus.HALTED
    assert [
        event.reason for event in rm.portfolio_state.circuit_breaker_events
    ][-1] is CircuitBreakerReason.DAILY_LOSS_LIMIT
    assert engine.reward_quotes == {}
    assert engine._fills[STRATEGY] == 0
    assert engine.ledger.cash(STRATEGY) == pytest.approx(cash_before)
    assert {
        lot.token_id: (lot.shares, lot.cost_usd)
        for lot in engine.ledger.positions(STRATEGY)
    } == {"yes": (1000.0, 500.0)}
    assert _pending(engine, "reward_quote") == []
    # Nothing may rest a new quote while halted either.
    allowed, refusal, _, _ = paper_risk.quote_denial(
        engine,
        condition_id="c1",
        token_id="no",
        price=0.57,
        shares=20.0,
        books=books,
        tick_size=0.01,
        now=time.time(),
    )
    assert allowed is False
    assert "halted" in refusal.lower()
    assert paper_risk.quote_block_reason(engine, "no") is not None
    engine.ledger.close()


def test_max_drawdown_breaker_halts_after_a_peak(tmp_path, monkeypatch):
    """Peak-to-trough drawdown trips even when the day is still profitable."""
    _no_network(monkeypatch)
    market = _market()
    books = {"yes": _book(0.48, 0.52), "no": _book(0.48, 0.52)}
    engine = _engine(tmp_path, books, [market], capital=1000.0)
    assert engine.ledger.buy(
        STRATEGY, condition_id="c1", token_id="yes", shares=1000.0, price=0.50
    )
    _plan(monkeypatch, [])

    # Peak: $500 cash + 1000 x $0.65 = $1,150.
    books["yes"] = _book(0.65, 0.66)
    engine._tick()
    assert engine.risk_manager.portfolio_state.peak_equity_usd == pytest.approx(1150.0)
    assert engine.risk_manager.portfolio_state.is_halted is False

    # Trough: $500 + 1000 x $0.52 = $1,020. The day is still +$20 over its
    # $1,000 opening equity, so only the cumulative drawdown can trip:
    # $130 = 11.3 % of the peak, over the shared 10 % limit.
    books["yes"] = _book(0.52, 0.53)
    engine._tick()

    rm = engine.risk_manager
    assert rm.portfolio_state.is_halted is True
    assert rm.portfolio_state.daily_loss_usd == pytest.approx(0.0)
    reasons = [event.reason for event in rm.portfolio_state.circuit_breaker_events]
    assert CircuitBreakerReason.MAX_DRAWDOWN_LIMIT in reasons
    assert engine._fills[STRATEGY] == 0
    engine.ledger.close()


def test_kill_switch_blocks_quoting_and_fills_until_resume(
    tmp_path, stable_books, monkeypatch,
):
    _no_network(monkeypatch)
    market = _market()
    engine = _engine(tmp_path, stable_books, [market])
    _plan(monkeypatch, [_wanted("c1", "yes")])

    engine._tick()
    assert len(_pending(engine, "reward_quote")) == 1
    time.sleep(0.003)
    engine._land(stable_books, [])
    assert engine.reward_quotes["yes"].cancel_at is None

    engine.risk_manager.emergency_unquote("test kill")
    cash_before = engine.ledger.cash(STRATEGY)
    # A print through the resting bid would fill the whole 20-share order if
    # the halt did not stop the replay.
    prints = [Print("yes", "c1", "SELL", 0.47, 200.0, time.time())]

    # A tick under the kill switch: no fill of the retired order, no
    # replacement, and the resting order is drained.
    monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: prints)
    engine._tick()
    assert engine.risk_manager.portfolio_state.status is RiskStatus.KILLED
    assert engine.reward_quotes == {}
    assert engine._fills[STRATEGY] == 0
    assert engine.ledger.cash(STRATEGY) == pytest.approx(cash_before)
    assert engine.ledger.positions(STRATEGY) == []
    assert _pending(engine, "reward_quote") == []

    # Recovery is the shared manager's resume: the next tick quotes again.
    engine.risk_manager.resume()
    assert engine.risk_manager.portfolio_state.is_halted is False
    engine._tick()
    assert len(_pending(engine, "reward_quote")) == 1
    time.sleep(0.003)
    engine._land(stable_books, [])
    assert "yes" in engine.reward_quotes
    assert engine.reward_quotes["yes"].cancel_at is None
    engine.ledger.close()


# ------------------------------------------------------- volatility cooldown
def test_price_jump_cooldown_blocks_only_the_affected_market(
    tmp_path, monkeypatch,
):
    _no_network(monkeypatch)
    market_a = _market("c1", "y1", "n1")
    market_b = _market("c2", "y2", "n2")
    books = {
        "y1": _book(0.48, 0.52), "n1": _book(0.48, 0.52),
        "y2": _book(0.48, 0.52), "n2": _book(0.48, 0.52),
    }
    engine = _engine(tmp_path, books, [market_a, market_b])
    _plan(monkeypatch, [])
    engine._tick()
    assert engine.risk_manager.portfolio_state.is_halted is False

    # y1's mid jumps 6 c inside the 60 s window; y2 is unchanged.
    books["y1"] = _book(0.54, 0.58)
    _plan(monkeypatch, [_wanted("c1", "y1"), _wanted("c2", "y2")])
    engine._tick()

    rm = engine.risk_manager
    assert rm.portfolio_state.is_halted is False
    assert rm.is_in_cooldown("y1") is True
    assert rm.is_in_cooldown("y2") is False
    placed = {
        action.payload["token_id"] for action in _pending(engine, "reward_quote")
    }
    assert placed == {"y2"}

    # Cooldown expiry is the reset: the same market quotes again. The sibling
    # may land in the same tick, so presence is checked on both states.
    rm.volatility_monitor.cooldowns["y1"] = time.time() - 1.0
    engine._tick()
    active = {
        action.payload["token_id"] for action in _pending(engine, "reward_quote")
    } | set(engine.reward_quotes)
    assert "y1" in active
    assert rm.is_in_cooldown("y1") is False
    engine.ledger.close()


def test_stale_book_cancels_the_quote_and_blocks_a_replacement(
    tmp_path, stable_books, monkeypatch,
):
    _no_network(monkeypatch)
    market = _market()
    engine = _engine(tmp_path, stable_books, [market])
    _plan(monkeypatch, [_wanted("c1", "yes")])

    engine._tick()
    time.sleep(0.003)
    engine._land(stable_books, [])
    assert engine.reward_quotes["yes"].cancel_at is None

    stable_books.pop("yes")
    engine._tick()

    # The book is the staleness evidence: no two-sided mid, no resting order.
    quote = engine.reward_quotes.get("yes")
    assert quote is not None and quote.cancel_at is not None
    assert _pending(engine, "reward_quote") == []
    assert engine._fills[STRATEGY] == 0
    engine.ledger.close()


# ------------------------------------------------------------- caps
def test_order_cap_clamps_the_planned_size(tmp_path, stable_books, monkeypatch):
    _no_network(monkeypatch)
    engine = _engine(tmp_path, stable_books, [_market()], capital=1000.0)
    _plan(monkeypatch, [_wanted("c1", "yes", shares=500.0)])

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="INFO")
    try:
        engine._tick()
    finally:
        logger.remove(sink_id)

    placed = _pending(engine, "reward_quote")
    assert len(placed) == 1
    resting = placed[0].payload["resting"]
    # The live aggregate fallback cap for $1,000 is 200 shares; the shared
    # position-cost headroom may clamp further. What matters is that the
    # 500-share plan was reduced, not that an unrelated 50-share default held.
    assert 0.0 < resting.shares < 500.0
    assert resting.shares <= 200.0
    assert resting.initial_shares == pytest.approx(resting.shares)
    assert any("clamped by risk" in message for message in messages)
    engine.ledger.close()


def test_position_cost_cap_refuses_a_quote_below_the_venue_minimum(
    tmp_path, stable_books, monkeypatch,
):
    _no_network(monkeypatch)
    engine = _engine(tmp_path, stable_books, [_market()], capital=100.0)
    # $100 bankroll -> $75 per-market cost cap (shared RiskConfig derivation).
    assert engine.ledger.buy(
        STRATEGY, condition_id="c1", token_id="yes", shares=140.0, price=0.50
    )
    cash_before = engine.ledger.cash(STRATEGY)
    lots_before = {
        lot.token_id: (lot.shares, lot.cost_usd)
        for lot in engine.ledger.positions(STRATEGY)
    }
    # $5 of cost headroom at $0.48 supports ~10.4 shares, under the 20-share
    # venue minimum: the quote is refused rather than rested unfillable.
    _plan(monkeypatch, [_wanted("c1", "yes", shares=20.0)])

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)),
                         level="WARNING")
    try:
        engine._tick()
    finally:
        logger.remove(sink_id)

    assert _pending(engine, "reward_quote") == []
    assert any("below the venue minimum" in message for message in messages)
    assert engine.ledger.cash(STRATEGY) == pytest.approx(cash_before)
    assert {
        lot.token_id: (lot.shares, lot.cost_usd)
        for lot in engine.ledger.positions(STRATEGY)
    } == lots_before
    engine.ledger.close()
