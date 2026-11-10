"""Permanent tests for the three pillar decision modules.

Each test fixes one economic invariant: reward quotes stay inside the venue
band and above the qualification size, and settlement funds only strictly
positive marginal edge. The CTF parity invariants moved with the archived
strategy (``archive/ctf/tests/test_ctf_pillars.py``).
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
from polymarketbot.paper.books import shares_at
from polymarketbot.paper.latency import ActionBook
from polymarketbot.paper.pillars import reward, settlement
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




# ------------------------------- settlement radar (tick must not block)
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
    from polymarketbot.trading.domain2.tail_evidence import evidence_from_counts

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
