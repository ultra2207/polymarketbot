"""Unit and integration tests for Polymarket Liquidity Rewards module."""

from __future__ import annotations

import pytest

from polymarketbot.strategy.reward.scoring import (
    OrderQuote,
    calculate_epoch_payout,
    compute_book_side_scores,
    compute_minimum_score,
    order_spread_cents,
    quadratic_order_score,
)


def test_order_spread_cents() -> None:
    # BUY order below mid
    assert order_spread_cents(0.49, 0.50, "BUY") == pytest.approx(1.0)
    # SELL order above mid
    assert order_spread_cents(0.51, 0.50, "SELL") == pytest.approx(1.0)
    # Invalid side
    with pytest.raises(ValueError):
        order_spread_cents(0.50, 0.50, "HOLD")


def test_quadratic_order_score_basic() -> None:
    # v = 3 cents, s = 1 cent, size = 100 shares
    # ((3 - 1)/3)^2 * 100 = (4/9) * 100 = 44.4444...
    score = quadratic_order_score(max_spread_cents=3.0, spread_cents=1.0, size=100.0)
    assert score == pytest.approx(44.4444, rel=1e-4)

    # Disqualification when spread > max_spread
    assert quadratic_order_score(max_spread_cents=3.0, spread_cents=3.1, size=100.0) == 0.0

    # Disqualification when size < min_size
    assert (
        quadratic_order_score(
            max_spread_cents=3.0, spread_cents=1.0, size=50.0, min_size=100.0
        )
        == 0.0
    )

    # No resting-time gate: the published rule scores whatever rests at the
    # sample instant (docs, equation 6), so there is no minimum order age and
    # no `duration_seconds` argument to zero a quote with.


def test_official_worked_example_from_docs() -> None:
    """Verify exact calculation from Polymarket official docs worked example."""
    mid = 0.50
    v = 3.0

    # Side one quotes:
    # - 100Q bid on m @ 0.49 (spread = 1c)
    # - 200Q bid on m @ 0.48 (spread = 2c)
    # - 100Q ask on m' @ 0.51 (m' mid is 0.50; ask 0.51 -> spread = 1c)
    quotes_one = [
        OrderQuote(side="BUY", outcome="YES", price=0.49, size=100.0),
        OrderQuote(side="BUY", outcome="YES", price=0.48, size=200.0),
        OrderQuote(side="SELL", outcome="NO", price=0.51, size=100.0),
    ]

    # Side two quotes (YES asks and NO bids):
    # - 100Q ask on m @ 0.515 (spread = 1.5c)
    # - 100Q bid on m' @ 0.48 (spread = 2c)
    # - 200Q ask on m @ 0.505 (spread = 0.5c)
    quotes_two = [
        OrderQuote(side="SELL", outcome="YES", price=0.515, size=100.0),
        OrderQuote(side="BUY", outcome="NO", price=0.48, size=100.0),
        OrderQuote(side="SELL", outcome="YES", price=0.505, size=200.0),
    ]

    all_quotes = quotes_one + quotes_two
    q1, q2 = compute_book_side_scores(all_quotes, mid_price_yes=mid, max_spread_cents=v)

    # Expected Q_one = ((2/3)^2 * 100) + ((1/3)^2 * 200) + ((2/3)^2 * 100) = 44.444 + 22.222 + 44.444 = 111.111
    assert q1 == pytest.approx(111.111, rel=1e-3)

    # Expected Q_two = ((1.5/3)^2 * 100) + ((1/3)^2 * 100) + ((2.5/3)^2 * 200) = 25 + 11.111 + 138.889 = 175.0
    assert q2 == pytest.approx(175.0, rel=1e-3)

    # Minimum score in [0.10, 0.90]:
    # max(min(111.111, 175.0), max(111.111/3, 175.0/3)) = max(111.111, 58.333) = 111.111
    q_min = compute_minimum_score(q1, q2, midpoint=mid, c_penalty=3.0)
    assert q_min == pytest.approx(111.111, rel=1e-3)


def test_minimum_score_single_sided_penalties() -> None:
    # When mid is inside [0.10, 0.90], single-sided order gets score / 3.0
    q1 = 300.0
    q2 = 0.0
    mid_center = 0.50
    q_min_center = compute_minimum_score(q1, q2, midpoint=mid_center, c_penalty=3.0)
    assert q_min_center == pytest.approx(100.0)

    # When mid is outside [0.10, 0.90] (e.g. 0.08), liquidity must be double-sided
    mid_tail = 0.08
    q_min_tail = compute_minimum_score(q1, q2, midpoint=mid_tail, c_penalty=3.0)
    assert q_min_tail == 0.0


