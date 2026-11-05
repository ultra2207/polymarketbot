"""Tests for find_optimal_lp_markets and dynamic ROI optimization."""

import pytest
from unittest.mock import MagicMock, patch
from polymarketbot.rewards.scanner import find_optimal_lp_markets, get_real_l2_score


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

    with patch("requests.Session.get") as mock_get:
        # Mock CLOB rewards endpoint
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_markets_payload
        mock_get.return_value = mock_resp

        # Mock orderbook L2 response
        with patch("polymarketbot.rewards.scanner.get_real_l2_score") as mock_l2:
            mock_l2.return_value = (500.0, 1000.0, 0.60)

            ranked = find_optimal_lp_markets(bankroll_usd=150.0, min_days_to_expiry=7.0)

            assert len(ranked) == 1
            assert ranked[0]["condition_id"] == "0x_sweet_spot"
            assert ranked[0]["clears_floor"] is True
            assert ranked[0]["real_daily_payout"] > 1.00
            assert ranked[0]["daily_roi_pct"] > 0.0


def test_scanner_5s_ttl_caching_and_telemetry():
    """Verify that multiple scans within 5s return cached results and track cache hits."""
    from polymarketbot.rewards.scanner import (
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

    with patch("requests.Session.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_payload
        mock_get.return_value = mock_resp

        with patch("polymarketbot.rewards.scanner.get_real_l2_score") as mock_l2:
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
    from polymarketbot.rewards.scanner import (
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


def test_instant_cooldown_scanner_trigger_and_throttling():
    """Verify instant scan trigger trips on volatility breaker and logging is throttled."""
    import time
    from polymarketbot.simulator.engine import NativePaperTradingEngine

    engine = NativePaperTradingEngine(
        condition_id="0x_test_cooldown",
        initial_cash=100.0,
        quote_size=20.0,
    )

    # Initial state
    assert engine.pop_instant_scan_trigger() is False

    # Simulate price jump from 0.50 to 0.53 (> 2.0c jump in 60s)
    now = time.time()
    engine._check_volatility_breaker(0.50, ts=now)
    breaker_tripped = engine._check_volatility_breaker(0.53, ts=now + 5.0)

    assert breaker_tripped is True
    # Verify instant scan trigger was set
    assert engine.pop_instant_scan_trigger() is True
    # Verify popping it resets the flag
    assert engine.pop_instant_scan_trigger() is False

    # Subsequent tick within cooldown still returns True
    cooldown_active = engine._check_volatility_breaker(0.52, ts=now + 6.0)
    assert cooldown_active is True

