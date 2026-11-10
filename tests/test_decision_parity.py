"""Paper and live must make the *same* reward decision.

The two traders own different execution (the paper engine fills against a
simulated tape, the live engine places signed CLOB orders), but the reward
decision behind a quote - which leg, at which price, at which size, and whether
the venue pays anything at all for it - must be one implementation. This module
pins that: every case runs the same fixed fixture through

* the **paper path**: :func:`polymarketbot.strategy.reward.decision.plan_quotes`
  over candidates built by ``reward_candidates`` (the pillar's public surface),
  and
* the **live path**: ``live/quoting.QuotingMixin`` - the neutral maker quoting
  that places the live bids, plus ``compute_quote_scores`` /
  ``_measure_reward_score`` / ``get_current_daily_yield``, which is how the live
  engine prices the reward on those bids,

and asserts identical output.

The fixture is one market's published reward parameters, one book (both
outcomes), one measured fill/markout cell (the paper path refuses to price a
quote on an unmeasured fill side), and one capital.

Why these numbers coincide, stated so a reader can check the test rather than
trust it: the fixture's book is two ticks wide on each leg and the live engine's
spread offset is one tick, so the live bid ``mid - offset`` and the paper bid
``bid + tick`` are the same price. The live engine splits its cash over the
prime pair (a YES bid and a NO bid, ``0.98`` combined at this fixture) while the
paper pillar rests one bid, and the fixture's capital funds the venue-minimum
20-share lot on each side (``19.60`` live, a ``0.43``/``20`` resting bid against
the paper reserve ledger). Sizes therefore agree at the venue minimum, which is
the size both paths would rest for this capital.

Drift fails the test: a changed price rule, a changed size rule, a changed band
or single-sided penalty, a changed score normalisation, or a changed
accept/refuse gate on either side breaks an ``==`` or a ``pytest.approx`` here.

The refuse cases pin the three semantics the live path adopted from the paper
pillar (it used to hold a second opinion on each):

* a **sub-floor** daily payout is zero, not a small cash amount (the venue's
  published $1.00/day term),
* only the **resting** quote is scored - never the configured spread offset -
  so a quote sitting at the band edge earns nothing,
* a missing book or a missing measured competing denominator is **unmeasured**
  and is refused, never read as zero competition (which would pay the pool).
"""

from __future__ import annotations

import dataclasses

import pytest

import polymarketbot.strategy.reward.quote_candidates as quote_candidates
from polymarketbot.live.base import BaseMarketMakingEngine, PositionView
from polymarketbot.strategy.books import mid, qualifying_score
from polymarketbot.strategy.reward.decision import (
    measured_accrual,
    plan_quotes,
    reward_candidates,
)
from polymarketbot.strategy.reward.fill_measurement import MarketFillYield
from polymarketbot.strategy.reward.scoring import order_spread_cents
from polymarketbot.strategy.venue.reads import Market

# --- the fixture -----------------------------------------------------------
# One market's published reward parameters.
TICK = 0.01
MIN_ORDER = 5.0
MIN_SIZE = 20.0
MAX_SPREAD_CENTS = 5.0
DAILY_POOL = 200.0
# One capital: enough for the venue-minimum lot on each side of the pair.
CAPITAL = 19.6
# The live engine's configured distance from the mid. One venue tick, i.e. the
# same distance ``bid + tick`` sits from the mid on a two-tick-wide book.
SPREAD_OFFSET_CENTS = 1.0


def _market(max_spread_cents: float = MAX_SPREAD_CENTS) -> Market:
    return Market(
        condition_id="0xPARITY",
        question="Parity market",
        outcomes=("Yes", "No"),
        token_ids=("TOK_YES", "TOK_NO"),
        tick=TICK,
        min_order=MIN_ORDER,
        daily_reward=DAILY_POOL,
        reward_max_spread_cents=max_spread_cents,
        reward_min_size=MIN_SIZE,
        fee_schedule=None,
        neg_risk=False,
        closed=False,
        uma_status="open",
    )


def _books(yes_bid: float, yes_ask: float) -> dict:
    """One book per outcome token; the NO book is the YES book's complement."""
    return {
        "TOK_YES": {
            "bids": [{"price": str(yes_bid), "size": "100"}],
            "asks": [{"price": str(yes_ask), "size": "100"}],
        },
        "TOK_NO": {
            "bids": [{"price": str(round(1.0 - yes_ask, 2)), "size": "100"}],
            "asks": [{"price": str(round(1.0 - yes_bid, 2)), "size": "100"}],
        },
    }