def test_epoch_payout_and_floor() -> None:
    rate = 100.0

    # Case 1: Gross payout is $5.00 >= $1.00 -> Net payout is $5.00
    share, gross, net = calculate_epoch_payout(
        maker_epoch_score=50.0,
        total_epoch_score=1000.0,
        rate_per_day=rate,
        payout_floor_usd=1.00,
    )
    assert share == pytest.approx(0.05)
    assert gross == pytest.approx(5.00)
    assert net == pytest.approx(5.00)

    # Case 2: Gross payout is $0.50 < $1.00 floor -> Net payout is $0.00
    share, gross, net = calculate_epoch_payout(
        maker_epoch_score=5.0,
        total_epoch_score=1000.0,
        rate_per_day=rate,
        payout_floor_usd=1.00,
    )
    assert share == pytest.approx(0.005)
    assert gross == pytest.approx(0.50)
    assert net == 0.0


# ---------------------------------------------------------------------------
# Allocator: measured inputs, USD activation lots, KKT water-filling
# ---------------------------------------------------------------------------

from polymarketbot.strategy.reward.allocator import (
    PROTOCOL_MIN_DAILY_PAYOUT_USD,
    LiquidityRewardsAllocator,
)


def _candidate(**over) -> dict:
    base = {
        "condition_id": "0xc",
        "question": "q",
        "daily_pool": 500.0,
        "competing_score": 1000.0,
        "max_spread": 3.0,
        "min_size": 20.0,
        "real_mid": 0.40,
        "quote_price": 0.39,
        "spread_offset_cents": 1.0,
    }
    base.update(over)
    return base


def test_alpha_is_measured_and_zero_outside_the_band() -> None:
    allocator = LiquidityRewardsAllocator()
    # 1c inside a 3c band: ((3 - 1)/3)^2
    assert allocator.calculate_alpha(3.0, 1.0) == pytest.approx(4.0 / 9.0)
    # A quote at or past the band scores nothing: no clamped positive default.
    assert allocator.calculate_alpha(3.0, 3.0) == 0.0
    assert allocator.calculate_alpha(3.0, 4.0) == 0.0
    # A candidate that cannot state its resting distance is not scoreable.
    bare = _candidate(spread_offset_cents=None, quote_price=None)
    result = allocator.optimize_allocation_milp([bare], bankroll_usd=100.0)
    assert result.allocations == []
    assert result.solver_name == "NO_VIABLE_MARKETS"


def test_activation_lot_is_the_matched_pair_not_one_leg() -> None:
    """min_size is a share count; a pair at mid 0.10 costs mid + (1 - mid)."""
    allocator = LiquidityRewardsAllocator()
    # 20 shares of YES at 0.10 plus 20 of NO at 0.90 is a $20 pair: it must
    # fit a $100 bankroll. Funding only one leg (20 x 0.10 = $2) is the bug
    # that left the second leg unfunded.
    cheap = _candidate(min_size=20.0, real_mid=0.10, quote_price=0.09)
    result = allocator.optimize_allocation_milp([cheap], bankroll_usd=100.0)
    assert result.active_markets_count == 1
    assert result.allocations[0].allocated_capital >= 20.0
    assert result.allocations[0].quote_size >= 20.0


def test_protocol_floor_is_the_only_payout_hurdle() -> None:
    allocator = LiquidityRewardsAllocator()
    alpha = allocator.calculate_alpha(3.0, 1.0)
    # A pool that cannot pay the documented $1.00/day floor is not fundable.
    assert allocator.calculate_min_floor_capital(1.0, 1000.0, alpha) == float("inf")
    # A pool that can: c = D / (alpha * (P - 1)).
    assert allocator.calculate_min_floor_capital(3.0, 1000.0, alpha) == pytest.approx(
        1000.0 / (alpha * (3.0 - PROTOCOL_MIN_DAILY_PAYOUT_USD))
    )


def test_water_filling_equalises_marginal_yield_across_markets() -> None:
    """The fallback must spread capital, not dump the bankroll into one market."""
    allocator = LiquidityRewardsAllocator()
    alpha = allocator.calculate_alpha(3.0, 1.0)
    parsed = [
        {
            "cand": _candidate(condition_id=f"0x{i}"),
            "P": 500.0,
            "D": 1000.0,
            "alpha": alpha,
            "c_min": 20.0 * 0.40,
            "c_floor": 0.0,
            "min_size": 20.0,
            "mid": 0.40,
        }
        for i in range(3)
    ]
    result = allocator._solve_water_filling(parsed, 1000.0, 990.0, 10.0)
    assert result.active_markets_count == 3
    caps = [a.allocated_capital for a in result.allocations]
    assert sum(caps) == pytest.approx(990.0, rel=1e-6)
    for cap in caps:
        assert cap == pytest.approx(330.0, rel=1e-6)
    margins = [
        allocator.marginal_payout(cap, 500.0, 1000.0, alpha) for cap in caps
    ]
    for margin in margins[1:]:
        assert margin == pytest.approx(margins[0], rel=1e-6)


