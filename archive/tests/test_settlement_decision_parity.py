"""Parity: the live settlement buy and the paper pillar are one decision.

The live trader had no settlement sizing at all before ``live/settlement.py``;
it now calls the same modules the audited paper pillar calls. This test pins
that down for one fixed candidate set and one fixed tail budget: the conditions
that get funded, the share counts and the total cost must be identical, because
both sides run

* :func:`polymarketbot.strategy.settlement.decision.verdict_for` /
  ``edge_inputs`` for the gate,
* ``MarginalSettlementAllocator.optimize_tail_budget`` for the dollars, and
* :func:`polymarketbot.strategy.settlement.decision.fundable_legs` to turn those
  dollars into legs on the venue's ladder.

The paper side runs the real pillar functions (``tail_budget`` from
``paper/pillars/tail_budget.py`` and ``land_settlement`` from
``paper/actions.py``), driven by the same loop body as
``paper/pillars/settlement_runner.run_settlement``. The live side runs
``live.settlement.decide``. Anything that made the two disagree - a different
gas figure, a different horizon, a fee-exclusive grant, a re-implemented gate -
would move a share count here.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from polymarketbot.fees import FeeSchedule
from polymarketbot.live import settlement as live
from polymarketbot.paper.actions import land_settlement
from polymarketbot.paper.pillars.tail_budget import tail_budget
from polymarketbot.paper.settlement_risk import SettlementTailPolicy
from polymarketbot.strategy.books import best_ask
from polymarketbot.strategy.ledger import Ledger
from polymarketbot.strategy.latency import ActionBook
from polymarketbot.strategy.settlement import decision as settlement
from polymarketbot.strategy.settlement.allocator import MarginalSettlementAllocator
from polymarketbot.strategy.settlement.fair_value import OracleVerdict
from polymarketbot.strategy.settlement.tail_evidence import evidence_from_counts
from polymarketbot.strategy.venue.reads import Market

# The measured window-9 book (2026-11-10 14:25): one condition, eight levels.
CASH = 1000.0
EQUITY = 1000.0
GAS_USD = 0.012
SETTLE_SEC = 1800.0
P_WIN = 0.98947  # 1 - the measured 1.0530 % dispute hazard
SHADOW_YIELD = 0.0042  # measured reward-pillar dollars per dollar per day

APPETITE = 150.0       # configs/risk.yaml
CONFIDENCE = 0.995     # configs/risk.yaml

C1 = "0xbcd8bf70c6"
C1_Q = "Will the window-9 condition resolve YES?"
C2 = "0x7a11c0ffee"
C2_Q = "Will the second condition resolve YES?"

# (condition, question, ladder) - two candidates so the allocator's *split* is
# part of what is being compared, not just its total.
CANDIDATES = (
    (C1, C1_Q, ((0.8100, 20.0), (0.8700, 84.61), (0.9600, 19.83), (0.9700, 300.0))),
    (C2, C2_Q, ((0.9000, 60.0), (0.9400, 40.0), (0.9750, 500.0))),
)


def _schedule() -> FeeSchedule:
    """Gamma's politics taker schedule, the one the paper pillar pays."""
    return FeeSchedule(rate=0.04, exponent=1.0, taker_only=True,
                       rebate_rate=0.0, source="test:politics")


def _market(condition_id: str, question: str, token_ids: tuple[str, str]) -> Market:
    return Market(
        condition_id=condition_id,
        question=question,
        outcomes=("Yes", "No"),
        token_ids=token_ids,
        tick=0.001,
        min_order=5.0,
        daily_reward=0.0,
        reward_max_spread_cents=0.0,
        reward_min_size=0.0,
        fee_schedule=_schedule(),
        neg_risk=False,
        closed=False,
        uma_status="proposed",
        outcome_prices=(),
        category="politics",
    )


def _fixed_model(verdicts: dict[str, OracleVerdict]) -> SimpleNamespace:
    """A model that answers with a fixed oracle verdict, and nothing else.

    ``refresh`` and ``tape.measured_liveness_sec`` are the two facts the decision
    reads besides the verdict itself; both are pinned so the two sides cannot
    disagree about the horizon.
    """

    def fair_value(*, condition_id: str, **_kwargs):
        return verdicts.get(condition_id)

    return SimpleNamespace(
        fair_value=fair_value,
        refresh=lambda *a, **k: None,
        tape=SimpleNamespace(measured_liveness_sec=SETTLE_SEC),
    )


