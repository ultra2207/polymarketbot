"""Permanent tests for the paper engine's complete-set merge and de-skew paths.

Each test fixes one transition of the live parity gap: a matched pair merges
through ``Ledger.merge`` at the measured gas, a one-legged book is de-skewed by
the shared optimizer's four pathways, the Tier-2 cross lands after the measured
round trip against the landing book, and an unmeasured calibration/gas refuses
the action instead of assuming zero. No network: the fake venue is the same
shape ``test_paper_engine`` uses.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import replace

import pytest
from loguru import logger

from polymarketbot.fees import FeeSchedule, taker_fee
from polymarketbot.paper import deskew as deskew_mod
from polymarketbot.paper import engine as engine_mod
from polymarketbot.paper.actions import TAKER_KIND
from polymarketbot.paper.deskew import DeskewState
from polymarketbot.strategy.reward.deskew import (
    DeSkewCalibration,
    DeSkewPathway,
)
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


class _Rtt:
    def __init__(self, seconds: float):
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
    reward_min_size: float = 20.0,
    schedule: FeeSchedule | None = None,
) -> Market:
    return Market(
        condition_id=condition_id,
        question="Q",
        outcomes=("Yes", "No"),
        token_ids=(yes, no),
        tick=0.01,
        min_order=5.0,
        daily_reward=100.0,
        reward_max_spread_cents=3.5,
        reward_min_size=reward_min_size,
        fee_schedule=schedule or FeeSchedule.zero(),
        neg_risk=False,
        closed=False,
        uma_status="",
        outcome_prices=(0.5, 0.5),
    )


def _calibration() -> DeSkewCalibration:
    """A structural calibration whose thresholds are small enough to cross.

    These coefficients are the test's own measured inputs (the same fields the
    fit writes); nothing here is read from the repository artifact.
    """
    return DeSkewCalibration(
        gamma=0.01,
        skew_aggressiveness=0.25,
        q_soft_bankroll_fraction=0.001,
        q_hard_risk_fraction=0.02,
        q_hard_absorption_quotes=2.0,
        q_hard_floor_quotes=1.0,
        reserve_usable_fraction=0.5,
        holding_turnover_cycles=0.25,
        holding_expiry_fraction=0.1,
        holding_min_seconds=1.0,
        holding_max_seconds=600.0,
        merge_gas_fraction=0.5,
        merge_quote_multiples=0.0,
        merge_bankroll_fraction=0.5,
        merge_min_shares=1.0,
        ofi_toxicity_threshold=1e9,
        drift_toxicity_cents=1e9,
    )


def _state(
    *,
    gas: float | None = 0.002,
    days: float | None = 7.0,
    calibration: DeSkewCalibration | None = None,
) -> DeskewState:
    return DeskewState(
        calibration=_calibration() if calibration is None else calibration,
        gas_cost_usd=gas,
        resolution_days=days,
        merge_refusal="" if gas is not None else "merge gas unmeasured",
        deskew_refusal="" if gas is not None and days is not None else "unmeasured",
    )


def _engine(tmp_path, books, *, rtt: float = 0.001, state: DeskewState | None = None,
            db: str = "deskew.db"):
    return engine_mod.PaperEngine(
        {STRATEGY: 1000.0},
        http=_Http(),
        gamma=_Gamma(),
        feed=_Feed(books),
        rtt=_Rtt(rtt),
        db_path=str(tmp_path / db),
        interval=1.0,
        deskew=state if state is not None else _state(),
    )


def _prints(condition_id: str = "c1", token_id: str = "yes") -> list[Print]:
    # One print inside the 120 s tape window: a measured, non-zero tape rate.
    return [Print(token_id, condition_id, "SELL", 0.45, 10.0, time.time())]


def _journal(db_path: str) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT kind, condition_id, token_id, shares, price, fee, cash_delta"
            " FROM trades ORDER BY rowid"
        ).fetchall()


def _hold(engine, token_id: str, shares: float, price: float,
          condition_id: str = "c1") -> None:
    assert engine.ledger.buy(
        STRATEGY, condition_id=condition_id, token_id=token_id,
        shares=shares, price=price,
    )


def _wait_for_landing(rtt: float = 0.001) -> None:
    time.sleep(max(0.01, rtt * 5.0))


# ------------------------------------------------------------------ merges
def test_complete_pair_merges_through_ledger_at_measured_gas(tmp_path):
    """Both legs held: the matched shares merge to par, less the measured gas."""
    books = {"yes": _book(0.44, 0.46), "no": _book(0.54, 0.56)}
    engine = _engine(tmp_path, books)
    engine.by_condition = {"c1": _market()}
    _hold(engine, "yes", 100.0, 0.40)
    _hold(engine, "no", 100.0, 0.55)
    cash_before = engine.ledger.cash(STRATEGY)
    gas = engine.deskew.gas_cost_usd

    engine._merge(books)

    assert engine.deskew.merges == 1
    assert engine.ledger.positions(STRATEGY) == []
    assert engine.ledger.cash(STRATEGY) == pytest.approx(cash_before + 100.0 - gas)
    rows = _journal(str(tmp_path / "deskew.db"))
    assert [row[0] for row in rows][-1] == "merge"
    merge_row = rows[-1]
    assert merge_row[3] == pytest.approx(100.0)
    assert merge_row[5] == pytest.approx(gas)      # the fee column carries gas
    assert merge_row[6] == pytest.approx(100.0 - gas)
    engine.ledger.close()


def test_one_leg_is_never_merged(tmp_path):
    """A single leg is directional inventory: it must not burn without a pair."""
    books = {"yes": _book(0.44, 0.46), "no": _book(0.54, 0.56)}
    engine = _engine(tmp_path, books)
    engine.by_condition = {"c1": _market()}
    _hold(engine, "yes", 100.0, 0.40)
    cash_before = engine.ledger.cash(STRATEGY)

    engine._merge(books)

    assert engine.deskew.merges == 0
    assert engine.ledger.cash(STRATEGY) == pytest.approx(cash_before)
    assert {lot.token_id: lot.shares for lot in engine.ledger.positions(STRATEGY)} == {
        "yes": 100.0
    }
    engine.ledger.close()


def test_merge_below_the_fitted_threshold_is_held_as_a_complete_set(tmp_path):
    books = {"yes": _book(0.44, 0.46), "no": _book(0.54, 0.56)}
    state = _state()
    state.calibration = replace(_calibration(), merge_min_shares=500.0)
    engine = _engine(tmp_path, books, state=state)
    engine.by_condition = {"c1": _market()}
    _hold(engine, "yes", 100.0, 0.40)
    _hold(engine, "no", 100.0, 0.55)
    cash_before = engine.ledger.cash(STRATEGY)

    engine._merge(books)

    assert engine.deskew.merges == 0
    assert engine.ledger.cash(STRATEGY) == pytest.approx(cash_before)
    engine.ledger.close()


# --------------------------------------------------------------- de-skew
def test_long_yes_synthetic_merge_buys_the_missing_leg_then_merges(tmp_path):
    schedule = FeeSchedule(rate=0.05, exponent=1.0, taker_only=True,
                           rebate_rate=0.0, source="test")
    books = {"yes": _book(0.44, 0.46), "no": _book(0.52, 0.54)}
    engine = _engine(tmp_path, books)
    engine.by_condition = {"c1": _market(schedule=schedule)}
    _hold(engine, "yes", 300.0, 0.42)
    cash_before = engine.ledger.cash(STRATEGY)

    engine._deskew(books, _prints())
    decision = engine.deskew.last_decision["c1"]
    assert decision.pathway is DeSkewPathway.SYNTHETIC_MERGE
    assert decision.target_token == "NO" and decision.action_side == "BUY"
    pending = [action for action in engine.actions.pending() if action.kind == TAKER_KIND]
    assert len(pending) == 1
    payload = pending[0].payload
    assert payload["token_id"] == "no"
    assert payload["side"] == "BUY"
    assert payload["shares"] == pytest.approx(40.0)   # min(|q|, 2 x quote size)
    # Nothing has moved yet: the cross is in flight, not executed at 0 ms.
    assert engine.ledger.cash(STRATEGY) == pytest.approx(cash_before)
    assert [lot.token_id for lot in engine.ledger.positions(STRATEGY)] == ["yes"]

    _wait_for_landing()
    landing = {"yes": _book(0.43, 0.45), "no": _book(0.53, 0.55)}
    engine._land(landing, [])
    tokens = {lot.token_id: lot.shares for lot in engine.ledger.positions(STRATEGY)}
    assert tokens["no"] == pytest.approx(40.0)
    fee = taker_fee(40.0, 0.55, schedule)
    assert engine.ledger.cash(STRATEGY) == pytest.approx(cash_before - 40.0 * 0.55 - fee)

    engine._merge(landing)
    assert engine.deskew.merges == 1
    tokens = {lot.token_id: lot.shares for lot in engine.ledger.positions(STRATEGY)}
    assert tokens == {"yes": 260.0}
    gas = engine.deskew.gas_cost_usd
    assert engine.ledger.cash(STRATEGY) == pytest.approx(
        cash_before - 40.0 * 0.55 - fee + 40.0 - gas
    )
    engine.ledger.close()


def test_long_no_synthetic_merge_buys_the_missing_yes_leg_then_merges(tmp_path):
    schedule = FeeSchedule(rate=0.05, exponent=1.0, taker_only=True,
                           rebate_rate=0.0, source="test")
    books = {"yes": _book(0.44, 0.46), "no": _book(0.52, 0.54)}
    engine = _engine(tmp_path, books)
    engine.by_condition = {"c1": _market(schedule=schedule)}
    _hold(engine, "no", 300.0, 0.53)
    cash_before = engine.ledger.cash(STRATEGY)

    engine._deskew(books, _prints(token_id="no"))
    decision = engine.deskew.last_decision["c1"]
    assert decision.pathway is DeSkewPathway.SYNTHETIC_MERGE
    assert decision.target_token == "YES" and decision.action_side == "BUY"

    _wait_for_landing()
    landing = {"yes": _book(0.44, 0.46), "no": _book(0.52, 0.54)}
    engine._land(landing, [])
    tokens = {lot.token_id: lot.shares for lot in engine.ledger.positions(STRATEGY)}
    assert tokens["yes"] == pytest.approx(40.0)

    engine._merge(landing)
    assert engine.deskew.merges == 1
    tokens = {lot.token_id: lot.shares for lot in engine.ledger.positions(STRATEGY)}
    assert tokens == {"no": 260.0}
    fee = taker_fee(40.0, 0.46, schedule)
    gas = engine.deskew.gas_cost_usd
    assert engine.ledger.cash(STRATEGY) == pytest.approx(
        cash_before - 40.0 * 0.46 - fee + 40.0 - gas
    )
    engine.ledger.close()


def test_long_yes_direct_cross_sells_the_excess_leg_at_the_landing_bid(tmp_path):
    schedule = FeeSchedule(rate=0.05, exponent=1.0, taker_only=True,
                           rebate_rate=0.0, source="test")
    # A deep NO ask makes the synthetic route worse than selling YES.
    books = {"yes": _book(0.44, 0.46), "no": _book(0.38, 0.60)}
    engine = _engine(tmp_path, books)
    engine.by_condition = {"c1": _market(schedule=schedule)}
    _hold(engine, "yes", 300.0, 0.42)
    cash_before = engine.ledger.cash(STRATEGY)

    engine._deskew(books, _prints())
    decision = engine.deskew.last_decision["c1"]
    assert decision.pathway is DeSkewPathway.DIRECT_CROSS
    assert decision.target_token == "YES" and decision.action_side == "SELL"

    _wait_for_landing()
    landing = {"yes": _book(0.40, 0.42), "no": _book(0.58, 0.60)}
    engine._land(landing, [])
    tokens = {lot.token_id: lot.shares for lot in engine.ledger.positions(STRATEGY)}
    assert tokens == {"yes": 260.0}
    fee = taker_fee(40.0, 0.40, schedule)
    assert engine.ledger.cash(STRATEGY) == pytest.approx(
        cash_before + 40.0 * 0.40 - fee
    )
    rows = _journal(str(tmp_path / "deskew.db"))
    assert rows[-1][0] == "sell"
    assert rows[-1][6] == pytest.approx(40.0 * 0.40 - fee)
    engine.ledger.close()


def test_hold_stable_leaves_the_symmetric_pair_alone(tmp_path):
    books = {"yes": _book(0.44, 0.46), "no": _book(0.54, 0.56)}
    engine = _engine(tmp_path, books)
    engine.by_condition = {"c1": _market()}
    _hold(engine, "yes", 10.0, 0.42)   # q_soft is the 20-share quote size
    cash_before = engine.ledger.cash(STRATEGY)

    engine._deskew(books, _prints())

    decision = engine.deskew.last_decision["c1"]
    assert decision.pathway is DeSkewPathway.HOLD_STABLE
    assert engine.deskew.skews == {}
    assert engine.actions.pending() == []
    assert engine.ledger.cash(STRATEGY) == pytest.approx(cash_before)
    engine.ledger.close()


def test_passive_fade_reprices_the_missing_leg_in_bid_space(tmp_path):
    books = {"yes": _book(0.44, 0.46), "no": _book(0.54, 0.56)}
    # 25 shares is above the 20-share q_soft and below the 40-share q_hard, so
    # the optimizer's Tier-1 passive fade is the decision.
    engine = _engine(tmp_path, books, state=_state())
    engine.by_condition = {"c1": _market()}
    _hold(engine, "yes", 25.0, 0.42)

    engine._deskew(books, _prints())

    decision = engine.deskew.last_decision["c1"]
    assert decision.pathway is DeSkewPathway.PASSIVE_FADE
    legs = engine.deskew.skews["c1"]
    assert "no" in legs and "yes" in legs
    # The missing NO leg rests at the complement of the faded YES ask, inside
    # the NO book, and is no longer the planner's symmetric bid.
    assert 0.0 < legs["no"].price < 0.56
    assert legs["yes"].price < 0.44
    engine.ledger.close()


def test_taker_lands_at_the_landing_book_and_reports_a_partial_fill(tmp_path):
    schedule = FeeSchedule(rate=0.05, exponent=1.0, taker_only=True,
                           rebate_rate=0.0, source="test")
    books = {"yes": _book(0.44, 0.46), "no": _book(0.38, 0.60)}
    engine = _engine(tmp_path, books)
    engine.by_condition = {"c1": _market(schedule=schedule)}
    _hold(engine, "yes", 300.0, 0.42)
    cash_before = engine.ledger.cash(STRATEGY)

    engine._deskew(books, _prints())
    assert engine.deskew.last_decision["c1"].pathway is DeSkewPathway.DIRECT_CROSS

    # The landing book has only 25 shares at the bid; the 40-share request
    # fills partially even though the decision snapshot showed 1000.
    _wait_for_landing()
    landing = {"yes": _book(0.40, 0.42, size=25.0), "no": _book(0.58, 0.60)}
    engine._land(landing, [])

    tokens = {lot.token_id: lot.shares for lot in engine.ledger.positions(STRATEGY)}
    assert tokens == {"yes": 275.0}
    fee = taker_fee(25.0, 0.40, schedule)
    assert engine.ledger.cash(STRATEGY) == pytest.approx(
        cash_before + 25.0 * 0.40 - fee
    )
    engine.ledger.close()


# ------------------------------------------------------------- refusals
def test_unmeasured_gas_refuses_merges_and_crosses(tmp_path, monkeypatch):
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        monkeypatch.setattr(deskew_mod, "load_deskew_calibration", lambda *a, **k: _calibration())
        monkeypatch.setattr(deskew_mod, "load_measured_merge_gas_usd", lambda *a, **k: None)
        state = deskew_mod.build_state()
    finally:
        logger.remove(sink)

    assert state.merge_enabled is False
    assert state.deskew_enabled is False
    assert "gas" in state.merge_refusal
    assert any("paper merge/de-skew refused" in message for message in messages)

    books = {"yes": _book(0.44, 0.46), "no": _book(0.54, 0.56)}
    engine = _engine(tmp_path, books, state=state)
    engine.by_condition = {"c1": _market()}
    _hold(engine, "yes", 300.0, 0.42)
    _hold(engine, "no", 100.0, 0.55)
    cash_before = engine.ledger.cash(STRATEGY)

    engine._deskew(books, _prints())
    engine._merge(books)

    assert engine.actions.pending() == []
    assert engine.deskew.merges == 0 and engine.deskew.takers == 0
    assert engine.ledger.cash(STRATEGY) == pytest.approx(cash_before)
    assert {lot.token_id: lot.shares for lot in engine.ledger.positions(STRATEGY)} == {
        "yes": 300.0, "no": 100.0
    }
    engine.ledger.close()


def test_missing_calibration_refuses_merges(tmp_path, monkeypatch):
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        monkeypatch.setattr(deskew_mod, "load_deskew_calibration", lambda *a, **k: None)
        monkeypatch.setattr(
            deskew_mod, "load_measured_merge_gas_usd", lambda *a, **k: 0.002
        )
        state = deskew_mod.build_state()
    finally:
        logger.remove(sink)

    assert state.merge_enabled is False
    assert "calibration" in state.merge_refusal
    assert any("paper merge/de-skew refused" in message for message in messages)

    books = {"yes": _book(0.44, 0.46), "no": _book(0.54, 0.56)}
    engine = _engine(tmp_path, books, state=state)
    engine.by_condition = {"c1": _market()}
    _hold(engine, "yes", 100.0, 0.40)
    _hold(engine, "no", 100.0, 0.55)
    cash_before = engine.ledger.cash(STRATEGY)

    engine._merge(books)

    assert engine.deskew.merges == 0
    assert engine.ledger.cash(STRATEGY) == pytest.approx(cash_before)
    engine.ledger.close()


def test_stale_persisted_gas_is_unmeasured(tmp_path):
    gas_file = tmp_path / "gas.json"
    pol_file = tmp_path / "pol.json"
    gas_file.write_text('{"gwei": 279.0, "ts": 1000.0}', encoding="utf-8")
    pol_file.write_text('{"usd": 0.0845, "ts": 1000.0}', encoding="utf-8")
    assert deskew_mod.load_measured_merge_gas_usd(
        gas_file, pol_file, now=1000.0 + 10.0, max_age_s=60.0
    ) == pytest.approx(0.001839, rel=1e-3)
    assert deskew_mod.load_measured_merge_gas_usd(
        gas_file, pol_file, now=1000.0 + 3600.0, max_age_s=60.0
    ) is None


def test_merge_and_synthetic_cross_keep_cash_and_inventory_reconciled(tmp_path):
    """Every deskew cash movement is a journal row the ledger reconciles.

    ``Ledger.snapshot`` commits a baseline, then the merge + synthetic cross
    run; ``reconcile`` must report no accounting defect and the cash delta over
    the window must equal the journal's own ``SUM(cash_delta)``.
    """
    schedule = FeeSchedule(rate=0.05, exponent=1.0, taker_only=True,
                           rebate_rate=0.0, source="test")
    # A deep NO ask keeps the de-skew on the direct-cross route.
    books = {"yes": _book(0.44, 0.46), "no": _book(0.38, 0.60)}
    engine = _engine(tmp_path, books)
    engine.by_condition = {"c1": _market(schedule=schedule)}
    _hold(engine, "yes", 300.0, 0.42)
    db = tmp_path / "deskew.db"
    engine.ledger.snapshot()
    cash_before = engine.ledger.cash(STRATEGY)
    net_before = 300.0

    # Complete 100 sets out of the excess YES first, then de-skew the rest.
    _hold(engine, "no", 100.0, 0.54)
    engine._merge(books)
    assert engine.deskew.merges == 1
    engine._deskew(books, _prints())
    assert engine.deskew.last_decision["c1"].pathway is DeSkewPathway.DIRECT_CROSS
    _wait_for_landing()
    landing = {"yes": _book(0.40, 0.42), "no": _book(0.58, 0.60)}
    engine._land(landing, [])

    tokens = {lot.token_id: lot.shares for lot in engine.ledger.positions(STRATEGY)}
    assert tokens == {"yes": 160.0}
    fee = taker_fee(40.0, 0.40, schedule)
    gas = engine.deskew.gas_cost_usd
    assert engine.ledger.cash(STRATEGY) == pytest.approx(
        cash_before - 100.0 * 0.54 + 100.0 - gas + 40.0 * 0.40 - fee
    )
    # Directional exposure shrank by the merged sets and the crossed shares.
    assert tokens["yes"] == pytest.approx(net_before - 100.0 - 40.0)

    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="ERROR")
    try:
        report = engine.ledger.reconcile()
    finally:
        logger.remove(sink)
    assert not [message for message in messages if "accounting defect" in message]
    assert report[STRATEGY]["cash"] == pytest.approx(engine.ledger.cash(STRATEGY))
    with sqlite3.connect(db) as conn:
        journaled = conn.execute(
            "SELECT COALESCE(SUM(cash_delta), 0.0) FROM trades WHERE strategy = ?",
            (STRATEGY,),
        ).fetchone()[0]
    total_cash_delta = engine.ledger.cash(STRATEGY) - 1000.0
    assert float(journaled) == pytest.approx(total_cash_delta)
    engine.ledger.close()
