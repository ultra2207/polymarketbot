"""Archived settlement-pillar tests: the verdict funding path, end to end.

Split out of ``tests/test_paper_pillars.py`` when the SettlementHarvester
strategy was archived; the reward half stayed in ``tests/``. These tests pin
the settlement funding path (verdict gate, fee-aware ladder, tail budget and
the engine's landing) and are preserved with the strategy they describe.
"""

from __future__ import annotations

import contextlib
import tempfile
import time
from pathlib import Path

import duckdb
import pytest
import yaml

from polymarketbot.fees import FeeSchedule, taker_fee
from polymarketbot.paper import engine as engine_mod
from polymarketbot.paper import radar
from polymarketbot.strategy.latency import ActionBook
import polymarketbot.strategy.settlement.decision as settlement
from polymarketbot.strategy.venue.reads import Market
from polymarketbot.strategy.settlement.hazard import (
    BayesianDisputeTracker,
    MeasuredDisputeCounts,
)
from polymarketbot.strategy.settlement.fair_value import (
    OracleVerdict,
    SettlementFairValueModel,
    UmaVerdictTape,
)


def _market(*, reward_rate: float = 0.0, max_spread: float = 3.5,
            reward_min: float = 200.0, min_order: float = 5.0,
            tick: float = 0.01, fee_rate: float = 0.0,
            rebate_rate: float = 0.0) -> Market:
    schedule = FeeSchedule(rate=fee_rate, exponent=1.0, taker_only=True,
                           rebate_rate=rebate_rate, source="test")
    return Market(
        condition_id="c1",
        question="Q",
        outcomes=("Yes", "No"),
        token_ids=("yes", "no"),
        tick=tick,
        min_order=min_order,
        daily_reward=reward_rate,
        reward_max_spread_cents=max_spread,
        reward_min_size=reward_min,
        fee_schedule=schedule,
        neg_risk=False,
        closed=False,
        uma_status="",
        outcome_prices=(0.5, 0.5),
    )


def _book(bid: float, ask: float, size: float = 100.0) -> dict:
    return {
        "bids": [{"price": str(bid), "size": str(size)}],
        "asks": [{"price": str(ask), "size": str(size)}],
    }
def _verdict(p_win: float, source: str = "gamma_payout") -> OracleVerdict:
    return OracleVerdict(0, p_win, source, time.time())


def test_settlement_funds_only_positive_marginal_edge():
    market = _market(fee_rate=0.8)
    ladder = [(0.90, 100.0), (0.96, 100.0)]
    legs = settlement.fundable_legs(market, _verdict(0.99), ladder, cash_usd=1000.0)
    # taker_fee(1, 0.96) = 0.03072 > 0.03 edge, so the second level is refused.
    assert len(legs) == 1
    assert legs[0].price == pytest.approx(0.90)
    assert legs[0].shares == pytest.approx(100.0)
    assert legs[0].edge_usd > 0.0


def test_settlement_sizes_to_cash_when_edge_holds():
    market = _market(fee_rate=0.0)
    legs = settlement.fundable_legs(market, _verdict(1.0), [(0.90, 100.0)],
                                    cash_usd=50.0)
    assert legs[0].shares == pytest.approx(50.0 / 0.90)
    assert legs[0].fee_usd == 0.0


def test_settlement_marks_gamma_payout_at_par_and_tape_at_p_win():
    market = _market()
    assert settlement.mark_value(market, _verdict(1.0)) == 1.0
    assert settlement.mark_value(market, _verdict(0.97, "uma_tape")) == pytest.approx(0.97)
    assert settlement.mark_value(market, None) == 0.0


def test_settlement_without_a_fee_schedule_is_unfundable():
    market = _market()
    market = Market(**{**market.__dict__, "fee_schedule": None})
    assert settlement.fundable_legs(market, _verdict(1.0), [(0.9, 100.0)], 1000.0) == []


# ------------------------------------------- settlement: measured fixture
#
# The fixture below is the market the 2026-11-10 04:22-04:52 IST soak actually
# held a verdict for: FL-26 Republican, UMA ``proposed`` YES, Gamma
# ``politics_fees`` (rate 0.04), ``orderMinSize`` 5, tick 0.001, and a YES book
# whose only resting interest was a 0.999 *bid*.  The verdict's probability is
# the measured UMA posterior, not a guess: 235 ``YES_OR_NO_QUERY`` proposals
# with 0 disputes on the tape (data/derived/uma/settlement_discount_summary.json)
# give a Clopper-Pearson upper-95 dispute hazard of 1.2614 %, i.e.
# ``p_win = 0.9873864``.

