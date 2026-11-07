"""Unit tests for Cluster 3 Macroeconomic & Government Releases Oracle Adapter."""

from __future__ import annotations

import json
from datetime import datetime, timezone
import pytest

from polymarketbot.oracle.base import (
    OracleCategory,
    OracleFeedSource,
    OracleStatus,
    ResolutionOutcome,
)
from polymarketbot.oracle.macro import (
    BLS_SERIES_MAP,
    MacroIndicator,
    MacroOracleAdapter,
    MacroReleaseData,
    ParsedMacroMarket,
)
from polymarketbot.oracle.pipeline import ExogenousOraclePipeline
from polymarketbot.trading.domain2.ortools_allocator import Domain2Candidate


@pytest.fixture
def macro_adapter() -> MacroOracleAdapter:
    """Fixture providing a deterministic, offline MacroOracleAdapter."""
    return MacroOracleAdapter(enable_network=False)


# --------------------------------------------------------------------------- #
# Question Matching Tests
# --------------------------------------------------------------------------- #

def test_can_handle_cpi_questions(macro_adapter: MacroOracleAdapter):
    assert macro_adapter.can_handle("US CPI: October 2026")
    assert macro_adapter.can_handle("Will Core CPI YoY be >= 2.6% in October 2026?")
    assert macro_adapter.can_handle("October Inflation UK - Annual", resolution_source="https://www.bls.gov/cpi/")
    assert macro_adapter.can_handle("Arbitrary question", resolution_source="https://www.bls.gov/bls/news-release/cpi.htm")


def test_can_handle_fomc_questions(macro_adapter: MacroOracleAdapter):
    assert macro_adapter.can_handle("Fed Decision in December?")
    assert macro_adapter.can_handle("How many Fed rate cuts in 2026?")
    assert macro_adapter.can_handle("FOMC meeting rate decision: 25 bps cut")
    assert macro_adapter.can_handle("Rate decision", resolution_source="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")


def test_can_handle_employment_questions(macro_adapter: MacroOracleAdapter):
    assert macro_adapter.can_handle("How many jobs added in October?")
    assert macro_adapter.can_handle("US Non-Farm Payrolls: October 2026")
    assert macro_adapter.can_handle("US Unemployment Rate: October 2026")
    assert macro_adapter.can_handle("Initial Jobless Claims: Week of Nov 6")


def test_can_handle_treasury_questions(macro_adapter: MacroOracleAdapter):
    assert macro_adapter.can_handle("How high will 2-year Treasury yield go before 2027?")
    assert macro_adapter.can_handle("10-Year Treasury Yield above 4.5% on Dec 31?")
    assert macro_adapter.can_handle("Treasury rates", resolution_source="https://home.treasury.gov/resource-center/data-chart-center/interest-rates/")


def test_can_handle_rejects_unrelated(macro_adapter: MacroOracleAdapter):
    assert not macro_adapter.can_handle("Will Real Madrid win Champions League 2026?")
    assert not macro_adapter.can_handle("Bitcoin price above $100k?")
    assert not macro_adapter.can_handle("Will Taylor Swift release a new album in 2026?")


# --------------------------------------------------------------------------- #
# Question & Period Parsing Tests
# --------------------------------------------------------------------------- #

def test_parse_cpi_question(macro_adapter: MacroOracleAdapter):
    q = "Will Core CPI YoY be >= 2.6% in October 2026?"
    parsed = macro_adapter.parse_macro_question(q)
    assert parsed is not None
    assert parsed.indicator == MacroIndicator.CPI_CORE_YOY
    assert parsed.period == "2026-10"
    assert parsed.operator == ">="
    assert parsed.threshold == 2.6


def test_parse_fed_decision_question(macro_adapter: MacroOracleAdapter):
    q = "Fed Decision in December?: 25 bps cut"
    parsed = macro_adapter.parse_macro_question(q)
    assert parsed is not None
    assert parsed.indicator == MacroIndicator.FOMC_RATE_DECISION
    assert parsed.period == "2026-12"
    assert parsed.operator == "CHOICE"
    assert parsed.target_choice == "25 bps cut"


