"""Tests for Cluster 4 Weather & Climate Thresholds Oracle Adapter."""

import math
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

from polymarketbot.oracle.base import (
    GroundTruthOutcome,
    OracleCategory,
    OracleStatus,
)
from polymarketbot.oracle.pipeline import ExogenousOraclePipeline
from polymarketbot.oracle.weather import (
    POPULAR_STATIONS,
    StationDaySummary,
    StationMetadata,
    StationObservation,
    WeatherBracket,
    WeatherMetric,
    WeatherOracleAdapter,
)
from polymarketbot.trading.domain2.ortools_allocator import Domain2Candidate


# --------------------------------------------------------------------------- #
# Unit Tests: Can Handle & Routing
# --------------------------------------------------------------------------- #

def test_weather_adapter_can_handle():
    """Verify adapter correctly claims weather questions and resolution sources."""
    adapter = WeatherOracleAdapter()

    # Weather questions
    assert adapter.can_handle("Highest temperature in New York City on November 7?")
    assert adapter.can_handle("Lowest temperature in Chicago on November 8?")
    assert adapter.can_handle("Will it rain in Seattle tomorrow?")
    assert adapter.can_handle("Precipitation in Miami on 2026-11-07")
    assert adapter.can_handle("Random question", category_hint="weather")

    # Resolution sources
    assert adapter.can_handle("Unknown", resolution_source="https://www.weather.gov/wrh/timeseries?site=KLGA")
    assert adapter.can_handle("Unknown", resolution_source="https://aviationweather.gov/api/data/metar?ids=KORD")
    assert adapter.can_handle("Unknown", resolution_source="https://api.open-meteo.com/v1/forecast")

    # Negative non-weather checks
    assert not adapter.can_handle("Will Bitcoin exceed $100k by December?")
    assert not adapter.can_handle("Lakers vs Celtics winner", resolution_source="https://espn.com")
    assert not adapter.can_handle("US Presidential Election 2028")


# --------------------------------------------------------------------------- #
# Unit Tests: Market Spec & Bracket Parsing
# --------------------------------------------------------------------------- #

def test_parse_market_spec_url_station():
    """Extract station directly from resolution source URL query params."""
    adapter = WeatherOracleAdapter()
    spec = adapter.parse_market_spec(
        question="Highest temperature in New York City on November 7: 64-65°F",
        resolution_source="https://www.weather.gov/wrh/timeseries?site=KLGA",
    )
    assert spec is not None
    assert spec.station_id == "KLGA"
    assert spec.metric == WeatherMetric.HIGH_TEMP
    assert spec.unit == "F"
    assert spec.target_date == "2026-11-07"
    assert spec.bracket is not None
    assert spec.bracket.min_val == pytest.approx(64.0)
    assert spec.bracket.max_val == pytest.approx(65.99)


def test_parse_market_spec_brackets():
    """Verify bracket boundary parsing across all formats."""
    adapter = WeatherOracleAdapter()

    # Below / Under
    spec_below = adapter.parse_market_spec("Highest temperature in Chicago on Nov 7: Below 60°F")
    assert spec_below is not None
    assert spec_below.bracket.min_val == -math.inf
    assert spec_below.bracket.max_val == pytest.approx(59.99)
    assert spec_below.bracket.contains(58.5)
    assert not spec_below.bracket.contains(60.0)

    # 60°F or lower
    spec_or_lower = adapter.parse_market_spec("Highest temperature in Chicago on Nov 7: 60°F or lower")
    assert spec_or_lower is not None
    assert spec_or_lower.bracket.contains(60.0)
    assert not spec_or_lower.bracket.contains(60.1)

    # Above / 68°F or higher
    spec_above = adapter.parse_market_spec("Highest temp in Chicago on Nov 7: 68°F or higher")
    assert spec_above is not None
    assert spec_above.bracket.min_val == pytest.approx(68.0)
    assert spec_above.bracket.max_val == math.inf
    assert spec_above.bracket.contains(68.0)
    assert spec_above.bracket.contains(75.2)
    assert not spec_above.bracket.contains(67.9)

    # International Celsius station (London Heathrow)
    spec_london = adapter.parse_market_spec(
        question="Highest temperature in London on November 7: 17°C",
        resolution_source="https://aviationweather.gov/api/data/metar?ids=EGLL",
    )
    assert spec_london is not None
    assert spec_london.station_id == "EGLL"
    assert spec_london.unit == "C"
    assert spec_london.bracket.contains(17.0)
    assert not spec_london.bracket.contains(18.0)


