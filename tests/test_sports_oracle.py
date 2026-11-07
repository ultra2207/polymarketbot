"""Unit tests for Sports In-Play & Final Outcome Oracle Adapter."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from polymarketbot.oracle.base import (
    GroundTruthOutcome,
    OracleCategory,
    OracleStatus,
    ResolutionOutcome,
)
from polymarketbot.oracle.pipeline import ExogenousOraclePipeline
from polymarketbot.oracle.sports import (
    BallDontLieClient,
    NormalizedMatchEvent,
    PandaScoreClient,
    SportsOracleAdapter,
    TheSportsDBClient,
    match_competitor,
    normalize_name,
)
from polymarketbot.trading.domain2.ortools_allocator import Domain2Candidate


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def sports_adapter() -> SportsOracleAdapter:
    """Instantiate adapter with offline mode by default for deterministic tests."""
    return SportsOracleAdapter(use_live_network=False, cache_ttl_seconds=60)


# --------------------------------------------------------------------------- #
# Tests: can_handle
# --------------------------------------------------------------------------- #

def test_can_handle_sports_questions(sports_adapter: SportsOracleAdapter) -> None:
    assert sports_adapter.can_handle("NBA: Boston Celtics vs Los Angeles Lakers")
    assert sports_adapter.can_handle("Will the Kansas City Chiefs defeat the Buffalo Bills?")
    assert sports_adapter.can_handle("Premier League: Arsenal vs Chelsea - Match Winner")
    assert sports_adapter.can_handle("UFC 309: Jon Jones vs. Stipe Miocic Winner")
    assert sports_adapter.can_handle("ATP Paris Masters: Sinner vs Alcaraz")
    assert sports_adapter.can_handle("CS2: Natus Vincere vs FaZe Clan")
    assert sports_adapter.can_handle("Will Liverpool win against Manchester City?")


def test_can_handle_resolution_source_hints(sports_adapter: SportsOracleAdapter) -> None:
    assert sports_adapter.can_handle("Random Title", resolution_source="https://site.api.espn.com/...")
    assert sports_adapter.can_handle("Random Title", resolution_source="https://www.nba.com/game/...")
    assert sports_adapter.can_handle("Random Title", resolution_source="https://ufc.com/event/...")
    assert sports_adapter.can_handle("Random Title", resolution_source="thesportsdb")
    assert sports_adapter.can_handle("Random Title", resolution_source="balldontlie.io")


def test_can_handle_category_hints(sports_adapter: SportsOracleAdapter) -> None:
    assert sports_adapter.can_handle("Unknown Title", category_hint="sports")
    assert sports_adapter.can_handle("Unknown Title", category_hint="sports:nba")


def test_cannot_handle_non_sports(sports_adapter: SportsOracleAdapter) -> None:
    assert not sports_adapter.can_handle("Will Bitcoin price exceed $100,000 by December 31?")
    assert not sports_adapter.can_handle("Will Donald Trump win the 2024 Presidential Election?")
    assert not sports_adapter.can_handle("Will NYC temperature exceed 85 degrees Fahrenheit?")


# --------------------------------------------------------------------------- #
# Tests: Normalization & Matching
# --------------------------------------------------------------------------- #

def test_team_normalization_and_aliases() -> None:
    assert normalize_name("Celtics") == "boston celtics"
    assert normalize_name("Lakers") == "los angeles lakers"
    assert normalize_name("Chiefs") == "kansas city chiefs"
    assert normalize_name("Man City") == "manchester city"
    assert normalize_name("Spurs") == "tottenham hotspur"
    assert normalize_name("76ers") == "philadelphia 76ers"


def test_competitor_matching() -> None:
    assert match_competitor("Celtics", "Boston Celtics")
    assert match_competitor("Boston Celtics", "Celtics")
    assert match_competitor("LAL", "Los Angeles Lakers") or match_competitor("Lakers", "Los Angeles Lakers")
    assert match_competitor("Jon Jones", "Jon Jones")
    assert match_competitor("Novak Djokovic", "Djokovic")


# --------------------------------------------------------------------------- #
# Tests: Market Question Parsing
# --------------------------------------------------------------------------- #

def test_parse_market_questions(sports_adapter: SportsOracleAdapter) -> None:
    # Pattern 1
    t, a, b, l = sports_adapter.parse_market_question("Will the Boston Celtics defeat the Los Angeles Lakers?")
    assert "Celtics" in a
    assert "Lakers" in b

    # Pattern 2
    t2, a2, b2, l2 = sports_adapter.parse_market_question("Will the Kansas City Chiefs win against the Buffalo Bills?")
    assert "Chiefs" in a2
    assert "Bills" in b2

    # Pattern 3
    t3, a3, b3, l3 = sports_adapter.parse_market_question("NBA: Detroit Pistons vs. Charlotte Hornets - Match Winner")
    assert "Pistons" in a3
    assert "Hornets" in b3
    assert l3 == "nba"


# --------------------------------------------------------------------------- #
# Tests: ESPN Payload Parsing
# --------------------------------------------------------------------------- #

def test_parse_espn_team_sports_data(sports_adapter: SportsOracleAdapter) -> None:
    espn_sample = {
        "events": [
            {
                "id": "401584800",
                "status": {
                    "period": 4,
                    "type": {
                        "name": "STATUS_FINAL",
                        "completed": True,
                        "state": "post",
                        "detail": "Final",
                    },
                },
                "competitions": [
                    {
                        "competitors": [
                            {
                                "team": {"displayName": "Boston Celtics"},
                                "score": "115",
                                "winner": True,
                            },
                            {
                                "team": {"displayName": "Los Angeles Lakers"},
                                "score": "108",
                                "winner": False,
                            },
                        ],
                        "status": {
                            "period": 4,
                            "type": {
                                "name": "STATUS_FINAL",
                                "completed": True,
                                "state": "post",
                                "detail": "Final",
                            },
                        },
                    }
                ],
            }
        ]
    }

    events = sports_adapter._parse_espn_data("nba", espn_sample)
    assert len(events) == 1
    ev = events[0]
    assert ev.competitor_a == "Boston Celtics"
    assert ev.competitor_b == "Los Angeles Lakers"
    assert ev.score_a == 115.0
    assert ev.score_b == 108.0
    assert ev.status == OracleStatus.CONCLUDED
    assert ev.completed is True
    assert ev.winning_competitor == "Boston Celtics"


def test_parse_espn_mma_data(sports_adapter: SportsOracleAdapter) -> None:
    espn_mma = {
        "events": [
            {
                "id": "600001",
                "competitions": [
                    {
                        "id": "600001_1",
                        "status": {
                            "type": {
                                "name": "STATUS_FINAL",
                                "completed": True,
                                "state": "post",
                            }
                        },
                        "competitors": [
                            {
                                "athlete": {"displayName": "Jon Jones"},
                                "winner": True,
                            },
                            {
                                "athlete": {"displayName": "Stipe Miocic"},
                                "winner": False,
                            },
                        ],
                    }
                ],
            }
        ]
    }

    events = sports_adapter._parse_espn_data("ufc", espn_mma)
    assert len(events) == 1
    ev = events[0]
    assert ev.competitor_a == "Jon Jones"
    assert ev.competitor_b == "Stipe Miocic"
    assert ev.status == OracleStatus.CONCLUDED
    assert ev.completed is True
    assert ev.winning_competitor == "Jon Jones"


# --------------------------------------------------------------------------- #
# Tests: Candidate Evaluation & Deterministic Conclusion
# --------------------------------------------------------------------------- #

def test_evaluate_candidate_concluded_win(sports_adapter: SportsOracleAdapter) -> None:
    # Inject final Celtics win
    sports_adapter.inject_mock_event(
        NormalizedMatchEvent(
            event_id="101",
            sport_league="nba",
            competitor_a="Boston Celtics",
            competitor_b="Los Angeles Lakers",
            score_a=115.0,
            score_b=108.0,
            status=OracleStatus.CONCLUDED,
            status_detail="STATUS_FINAL",
            completed=True,
            winning_competitor="Boston Celtics",
        )
    )

    candidate = Domain2Candidate(
        condition_id="0xabc123",
        token_id="1",
        question="Will the Boston Celtics defeat the Los Angeles Lakers?",
        category="sports",
        price=0.85,
        hours_to_resolution=0.5,
        depth_usd=5000.0,
    )

    outcome = sports_adapter.evaluate(candidate)
    assert outcome is not None
    assert outcome.concluded is True
    assert outcome.status == OracleStatus.CONCLUDED
    assert outcome.winning_outcome == "Boston Celtics"
    assert outcome.p_hat == 1.000
    assert outcome.h_reversal == 0.000
    assert outcome.confidence == 1.000
    assert outcome.is_deterministic_conclusion is True


def test_evaluate_candidate_concluded_loss(sports_adapter: SportsOracleAdapter) -> None:
    # Inject final Lakers win (Celtics lost)
    sports_adapter.inject_mock_event(
        NormalizedMatchEvent(
            event_id="102",
            sport_league="nba",
            competitor_a="Boston Celtics",
            competitor_b="Los Angeles Lakers",
            score_a=102.0,
            score_b=110.0,
            status=OracleStatus.CONCLUDED,
            status_detail="STATUS_FINAL",
            completed=True,
            winning_competitor="Los Angeles Lakers",
        )
    )

    candidate = Domain2Candidate(
        condition_id="0xdef456",
        token_id="1",
        question="Will the Boston Celtics defeat the Los Angeles Lakers?",
        category="sports",
        price=0.20,
        hours_to_resolution=0.5,
        depth_usd=5000.0,
    )

    outcome = sports_adapter.evaluate(candidate)
    assert outcome is not None
    assert outcome.concluded is True
    assert outcome.status == OracleStatus.CONCLUDED
    assert outcome.winning_outcome == "Los Angeles Lakers"
    # Target (Celtics) lost -> p_hat is 0.0
    assert outcome.p_hat == 0.000
    assert outcome.h_reversal == 0.000


def test_evaluate_candidate_in_play(sports_adapter: SportsOracleAdapter) -> None:
    # Inject in-play 3rd quarter event
    sports_adapter.inject_mock_event(
        NormalizedMatchEvent(
            event_id="103",
            sport_league="nba",
            competitor_a="Kansas City Chiefs",
            competitor_b="Buffalo Bills",
            score_a=24.0,
            score_b=14.0,
            status=OracleStatus.IN_PLAY,
            status_detail="STATUS_IN_PROGRESS",
            completed=False,
            clock_detail="Q3 04:12",
        )
    )

    candidate = Domain2Candidate(
        condition_id="0x789",
        token_id="1",
        question="Will the Kansas City Chiefs defeat the Buffalo Bills?",
        category="sports",
        price=0.60,
        hours_to_resolution=1.0,
        depth_usd=3000.0,
    )

    outcome = sports_adapter.evaluate(candidate)
    assert outcome is not None
    assert outcome.status == OracleStatus.IN_PLAY
    assert outcome.concluded is False
    assert outcome.p_hat > 0.50  # Leading by 10 points
    assert outcome.h_reversal > 0.00  # Non-zero hazard in-play
    assert outcome.is_deterministic_conclusion is False


# --------------------------------------------------------------------------- #
# Tests: Ancillary Data & Market Resolution Verdicts
# --------------------------------------------------------------------------- #

def test_parse_ancillary_data(sports_adapter: SportsOracleAdapter) -> None:
    raw_ancillary = 'q: title: "NBA: Boston Celtics vs. Los Angeles Lakers", description: "Official source: NBA.com / ESPN", res_data: p1: 0, p2: 1, p3: 0.5'
    parsed = sports_adapter.parse_ancillary_data(raw_ancillary)
    assert "Boston Celtics" in parsed.title
    assert parsed.source_feed == "espn" or parsed.source_feed == "nba.com"
    assert parsed.early_resolution_allowed is True


def test_evaluate_market_resolution_verdict(sports_adapter: SportsOracleAdapter) -> None:
    sports_adapter.inject_mock_event(
        NormalizedMatchEvent(
            event_id="201",
            sport_league="epl",
            competitor_a="Arsenal",
            competitor_b="Chelsea",
            score_a=2.0,
            score_b=1.0,
            status=OracleStatus.CONCLUDED,
            status_detail="STATUS_FULL_TIME",
            completed=True,
            winning_competitor="Arsenal",
        )
    )

    market_info = {
        "condition_id": "0xarsenal_win",
        "question": "Will Arsenal defeat Chelsea?",
        "category": "sports",
    }
    verdict = sports_adapter.evaluate_market_resolution(market_info)
    assert verdict.outcome == ResolutionOutcome.YES
    assert verdict.p_yes == 1.000
    assert verdict.is_deterministic is True


# --------------------------------------------------------------------------- #
# Tests: Integration with ExogenousOraclePipeline
# --------------------------------------------------------------------------- #

def test_pipeline_integration_harvests_conclusion_gap(sports_adapter: SportsOracleAdapter) -> None:
    sports_adapter.inject_mock_event(
        NormalizedMatchEvent(
            event_id="301",
            sport_league="nba",
            competitor_a="Boston Celtics",
            competitor_b="Los Angeles Lakers",
            score_a=115.0,
            score_b=108.0,
            status=OracleStatus.CONCLUDED,
            status_detail="STATUS_FINAL",
            completed=True,
            winning_competitor="Boston Celtics",
        )
    )

    pipeline = ExogenousOraclePipeline(adapters=[sports_adapter])

    candidate = Domain2Candidate(
        condition_id="0xgap_harvest",
        token_id="1",
        question="Will the Boston Celtics defeat the Los Angeles Lakers?",
        category="sports",
        price=0.91,  # Discounted at 91c despite game being final!
        hours_to_resolution=0.8,
        depth_usd=10000.0,
    )

    outcome = pipeline.evaluate_candidate(candidate)
    assert outcome is not None
    assert outcome.is_deterministic_conclusion is True
    # In-place p_hat override applied to candidate
    assert candidate.p_hat == 1.000


# --------------------------------------------------------------------------- #
# Tests: Secondary Clients Configuration & Resilience
# --------------------------------------------------------------------------- #

def test_balldontlie_client_unconfigured() -> None:
    client = BallDontLieClient(api_key=None)
    assert not client.is_configured
    assert client.get_games("2024-11-08") == []


def test_thesportsdb_client_default() -> None:
    client = TheSportsDBClient()
    assert client.api_key is not None


def test_pandascore_client_unconfigured() -> None:
    client = PandaScoreClient(api_key=None)
    assert not client.is_configured
    assert client.get_running_matches("cs-go") == []


def test_overtime_and_shootout_parsing(sports_adapter: SportsOracleAdapter) -> None:
    # Overtime game
    espn_ot = {
        "events": [
            {
                "id": "500001",
                "status": {
                    "period": 5,
                    "type": {
                        "name": "STATUS_FINAL_OT",
                        "completed": True,
                        "state": "post",
                        "detail": "Final/OT",
                    },
                },
                "competitions": [
                    {
                        "competitors": [
                            {"team": {"displayName": "Miami Heat"}, "score": "120", "winner": True},
                            {"team": {"displayName": "Chicago Bulls"}, "score": "118", "winner": False},
                        ],
                        "status": {
                            "type": {"name": "STATUS_FINAL_OT", "completed": True, "state": "post"}
                        },
                    }
                ],
            }
        ]
    }
    events = sports_adapter._parse_espn_data("nba", espn_ot)
    assert len(events) == 1
    assert events[0].completed is True
    assert events[0].status == OracleStatus.CONCLUDED
    assert events[0].winning_competitor == "Miami Heat"


def test_soccer_draw_handling(sports_adapter: SportsOracleAdapter) -> None:
    sports_adapter.inject_mock_event(
        NormalizedMatchEvent(
            event_id="500002",
            sport_league="epl",
            competitor_a="Liverpool",
            competitor_b="Manchester City",
            score_a=1.0,
            score_b=1.0,
            status=OracleStatus.CONCLUDED,
            status_detail="STATUS_FULL_TIME",
            completed=True,
            winning_competitor="DRAW",
        )
    )

    candidate = Domain2Candidate(
        condition_id="0xdraw",
        token_id="1",
        question="Will Liverpool defeat Manchester City?",
        category="sports",
        price=0.50,
        hours_to_resolution=0.1,
        depth_usd=2000.0,
    )

    outcome = sports_adapter.evaluate(candidate)
    assert outcome is not None
    assert outcome.concluded is True
    assert outcome.winning_outcome == "DRAW"
    assert outcome.p_hat == 0.500


def test_tennis_groupings_parsing(sports_adapter: SportsOracleAdapter) -> None:
    espn_tennis = {
        "events": [
            {
                "id": "700001",
                "name": "Rolex Paris Masters",
                "groupings": [
                    {
                        "grouping": {"displayName": "Men's Singles"},
                        "competitions": [
                            {
                                "id": "700001_m1",
                                "status": {
                                    "period": 3,
                                    "type": {
                                        "name": "STATUS_FINAL",
                                        "completed": True,
                                        "state": "post",
                                        "detail": "Final",
                                    },
                                },
                                "competitors": [
                                    {"athlete": {"displayName": "Jannik Sinner"}, "winner": True},
                                    {"athlete": {"displayName": "Carlos Alcaraz"}, "winner": False},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    events = sports_adapter._parse_espn_data("atp", espn_tennis)
    assert len(events) == 1
    assert events[0].competitor_a == "Jannik Sinner"
    assert events[0].competitor_b == "Carlos Alcaraz"
    assert events[0].winning_competitor == "Jannik Sinner"
    assert events[0].status == OracleStatus.CONCLUDED


def test_postponed_game_disputed(sports_adapter: SportsOracleAdapter) -> None:
    sports_adapter.inject_mock_event(
        NormalizedMatchEvent(
            event_id="800001",
            sport_league="nfl",
            competitor_a="Buffalo Bills",
            competitor_b="Cincinnati Bengals",
            status=OracleStatus.DISPUTED,
            status_detail="STATUS_POSTPONED",
            completed=False,
        )
    )

    candidate = Domain2Candidate(
        condition_id="0xpostponed",
        token_id="1",
        question="Will the Buffalo Bills defeat the Cincinnati Bengals?",
        category="sports",
        price=0.50,
        hours_to_resolution=0.1,
        depth_usd=1000.0,
    )

    outcome = sports_adapter.evaluate(candidate)
    assert outcome is not None
    assert outcome.status == OracleStatus.DISPUTED
    assert outcome.h_reversal == 1.000

