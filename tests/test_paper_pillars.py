"""Permanent tests for the three pillar decision modules.

Each test fixes one economic invariant: reward quotes stay inside the venue
band and above the qualification size, settlement funds only strictly positive
marginal edge, and CTF parity funds only a pair that costs less than par.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
import time
from pathlib import Path

import duckdb
import pytest
import yaml

from polymarketbot.fees import FeeSchedule, taker_fee
from polymarketbot.paper import engine as engine_mod
from polymarketbot.paper.books import shares_at
from polymarketbot.paper.latency import ActionBook
from polymarketbot.paper.pillars import ctf, reward, settlement
from polymarketbot.paper.venue import Market, Print
from polymarketbot.trading.domain2.robust_hazard import (
    BayesianDisputeTracker,
    MeasuredDisputeCounts,
)
from polymarketbot.trading.domain2.settlement_fair_value import (
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


# ------------------------------------------------------------- reward
def test_reward_candidate_stays_inside_venue_band():
    market = _market(reward_rate=500.0, max_spread=3.0)
    books = {"yes": _book(0.48, 0.52), "no": _book(0.48, 0.52)}
    candidates = reward.reward_candidates([market], books, [])
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["daily_pool"] == 500.0
    assert candidate["competing_score"] > 0.0
    assert abs(candidate["quote_price"] - 0.49) < 1e-9
    # Outside the band: bid 0.42 leaves the quote 2.5c from the 0.455 mid.
    far = {"yes": _book(0.42, 0.49), "no": _book(0.42, 0.49)}
    assert reward.reward_candidates([market], far, []) == []


def test_reward_candidate_requires_a_published_minimum():
    market = _market(reward_rate=500.0, reward_min=0.0)
    books = {"yes": _book(0.48, 0.52), "no": _book(0.48, 0.52)}
    # A null rewardsMinSize is not a zero-size qualification.
    assert reward.reward_candidates([market], books, []) == []


def test_reward_fill_uses_only_crossing_sell_prints():
    prints = [
        Print("yes", "c1", "SELL", 0.48, 50.0, time.time()),
        Print("yes", "c1", "SELL", 0.49, 30.0, time.time()),
        Print("yes", "c1", "BUY", 0.47, 500.0, time.time()),
    ]
    # Only the print at our own bid's price fills us; the 0.49 SELL reached a
    # higher bid that is not ours. Our 100 shares behind a 100-share level get
    # half of that level's flow.
    assert reward.measured_fill_shares(0.48, 100.0, 100.0, prints, 0.01) == pytest.approx(25.0)
    assert reward.measured_fill_shares(0.48, 100.0, 0.0, prints, 0.01) == pytest.approx(50.0)
    assert reward.measured_fill_shares(0.48, 100.0, 100.0, [], 0.01) == 0.0
    # A print that cleared below our bid proves our bid was not resting there:
    # it belongs to the maker whose price it printed at, not to us.
    below = [Print("yes", "c1", "SELL", 0.29, 20.31, time.time())]
    assert reward.measured_fill_shares(0.321, 100.0, 0.0, below, 0.001) == 0.0
    assert reward.measured_fill_shares(0.321, 100.0, 0.0,
                                       [Print("yes", "c1", "SELL", 0.321, 20.31, time.time())],
                                       0.001) == pytest.approx(20.31)


def test_reward_allocation_never_emits_below_min_size():
    market = _market(reward_rate=500.0, reward_min=200.0)
    books = {"yes": _book(0.48, 0.52), "no": _book(0.48, 0.52)}
    candidates = reward.reward_candidates([market], books, [])
    result = reward.allocate(candidates, bankroll_usd=1000.0)
    quotes = reward.quotes_from_allocation(result, candidates, books)
    assert quotes
    for quote in quotes:
        assert quote.shares >= quote.min_size
        assert 0.0 < quote.price < 1.0
        assert quote.strategy == "RewardHarvester"


# --------------------------------------------------------- settlement
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


# ------------------------------------------------------------------ ctf
def test_ctf_parity_funds_a_pair_below_par():
    market = _market()
    tranches = ctf.optimize_taker_parity(market, _book(0.39, 0.40), _book(0.54, 0.55),
                                         cash_usd=100.0, gas_usd=0.0)
    assert len(tranches) == 1
    tranche = tranches[0]
    assert tranche.shares == pytest.approx(100.0)  # ladder depth binds first
    assert tranche.edge_usd == pytest.approx(tranche.shares * 0.05)


def test_ctf_parity_refuses_a_pair_above_par():
    market = _market()
    assert ctf.optimize_taker_parity(market, _book(0.39, 0.60), _book(0.54, 0.55),
                                     1000.0, 0.0) == []


def test_ctf_parity_fee_can_erase_the_edge():
    market = _market(fee_rate=0.2)
    # Pair fee per share = 0.0975 > the 0.05 price edge.
    assert ctf.optimize_taker_parity(market, _book(0.39, 0.40), _book(0.54, 0.55),
                                     1000.0, 0.0) == []


def _markout(*cells) -> "ctf.MakerMarkout":
    """A measured-markout stub: ``(horizon_s, tick, usd_per_share, fills)``."""
    return ctf.MakerMarkout(cells=tuple(cells))


def _gate(*args, **kwargs):
    """``optimize_maker_parity`` with the cost of capital stated explicitly.

    The gate refuses when the opportunity cost is unmeasured, so every fixture
    here declares its capital free (``min_daily_yield=0.0``) to isolate the
    behaviour under test. The hurdle itself is covered by
    ``test_ctf_maker_refuses_an_edge_below_the_shadow_yield``.
    """
    kwargs.setdefault("min_daily_yield", 0.0)
    return ctf.optimize_maker_parity(*args, **kwargs)


def test_ctf_maker_prices_the_pair_as_a_three_outcome_bet():
    # A rebate-paying schedule and a zero-markout measurement keep this test
    # about the EV arithmetic.  0.8 fee rate at a 25 % rebate share pays
    # 0.25 * 0.8 * 0.25 = 0.05/share against a 0.01 one-tick unwind, so both
    # naked legs are self-financing and the quote may be emitted.
    market = _market(fee_rate=0.8, rebate_rate=0.25)
    p = 0.5
    quote = _gate(
        market, _book(0.39, 0.41), _book(0.50, 0.52), 100.0,
        requote_horizon_s=5.0, fill_probability=p,
        markout_model=_markout((5.0, 0.01, 0.0, 1000)),
    )
    assert quote is not None
    assert quote.fill_prob_yes == pytest.approx(p)
    assert quote.markout_usd == pytest.approx(0.0)
    # Both legs rest one tick over the bid; the unwind costs exactly that tick.
    assert quote.unwind_cost_yes == pytest.approx(market.tick)
    assert quote.unwind_cost_no == pytest.approx(market.tick)
    surplus = quote.rebate_yes - quote.unwind_cost_yes - quote.markout_usd
    assert surplus > 0.0 and quote.rebate_no - quote.unwind_cost_no > 0.0
    assert quote.edge_usd == pytest.approx(
        p * p * quote.edge_if_both_fill
        + p * (1 - p) * surplus
        + p * (1 - p) * (quote.rebate_no - quote.unwind_cost_no - quote.markout_usd)
    )
    # A pair above par is refused outright: nothing is left to lock.
    assert _gate(
        market, _book(0.39, 0.41), _book(0.60, 0.62), 100.0,
        requote_horizon_s=5.0, fill_probability=p,
        markout_model=_markout((5.0, 0.01, 0.0, 1000)),
    ) is None


def test_ctf_maker_refuses_a_naked_leg_that_does_not_cover_its_measured_costs():
    """A rebate above the unwind is not enough: the measured markout must fit too.

    This is the 12:53 window's defect stated as a test.  There, the 0.25c rebate
    beat the 0.1c one-tick unwind at tick 0.001, so 68-150 orders rested on
    99.7 % of the pillar's cash and none filled.  At the measured drift of a
    single leg (~0.19c over a 400 s life) the same quote loses on its most
    likely non-zero outcome, and the gate must refuse it however large the lock.
    """
    market = _market(tick=0.001, fee_rate=0.04, rebate_rate=0.25)
    # Live measured Politics schedule (rate 0.04, rebate 0.25) at a price near
    # 0.5, which is where the 12:53 quotes sat: the rebate is 0.25c/share.
    books = (_book(0.489, 0.492), _book(0.499, 0.502))
    rebate = ctf.maker_fee_rebate(0.490, 1.0, market.fee_schedule)
    assert rebate > market.tick  # the old gate accepted on this alone
    refused = _gate(
        market, *books, 100.0, requote_horizon_s=400.0, fill_probability=0.5,
        markout_model=_markout((400.0, 0.001, 0.0016, 3975)),
    )
    assert refused is None
    # The same quote clears when the drift is measured to be zero, so the
    # refusal is the measured markout and not an unrelated floor.
    accepted = _gate(
        market, *books, 100.0, requote_horizon_s=400.0, fill_probability=0.5,
        markout_model=_markout((400.0, 0.001, 0.0, 3975)),
    )
    assert accepted is not None and accepted.locked_spread > 0.0


def test_ctf_maker_refuses_when_the_single_leg_markout_is_unmeasured(monkeypatch):
    """No measured drift for this horizon means no quote, and it says so."""
    warnings: list[str] = []
    monkeypatch.setattr(ctf, "logger", type("R", (), {
        "warning": lambda _s, m: warnings.append(m),
        "info": lambda _s, m: None,
        "error": lambda _s, m: None,
    })())
    monkeypatch.setattr(ctf, "_UNMEASURED_MARKOUT_LOGGED", set())
    monkeypatch.setattr(ctf, "_QUOTE_AUDIT", {
        "logged_at": 0.0, "evaluated": 0, "accepted": 0, "refused": {},
        "best_refusal": None,
    })
    market = _market(fee_rate=0.8, rebate_rate=0.25)
    # The model covers 5 s only; a 400 s quote has no measured drift.
    short = _markout((5.0, 0.01, 0.0, 1000))
    assert _gate(
        market, _book(0.39, 0.41), _book(0.50, 0.52), 100.0,
        requote_horizon_s=400.0, fill_probability=0.5, markout_model=short,
    ) is None
    assert any("no single-leg markout is measured there" in message for message in warnings)
    # An absent measurement is missing data, not a free exit.
    monkeypatch.setattr(ctf, "_default_maker_markout", lambda: None)
    assert _gate(
        market, _book(0.39, 0.41), _book(0.50, 0.52), 100.0,
        requote_horizon_s=400.0, fill_probability=0.5,
    ) is None
    assert ctf._QUOTE_AUDIT["refused"].get("markout_model_missing") == 1


def test_ctf_maker_requires_a_measured_requote_horizon():
    """There is no default quote lifetime: an assumed one prices a guess."""
    market = _market(fee_rate=0.8, rebate_rate=0.25)
    assert _gate(
        market, _book(0.39, 0.41), _book(0.50, 0.52), 100.0,
        fill_probability=0.5, markout_model=_markout((5.0, 0.01, 0.0, 1000)),
    ) is None


def test_ctf_maker_refuses_an_unmeasured_horizon_probability():
    """No measured cell for this horizon/tick/minimum order means no quote."""
    market = _market(fee_rate=0.8, rebate_rate=0.25)
    drift = _markout((5.0, 0.01, 0.0, 1000))
    empty = ctf.QuoteFillHorizon(cells=())
    assert _gate(market, _book(0.39, 0.41), _book(0.50, 0.52), 100.0,
                                     requote_horizon_s=5.0, horizon_model=empty,
                                     markout_model=drift) is None
    # A measured cell for a harder fill size is a conservative lower bound, so
    # it is used; a minimum order larger than every measured bucket is not.
    coarse = ctf.QuoteFillHorizon(cells=((5.0, 0.01, 50.0, 0.3),))
    priced = _gate(market, _book(0.39, 0.41), _book(0.50, 0.52), 100.0,
                                       requote_horizon_s=5.0, horizon_model=coarse,
                                       markout_model=drift)
    assert priced is not None and priced.fill_prob_yes == pytest.approx(0.3)
    big = _market(min_order=5_000.0, fee_rate=0.8, rebate_rate=0.25)
    assert _gate(big, _book(0.39, 0.41), _book(0.50, 0.52), 10_000.0,
                                     requote_horizon_s=5.0, horizon_model=coarse,
                                     markout_model=drift) is None


def test_ctf_maker_logs_the_six_ev_components_of_every_quote(monkeypatch):
    """An accepted quote must be auditable from the log alone.

    The 12:53 window rested 68-150 orders for 30 minutes with no line saying
    what any of them was priced at, so "zero fills" could not be attributed.
    """
    lines: list[str] = []
    monkeypatch.setattr(ctf, "logger", type("R", (), {
        "warning": lambda _s, m: None,
        "info": lambda _s, m: lines.append(m),
        "error": lambda _s, m: None,
    })())
    monkeypatch.setattr(ctf, "_QUOTE_AUDIT", {
        "logged_at": 0.0, "evaluated": 0, "accepted": 0, "refused": {},
        "best_refusal": None,
    })
    monkeypatch.setattr(ctf, "_ACCEPTED_QUOTES_LOGGED", set())
    market = _market(fee_rate=0.8, rebate_rate=0.25)
    quote = _gate(
        market, _book(0.39, 0.41), _book(0.50, 0.52), 100.0,
        requote_horizon_s=5.0, fill_probability=0.5,
        markout_model=_markout((5.0, 0.01, 0.0, 1000)),
    )
    assert quote is not None
    joined = "\n".join(lines)
    for component in ("p_yes=", "p_no=", "locked=", "rebate=", "unwind=",
                      "markout=", "expected="):
        assert component in joined
    # The engine re-prices the same book every tick; the components are
    # published once per distinct quote, and the summary still counts it.
    lines.clear()
    assert _gate(
        market, _book(0.39, 0.41), _book(0.50, 0.52), 100.0,
        requote_horizon_s=5.0, fill_probability=0.5,
        markout_model=_markout((5.0, 0.01, 0.0, 1000)),
    ) is not None
    assert not any("CTF maker quote" in line for line in lines)


def test_ctf_maker_audit_states_the_best_refusal_when_nothing_quotes(monkeypatch):
    """A window that quotes nothing must publish the nearest miss's components."""
    lines: list[str] = []
    monkeypatch.setattr(ctf, "logger", type("R", (), {
        "warning": lambda _s, m: None,
        "info": lambda _s, m: lines.append(m),
        "error": lambda _s, m: None,
    })())
    monkeypatch.setattr(ctf, "_QUOTE_AUDIT", {
        "logged_at": 0.0, "evaluated": 0, "accepted": 0, "refused": {},
        "best_refusal": None,
    })
    market = _market(tick=0.001, fee_rate=0.04, rebate_rate=0.25)
    assert _gate(
        market, _book(0.489, 0.492), _book(0.499, 0.502), 100.0,
        requote_horizon_s=400.0, fill_probability=0.5,
        markout_model=_markout((400.0, 0.001, 0.0016, 3975)),
    ) is None
    summary = ctf.log_quote_audit(force=True)
    if summary is None:
        # The gate already drained and published on this evaluation (the first
        # drain of a process always fires); take the line it wrote.
        summary = lines[-1] if lines else None
    assert summary is not None
    assert "single_leg_below_measured_cost" in summary
    for component in ("p_yes=", "p_no=", "locked=", "rebate=", "unwind=",
                      "markout=", "surplus=", "expected="):
        assert component in summary