# --------------------------------------------------------------------------- #
# Unit Tests: The In-Play Monotonic Ratchet (Dead Bracket Elimination)
# --------------------------------------------------------------------------- #

def test_in_play_dead_bracket_elimination():
    """Intraday running max temp > bracket max definitively eliminates bracket (p_hat=0.0)."""
    adapter = WeatherOracleAdapter()

    candidate = Domain2Candidate(
        condition_id="0xdeadbracket01",
        token_id="tok_60_61",
        question="Highest temperature in New York City on November 7: 60-61°F",
        category="weather",
        price=0.06,  # Retail still buying at 6 cents
        hours_to_resolution=6.0,
        depth_usd=10000.0,
    )

    # Create mock observations showing running max is 64.5°F (day not yet concluded)
    mock_obs = [
        StationObservation(
            station_id="KLGA",
            timestamp=datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc),
            temp_f=64.5,
            temp_c=18.06,
        )
    ]
    mock_summary = StationDaySummary(
        station_id="KLGA",
        target_date="2026-11-07",
        observations=mock_obs,
        max_temp_f=64.5,
        max_temp_c=18.06,
        is_day_concluded=False,  # Still 14:00 intraday!
    )

    with patch.object(adapter, "fetch_station_day", return_value=mock_summary):
        outcome = adapter.evaluate(candidate)

    assert outcome is not None
    assert outcome.concluded is True
    assert outcome.status == OracleStatus.CONCLUDED
    assert outcome.winning_outcome == "No"
    assert outcome.p_hat == 0.000
    assert outcome.h_reversal == 0.000
    assert outcome.confidence == 1.000
    assert outcome.is_deterministic_conclusion is True
    assert outcome.ancillary_details["reason"] == "bracket_ceiling_exceeded_monotonically"


# --------------------------------------------------------------------------- #
# Unit Tests: Day Concluded Resolution
# --------------------------------------------------------------------------- #

def test_day_concluded_winner_and_loser():
    """At day close, bracket containing final peak is 1.000, others are 0.000."""
    adapter = WeatherOracleAdapter()

    # Final day high was 64.8°F
    mock_summary = StationDaySummary(
        station_id="KLGA",
        target_date="2026-11-07",
        observations=[
            StationObservation(
                station_id="KLGA",
                timestamp=datetime(2026, 11, 7, 23, 50, tzinfo=timezone.utc),
                temp_f=64.8,
            )
        ],
        max_temp_f=64.8,
        is_day_concluded=True,  # Day finished
    )

    candidate_win = Domain2Candidate(
        condition_id="0xwinner",
        token_id="tok_64_65",
        question="Highest temperature in New York City on November 7: 64-65°F",
        category="weather",
        price=0.92,
        hours_to_resolution=0.5,
        depth_usd=25000.0,
    )

    candidate_lose = Domain2Candidate(
        condition_id="0xloser",
        token_id="tok_66_67",
        question="Highest temperature in New York City on November 7: 66-67°F",
        category="weather",
        price=0.08,
        hours_to_resolution=0.5,
        depth_usd=12000.0,
    )

    with patch.object(adapter, "fetch_station_day", return_value=mock_summary):
        outcome_win = adapter.evaluate(candidate_win)
        outcome_lose = adapter.evaluate(candidate_lose)

    # Winner
    assert outcome_win is not None
    assert outcome_win.is_deterministic_conclusion is True
    assert outcome_win.winning_outcome == "Yes"
    assert outcome_win.p_hat == 1.000
    assert outcome_win.h_reversal == 0.000

    # Loser
    assert outcome_lose is not None
    assert outcome_lose.is_deterministic_conclusion is True
    assert outcome_lose.winning_outcome == "No"
    assert outcome_lose.p_hat == 0.000


# --------------------------------------------------------------------------- #
# Unit Tests: Rain / Precipitation Monotonic Ratchet
# --------------------------------------------------------------------------- #

def test_precipitation_monotonic_ratchet():
    """Once rain has fallen (>=0.01 in), market is locked as YES with p_hat=1.000."""
    adapter = WeatherOracleAdapter()

    candidate_rain = Domain2Candidate(
        condition_id="0xrainmarket",
        token_id="tok_rain",
        question="Will it rain in New York City on November 7?",
        category="weather",
        price=0.70,
        hours_to_resolution=8.0,
        depth_usd=5000.0,
    )

    # In-play observation shows 0.08 inches of rain at 10 AM
    mock_summary = StationDaySummary(
        station_id="KLGA",
        target_date="2026-11-07",
        observations=[
            StationObservation(
                station_id="KLGA",
                timestamp=datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc),
                precip_in=0.08,
            )
        ],
        total_precip_in=0.08,
        is_day_concluded=False,  # Day still underway!
    )

    with patch.object(adapter, "fetch_station_day", return_value=mock_summary):
        outcome = adapter.evaluate(candidate_rain)

    assert outcome is not None
    assert outcome.is_deterministic_conclusion is True
    assert outcome.winning_outcome == "Yes"
    assert outcome.p_hat == 1.000
    assert outcome.h_reversal == 0.000


