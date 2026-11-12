"""Paper resolution lifecycle: verdict, redemption, blocking and avg-cost marks.

Covers the four transitions the paper engine was missing: a final verdict pays
the winner and writes the loser off, a closed-but-unresolved market stops
quoting without writing anything off, an unmeasured redemption is refused
instead of booked at zero gas, and a vanished book is carried at average cost
instead of a frozen last mid.
"""

from __future__ import annotations

import pytest

from polymarketbot.fees import FeeSchedule
from polymarketbot.paper import deskew as paper_deskew
from polymarketbot.paper import engine as engine_mod
from polymarketbot.paper import lifecycle
from polymarketbot.paper.actions import Resting
from polymarketbot.strategy.venue.reads import Market


class _Http:
    def get(self, *args, **kwargs):
        raise AssertionError("fake venue: no HTTP expected")

    def post(self, *args, **kwargs):
        raise AssertionError("fake venue: no HTTP expected")


class _Gamma:
    def __init__(self, rows=None, error: Exception | None = None):
        self.rows = list(rows or [])
        self.error = error
        self.calls: list[list[str]] = []

    def markets(self, ids):
        self.calls.append(list(ids))
        if self.error is not None:
            raise self.error
        return [dict(row) for row in self.rows]


class _Feed:
    def __init__(self, books=None):
        self._books = books or {}

    def books(self, token_ids, force=False):
        return {token: book for token, book in self._books.items() if token in token_ids}

    def snapshot(self, max_age=None):
        return dict(self._books)


class _Walker:
    def __init__(self, markets=()):
        self._markets = list(markets)
        self.complete = True

    def step(self):
        return list(self._markets), True

    def snapshot(self):
        return list(self._markets)


class _Rtt:
    def __init__(self, seconds: float = 0.01):
        self._seconds = seconds

    def measure(self):
        return self._seconds

    def p50(self):
        return self._seconds


def _market(condition_id="c1", yes="yes", no="no", *, reward=0.0) -> Market:
    return Market(
        condition_id=condition_id,
        question="Q",
        outcomes=("Yes", "No"),
        token_ids=(yes, no),
        tick=0.01,
        min_order=5.0,
        daily_reward=reward,
        reward_max_spread_cents=3.5,
        reward_min_size=20.0,
        fee_schedule=FeeSchedule.zero(),
        neg_risk=False,
        closed=False,
        uma_status="",
        outcome_prices=(0.5, 0.5),
    )


def _book(bid: float | None, ask: float | None, size: float = 1000.0) -> dict:
    return {
        "bids": [] if bid is None else [{"price": str(bid), "size": str(size)}],
        "asks": [] if ask is None else [{"price": str(ask), "size": str(size)}],
    }


def _resolution_row(*, closed=True, prices=("1", "0"), uma="resolved",
                    condition_id="c1", tokens=("yes", "no")):
    return {
        "conditionId": condition_id,
        "closed": closed,
        "umaResolutionStatus": uma,
        "clobTokenIds": list(tokens),
        "outcomes": ["Yes", "No"],
        "outcomePrices": list(prices),
    }


def _engine(tmp_path, *, rows=None, error=None, books=None, capital=100.0,
            gas=0.01):
    """A paper engine over fake venues, with the measured gas injected.

    ``build_state`` in an offline test has no measured calibration or gas, so
    the de-skew/merge paths stay disabled; the reshape is explicit so a test
    that needs measured gas for redemption sets it on this state.
    """
    deskew = paper_deskew.DeskewState(gas_cost_usd=gas)
    engine = engine_mod.PaperEngine(
        {"RewardHarvester": capital},
        http=_Http(),
        gamma=_Gamma(rows, error),
        feed=_Feed(books),
        rtt=_Rtt(),
        db_path=str(tmp_path / "lifecycle.db"),
        interval=1.0,
        resume=False,
        deskew=deskew,
    )
    engine.walker = _Walker()
    return engine


def _trades(engine):
    return engine.ledger._conn.execute(
        "SELECT kind, condition_id, token_id, shares, price, fee, cash_delta, detail"
        " FROM trades ORDER BY rowid"
    ).fetchall()


