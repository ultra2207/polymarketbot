"""Live feed -> unified risk engine wiring.

The bug this locks down: public trade prints were discarded in live mode
(`on_trade_update` returned before doing anything), so an L3 queue position
could never advance. These tests drive the real ``FeedMixin`` methods against a
stub engine so the wiring, not the model, is what is under test.
"""

from __future__ import annotations

import types

import pytest

from polymarketbot.live.engine.feed import FeedMixin
from polymarketbot.risk.unified import UnifiedRiskEngine
from polymarketbot.strategy.reward.microstructure import MicrostructureEngine

YES = "yes-token"
NO = "no-token"


def _flow() -> MicrostructureEngine:
    """Tape-fitted flow engine; production callers load the same fit values."""
    return MicrostructureEngine(
        c_depth=1.5,
        z_score=3.0,
        v_ref=50_000.0,
        beta_vol=0.15,
        pull_multiplier=1.5,
        widen_offset_multiplier=1.5,
    )


def _stub(engine: UnifiedRiskEngine, *, dry_run: bool = False) -> types.SimpleNamespace:
    stub = types.SimpleNamespace()
    stub.micro_risk = engine
    stub._best_bids = {}
    stub._best_asks = {}
    stub._book_ts = {}
    stub._book_depth = {}
    stub.asset_id_yes = YES
    stub.asset_id_no = NO
    stub.condition_id = "0xcond"
    stub.active_quotes = {}
    stub.trading_enabled = True
    stub.market_closed = False
    stub.dry_run = dry_run
    stub.risk_manager = types.SimpleNamespace(
        portfolio_state=types.SimpleNamespace(is_halted=False, kill_switch_active=False),
        update_market_price=lambda **kwargs: (True, "ok"),
        record_heartbeat=lambda ts: None,
    )
    stub._fresh_mid = lambda tok: (
        None
        if stub._best_bids.get(tok) is None or stub._best_asks.get(tok) is None
        else (stub._best_bids[tok] + stub._best_asks[tok]) / 2.0
    )
    stub.evaluate_quotes = lambda timestamp=None: None
    stub.cancel_all_quotes = lambda: None
    return stub


def _engine() -> UnifiedRiskEngine:
    return UnifiedRiskEngine(
        tick_size=0.01,
        flow_factory=_flow,
        horizon_seconds=10.0,
        consecutive_fills_threshold=3,
    )


def test_a_book_update_reaches_the_engine_and_keeps_depth() -> None:
    engine = _engine()
    stub = _stub(engine)

    FeedMixin.on_book_update(
        stub, "YES", [(0.49, 100.0), (0.48, 50.0)], [(0.51, 80.0)], timestamp=1.0
    )

    assert engine.flow(YES) is not None
    # Retained raw depth has no public accessor; ``flow()`` above covers the
    # derived state, and this is the only observable of the depth the engine kept.
    assert stub._book_depth["YES"][0] == [(0.49, 100.0), (0.48, 50.0)]
    assert stub._book_depth["YES"][1] == [(0.51, 80.0)]


def test_live_trade_prints_advance_the_queue_position() -> None:
    engine = _engine()
    stub = _stub(engine, dry_run=False)
    FeedMixin.on_book_update(stub, "YES", [(0.49, 100.0)], [(0.51, 80.0)], timestamp=1.0)
    engine.on_entry(
        "o1",
        YES,
        "BUY",
        0.49,
        20.0,
        1.5,
        bids=[(0.49, 100.0)],
        asks=[(0.51, 80.0)],
    )

    FeedMixin.on_trade_update(stub, "YES", 0.49, 10.0, "SELL", timestamp=2.0)

    progress = engine._queue.progress("o1")
    assert progress is not None
    assert progress.ahead == 90.0
    assert progress.traded_at_level == 10.0


def test_a_through_print_marks_our_resting_bid_filled() -> None:
    engine = _engine()
    stub = _stub(engine, dry_run=False)
    FeedMixin.on_book_update(stub, "YES", [(0.49, 100.0)], [(0.51, 80.0)], timestamp=1.0)
    engine.on_entry(
        "o2",
        YES,
        "BUY",
        0.49,
        20.0,
        1.5,
        bids=[(0.49, 100.0)],
        asks=[(0.51, 80.0)],
    )

    FeedMixin.on_trade_update(stub, "YES", 0.48, 5.0, "SELL", timestamp=2.0)

    progress = engine._queue.progress("o2")
    assert progress is not None and progress.filled


def test_a_fill_reaches_the_streak_counter() -> None:
    engine = _engine()
    stub = _stub(engine)
    FeedMixin.on_book_update(stub, "YES", [(0.49, 100.0)], [(0.51, 80.0)], timestamp=1.0)
    engine.on_entry(
        "o3", YES, "BUY", 0.49, 20.0, 1.5, bids=[(0.49, 100.0)], asks=[(0.51, 80.0)]
    )

    engine.on_fill(YES, "BUY", 0.49, 20.0, 2.0)

    assert engine.snapshot()["tokens"][YES]["streaks"]["BUY"] == 1
    assert engine.snapshot()["orders"] == 0


def test_engine_advances_only_on_prints_it_is_given() -> None:
    engine = _engine()
    stub = _stub(engine, dry_run=False)
    FeedMixin.on_book_update(stub, "YES", [(0.49, 100.0)], [(0.51, 80.0)], timestamp=1.0)
    engine.on_entry(
        "o4", YES, "BUY", 0.49, 20.0, 1.5, bids=[(0.49, 100.0)], asks=[(0.51, 80.0)]
    )

    progress = engine._queue.progress("o4")
    assert progress is not None
    assert progress.ahead == 100.0
    assert pytest.approx(progress.consumed_fraction) == 0.0