def _measured_cell() -> MarketFillYield:
    """One measured fill/markout cell at this fixture's tick and horizon."""
    leg_mid = 0.44
    quote_price = 0.43
    markout = 0.002
    horizon_s = 30.0
    p_fill = 0.001
    edge = (leg_mid - quote_price - markout) / quote_price
    return MarketFillYield(
        p_fill=p_fill,
        horizon_s=horizon_s,
        markout_usd_per_share=markout,
        fill_edge_per_usd=edge,
        fill_yield_usd_per_usd_per_day=p_fill * (86_400.0 / horizon_s) * edge,
    )


def _measured_competing_score(books: dict) -> float:
    """The fixture's measured in-band competing score, from both token books.

    This is the denominator both paths divide by: the paper pillar measures it
    inside ``reward_candidates``/``measured_accrual``, the live engine is given
    it as ``competing_score_override`` (it does not measure competitor depth).
    """
    return qualifying_score(
        books["TOK_YES"], max_spread_cents=MAX_SPREAD_CENTS, mid_price=mid(books["TOK_YES"])
    ) + qualifying_score(
        books["TOK_NO"], max_spread_cents=MAX_SPREAD_CENTS, mid_price=mid(books["TOK_NO"])
    )


class _HarnessEngine(BaseMarketMakingEngine):
    """Minimal live engine: real quoting/scoring, in-memory execution hooks."""

    def __init__(
        self,
        tmp_path,
        *,
        cash: float,
        competing_score: float | None,
        max_spread: float,
        daily_reward: float = DAILY_POOL,
        spread_offset_cents: float = SPREAD_OFFSET_CENTS,
    ) -> None:
        self.cash = float(cash)
        self.position = PositionView(condition_id="0xPARITY")
        self.placed: list[tuple[str, float, float, float]] = []
        self.canceled: list[str] = []
        super().__init__(
            condition_id="0xPARITY",
            asset_id_yes="TOK_YES",
            asset_id_no="TOK_NO",
            quote_size=MIN_SIZE,
            spread_offset_cents=spread_offset_cents,
            daily_reward_rate=daily_reward,
            rewards_max_spread_cents=max_spread,
            rewards_min_size=MIN_SIZE,
            institutional_mode=True,
            competing_score_override=competing_score,
            db_path=tmp_path / "parity.db",
            ledger_path=tmp_path / "parity_ledger.json",
        )
        self.quote_tick_size = TICK

    # --- execution hooks (the wave keeps execution in live/) ---------------
    def _set_quote(self, token_side, quote_side, price, size, ts) -> None:
        qid = f"{quote_side}_{token_side}".upper()
        self.placed.append((qid, price, size, ts))
        self.active_quotes[qid] = {
            "quote_id": qid,
            "token_side": token_side.upper(),
            "side": quote_side.upper(),
            "price": price,
            "size": size,
            "timestamp": ts,
            "status": "ACTIVE",
        }

    def _cancel_quote(self, qid: str) -> None:
        if qid in self.active_quotes:
            self.active_quotes.pop(qid)
            self.canceled.append(qid)

    def cancel_all_quotes(self) -> None:
        self.canceled.extend(self.active_quotes.keys())
        self.active_quotes.clear()

    def _execute_tier2_taker_deskew(self, decision, ts) -> None:
        return None

    def trigger_ctf_merge(self, amount_sets=None):
        return None

    def trigger_ctf_split(self, amount_usdc):
        return None

    def get_cash(self) -> float:
        return self.cash

    def get_position(self, condition_id=None) -> PositionView:
        return self.position

    def get_equity(self, prices=None) -> float:
        return self.cash


@pytest.fixture
def measured_cell(monkeypatch):
    """Install the fixture's one measured fill/markout cell on the paper path."""
    cell = _measured_cell()
    monkeypatch.setattr(quote_candidates, "expected_fill_yield", lambda **_kwargs: cell)
    return cell


def _set_book(engine: _HarnessEngine, books: dict) -> None:
    for token, side in (("TOK_YES", "YES"), ("TOK_NO", "NO")):
        engine._best_bids[side] = float(books[token]["bids"][0]["price"])
        engine._best_asks[side] = float(books[token]["asks"][0]["price"])


def _quote_key(quote_token: str) -> str:
    """The lead-quote key the live engine uses for a token-side bid."""
    return "BUY_YES" if quote_token == "TOK_YES" else "BUY_NO"


