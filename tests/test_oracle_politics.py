"""Unit tests for Cluster 5: Politics, Elections & Geopolitics Oracle Adapter.

Validates:
1. Market pattern matching and question parsing across US Senate, House, Presidential,
   Congressional control, appointments, and international elections.
2. UMA ancillary data parsing (AP, Fox, NBC, DDHQ, FEC.gov, state certification).
3. Associated Press wire and headline regex extraction.
4. Deterministic evaluation of Domain2Candidate (overriding p_hat=1.000, h_reversal=0.000).
5. Kalshi cross-platform political parity settlement.
6. Graceful API key handling and fallback behavior.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
import pytest

from polymarketbot.oracle.base import (
    GroundTruthOutcome,
    OracleCategory,
    OracleStatus,
    ResolutionOutcome,
)
from polymarketbot.oracle.politics import (
    APNewsFeedClient,
    FecApiClient,
    GoogleCivicApiClient,
    KalshiParityClient,
    PoliticalRaceType,
    PoliticsOracleAdapter,
    RaceCallStatus,
    VerifiedRaceCall,
)
from polymarketbot.trading.domain2.ortools_allocator import Domain2Candidate


@pytest.fixture
def politics_adapter() -> PoliticsOracleAdapter:
    """Provides a fresh PoliticsOracleAdapter instance."""
    return PoliticsOracleAdapter()


def test_can_handle_political_markets(politics_adapter: PoliticsOracleAdapter) -> None:
    """Verify adapter correctly identifies political questions and resolution sources."""
    # Positive cases
    assert politics_adapter.can_handle("Will Republicans win the Idaho Senate race in 2026?")
    assert politics_adapter.can_handle("Will the Republican Party win the TX-24 House seat?")
    assert politics_adapter.can_handle("Republican Party wins control of the Senate")
    assert politics_adapter.can_handle("Will Donald Trump win the 2024 Presidential Election?")
    assert politics_adapter.can_handle("Will nominee be confirmed as Secretary of Defense by the Senate?")
    assert politics_adapter.can_handle("Will Labour win the New Zealand General Election?", resolution_source="elections.nz")
    assert politics_adapter.can_handle("Random question", resolution_source="https://apnews.com/hub/ap-race-calls")
    assert politics_adapter.can_handle("Random question", category_hint="politics")

    # Negative cases
    assert not politics_adapter.can_handle("Will Bitcoin hit $100k by December?")
    assert not politics_adapter.can_handle("Los Angeles Kings vs San Jose Sharks")
    assert not politics_adapter.can_handle("Will NYC temperature exceed 85F on July 4?")


def test_parse_political_market_spec(politics_adapter: PoliticsOracleAdapter) -> None:
    """Test extracting structured metadata from real-world Polymarket ancillary strings."""
    # Case 1: Idaho Senate Race with 3-source consensus rule
    anc_idaho = (
        "q: title: Will the Republicans win the Idaho Senate race in 2026?, description: This market "
        "will resolve according to the winner of the 2026 midterm Idaho U.S. Senate election... "
        "The resolution source for this market is the Associated Press, Fox News, and NBC. "
        "This market will resolve once all three sources call the race for the same candidate. "
        "If all three sources haven’t called the race... official certification."
    )
    parsed = politics_adapter.parse_political_market(
        question="Will the Republicans win the Idaho Senate race in 2026?",
        ancillary_text=anc_idaho,
    )
    assert parsed.race_type == PoliticalRaceType.SENATE
    assert parsed.state_or_district == "ID"
    assert parsed.target_party == "Republican"
    assert "Associated Press" in parsed.resolution_sources
    assert "Fox News" in parsed.resolution_sources
    assert "NBC" in parsed.resolution_sources
    assert parsed.consensus_required is True
    assert parsed.certification_fallback is True

    # Case 2: TX-24 House seat with FEC fallback
    anc_tx = (
        "q: title: Will the Republican Party win the TX-24 House seat?, description: ... "
        "This market will resolve based solely on the official results as reported by the "
        "United States government, specifically the Federal Election Commission (https://www.fec.gov/)."
    )
    parsed_tx = politics_adapter.parse_political_market(
        question="Will the Republican Party win the TX-24 House seat?",
        ancillary_text=anc_tx,
    )
    assert parsed_tx.race_type == PoliticalRaceType.HOUSE
    assert parsed_tx.state_or_district == "TX-24"
    assert parsed_tx.target_party == "Republican"
    assert "FEC" in parsed_tx.resolution_sources


def test_ap_headline_parsing() -> None:
    """Test AP news wire headline parsing into structured VerifiedRaceCall."""
    client = APNewsFeedClient()

    # Pattern 1: AP Race Call: Trump wins Pennsylvania
    h1 = "AP Race Call: Trump wins Pennsylvania presidential race"
    call1 = client.parse_ap_headline(h1, url="https://apnews.com/article/123")
    assert call1 is not None
    assert "Trump" in call1.winner_name
    assert call1.state_or_country.lower() == "pennsylvania"
    assert call1.status == RaceCallStatus.CALLED
    assert call1.confidence == 1.000

    # Pattern 2: AP calls Senate race for candidate
    h2 = "AP calls Pennsylvania Senate race for McCormick"
    call2 = client.parse_ap_headline(h2)
    assert call2 is not None
    assert "McCormick" in call2.winner_name
    assert call2.race_type == PoliticalRaceType.SENATE

    # Pattern 3: Chamber control call
    h3 = "Republicans win control of the Senate, AP projects"
    call3 = client.parse_ap_headline(h3)
    assert call3 is not None
    assert call3.race_type == PoliticalRaceType.CONGRESSIONAL_CONTROL
    assert call3.winner_party == "Republican"
    assert call3.is_conclusive is True


def test_domain2_candidate_evaluation_with_gap_detection(politics_adapter: PoliticsOracleAdapter) -> None:
    """Test evaluating a Domain2Candidate and capturing the conclusion gap."""
    # 1. Inject a verified AP call for Idaho Senate Race
    call = VerifiedRaceCall(
        race_id="US-ID-SENATE-2026",
        race_type=PoliticalRaceType.SENATE,
        office="Senate",
        state_or_country="ID",
        winner_name="Mike Crapo",
        winner_party="Republican",
        source="Associated Press",
        status=RaceCallStatus.CALLED,
        confidence=1.000,
        source_url="https://apnews.com/race-call/idaho-senate-2026",
    )
    politics_adapter.ingest_race_call(call)

    # 2. Candidate: "Will the Republicans win the Idaho Senate race in 2026?"
    # Currently trading at 0.94 on Polymarket before UMA proposal
    cand_yes = Domain2Candidate(
        condition_id="0xabc123idaho",
        token_id="token_yes",
        question="Will the Republicans win the Idaho Senate race in 2026?",
        category="politics",
        price=0.94,
        hours_to_resolution=24.0,
        depth_usd=50000.0,
        p_hat=None,
        is_uma_proposed=False,
    )

    outcome = politics_adapter.evaluate(cand_yes)
    assert outcome is not None
    assert outcome.concluded is True
    assert outcome.status == OracleStatus.CONCLUDED
    assert outcome.is_deterministic_conclusion is True
    assert outcome.p_hat == 1.000
    assert outcome.h_reversal == 0.000
    assert outcome.winning_outcome == "Yes"
    # Verify candidate.p_hat was directly updated in-place
    assert cand_yes.p_hat == 1.000

    # 3. Opposing Token: "Democratic Party" or "Will Democrats win Idaho Senate?"
    cand_no = Domain2Candidate(
        condition_id="0xabc123idaho_dem",
        token_id="token_no",
        question="Will the Democrats win the Idaho Senate race in 2026?",
        category="politics",
        price=0.06,
        hours_to_resolution=24.0,
        depth_usd=10000.0,
        p_hat=None,
        is_uma_proposed=False,
    )
    outcome_no = politics_adapter.evaluate(cand_no)
    assert outcome_no is not None
    assert outcome_no.p_hat == 0.000
    assert outcome_no.winning_outcome == "No"
    assert cand_no.p_hat == 0.000


def test_congressional_control_evaluation(politics_adapter: PoliticsOracleAdapter) -> None:
    """Test evaluation of chamber control markets (e.g. Senate majority)."""
    # Register AP Senate control call for Republicans
    control_call = VerifiedRaceCall(
        race_id="US-SENATE-CONTROL",
        race_type=PoliticalRaceType.CONGRESSIONAL_CONTROL,
        office="Control of the Senate",
        state_or_country="US",
        winner_name="Republican Party",
        winner_party="Republican",
        source="Associated Press",
        status=RaceCallStatus.CALLED,
        confidence=1.000,
    )
    politics_adapter.ingest_race_call(control_call)

    # Candidate for Republican Party token
    cand_gop = Domain2Candidate(
        condition_id="0xsenate_gop",
        token_id="gop_tok",
        question="Republican Party wins control of the Senate",
        category="politics",
        price=0.96,
        hours_to_resolution=48.0,
        depth_usd=100000.0,
    )
    outcome_gop = politics_adapter.evaluate(cand_gop)
    assert outcome_gop is not None
    assert outcome_gop.p_hat == 1.000
    assert outcome_gop.is_deterministic_conclusion is True

    # Candidate for Democratic Party token
    cand_dem = Domain2Candidate(
        condition_id="0xsenate_dem",
        token_id="dem_tok",
        question="Democratic Party wins control of the Senate",
        category="politics",
        price=0.04,
        hours_to_resolution=48.0,
        depth_usd=100000.0,
    )
    outcome_dem = politics_adapter.evaluate(cand_dem)
    assert outcome_dem is not None
    assert outcome_dem.p_hat == 0.000


def test_evaluate_market_resolution_verdict(politics_adapter: PoliticsOracleAdapter) -> None:
    """Test evaluate_market_resolution returns ResolutionVerdict."""
    # Inject call
    politics_adapter.ingest_race_call(
        VerifiedRaceCall(
            race_id="US-TX-24-HOUSE",
            race_type=PoliticalRaceType.HOUSE,
            office="House",
            state_or_country="TX-24",
            winner_name="Beth Van Duyne",
            winner_party="Republican",
            source="Decision Desk HQ",
            status=RaceCallStatus.CALLED,
            confidence=1.000,
        )
    )

    market_info = {
        "condition_id": "0xtx24house",
        "question": "Will the Republican Party win the TX-24 House seat?",
        "ancillary_text": "Resolution source: Decision Desk HQ or FEC",
    }
    verdict = politics_adapter.evaluate_market_resolution(market_info)
    assert verdict.outcome == ResolutionOutcome.YES
    assert verdict.p_yes == 1.000
    assert verdict.is_deterministic is True
    assert "Decision Desk HQ" in verdict.rationale


def test_kalshi_parity_client_evaluation() -> None:
    """Test Kalshi parity client handling mock closed/settled contracts."""
    client = KalshiParityClient()

    # Mock query_market_status returning settled market
    client.query_market_status = lambda ticker: {
        "ticker": ticker,
        "status": "settled",
        "result": "yes",
        "yes_sub_title": "Donald Trump",
        "subtitle": "Republican",
    }

    call = client.evaluate_parity_call(
        ticker="PRES-2024-TRUMP",
        race_type=PoliticalRaceType.PRESIDENTIAL,
        office="President",
        state="US",
    )
    assert call is not None
    assert call.winner_name == "Donald Trump"
    assert call.source == "Kalshi Parity Oracle"
    assert call.is_conclusive is True


def test_fec_and_google_civic_api_key_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test FEC and Google Civic clients initialize safely with environment variables or fallbacks."""
    monkeypatch.setenv("DATA_GOV_API_KEY", "test_data_gov_key_123")
    monkeypatch.setenv("GOOGLE_CIVIC_API_KEY", "test_google_civic_key_456")

    fec = FecApiClient()
    assert fec.api_key == "test_data_gov_key_123"

    civic = GoogleCivicApiClient()
    assert civic.api_key == "test_google_civic_key_456"

    # Test default fallback when env is cleared
    monkeypatch.delenv("DATA_GOV_API_KEY", raising=False)
    monkeypatch.delenv("FEC_API_KEY", raising=False)
    fec_default = FecApiClient()
    assert fec_default.api_key == "DEMO_KEY"


def test_exogenous_oracle_pipeline_integration() -> None:
    """Test full integration with ExogenousOraclePipeline."""
    from polymarketbot.oracle.pipeline import ExogenousOraclePipeline

    adapter = PoliticsOracleAdapter()
    adapter.ingest_race_call(
        VerifiedRaceCall(
            race_id="US-PRES-2024",
            race_type=PoliticalRaceType.PRESIDENTIAL,
            office="President",
            state_or_country="US",
            winner_name="Donald Trump",
            winner_party="Republican",
            source="Associated Press",
            status=RaceCallStatus.CALLED,
            confidence=1.000,
        )
    )

    pipeline = ExogenousOraclePipeline([adapter])

    candidate = Domain2Candidate(
        condition_id="0xpres2024trump",
        token_id="tok_trump",
        question="Will Donald Trump win the 2024 Presidential Election?",
        category="politics",
        price=0.92,
        hours_to_resolution=12.0,
        depth_usd=250000.0,
    )

    outcome = pipeline.evaluate_candidate(candidate)
    assert outcome is not None
    assert outcome.is_deterministic_conclusion is True
    assert outcome.p_hat == 1.000
    assert candidate.p_hat == 1.000

