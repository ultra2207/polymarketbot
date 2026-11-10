"""Unit and integration tests for Polymarket Liquidity Rewards module."""

from __future__ import annotations

import pytest

from polymarketbot.rewards.scoring import (
    OrderQuote,
    calculate_epoch_payout,
    compute_book_side_scores,
    compute_minimum_score,
    normalize_sample_score,
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


def test_competitor_depth_ladder_and_jensens_inequality() -> None:
    from polymarketbot.rewards.scoring import (
        DEFAULT_COMPETITOR_LADDER,
        compute_competitor_ladder_score,
    )

    v = 3.5  # 3.5c max qualifying spread
    depth = 10_000.0  # 10,000 shares qualifying depth

    # Lumped assumption at s = 1.5c:
    lumped_score = quadratic_order_score(max_spread_cents=v, spread_cents=1.5, size=depth)
    # ((3.5 - 1.5)/3.5)^2 * 10000 = (2.0/3.5)^2 * 10000 = 0.32653 * 10000 = 3265.31

    # Realistic 4-tier ladder:
    ladder_score = compute_competitor_ladder_score(
        max_spread_cents=v,
        qualifying_depth=depth,
        ladder=DEFAULT_COMPETITOR_LADDER,
    )

    # Jensen's inequality: convexity of ((v - s)/v)^2 means ladder score must be strictly greater than lumped!
    assert ladder_score > lumped_score
    # Exact calculation for ladder:
    # 35% @ 0.5c: ((3.0/3.5)^2) * 3500 = 0.73469 * 3500 = 2571.43
    # 30% @ 1.0c: ((2.5/3.5)^2) * 3000 = 0.51020 * 3000 = 1530.61
    # 20% @ 2.0c: ((1.5/3.5)^2) * 2000 = 0.18367 * 2000 = 367.35
    # 15% @ 3.5c: ((0.0/3.5)^2) * 1500 = 0
    # Total = 2571.43 + 1530.61 + 367.35 = 4469.39
    assert ladder_score == pytest.approx(4469.39, rel=1e-3)
    # Ladder score is ~36.9% higher than lumped score
    assert (ladder_score / lumped_score) == pytest.approx(1.3687, rel=1e-3)



# ---------------------------------------------------------------------------
# Allocator: measured inputs, USD activation lots, KKT water-filling
# ---------------------------------------------------------------------------

from polymarketbot.rewards.allocator import (  # noqa: E402
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


def test_activation_lot_is_shares_at_the_mid_not_dollars() -> None:
    """min_size is a share count; the optimizer budget is USD."""
    allocator = LiquidityRewardsAllocator()
    # 200 shares at mid 0.10 is a $20 lot: it must fit a $100 bankroll, which
    # the old share-as-dollar floor (200 > 100) wrongly rejected.
    cheap = _candidate(min_size=200.0, real_mid=0.10, quote_price=0.09)
    result = allocator.optimize_allocation_milp([cheap], bankroll_usd=100.0)
    assert result.active_markets_count == 1
    assert result.allocations[0].allocated_capital >= 200.0 * 0.10
    assert result.allocations[0].quote_size >= 200.0


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
    from polymarketbot.rewards.allocator import calculate_endogenous_reserve

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
    from polymarketbot.rewards.tape_regime import MarketRegime, NS_PER_HOUR

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
MEASURED_INPUTS = dict(
    allocated_capital_usd=1000.0,
    tick_size=0.01,
    quote_horizon_s=300.0,
    regime=_measured_regime(),
)


def test_expected_yield_uses_measured_distance_and_mid() -> None:
    """No 0.5c / 0.50 defaults: an unmeasured quote earns no estimate."""
    from polymarketbot.rewards.scanner import calculate_expected_yield

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
    from polymarketbot.rewards.scanner import calculate_expected_yield

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
    import polymarketbot.trading.ctf as ctf_mod
    from polymarketbot.rewards.scanner import calculate_expected_yield

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

    accrual_only = allocator.optimize_allocation_milp(
        [dict(empty, fill_yield_usd_per_usd_per_day=None, stay_fraction=None, p_fill=None),
         dict(liquid, fill_yield_usd_per_usd_per_day=None, stay_fraction=None, p_fill=None)],
        bankroll_usd=200.0,
    )
    assert accrual_only.allocations, "accrual-only mode must still fund something"
    # The defect: on the accrual alone the empty book ranks first.
    assert accrual_only.allocations[0].condition_id == "0xempty"

    priced = allocator.optimize_allocation_milp([empty, liquid], bankroll_usd=200.0)
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
    from polymarketbot.paper.pillars.reward import expected_fill_yield

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
