"""Tests for find_optimal_lp_markets and dynamic ROI optimization."""

import pytest
from unittest.mock import MagicMock, patch
from polymarketbot.strategy.reward.scanner import find_optimal_lp_markets, get_real_l2_score


def _venue_get(markets_payload: dict, tick: float = 0.01):
    """A ``Session.get`` stand-in that answers both endpoints the scan calls.

    The scanner reads the venue's published tick per token (``/tick-size``) in
    addition to the reward campaigns, because the tick is the grid the quote
    distance is measured on and the campaign rows do not publish it. A mock that
    answers every URL with the campaign payload leaves the tick unmeasured, and
    the scanner now correctly refuses those markets.
    """

    def _get(*args, **kwargs):
        # Works both as a bound replacement method (self, url, ...) and as a
        # ``side_effect`` (url, ...).
        url = kwargs.get("url") if "url" in kwargs else (args[1] if len(args) > 1 else args[0])
        resp = MagicMock()
        resp.status_code = 200
        if "tick-size" in str(url):
            resp.json.return_value = {"minimum_tick_size": str(tick)}
        else:
            resp.json.return_value = markets_payload
        return resp

    return _get


def test_get_real_l2_score_calculation():
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "bids": [
            {"price": "0.49", "size": "100.0"},
            {"price": "0.48", "size": "200.0"},
        ],
        "asks": [
            {"price": "0.51", "size": "100.0"},
            {"price": "0.52", "size": "200.0"},
        ],
    }
    mock_session.get.return_value = mock_resp

    comp_score, qual_shares, mid = get_real_l2_score(
        session=mock_session,
        token_id="0x123",
        max_spread_cents=3.0,
    )

    assert mid == 0.50
    assert qual_shares == 600.0
    assert comp_score > 0.0


def test_find_optimal_lp_markets_filters():
    mock_markets_payload = {
        "data": [
            # 1. Market that is too close to expiry (< 7 days) -> should be excluded
            {
                "condition_id": "0x_expired",
                "question": "Expiring Tomorrow",
                "rewards_config": [{"rate_per_day": "500.0"}],
                "rewards_max_spread": 4.5,
                "rewards_min_size": 20.0,
                "end_date_iso": "2026-11-04T00:00:00Z",
                "tokens": [
                    {"token_id": "100000000000000000000000000000000000000000000000000000000000001", "outcome": "YES", "price": "0.50"},
                    {"token_id": "100000000000000000000000000000000000000000000000000000000000002", "outcome": "NO", "price": "0.50"},
                ],
            },
            # 2. Market with extreme odds (diverged to 0.95 / 0.05) -> should be excluded
            {
                "condition_id": "0x_diverged",
                "question": "Already Decided",
                "rewards_config": [{"rate_per_day": "500.0"}],
                "rewards_max_spread": 4.5,
                "rewards_min_size": 20.0,
                "end_date_iso": "2026-12-31T00:00:00Z",
                "tokens": [
                    {"token_id": "100000000000000000000000000000000000000000000000000000000000003", "outcome": "YES", "price": "0.95"},
                    {"token_id": "100000000000000000000000000000000000000000000000000000000000004", "outcome": "NO", "price": "0.05"},
                ],
            },
            # 3. Valid sweet spot market -> should pass and rank high
            {
                "condition_id": "0x_sweet_spot",
                "question": "Valid Sweet Spot Market",
                "rewards_config": [{"rate_per_day": "400.0"}],
                "rewards_max_spread": 4.5,
                "rewards_min_size": 20.0,
                "end_date_iso": "2026-12-31T00:00:00Z",
                "tokens": [
                    {"token_id": "100000000000000000000000000000000000000000000000000000000000005", "outcome": "YES", "price": "0.60"},
                    {"token_id": "100000000000000000000000000000000000000000000000000000000000006", "outcome": "NO", "price": "0.40"},
                ],
            },
        ]
    }

    with patch("requests.Session.get", _venue_get(mock_markets_payload)):
        # Mock orderbook L2 response
        with patch("polymarketbot.strategy.reward.scanner.lp_screen.get_real_l2_score") as mock_l2:
            mock_l2.return_value = (500.0, 1000.0, 0.60)

            ranked = find_optimal_lp_markets(bankroll_usd=150.0, min_days_to_expiry=7.0)

            assert len(ranked) == 1
            assert ranked[0]["condition_id"] == "0x_sweet_spot"
            assert ranked[0]["clears_floor"] is True
            assert ranked[0]["real_daily_payout"] > 1.00
            assert ranked[0]["daily_roi_pct"] > 0.0


