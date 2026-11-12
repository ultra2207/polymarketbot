"""End-to-end engine test with a fake venue: no network, real decision modules.

Proves the reward transitions that matter: a resting bid fills only from
measured tape flow, the RTT probe follows its cadence, and an unmeasured book
is flagged rather than priced. The CTF parity transition moved with the
archived strategy (``archive/ctf/tests/test_ctf_runner.py``), and the
settlement transition moved with the archived SettlementHarvester
(``archive/tests/test_paper_pillars_settlement.py``).
"""

from __future__ import annotations

import time

import pytest

from polymarketbot.fees import FeeSchedule
from polymarketbot.paper import engine as engine_mod
from polymarketbot.strategy.venue.reads import Market


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
    def __init__(self, seconds: float):
        self._seconds = seconds

    def measure(self):
        return self._seconds

    def p50(self):
        return self._seconds


def _market(condition_id: str, yes: str, no: str, *, reward: float = 0.0,
            min_order: float = 5.0, min_size: float = 20.0) -> Market:
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


def _book(bid: float | None, ask: float | None, size: float = 1000.0) -> dict:
    return {
        "bids": [] if bid is None else [{"price": str(bid), "size": str(size)}],
        "asks": [] if ask is None else [{"price": str(ask), "size": str(size)}],
    }


@pytest.fixture()
def fake_venue(monkeypatch):
    books = {"yes": _book(0.89, 0.90), "no": _book(0.10, 0.11)}
    market = _market("c1", "yes", "no")
    monkeypatch.setattr(engine_mod.venue, "universe", lambda gamma, max_pages=30: [market])
    monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: [])
    return market, books


def _engine(tmp_path, books, *, rtt: float = 0.01):
    return engine_mod.PaperEngine(
        {"RewardHarvester": 100.0},
        http=_Http(),
        gamma=_Gamma(),
        feed=_Feed(books),
        rtt=_Rtt(rtt),
        db_path=str(tmp_path / "engine.db"),
        interval=1.0,
        resume=False,
    )


def test_reward_fill_uses_measured_tape_flow(tmp_path, monkeypatch):
    """A resting bid fills only from prints measured after it landed."""
    market = _market("c4", "y4", "n4", reward=100.0)
    # Our bid sits one tick above the best bid, so there is no level ahead.
    books = {"y4": _book(0.47, 0.52), "n4": _book(0.47, 0.52)}
    monkeypatch.setattr(engine_mod.venue, "universe", lambda gamma, max_pages=30: [market])
    monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: [])
    engine = _engine(tmp_path, books)
    engine.walker = _Walker([market])
    quote = engine_mod.Resting("RewardHarvester", "c4", "y4", 0.48, 100.0, 20.0, 0.0)
    engine.reward_quotes["y4"] = quote
    now = time.time()
    prints = [engine_mod.venue.Print("y4", "c4", "SELL", 0.48, 50.0, now)]
    engine._apply_resting_fills(engine.reward_quotes, "RewardHarvester", books, prints, now)
    lots = engine.ledger.positions("RewardHarvester")
    assert len(lots) == 1
    # No level ahead at our price: the full 50-share print fills us.
    assert lots[0].shares == pytest.approx(50.0)
    assert engine.ledger.cash("RewardHarvester") == pytest.approx(100.0 - 50.0 * 0.48)
    # A print above our bid is not at our price and is not a trade-through:
    # under FIFO it cannot fill the order. (A print *below* a resting bid is
    # the tracker's fill case: it means the taker swept through us.)
    engine.reward_quotes["y4"] = engine_mod.Resting(
        "RewardHarvester", "c4", "y4", 0.48, 100.0, 20.0, 0.0
    )
    above_prints = [engine_mod.venue.Print("y4", "c4", "BUY", 0.49, 500.0, now)]
    engine._apply_resting_fills(engine.reward_quotes, "RewardHarvester", books,
                                above_prints, now)
    assert engine.ledger.positions("RewardHarvester")[0].shares == pytest.approx(50.0)
    engine.ledger.close()


def test_rtt_probe_is_refreshed_on_its_cadence_not_only_at_startup(
    tmp_path, fake_venue, monkeypatch,
):
    """The landing model pays `p50()`, so a startup-only sample prices every
    action for the whole run. Measured drift between soak windows is 1.7x
    (344 ms -> 570 ms), which a single sample cannot track."""
    _market, books = fake_venue

    class _CountingRtt:
        def __init__(self):
            self.calls = 0
            self._seconds = 0.01

        def measure(self):
            self.calls += 1
            return self._seconds

        def p50(self):
            return self._seconds

    probe = _CountingRtt()
    engine = engine_mod.PaperEngine(
        {"RewardHarvester": 100.0},
        http=_Http(),
        gamma=_Gamma(),
        feed=_Feed(books),
        rtt=probe,
        db_path=str(tmp_path / "rtt.db"),
        interval=1.0,
        resume=False,
        rtt_refresh_s=60.0,
    )
    engine.walker = _Walker([_market])
    monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: [])

    # The probe runs on its own thread; one refresh step is one measurement.
    engine._refresh_venue_state()
    assert probe.calls == 1
    engine._refresh_venue_state()
    assert probe.calls == 2
    # A tick must not pay for a network probe.
    engine._tick()
    assert probe.calls == 2
    engine.ledger.close()


def test_mark_stage_flags_a_vanished_book_once_then_clears(tmp_path):
    """No book: flag the lot, keep the last measurement, log one transition."""
    from loguru import logger

    engine = _engine(tmp_path, {})
    assert engine.ledger.buy(
        "RewardHarvester", condition_id="c9", token_id="tok9",
        shares=100.0, price=0.40,
    )
    lot = engine.ledger.positions("RewardHarvester")[0]

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)),
                         level="WARNING")
    try:
        engine._mark({"tok9": _book(0.55, 0.56)})
        assert lot.mark_live is True
        assert engine.ledger.equity()["RewardHarvester"] == pytest.approx(
            100.0 - 40.0 + 100.0 * 0.55
        )

        engine._mark({})
        assert lot.mark_live is False
        assert [item.token_id for item in
                engine.ledger.unmarked("RewardHarvester")] == ["tok9"]
        # Repeated no-book ticks must not re-log the same transition.
        engine._mark({})
        assert sum("mark unmeasured" in message for message in messages) == 1

        engine._mark({"tok9": _book(0.60, 0.61)})
        assert lot.mark_live is True
        assert lot.last_mark == pytest.approx(0.60)
        assert engine.ledger.unmarked("RewardHarvester") == []
    finally:
        logger.remove(sink_id)
    engine.ledger.close()