def test_resolved_winner_is_redeemed_and_loser_written_off(tmp_path, monkeypatch):
    engine = _engine(tmp_path, rows=[_resolution_row()])
    monkeypatch.setattr(
        lifecycle, "measure_redeem_flight",
        lambda: lifecycle.RedeemFlight(2.0),
    )
    assert engine.ledger.buy(
        "RewardHarvester", condition_id="c1", token_id="yes",
        shares=100.0, price=0.55,
    )
    assert engine.ledger.buy(
        "RewardHarvester", condition_id="c1", token_id="no",
        shares=40.0, price=0.30,
    )
    cash_before = engine.ledger.cash("RewardHarvester")
    assert cash_before == pytest.approx(100.0 - 55.0 - 12.0)

    resting = Resting("RewardHarvester", "c1", "yes", 0.54, 10.0, 5.0, placed=0.0)
    engine.reward_quotes["yes"] = resting
    engine.actions.submit(
        "reward_quote", "RewardHarvester",
        {"token_id": "yes", "resting": Resting(
            "RewardHarvester", "c1", "yes", 0.54, 10.0, 5.0, placed=0.0)},
    )

    lifecycle.refresh(engine, now=1000.0, force=True)

    # Quotes are cancelled before settlement, including the in-flight placement.
    assert resting.cancel_at == pytest.approx(1000.0)
    assert [a for a in engine.actions.pending() if a.kind == "reward_quote"] == []
    # Par applied to every strategy; the loser is removed at zero, no cash moves.
    held = engine.ledger.positions("RewardHarvester")
    assert {lot.token_id for lot in held} == {"yes"}
    assert held[0].par_value == pytest.approx(1.0)
    assert engine.ledger.cash("RewardHarvester") == pytest.approx(cash_before)
    assert engine.ledger.equity()["RewardHarvester"] == pytest.approx(
        cash_before + 100.0
    )
    assert "c1" in engine.resolution.redeems
    assert engine.resolution.redeems["c1"].lands == pytest.approx(1002.0)

    kinds = [row[0] for row in _trades(engine)]
    assert kinds.count("write_off") == 1
    assert "redeem" not in kinds
    write_off = next(row for row in _trades(engine) if row[0] == "write_off")
    assert write_off[3] == pytest.approx(40.0)
    assert write_off[6] == pytest.approx(-12.0)
    assert write_off[7] == "resolution_loser"

    # The redemption lands only once the measured on-chain flight has elapsed.
    lifecycle.refresh(engine, now=1002.0, force=True)

    gas = 0.01 * 45_000 / 78_000
    assert engine.ledger.positions("RewardHarvester") == []
    assert engine.ledger.cash("RewardHarvester") == pytest.approx(
        cash_before + 100.0 - gas
    )
    assert engine.ledger.equity()["RewardHarvester"] == pytest.approx(
        cash_before + 100.0 - gas
    )
    redeem = next(row for row in _trades(engine) if row[0] == "redeem")
    assert redeem[3] == pytest.approx(100.0)
    assert redeem[4] == pytest.approx(1.0)
    assert redeem[5] == pytest.approx(gas)
    assert redeem[6] == pytest.approx(100.0 - gas)
    assert engine.resolution.redeems == {}


def test_closed_unresolved_cancels_quotes_and_blocks_new_ones(tmp_path, monkeypatch):
    from loguru import logger

    rows = [_resolution_row(closed=True, prices=("0.62", "0.38"), uma="")]
    engine = _engine(tmp_path, rows=rows, books={})
    market = _market()
    engine.walker = _Walker([market])
    monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: [])
    monkeypatch.setattr(
        engine_mod.venue, "condition_tape", lambda http, conditions, **kwargs: []
    )
    assert engine.ledger.buy(
        "RewardHarvester", condition_id="c1", token_id="yes",
        shares=10.0, price=0.50,
    )
    resting = Resting("RewardHarvester", "c1", "yes", 0.49, 10.0, 5.0, placed=0.0)
    engine.reward_quotes["yes"] = resting

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)),
                         level="WARNING")
    try:
        engine._tick()
        assert resting.cancel_at is not None
        assert "c1" not in engine.by_condition
        assert [m.condition_id for m in engine.markets] == []
        # No verdict: the held lot survives, unmarked, with nothing written off.
        held = engine.ledger.positions("RewardHarvester")
        assert [lot.token_id for lot in held] == ["yes"]
        assert held[0].par_value is None
        assert "write_off" not in [row[0] for row in _trades(engine)]
        # A second poll must not re-log the same closed market.
        lifecycle.refresh(engine, now=engine.resolution.last_poll + 61.0,
                          force=True)
        assert sum("closed without a final verdict" in message
                   for message in messages) == 1
    finally:
        logger.remove(sink_id)