def test_ctf_maker_pins_a_scan_gap_wider_than_the_measured_horizon(monkeypatch):
    """A 244-tick sweep gap (244 s) past a 225 s table is pinned, not dropped.

    The engine derives the requote horizon from the scan cadence, so a tick that
    overruns its interval silently widens every quote's life past the measured
    support.  Refusing turned the pillar off (measured: 415-612 s gaps, 0 quotes
    emitted); the policy is to price at the longest measured horizon, which is a
    lower bound on the true probability, log the gap and the bound it costs, and
    tell the caller which horizon it actually paid for.
    """
    warnings: list[str] = []

    class Recorder:
        def warning(self, message):
            warnings.append(str(message))

        def info(self, message):  # pragma: no cover - not exercised
            pass

        def error(self, message):  # pragma: no cover - not exercised
            pass

    monkeypatch.setattr(ctf, "logger", Recorder())
    monkeypatch.setattr(ctf, "_UNMEASURED_HORIZON_LOGGED", set())
    monkeypatch.setattr(ctf, "_HORIZON_CLAMP_LOGGED", set())
    model = ctf.QuoteFillHorizon(
        cells=((5.0, 0.001, 5.0, 0.0002), (225.0, 0.001, 5.0, 0.0064))
    )
    # A 0.1-cent tick pair with the live measured Politics schedule
    # (rate 0.04, rebate 0.25) and a measured-zero drift: the clamp's job is to
    # price the horizon, so the branch economics are supplied as measurements.
    market = _market(tick=0.001, fee_rate=0.04, rebate_rate=0.25)
    drift = _markout((225.0, 0.001, 0.0, 3975))
    held = _gate(market, _book(0.489, 0.492), _book(0.499, 0.502), 100.0,
                                     requote_horizon_s=244.0, horizon_model=model,
                                     markout_model=drift)
    assert held is not None
    assert held.fill_prob_yes == pytest.approx(0.0064)
    assert held.priced_horizon_s == pytest.approx(225.0)
    assert held.horizon_clamped_from_s == pytest.approx(244.0)
    assert len(warnings) == 1
    assert "measured maximum 225s horizon" in warnings[0]
    assert "244s requote gap" in warnings[0]
    assert "lower bound" in warnings[0]
    # Second attempt at the same gap does not spam the log.
    assert _gate(market, _book(0.489, 0.492), _book(0.499, 0.502), 100.0,
                                     requote_horizon_s=244.0, horizon_model=model,
                                     markout_model=drift) is not None
    assert len(warnings) == 1