def test_scanner_5s_ttl_caching_and_telemetry():
    """Verify that multiple scans within 5s return cached results and track cache hits."""
    from polymarketbot.strategy.reward.scanner import (
        get_scanner_telemetry,
        reset_scanner_telemetry,
        find_optimal_lp_markets,
    )

    reset_scanner_telemetry()
    mock_payload = {
        "data": [
            {
                "condition_id": "0x_cache_test",
                "question": "Cache Test Market",
                "rewards_config": [{"rate_per_day": "300.0"}],
                "rewards_max_spread": 4.5,
                "rewards_min_size": 20.0,
                "end_date_iso": "2026-12-31T00:00:00Z",
                "tokens": [
                    {"token_id": "200000000000000000000000000000000000000000000000000000000000001", "outcome": "YES", "price": "0.55"},
                    {"token_id": "200000000000000000000000000000000000000000000000000000000000002", "outcome": "NO", "price": "0.45"},
                ],
            }
        ]
    }

    with patch("requests.Session.get", side_effect=_venue_get(mock_payload)) as mock_get:
        with patch("polymarketbot.strategy.reward.scanner.lp_screen.get_real_l2_score") as mock_l2:
            mock_l2.return_value = (300.0, 500.0, 0.55)

            # First scan -> Cache Miss (fetches via network)
            res1 = find_optimal_lp_markets(bankroll_usd=150.0)
            telemetry = get_scanner_telemetry()
            assert telemetry.total_scans == 1
            assert telemetry.cache_misses == 1
            assert telemetry.cache_hits == 0
            assert len(res1) == 1
            assert res1[0]["condition_id"] == "0x_cache_test"

            # Second scan immediately -> 5s TTL Cache Hit (zero HTTP calls!)
            mock_get.reset_mock()
            res2 = find_optimal_lp_markets(bankroll_usd=150.0)
            mock_get.assert_not_called()  # Proves zero network calls
            assert telemetry.total_scans == 2
            assert telemetry.cache_hits == 1
            assert len(res2) == 1
            assert res2[0]["condition_id"] == "0x_cache_test"


def test_get_real_l2_score_caching_and_429():
    """Verify that get_real_l2_score caches inside 5s and tracks HTTP 429 status."""
    from polymarketbot.strategy.reward.scanner import (
        get_real_l2_score,
        get_scanner_telemetry,
        reset_scanner_telemetry,
    )

    reset_scanner_telemetry()
    mock_session = MagicMock()

    # 1. Successful query
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "bids": [{"price": "0.50", "size": "100.0"}],
        "asks": [{"price": "0.52", "size": "100.0"}],
    }
    mock_session.get.return_value = mock_resp

    score1, sz1, mid1 = get_real_l2_score(mock_session, "token_l2_cache", 3.0)
    assert mid1 == 0.51
    telemetry = get_scanner_telemetry()
    assert telemetry.total_book_queries == 1

    # Immediate second call for same token -> served from _BOOK_CACHE
    mock_session.get.reset_mock()
    score2, sz2, mid2 = get_real_l2_score(mock_session, "token_l2_cache", 3.0)
    mock_session.get.assert_not_called()
    assert score2 == score1
    assert mid2 == mid1
    assert telemetry.total_book_queries == 1  # Not incremented because cached

    # 2. HTTP 429 Error on a new token
    mock_session.get.reset_mock()
    mock_429 = MagicMock()
    mock_429.status_code = 429
    mock_session.get.return_value = mock_429

    score_429, sz_429, mid_429 = get_real_l2_score(mock_session, "token_429", 3.0)
    assert telemetry.http_429_errors == 1
    assert "HTTP 429" in telemetry.last_error_message



# ---------------------------------------------------------------------------
# Measured microstructure: one test per removed invention.
#
# The scanner used to price every projection off constants it made up:
# ``CATEGORY_REGIMES`` (per-category vol/jump/trade-rate with no source and no
# window), ``touch_depth = 1000.0``, a ``min(0.30, ...)`` fill-share cap,
# ``concentration_pct = 0.75`` and ``bankroll_usd = 300.0``. Each test below
# pins the replacement: a measured input is used as measured, and a missing one
# refuses the market.
# ---------------------------------------------------------------------------

from polymarketbot.strategy.reward.regime import MarketRegime, NS_PER_HOUR