MEASURED_PROPOSALS = 235
MEASURED_DISPUTES = 0
MEASURED_P_WIN = 0.9873864

FL26_CONDITION = "0x2d0d0159d0ed"
FL26_QUESTION = "Will the Republican Party win the FL-26 House seat?"


def _measured_tracker() -> BayesianDisputeTracker:
    tracker = BayesianDisputeTracker()
    tracker.seed_measured(
        MeasuredDisputeCounts(
            proposals=MEASURED_PROPOSALS,
            disputes=MEASURED_DISPUTES,
            source="uma tape 2026-11-09",
        )
    )
    return tracker


def _era_rates() -> dict:
    """The fee rates in force today, straight from ``configs/fees.yaml``."""
    cfg = yaml.safe_load(Path("configs/fees.yaml").read_text(encoding="utf-8"))
    latest = sorted(cfg["eras"], key=lambda era: era["effective_from"])[-1]
    return dict(latest["rates"])


def _schedule(category: str) -> FeeSchedule:
    return FeeSchedule(
        rate=float(_era_rates()[category]),
        exponent=1.0,
        taker_only=True,
        rebate_rate=0.0,
        source=f"fees.yaml:{category}",
    )


def _verdict_market(*, fee_category: str = "politics", **overrides) -> Market:
    fields = dict(
        condition_id=FL26_CONDITION,
        question=FL26_QUESTION,
        outcomes=("Yes", "No"),
        token_ids=("yes-fl26", "no-fl26"),
        tick=0.001,
        min_order=5.0,
        daily_reward=0.0,
        reward_max_spread_cents=0.0,
        reward_min_size=0.0,
        fee_schedule=_schedule(fee_category),
        neg_risk=False,
        closed=False,
        uma_status="proposed",
        outcome_prices=(0.9995, 0.0005),
        category=fee_category,
    )
    fields.update(overrides)
    return Market(**fields)


@contextlib.contextmanager
def _uma_tape(rows):
    """A real UMA proposal parquet the production tape reader can load.

    ``rows`` is ``(condition_id, question, proposed_price, ts, expiry)`` with
    ``proposed_price`` in the oracle's 1e18 fixed point.  The directory sits
    inside the repo because ``UmaVerdictTape._signature`` globs a *relative*
    pattern from the cwd: the change detector and the reader must see the same
    files, so the fixture cannot live in a system temp dir.
    """
    with tempfile.TemporaryDirectory(dir=".", prefix=".uma_tape_fixture_") as root:
        (Path(root) / "uma").mkdir()
        values = ",".join(
            "('ProposePrice','YES_OR_NO_QUERY','{price}',{ts},{expiry},false,"
            "'{cid}','{question}')".format(
                price=price,
                ts=int(ts * 1000.0),
                expiry=int(expiry),
                cid=cid,
                question=question.replace("'", "''"),
            )
            for cid, question, price, ts, expiry in rows
        )
        target = Path(root) / "uma" / "part.parquet"
        con = duckdb.connect()
        try:
            con.execute(
                "COPY (SELECT * FROM (VALUES " + values + ") AS t(event,"
                " identifier_text, proposed_price, ts, expiry, disputed,"
                f" condition_id, question)) TO '{target}' (FORMAT parquet)"
            )
        finally:
            con.close()
        yield f"{Path(root).name}/uma/*.parquet"


def _live_model(glob: str) -> SettlementFairValueModel:
    model = SettlementFairValueModel(
        dispute_tracker=_measured_tracker(),
        tape=UmaVerdictTape(glob=glob, overlay_path=""),
    )
    model.refresh(force=True)
    return model