def test_reserve_ceiling_is_total_exposure_not_a_bankroll_fraction() -> None:
    """The reserve cap is the notional that could fill at once, never 35 %."""
    from polymarketbot.strategy.reward.allocator import calculate_endogenous_reserve

    # Three $100 exposures at 0.5 pay-out each: h_i = 50, so no more than $150
    # of inventory can fill simultaneously. A 35 % ceiling on this bankroll
    # would have been $35,000 - a number with no measurement behind it.
    allocations = [
        {"allocated_capital": 100.0, "mid_price": 0.5} for _ in range(3)
    ]
    reserve = calculate_endogenous_reserve(
        allocations=allocations,
        bankroll_usd=100_000.0,
        base_gas_buffer_usd=0.0,
        jump_shock_factor=1.0,
    )
    assert reserve <= 150.0 + 1e-6
    # The gas floor still holds when it is the larger term: the reserve never
    # drops below the buffer that guarantees the merge calls can be paid for.
    floor = calculate_endogenous_reserve(
        allocations=[{"allocated_capital": 1.0, "mid_price": 0.5}],
        bankroll_usd=100_000.0,
        base_gas_buffer_usd=25.0,
        jump_shock_factor=0.0,
    )
    assert floor >= 25.0
    assert floor <= 150.0
    # And the cap scales with the exposure it is derived from.
    doubled = [
        {"allocated_capital": 200.0, "mid_price": 0.5} for _ in range(3)
    ]
    assert calculate_endogenous_reserve(
        allocations=doubled,
        bankroll_usd=100_000.0,
        base_gas_buffer_usd=0.0,
        jump_shock_factor=1.0,
    ) > reserve


def _measured_regime(condition_id: str = "0xtest"):
    """A tape regime shaped like a measured one (window, prints, statistics)."""
    from polymarketbot.strategy.reward.regime import NS_PER_HOUR, MarketRegime

    window_end_ns = 1_794_306_326_926_867_001
    return MarketRegime(
        condition_id=condition_id,
        window_start_ns=window_end_ns - int(24.0 * NS_PER_HOUR),
        window_end_ns=window_end_ns,
        trades=500,
        trades_per_min=0.35,
        mean_trade_size_shares=40.0,
        size_cv=1.2,
        realised_vol_bps_per_min=100.0,
        jump_freq=0.02,
        span_minutes=60.0,
        required_sample=100,
        jump_band_cents=3.0,
    )


# Measured inputs every yield estimate now needs: the caller's capital, the
# market's published tick, the quote lifetime, and the market's tape regime.
# (tick 0.01, flow 20 shares, 300 s) is a measured cell of
# data/derived/ctf_quote_fill_horizon.json, and (300 s, tick 0.01) is a measured
# cell of data/derived/ctf_maker_markout.json.
MEASURED_INPUTS = {
    "allocated_capital_usd": 1000.0,
    "tick_size": 0.01,
    "quote_horizon_s": 300.0,
    "regime": _measured_regime(),
}


def test_expected_yield_uses_measured_distance_and_mid() -> None:
    """No 0.5c / 0.50 defaults: an unmeasured quote earns no estimate."""
    from polymarketbot.strategy.reward.scanner import calculate_expected_yield

    base = dict(
        daily_rate=500.0,
        max_spread_cents=3.0,
        min_size=20.0,
        competing_score=1000.0,
        gas_fee_per_merge=0.01,
        merge_threshold=30.0,
        **MEASURED_INPUTS,
    )
    unmeasured = calculate_expected_yield(**base)
    assert unmeasured["maker_score"] == 0.0
    assert unmeasured["net_expected_yield"] == 0.0

    measured = calculate_expected_yield(**base, spread_cents=1.0, mid_price=0.40)
    assert measured["maker_score"] > 0.0
    # The score is a function of the distance it was given, not of 0.5c.
    nearer = calculate_expected_yield(**base, spread_cents=0.5, mid_price=0.40)
    assert nearer["maker_score"] > measured["maker_score"]


