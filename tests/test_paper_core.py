"""Focused tests for the rebuilt paper package.

Accounting, latency and book math are pure and tested without a network.
"""

from __future__ import annotations

import pytest

from polymarketbot.strategy.books import (
    best_ask,
    best_bid,
    ladder,
    mid,
    qualifying_score,
    shares_at,
    walk_cost,
)
from polymarketbot.strategy.latency import ActionBook
from polymarketbot.strategy.ledger import Ledger


def _book(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> dict:
    return {
        "bids": [{"price": str(price), "size": str(size)} for price, size in bids],
        "asks": [{"price": str(price), "size": str(size)} for price, size in asks],
    }


# ---------------------------------------------------------------- ledger
def test_buy_sell_and_cash_are_exact(tmp_path):
    ledger = Ledger({"S": 100.0}, tmp_path / "l.db")
    assert ledger.buy("S", condition_id="c", token_id="t", shares=10.0, price=0.40, fee=0.02)
    assert ledger.cash("S") == pytest.approx(100.0 - 4.0 - 0.02)
    pnl = ledger.sell("S", condition_id="c", token_id="t", shares=4.0, price=0.50, fee=0.01)
    assert pnl == pytest.approx(4.0 * 0.50 - 0.01 - 4.0 * 0.40)
    assert ledger.cash("S") == pytest.approx(100.0 - 4.02 + 1.99)
    assert len(ledger.positions("S")) == 1
    assert ledger.positions("S")[0].shares == pytest.approx(6.0)
    ledger.close()


def test_buy_is_refused_when_cash_does_not_cover_fee(tmp_path):
    ledger = Ledger({"S": 5.0}, tmp_path / "l.db")
    assert not ledger.buy("S", condition_id="c", token_id="t", shares=10.0, price=0.50,
                          fee=0.01)
    assert ledger.cash("S") == 5.0
    ledger.close()


def test_merge_burns_pairs_and_pays_par(tmp_path):
    ledger = Ledger({"S": 10.0}, tmp_path / "l.db")
    ledger.buy("S", condition_id="c", token_id="yes", shares=10.0, price=0.40)
    ledger.buy("S", condition_id="c", token_id="no", shares=6.0, price=0.55)
    before = ledger.cash("S")
    pnl = ledger.merge("S", condition_id="c", token_id="yes", pair_shares=6.0, gas=0.0)
    assert pnl == pytest.approx(6.0 * 1.0 - 6.0 * (0.40 + 0.55))
    assert ledger.cash("S") == pytest.approx(before + 6.0)
    assert {lot.token_id: lot.shares for lot in ledger.positions("S")} == {"yes": 4.0}
    ledger.close()


def test_merge_refuses_without_burning_inventory(tmp_path):
    """A merge that cannot complete must not consume either leg.

    The first leg used to be removed before the sibling was checked, so a
    missing or short complement destroyed shares with no journal row and no
    cash movement - an inventory loss the cash-based reconciliation cannot see.
    """
    ledger = Ledger({"S": 100.0}, tmp_path / "l.db")
    ledger.buy("S", condition_id="c", token_id="yes", shares=10.0, price=0.40)
    # No sibling leg at all.
    cash_before, lots_before = ledger.cash("S"), {
        lot.token_id: (lot.shares, lot.cost_usd) for lot in ledger.positions("S")
    }
    assert ledger.merge("S", condition_id="c", token_id="yes", pair_shares=4.0) == 0.0
    assert ledger.cash("S") == pytest.approx(cash_before)
    assert {
        lot.token_id: (lot.shares, lot.cost_usd) for lot in ledger.positions("S")
    } == lots_before

    # Sibling present but short.
    ledger.buy("S", condition_id="c", token_id="no", shares=2.0, price=0.55)
    cash_before, lots_before = ledger.cash("S"), {
        lot.token_id: (lot.shares, lot.cost_usd) for lot in ledger.positions("S")
    }
    assert ledger.merge("S", condition_id="c", token_id="yes", pair_shares=4.0) == 0.0
    assert ledger.cash("S") == pytest.approx(cash_before)
    assert {
        lot.token_id: (lot.shares, lot.cost_usd) for lot in ledger.positions("S")
    } == lots_before

    # And the satisfiable case still merges.
    assert ledger.merge("S", condition_id="c", token_id="yes", pair_shares=2.0) > 0.0
    assert {lot.token_id: lot.shares for lot in ledger.positions("S")} == {
        "yes": 8.0
    }
    ledger.close()


def test_equity_marks_unresolved_at_bid_and_resolved_at_par(tmp_path):
    ledger = Ledger({"S": 100.0}, tmp_path / "l.db")
    ledger.buy("S", condition_id="c", token_id="t", shares=10.0, price=0.50)
    ledger.mark("t", 0.30)
    assert ledger.equity()["S"] == pytest.approx(100.0 - 5.0 + 10.0 * 0.30)
    ledger.set_par("c", "t")
    assert ledger.equity()["S"] == pytest.approx(100.0 - 5.0 + 10.0)
    ledger.close()


def test_resume_restores_cash_and_lots(tmp_path):
    path = tmp_path / "l.db"
    ledger = Ledger({"S": 50.0}, path)
    ledger.buy("S", condition_id="c", token_id="t", shares=8.0, price=0.50)
    ledger.snapshot()
    ledger.close()
    resumed = Ledger({"S": 999.0}, path, resume=True)
    assert resumed.cash("S") == pytest.approx(50.0 - 4.0)
    assert resumed.positions("S")[0].shares == pytest.approx(8.0)
    resumed.close()


def test_missing_lot_sell_is_refused(tmp_path):
    ledger = Ledger({"S": 10.0}, tmp_path / "l.db")
    assert ledger.sell("S", condition_id="c", token_id="t", shares=1.0, price=0.5) == 0.0
    assert ledger.cash("S") == 10.0
    ledger.close()


def test_mark_move_reaches_equity_without_moving_cash(tmp_path):
    """A bid move is PnL: shares x price, zero fills."""
    ledger = Ledger({"S": 100.0}, tmp_path / "l.db")
    assert ledger.buy("S", condition_id="c", token_id="t", shares=200.0, price=0.40)
    cash_before = ledger.cash("S")
    equity_before = ledger.equity()["S"]
    # Entry price alone is not a measurement.
    assert [lot.token_id for lot in ledger.unmarked("S")] == ["t"]
    assert ledger.mark("t", 0.55) is True
    assert ledger.cash("S") == cash_before
    assert ledger.equity()["S"] == pytest.approx(equity_before + 200.0 * 0.15)
    assert ledger.unmarked("S") == []
    assert ledger.mark("t", 0.60) is False  # already live: no new transition
    ledger.close()


def test_vanished_book_is_reported_unmarked_and_a_real_book_clears_it(tmp_path):
    ledger = Ledger({"S": 100.0}, tmp_path / "l.db")
    ledger.buy("S", condition_id="c", token_id="t", shares=100.0, price=0.40)
    ledger.mark("t", 0.55)
    lot = ledger.positions("S")[0]
    equity_live = ledger.equity()["S"]
    assert lot.mark_live is True

    # No bid this tick: the last real measurement is kept as the value (no
    # invented price), but the lot is now explicitly unmarked.
    assert ledger.mark_unmeasured("t", "S") is True
    assert lot.mark_live is False
    assert lot.last_mark == pytest.approx(0.55)
    assert [item.token_id for item in ledger.unmarked("S")] == ["t"]
    assert ledger.equity()["S"] == pytest.approx(equity_live)
    # A second no-book tick is not a new transition.
    assert ledger.mark_unmeasured("t", "S") is False

    assert ledger.mark("t", 0.60, "S") is True
    assert lot.mark_live is True
    assert ledger.unmarked("S") == []
    assert ledger.equity()["S"] == pytest.approx(equity_live + 100.0 * 0.05)
    ledger.close()


def _capture_errors() -> tuple[list[str], int]:
    """Capture loguru ERROR records (the ledger's logger is loguru)."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="ERROR")
    return messages, sink_id


def test_reconciliation_fires_on_a_broken_book_then_clears(tmp_path):
    from loguru import logger

    ledger = Ledger({"S": 100.0}, tmp_path / "l.db")
    ledger.buy("S", condition_id="c", token_id="t", shares=10.0, price=0.40)
    ledger.snapshot()  # clean baseline
    cash = ledger._books["S"].cash

    # Cash moves outside the journal: the decomposition must not stay silent.
    ledger._books["S"].cash = cash + 5.0
    messages, sink_id = _capture_errors()
    try:
        report = ledger.reconcile()
    finally:
        logger.remove(sink_id)
    assert report["S"]["cash"] == pytest.approx(cash + 5.0)
    assert any("accounting defect [S] window" in message for message in messages)
    assert any("journal says $+0.000000" in message for message in messages)

    # Reverted: the broken state was never persisted, so the next check
    # against the clean baseline passes.
    ledger._books["S"].cash = cash
    messages, sink_id = _capture_errors()
    try:
        ledger.reconcile()
    finally:
        logger.remove(sink_id)
    assert not any("accounting defect" in message for message in messages)
    ledger.close()


def test_reconciliation_catches_positions_value_drift(tmp_path):
    from loguru import logger

    ledger = Ledger({"S": 100.0}, tmp_path / "l.db")
    ledger.buy("S", condition_id="c", token_id="t", shares=10.0, price=0.40)
    ledger.mark("t", 0.55)
    book = ledger._books["S"]
    book.positions_value = lambda: 999.0  # a cached/rounded position total
    messages, sink_id = _capture_errors()
    try:
        ledger.reconcile()
    finally:
        logger.remove(sink_id)
    assert any("book reports positions=$999.00" in message for message in messages)
    del book.positions_value
    messages, sink_id = _capture_errors()
    try:
        ledger.reconcile()
    finally:
        logger.remove(sink_id)
    assert not any("accounting defect" in message for message in messages)
    ledger.close()


def test_reconciliation_accepts_a_journaled_accrual(tmp_path):
    from loguru import logger

    ledger = Ledger({"RewardHarvester": 100.0}, tmp_path / "l.db")
    ledger.snapshot()
    ledger.credit("RewardHarvester", amount=1.25, condition_id="c",
                  detail="reward_accrual")
    messages, sink_id = _capture_errors()
    try:
        equity = ledger.snapshot()
    finally:
        logger.remove(sink_id)
    assert not any("accounting defect" in message for message in messages)
    assert equity["RewardHarvester"] == pytest.approx(101.25)
    ledger.close()


# ---------------------------------------------------------------- latency
def test_action_lands_only_after_measured_rtt():
    clock = [0.0]
    actions = ActionBook(lambda: 0.25, clock=lambda: clock[0])
    assert actions.submit("buy", "S", {"x": 1}) is not None
    assert actions.due() == []
    clock[0] = 0.24
    assert actions.due() == []
    clock[0] = 0.25
    assert [a.kind for a in actions.due()] == ["buy"]


def test_paper_engine_refuses_to_schedule_at_zero_latency():
    from types import SimpleNamespace

    from polymarketbot.paper.engine import PaperEngine

    class _Probe:
        @staticmethod
        def p50():
            return None

    with pytest.raises(RuntimeError, match="measured CLOB RTT"):
        PaperEngine._rtt(SimpleNamespace(rtt=_Probe()))


def test_action_is_refused_without_measured_rtt():
    actions = ActionBook(lambda: None)
    assert actions.submit("buy", "S", {}) is None
    assert actions.pending() == []


def test_cancel_removes_pending_action():
    actions = ActionBook(lambda: 10.0)
    actions.submit("quote", "S", {"token_id": "a"})
    actions.submit("quote", "S", {"token_id": "b"})
    assert actions.cancel(lambda action: action.payload["token_id"] == "a") == 1
    assert [action.payload["token_id"] for action in actions.pending()] == ["b"]


# ---------------------------------------------------------------- books
def test_best_mid_and_missing_side():
    book = _book([(0.40, 10), (0.42, 5)], [(0.45, 3), (0.50, 9)])
    assert best_bid(book) == 0.42
    assert best_ask(book) == 0.45
    assert mid(book) == pytest.approx(0.435)
    assert mid({"bids": []}) is None
    assert shares_at(book, bid=True, price=0.42) == 5


def test_ladder_order_and_walk_cost():
    book = _book([], [(0.50, 100), (0.45, 50)])
    assert ladder(book, asks=True) == [(0.45, 50), (0.50, 100)]
    cost, filled = walk_cost(ladder(book, asks=True), 75)
    assert filled == 75
    assert cost == pytest.approx(50 * 0.45 + 25 * 0.50)


def test_qualifying_score_uses_quadratic_distance():
    # 3.5c band, level 1.5c from mid: ((3.5-1.5)/3.5)^2 = 0.3265
    book = _book([(0.485, 100)], [])
    score = qualifying_score(book, max_spread_cents=3.5, mid_price=0.50)
    assert score == pytest.approx(100 * ((3.5 - 1.5) / 3.5) ** 2)
    # Outside the band contributes nothing.
    far = _book([(0.40, 100)], [])
    assert qualifying_score(far, max_spread_cents=3.5, mid_price=0.50) == 0.0


class _StubRttSession:
    """HTTP session whose /time answers instantly."""

    status_code = 200

    def get(self, url, timeout=None):
        return self


def test_rtt_probe_keeps_its_sample_out_of_the_latency_report(tmp_path):
    """The CLOB p50 must not overwrite `local_latency.json`: the on-chain path
    reads that report for the measured Polygon RPC round trip, and clobbering
    it is why no measured RPC RTT ever existed."""
    import json

    from polymarketbot.strategy.latency import RttProbe

    report = tmp_path / "local_latency.json"
    report.write_text(
        json.dumps({"ts_utc": "2026-11-10T04:40:00Z",
                    "endpoints": {"clob_time": {"ttfb_p50_ms": 250.0}}}),
        encoding="utf-8",
    )
    stamp = report.stat().st_mtime_ns

    probe = RttProbe(
        "https://clob.example",
        _StubRttSession(),
        persisted=tmp_path / "clob_rtt.json",
        legacy=report,
    )
    probe.measure()
    probe.measure()

    assert (tmp_path / "clob_rtt.json").exists()
    assert report.stat().st_mtime_ns == stamp, "the latency report must not be rewritten"
    assert json.loads(report.read_text(encoding="utf-8"))["endpoints"]["clob_time"][
        "ttfb_p50_ms"
    ] == 250.0


def test_rtt_probe_seeds_from_a_legacy_sample_when_its_own_file_is_absent(tmp_path):
    import json

    from polymarketbot.strategy.latency import RttProbe

    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"p50_ms": 300.0}), encoding="utf-8")

    probe = RttProbe(
        "https://clob.example",
        _StubRttSession(),
        persisted=tmp_path / "absent.json",
        legacy=legacy,
    )
    assert probe.p50() == pytest.approx(0.300)


def test_paper_main_refuses_the_retired_one_sided_pillar():
    """The one-sided reward pillar must not run by default.

    It quoted one leg per market; the live engine quotes both. The retired path
    is reachable only with `--legacy-one-sided`.
    """
    from polymarketbot.paper.main import main

    assert main(["--capital", "40", "--duration", "1"]) == 2