def _max_ev_ask(market: Market, verdict: OracleVerdict) -> float:
    """Largest ask whose marginal edge is still strictly positive."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) * 0.5
        if settlement.marginal_edge(market, verdict, mid) > 0.0:
            lo = mid
        else:
            hi = mid
    return lo


def test_settlement_funds_a_live_uma_verdict_end_to_end():
    """The whole funding path on the measured window fixture: tape -> leg."""
    now = time.time()
    with _uma_tape([(FL26_CONDITION, FL26_QUESTION, 10**18, now, now + 1800.0)]) as glob:
        model = _live_model(glob)
        market = _verdict_market()
        verdict = settlement.verdict_for(model, market, now=now)

        assert verdict is not None, "a live undisputed proposal must be a verdict"
        assert verdict.source == "uma_tape"
        assert verdict.winning_index == 0, "UMA proposed YES, so token 0 pays"
        assert verdict.p_win == pytest.approx(MEASURED_P_WIN, abs=1e-6)

        # Ask levels and depths measured on 2026-11-10 in the live universe:
        # 0.9610 x120 (politics), 0.9830 x54 (politics), 0.9880 x10199 (the only
        # live ``proposed`` market found with an ask).
        ladder = [(0.9610, 120.0), (0.9830, 54.0), (0.9880, 10199.0)]
        legs = settlement.fundable_legs(market, verdict, ladder, 1000.0)

        assert [leg.price for leg in legs] == [pytest.approx(0.9610),
                                              pytest.approx(0.9830)]
        assert [leg.shares for leg in legs] == [pytest.approx(120.0),
                                                pytest.approx(54.0)]
        assert all(leg.token_id == "yes-fl26" for leg in legs)
        spent = sum(leg.shares * leg.price + leg.fee_usd for leg in legs)
        assert spent <= 1000.0
        assert sum(leg.edge_usd for leg in legs) == pytest.approx(
            sum(leg.shares for leg in legs) * verdict.p_win - spent
        )
        assert sum(leg.edge_usd for leg in legs) > 0.0
        # 0.9880 is above the break-even ask: the one live ``proposed`` market
        # in the sampled universe is not fundable at the measured hazard.
        assert settlement.marginal_edge(market, verdict, 0.9880) < 0.0


@pytest.mark.parametrize("category", ["politics", "other", "crypto", "geopolitics"])
def test_settlement_break_even_ask_comes_from_the_fee_schedule(category):
    """At the measured p_win only an ask below ~0.9869 is +EV, fee included."""
    market = _verdict_market(fee_category=category)
    verdict = OracleVerdict(0, MEASURED_P_WIN, "uma_tape", time.time())
    bound = _max_ev_ask(market, verdict)
    rate = market.fee_schedule.rate
    # fee per share = rate * p * (1-p) <= rate / 4, so the break-even ask is
    # between p_win - rate/4 and p_win, and it is exactly p_win when fee-free.
    assert MEASURED_P_WIN - rate / 4.0 - 1e-12 <= bound <= MEASURED_P_WIN + 1e-12
    assert settlement.marginal_edge(market, verdict, bound * (1.0 - 1e-9)) > 0.0
    assert settlement.marginal_edge(market, verdict, bound + 1e-4) <= 0.0
    if rate == 0.0:
        assert bound == pytest.approx(MEASURED_P_WIN, abs=1e-9)


def test_settlement_one_thousand_dollars_at_the_measured_ask():
    """Cash, not depth, binds at 0.9830: $1,000 buys ~1,016.6 shares."""
    market = _verdict_market()
    verdict = OracleVerdict(0, MEASURED_P_WIN, "uma_tape", time.time())
    legs = settlement.fundable_legs(market, verdict, [(0.9830, 10_000.0)], 1000.0)
    assert len(legs) == 1
    leg = legs[0]
    spent = leg.shares * leg.price + leg.fee_usd
    assert spent <= 1000.0
    assert 1000.0 - spent < leg.price, "the budget, not the depth, sized this leg"
    per_share = 0.9830 + taker_fee(1.0, 0.9830, market.fee_schedule)
    assert leg.shares == pytest.approx(1000.0 / per_share, rel=1e-3)
    assert leg.edge_usd > 0.0


def test_settlement_marginal_edge_is_none_when_it_cannot_be_priced():
    market = _verdict_market()
    verdict = OracleVerdict(0, MEASURED_P_WIN, "uma_tape", time.time())
    assert settlement.marginal_edge(market, None, 0.98) is None
    assert settlement.marginal_edge(market, verdict, 1.0) is None
    assert settlement.marginal_edge(market, verdict, 0.0) is None
    unscheduled = _verdict_market(fee_schedule=None)
    assert settlement.marginal_edge(unscheduled, verdict, 0.98) is None
    # The gate and the ladder must agree level by level.
    for price in (0.90, 0.9610, 0.9830, 0.9868, 0.9880, 0.999):
        edge = settlement.marginal_edge(market, verdict, price)
        funded = settlement.fundable_legs(market, verdict, [(price, 100.0)], 1000.0)
        assert (edge is not None and edge > 0.0) == bool(funded), price


def test_settlement_pending_or_disputed_status_is_never_a_payout():
    """``closed`` alone is not a payout: 476 of 12,000 closed markets are pending.

    Measured 2026-11-10: 472 closed markets carried ``proposed`` with an exact
    1/0 vector, 2 ``disputed``, 2 ``requested``.  Paying par on those bypasses
    the 1.2614 % dispute hazard the tape verdict applies.
    """
    model = SettlementFairValueModel(
        dispute_tracker=_measured_tracker(),
        tape=UmaVerdictTape(glob="no-such-tape/*.parquet", overlay_path=""),
    )
    for status in ("proposed", "disputed", "requested"):
        assert model.final_payout(["1", "0"], True, status) is None, status
        assert model.final_payout(["1", "0"], False, status) is None, status
    for status in ("resolved", "settled", ""):
        assert model.final_payout(["1", "0"], True, status) == 0, status
    assert model.final_payout(["1", "0"], False, "resolved") == 0
    assert model.final_payout(["1", "0"], False, "") is None

    # A pending market with a 1/0 print must fall through to the tape verdict,
    # which carries the hazard, rather than claim par.
    now = time.time()
    with _uma_tape([(FL26_CONDITION, FL26_QUESTION, 10**18, now, now + 1800.0)]) as glob:
        live = _live_model(glob)
        pending = _verdict_market(outcome_prices=(1.0, 0.0))
        verdict = settlement.verdict_for(live, pending, now=now)
        assert verdict is not None
        assert verdict.source == "uma_tape"
        assert verdict.p_win == pytest.approx(MEASURED_P_WIN, abs=1e-6)
        assert settlement.mark_value(pending, verdict) == pytest.approx(MEASURED_P_WIN)
        resolved = _verdict_market(outcome_prices=(1.0, 0.0), uma_status="resolved")
        final = settlement.verdict_for(live, resolved, now=now)
        assert final is not None and final.source == "gamma_payout"
        assert settlement.mark_value(resolved, final) == 1.0


# ------------------------------- settlement through the engine (fakes)
class _FakeFeed:
    def __init__(self, books: dict):
        self._books = books

    def books(self, token_ids, force: bool = False):
        return {token: self._books[token] for token in token_ids if token in self._books}

    def snapshot(self, max_age=None):
        return dict(self._books)


class _FakeGamma:
    def markets(self, condition_ids):
        return []


class _FakeRtt:
    def p50(self):
        return 0.01

    def measure(self):
        return 0.01


class _FakeWalker:
    def __init__(self, markets):
        self._markets = list(markets)
        self.complete = True

    def snapshot(self):
        return list(self._markets)

    def step(self):
        return list(self._markets), True


def _settlement_engine(tmp_path, market: Market, books: dict, model):
    engine = engine_mod.PaperEngine(
        {"RewardHarvester": 1000.0, "SettlementHarvester": 1000.0, "CtfMinter": 1000.0},
        http=object(),
        gamma=_FakeGamma(),
        feed=_FakeFeed(books),
        rtt=_FakeRtt(),
        db_path=str(tmp_path / "engine.db"),
        interval=1.0,
        gas_usd=None,
        enable_uma_polling=False,
    )
    engine.walker = _FakeWalker([market])
    engine.settlement_model = model
    return engine


def _fl26_books(*, yes_asks: list[tuple[float, float]],
                yes_bids: list[tuple[float, float]]) -> dict:
    """The measured FL-26 Republican book: bids 0.999 x14,469.67 / 0.985 x102."""
    return {
        "yes-fl26": {
            "asks": [{"price": str(px), "size": str(sz)} for px, sz in yes_asks],
            "bids": [{"price": str(px), "size": str(sz)} for px, sz in yes_bids],
        },
        "no-fl26": {"asks": [], "bids": []},
    }


def test_settlement_engine_funds_a_live_verdict_market(tmp_path, monkeypatch):
    now = time.time()
    with _uma_tape([(FL26_CONDITION, FL26_QUESTION, 10**18, now, now + 1800.0)]) as glob:
        model = _live_model(glob)
        market = _verdict_market()
        books = _fl26_books(yes_asks=[(0.9830, 54.0), (0.9880, 10199.0)],
                            yes_bids=[(0.9990, 14469.67), (0.9850, 102.0)])
        monkeypatch.setattr(settlement, "model", lambda **kwargs: model)
        monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: [])
        engine = _settlement_engine(tmp_path, market, books, model)
        clock = [1000.0]
        engine.actions = ActionBook(lambda: 0.01, clock=lambda: clock[0])
        try:
            engine._tick()
            assert [action.kind for action in engine.actions.pending()] == ["settle_buy"]

            clock[0] += 1.0
            engine._land(books, [])

            lots = engine.ledger.positions("SettlementHarvester")
            assert len(lots) == 1
            assert lots[0].tag == "settlement"
            assert lots[0].token_id == "yes-fl26"
            assert lots[0].shares == pytest.approx(54.0)
            verdict = settlement.verdict_for(model, market)
            assert verdict is not None
            assert verdict.p_win == pytest.approx(MEASURED_P_WIN, abs=1e-6)
            assert lots[0].par_value == pytest.approx(verdict.p_win, abs=1e-9)
            spent = 1000.0 - engine.ledger.cash("SettlementHarvester")
            assert spent == pytest.approx(
                54.0 * 0.9830 + taker_fee(54.0, 0.9830, market.fee_schedule), rel=1e-9
            )
            # Equity books the oracle's value, not the 0.999 bid or the 0.983 pay.
            assert engine.ledger.equity()["SettlementHarvester"] == pytest.approx(
                1000.0 + 54.0 * verdict.p_win - spent, abs=1e-9
            )
            assert engine._fills["SettlementHarvester"] == 1
        finally:
            engine.ledger.close()


@pytest.mark.parametrize(
    "yes_asks,expected",
    [
        # The window's actual book: the winner is bid-only, so no ask exists.
        ([], []),
        # The only live ``proposed`` market found with an ask: 0.9880 is above
        # the 0.9869 break-even, so the pre-screen refuses it outright.
        ([(0.9880, 10199.0)], []),
    ],
)
def test_settlement_engine_refuses_markets_without_a_discounted_ask(
    tmp_path, monkeypatch, yes_asks, expected
):
    now = time.time()
    with _uma_tape([(FL26_CONDITION, FL26_QUESTION, 10**18, now, now + 1800.0)]) as glob:
        model = _live_model(glob)
        market = _verdict_market()
        books = _fl26_books(yes_asks=yes_asks, yes_bids=[(0.9990, 14469.67)])
        monkeypatch.setattr(settlement, "model", lambda **kwargs: model)
        monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: [])
        engine = _settlement_engine(tmp_path, market, books, model)
        try:
            engine._tick()
            assert [action.kind for action in engine.actions.pending()] == expected
            assert engine.ledger.positions("SettlementHarvester") == []
            assert engine.ledger.cash("SettlementHarvester") == pytest.approx(1000.0)
        finally:
            engine.ledger.close()
class _SlowGamma:
    """Gamma whose row fetch takes the venue's time, not ours."""

    def __init__(self, rows: list[dict], delay_s: float):
        self.rows = rows
        self.delay_s = delay_s
        self.calls = 0

    def markets(self, condition_ids):
        self.calls += 1
        time.sleep(self.delay_s)
        wanted = {str(c).lower() for c in condition_ids}
        return [
            row for row in self.rows
            if str(row.get("conditionId") or "").lower() in wanted
        ]

    @staticmethod
    def parse_ids(value):
        from polymarketbot.gamma import GammaClient

        return GammaClient.parse_ids(value)