def _regime(mean_size: float = 40.0) -> MarketRegime:
    window_end_ns = 1_794_306_326_926_867_001
    return MarketRegime(
        condition_id="0xmeasured",
        window_start_ns=window_end_ns - int(24.0 * NS_PER_HOUR),
        window_end_ns=window_end_ns,
        trades=400,
        trades_per_min=0.28,
        mean_trade_size_shares=mean_size,
        size_cv=1.1,
        realised_vol_bps_per_min=90.0,
        jump_freq=0.02,
        span_minutes=55.0,
        required_sample=80,
        jump_band_cents=3.0,
    )


def _yield_kwargs(**overrides):
    base = dict(
        daily_rate=500.0,
        max_spread_cents=3.0,
        min_size=20.0,
        allocated_capital_usd=1000.0,
        tick_size=0.01,
        quote_horizon_s=300.0,
        spread_cents=1.0,
        mid_price=0.40,
        competing_score=1000.0,
        regime=_regime(),
        gas_fee_per_merge=0.01,
        merge_threshold=30.0,
    )
    base.update(overrides)
    return base


def test_category_regimes_are_gone_and_the_tape_regime_drives_the_estimate():
    """No category table: the market's own measured print size sets the terms."""
    import polymarketbot.strategy.reward.scoring as scoring
    from polymarketbot.strategy.reward.scanner import calculate_expected_yield

    assert not hasattr(scoring, "CATEGORY_REGIMES")

    small = calculate_expected_yield(**_yield_kwargs(regime=_regime(40.0)))
    large = calculate_expected_yield(**_yield_kwargs(regime=_regime(80.0)))

    # Spread capture is (measured fills/day) * min(quote_size, measured mean
    # print size) * distance, so doubling the measured print size doubles it.
    assert small["expected_spread_capture"] > 0.0
    assert large["expected_spread_capture"] == pytest.approx(
        2.0 * small["expected_spread_capture"]
    )
    # The measured regime travels with the estimate, window and prints included.
    assert small["unmeasured_reason"] is None


def test_missing_tape_regime_refuses_the_market():
    """An unmeasured regime is missing data: no estimate, reason logged."""
    from polymarketbot.strategy.reward.scanner import calculate_expected_yield

    refused = calculate_expected_yield(**_yield_kwargs(regime=None))
    assert refused["daily_reward_payout"] is None
    assert refused["unmeasured_reason"].startswith("unmeasured:")


def test_fill_probability_is_the_measured_horizon_cell_not_a_fill_share():
    """P(fill) comes from the measured (tick, flow, horizon) cell, uncapped."""
    from polymarketbot.strategy.reward.measurement import load_quote_fill_horizon
    from polymarketbot.strategy.reward.scanner import calculate_expected_yield

    table = load_quote_fill_horizon()
    expected_p = table.p_fill(300.0, 0.01, 20.0)
    assert expected_p is not None

    metrics = calculate_expected_yield(**_yield_kwargs())
    assert metrics["p_fill"] == pytest.approx(expected_p)
    assert metrics["priced_horizon_s"] == pytest.approx(300.0)
    # Fills per day is the measured probability repeated over the lifetime; the
    # old 0.30 fill-share cap and 1000-share touch depth are gone, so this is
    # the table's number and nothing else.
    assert metrics["expected_fills_per_day"] == pytest.approx(
        expected_p * (86_400.0 / 300.0)
    )


def test_fill_probability_is_read_per_tick_from_the_measured_table():
    """A finer tick has its own measured probability; it is not a ratio guess."""
    from polymarketbot.strategy.reward.scanner import calculate_expected_yield

    coarse = calculate_expected_yield(**_yield_kwargs(tick_size=0.01))
    fine = calculate_expected_yield(**_yield_kwargs(tick_size=0.001))

    assert coarse["p_fill"] != fine["p_fill"]
    assert fine["p_fill"] < coarse["p_fill"]  # measured: a 1-tick-e-3 quote is rarer


def test_unmeasured_fill_cell_refuses_instead_of_capping():
    """A (tick, flow, horizon) the table does not cover has no probability."""
    from polymarketbot.strategy.reward.scanner import calculate_expected_yield

    # Below the shortest measured lifetime (5 s) there is no cell to lean on.
    refused = calculate_expected_yield(**_yield_kwargs(quote_horizon_s=1.0))
    assert refused["daily_reward_payout"] is None
    assert "no measured fill probability" in refused["unmeasured_reason"]

    # Above every measured flow bucket for this tick: also unmeasured.
    refused_flow = calculate_expected_yield(**_yield_kwargs(min_size=10_000.0))
    assert refused_flow["daily_reward_payout"] is None
    assert "no measured fill probability" in refused_flow["unmeasured_reason"]


