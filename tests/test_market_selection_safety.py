"""Safety tests for live market selection (audit S16 + the unit-guard half of C12).

Covers:
- find_optimal_lp_markets rejects fake/non-numeric token ids and unusable reward configs.
- Candidate payloads carry neg_risk / tick_size / fee_rate_bps / end_date_iso / accepting_orders.
- Reported qualifying depth is the audited sum; the payout-model floor is flagged, not hidden.
- get_real_l2_score refuses crossed books and non-positive spread thresholds.
- validate_market_for_live returns a human-readable reason for every rejection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from polymarketbot.strategy.reward import scanner
from polymarketbot.strategy.reward.scanner import (
    find_optimal_lp_markets,
    get_real_l2_score,
    reset_scanner_telemetry,
    validate_market_for_live,
)

YES_TOKEN = "1234567890123456789012345678901234567890"
NO_TOKEN = "9876543210987654321098765432109876543210"
FUTURE_END = "2099-12-31T00:00:00Z"


def _tokens(yes_token: str = YES_TOKEN, no_token: str = NO_TOKEN) -> list[dict[str, str]]:
    return [
        {"token_id": yes_token, "outcome": "YES", "price": "0.60"},
        {"token_id": no_token, "outcome": "NO", "price": "0.40"},
    ]


def _market(**overrides: object) -> dict:
    payload: dict = {
        "condition_id": "0xabc123",
        "question": "Will the safety test pass?",
        "rewards_config": [{"rate_per_day": "400.0"}],
        "rewards_max_spread": 4.5,
        "rewards_min_size": 20.0,
        "end_date_iso": FUTURE_END,
        "tokens": _tokens(),
    }
    payload.update(overrides)
    return payload


def _venue_get(markets: list[dict], tick: float = 0.01):
    """Answer both endpoints the scan calls: reward campaigns and ``/tick-size``.

    The tick is the grid the quote distance is measured on and the campaign rows
    do not publish it, so the scanner reads it from the venue; a mock that
    answers every URL with the campaign payload leaves it unmeasured and the
    market is refused.
    """

    def _get(*args, **kwargs):
        url = kwargs.get("url") if "url" in kwargs else (args[1] if len(args) > 1 else args[0])
        response = MagicMock()
        response.status_code = 200
        if "tick-size" in str(url):
            response.json.return_value = {"minimum_tick_size": str(tick)}
        else:
            response.json.return_value = {"data": markets}
        return response

    return _get


def _run_scan(markets: list[dict], **kwargs: object) -> list[dict]:
    reset_scanner_telemetry()
    with patch("requests.Session.get", side_effect=_venue_get(markets)), patch(
        "polymarketbot.strategy.reward.scanner.lp_screen.get_real_l2_score",
        return_value=(500.0, 1000.0, 0.60),
    ):
        return find_optimal_lp_markets(bankroll_usd=150.0, limit_output=5, **kwargs)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"tokens": _tokens(yes_token="0xabc_YES")}, id="yes-token-fake-suffix"),
        pytest.param({"tokens": _tokens(yes_token="cid123_YES")}, id="yes-token-cid-fallback"),
        pytest.param({"tokens": _tokens(no_token="0xdeadbeef")}, id="no-token-hex"),
        pytest.param({"tokens": _tokens(no_token="")}, id="no-token-empty"),
        pytest.param({"tokens": [_tokens()[0]]}, id="no-token-missing"),
        pytest.param({"rewards_min_size": 0}, id="min-size-zero"),
        pytest.param({"rewards_min_size": None}, id="min-size-missing"),
        pytest.param({"rewards_max_spread": 0.05}, id="spread-too-tight"),
        pytest.param({"rewards_max_spread": 10.5}, id="spread-too-wide"),
        pytest.param({"rewards_max_spread": None}, id="spread-missing"),
        pytest.param({"rewards_max_spread": float("nan")}, id="spread-nan"),
        pytest.param({"closed": True}, id="closed"),
        pytest.param({"archived": True}, id="archived"),
        pytest.param({"accepting_orders": False}, id="not-accepting-orders"),
        pytest.param({"accepting_orders": "maybe"}, id="accepting-orders-malformed"),
        pytest.param({"end_date_iso": "not-a-date"}, id="end-date-unparseable"),
        pytest.param({"end_date_iso": "2020-01-01T00:00:00Z"}, id="end-date-past"),
    ],
)
def test_find_optimal_rejects_unsafe_markets(overrides: dict) -> None:
    ranked = _run_scan([_market(**overrides)])

    assert ranked == []
    # The empty result is cached, not silently skipped: a repeat scan answers
    # from the cache without touching the venue again.
    with patch("requests.Session.get") as mock_get:
        again = find_optimal_lp_markets(bankroll_usd=150.0, limit_output=5)
    assert again == []
    mock_get.assert_not_called()


def test_find_optimal_accepts_valid_market() -> None:
    ranked = _run_scan([_market()])

    assert len(ranked) == 1
    candidate = ranked[0]
    assert candidate["condition_id"] == "0xabc123"
    assert candidate["yes_token"] == YES_TOKEN
    assert candidate["no_token"] == NO_TOKEN


def test_market_without_any_expiry_is_rejected_not_ranked_distant() -> None:
    """An unknown expiry is missing data, not the furthest date.

    Ranking it 999 days out admitted already-ended markets to the live validator,
    which then aborted startup. A payload that publishes ``end_date`` (the field
    the CLOB rewards endpoint actually sends) stays rankable.
    """
    no_expiry = _market()
    no_expiry.pop("end_date_iso")

    assert _run_scan([no_expiry]) == []

    payload_expiry = _market()
    payload_expiry.pop("end_date_iso")
    payload_expiry["end_date"] = FUTURE_END
    ranked = _run_scan([payload_expiry])

    assert len(ranked) == 1
    assert ranked[0]["days_left"] > 0.0


def test_find_optimal_carries_execution_metadata() -> None:
    ranked = _run_scan(
        [
            _market(
                neg_risk=True,
                tick_size=0.001,
                fee_rate_bps=75,
                accepting_orders=True,
            )
        ]
    )

    assert len(ranked) == 1
    candidate = ranked[0]
    assert candidate["neg_risk"] is True
    assert candidate["tick_size"] == pytest.approx(0.001)
    assert candidate["fee_rate_bps"] == 75
    assert candidate["end_date_iso"] == FUTURE_END
    assert candidate["accepting_orders"] is True


def test_find_optimal_metadata_defaults_and_camel_case_aliases() -> None:
    ranked = _run_scan([_market(negRisk=True, minimum_tick_size=0.001)])

    assert len(ranked) == 1
    candidate = ranked[0]
    assert candidate["neg_risk"] is True
    assert candidate["tick_size"] == pytest.approx(0.001)
    # Absent payload fields use the documented safe defaults, not made-up numbers.
    assert candidate["fee_rate_bps"] == 0
    assert candidate["accepting_orders"] is None


@pytest.mark.parametrize(
    "mocked_l2, expected_score, expected_shares",
    [
        # A thin book: the audited sum is reported as measured. There is no
        # ``max(1000.0, rate * 40.0)`` floor any more - inflating a thin book to
        # 1000 made every pool look equally contested.
        ((10.0, 12.5, 0.60), 20.0, 25.0),
        # A deep book: the same sum, no cap and no floor.
        ((500.0, 1000.0, 0.60), 1000.0, 2000.0),
    ],
)
def test_reported_depth_is_the_audited_sum(
    mocked_l2: tuple[float, float, float],
    expected_score: float,
    expected_shares: float,
) -> None:
    reset_scanner_telemetry()
    with patch("requests.Session.get", side_effect=_venue_get([_market()])), patch(
        "polymarketbot.strategy.reward.scanner.lp_screen.get_real_l2_score",
        return_value=mocked_l2,
    ):
        ranked = find_optimal_lp_markets(bankroll_usd=150.0, limit_output=5)

    assert len(ranked) == 1
    candidate = ranked[0]
    assert candidate["qualifying_shares"] == pytest.approx(expected_shares)
    assert candidate["competing_score"] == pytest.approx(expected_score)
    assert "competing_score_floor_applied" not in candidate


def _book_response(bids: list[dict], asks: list[dict]) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"bids": bids, "asks": asks}
    return response


def test_crossed_book_is_not_scored() -> None:
    reset_scanner_telemetry()
    session = MagicMock()
    session.get.return_value = _book_response(
        bids=[{"price": "0.55", "size": "100.0"}],
        asks=[{"price": "0.52", "size": "100.0"}],
    )

    assert get_real_l2_score(session, "999000001", 3.0) == (None, None, None)
    # A crossed book is not cached: the next read goes back to the venue and
    # scores the now-valid book.
    session.get.return_value = _book_response(
        bids=[{"price": "0.49", "size": "100.0"}],
        asks=[{"price": "0.51", "size": "100.0"}],
    )
    scored = get_real_l2_score(session, "999000001", 3.0)
    assert scored[0] is not None and scored[0] > 0.0
    assert session.get.call_count == 2


def test_crossed_book_falls_back_to_cached_value(monkeypatch) -> None:
    reset_scanner_telemetry()
    token_id = "999000002"
    # Expire every fresh read so the crossed book must take the stale fallback.
    monkeypatch.setattr(scanner.state, "SCANNER_CACHE_TTL_SECONDS", 0.0)
    session = MagicMock()
    session.get.return_value = _book_response(
        bids=[{"price": "0.49", "size": "100.0"}],
        asks=[{"price": "0.51", "size": "100.0"}],
    )
    first = get_real_l2_score(session, token_id, 3.0)
    assert first[0] is not None and first[0] > 0.0

    session.get.return_value = _book_response(
        bids=[{"price": "0.55", "size": "100.0"}],
        asks=[{"price": "0.52", "size": "100.0"}],
    )
    assert get_real_l2_score(session, token_id, 3.0) == first


def test_non_positive_max_spread_is_not_scored() -> None:
    reset_scanner_telemetry()
    session = MagicMock()
    session.get.return_value = _book_response(
        bids=[{"price": "0.49", "size": "100.0"}],
        asks=[{"price": "0.51", "size": "100.0"}],
    )

    assert get_real_l2_score(session, "999000003", 0.0) == (None, None, None)
    session.get.assert_not_called()


def test_validate_market_for_live_accepts_valid_market() -> None:
    ok, reason = validate_market_for_live(_market())

    assert (ok, reason) == (True, "ok")


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        pytest.param({"tokens": _tokens(yes_token="cid_YES")}, "token_yes", id="yes-token"),
        pytest.param({"tokens": _tokens(no_token="0x1234")}, "token_no", id="no-token"),
        pytest.param({"condition_id": ""}, "condition_id", id="condition-id"),
        pytest.param({"rewards_min_size": 0}, "rewards_min_size", id="min-size"),
        pytest.param({"rewards_max_spread": 0.05}, "rewards_max_spread", id="spread-tight"),
        pytest.param({"rewards_max_spread": 10.5}, "rewards_max_spread", id="spread-wide"),
        pytest.param({"closed": True}, "closed", id="closed"),
        pytest.param({"archived": True}, "archived", id="archived"),
        pytest.param({"accepting_orders": False}, "accepting orders", id="not-accepting"),
        pytest.param({"accepting_orders": "maybe"}, "not a boolean", id="accepting-malformed"),
        pytest.param({"end_date_iso": None}, "end_date_iso", id="end-missing"),
        pytest.param({"end_date_iso": "not-a-date"}, "end_date_iso", id="end-unparseable"),
        pytest.param({"end_date_iso": "2020-01-01T00:00:00Z"}, "already ended", id="end-past"),
    ],
)
def test_validate_market_for_live_reports_reason(overrides: dict, needle: str) -> None:
    ok, reason = validate_market_for_live(_market(**overrides))

    assert ok is False
    assert needle in reason


def test_validate_market_for_live_uses_supplied_now() -> None:
    market = _market()

    ok, reason = validate_market_for_live(
        market, now=datetime(2099, 12, 30, tzinfo=UTC)
    )
    assert (ok, reason) == (True, "ok")

    ok, reason = validate_market_for_live(
        market, now=datetime(2100, 1, 1, tzinfo=UTC)
    )
    assert ok is False
    assert "already ended" in reason