def _engine(
    tmp_path,
    books: dict,
    *,
    cash: float,
    max_spread: float,
    daily_reward: float = DAILY_POOL,
) -> _HarnessEngine:
    engine = _HarnessEngine(
        tmp_path,
        cash=cash,
        competing_score=_measured_competing_score(books),
        max_spread=max_spread,
        daily_reward=daily_reward,
    )
    _set_book(engine, books)
    return engine


def _paper_quote(books: dict, market: Market, capital: float):
    """The paper path: candidates, then the tick decision and its reward price.

    The resting entry is what the paper engine really supplies: the bid it
    already keeps on the book at this plan's own price. ``commit_quotes`` keeps
    a resting bid at its resting size rather than re-sizing it every tick, so
    the fixture states the lot both paths rest for this capital.
    """
    candidates = reward_candidates([market], books, [], fill_horizon_s=30.0)
    assert len(candidates) == 1
    candidate = candidates[0]
    resting = {candidate["quote_token"]: (candidate["quote_price"], MIN_SIZE)}
    quotes = plan_quotes(candidates, capital, resting)
    assert len(quotes) == 1
    return candidate, quotes[0]


def _assert_same_quote(quote, live_price: float, live_size: float) -> None:
    """Same price to the tick, same size."""
    tick = TICK
    assert round(live_price / tick) == round(quote.price / tick)
    assert abs(live_price - quote.price) <= 1e-9
    assert abs(live_size - quote.shares) <= 1e-9


# ---------------------------------------------------------------------------
# Case 1: book inside the band, YES is the cheaper leg - both accept
# ---------------------------------------------------------------------------

def test_in_band_yes_leg_decision_matches(tmp_path, measured_cell):
    books = _books(0.42, 0.46)
    market = _market()
    candidate, quote = _paper_quote(books, market, CAPITAL)

    assert candidate["quote_token"] == "TOK_YES"
    assert quote.token_id == "TOK_YES"
    assert quote.price == pytest.approx(0.43)
    assert quote.shares == pytest.approx(MIN_SIZE)

    engine = _engine(tmp_path, books, cash=CAPITAL, max_spread=MAX_SPREAD_CENTS)
    try:
        engine._execute_neutral_maker_quoting(0.44, 0.42, 0.46, 1000.0)
        live = engine.active_quotes[_quote_key(quote.token_id)]
        _assert_same_quote(quote, live["price"], live["size"])
        # Same notional committed on the same leg.
        assert live["price"] * live["size"] == pytest.approx(quote.price * quote.shares)

        # Reward decision parity: both paths price the same one-sided bid.
        accrual = measured_accrual(
            pool_usd_per_day=market.daily_reward,
            price=quote.price,
            shares=quote.shares,
            min_size=market.reward_min_size,
            max_spread_cents=market.reward_max_spread_cents,
            leg_book=books[quote.token_id],
            other_book=books["TOK_NO"],
            competing=None,
        )
        assert accrual is not None
        assert accrual.competing_score == pytest.approx(
            _measured_competing_score(books), rel=1e-12
        )
        assert accrual.qualifies is True
        assert accrual.usd_per_day >= 1.0  # clears the venue's daily floor

        # The live engine scores the same single bid from its own book state.
        engine._cancel_quote("BUY_NO")
        q_min, _q_one, _q_two = engine.compute_quote_scores()
        assert q_min == pytest.approx(accrual.our_score, rel=1e-12)
        assert engine._measure_reward_score() == pytest.approx(accrual.our_score, rel=1e-12)
        assert engine.get_current_daily_yield() == pytest.approx(
            accrual.usd_per_day, rel=1e-9
        )
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Case 2: book inside the band, NO is the cheaper leg - both accept
# ---------------------------------------------------------------------------

