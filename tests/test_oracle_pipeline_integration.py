"""Comprehensive Integration Tests for Track 6: In-Play Exogenous Real-World Oracle Pipeline.

Verifies the end-to-end integration across all 5 cluster adapters (Crypto, Sports, Macro,
Weather, Politics), candidate ground-truth overrides (p_hat=1.000, h_reversal=0.000),
and allocation harvesting in Domain 2.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from polymarketbot.oracle.base import (
    OracleCategory,
    OracleStatus,
    GroundTruthOutcome,
)
from polymarketbot.oracle.pipeline import (
    ExogenousOraclePipeline,
    create_default_oracle_pipeline,
)
from polymarketbot.oracle.crypto import CryptoOracleAdapter
from polymarketbot.oracle.sports import SportsOracleAdapter
from polymarketbot.oracle.macro import MacroOracleAdapter
from polymarketbot.oracle.weather import WeatherOracleAdapter
from polymarketbot.oracle.politics import PoliticsOracleAdapter
from polymarketbot.trading.domain2.ortools_allocator import (
    Domain2Candidate,
    Domain2OrtoolsAllocator,
)


def test_default_pipeline_instantiation():
    """Verify default pipeline creates all 5 cluster adapters."""
    pipeline = create_default_oracle_pipeline()
    assert len(pipeline.adapters) == 5
    categories = {a.category for a in pipeline.adapters}
    assert categories == {
        OracleCategory.CRYPTO,
        OracleCategory.SPORTS,
        OracleCategory.MACRO,
        OracleCategory.WEATHER,
        OracleCategory.POLITICS,
    }


def test_pipeline_routing_across_all_clusters():
    """Verify find_adapter correctly routes representative market questions."""
    pipeline = create_default_oracle_pipeline()

    # 1. Crypto
    adapter = pipeline.find_adapter(
        question="Will the price of Bitcoin be above $76,000 on November 7?",
        resolution_source="https://www.binance.com/en/trade/BTC_USDT",
    )
    assert adapter is not None
    assert adapter.category == OracleCategory.CRYPTO

    # 2. Sports
    adapter = pipeline.find_adapter(
        question="NBA: Boston Celtics vs Los Angeles Lakers",
        resolution_source="https://www.espn.com/nba",
    )
    assert adapter is not None
    assert adapter.category == OracleCategory.SPORTS

    # 3. Macro
    adapter = pipeline.find_adapter(
        question="Will the Fed increase interest rates by 25 bps after the December 2026 meeting?",
        resolution_source="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    )
    assert adapter is not None
    assert adapter.category == OracleCategory.MACRO

    # 4. Weather
    adapter = pipeline.find_adapter(
        question="Will the highest temperature in Toronto be 13°C on November 6?",
        resolution_source="https://aviationweather.gov/api/data/metar?ids=CYYZ&format=raw",
    )
    assert adapter is not None
    assert adapter.category == OracleCategory.WEATHER

    # 5. Politics
    adapter = pipeline.find_adapter(
        question="Will Green Party win the third-most seats in the 2026 New Zealand election?",
        resolution_source="https://elections.nz/",
    )
    assert adapter is not None
    assert adapter.category == OracleCategory.POLITICS


def test_candidate_ground_truth_override():
    """Verify that applying ground truth updates p_hat and h_reversal."""
    candidate = Domain2Candidate(
        condition_id="0xabc123",
        token_id="tok_yes",
        question="NBA: Denver Nuggets vs Miami Heat - Winner",
        category="sports",
        price=0.92,  # 8¢ discount to physical truth
        hours_to_resolution=0.5,  # 30 mins before UMA proposal
        depth_usd=5000.0,
    )

    outcome = GroundTruthOutcome(
        event_id="espn:nba:401",
        category=OracleCategory.SPORTS,
        status=OracleStatus.CONCLUDED,
        concluded=True,
        winning_outcome="Denver Nuggets",
        p_hat=1.000,
        h_reversal=0.000,
        confidence=1.000,
        resolution_source_url="https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    )

    assert candidate.p_hat is None
    candidate.apply_ground_truth(outcome)

    assert candidate.p_hat == 1.000
    assert candidate.h_reversal == 0.000
    assert candidate.is_concluded is True
    assert candidate.estimated_win_prob == 1.000


def test_ortools_harvests_conclusion_gap_discount():
    """Verify OR-Tools allocates aggressively to candidate with verified ground truth."""
    allocator = Domain2OrtoolsAllocator(
        reserve_pct=0.10,
        max_drawdown_target=0.15,
        max_single_bet_pct=0.10,
        max_active_markets=5,
    )

    # Candidate A: Uncertain market (price=0.92, p_hat=None, positive hazard)
    cand_uncertain = Domain2Candidate(
        condition_id="0xuncertain",
        token_id="tok_a",
        question="Will Bitcoin reach $150k by 2027?",
        category="crypto",
        price=0.92,
        hours_to_resolution=24.0,
        depth_usd=10000.0,
    )

    # Candidate B: Concluded event in In-Play Gap (game ended, price=0.92, p_hat=1.0, h_reversal=0.0)
    cand_concluded = Domain2Candidate(
        condition_id="0xconcluded",
        token_id="tok_b",
        question="NBA: Boston Celtics vs Miami Heat",
        category="sports",
        price=0.92,
        hours_to_resolution=0.5,
        depth_usd=10000.0,
        p_hat=1.000,
        h_reversal=0.000,
        is_concluded=True,
        is_uma_proposed=True,
    )

    result = allocator.optimize_allocation([cand_uncertain, cand_concluded], bankroll_usd=10000.0)
    assert result.solver_status in ("OPTIMAL", "FEASIBLE")

    # Find allocation for concluded candidate
    concluded_alloc = next((a for a in result.allocations if a.condition_id == "0xconcluded"), None)
    assert concluded_alloc is not None
    # With zero hazard and p_win=1.0, it should be allocated up to max single bet or gamma * depth bound
    assert concluded_alloc.allocated_usd >= 900.0
    assert concluded_alloc.hazard_pct == 0.0
    assert concluded_alloc.net_edge_pct >= 0.07  # >= 7% edge on capital
