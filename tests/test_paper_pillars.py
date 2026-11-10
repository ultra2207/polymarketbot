"""Permanent tests for the reward pillar's decision module.

Each test fixes one economic invariant: reward quotes stay inside the venue
band and above the qualification size, never cross the ask, and never reserve
more cash than the pillar holds. The CTF parity invariants moved with the
archived strategy (``archive/ctf/tests/test_ctf_pillars.py``), and the
settlement funding tests moved to
``archive/tests/test_paper_pillars_settlement.py`` with the archived strategy.
"""

from __future__ import annotations

import time

import pytest

from polymarketbot.fees import FeeSchedule
from polymarketbot.strategy.books import shares_at
import polymarketbot.strategy.reward.decision as reward
from polymarketbot.strategy.venue.reads import Market, Print


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