def test_in_band_no_leg_decision_matches(tmp_path, measured_cell):
    books = _books(0.60, 0.64)
    market = _market()
    candidate, quote = _paper_quote(books, market, CAPITAL)

    assert candidate["quote_token"] == "TOK_NO"
    assert quote.token_id == "TOK_NO"
    assert quote.price == pytest.approx(0.37)
    assert quote.shares == pytest.approx(MIN_SIZE)

    engine = _engine(tmp_path, books, cash=CAPITAL, max_spread=MAX_SPREAD_CENTS)
    try:
        engine._execute_neutral_maker_quoting(0.62, 0.60, 0.64, 1000.0)
        live = engine.active_quotes[_quote_key(quote.token_id)]
        _assert_same_quote(quote, live["price"], live["size"])
        assert live["price"] * live["size"] == pytest.approx(quote.price * quote.shares)

        accrual = measured_accrual(
            pool_usd_per_day=market.daily_reward,
            price=quote.price,
            shares=quote.shares,
            min_size=market.reward_min_size,
            max_spread_cents=market.reward_max_spread_cents,
            leg_book=books["TOK_NO"],
            other_book=books["TOK_YES"],
            competing=None,
        )
        assert accrual is not None
        assert accrual.qualifies is True
        assert accrual.usd_per_day >= 1.0

        # A NO bid is the Q_two half of the pair: the live scorer must place it
        # on the same side the paper accrual prices it (single-sided / c).
        engine._cancel_quote("BUY_YES")
        q_min, q_one, q_two = engine.compute_quote_scores()
        assert q_two > 0.0 and q_one == 0.0
        assert q_min == pytest.approx(accrual.our_score, rel=1e-12)
        assert engine._measure_reward_score() == pytest.approx(accrual.our_score, rel=1e-12)
        assert engine.get_current_daily_yield() == pytest.approx(
            accrual.usd_per_day, rel=1e-9
        )
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Case 3: book outside the band - both refuse
# ---------------------------------------------------------------------------

def test_out_of_band_book_is_refused_by_both_paths(tmp_path, measured_cell):
    max_spread = 1.0  # one tick: a bid one tick inside the touch is at the band edge
    books = _books(0.42, 0.46)
    market = _market(max_spread_cents=max_spread)

    # Paper: the candidate screen drops it, so the pillar quotes nothing.
    assert reward_candidates([market], books, [], fill_horizon_s=30.0) == []
    assert plan_quotes([], CAPITAL, {}) == []

    engine = _engine(tmp_path, books, cash=CAPITAL, max_spread=max_spread)
    try:
        engine._execute_neutral_maker_quoting(0.44, 0.42, 0.46, 1000.0)
        # Live still rests its spread-capture quotes at the same price...
        yes_quote = engine.active_quotes[_quote_key("TOK_YES")]
        assert yes_quote["price"] == pytest.approx(0.43)
        # ...but the shared gate is what both paths refuse on: the distance from
        # the mid is not inside the band.
        distance = order_spread_cents(yes_quote["price"], 0.44, "BUY")
        assert distance >= max_spread
        # ...and the live reward decision values the quote at zero, exactly as
        # the paper pillar refuses to claim a payout for it.
        engine._cancel_quote("BUY_NO")
        assert engine.compute_quote_scores() == (0.0, 0.0, 0.0)
        assert engine._measure_reward_score() == 0.0
        assert engine.get_current_daily_yield() == 0.0
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Case 4: capital below the venue's minimum lot - both refuse
# ---------------------------------------------------------------------------

def test_capital_below_minimum_lot_is_refused_by_both_paths(tmp_path, measured_cell):
    books = _books(0.42, 0.46)
    market = _market()

    # One capital that cannot fund the published minimum share lot.
    capital = 5.0
    assert capital < MIN_SIZE * 0.43  # below the lot at the paper quote
    assert capital < MIN_SIZE * (0.43 + 0.55)  # below the pair at the live quotes

    assert plan_quotes(reward_candidates([market], books, [], fill_horizon_s=30.0), capital, {}) == []

    engine = _engine(tmp_path, books, cash=capital, max_spread=MAX_SPREAD_CENTS)
    try:
        engine._execute_neutral_maker_quoting(0.44, 0.42, 0.46, 1000.0)
        assert engine.placed == []
        assert engine.active_quotes == {}
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Case 5: sub-floor pool - the venue pays nothing, both paths report nothing
# ---------------------------------------------------------------------------