def test_ctf_maker_still_refuses_a_horizon_below_the_measured_support(monkeypatch):
    """Clamping is one-directional: a too-short life must not be priced up.

    Pricing a 1 s quote with the 5 s cell would overstate its fill probability,
    which is the direction that fabricates an edge.
    """
    warnings: list[str] = []
    monkeypatch.setattr(ctf, "logger", type("R", (), {"warning": lambda _s, m: warnings.append(m),
                                                      "info": lambda *_a: None,
                                                      "error": lambda *_a: None})())
    monkeypatch.setattr(ctf, "_UNMEASURED_HORIZON_LOGGED", set())
    monkeypatch.setattr(ctf, "_HORIZON_CLAMP_LOGGED", set())
    model = ctf.QuoteFillHorizon(cells=((5.0, 0.01, 5.0, 0.0002), (225.0, 0.01, 5.0, 0.6)))
    market = _market()
    assert _gate(market, _book(0.39, 0.41), _book(0.50, 0.52), 100.0,
                                     requote_horizon_s=1.0, horizon_model=model) is None
    assert any("1.0s requote horizon" in message for message in warnings)


def test_shipped_horizon_table_prices_the_exact_price_event():
    """The gate must price the event the paper execution model can realise.

    ``load_quote_fill_horizon`` prices ``p_fill_exact``: a print that lands at the
    quote's own price, which is what ``reward.measured_fill_shares`` books.  The
    artifact also carries the at-or-below count as ``p_fill``.  Two things must
    hold for the shipped table and this asserts both:

    * the probability the model holds is the measured exact count, cell for cell
      -- no scaling, no flattening of the horizon/tick dependence into a ratio;
    * ``p_fill_exact <= p_fill`` everywhere, because a print at the quote is a
      subset of a print at or below it.  An inversion means the two definitions
      have been swapped again, which is the defect that let the gate price ~9
      fills for every one the execution model could book.
    """
    payload = json.loads(ctf.DEFAULT_QUOTE_FILL_HORIZON_PATH.read_text(encoding="utf-8"))
    rows = payload.get("cells") or []
    assert rows
    measured: dict[tuple[float, float, float], float] = {}
    for row in rows:
        key = (float(row["horizon_s"]), float(row["tick"]), float(row["min_flow_shares"]))
        exact = row.get("p_fill_exact")
        # A table measured before the exact cells existed is not a usable
        # measurement: the loader must refuse it rather than silently price the
        # at-or-below count, so its absence here is a defect too.
        assert exact is not None, f"cell {key} has no measured exact-price count"
        assert float(exact) <= float(row["p_fill"]) + 1e-12, f"cell {key} inverted"
        measured[key] = float(exact)

    model = ctf.load_quote_fill_horizon()
    loaded = {(cell[0], cell[1], cell[2]): cell[3] for cell in model.cells}
    assert loaded == measured

    # The one cell both definitions were measured on must differ: the exact
    # event is strictly rarer than the at-or-below event wherever prints sweep
    # past the quote.
    at_quote = next(
        row for row in rows
        if (row["horizon_s"], row["tick"], row["min_flow_shares"]) == (225.0, 0.01, 5.0)
    )
    assert float(at_quote["p_fill_exact"]) < float(at_quote["p_fill"])