def test_unmeasured_gas_refuses_redemption_but_applies_par(tmp_path, monkeypatch):
    from loguru import logger

    engine = _engine(tmp_path, rows=[_resolution_row()], gas=None)
    monkeypatch.setattr(
        lifecycle, "measure_redeem_flight",
        lambda: lifecycle.RedeemFlight(2.0),
    )
    assert engine.ledger.buy(
        "RewardHarvester", condition_id="c1", token_id="yes",
        shares=100.0, price=0.55,
    )
    assert engine.ledger.buy(
        "RewardHarvester", condition_id="c1", token_id="no",
        shares=40.0, price=0.30,
    )
    cash_before = engine.ledger.cash("RewardHarvester")

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)),
                         level="WARNING")
    try:
        lifecycle.refresh(engine, now=1000.0, force=True)
        held = engine.ledger.positions("RewardHarvester")
        assert {lot.token_id for lot in held} == {"yes"}
        assert held[0].par_value == pytest.approx(1.0)
        assert engine.ledger.cash("RewardHarvester") == pytest.approx(cash_before)
        assert engine.ledger.equity()["RewardHarvester"] == pytest.approx(
            cash_before + 100.0
        )
        assert engine.resolution.redeems == {}
        assert "redeem" not in [row[0] for row in _trades(engine)]
        assert sum("redemption refused" in message for message in messages) == 1
        # A later poll retries but does not re-log the same refusal.
        lifecycle.refresh(engine, now=1061.0, force=True)
        assert sum("redemption refused" in message for message in messages) == 1
        assert engine.ledger.cash("RewardHarvester") == pytest.approx(cash_before)
        assert engine.resolution.redeems == {}
    finally:
        logger.remove(sink_id)


def test_unmeasured_onchain_flight_refuses_redemption(tmp_path, monkeypatch):
    """A measured gas cost is not enough: a zero-latency redeem is never booked."""
    from loguru import logger

    engine = _engine(tmp_path, rows=[_resolution_row()], gas=0.01)
    monkeypatch.setattr(
        lifecycle, "measure_redeem_flight",
        lambda: lifecycle.RedeemFlight(0.0, ("RPC round trip unmeasured",)),
    )
    assert engine.ledger.buy(
        "RewardHarvester", condition_id="c1", token_id="yes",
        shares=100.0, price=0.55,
    )
    cash_before = engine.ledger.cash("RewardHarvester")
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)),
                         level="WARNING")
    try:
        lifecycle.refresh(engine, now=1000.0, force=True)
        held = engine.ledger.positions("RewardHarvester")
        assert [lot.par_value for lot in held] == [pytest.approx(1.0)]
        assert engine.resolution.redeems == {}
        assert engine.ledger.cash("RewardHarvester") == pytest.approx(cash_before)
        assert "redeem" not in [row[0] for row in _trades(engine)]
        assert sum("redemption refused" in message for message in messages) == 1
    finally:
        logger.remove(sink_id)


def test_no_book_marks_at_average_cost_not_the_last_mark(tmp_path):
    from loguru import logger

    engine = _engine(tmp_path)
    assert engine.ledger.buy(
        "RewardHarvester", condition_id="c9", token_id="tok9",
        shares=100.0, price=0.40,
    )
    lot = engine.ledger.positions("RewardHarvester")[0]
    cash = engine.ledger.cash("RewardHarvester")

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)),
                         level="WARNING")
    try:
        engine._mark({"tok9": _book(0.55, 0.56)})
        assert lot.last_mark == pytest.approx(0.555)
        assert lot.mark_live is True

        engine._mark({})
        assert lot.last_mark == pytest.approx(0.40)
        assert lot.mark_live is False
        assert lot in engine.ledger.unmarked("RewardHarvester")
        assert engine.ledger.equity()["RewardHarvester"] == pytest.approx(
            cash + 100.0 * 0.40
        )
        assert sum("mark unmeasured" in message for message in messages) == 1
        # Repeated no-book ticks are not a new transition.
        engine._mark({})
        assert sum("mark unmeasured" in message for message in messages) == 1
    finally:
        logger.remove(sink_id)


def test_missing_gamma_answer_leaves_held_lots_open(tmp_path):
    engine = _engine(tmp_path, error=RuntimeError("gamma offline"))
    assert engine.ledger.buy(
        "RewardHarvester", condition_id="c1", token_id="yes",
        shares=10.0, price=0.50,
    )
    lifecycle.refresh(engine, now=1000.0, force=True)
    lot = engine.ledger.positions("RewardHarvester")[0]
    assert lot.par_value is None
    assert lot.shares == pytest.approx(10.0)
    assert "c1" not in engine.resolution.blocked

    # An empty answer is equally not a verdict.
    engine.gamma.rows = []
    lifecycle.refresh(engine, now=1061.0, force=True)
    assert engine.ledger.positions("RewardHarvester")[0].par_value is None
    assert "write_off" not in [row[0] for row in _trades(engine)]
