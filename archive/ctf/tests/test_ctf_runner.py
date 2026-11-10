"""The archived CTF loop end to end: decide, land both legs, merge to cash.

These are the three engine-level CTF tests from ``tests/test_paper_engine.py``
(parity buys both legs and merges; a merge whose flight is unmeasured leaves the
legs as inventory; an unmeasured gas cost disables the pillar).  The live engine
no longer wires the pillar, so they drive ``archive.ctf.runner.run_ctf`` and
``archive.ctf.landing.land_ctf`` against a minimal engine stub.  Every assertion
is unchanged from the engine test; only the harness is smaller.
"""

from __future__ import annotations

import time

import pytest

from archive.ctf import pillars as ctf
from archive.ctf.harness import Market
from archive.ctf.landing import land_ctf
from archive.ctf.runner import run_ctf
from polymarketbot.fees import FeeSchedule
from polymarketbot.strategy.latency import ActionBook
from polymarketbot.strategy.ledger import Ledger


def _book(bid: float | None, ask: float | None, size: float = 1000.0) -> dict:
    return {
        "bids": [] if bid is None else [{"price": str(bid), "size": str(size)}],
        "asks": [] if ask is None else [{"price": str(ask), "size": str(size)}],
    }


def _market(condition_id: str, yes: str, no: str, *, min_order: float = 5.0) -> Market:
    return Market(
        condition_id=condition_id,
        question="Q",
        outcomes=("Yes", "No"),
        token_ids=(yes, no),
        tick=0.01,
        min_order=min_order,
        daily_reward=0.0,
        reward_max_spread_cents=3.5,
        reward_min_size=20.0,
        fee_schedule=FeeSchedule.zero(),
        neg_risk=False,
        closed=False,
        uma_status="",
        outcome_prices=(0.5, 0.5),
    )


class _Feed:
    """The landing re-read; the stub's books are already fresh."""

    def __init__(self, books: dict) -> None:
        self._books = books

    def books(self, token_ids, force: bool = False):
        return {token: book for token, book in self._books.items() if token in token_ids}


class _Engine:
    """The engine surface ``run_ctf`` and ``land_ctf`` read, and nothing else."""

    def __init__(self, market: Market, books: dict, *, gas_usd: float | None,
                 tmp_path) -> None:
        self.ledger = Ledger({"CtfMinter": 100.0}, str(tmp_path / "ctf.db"))
        self.markets = [market]
        self.by_condition = {market.condition_id: market}
        self._token_index = {token: market for token in market.token_ids}
        self.feed = _Feed(books)
        self.actions = ActionBook(lambda: 0.01)
        self.gas_usd = gas_usd
        self.maker_quotes = {}
        self._fills = {"CtfMinter": 0}
        self._ctf_budget = 1500
        self._ctf_cursor = 0
        self._ctf_coverage_ts = 0.0
        self._last_tick_dt = 1.0
        self._shadow_yield = None

    @staticmethod
    def _reserved(quotes) -> float:
        return sum(q.shares * q.price for q in quotes.values() if q.cancel_at is None)

    def _rtt(self) -> float:
        return self.actions._rtt_source() or 0.0


def test_ctf_parity_buys_both_legs_and_merges_to_cash(tmp_path, monkeypatch):
    market = _market("c2", "y2", "n2")
    books = {"y2": _book(0.39, 0.40), "n2": _book(0.54, 0.55)}
    engine = _Engine(market, books, gas_usd=0.0, tmp_path=tmp_path)
    clock = [1000.0]
    engine.actions = ActionBook(lambda: 0.01, clock=lambda: clock[0])
    # A merge lands after the Polygon round trip plus one block, not after the
    # CLOB round trip; the test supplies that measured flight explicitly.
    monkeypatch.setattr(
        ctf, "resolve_on_chain_timing",
        lambda action, **kw: ctf.OnChainTiming(0.004, 0.002),
    )
    try:
        run_ctf(engine, books, [])
        assert engine.actions.pending(), "parity edge must queue a taker action"
        clock[0] += 1.0
        land_ctf(engine, books)
        clock[0] += 1.0
        land_ctf(engine, books)
        assert engine.ledger.positions("CtfMinter") == []
        # Cash-bounded size = 100 / (0.40 + 0.55) shares, merged at $1: +$0.05/share.
        shares = 100.0 / 0.95
        assert engine.ledger.cash("CtfMinter") == pytest.approx(
            100.0 + shares * 0.05, rel=1e-6
        )
    finally:
        engine.ledger.close()


def test_ctf_merge_is_refused_when_its_flight_is_unmeasured(tmp_path, monkeypatch):
    """Filled parity legs stay as inventory rather than being merged on the
    CLOB round trip: a merge priced with an unmeasured RPC clock would credit
    par for a transaction that was never broadcast."""
    market = _market("c5", "y5", "n5")
    books = {"y5": _book(0.39, 0.40), "n5": _book(0.54, 0.55)}

    def _refuse(action, **kwargs):
        raise ctf.OnChainRefused("on-chain merge refused: no measured RPC round trip")

    monkeypatch.setattr(ctf, "resolve_on_chain_timing", _refuse)
    engine = _Engine(market, books, gas_usd=0.0, tmp_path=tmp_path)
    clock = [1000.0]
    engine.actions = ActionBook(lambda: 0.01, clock=lambda: clock[0])
    try:
        run_ctf(engine, books, [])
        clock[0] += 1.0
        land_ctf(engine, books)
        lots = engine.ledger.positions("CtfMinter")
        assert lots, "both parity legs remain as inventory"
        assert {lot.tag for lot in lots} == {"ctf_leg"}
        assert not [q for q in engine.actions.pending() if q.kind == "ctf_merge"]
    finally:
        engine.ledger.close()


def test_ctf_is_refused_when_gas_is_unmeasured(tmp_path):
    market = _market("c3", "y3", "n3")
    books = {"y3": _book(0.39, 0.40), "n3": _book(0.54, 0.55)}
    engine = _Engine(market, books, gas_usd=None, tmp_path=tmp_path)
    try:
        run_ctf(engine, books, [])
        assert engine.actions.pending() == []
        assert engine.ledger.cash("CtfMinter") == 100.0
    finally:
        engine.ledger.close()