def test_ctf_maker_markout_brackets_an_unmeasured_horizon_with_the_worse_neighbour():
    """A gap between cells is charged a measured neighbour, never a line.

    The drift is not monotone in the measured table, so interpolating it would
    invent values; charging the larger bracketing cell can only overstate the
    cost the quote must clear.
    """
    model = ctf.MakerMarkout(cells=((5.0, 0.001, 0.0008, 76), (30.0, 0.001, 0.0025, 401)))
    assert model.markout(5.0, 0.001) == pytest.approx(0.0008)
    assert model.markout(11.0, 0.001) == pytest.approx(0.0025)
    assert model.markout(4.9, 0.001) is None      # below support: missing data
    assert model.markout(400.0, 0.001) is None    # above support: out of contract
    assert model.markout(11.0, 0.01) is None      # tick not measured


def test_shipped_markout_covers_every_horizon_the_fill_table_prices():
    """The two artifacts must sit on one grid, or the gate refuses on a mismatch.

    The gate looks the markout up at the horizon the fill table priced.  A
    markout cell missing there is missing data, so the pillar would go dark for
    a reason that is an artifact mismatch rather than an economic verdict.
    """
    fill_payload = json.loads(
        ctf.DEFAULT_QUOTE_FILL_HORIZON_PATH.read_text(encoding="utf-8")
    )
    fill_cells = {
        (float(row["horizon_s"]), float(row["tick"]))
        for row in fill_payload.get("cells") or []
    }
    markout = ctf.load_maker_markout()
    measured = {(cell[0], cell[1]) for cell in markout.cells}
    missing = sorted(fill_cells - measured)
    assert not missing, f"markout table has no cell for {missing}"
    # Every shipped markout cell carries a real sample and a signed estimate.
    for cell in markout.cells:
        assert cell[3] > 0
    # The measurement must cover the life the 12:53 engine actually rested
    # quotes for (the 428-459 s sweep gaps were clamped to the 400 s cell).
    assert markout.markout(400.0, 0.01) is not None
    assert markout.markout(400.0, 0.001) is not None