def test_parse_nonfarm_range_bracket(macro_adapter: MacroOracleAdapter):
    q = "US Nonfarm Payrolls October 2026: 100k - 150k"
    parsed = macro_adapter.parse_macro_question(q)
    assert parsed is not None
    assert parsed.indicator == MacroIndicator.NONFARM_PAYROLLS
    assert parsed.period == "2026-10"
    assert parsed.operator == "BRACKET"
    assert parsed.bracket_min == 100.0
    assert parsed.bracket_max == 150.0


def test_parse_treasury_question(macro_adapter: MacroOracleAdapter):
    q = "How high will 2-year Treasury yield go before 2027? Above 4.5%"
    parsed = macro_adapter.parse_macro_question(q)
    assert parsed is not None
    assert parsed.indicator == MacroIndicator.TREASURY_YIELD_2Y
    assert parsed.threshold == 4.5
    assert parsed.operator == ">="


# --------------------------------------------------------------------------- #
# Release Text Parsing Tests
# --------------------------------------------------------------------------- #

def test_parse_bls_cpi_official_text():
    sample_text = """
    The Consumer Price Index for All Urban Consumers (CPI-U) increased 0.2 percent
    in October on a seasonally adjusted basis, after rising 0.2 percent in September,
    the U.S. Bureau of Labor Statistics reported today. Over the last 12 months,
    the all items index increased 2.6 percent before seasonal adjustment.

    The index for all items less food and energy rose 0.3 percent in October,
    the same increase as in September. Over the past 12 months, the index for
    all items less food and energy increased 3.3 percent before seasonal adjustment.
    """
    res = MacroOracleAdapter.parse_bls_cpi_text(sample_text)
    assert res[MacroIndicator.CPI_HEADLINE_YOY] == 2.6
    assert res[MacroIndicator.CPI_HEADLINE_MOM] == 0.2
    assert res[MacroIndicator.CPI_CORE_YOY] == 3.3
    assert res[MacroIndicator.CPI_CORE_MOM] == 0.3


def test_parse_bls_employment_official_text():
    sample_text = """
    Total nonfarm payroll employment increased by 12,000 in October, and the
    unemployment rate was unchanged at 4.1 percent, the U.S. Bureau of Labor
    Statistics reported today.
    """
    res = MacroOracleAdapter.parse_bls_employment_text(sample_text)
    assert res[MacroIndicator.NONFARM_PAYROLLS] == 12.0  # normalized to thousands
    assert res[MacroIndicator.UNEMPLOYMENT_RATE] == 4.1


def test_parse_fomc_cut_statement():
    sample_cut = """
    The Committee decided to lower the target range for the federal funds rate by
    1/4 percentage point to 4-1/2 to 4-3/4 percent. In considering additional
    adjustments to the target range for the federal funds rate, the Committee will
    carefully assess incoming data, the evolving outlook, and the balance of risks.
    """
    res = MacroOracleAdapter.parse_fomc_statement_text(sample_cut)
    assert res[MacroIndicator.FED_FUNDS_TARGET_UPPER] == 4.75
    assert res[MacroIndicator.FOMC_RATE_DECISION] == -25.0


def test_parse_fomc_hold_statement():
    sample_hold = """
    The Committee decided to maintain the target range for the federal funds rate
    at 5-1/4 to 5-1/2 percent. In determining the extent of any additional policy
    firming that may be appropriate to return inflation to 2 percent over time...
    """
    res = MacroOracleAdapter.parse_fomc_statement_text(sample_hold)
    assert res[MacroIndicator.FED_FUNDS_TARGET_UPPER] == 5.50
    assert res[MacroIndicator.FOMC_RATE_DECISION] == 0.0


def test_parse_treasury_xml():
    sample_xml = """<?xml version="1.0" encoding="utf-8" standalone="yes" ?>
    <feed xml:base="https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml" 
          xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" 
          xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" 
          xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <content type="application/xml">
          <m:properties>
            <d:NEW_DATE>2026-11-06T00:00:00</d:NEW_DATE>
            <d:BC_3MONTH>4.52</d:BC_3MONTH>
            <d:BC_2YEAR>4.20</d:BC_2YEAR>
            <d:BC_10YEAR>4.44</d:BC_10YEAR>
            <d:BC_30YEAR>4.61</d:BC_30YEAR>
          </m:properties>
        </content>
      </entry>
    </feed>
    """
    res = MacroOracleAdapter.parse_treasury_xml(sample_xml)
    assert "2026-11-06" in res
    rates = res["2026-11-06"]
    assert rates[MacroIndicator.TREASURY_YIELD_10Y] == 4.44
    assert rates[MacroIndicator.TREASURY_YIELD_2Y] == 4.20
    assert rates[MacroIndicator.TREASURY_YIELD_3M] == 4.52