def _paper_books(condition_id: str, ladder_rows) -> dict[str, dict]:
    token = f"{condition_id}-yes"
    return {
        token: {
            "asks": [{"price": str(px), "size": str(sz)} for px, sz in ladder_rows],
            "bids": [],
        },
        f"{condition_id}-no": {"asks": [], "bids": []},
    }


def _fixture(tmp_path):
    """The same facts for both sides: markets, verdicts, books, inputs."""
    markets: dict[str, Market] = {}
    verdicts: dict[str, OracleVerdict] = {}
    books: dict[str, dict] = {}
    for condition_id, question, ladder_rows in CANDIDATES:
        tokens = (f"{condition_id}-yes", f"{condition_id}-no")
        markets[condition_id] = _market(condition_id, question, tokens)
        verdicts[condition_id] = OracleVerdict(
            winning_index=0, p_win=P_WIN, source="uma_tape", observed_ts=time.time()
        )
        books.update(_paper_books(condition_id, ladder_rows))
    model = _fixed_model(verdicts)
    policy = SettlementTailPolicy(
        risk_appetite_usd=APPETITE, tail_confidence=CONFIDENCE, source="test"
    )
    evidence = evidence_from_counts(
        70, 0, disputed=0, source="test", challenge_trials=134664, challenges=158
    )
    return markets, books, model, policy, evidence


def _run_paper(tmp_path, markets, books, model, policy, evidence):
    """The paper pillar's tick and landing, over the same facts.

    This is ``paper/pillars/settlement_runner.run_settlement``'s loop: price with
    the decision module, ask the allocator for dollars, land them with
    ``land_settlement`` (which re-derives the legs with ``fundable_legs``).
    """
    ledger = Ledger({live.STRATEGY: CASH}, tmp_path / "paper.db")
    engine = SimpleNamespace(
        ledger=ledger,
        settlement_model=model,
        by_condition=dict(markets),
        gas_usd=GAS_USD,
        settlement_allocator=MarginalSettlementAllocator("tail_budget"),
        _tail_policy=policy,
        _tail_evidence=evidence,
        _shadow_yield=SHADOW_YIELD,
        _fills={live.STRATEGY: 0},
    )
    priced = []
    for condition_id, market in markets.items():
        verdict = settlement.verdict_for(model, market)
        if verdict is None:
            continue
        token_id = market.token_ids[verdict.winning_index]
        ask = best_ask(books.get(token_id) or {})
        if ask is None:
            continue
        edge = settlement.marginal_edge(market, verdict, ask)
        if edge is None or edge <= 0.0:
            continue
        priced.append((edge / ask, market, ask, verdict))
    budget = tail_budget(engine, priced, books, {}, SETTLE_SEC,
                         engine.ledger.cash(live.STRATEGY))
    remaining = engine.ledger.cash(live.STRATEGY)
    try:
        for _edge, market, ask, _verdict in sorted(priced, key=lambda item: -item[0]):
            cap = budget.get(market.condition_id, 0.0)
            if cap <= 0.0 or ask * max(market.min_order, 1.0) > cap + 1e-9:
                continue
            cap = min(cap, remaining)
            land_settlement(
                engine, live.STRATEGY,
                {"condition_id": market.condition_id, "max_cost": cap},
                books,
            )
            remaining -= cap
        lots = ledger.positions(live.STRATEGY)
        shares: dict[str, float] = {}
        for lot in lots:
            shares[lot.condition_id] = shares.get(lot.condition_id, 0.0) + lot.shares
        return {
            "conditions": set(shares),
            "shares": shares,
            "total_shares": sum(shares.values()),
            "cost_usd": CASH - ledger.cash(live.STRATEGY),
        }
    finally:
        ledger.close()


def _run_live(markets, books, model, policy, evidence):
    """The live decision over the same facts, through the new module."""
    priced = [
        live.PricedMarket(
            market=market,
            ladders={
                token: tuple(
                    (float(level["price"]), float(level["size"]))
                    for level in (books.get(token) or {}).get("asks") or []
                )
                for token in market.token_ids
            },
        )
        for market in markets.values()
    ]
    decision = live.decide(
        priced=priced,
        cash_usd=CASH,
        model=model,
        policy=policy,
        evidence=evidence,
        gas_usd=GAS_USD,
        settle_sec=SETTLE_SEC,
        prior=(),
        equity_usd=EQUITY,
        shadow_yield=SHADOW_YIELD,
        allocator=MarginalSettlementAllocator("tail_budget"),
    )
    return {
        "decision": decision,
        "conditions": {grant.condition_id for grant in decision.grants},
        "shares": {grant.condition_id: grant.shares for grant in decision.grants},
        "total_shares": sum(grant.shares for grant in decision.grants),
        "cost_usd": decision.all_in_usd,
    }


