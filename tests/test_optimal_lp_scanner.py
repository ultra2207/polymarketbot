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
                    {"token_id": "tok1", "outcome": "YES", "price": "0.50"},
                    {"token_id": "tok2", "outcome": "NO", "price": "0.50"},
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
                    {"token_id": "tok3", "outcome": "YES", "price": "0.95"},
                    {"token_id": "tok4", "outcome": "NO", "price": "0.05"},
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
                    {"token_id": "tok5", "outcome": "YES", "price": "0.60"},
                    {"token_id": "tok6", "outcome": "NO", "price": "0.40"},
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