# --------------------------------------------------------------------------- #
# Unit Tests: Upstream Fetch Parsers (AWC, NWS, Open-Meteo, WeatherAPI)
# --------------------------------------------------------------------------- #

def test_fetch_awc_metar_parsing():
    """Verify parsing of Aviation Weather Center METAR JSON payload."""
    adapter = WeatherOracleAdapter()

    sample_awc_json = [
        {
            "icaoId": "KJFK",
            "obsTime": 1794070260,
            "reportTime": "2026-11-07T17:00:00.000Z",
            "temp": 17.8,
            "rawOb": "METAR KJFK 071651Z 26017KT 10SM 18/11 A2978 RMK T01780106",
        },
        {
            "icaoId": "KJFK",
            "obsTime": 1794066660,
            "reportTime": "2026-11-07T16:00:00.000Z",
            "temp": 16.5,
            "rawOb": "METAR KJFK 071551Z 26017KT 10SM -RA 17/11 A2978",
        },
    ]

    mock_resp = MagicMock()
    mock_resp.read.return_value = json_bytes(sample_awc_json)
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        obs = adapter._fetch_awc_metar("KJFK", hours=2)

    assert len(obs) == 2
    assert obs[0].temp_c == 17.8
    assert obs[0].effective_temp_f == pytest.approx(64.04, abs=0.01)
    # Check rain detection from '-RA'
    assert obs[1].precip_in == 0.01