def test_one_sided_quote_is_scored_at_a_third() -> None:
    """The venue's one-sided penalty applies to the scanner, not just paper."""
    from polymarketbot.strategy.reward.scanner import calculate_expected_yield

    common = dict(
        daily_rate=500.0,
        max_spread_cents=3.0,
        min_size=20.0,
        competing_score=1000.0,
        gas_fee_per_merge=0.01,
        merge_threshold=30.0,
        spread_cents=1.0,
        mid_price=0.40,
        **MEASURED_INPUTS,
    )
    two_sided = calculate_expected_yield(**common, quote_sides=2)
    one_sided = calculate_expected_yield(**common, quote_sides=1)
    # Same capital: the one-sided plan puts it all in one leg (twice the shares)
    # but is paid a third of that leg's raw score, so it earns 2/3 of what the
    # same money earns split across both sides (625 alpha vs 937.5 alpha).
    assert one_sided["maker_score"] == pytest.approx(
        two_sided["maker_score"] * 2.0 / 3.0
    )
    # Outside the [0.10, 0.90] band a one-sided quote is paid nothing at all.
    outside = calculate_expected_yield(**{**common, "mid_price": 0.95}, quote_sides=1)
    assert outside["maker_score"] == 0.0


def test_unmeasured_gas_refuses_the_yield_estimate(monkeypatch) -> None:
    """A rate-limited RPC must not price merges at a fabricated $0.015."""
    import polymarketbot.strategy.onchain.ctf as ctf_mod
    from polymarketbot.strategy.reward.scanner import calculate_expected_yield

    def boom():
        raise RuntimeError("eth_blockNumber rate limited")

    monkeypatch.setattr(ctf_mod, "estimate_merge_cost_usd", boom)
    metrics = calculate_expected_yield(
        daily_rate=500.0,
        max_spread_cents=3.0,
        min_size=20.0,
        spread_cents=1.0,
        mid_price=0.40,
        competing_score=1000.0,
        **MEASURED_INPUTS,
    )
    assert metrics["gas_measured"] is False
    assert metrics["net_expected_yield"] == float("-inf")
    assert metrics["passed_payout_floor"] is False


# ---------------------------------------------------------------------------
# Expected total yield: accrual while unfilled + measured fill yield
#
# Window 9 measured the defect this pins. The optimiser ranked markets by the
# marginal yield of the accrual alone, which is largest where the competing
# score ``D`` is smallest - it funded ``D = 4.0 shares`` books claiming 52.9 %
# of a pool. It also logged an instantaneous implied rate ($104 -> $472.91/day
# between ticks) against an equity delta ($5.95 / 30 min ~ $286/day), a 1.65x
# "disagreement" that was only ever a point estimate versus a time average.
# ---------------------------------------------------------------------------


def test_optimizer_prefers_the_market_that_actually_keeps_its_yield() -> None:
    """An empty book's thin ``D`` loses to a book whose fills do not cost money.

    Both candidates claim the same pool. The empty book (``D = 4``) has the
    larger marginal accrual; the liquid one (``D = 400``) is where a fill keeps
    the spread instead of paying the measured markout. Once the fill side is
    priced, the ranking is the *total* yield and the empty book no longer wins
    on its denominator alone.
    """
    allocator = LiquidityRewardsAllocator()
    allocator.reserve_pct = 0.25  # stated test fixture, not a production default
    empty = _candidate(
        condition_id="0xempty",
        competing_score=4.0,
        # A book with no queue: every fill is adverse (drift > distance to mid).
        fill_yield_usd_per_usd_per_day=-6.0,
        stay_fraction=0.5,
        p_fill=0.5,
    )
    liquid = _candidate(
        condition_id="0xliquid",
        competing_score=400.0,
        # A deep book: a fill earns the distance to the mid, net of markout.
        fill_yield_usd_per_usd_per_day=0.05,
        stay_fraction=0.9999,
        p_fill=1e-4,
    )

    # The fixed reserve keeps enough capital to fund both books, so this test
    # measures the fill-side pricing rather than the reserve size.
    accrual_only = allocator.optimize_allocation_milp(
        [dict(empty, fill_yield_usd_per_usd_per_day=None, stay_fraction=None, p_fill=None),
         dict(liquid, fill_yield_usd_per_usd_per_day=None, stay_fraction=None, p_fill=None)],
        bankroll_usd=200.0,
        use_endogenous_reserve=False,
    )
    assert accrual_only.allocations, "accrual-only mode must still fund something"
    # The defect: on the accrual alone the empty book ranks first.
    assert accrual_only.allocations[0].condition_id == "0xempty"

    priced = allocator.optimize_allocation_milp(
        [empty, liquid],
        bankroll_usd=200.0,
        use_endogenous_reserve=False,
    )
    assert priced.allocations, "a priced fill side must still fund something"
    assert priced.allocations[0].condition_id == "0xliquid"
    assert priced.allocations[0].expected_fill_yield_usd_per_day > 0.0
    assert priced.allocations[0].expected_total_yield_usd_per_day == pytest.approx(
        priced.allocations[0].daily_reward_payout * 0.9999
        + priced.allocations[0].expected_fill_yield_usd_per_day,
        abs=0.05,
    )