def test_settlement_fetch_is_off_the_tick_and_coverage_still_advances(tmp_path, monkeypatch):
    """A slow verdict-row fetch must not enter the tick stage.

    Measured 2026-11-10 14:17 window: ``settlement=15.11s ... tick=18.76s`` and
    ``settlement=22.06s ... tick=13.56s`` inside a 5 s tick, settling to
    0.31-0.67 s once the one-per-minute cache refresh had run. The blocking
    call was the Gamma row fetch for verdict conditions the walker does not
    list; it now runs on the radar thread. This pins both halves: the stage
    stays far below the interval while the fetch is in flight, and the market
    is priced the moment the measurement lands.
    """
    now = time.time()
    delay = 1.5
    with _uma_tape([(FL26_CONDITION, FL26_QUESTION, 10**18, now, now + 1800.0)]) as glob:
        model = _live_model(glob)
        market = _verdict_market()
        books = _fl26_books(yes_asks=[(0.9830, 54.0)], yes_bids=[(0.9990, 14469.67)])
        row = {
            "conditionId": market.condition_id,
            "question": market.question,
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes-fl26", "no-fl26"]',
            "orderPriceMinTickSize": str(market.tick),
            "orderMinSize": str(market.min_order),
            "acceptingOrders": True,
            "closed": False,
            "negRisk": False,
            "events": [{"id": "evt-fl26"}],
            # A fee-free market, so the row parses to a zero schedule rather
            # than to an unpriced one and the allocator reaches its ladder.
            "feesEnabled": False,
        }
        monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: [])
        engine = _settlement_engine(tmp_path, market, books, model)
        engine.gamma = _SlowGamma([row], delay)
        # The walker does NOT list the verdict market: that is the whole point
        # of the row fetch, and it is what made the stage block.
        engine.walker = _FakeWalker([])
        try:
            started = time.monotonic()
            engine._settlement(books)
            cold = time.monotonic() - started
            assert cold < delay / 3.0, (
                f"the settlement stage waited {cold:.2f}s on a {delay:.1f}s fetch"
            )
            assert engine.ledger.positions("SettlementHarvester") == []
            assert FL26_CONDITION in engine._radar_want

            # One radar cycle absorbs the latency, off the tick.
            engine._fetch_verdict_rows([FL26_CONDITION], time.time())
            rows, markets = engine._radar_snapshot()
            assert FL26_CONDITION in rows and FL26_CONDITION in markets

            started = time.monotonic()
            engine._settlement(books)
            warm = time.monotonic() - started
            assert warm < delay / 3.0, f"the priced stage took {warm:.2f}s"
            # Coverage advanced: the market that had no row now prices and is
            # granted capital by the risk-adjusted allocator.
            pending = [action.kind for action in engine.actions.pending()]
            assert pending == ["settle_buy"], pending
            granted = engine.actions.pending()[0].payload["max_cost"]
            assert granted > 0.0
        finally:
            engine.ledger.close()