def test_live_and_paper_size_the_same_positions_from_the_same_candidates(tmp_path):
    """Same conditions, same share counts, same total cost."""
    markets, books, model, policy, evidence = _fixture(tmp_path)
    paper = _run_paper(tmp_path, markets, books, model, policy, evidence)
    paper_ledger_cash = CASH - paper["cost_usd"]
    live_side = _run_live(markets, books, model, policy, evidence)

    # The decision actually funded something on both sides, or the test is vacuous.
    assert paper["conditions"], "the paper pillar funded nothing"
    assert live_side["conditions"], live_side["decision"].describe()

    assert live_side["conditions"] == paper["conditions"]
    for condition_id, shares in paper["shares"].items():
        assert live_side["shares"][condition_id] == pytest.approx(shares, rel=1e-12), (
            condition_id
        )
    assert live_side["total_shares"] == pytest.approx(paper["total_shares"], rel=1e-12)
    assert live_side["cost_usd"] == pytest.approx(paper["cost_usd"], rel=1e-12)

    # The grant is the whole order and stays inside the cash it was given.
    assert paper_ledger_cash >= 0.0
    assert live_side["cost_usd"] <= CASH + 1e-9

    # Both sides priced the same measured tail, so the spend cannot exceed the
    # same band-limited capacity.
    decision = live_side["decision"]
    assert decision.tail_report is not None
    q = decision.tail_report.tail_pd
    assert decision.tail_budget_usd == pytest.approx(
        (1.0 - CONFIDENCE) * APPETITE / q, rel=1e-9
    )
    assert decision.all_in_usd <= decision.tail_budget_usd * (1.0 + 1e-6) + 1e-3


def test_live_refuses_when_the_cost_of_the_exit_is_unmeasured(tmp_path):
    """An unmeasured gas/POL input is a refusal, and it is the only difference."""
    markets, _books, model, policy, evidence = _fixture(tmp_path)
    decision = live.decide(
        priced=[
            live.PricedMarket(
                market=market,
                ladders={
                    token: ((0.96, 100.0),)
                    for token in market.token_ids
                },
            )
            for market in markets.values()
        ],
        cash_usd=CASH,
        model=model,
        policy=policy,
        evidence=evidence,
        gas_usd=None,
        settle_sec=SETTLE_SEC,
        equity_usd=EQUITY,
        shadow_yield=SHADOW_YIELD,
    )
    assert decision.grants == ()
    assert [reason for reason, _detail in decision.refusals] == [live.GAS_UNMEASURED]
    # Nothing was sized, so nothing may be reported as a tail budget either.
    assert decision.tail_report is None
    assert "gas UNMEASURED" in decision.describe()


def test_live_refuses_a_market_without_an_oracle_verdict(tmp_path):
    """No proposal, no price: the book is never substituted for the verdict."""
    markets, _books, _model, policy, evidence = _fixture(tmp_path)
    empty_model = _fixed_model({})
    decision = live.decide(
        priced=[
            live.PricedMarket(
                market=market,
                ladders={token: ((0.96, 100.0),) for token in market.token_ids},
            )
            for market in markets.values()
        ],
        cash_usd=CASH,
        model=empty_model,
        policy=policy,
        evidence=evidence,
        gas_usd=GAS_USD,
        settle_sec=SETTLE_SEC,
        equity_usd=EQUITY,
        shadow_yield=SHADOW_YIELD,
    )
    assert decision.grants == ()
    assert {reason for reason, _detail in decision.refusals} == {live.NO_VERDICT}


# ---------------------------------------------------------------------------
# Landing: the order pays one measured round trip and re-reads the book
# ---------------------------------------------------------------------------
class _Book:
    """A book reader whose snapshot can be replaced, standing in for the feed."""

    def __init__(self, books: dict[str, dict]):
        self.books = books

    def __call__(self, token_id: str):
        return self.books.get(token_id)