def test_water_filling_funds_nothing_whose_accrual_cannot_beat_its_own_fill_yield() -> None:
    """The KKT inverse carries the fill term, so ``lambda <= fill`` is zero size."""
    allocator = LiquidityRewardsAllocator()
    alpha = allocator.calculate_alpha(3.0, 1.0)
    parsed = [
        {
            "cand": _candidate(condition_id="0xneg"),
            "P": 100.0,
            "D": 100.0,
            "alpha": alpha,
            "c_min": 8.0,
            "c_floor": 0.0,
            "min_size": 20.0,
            "mid": 0.40,
            # Filling costs more than the whole pool pays: no size is funded.
            "fill_side": (1.0, -1000.0),
        },
    ]
    result = allocator._solve_water_filling(parsed, 100.0, 90.0, 10.0)
    assert result.total_allocated == 0.0


def test_fill_probability_is_read_from_the_measured_table_or_refused() -> None:
    """A quote horizon outside the measured grid has no probability: refuse."""
    from polymarketbot.strategy.reward.decision import expected_fill_yield

    priced = expected_fill_yield(
        tick=0.01, min_flow_shares=50.0, horizon_s=5.0,
        quote_price=0.30, leg_mid=0.305,
    )
    assert priced is not None
    # Measured cells, not invented ones: the probability is small, the adverse
    # drift is the measured 0.10c/share, and the daily rate follows from them.
    assert 0.0 < priced.p_fill < 1e-2
    assert priced.markout_usd_per_share > 0.0
    assert priced.fill_yield_usd_per_usd_per_day == pytest.approx(
        priced.p_fill * (86_400.0 / priced.horizon_s) * priced.fill_edge_per_usd
    )
    assert priced.stay_fraction == pytest.approx(1.0 - priced.p_fill)
    # No measured cell at this tick -> missing data, never a default.
    assert expected_fill_yield(
        tick=0.05, min_flow_shares=50.0, horizon_s=5.0,
        quote_price=0.30, leg_mid=0.305,
    ) is None


# ---------------------------------------------------------------------------
# Issue #15: the $1.00 venue floor is a property of a market's UTC day, never
# of an instantaneous rate, and an order earns nothing until it has rested the
# documented 3.5 s. Issue #6: a fully-filled quote is gone and must be
# re-placed by the next tick's plan.
# ---------------------------------------------------------------------------

from datetime import UTC, date, datetime

import polymarketbot.strategy.reward.decision as reward
from polymarketbot.paper.actions import Resting
from polymarketbot.paper.pillars import reward_runner
from polymarketbot.strategy.ledger import Ledger
from polymarketbot.strategy.reward.accrual import (
    REWARD_ELIGIBILITY_SECONDS,
    RewardDayAccrual,
)
from polymarketbot.strategy.reward.quote_plan import RewardQuote
from polymarketbot.strategy.venue.reads import Market


def _utc(year: int, month: int, day: int, hour: int = 0, second: float = 0.0) -> float:
    return datetime(year, month, day, hour, tzinfo=UTC).timestamp() + second


def _accrual_market() -> Market:
    return Market(
        condition_id="0xreward",
        question="Q",
        outcomes=("Yes", "No"),
        token_ids=("yes", "no"),
        tick=0.01,
        min_order=5.0,
        daily_reward=500.0,
        reward_max_spread_cents=3.0,
        reward_min_size=200.0,
        fee_schedule=None,
        neg_risk=False,
        closed=False,
        uma_status="",
        outcome_prices=(0.5, 0.5),
    )


def _accrual_book(bid: float, ask: float, size: float = 100.0) -> dict:
    return {
        "bids": [{"price": str(bid), "size": str(size)}],
        "asks": [{"price": str(ask), "size": str(size)}],
    }


class _Actions:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, str, dict]] = []

    def submit(self, kind: str, strategy: str, payload: dict) -> None:
        self.submitted.append((kind, strategy, payload))


class _Engine:
    """The slice of the paper engine ``run_reward`` actually reads."""

    def __init__(self, ledger, market, quotes, now: float) -> None:
        self.ledger = ledger
        self.markets = [market]
        self.by_condition = {market.condition_id: market}
        self.reward_quotes = quotes
        self.actions = _Actions()
        self.interval = 10.0
        self._reward_accrual_ts = now - 10.0
        self._reward_log_ts = now
        self._reward_window = None
        self._fills = {"RewardHarvester": 0}

    def _rtt(self) -> float:
        return 0.0

    def _reserved(self, resting) -> float:
        return 0.0