# ------------------------------- window 9: concentration, priced
WIN9_CONDITION = "0xbcd8bf70c6"
WIN9_QUESTION = "Will the window-9 condition resolve YES?"
WIN9_LADDER = [
    (0.8100, 20.0), (0.8300, 20.0), (0.8600, 25.0), (0.8700, 84.61),
    (0.8800, 83.33), (0.9600, 19.83), (0.9700, 300.0), (0.9800, 496.32),
]


def _win9_tracker() -> BayesianDisputeTracker:
    """282 proposals, 0 disputed: the measured window-9 posterior (1.0530 %)."""
    tracker = BayesianDisputeTracker()
    tracker.seed_measured(
        MeasuredDisputeCounts(proposals=282, disputes=0, source="window 9 tape")
    )
    return tracker


_MEASURED_Q = 0.00133477   # data/derived/uma/outcome_accuracy.json, 2026-11-10


def _policy(confidence: float):
    from polymarketbot.paper.settlement_risk import SettlementTailPolicy

    return SettlementTailPolicy(
        risk_appetite_usd=150.0, tail_confidence=confidence, source="test"
    )


def _evidence():
    from polymarketbot.strategy.settlement.tail_evidence import evidence_from_counts

    return evidence_from_counts(
        70, 0, disputed=0, source="test", challenge_trials=134664, challenges=158
    )