def _desk(tmp_path, books, model, policy, evidence, *, rtt, placed):
    clock = [1000.0]
    actions = ActionBook(lambda: rtt, clock=lambda: clock[0])
    desk = live.LiveSettlementDesk.build(
        actions=actions,
        book_reader=_Book(books),
        place_taker=lambda token_id, price, shares: placed.append(
            (token_id, price, shares)
        )
        or True,
        gas_probe=lambda **kwargs: GAS_USD,
        policy=policy,
        evidence=evidence,
        model=model,
    )
    return desk, clock


def test_live_desk_lands_after_the_measured_flight_against_the_fresh_book(
    tmp_path,
):
    """The order is not executed where it is decided, and re-prices on landing."""
    markets, books, model, policy, evidence = _fixture(tmp_path)
    placed: list[tuple[str, float, float]] = []
    desk, clock = _desk(tmp_path, books, model, policy, evidence, rtt=0.25, placed=placed)

    decision = desk.plan(
        markets=[markets[C1]],
        cash_usd=CASH,
        equity_usd=EQUITY,
        shadow_yield=SHADOW_YIELD,
    )
    assert decision.grants, decision.describe()
    queued = desk.pending()
    assert [action.kind for action in queued] == ["settle_buy"]
    assert queued[0].lands - queued[0].submitted == pytest.approx(0.25)

    # Nothing lands before the flight has elapsed.
    assert desk.land_due() == []
    assert placed == []

    # The book improves on the way: a cheaper ask appears at the landing price.
    token_id = f"{C1}-yes"
    books[token_id] = {
        "asks": [{"price": "0.8000", "size": "50.0"}],
        "bids": [],
    }
    clock[0] += 0.25
    landed = desk.land_due()
    assert len(landed) == 1
    assert landed[0].flight_seconds == pytest.approx(0.25)
    assert landed[0].accepted
    # Landed against the fresh book - the 0.81-0.97 ladder it was decided on is
    # gone - and inside the allocator's grant.
    assert landed[0].limit_price == pytest.approx(0.80)
    assert placed and placed[0][:2] == (token_id, pytest.approx(0.80))
    assert landed[0].cost_usd + landed[0].fee_usd <= decision.grants[0].all_in_usd + 1e-9


def _deep_book() -> dict[str, dict]:
    """A book whose deepest funded level is far above the walk's average price."""
    ladder_rows = CANDIDATES[0][2]
    return {
        f"{C1}-yes": {
            "asks": [{"price": str(px), "size": str(sz)} for px, sz in ladder_rows],
            "bids": [],
        },
        f"{C1}-no": {"asks": [], "bids": []},
    }


def test_live_desk_landing_cannot_debit_more_than_the_grant(tmp_path):
    """One fill-or-kill limit at the deepest level, sized to the worst case.

    A marketable limit does not bound the spend by the walk's *average* price:
    sized on the walk it could debit ``shares * deepest`` instead. The landing
    therefore caps the order at the limit, so every share filling there still
    fits the allocator's grant.
    """
    markets, books, model, policy, evidence = _fixture(tmp_path)
    books = _deep_book()
    placed: list[tuple[str, float, float]] = []
    desk, clock = _desk(tmp_path, books, model, policy, evidence, rtt=0.05, placed=placed)

    decision = desk.plan(
        markets=[markets[C1]],
        cash_usd=CASH,
        equity_usd=EQUITY,
        shadow_yield=SHADOW_YIELD,
    )
    grant = decision.grants[0]
    assert grant.avg_price < max(price for price, _size in CANDIDATES[0][2])

    clock[0] += 0.05
    landed = desk.land_due()
    assert len(landed) == 1
    landing = landed[0]
    assert landing.limit_price == pytest.approx(max(px for px, _sz in CANDIDATES[0][2]))
    assert landing.cost_usd + landing.fee_usd <= grant.all_in_usd + 1e-9
    assert landing.shares <= grant.shares + 1e-9
    assert placed[0][2] == pytest.approx(landing.shares)


def test_live_desk_submits_nothing_without_a_measured_round_trip(tmp_path):
    """No measured RTT means no order - never an execution at 0 ms."""
    markets, books, model, policy, evidence = _fixture(tmp_path)
    placed: list[tuple[str, float, float]] = []
    desk, _clock = _desk(tmp_path, books, model, policy, evidence, rtt=None, placed=placed)

    decision = desk.plan(
        markets=[markets[C1]],
        cash_usd=CASH,
        equity_usd=EQUITY,
        shadow_yield=SHADOW_YIELD,
    )
    assert decision.grants == ()
    assert [reason for reason, _detail in decision.refusals] == [live.NO_RTT]
    assert desk.pending() == []
    assert placed == []