def test_horizon_table_without_the_exact_count_refuses(tmp_path):
    """A pre-exact table is missing data: refuse, never revert to crossing.

    Pricing the at-or-below count is a 9-13x overstatement of the fill
    probability the paper model can produce, so a cell without an exact count is
    dropped and a table with no exact cells raises.
    """
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"cells": [
        {"horizon_s": 5.0, "tick": 0.01, "min_flow_shares": 5.0, "p_fill": 0.5},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError):
        ctf.load_quote_fill_horizon(legacy)

    # A mixed table keeps only the cells that carry the exact count.
    mixed = tmp_path / "mixed.json"
    mixed.write_text(json.dumps({"cells": [
        {"horizon_s": 5.0, "tick": 0.01, "min_flow_shares": 5.0, "p_fill": 0.5},
        {"horizon_s": 5.0, "tick": 0.01, "min_flow_shares": 20.0,
         "p_fill": 0.4, "p_fill_exact": 0.0002},
    ]}), encoding="utf-8")
    model = ctf.load_quote_fill_horizon(mixed)
    assert [cell[2] for cell in model.cells] == [20.0]
    assert model.cells[0][3] == pytest.approx(0.0002)



def test_ctf_merge_pairs_is_the_complete_set_count():
    class Lot:
        def __init__(self, token, shares):
            self.condition_id = "c1"
            self.token_id = token
            self.shares = shares

    lots = [Lot("yes", 10.0), Lot("yes", 5.0), Lot("no", 12.0), Lot("other", 99.0)]
    assert ctf.merge_pairs(lots, "c1") == pytest.approx(12.0)


def test_ctf_onchain_leg_pays_rpc_rtt_plus_block_interval():
    """An admitted merge flies for the measured RPC round trip plus one block."""
    timing = ctf.require_on_chain_timing(0.42, 2.0, action="merge")
    assert timing.flight_s == pytest.approx(2.42)


@pytest.mark.parametrize(
    "rpc_rtt_s, block_interval_s, head_error, missing",
    [
        # eth_blockNumber rate limited: cooldown 921 s, head unreadable.
        (0.42, 2.0, "eth_blockNumber rate limited, cooldown 921s", "live Polygon head"),
        # The latency probe never recorded a polygon_rpc endpoint, because the
        # paper engine's RttProbe overwrote data/derived/local_latency.json.
        (None, 2.0, None, "measured Polygon RPC round trip"),
        # No consecutive-head sequence exists to measure the block interval.
        (0.42, None, None, "measured Polygon block interval"),
        (0.42, 0.0, None, "measured Polygon block interval"),
        (0.0, 2.0, None, "measured Polygon RPC round trip"),
    ],
)
def test_ctf_onchain_leg_is_refused_when_its_clock_is_unmeasured(
    rpc_rtt_s, block_interval_s, head_error, missing
):
    """No fallback interval: a leg with an unmeasured clock refuses and says why."""
    with pytest.raises(ctf.OnChainRefused) as excinfo:
        ctf.require_on_chain_timing(
            rpc_rtt_s, block_interval_s, action="merge", head_unavailable_reason=head_error
        )
    assert missing in str(excinfo.value)


def test_ctf_onchain_leg_refuses_during_an_rpc_outage_without_touching_the_tape():
    """A starved RPC refuses the merge even when historical measurements exist.

    The block interval measured from the header tape is a fact about Polygon,
    not about this transaction: with the head unreadable the transaction cannot
    be broadcast, so the merge must not be scheduled against a stale clock.
    """
    calls = {"rpc": 0, "block": 0}

    def rpc_rtt():
        calls["rpc"] += 1
        return 0.42

    def block_interval():
        calls["block"] += 1
        return 2.0

    with pytest.raises(ctf.OnChainRefused):
        ctf.resolve_on_chain_timing(
            "merge",
            head_error="eth_blockNumber rate limited, cooldown 921s",
            rpc_rtt_loader=rpc_rtt,
            block_interval_loader=block_interval,
        )
    assert "cooldown 921s" in str(
        ctf.OnChainRefused("eth_blockNumber rate limited, cooldown 921s")
    )
    assert calls == {"rpc": 1, "block": 1}  # loaders are cheap reads, not the blocker

    admitted = ctf.resolve_on_chain_timing(
        "merge",
        probe_head=False,
        rpc_rtt_loader=rpc_rtt,
        block_interval_loader=block_interval,
    )
    assert admitted.flight_s == pytest.approx(2.42)