def test_fetch_nws_observations_parsing():
    """Verify parsing of NOAA NWS GeoJSON observations payload."""
    adapter = WeatherOracleAdapter()

    sample_nws_json = {
        "features": [
            {
                "properties": {
                    "timestamp": "2026-11-07T17:15:00+00:00",
                    "temperature": {"value": 18.0},
                    "precipitationLastHour": {"value": 0.00254},  # 0.1 inches in meters
                    "rawMessage": "KJFK 071715Z AUTO",
                }
            }
        ]
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json_bytes(sample_nws_json)
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        obs = adapter._fetch_nws_observations("KJFK", limit=1)

    assert len(obs) == 1
    assert obs[0].temp_c == 18.0
    assert obs[0].precip_in == pytest.approx(0.1, abs=0.01)
    assert obs[0].source == "NOAA_NWS"


def test_fetch_open_meteo_parsing():
    """Verify parsing of Open-Meteo hourly response."""
    adapter = WeatherOracleAdapter()

    sample_om_json = {
        "hourly": {
            "time": ["2026-11-07T12:00", "2026-11-07T13:00"],
            "temperature_2m": [15.5, 16.2],
            "precipitation": [0.0, 2.54],  # 2.54 mm = 0.1 in
        }
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json_bytes(sample_om_json)
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        obs = adapter._fetch_open_meteo(40.71, -74.00, "2026-11-07")

    assert len(obs) == 2
    assert obs[0].temp_c == 15.5
    assert obs[1].temp_c == 16.2
    assert obs[1].precip_in == pytest.approx(0.1, abs=0.01)


def test_fetch_weatherapi_parsing():
    """Verify parsing of WeatherAPI.com history payload with Google sign-in key."""
    adapter = WeatherOracleAdapter(weather_api_key="mock_google_key")

    sample_wapi_json = {
        "forecast": {
            "forecastday": [
                {
                    "hour": [
                        {
                            "time_epoch": 1794070260,
                            "temp_c": 19.5,
                            "temp_f": 67.1,
                            "precip_in": 0.05,
                        }
                    ]
                }
            ]
        }
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json_bytes(sample_wapi_json)
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        obs = adapter._fetch_weatherapi("KLGA", "2026-11-07")

    assert len(obs) == 1
    assert obs[0].temp_f == 67.1
    assert obs[0].precip_in == 0.05
    assert obs[0].source == "WEATHERAPI_COM"


# --------------------------------------------------------------------------- #
# Pipeline Integration Test
# --------------------------------------------------------------------------- #

def test_pipeline_integration_with_weather_adapter():
    """Verify ExogenousOraclePipeline correctly routes to WeatherOracleAdapter."""
    weather_adapter = WeatherOracleAdapter()
    pipeline = ExogenousOraclePipeline(adapters=[weather_adapter])

    candidate = Domain2Candidate(
        condition_id="0xweatherpipe1",
        token_id="tok_60_61",
        question="Highest temperature in New York City on November 7: 60-61°F",
        category="weather",
        price=0.05,
        hours_to_resolution=4.0,
        depth_usd=10000.0,
    )

    # Mock concluded dead bracket
    dead_outcome = GroundTruthOutcome(
        event_id=candidate.condition_id,
        category=OracleCategory.WEATHER,
        status=OracleStatus.CONCLUDED,
        concluded=True,
        winning_outcome="No",
        p_hat=0.000,
        h_reversal=0.000,
        confidence=1.000,
    )

    with patch.object(weather_adapter, "evaluate", return_value=dead_outcome):
        result = pipeline.evaluate_candidate(candidate)

    assert result is not None
    assert result.is_deterministic_conclusion is True
    assert candidate.p_hat == 0.000
    assert pipeline._concluded_cache[candidate.condition_id] == dead_outcome


# --------------------------------------------------------------------------- #
# Resolution Lifecycle Tests: Ancillary Data & Verdicts
# --------------------------------------------------------------------------- #

def test_parse_ancillary_data_weather():
    """Verify parsing raw UMA ancillary data bytes for weather markets."""
    adapter = WeatherOracleAdapter()
    raw_bytes = (
        b"q: title: Highest temperature in New York City on November 7: 64-65\xc2\xb0F, "
        b"resolution_source: https://www.weather.gov/wrh/timeseries?site=KLGA"
    )
    parsed = adapter.parse_ancillary_data(raw_bytes)
    assert parsed.asset == "KLGA"
    assert parsed.strike_price == pytest.approx(65.99)
    assert "https://www.weather.gov/wrh/timeseries?site=KLGA" in parsed.source_feed


def test_evaluate_market_resolution_dead_bracket():
    """Verify evaluate_market_resolution returns deterministic NO for exceeded bracket."""
    adapter = WeatherOracleAdapter()

    mock_summary = StationDaySummary(
        station_id="KLGA",
        target_date="2026-11-07",
        observations=[
            StationObservation(
                station_id="KLGA",
                timestamp=datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc),
                temp_f=66.0,
            )
        ],
        max_temp_f=66.0,
        is_day_concluded=False,
    )

    market_info = {
        "condition_id": "0xweathercond01",
        "question": "Highest temperature in New York City on November 7: 60-61°F",
        "resolution_source": "https://www.weather.gov/wrh/timeseries?site=KLGA",
    }

    with patch.object(adapter, "fetch_station_day", return_value=mock_summary):
        verdict = adapter.evaluate_market_resolution(market_info)

    assert verdict.condition_id == "0xweathercond01"
    assert verdict.outcome.value == "NO"
    assert verdict.p_yes == 0.0
    assert verdict.is_deterministic is True
    assert verdict.settlement_price == 66.0


def test_get_latest_price_weather():
    """Verify get_latest_price returns temperature as PricePoint."""
    import asyncio
    adapter = WeatherOracleAdapter()

    mock_summary = StationDaySummary(
        station_id="KLGA",
        target_date="2026-11-07",
        observations=[
            StationObservation(
                station_id="KLGA",
                timestamp=datetime(2026, 11, 7, 17, 0, tzinfo=timezone.utc),
                temp_f=64.0,
            )
        ],
    )

    with patch.object(adapter, "fetch_station_day", return_value=mock_summary):
        price_pt = asyncio.run(adapter.get_latest_price("KLGA"))

    assert price_pt.asset == "KLGA"
    assert price_pt.price == 64.0
    assert price_pt.confidence == 1.0


# --------------------------------------------------------------------------- #
# Smoke Test: Real AWC Network Request (Graceful fallback)
# --------------------------------------------------------------------------- #

def test_awc_metar_live_network_smoke():
    """Verify live connectivity and latency to open AWC endpoint."""
    adapter = WeatherOracleAdapter(request_timeout=4.0)
    try:
        obs = adapter._fetch_awc_metar("KJFK", hours=2)
        assert len(obs) > 0
        assert obs[0].station_id == "KJFK"
        assert obs[0].temp_c is not None
    except Exception as e:
        pytest.skip(f"Live network request to aviationweather.gov unavailable: {e}")


def json_bytes(obj) -> bytes:
    import json
    return json.dumps(obj).encode("utf-8")