def _stub_plan(monkeypatch, now: float, wanted: list[RewardQuote]) -> None:
    monkeypatch.setattr(reward_runner.time, "time", lambda: now)
    monkeypatch.setattr(reward, "reward_candidates", lambda *a, **k: [])
    monkeypatch.setattr(reward, "plan_quotes", lambda *a, **k: list(wanted))


def _stub_rate(monkeypatch, usd_per_day: float) -> None:
    share = reward.AccrualShare(
        usd_per_day=usd_per_day,
        share=0.1,
        our_score=1.0,
        competing_score=9.0,
        pool_usd_per_day=usd_per_day,
        qualifies=True,
        competing_samples=3,
    )
    # The runner prices a matched pair now; the legacy one-sided entry point
    # remains for callers that still ask for it.
    monkeypatch.setattr(reward, "measured_accrual", lambda **kwargs: share)
    monkeypatch.setattr(reward, "measured_pair_accrual", lambda **kwargs: share)


def test_day_accrual_below_floor_settles_to_zero() -> None:
    """A day that never reached $1.00 is not paid, whatever the rate was."""
    account = RewardDayAccrual("0xcond")
    day = date(2026, 11, 10)
    start = _utc(2026, 11, 10)
    account.record(start, 0.0)
    account.record(start + 3_600.0, 12.0)  # $12/day for one hour = $0.50

    assert account.pending_utc_day(day) == pytest.approx(0.5)
    assert account.settle_utc_day(day) == 0.0
    # Settling clears the day and can never pay it twice.
    assert account.pending_utc_day(day) == 0.0
    assert account.settle_utc_day(day) == 0.0


def test_day_accrual_above_floor_settles_the_day_total() -> None:
    """The payout is the day's integrated total, not a floored rate."""
    account = RewardDayAccrual("0xcond")
    day = date(2026, 11, 10)
    start = _utc(2026, 11, 10)
    account.record(start, 0.0)
    account.record(start + 3_600.0, 48.0)  # $48/day for one hour = $2.00

    assert account.pending_utc_day(day) == pytest.approx(2.0)
    assert account.settle_utc_day(day) == pytest.approx(2.0)


def test_day_accrual_splits_an_interval_at_utc_midnight() -> None:
    """Payout periods are UTC days, so a cross-midnight tick lands in two."""
    account = RewardDayAccrual("0xcond")
    first = date(2026, 11, 10)
    second = date(2026, 11, 11)
    account.record(_utc(2026, 11, 10, 23), 0.0)
    account.record(_utc(2026, 11, 11, 0) + 1_800.0, 24.0)  # 1.5 h at $24/day

    assert account.pending_utc_day(first) == pytest.approx(1.0)
    assert account.pending_utc_day(second) == pytest.approx(0.5)


def test_quote_resting_under_eligibility_age_accrues_nothing(monkeypatch, tmp_path) -> None:
    """3.0 s of rest is below the venue's 3.5 s minimum: a measured zero."""
    now = _utc(2026, 11, 10, 12)
    market = _accrual_market()
    ledger = Ledger({"RewardHarvester": 1_000.0}, tmp_path / "ledger.db")
    resting = Resting(
        strategy="RewardHarvester",
        condition_id=market.condition_id,
        token_id="yes",
        price=0.49,
        shares=200.0,
        min_order=5.0,
        placed=now - REWARD_ELIGIBILITY_SECONDS + 0.5,
        tick=0.01,
    )
    engine = _Engine(ledger, market, {"yes": resting}, now)
    _stub_plan(monkeypatch, now, [])
    _stub_rate(monkeypatch, 864.0)

    reward_runner.run_reward(
        engine, {"yes": _accrual_book(0.48, 0.52), "no": _accrual_book(0.48, 0.52)}, []
    )

    account = engine._reward_accruals[market.condition_id]
    today = datetime.fromtimestamp(now, UTC).date()
    assert account.pending_utc_day(today) == 0.0
    assert ledger.cash("RewardHarvester") == pytest.approx(1_000.0)
    ledger.close()


def test_quote_cancelled_before_eligibility_accrues_nothing(monkeypatch, tmp_path) -> None:
    """An order pulled before it ever qualified earns nothing at all."""
    now = _utc(2026, 11, 10, 12)
    market = _accrual_market()
    ledger = Ledger({"RewardHarvester": 1_000.0}, tmp_path / "ledger.db")
    resting = Resting(
        strategy="RewardHarvester",
        condition_id=market.condition_id,
        token_id="yes",
        price=0.49,
        shares=200.0,
        min_order=5.0,
        placed=now - 3.0,
        cancel_at=now - 1.0,
        tick=0.01,
    )
    engine = _Engine(ledger, market, {"yes": resting}, now)
    _stub_plan(monkeypatch, now, [])
    _stub_rate(monkeypatch, 864.0)

    reward_runner.run_reward(
        engine, {"yes": _accrual_book(0.48, 0.52), "no": _accrual_book(0.48, 0.52)}, []
    )

    # A cancelled quote is not resting, so it is never even priced for accrual;
    # the public journal proves no accrual was banked.
    assert ledger.reward_accrual_by_condition(reward_runner.utc_day(now)) == {}
    assert ledger.cash("RewardHarvester") == pytest.approx(1_000.0)
    ledger.close()