# --------------------------------------------------------------------------- #
# Ground Truth Evaluation & Pipeline Integration Tests
# --------------------------------------------------------------------------- #

def test_evaluate_candidate_cpi_conclusion(macro_adapter: MacroOracleAdapter):
    # Register an official ground truth release for October 2026 Core CPI YoY = 3.3%
    macro_adapter.record_release(
        MacroReleaseData(
            indicator=MacroIndicator.CPI_CORE_YOY,
            period="2026-10",
            value=3.3,
            unit="%",
            source=OracleFeedSource.BLS_GOV,
            release_timestamp=datetime(2026, 11, 10, 13, 30, tzinfo=timezone.utc).timestamp(),
            source_url="https://www.bls.gov/news.release/cpi.nr0.htm",
        )
    )

    # Candidate 1: YES on Core CPI >= 2.6% (should win)
    cand_yes = Domain2Candidate(
        condition_id="0xcpi_core_oct2026_cond1",
        token_id="0xtok1",
        question="Core CPI YoY - October 2026 >= 2.6%?",
        category="macro",
        price=0.72,
        hours_to_resolution=0.5,
        depth_usd=25000.0,
    )
    outcome_yes = macro_adapter.evaluate(cand_yes)
    assert outcome_yes is not None
    assert outcome_yes.is_deterministic_conclusion
    assert outcome_yes.winning_outcome == "Yes"
    assert outcome_yes.p_hat == 1.000
    assert outcome_yes.h_reversal == 0.000
    assert outcome_yes.ancillary_details["release_value"] == 3.3

    # Candidate 2: YES on Core CPI >= 3.5% (should lose)
    cand_no = Domain2Candidate(
        condition_id="0xcpi_core_oct2026_cond2",
        token_id="0xtok2",
        question="Core CPI YoY - October 2026 >= 3.5%?",
        category="macro",
        price=0.15,
        hours_to_resolution=0.5,
        depth_usd=10000.0,
    )
    outcome_no = macro_adapter.evaluate(cand_no)
    assert outcome_no is not None
    assert outcome_no.is_deterministic_conclusion
    assert outcome_no.winning_outcome == "No"
    assert outcome_no.p_hat == 0.000


def test_evaluate_candidate_fed_decision(macro_adapter: MacroOracleAdapter):
    # Register Fed rate cut of 25 bps in December 2026
    macro_adapter.record_release(
        MacroReleaseData(
            indicator=MacroIndicator.FOMC_RATE_DECISION,
            period="2026-12",
            value=-25.0,
            unit="bps",
            source=OracleFeedSource.FEDERAL_RESERVE,
            release_timestamp=datetime(2026, 12, 9, 19, 0, tzinfo=timezone.utc).timestamp(),
            source_url="https://www.federalreserve.gov/newsevents/pressreleases/monetary20261209a.htm",
        )
    )

    cand_cut25 = Domain2Candidate(
        condition_id="0xfed_dec2026_cut25",
        token_id="0xtok3",
        question="Fed Decision in December?: 25 bps cut",
        category="macro",
        price=0.80,
        hours_to_resolution=0.2,
        depth_usd=50000.0,
    )
    res = macro_adapter.evaluate(cand_cut25)
    assert res is not None
    assert res.is_deterministic_conclusion
    assert res.winning_outcome == "Yes"
    assert res.p_hat == 1.000

    cand_hold = Domain2Candidate(
        condition_id="0xfed_dec2026_hold",
        token_id="0xtok4",
        question="Fed Decision in December?: No change",
        category="macro",
        price=0.18,
        hours_to_resolution=0.2,
        depth_usd=20000.0,
    )
    res_hold = macro_adapter.evaluate(cand_hold)
    assert res_hold is not None
    assert res_hold.winning_outcome == "No"
    assert res_hold.p_hat == 0.000