def test_window9_treasury_is_sized_by_the_tail_budget_not_by_a_variance_charge(
    tmp_path, monkeypatch
):
    """The measured window-9 book is sized by the *tail*, and the band decides.

    Measured 2026-11-10 14:25 (window 9): one tick, one condition, 8 fills,
    1,049.1 shares at 0.81-0.98, cash $1,000.00 -> $0.00.

    The loss in that position is total (LGD = 1) but rare: the measured
    verdict-reversal bound is 0.001335, so at a 95 % band one condition costs
    ``0.001335 * 1,000 / 0.05 = $26.70`` of a $150 appetite and **cash** - not
    the risk - limits the tick.  Tighten the band to 99.5 % and the same appetite
    pays for only ``0.005 * 150 / 0.001335 = $562``: the tail budget truncates
    the ladder and the unspent cash is free for the next market.

    Same formula in both cases.  The band is the stated preference that moves
    the answer, which is why it is an input and not a constant.
    """
    now = time.time()
    with _uma_tape([(WIN9_CONDITION, WIN9_QUESTION, 10**18, now, now + 1800.0)]) as glob:
        model = SettlementFairValueModel(
            dispute_tracker=_win9_tracker(),
            tape=UmaVerdictTape(glob=glob, overlay_path=""),
        )
        model.refresh(force=True)
        market = _verdict_market(
            condition_id=WIN9_CONDITION,
            question=WIN9_QUESTION,
            token_ids=("yes-w9", "no-w9"),
            tick=0.01,
            uma_status="proposed",
            outcome_prices=(),
        )
        books = {
            "yes-w9": {
                "asks": [{"price": str(p), "size": str(s)} for p, s in WIN9_LADDER],
                "bids": [],
            },
            "no-w9": {"asks": [], "bids": []},
        }
        monkeypatch.setattr(settlement, "model", lambda **kwargs: model)
        monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: [])
        engine = _settlement_engine(tmp_path, market, books, model)
        engine._tail_evidence = _evidence()
        clock = [1000.0]
        engine.actions = ActionBook(lambda: 0.01, clock=lambda: clock[0])
        try:
            # ---- a 95 % band: cash is the binding limit ---------------------
            engine._tail_policy = _policy(0.95)
            engine._tick()
            pending = list(engine.actions.pending())
            assert [a.kind for a in pending] == ["settle_buy"]
            granted = pending[0].payload["max_cost"]
            assert 990.0 < granted <= 1000.0, granted
            clock[0] += 1.0
            engine._land(books, [])
            lots = engine.ledger.positions("SettlementHarvester")
            assert len(lots) == 1
            all_in = sum(lot.cost_usd for lot in lots)
            # The book's shortfall is inside the appetite on the measured tail,
            # and nowhere near it: cash - not risk - stopped the tick.
            assert all_in * _MEASURED_Q / 0.05 < 150.0
            # Par is the oracle's, so the position is still marked at the
            # measured value rather than at the ask it paid.
            assert lots[0].par_value == pytest.approx(0.98947, abs=1e-4)
        finally:
            engine.ledger.close()

    # ---- a 99.5 % band: the tail budget is now what truncates -------------
    with _uma_tape([(WIN9_CONDITION, WIN9_QUESTION, 10**18, now, now + 1800.0)]) as glob:
        model = SettlementFairValueModel(
            dispute_tracker=_win9_tracker(),
            tape=UmaVerdictTape(glob=glob, overlay_path=""),
        )
        model.refresh(force=True)
        market = _verdict_market(
            condition_id=WIN9_CONDITION,
            question=WIN9_QUESTION,
            token_ids=("yes-w9", "no-w9"),
            tick=0.01,
            uma_status="proposed",
            outcome_prices=(),
        )
        books = {
            "yes-w9": {
                "asks": [{"price": str(p), "size": str(s)} for p, s in WIN9_LADDER],
                "bids": [],
            },
            "no-w9": {"asks": [], "bids": []},
        }
        monkeypatch.setattr(settlement, "model", lambda **kwargs: model)
        engine = _settlement_engine(tmp_path, market, books, model)
        engine._tail_evidence = _evidence()
        clock = [1000.0]
        engine.actions = ActionBook(lambda: 0.01, clock=lambda: clock[0])
        try:
            engine._tail_policy = _policy(0.995)
            engine._tick()
            pending = list(engine.actions.pending())
            assert [a.kind for a in pending] == ["settle_buy"]
            granted = pending[0].payload["max_cost"]
            # (1 - 0.995) * 150 / 0.001335 = $562 of notional, not $1,000.
            assert 500.0 < granted < 620.0, granted
            clock[0] += 1.0
            engine._land(books, [])
            lots = engine.ledger.positions("SettlementHarvester")
            all_in = sum(lot.cost_usd for lot in lots)
            assert all_in < 620.0
            assert all_in * _MEASURED_Q / 0.005 <= 150.0 * (1.0 + 1e-3)
            assert engine.ledger.cash("SettlementHarvester") > 380.0
        finally:
            engine.ledger.close()


