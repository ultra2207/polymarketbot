"""End-to-end engine test with a fake venue: no network, real decision modules.

Proves the two state transitions that matter: settlement buys against the
landing book, and the ledger's equity reflects par value rather than the last
trade price. The CTF parity transition moved with the archived strategy
(``archive/ctf/tests/test_ctf_runner.py``).
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


def _engine(tmp_path, books, *, gas: float | None, rtt: float = 0.01):
    return engine_mod.PaperEngine(
        {"RewardHarvester": 100.0, "SettlementHarvester": 100.0, "CtfMinter": 100.0},
        http=_Http(),
        gamma=_Gamma(),
        feed=_Feed(books),
        rtt=_Rtt(rtt),
        db_path=str(tmp_path / "engine.db"),
        interval=1.0,
        gas_usd=gas,
        resume=False,
        enable_uma_polling=False,
    )


def test_settlement_buys_winner_at_landing_and_marks_par(tmp_path, fake_venue,
                                                       monkeypatch):
    _market, books = fake_venue
    engine = _engine(tmp_path, books, gas=None)
    engine.walker = _Walker([_market])
    engine.settlement_model = type(
        "Model", (), {
            "refresh": lambda self, now=None: None,
            "tape": type("Tape", (), {
                "bind_condition_ids": lambda self, by_question: 0,
                "live_verdict_condition_ids": lambda self, now=None: ["c1"],
            })(),
        }
    )()
    monkeypatch.setattr(
        engine_mod.settlement, "verdict_for",
        lambda model, m, now=None: __import__(
            "polymarketbot.strategy.settlement.fair_value",
            fromlist=["OracleVerdict"],
        ).OracleVerdict(0, 1.0, "gamma_payout", time.time()),
    )
    engine._tick()
    assert engine.ledger.cash("SettlementHarvester") == 100.0
    time.sleep(0.02)
    engine._land(books, [])
    lots = engine.ledger.positions("SettlementHarvester")
    assert len(lots) == 1
    assert lots[0].token_id == "yes"
    assert lots[0].shares == pytest.approx(100.0 / 0.90, rel=1e-6)
    # Par value, not the 0.89 bid: buying at 0.90 converts $100 of cash into
    # 111.11 shares worth $1.00 each at the oracle verdict.
    equity = engine.ledger.equity()["SettlementHarvester"]
    assert equity == pytest.approx(100.0 / 0.90, rel=1e-6)
    engine.ledger.close()


def test_reward_fill_uses_measured_tape_flow(tmp_path, monkeypatch):
    """A resting bid fills only from prints measured after it landed."""
    market = _market("c4", "y4", "n4", reward=100.0)
    # Our bid sits one tick above the best bid, so there is no level ahead.
    books = {"y4": _book(0.47, 0.52), "n4": _book(0.47, 0.52)}
    monkeypatch.setattr(engine_mod.venue, "universe", lambda gamma, max_pages=30: [market])
    monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: [])
    engine = _engine(tmp_path, books, gas=None)
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
    # A BUY print (taker buying) cannot fill our resting bid.
    engine.reward_quotes["y4"] = engine_mod.Resting(
        "RewardHarvester", "c4", "y4", 0.48, 100.0, 20.0, 0.0
    )
    buy_prints = [engine_mod.venue.Print("y4", "c4", "BUY", 0.47, 500.0, now)]
    engine._apply_resting_fills(engine.reward_quotes, "RewardHarvester", books,
                                buy_prints, now)
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
        {"RewardHarvester": 100.0, "SettlementHarvester": 100.0, "CtfMinter": 100.0},
        http=_Http(),
        gamma=_Gamma(),
        feed=_Feed(books),
        rtt=probe,
        db_path=str(tmp_path / "rtt.db"),
        interval=1.0,
        gas_usd=None,
        resume=False,
        enable_uma_polling=False,
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


def test_failed_gas_refresh_keeps_the_last_measured_cost(tmp_path, fake_venue, monkeypatch):
    """A refresh that cannot be priced must not disable the pillars: the last
    measured dollar cost stays in force, and it is never replaced by zero."""
    _market, books = fake_venue
    answers = [0.00032, None, 0.00040]

    class _Rtt:
        def measure(self):
            return 0.01

        def p50(self):
            return 0.01

    engine = engine_mod.PaperEngine(
        {"RewardHarvester": 100.0, "SettlementHarvester": 100.0, "CtfMinter": 100.0},
        http=_Http(),
        gamma=_Gamma(),
        feed=_Feed(books),
        rtt=_Rtt(),
        db_path=str(tmp_path / "gas.db"),
        interval=1.0,
        gas_usd=0.00030,
        resume=False,
        enable_uma_polling=False,
        rtt_refresh_s=60.0,
        gas_probe=lambda: answers.pop(0),
    )
    engine.walker = _Walker([_market])
    monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: [])

    engine._refresh_venue_state()
    assert engine.gas_usd == pytest.approx(0.00032)

    engine._refresh_venue_state()  # probe returns None
    assert engine.gas_usd == pytest.approx(0.00032)

    engine._refresh_venue_state()  # probe recovers
    assert engine.gas_usd == pytest.approx(0.00040)
    engine.ledger.close()


def test_block_interval_is_seconds_per_block_from_the_tape(tmp_path, fake_venue,
                                                           monkeypatch):
    """The poller's liveness maths divides by this constant, so it must be
    seconds per block. Dividing blocks by elapsed measures blocks per second
    and reported 0.79 s while the header tape measured 1.50 s."""
    _market, books = fake_venue
    engine = _engine(tmp_path, books, gas=None)

    monkeypatch.setattr(
        "polymarketbot.strategy.onchain.ctf.load_measured_block_interval_s",
        lambda *a, **kw: 1.501,
    )
    poller = engine._make_uma_poller()
    assert poller is not None
    assert poller.block_interval_s == pytest.approx(1.501)

    # Without a tape, two live heads over five seconds must give 5/4 = 1.25 s
    # per block, not 4/5 = 0.8 blocks per second.
    monkeypatch.setattr(
        "polymarketbot.strategy.onchain.ctf.load_measured_block_interval_s",
        lambda *a, **kw: None,
    )
    engine.interval = 10.0  # the live probe sleeps interval/2 = 5 s
    heads = iter([1000, 1004])
    clock = {"now": 0.0}

    class _Chain:
        def block_number(self):
            return next(heads)

    def _sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr("polymarketbot.strategy.onchain.core.ChainClient", _Chain)
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(engine_mod.time, "sleep", _sleep)

    live = engine._make_uma_poller()
    assert live is not None
    assert live.block_interval_s == pytest.approx(1.25)
    engine.ledger.close()