def test_ctf_onchain_leg_never_probes_the_head_when_a_reason_is_supplied(monkeypatch):
    """A caller that already saw the outage failure does not pay a second probe."""
    probed = {"n": 0}

    def boom():
        probed["n"] += 1
        return "should not be called"

    monkeypatch.setattr(ctf, "_live_head_error", boom)
    with pytest.raises(ctf.OnChainRefused):
        ctf.resolve_on_chain_timing(
            "redeem",
            head_error="eth_blockNumber unavailable",
            rpc_rtt_loader=lambda: 0.42,
            block_interval_loader=lambda: 2.0,
        )
    assert probed["n"] == 0


def test_reward_candidate_carries_its_measured_resting_distance():
    market = _market(reward_rate=500.0, max_spread=3.0, tick=0.01)
    books = {"yes": _book(0.48, 0.52), "no": _book(0.48, 0.52)}
    candidate = reward.reward_candidates([market], books, [])[0]
    # quote = bid + tick = 0.49 against a 0.50 mid -> exactly 1c of the band.
    assert candidate["quote_price"] == pytest.approx(0.49)
    assert candidate["spread_offset_cents"] == pytest.approx(1.0)


def test_reward_quote_never_crosses_the_ask():
    """A bid at the ask is marketable: it cannot rest for the pool or fill.

    ``best_bid + tick`` equals the ask on a one-tick-wide book, and those books
    are the highest-scoring ones, so the allocator funds them first. Measured
    on 40 live reward candidates, 16 (40 %) priced at the ask; over 4,229 tape
    prints those quotes saw 0.22 crossing prints per 30-minute window against
    0.81 at the highest price that rests (``ask - tick``), and four consecutive
    soaks booked zero fills. The quote must therefore be the highest price that
    still rests: one tick inside the spread, or the touch when the spread is
    one tick.
    """
    market = _market(reward_rate=500.0, max_spread=3.0, tick=0.01)
    one_tick = {"yes": _book(0.33, 0.34), "no": _book(0.33, 0.34)}
    candidate = reward.reward_candidates([market], one_tick, [])[0]
    assert candidate["quote_price"] == pytest.approx(0.33)
    assert candidate["quote_price"] < 0.34
    assert candidate["spread_offset_cents"] == pytest.approx(0.5)

    # Two ticks wide: one tick of price improvement still rests below the ask.
    two_ticks = {"yes": _book(0.32, 0.34), "no": _book(0.32, 0.34)}
    assert reward.reward_candidates([market], two_ticks, [])[0]["quote_price"] == pytest.approx(0.33)

    # A locked book has no resting price between the touch and the ask, on
    # either leg of the binary market.
    locked = {"yes": {"bids": [{"price": "0.34", "size": "100"}],
                      "asks": [{"price": "0.34", "size": "100"}]},
              "no": {"bids": [{"price": "0.66", "size": "100"}],
                     "asks": [{"price": "0.66", "size": "100"}]}}
    assert reward.reward_candidates([market], locked, []) == []


def test_reward_touch_quote_fills_from_a_print_at_the_touch():
    """Joining the touch is a price a SELL print can actually reach.

    With the quote capped at ``ask - tick`` it sits on the best bid, whose depth
    the book publishes, so the queue share is the honest ``ours / (level + ours)``
    instead of the 1.0 an empty level above the touch reports. Under the old
    ``bid + tick`` rule the quote was the ask (0.34), a print at 0.33 was a full
    tick away, and the fill was 0.0.
    """
    market = _market(reward_rate=500.0, max_spread=3.0, tick=0.01, reward_min=50.0)
    books = {"yes": _book(0.33, 0.34, size=300.0), "no": _book(0.33, 0.34, size=300.0)}
    candidate = reward.reward_candidates([market], books, [])[0]
    quote_price = candidate["quote_price"]
    level = shares_at(books[candidate["quote_token"]], bid=True, price=quote_price)
    assert level == pytest.approx(300.0)
    prints = [
        Print(candidate["quote_token"], "c1", "SELL", quote_price, 120.0, time.time()),
        Print(candidate["quote_token"], "c1", "SELL", quote_price - 0.01, 60.0, time.time()),
    ]
    decision = reward.fill_audit(quote_price, 100.0, level, prints, 0.01)
    assert decision.crossing == 1
    assert decision.below == 1
    assert decision.queue_share == pytest.approx(100.0 / 400.0)
    assert decision.shares == pytest.approx(120.0 * 100.0 / 400.0)
    assert reward.measured_fill_shares(quote_price, 100.0, level, prints, 0.01) == pytest.approx(
        30.0
    )


def test_reward_fill_tape_separates_absent_flow_from_a_broken_gate():
    """The window's fill inputs are countable, so zero fills are explainable.

    A window with no crossing print is missing flow; a window with crossing
    prints but no booked fill is the ``min_order`` gate. The engine logs the
    second outcome and nothing for the first, which is why four consecutive
    zero-fill windows were indistinguishable from a broken rule.
    """
    tape = reward.reset_fill_tape(now=time.time())
    assert tape.evaluations == 0
    prints = [
        Print("yes", "c1", "SELL", 0.48, 40.0, 10.0),
        Print("yes", "c1", "SELL", 0.47, 20.0, 10.0),
        Print("yes", "c1", "BUY", 0.48, 500.0, 10.0),
    ]
    assert reward.measured_fill_shares(0.48, 100.0, 100.0, prints, 0.01) == pytest.approx(20.0)
    reward.measured_fill_shares(0.48, 100.0, 100.0, [], 0.01)
    snapshot = reward.fill_tape()
    assert snapshot.evaluations == 2
    assert snapshot.with_flow == 1
    assert snapshot.prints == 3
    assert snapshot.crossing == 1
    assert snapshot.below == 1
    assert snapshot.flow_shares == pytest.approx(40.0)
    assert snapshot.booked_shares == pytest.approx(20.0)
    # Nothing is flushed before the window elapses; a flush resets the tally.
    snapshot = reward.fill_tape()
    snapshot.flush(snapshot.since + 1.0)
    assert snapshot.evaluations == 2
    snapshot.flush(snapshot.since + snapshot.window_s + 1.0)
    assert snapshot.evaluations == 0
    assert snapshot.prints == 0