def test_quote_resting_past_eligibility_accrues_the_eligible_interval(
    monkeypatch, tmp_path
) -> None:
    """Only the time past ``placed + 3.5 s`` is accruable."""
    now = _utc(2026, 11, 10, 12)
    market = _accrual_market()
    ledger = Ledger({"RewardHarvester": 1_000.0}, tmp_path / "ledger.db")
    resting = Resting(
        strategy="RewardHarvester",
        condition_id=market.condition_id,
        token_id="yes",
        price=0.49,
        shares=200.0,
        min_order=5.0,
        placed=now - 103.5,
        tick=0.01,
    )
    engine = _Engine(ledger, market, {"yes": resting}, now)
    _stub_plan(monkeypatch, now, [])
    _stub_rate(monkeypatch, 864.0)

    reward_runner.run_reward(
        engine, {"yes": _accrual_book(0.48, 0.52), "no": _accrual_book(0.48, 0.52)}, []
    )

    today = datetime.fromtimestamp(now, UTC).date()
    expected = 864.0 * 100.0 / 86_400.0
    account = engine._reward_accruals[market.condition_id]
    assert account.pending_utc_day(today) == pytest.approx(expected)
    # Nothing is credited until the day rolls over and clears the floor.
    assert ledger.cash("RewardHarvester") == pytest.approx(1_000.0)
    assert ledger.reward_accrual_by_condition(today.isoformat()) == {
        market.condition_id: pytest.approx(expected)
    }
    ledger.close()


def test_runner_settles_completed_days_at_the_venue_floor(monkeypatch, tmp_path) -> None:
    """At rollover the day clears: >= $1.00 pays, below $1.00 pays nothing."""
    now = _utc(2026, 11, 11, 0, second=5.0)
    market = _accrual_market()
    ledger = Ledger({"RewardHarvester": 1_000.0}, tmp_path / "ledger.db")
    engine = _Engine(ledger, market, {}, now)
    yesterday = _utc(2026, 11, 10, 1)

    clears = RewardDayAccrual("0xpays")
    clears.record(_utc(2026, 11, 10), 0.0)
    clears.record(yesterday, 48.0)  # $2.00 over the hour
    short = RewardDayAccrual("0xshort")
    short.record(_utc(2026, 11, 10), 0.0)
    short.record(yesterday, 12.0)  # $0.50 over the hour
    engine._reward_accruals = {"0xpays": clears, "0xshort": short}
    _stub_plan(monkeypatch, now, [])

    reward_runner.run_reward(engine, {}, [])

    assert ledger.cash("RewardHarvester") == pytest.approx(1_002.0)
    assert clears.pending_days() == ()
    assert short.pending_days() == ()
    ledger.close()


def test_fully_filled_quote_is_re_placed(monkeypatch, tmp_path) -> None:
    """A fill leaves ``shares == 0`` with ``cancel_at is None``: it is gone."""
    now = _utc(2026, 11, 10, 12)
    market = _accrual_market()
    ledger = Ledger({"RewardHarvester": 1_000.0}, tmp_path / "ledger.db")
    filled = Resting(
        strategy="RewardHarvester",
        condition_id=market.condition_id,
        token_id="yes",
        price=0.49,
        shares=0.0,
        min_order=5.0,
        placed=now - 600.0,
        tick=0.01,
    )
    engine = _Engine(ledger, market, {"yes": filled}, now)
    _stub_plan(
        monkeypatch,
        now,
        [
            RewardQuote(
                condition_id=market.condition_id,
                question="Q",
                token_id="yes",
                price=0.49,
                shares=200.0,
                daily_payout=12.0,
                competing_score=1_000.0,
                max_spread_cents=3.0,
                min_size=200.0,
            )
        ],
    )

    reward_runner.run_reward(
        engine, {"yes": _accrual_book(0.48, 0.52), "no": _accrual_book(0.48, 0.52)}, []
    )

    kinds = [kind for kind, *_ in engine.actions.submitted]
    assert "reward_quote" in kinds
    # The remainder is already gone; there is nothing to cancel first.
    assert "reward_cancel" not in kinds
    placed = next(payload for kind, _, payload in engine.actions.submitted
                  if kind == "reward_quote")
    assert placed["token_id"] == "yes"
    assert placed["resting"].shares == pytest.approx(200.0)
    ledger.close()