def test_sub_floor_pool_pays_nothing_in_both_paths(tmp_path, measured_cell):
    books = _books(0.42, 0.46)
    pool = 10.0  # a day's pool this quote's share cannot lift over $1.00
    market = dataclasses.replace(_market(), daily_reward=pool)

    # Paper: the payout for the lot both paths would rest is below the floor, so
    # there is no payout, and the allocator cannot clear its minimum floor
    # capital, so it quotes nothing.
    assert plan_quotes(
        reward_candidates([market], books, [], fill_horizon_s=30.0), CAPITAL, {}
    ) == []
    accrual = measured_accrual(
        pool_usd_per_day=pool,
        price=0.43,
        shares=MIN_SIZE,
        min_size=MIN_SIZE,
        max_spread_cents=market.reward_max_spread_cents,
        leg_book=books["TOK_YES"],
        other_book=books["TOK_NO"],
        competing=None,
    )
    assert accrual is not None
    assert accrual.qualifies is False
    assert accrual.usd_per_day == 0.0

    engine = _engine(
        tmp_path, books, cash=CAPITAL, max_spread=MAX_SPREAD_CENTS, daily_reward=pool
    )
    try:
        engine._execute_neutral_maker_quoting(0.44, 0.42, 0.46, 1000.0)
        # The quote still scores (live places it for spread capture)...
        assert engine.compute_quote_scores()[0] > 0.0
        # ...but the sub-floor share is worth zero to the venue, so it is not a
        # yield: the old live answer here was that raw sub-floor share (0.8163).
        score = engine._measure_reward_score()
        assert score is not None and score > 0.0
        gross = pool * score / (score + accrual.competing_score)
        assert gross < 1.0  # the number the live path used to report, sub-floor
        assert engine.get_current_daily_yield() == 0.0
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Case 6: no book - unmeasured in both paths, never a measured zero
# ---------------------------------------------------------------------------

def test_no_book_is_unmeasured_in_both_paths(tmp_path, measured_cell):
    market = _market()

    # Paper: no mid, so no candidate; an unmeasured leg refuses the accrual.
    assert reward_candidates([market], {}, [], fill_horizon_s=30.0) == []
    assert plan_quotes([], CAPITAL, {}) == []
    assert (
        measured_accrual(
            pool_usd_per_day=market.daily_reward,
            price=0.43,
            shares=MIN_SIZE,
            min_size=MIN_SIZE,
            max_spread_cents=market.reward_max_spread_cents,
            leg_book=None,
            other_book=None,
            competing=None,
        )
        is None
    )

    # Live: the score is None (unmeasured), not 0.0, and the yield refuses.
    engine = _HarnessEngine(
        tmp_path,
        cash=CAPITAL,
        competing_score=_measured_competing_score(_books(0.42, 0.46)),
        max_spread=MAX_SPREAD_CENTS,
    )
    try:
        assert engine._measure_reward_score() is None
        assert engine.get_current_daily_yield() == 0.0
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Case 7: the yield never scores an offset that is not resting
# ---------------------------------------------------------------------------

def test_yield_never_scores_a_non_resting_offset(tmp_path):
    books = _books(0.42, 0.46)
    engine = _HarnessEngine(
        tmp_path,
        cash=CAPITAL,
        competing_score=_measured_competing_score(books),
        max_spread=1.0,  # one tick: 0.43 sits exactly at the band edge
        spread_offset_cents=0.5,  # the input the removed fallback scored
    )
    _set_book(engine, books)
    try:
        engine._set_quote("YES", "BUY", 0.43, MIN_SIZE, 1000.0)
        assert engine.compute_quote_scores()[0] == 0.0
        # This is what the removed fallback invented from the configured offset
        # (it was not on the book) - the live answer must not be derived from it.
        fallback_score = ((1.0 - 0.5) / 1.0) ** 2 * MIN_SIZE
        assert fallback_score == pytest.approx(5.0)
        assert engine.get_current_daily_yield() == 0.0
    finally:
        engine.close()


# ---------------------------------------------------------------------------
# Case 8: absent / zero competing denominator - refused, never the whole pool
# ---------------------------------------------------------------------------

def test_absent_denominator_is_refused_in_both_paths(tmp_path, measured_cell):
    books = _books(0.42, 0.46)

    # Live: in-band quotes but no measured competitor. Reading the denominator
    # as zero would pay the whole pool on any size; it is refused instead.
    engine = _HarnessEngine(
        tmp_path,
        cash=CAPITAL,
        competing_score=None,
        max_spread=MAX_SPREAD_CENTS,
    )
    _set_book(engine, books)
    try:
        engine._execute_neutral_maker_quoting(0.44, 0.42, 0.46, 1000.0)
        assert engine.compute_quote_scores()[0] > 0.0
        assert engine.get_current_daily_yield() == 0.0
    finally:
        engine.close()

    # Paper: a book with no in-band level measures zero competition, which is
    # refused (None), so nothing is quoted here either.
    market = _market(max_spread_cents=1.0)
    assert reward_candidates([market], books, [], fill_horizon_s=30.0) == []
    assert (
        measured_accrual(
            pool_usd_per_day=market.daily_reward,
            price=0.43,
            shares=MIN_SIZE,
            min_size=MIN_SIZE,
            max_spread_cents=market.reward_max_spread_cents,
            leg_book=books["TOK_YES"],
            other_book=books["TOK_NO"],
            competing=None,
        )
        is None
    )