def test_reward_quote_shares_are_whole_units():
    market = _market(reward_rate=500.0, reward_min=20.0, tick=0.01)
    books = {"yes": _book(0.28, 0.32), "no": _book(0.28, 0.32)}
    candidates = reward.reward_candidates([market], books, [])
    result = reward.allocate(candidates, bankroll_usd=1000.0)
    for quote in reward.quotes_from_allocation(result, candidates, books):
        assert quote.shares == float(int(quote.shares))
        assert quote.shares >= quote.min_size
        # The price tick must not quantise the share count.
        assert quote.shares * quote.price <= 1000.0 + 1e-6


# ------------------------------------- reward: reserve accounting
def _reward_pair_market(cid: str, yes_bid: float, pool: float) -> Market:
    """One rewarded binary market whose cheaper leg is the YES token."""
    schedule = FeeSchedule(rate=0.0, exponent=1.0, taker_only=True,
                           rebate_rate=0.0, source="test")
    return Market(
        condition_id=cid,
        question=f"Q {cid}",
        outcomes=("yes", "no"),
        token_ids=(f"{cid}y", f"{cid}n"),
        tick=0.01,
        min_order=5.0,
        daily_reward=pool,
        reward_max_spread_cents=4.0,
        reward_min_size=100.0,
        fee_schedule=schedule,
        neg_risk=False,
        closed=False,
        uma_status="",
        outcome_prices=(yes_bid + 0.02, 1.0 - yes_bid - 0.02),
    )


def _engine_would_rest(wanted, resting):
    """The engine's own keep rule: same price -> keep the resting size.

    ``Engine._reward`` keeps a live quote whose price has not moved instead of
    cancel-and-replacing it, so the size that stays on the book is the one from
    the tick that first placed it, not this tick's optimised size.
    """
    out = {}
    for token_id, quote in wanted.items():
        current = resting.get(token_id)
        if current is not None and abs(current[0] - quote.price) < 1e-9:
            out[token_id] = current
        else:
            out[token_id] = (quote.price, quote.shares)
    return out


def _run_ticks(tick_candidates, cash, planner, accrual_per_tick=0.0):
    """Apply ``planner`` over successive candidate sets; return notional per tick.

    ``accrual_per_tick`` moves cash between ticks the way the engine's reward
    accrual does, so the invariant is checked against a balance that drifts
    while quotes rest rather than against a frozen constant.
    """
    resting: dict[str, tuple[float, float]] = {}
    history = []
    balance = cash
    for candidates in tick_candidates:
        wanted = {q.token_id: q for q in planner(candidates, balance, resting)}
        resting = _engine_would_rest(wanted, resting)
        history.append((balance, sum(price * shares for price, shares in resting.values())))
        balance += accrual_per_tick
    return history


def test_reward_resting_notional_never_exceeds_cash():
    """Sum of resting reward notional stays within cash across a multi-tick run.

    The tick-1 book is two markets; later ticks add higher-paying markets, so
    the optimizer's per-market ideal shrinks while the quotes already resting
    keep their original size. The 2026-11-10 window measured the result:
    $1,445.65 of resting reward notional against $1,000.51 of cash.
    """
    books = {
        "ay": _book(0.48, 0.52), "an": _book(0.48, 0.52),
        "by": _book(0.48, 0.52), "bn": _book(0.48, 0.52),
        "cy": _book(0.38, 0.42), "cn": _book(0.58, 0.62),
        "dy": _book(0.38, 0.42), "dn": _book(0.58, 0.62),
        "ey": _book(0.28, 0.32), "en": _book(0.68, 0.72),
        "fy": _book(0.28, 0.32), "fn": _book(0.68, 0.72),
    }
    a = _reward_pair_market("a", 0.48, 400.0)
    b = _reward_pair_market("b", 0.48, 400.0)
    c = _reward_pair_market("c", 0.38, 900.0)
    d = _reward_pair_market("d", 0.38, 900.0)
    e = _reward_pair_market("e", 0.28, 1400.0)
    f = _reward_pair_market("f", 0.28, 1400.0)
    cash = 1000.0
    ticks = [
        reward.reward_candidates([a, b], books, []),
        reward.reward_candidates([a, b, c, d], books, []),
        reward.reward_candidates([a, b, c, d, e, f], books, []),
    ]

    def old_planner(candidates, bankroll, _resting):
        return reward.quotes_from_allocation(
            reward.allocate(candidates, bankroll_usd=bankroll), candidates, books
        )

    old = _run_ticks(ticks, cash, old_planner, accrual_per_tick=0.4)
    new = _run_ticks(ticks, cash, reward.plan_quotes, accrual_per_tick=0.4)

    # The compound plan spends the whole balance again while earlier quotes
    # still rest, so it over-commits; the reserve ledger cannot.
    assert max(reserved for _balance, reserved in old) > cash * 1.1, old
    for balance, reserved in new:
        assert reserved <= balance + 1e-6, new
    assert max(reserved for _balance, reserved in new) > 0.0, new