def test_pipeline_integration(macro_adapter: MacroOracleAdapter):
    pipeline = ExogenousOraclePipeline()
    pipeline.register_adapter(macro_adapter)

    # Pre-load BLS Nonfarm payrolls release = 12.0k
    macro_adapter.record_release(
        MacroReleaseData(
            indicator=MacroIndicator.NONFARM_PAYROLLS,
            period="2026-10",
            value=12.0,
            unit="k",
            source=OracleFeedSource.BLS_GOV,
        )
    )

    candidate = Domain2Candidate(
        condition_id="0xnonfarm_oct2026",
        token_id="0xtok5",
        question="US Non-Farm Payrolls: October 2026 < 100k",
        category="macro",
        price=0.60,
        hours_to_resolution=0.1,
        depth_usd=15000.0,
        p_hat=None,
    )

    assert candidate.p_hat is None
    outcome = pipeline.evaluate_candidate(candidate)
    assert outcome is not None
    assert outcome.is_deterministic_conclusion
    # Candidate p_hat must be overridden to 1.000 because 12.0 < 100.0
    assert candidate.p_hat == 1.000


def test_evaluate_market_resolution_interface(macro_adapter: MacroOracleAdapter):
    macro_adapter.record_release(
        MacroReleaseData(
            indicator=MacroIndicator.TREASURY_YIELD_10Y,
            period="2026-11",
            value=4.44,
            unit="%",
            source=OracleFeedSource.US_TREASURY,
        )
    )
    mkt_info = {
        "condition_id": "0xtreasury_10y_nov2026",
        "question": "10-Year Treasury Yield in November 2026 >= 4.40%?",
    }
    verdict = macro_adapter.evaluate_market_resolution(mkt_info)
    assert verdict.outcome == ResolutionOutcome.YES
    assert verdict.p_yes == 1.0
    assert verdict.is_deterministic
    assert verdict.settlement_price == 4.44


def test_parse_jobless_claims_question(macro_adapter: MacroOracleAdapter):
    q = "Initial Jobless Claims: Week of Nov 6 > 220k?"
    parsed = macro_adapter.parse_macro_question(q)
    assert parsed is not None
    assert parsed.indicator == MacroIndicator.INITIAL_JOBLESS_CLAIMS
    assert parsed.operator == ">"
    assert parsed.threshold == 220.0


def test_fred_api_mock_parsing(macro_adapter: MacroOracleAdapter):
    # Mock FRED API response
    mock_fred_json = {
        "realtime_start": "2026-11-07",
        "realtime_end": "2026-11-07",
        "observations": [
            {"date": "2026-10-01", "value": "4.1"},
            {"date": "2026-09-01", "value": "4.1"},
        ]
    }
    # Verify indicator mapping
    ind = macro_adapter._map_fred_to_indicator("UNRATE")
    assert ind == MacroIndicator.UNEMPLOYMENT_RATE
    ind_cpi = macro_adapter._map_fred_to_indicator("CPIAUCNS")
    assert ind_cpi == MacroIndicator.CPI_HEADLINE_YOY
    ind_fed = macro_adapter._map_fred_to_indicator("DFEDTARU")
    assert ind_fed == MacroIndicator.FED_FUNDS_TARGET_UPPER


def test_bls_v2_series_mapping():
    assert BLS_SERIES_MAP["CPI_HEADLINE_NSA"] == "CUUR0000SA0"
    assert BLS_SERIES_MAP["UNEMPLOYMENT_RATE"] == "LNS14000000"
    assert BLS_SERIES_MAP["NONFARM_EMPLOYMENT"] == "CES0000000001"


def test_initial_release_vs_revisions_rule(macro_adapter: MacroOracleAdapter):
    # Polymarket UMA rule: Initial release is authoritative, revisions ignored.
    # Record initial release = 150k
    macro_adapter.record_release(
        MacroReleaseData(
            indicator=MacroIndicator.NONFARM_PAYROLLS,
            period="2026-10",
            value=150.0,
            unit="k",
            source=OracleFeedSource.BLS_GOV,
            is_initial_release=True,
        )
    )
    cand = Domain2Candidate(
        condition_id="0xnfp_initial_test",
        token_id="0xtok6",
        question="US Non-Farm Payrolls October 2026 >= 140k",
        category="macro",
        price=0.50,
        hours_to_resolution=0.1,
        depth_usd=10000.0,
    )
    outcome = macro_adapter.evaluate(cand)
    assert outcome is not None
    assert outcome.winning_outcome == "Yes"
    assert outcome.ancillary_details["is_initial_release"] is True