def test_resting_quote_at_the_same_price_is_not_re_placed(monkeypatch, tmp_path) -> None:
    """The replacement gate must not duplicate an order that is still resting."""
    now = _utc(2026, 11, 10, 12)
    market = _accrual_market()
    ledger = Ledger({"RewardHarvester": 1_000.0}, tmp_path / "ledger.db")
    live = Resting(
        strategy="RewardHarvester",
        condition_id=market.condition_id,
        token_id="yes",
        price=0.49,
        shares=200.0,
        min_order=5.0,
        placed=now - 600.0,
        tick=0.01,
    )
    engine = _Engine(ledger, market, {"yes": live}, now)
    _stub_rate(monkeypatch, 864.0)
    _stub_plan(
        monkeypatch,
        now,
        [
            RewardQuote(
                condition_id=market.condition_id,
                question="Q",
                token_id="yes",
                price=0.49,
                shares=200.0,
                daily_payout=12.0,
                competing_score=1_000.0,
                max_spread_cents=3.0,
                min_size=200.0,
            )
        ],
    )

    reward_runner.run_reward(
        engine, {"yes": _accrual_book(0.48, 0.52), "no": _accrual_book(0.48, 0.52)}, []
    )

    kinds = [kind for kind, *_ in engine.actions.submitted]
    assert "reward_quote" not in kinds
    assert "reward_cancel" not in kinds
    ledger.close()


# ---------------------------------------------------------------------------
# Paper fill model: FIFO consumption, not a pro-rata share of the level
#
# The old path gave the virtual quote ``our / (level + our)`` of every print at
# its price. That is the share of a *pro-rata* venue. On a FIFO book the quote
# sits behind the size that was at its price when it landed, so a print first
# pays that queue and only the overflow is ours.
# ---------------------------------------------------------------------------


def _fifo_quote(market, price: float = 0.49, shares: float = 100.0) -> Resting:
    return Resting(
        strategy="RewardHarvester",
        condition_id=market.condition_id,
        token_id="yes",
        price=price,
        shares=shares,
        min_order=20.0,
        placed=1_000.0,
        tick=0.01,
    )


def test_fifo_print_pays_the_queue_ahead_before_it_fills_us(tmp_path) -> None:
    """100 shares rest ahead of us: a 100-share print is not our fill."""
    from polymarketbot.paper.pillars import reward_runner
    from polymarketbot.strategy.venue.reads import Print

    market = _accrual_market()
    ledger = Ledger({"RewardHarvester": 1_000.0}, tmp_path / "fifo.db")
    quote = _fifo_quote(market)
    engine = _Engine(ledger, market, {"yes": quote}, 1_000.0)
    books = {"yes": _accrual_book(0.49, 0.52, size=100.0)}
    # 100 shares are displayed at our price when we land: they are ahead of us.
    reward_runner.apply_resting_fills(
        engine, {"yes": quote}, "RewardHarvester", books,
        [Print("yes", market.condition_id, "SELL", 0.49, 100.0, 1_001.0)], 1_001.0,
    )
    assert ledger.positions("RewardHarvester") == []

    # The queue ahead is now clear; the next print reaches us.
    reward_runner.apply_resting_fills(
        engine, {"yes": quote}, "RewardHarvester", books,
        [Print("yes", market.condition_id, "SELL", 0.49, 40.0, 1_002.0)], 1_002.0,
    )
    lots = ledger.positions("RewardHarvester")
    assert len(lots) == 1
    assert lots[0].shares == pytest.approx(40.0)
    ledger.close()


def test_fifo_print_through_the_bid_fills_the_order(tmp_path) -> None:
    """A print strictly below a resting bid means the taker swept through us."""
    from polymarketbot.paper.pillars import reward_runner
    from polymarketbot.strategy.venue.reads import Print

    market = _accrual_market()
    ledger = Ledger({"RewardHarvester": 1_000.0}, tmp_path / "fifo2.db")
    quote = _fifo_quote(market, shares=100.0)
    engine = _Engine(ledger, market, {"yes": quote}, 1_000.0)
    books = {"yes": _accrual_book(0.49, 0.52, size=500.0)}
    reward_runner.apply_resting_fills(
        engine, {"yes": quote}, "RewardHarvester", books,
        [Print("yes", market.condition_id, "SELL", 0.47, 5.0, 1_001.0)], 1_001.0,
    )
    lots = ledger.positions("RewardHarvester")
    assert len(lots) == 1
    assert lots[0].shares == pytest.approx(100.0)
    ledger.close()