def test_capital_is_the_callers_and_has_no_default():
    """``bankroll_usd``/``concentration_pct`` are gone; capital is required."""
    from polymarketbot.strategy.reward.scanner import calculate_expected_yield

    # Passing the old pair is a TypeError, not a silently accepted allocation.
    with pytest.raises(TypeError):
        calculate_expected_yield(**_yield_kwargs(bankroll_usd=300.0))
    with pytest.raises(TypeError):
        calculate_expected_yield(**_yield_kwargs(concentration_pct=0.75))

    # And omitting the allocation cannot fall back to a default.
    omitted = {k: v for k, v in _yield_kwargs().items() if k != "allocated_capital_usd"}
    with pytest.raises(TypeError):
        calculate_expected_yield(**omitted)

    small = calculate_expected_yield(**_yield_kwargs(allocated_capital_usd=500.0))
    large = calculate_expected_yield(**_yield_kwargs(allocated_capital_usd=1000.0))
    # More capital rests more shares, so the maker score rises with it.
    assert large["maker_score"] > small["maker_score"]
    assert large["net_yield_per_dollar"] == pytest.approx(
        large["net_expected_yield"] / 1000.0
    )

    zero = calculate_expected_yield(**_yield_kwargs(allocated_capital_usd=0.0))
    assert zero["daily_reward_payout"] is None
    assert "allocated no capital" in zero["unmeasured_reason"]


def test_quote_horizon_is_required_and_has_no_default():
    """A fill probability without the lifetime it was measured over is not one."""
    from polymarketbot.strategy.reward.scanner import calculate_expected_yield

    with pytest.raises(TypeError):
        calculate_expected_yield(
            **{k: v for k, v in _yield_kwargs().items() if k != "quote_horizon_s"}
        )


def test_unavailable_measurement_tables_refuse_rather_than_extrapolate(monkeypatch):
    """A missing fill/markout artifact is an explicit refusal, not a guess."""
    import polymarketbot.strategy.reward.scanner.pricing as scanner_mod

    scanner_mod.reset_measured_tables()
    monkeypatch.setattr(scanner_mod, "_FILL_HORIZON", None)
    monkeypatch.setattr(scanner_mod, "_FILL_HORIZON_TRIED", True)
    refused = scanner_mod.calculate_expected_yield(**_yield_kwargs())
    assert refused["daily_reward_payout"] is None
    assert "horizon fill measurement unavailable" in refused["unmeasured_reason"]

    scanner_mod.reset_measured_tables()
    monkeypatch.setattr(scanner_mod, "_MAKER_MARKOUT", None)
    monkeypatch.setattr(scanner_mod, "_MAKER_MARKOUT_TRIED", True)
    refused_markout = scanner_mod.calculate_expected_yield(**_yield_kwargs())
    assert refused_markout["daily_reward_payout"] is None
    assert "markout measurement unavailable" in refused_markout["unmeasured_reason"]


def test_lp_scan_requires_the_callers_bankroll():
    """``find_optimal_lp_markets`` has no default bankroll any more."""
    from polymarketbot.strategy.reward.scanner import find_optimal_lp_markets

    with pytest.raises(TypeError):
        find_optimal_lp_markets()
    # A zero bankroll cannot fund a minimum lot, so the scan refuses up front
    # instead of returning a universe sized on nothing.
    assert find_optimal_lp_markets(bankroll_usd=0.0) == []


def test_scan_refuses_markets_without_a_measured_regime():
    """The scanner measures regimes from the tape and refuses the rest."""
    from polymarketbot.strategy.reward.scanner import OpportunityScanner

    payload = {
        "data": [
            {
                "condition_id": "0x_no_tape_prints",
                "question": "A market with no measured prints",
                "rewards_config": [{"rate_per_day": "400.0"}],
                "rewards_max_spread": 3.0,
                "rewards_min_size": 20.0,
                "tokens": [
                    {"token_id": "1", "outcome": "YES", "price": "0.60"},
                    {"token_id": "2", "outcome": "NO", "price": "0.40"},
                ],
            }
        ]
    }
    scanner = OpportunityScanner()
    with patch("polymarketbot.strategy.reward.scanner.screen.measure_regimes_cached") as mock_measure:
        from polymarketbot.strategy.reward.regime import RegimeMeasurement

        mock_measure.return_value = RegimeMeasurement(
            window_start_ns=0,
            window_end_ns=1,
            regimes={},
            refusals={"0x_no_tape_prints": "unmeasured: 1 print(s) in the 24 h window"},
        )
        ranked = scanner.scan_and_rank_markets(
            allocated_capital_usd=500.0,
            quote_horizon_s=300.0,
            raw_markets=payload["data"],
        )
    assert ranked == []