def test_reward_reserve_trims_resting_bids_that_no_longer_fit():
    """A resting book that no longer fits in cash is retired, not compounded."""
    books = {"ay": _book(0.48, 0.52), "an": _book(0.48, 0.52),
             "by": _book(0.38, 0.42), "bn": _book(0.58, 0.62)}
    a = _reward_pair_market("a", 0.48, 400.0)
    b = _reward_pair_market("b", 0.38, 400.0)
    candidates = reward.reward_candidates([a, b], books, [])
    cash = 1000.0
    ideal = reward.quotes_from_allocation(
        reward.allocate(candidates, bankroll_usd=cash), candidates, books
    )
    assert len(ideal) == 2
    # Both bids are already resting 20 % above what this tick's cash covers, so
    # the cheaper half of the book has to go rather than over-commit.
    bloated = {q.token_id: (q.price, q.shares * 1.2) for q in ideal}
    assert sum(price * shares for price, shares in bloated.values()) > cash
    kept = reward.commit_quotes(ideal, cash, bloated)
    assert len(kept) == 1
    assert sum(q.shares * q.price for q in kept) <= cash + 1e-6
    # A moved price is a cancel, so its cash must not be charged again: the
    # fresh plan is funded in full even though the stale sizes would not fit.
    moved = {q.token_id: (q.price + 0.01, q.shares * 4.0) for q in ideal}
    fresh = reward.commit_quotes(ideal, cash, moved)
    assert {q.token_id: q.shares for q in fresh} == {q.token_id: q.shares for q in ideal}


def test_reward_markout_has_no_invented_constant():
    """An unmeasured markout adds no cost; a measured one is linear."""
    assert reward.markout_cost_usd(100.0, None) == 0.0
    assert reward.markout_cost_usd(0.0, -0.209) == 0.0
    assert reward.markout_cost_usd(100.0, -0.209) == pytest.approx(-0.209)


def test_horizon_fill_probability_interpolates_only_between_measured_cells() -> None:
    """A gap that falls between measured horizons must be priced, not refused:
    the operating scan gap (sweep ticks x tick) is rarely a measured value.
    Interpolation is linear in time and validated at 5.31 % worst held-out error,
    so the midpoint of two cells is their mean, and anything outside the
    measured support still refuses rather than extrapolating."""
    model = ctf.QuoteFillHorizon(cells=(
        (10.0, 0.01, 5.0, 0.001),
        (30.0, 0.01, 5.0, 0.003),
        (60.0, 0.01, 5.0, 0.006),
        (10.0, 0.01, 100.0, 0.0005),
        (30.0, 0.01, 100.0, 0.002),
    ), placements=1_000)

    assert model.p_fill(10.0, 0.01, 5.0) == pytest.approx(0.001)  # exact cell wins
    assert model.p_fill(20.0, 0.01, 5.0) == pytest.approx(0.002)  # midpoint
    assert model.p_fill(30.0, 0.01, 5.0) == pytest.approx(0.003)
    assert model.p_fill(15.0, 0.01, 5.0) == pytest.approx(0.0015)
    assert model.p_fill(45.0, 0.01, 5.0) == pytest.approx(0.0045)

    # Outside the measured support: no extrapolation, no invented probability.
    assert model.p_fill(5.0, 0.01, 5.0) is None
    assert model.p_fill(61.0, 0.01, 5.0) is None
    # The 100-share bucket stops at 30 s, so a 45 s quote for 100 shares is
    # outside its support even though the 5-share bucket covers it.
    assert model.p_fill(45.0, 0.01, 100.0) is None
    # An unmeasured tick is not interpolated across.
    assert model.p_fill(20.0, 0.001, 5.0) is None


def test_ctf_maker_refuses_an_edge_below_the_shadow_yield():
    """A positive expected value is not a reason to lock capital.

    The 14:06 window accepted two quotes at ``expected=$6.629e-09/set`` with
    ``p_fill=6.163e-06``: the lock carries weight ``p^2`` and a naked leg only
    ``p``, so at that probability the rebate surplus collapses to a rounding
    error while ~$0.82 of capital per set rests for the horizon. Annualised
    over the 9 s horizon that is 7.7e-05 /USD/day, three orders of magnitude
    below what the same dollar earns on the reward book.
    """
    market = _market(fee_rate=0.8, rebate_rate=0.25)
    books = (_book(0.39, 0.41), _book(0.50, 0.52))
    dust = 6.163e-06

    # Positive EV, uneconomic yield: refused on the measured hurdle.
    assert _gate(market, *books, 100.0, requote_horizon_s=5.0,
                 fill_probability=dust,
                 markout_model=_markout((5.0, 0.01, 0.0, 1000)),
                 min_daily_yield=0.03) is None

    # The identical quote clears when capital is genuinely free, so the refusal
    # is the hurdle and not an unrelated floor.
    assert _gate(market, *books, 100.0, requote_horizon_s=5.0,
                 fill_probability=dust,
                 markout_model=_markout((5.0, 0.01, 0.0, 1000))) is not None

    # An unmeasured opportunity cost is missing data, never a zero hurdle: the
    # gate must refuse without the argument at all.
    assert ctf.optimize_maker_parity(
        market, *books, 100.0, requote_horizon_s=5.0, fill_probability=0.5,
        markout_model=_markout((5.0, 0.01, 0.0, 1000)),
    ) is None