# ------------------------------- near-close coverage
class _NearCloseGamma(_FakeGamma):
    """Gamma whose close-date filter returns the market the walk does not list."""

    def __init__(self, rows: list[dict]):
        self._rows = list(rows)
        self.keyset_calls = 0
        self.last_extra: dict = {}

    def markets(self, condition_ids):
        wanted = {str(c).lower() for c in condition_ids}
        return [row for row in self._rows
                if str(row.get("conditionId") or "").lower() in wanted]

    def markets_keyset(self, *, closed=None, version=None, after_cursor=None,
                       limit=100, extra=None):
        self.keyset_calls += 1
        self.last_extra = dict(extra or {})
        return {"markets": list(self._rows), "next_cursor": None}

    @staticmethod
    def parse_ids(value):
        from polymarketbot.gamma import GammaClient

        return GammaClient.parse_ids(value)


def test_near_close_staging_binds_a_cid_less_proposal_before_any_price(
    tmp_path, monkeypatch
):
    """A market the walk dropped is bound and priced only after staging.

    Measured 2026-11-10 10:08Z: 27 of 34 proposals in the live overlay carried
    no condition id, and the only one still inside its liveness window was one
    of them, so the funding census was empty while the venue held 10,028
    markets closing inside 24 h. ``ProposePrice`` names no condition, and
    ``venue.parse_market`` refuses a market that stopped accepting orders, so
    the walk alone can never supply the join. Three facts are pinned here:
    the unbound proposal is invisible to the census, staging binds it from
    Gamma's own close-date filter, and the same proposal then funds - with
    staging itself deriving no verdict.
    """
    now = time.time()
    with _uma_tape([("", FL26_QUESTION, 10**18, now, now + 1800.0)]) as glob:
        model = _live_model(glob)
        # The proposal has no condition id, so the census cannot name it.
        assert model.tape.live_verdict_condition_ids(now) == []

        market = _verdict_market()
        books = _fl26_books(yes_asks=[(0.9830, 54.0)],
                            yes_bids=[(0.9990, 14469.67)])
        row = {
            "conditionId": market.condition_id,
            "question": market.question,
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes-fl26", "no-fl26"]',
            "orderPriceMinTickSize": str(market.tick),
            "orderMinSize": str(market.min_order),
            "acceptingOrders": True,
            "closed": False,
            "negRisk": False,
            "feesEnabled": False,
            "endDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 1800.0)),
        }
        monkeypatch.setattr(engine_mod.venue, "tape", lambda http, **kwargs: [])
        engine = _settlement_engine(tmp_path, market, books, model)
        gamma = _NearCloseGamma([row])
        engine.gamma = gamma
        # The walker never lists the market: it stopped accepting orders before
        # the oracle spoke, which is the measured disposition of most proposals.
        engine.walker = _FakeWalker([])
        try:
            engine._settlement(books)
            assert engine.ledger.positions("SettlementHarvester") == []
            assert engine.actions.pending() == []

            radar.stage_near_close(engine, now, force=True)

            # Gamma was asked its own close-date filter, not a re-scan of the
            # walk, and both bounds are the measured ones.
            assert gamma.keyset_calls == 1
            assert "end_date_min" in gamma.last_extra
            assert "end_date_max" in gamma.last_extra
            rows, accepting = radar.near_close_snapshot(engine)
            assert market.condition_id in rows
            assert accepting == (market.condition_id,)
            # The join is what the census was missing.
            assert model.tape.live_verdict_condition_ids(now) == [market.condition_id]

            engine._radar_fetched.clear()
            engine._settlement(books)
            pending = [action.kind for action in engine.actions.pending()]
            assert pending == ["settle_buy"], pending
        finally:
            engine.ledger.close()
