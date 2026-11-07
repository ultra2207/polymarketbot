"""Cluster 3 Real-Time Oracle Adapter: Macroeconomic & Government Releases.

Covers:
- U.S. Consumer Price Index (CPI) Headline and Core (YoY and MoM) prints.
- Federal Reserve FOMC Rate Decisions (Target Range Upper Bound and basis-point shifts).
- U.S. Employment Situation: Non-Farm Payrolls (NFP) and Civilian Unemployment Rate.
- U.S. Department of Labor (DOL) Initial Jobless Claims.
- U.S. Treasury Constant Maturity Daily Par Yields (10Y, 2Y, 3M, 30Y).

Resolution Mechanics & UMA Ancillary Data:
- Primary Resolution Sources:
  * BLS CPI: https://www.bls.gov/cpi/ and news release table cpi.t01.htm.
  * BLS Employment Situation: https://www.bls.gov/news.release/empsit.nr0.htm.
  * Federal Reserve FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm.
  * Treasury Yields: https://home.treasury.gov/resource-center/data-chart-center/interest-rates/.
  * DOL Jobless Claims: https://www.dol.gov/ui/data.pdf.
- Critical UMA Rule: Settlement is determined by the INITIAL official release.
  Subsequent retroactive revisions or rebenchmarking are explicitly ignored.
- Precision: BLS CPI and Unemployment resolve to exactly one decimal place (e.g. 2.6%).
  Fed Funds changes resolve to standard 25 bps brackets (e.g. 25 bps cut, 50 bps cut).

Multi-Tier Data Provider Hierarchy:
- Tier 1 (Lowest Latency, Sub-Second): Zero-auth direct scraping/RSS of BLS, Fed, and Treasury.
- Tier 2 (Backup Verification): Federal Reserve FRED API (Free key with Google login, 120 req/min).
- Tier 3 (Timeseries Verification): BLS API v2 (Free key with registration, 500 req/day).
- Tier 4 (Deterministic Testing): In-memory mock/fixture store for sub-millisecond evaluation.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union, TYPE_CHECKING
import urllib.request
import urllib.parse
import json

from polymarketbot.logs import get_logger
from polymarketbot.oracle.base import (
    BaseOracleAdapter,
    GroundTruthOutcome,
    OracleCategory,
    OracleFeedSource,
    OracleStatus,
    ParsedAncillaryData,
    PricePoint,
    ResolutionOutcome,
    ResolutionVerdict,
)

if TYPE_CHECKING:
    from polymarketbot.trading.domain2.ortools_allocator import Domain2Candidate

log = get_logger("oracle.macro")

# API Configuration and Defaults
DEFAULT_FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
DEFAULT_BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
DEFAULT_FED_RSS_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
DEFAULT_TREASURY_XML_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
DEFAULT_USER_AGENT = "PolymarketBot-MacroOracle/1.0 (+https://github.com/polymarketbot)"

# FRED Series IDs for macro indicators
FRED_SERIES_MAP = {
    "CPI_HEADLINE_YOY": "CPIAUCNS",        # CPI-U All Items (NSA for YoY calculation)
    "CPI_HEADLINE_SA": "CPIAUCSL",         # CPI-U All Items (Seasonally Adjusted)
    "CPI_CORE_YOY": "CPILFENS",            # Core CPI Less Food and Energy (NSA)
    "CPI_CORE_SA": "CPILFESL",             # Core CPI (SA)
    "NONFARM_PAYROLLS": "PAYEMS",          # Total Nonfarm Employees (SA, thousands)
    "UNEMPLOYMENT_RATE": "UNRATE",         # Civilian Unemployment Rate (%)
    "INITIAL_JOBLESS_CLAIMS": "ICSA",      # Initial Jobless Claims (SA)
    "FED_FUNDS_UPPER": "DFEDTARU",         # Fed Funds Target Range - Upper Limit (%)
    "FED_FUNDS_LOWER": "DFEDTARL",         # Fed Funds Target Range - Lower Limit (%)
    "TREASURY_10Y": "DGS10",               # 10-Year Treasury Constant Maturity (%)
    "TREASURY_2Y": "DGS2",                 # 2-Year Treasury Constant Maturity (%)
    "TREASURY_3M": "DGS3MO",               # 3-Month Treasury Constant Maturity (%)
    "TREASURY_30Y": "DGS30",               # 30-Year Treasury Constant Maturity (%)
}

# BLS Series IDs
BLS_SERIES_MAP = {
    "CPI_HEADLINE_NSA": "CUUR0000SA0",     # All Items, NSA
    "CPI_HEADLINE_SA": "CUSR0000SA0",      # All Items, SA
    "CPI_CORE_NSA": "CUUR0000SA0L1E",      # Less food and energy, NSA
    "CPI_CORE_SA": "CUSR0000SA0L1E",       # Less food and energy, SA
    "NONFARM_EMPLOYMENT": "CES0000000001",  # Total nonfarm employment (thousands)
    "UNEMPLOYMENT_RATE": "LNS14000000",    # Civilian unemployment rate
}


class MacroIndicator(str, Enum):
    """Supported Macroeconomic and Government Release Indicators."""
    CPI_HEADLINE_YOY = "CPI_HEADLINE_YOY"
    CPI_HEADLINE_MOM = "CPI_HEADLINE_MOM"
    CPI_CORE_YOY = "CPI_CORE_YOY"
    CPI_CORE_MOM = "CPI_CORE_MOM"
    FED_FUNDS_TARGET_UPPER = "FED_FUNDS_TARGET_UPPER"
    FOMC_RATE_DECISION = "FOMC_RATE_DECISION"
    NONFARM_PAYROLLS = "NONFARM_PAYROLLS"
    UNEMPLOYMENT_RATE = "UNEMPLOYMENT_RATE"
    INITIAL_JOBLESS_CLAIMS = "INITIAL_JOBLESS_CLAIMS"
    TREASURY_YIELD_10Y = "TREASURY_YIELD_10Y"
    TREASURY_YIELD_2Y = "TREASURY_YIELD_2Y"
    TREASURY_YIELD_3M = "TREASURY_YIELD_3M"
    TREASURY_YIELD_30Y = "TREASURY_YIELD_30Y"


@dataclass
class MacroReleaseData:
    """Standardized record of an exogenous macroeconomic release observation."""
    indicator: MacroIndicator
    period: str                             # Normalized period string: "2026-10", "2026-11-06", etc.
    value: float                            # Numerical release value (e.g. 2.6, 4.75, 150.0)
    unit: str                               # "%", "bps", "k", "level"
    source: OracleFeedSource
    release_timestamp: float = 0.0          # Unix timestamp of publication
    is_initial_release: bool = True         # True if initial unrevised print
    source_url: str = ""
    raw_text: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedMacroMarket:
    """Structured resolution criteria parsed from a Polymarket macro condition."""
    indicator: MacroIndicator
    period: str                             # Target period (e.g. "2026-10", "December 2026")
    operator: str = ">="                    # ">=", "<=", ">", "<", "==", "BRACKET", "CHOICE"
    threshold: Optional[float] = None       # Numerical target value (e.g. 2.6, 4.5)
    bracket_min: Optional[float] = None
    bracket_max: Optional[float] = None
    target_choice: Optional[str] = None     # E.g. "25 bps cut", "No change", "50+ bps cut"
    is_initial_only: bool = True
    precision_decimals: int = 1


class MacroOracleAdapter(BaseOracleAdapter):
    """Cluster 3 Real-Time Oracle Adapter for Macroeconomic Releases.
    
    Provides deterministic conclusion detection before UMA proposals across:
    - US CPI (Headline & Core, YoY & MoM)
    - Federal Reserve FOMC decisions (Upper target range & basis points change)
    - BLS Non-Farm Payrolls (NFP) and Civilian Unemployment Rate
    - Initial Jobless Claims
    - Treasury Constant Maturity Par Yields
    """

    def __init__(
        self,
        fred_api_key: Optional[str] = None,
        bls_api_key: Optional[str] = None,
        enable_network: bool = True,
        timeout_seconds: float = 3.0,
        user_agent: Optional[str] = None,
    ) -> None:
        super().__init__(name="macro", category=OracleCategory.MACRO)
        self.fred_api_key = fred_api_key or os.getenv("FRED_API_KEY")
        self.bls_api_key = bls_api_key or os.getenv("BLS_API_KEY")
        self.enable_network = enable_network
        self.timeout_s = timeout_seconds
        self.user_agent = user_agent or os.getenv("MACRO_ORACLE_USER_AGENT", DEFAULT_USER_AGENT)

        # In-memory store of ground truth releases: (indicator, period) -> MacroReleaseData
        self._releases: Dict[Tuple[MacroIndicator, str], MacroReleaseData] = {}
        log.info(
            f"Initialized MacroOracleAdapter (Network={enable_network}, "
            f"FRED_KEY={'SET' if self.fred_api_key else 'NONE'}, "
            f"BLS_KEY={'SET' if self.bls_api_key else 'NONE'})"
        )

    # -------------------------------------------------------------------------
    # Core Adapter Interface
    # -------------------------------------------------------------------------

    def can_handle(
        self,
        question: str,
        resolution_source: Optional[str] = None,
        category_hint: Optional[str] = None,
    ) -> bool:
        """Check if this adapter handles the given market question or resolution source."""
        q_lower = (question or "").lower()
        res_lower = (resolution_source or "").lower()
        cat_lower = (category_hint or "").lower()

        if cat_lower in ("macro", "economic", "economy"):
            return True

        # Check resolution sources
        macro_sources = [
            "bls.gov",
            "federalreserve.gov",
            "home.treasury.gov",
            "dol.gov",
            "stlouisfed.org",
            "fred",
        ]
        if any(src in res_lower for src in macro_sources):
            return True

        # Check question keywords
        macro_patterns = [
            r"\bcpi\b",
            r"\binflation\b",
            r"\bcore cpi\b",
            r"\bfomc\b",
            r"\bfed decision\b",
            r"\bfed rate\b",
            r"\bfederal reserve\b",
            r"\bnonfarm\b",
            r"\bnon-farm\b",
            r"\bpayrolls\b",
            r"\bunemployment rate\b",
            r"\bjobless claims\b",
            r"\binitial claims\b",
            r"\btreasury yield\b",
            r"\b10-year treasury\b",
            r"\b2-year treasury\b",
            r"\bjobs added\b",
        ]
        return any(re.search(pat, q_lower) for pat in macro_patterns)

    def evaluate(self, candidate: Domain2Candidate) -> Optional[GroundTruthOutcome]:
        """Evaluate a Domain2Candidate and return ground truth if conclusion is detected."""
        parsed = self.parse_macro_question(candidate.question)
        if not parsed:
            return None

        # Look for release in local ground truth store
        key = (parsed.indicator, parsed.period)
        release = self._releases.get(key)

        # If not in cache and network enabled, attempt to poll/fetch
        if not release and self.enable_network:
            release = self._fetch_live_indicator(parsed.indicator, parsed.period)
            if release:
                self.record_release(release)

        if not release:
            return None

        # Determine whether the candidate condition passed or failed
        is_winner, rationale = self._evaluate_condition(parsed, release, candidate.question)
        if is_winner is None:
            return None

        winning_outcome = "Yes" if is_winner else "No"
        p_hat = 1.000 if is_winner else 0.000

        outcome = GroundTruthOutcome(
            event_id=candidate.condition_id,
            category=self.category,
            status=OracleStatus.CONCLUDED,
            concluded=True,
            winning_outcome=winning_outcome,
            p_hat=p_hat,
            h_reversal=0.000,
            confidence=1.000,
            timestamp_s=release.release_timestamp or datetime.now(timezone.utc).timestamp(),
            resolution_source_url=release.source_url or "https://www.bls.gov/news.release/",
            ancillary_details={
                "indicator": parsed.indicator.value,
                "target_period": parsed.period,
                "release_value": release.value,
                "release_unit": release.unit,
                "is_initial_release": release.is_initial_release,
                "rationale": rationale,
                "operator": parsed.operator,
                "threshold": parsed.threshold,
                "target_choice": parsed.target_choice,
            },
        )
        return outcome

    def poll_active_events(self) -> Dict[str, GroundTruthOutcome]:
        """Poll upstream APIs for latest releases across all tracked indicators."""
        outcomes: Dict[str, GroundTruthOutcome] = {}
        for (indicator, period), release in list(self._releases.items()):
            key = f"{indicator.value}_{period}"
            outcomes[key] = GroundTruthOutcome(
                event_id=key,
                category=self.category,
                status=OracleStatus.CONCLUDED,
                concluded=True,
                winning_outcome=str(release.value),
                p_hat=1.0,
                h_reversal=0.0,
                confidence=1.0,
                timestamp_s=release.release_timestamp or datetime.now(timezone.utc).timestamp(),
                resolution_source_url=release.source_url,
                ancillary_details={
                    "indicator": indicator.value,
                    "period": period,
                    "value": release.value,
                    "unit": release.unit,
                },
            )
        return outcomes

    # -------------------------------------------------------------------------
    # BaseOracleAdapter Interface Conformance
    # -------------------------------------------------------------------------

    async def get_latest_price(self, asset: str) -> PricePoint:
        """Fetch latest reference rate/value for a macro asset (e.g. DGS10, CPI)."""
        asset_norm = asset.upper().replace("-", "_")
        for (ind, _), rel in self._releases.items():
            if asset_norm in ind.value:
                return PricePoint(
                    asset=asset,
                    price=rel.value,
                    timestamp_ms=int((rel.release_timestamp or datetime.now(timezone.utc).timestamp()) * 1000),
                    source=rel.source,
                )
        raise ValueError(f"No macro price available for asset '{asset}'")

    async def get_historical_price(
        self, asset: str, timestamp_s: float
    ) -> Optional[PricePoint]:
        """Fetch historical rate near timestamp."""
        return None

    def parse_ancillary_data(self, ancillary: Union[str, bytes]) -> ParsedAncillaryData:
        """Parse raw UMA ancillary data bytes/string for macro parameters."""
        raw_text = ancillary if isinstance(ancillary, str) else ancillary.decode("utf-8", errors="ignore")
        parsed_mkt = self.parse_macro_question(raw_text)

        strike = parsed_mkt.threshold if parsed_mkt else None
        op = parsed_mkt.operator if parsed_mkt else ">="
        asset = parsed_mkt.indicator.value if parsed_mkt else "MACRO"

        return ParsedAncillaryData(
            raw_ancillary=raw_text,
            title=raw_text.split("\n")[0] if "\n" in raw_text else raw_text[:100],
            asset=asset,
            strike_price=strike,
            comparison_operator=op,
            raw_fields={"parsed_macro": parsed_mkt},
        )

    def evaluate_market_resolution(
        self,
        market_info: Dict[str, Any],
        current_prices: Optional[Dict[str, float]] = None,
        as_of_timestamp: Optional[float] = None,
    ) -> ResolutionVerdict:
        """Evaluate market resolution state."""
        question = market_info.get("question", "")
        condition_id = market_info.get("condition_id", "unknown")
        parsed = self.parse_macro_question(question)

        if not parsed:
            return ResolutionVerdict(
                condition_id=condition_id,
                outcome=ResolutionOutcome.UNKNOWN,
                p_yes=0.5,
                rationale="Unparseable macro question",
            )

        key = (parsed.indicator, parsed.period)
        release = self._releases.get(key)
        if not release:
            return ResolutionVerdict(
                condition_id=condition_id,
                outcome=ResolutionOutcome.PENDING,
                p_yes=0.5,
                rationale=f"Release {parsed.indicator.value} for {parsed.period} pending",
            )

        is_winner, rationale = self._evaluate_condition(parsed, release, question)
        outcome = ResolutionOutcome.YES if is_winner else ResolutionOutcome.NO
        return ResolutionVerdict(
            condition_id=condition_id,
            outcome=outcome,
            p_yes=1.0 if is_winner else 0.0,
            settlement_price=release.value,
            benchmark_price=parsed.threshold,
            confidence=1.0,
            rationale=rationale,
            is_deterministic=True,
            seconds_remaining=0.0,
        )

    # -------------------------------------------------------------------------
    # Polymarket Question & Ancillary Data Parsing
    # -------------------------------------------------------------------------

    def parse_macro_question(
        self, question: str, description: Optional[str] = None
    ) -> Optional[ParsedMacroMarket]:
        """Parse Polymarket macroeconomic market title/description into structured query."""
        text = f"{question} {description or ''}".strip()
        text_lower = text.lower()

        # 1. Normalize period/month
        period = self._extract_period(text)

        # 2. Check CPI
        if "cpi" in text_lower or "inflation" in text_lower:
            is_core = "core" in text_lower or "excluding food and energy" in text_lower or "less food" in text_lower
            is_mom = "mom" in text_lower or "month-over-month" in text_lower or "monthly" in text_lower
            
            if is_core:
                indicator = MacroIndicator.CPI_CORE_MOM if is_mom else MacroIndicator.CPI_CORE_YOY
            else:
                indicator = MacroIndicator.CPI_HEADLINE_MOM if is_mom else MacroIndicator.CPI_HEADLINE_YOY

            # Extract threshold or bracket
            th, op = self._extract_numerical_threshold(text)
            return ParsedMacroMarket(
                indicator=indicator,
                period=period,
                operator=op,
                threshold=th,
                precision_decimals=1,
            )

        # 3. Check Fed / FOMC Rate Decision
        if "fomc" in text_lower or "fed decision" in text_lower or "fed rate" in text_lower or "federal reserve" in text_lower:
            # Check if asking for specific action choice (e.g. 25 bps cut, No change)
            choice = self._extract_fed_choice(text)
            if choice:
                return ParsedMacroMarket(
                    indicator=MacroIndicator.FOMC_RATE_DECISION,
                    period=period,
                    operator="CHOICE",
                    target_choice=choice,
                    precision_decimals=0,
                )
            # Check target range rate
            th, op = self._extract_numerical_threshold(text)
            return ParsedMacroMarket(
                indicator=MacroIndicator.FED_FUNDS_TARGET_UPPER,
                period=period,
                operator=op,
                threshold=th,
                precision_decimals=2,
            )

        # 4. Check Nonfarm Payrolls / Jobs Added
        if "nonfarm" in text_lower or "non-farm" in text_lower or "payrolls" in text_lower or "jobs added" in text_lower:
            # Bracket or threshold
            b_min, b_max = self._extract_range_bracket(text)
            if b_min is not None or b_max is not None:
                return ParsedMacroMarket(
                    indicator=MacroIndicator.NONFARM_PAYROLLS,
                    period=period,
                    operator="BRACKET",
                    bracket_min=b_min,
                    bracket_max=b_max,
                    precision_decimals=0,
                )
            th, op = self._extract_numerical_threshold(text)
            return ParsedMacroMarket(
                indicator=MacroIndicator.NONFARM_PAYROLLS,
                period=period,
                operator=op,
                threshold=th,
                precision_decimals=0,
            )

        # 5. Check Unemployment Rate
        if "unemployment" in text_lower:
            th, op = self._extract_numerical_threshold(text)
            return ParsedMacroMarket(
                indicator=MacroIndicator.UNEMPLOYMENT_RATE,
                period=period,
                operator=op,
                threshold=th,
                precision_decimals=1,
            )

        # 6. Check Initial Jobless Claims
        if "jobless claims" in text_lower or "initial claims" in text_lower:
            th, op = self._extract_numerical_threshold(text)
            return ParsedMacroMarket(
                indicator=MacroIndicator.INITIAL_JOBLESS_CLAIMS,
                period=period,
                operator=op,
                threshold=th,
                precision_decimals=0,
            )

        # 7. Check Treasury Yields
        if "treasury" in text_lower and "yield" in text_lower:
            if "2-year" in text_lower or "2 yr" in text_lower or "2y" in text_lower:
                indicator = MacroIndicator.TREASURY_YIELD_2Y
            elif "3-month" in text_lower or "3 mo" in text_lower or "3m" in text_lower:
                indicator = MacroIndicator.TREASURY_YIELD_3M
            elif "30-year" in text_lower or "30 yr" in text_lower or "30y" in text_lower:
                indicator = MacroIndicator.TREASURY_YIELD_30Y
            else:
                indicator = MacroIndicator.TREASURY_YIELD_10Y

            th, op = self._extract_numerical_threshold(text)
            return ParsedMacroMarket(
                indicator=indicator,
                period=period,
                operator=op,
                threshold=th,
                precision_decimals=2,
            )

        return None

    def record_release(self, release: MacroReleaseData) -> None:
        """Store ground-truth release data in memory."""
        key = (release.indicator, release.period)
        self._releases[key] = release
        log.info(
            f"[MACRO RELEASE STORED] {release.indicator.value} [{release.period}]: "
            f"{release.value}{release.unit} Source: {release.source.value}"
        )

    # -------------------------------------------------------------------------
    # Evaluation Helpers
    # -------------------------------------------------------------------------

    def _evaluate_condition(
        self,
        parsed: ParsedMacroMarket,
        release: MacroReleaseData,
        question: str,
    ) -> Tuple[Optional[bool], str]:
        """Compare release value against market criteria to determine win/loss."""
        # 1. Action Choice (Fed rate cut/hike/hold)
        if parsed.operator == "CHOICE":
            # release.value is basis point change (e.g. -25.0, 0.0, +25.0)
            change_bps = int(release.value)
            target = (parsed.target_choice or "").lower()

            if "25 bps cut" in target or "-25" in target:
                won = change_bps == -25
            elif "50" in target and "cut" in target:
                won = change_bps <= -50
            elif "no change" in target or "pause" in target or "hold" in target or "0 bps" in target:
                won = change_bps == 0
            elif "25 bps hike" in target or "+25" in target:
                won = change_bps == 25
            elif "50" in target and "hike" in target:
                won = change_bps >= 50
            else:
                won = False

            rationale = f"FOMC rate change was {change_bps} bps; target was '{parsed.target_choice}'"
            return won, rationale

        # 2. Range Bracket (e.g. Nonfarm payrolls 100k - 150k)
        if parsed.operator == "BRACKET":
            val = release.value
            b_min = parsed.bracket_min if parsed.bracket_min is not None else -float("inf")
            b_max = parsed.bracket_max if parsed.bracket_max is not None else float("inf")
            
            won = (val >= b_min) and (val <= b_max)
            rationale = f"Release {val} {release.unit} in bracket [{b_min}, {b_max}]"
            return won, rationale

        # 3. Standard Threshold (>=, <=, >, <)
        if parsed.threshold is not None:
            # Round to required precision (e.g. 1 decimal for BLS CPI)
            val = round(release.value, parsed.precision_decimals)
            th = round(parsed.threshold, parsed.precision_decimals)
            op = parsed.operator

            if op == ">=":
                won = val >= th
            elif op == ">":
                won = val > th
            elif op == "<=":
                won = val <= th
            elif op == "<":
                won = val < th
            elif op == "==":
                won = abs(val - th) <= 1e-4
            else:
                won = val >= th

            rationale = f"Release {val} {op} threshold {th}"
            return won, rationale

        return None, "No evaluatable condition found"

    # -------------------------------------------------------------------------
    # Parsing Government Releases (Zero-Auth Direct Scraping & Parsing)
    # -------------------------------------------------------------------------

    @staticmethod
    def parse_bls_cpi_text(text: str) -> Dict[MacroIndicator, float]:
        """Extract headline and core CPI percentages from BLS CPI news release text."""
        results: Dict[MacroIndicator, float] = {}

        # 1. Headline YoY: "all items index increased 2.6 percent before seasonal adjustment"
        m_headline_yoy = re.search(
            r"all items index\s+(?:increased|rose|decreased|fell)\s+([\d\.]+)\s+percent.*?(?:over the|for the \d+-month|over the last 12 months|before seasonal adjustment)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not m_headline_yoy:
            m_headline_yoy = re.search(
                r"over the last 12 months.*?all items index\s+(?:increased|rose|fell)\s+([\d\.]+)\s+percent",
                text,
                re.IGNORECASE | re.DOTALL,
            )
        if m_headline_yoy:
            results[MacroIndicator.CPI_HEADLINE_YOY] = float(m_headline_yoy.group(1))

        # 2. Headline MoM: "CPI-U increased 0.2 percent in October on a seasonally adjusted basis"
        m_headline_mom = re.search(
            r"(?:consumer price index for all urban consumers|cpi-u|\bcpi\b)[^\d%]{0,50}?(?:increased|rose|decreased|fell)\s+([\d\.]+)\s+percent[^\.\;]*?seasonally adjusted",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if m_headline_mom:
            results[MacroIndicator.CPI_HEADLINE_MOM] = float(m_headline_mom.group(1))

        # 3. Core YoY: "index for all items less food and energy increased 3.3 percent"
        m_core_yoy = re.search(
            r"(?:all items less food and energy|core cpi)[^\.\;]*?(?:increased|rose|fell)\s+([\d\.]+)\s+percent[^\.\;]*?(?:12 months|over the past 12)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not m_core_yoy:
            m_core_yoy = re.search(
                r"over the past 12 months.*?all items less food and energy\s+(?:increased|rose|fell)\s+([\d\.]+)\s+percent",
                text,
                re.IGNORECASE | re.DOTALL,
            )
        if m_core_yoy:
            results[MacroIndicator.CPI_CORE_YOY] = float(m_core_yoy.group(1))

        # 4. Core MoM: "index for all items less food and energy rose 0.3 percent in October"
        m_core_mom = re.search(
            r"all items less food and energy\s+(?:rose|increased|fell)\s+([\d\.]+)\s+percent",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if m_core_mom:
            results[MacroIndicator.CPI_CORE_MOM] = float(m_core_mom.group(1))

        return results

    @staticmethod
    def parse_bls_employment_text(text: str) -> Dict[MacroIndicator, float]:
        """Extract Non-Farm Payrolls and Unemployment Rate from BLS Employment Situation text."""
        results: Dict[MacroIndicator, float] = {}

        # 1. Non-Farm Payrolls net change: "Total nonfarm payroll employment increased by 12,000"
        m_nfp = re.search(
            r"total nonfarm payroll employment (?:increased by|rose by|changed little \(?\+?|\+?)([\d,]+)",
            text,
            re.IGNORECASE,
        )
        if not m_nfp:
            m_nfp = re.search(
                r"payroll employment (?:increased|rose|fell|decreased) by ([\d,]+)",
                text,
                re.IGNORECASE,
            )
        if m_nfp:
            raw_str = m_nfp.group(1).replace(",", "")
            val = float(raw_str)
            # Normalize to thousands if reported in single jobs
            if val >= 1000:
                val = val / 1000.0
            results[MacroIndicator.NONFARM_PAYROLLS] = val

        # 2. Unemployment rate: "unemployment rate was unchanged at 4.1 percent"
        m_unrate = re.search(
            r"unemployment rate (?:was unchanged at|edged (?:up|down) to|rose to|fell to|stood at) ([\d\.]+) percent",
            text,
            re.IGNORECASE,
        )
        if m_unrate:
            results[MacroIndicator.UNEMPLOYMENT_RATE] = float(m_unrate.group(1))

        return results

    @staticmethod
    def parse_fomc_statement_text(
        text: str, prior_target_upper: Optional[float] = None
    ) -> Dict[MacroIndicator, float]:
        """Extract target range upper bound and basis-point change from Fed FOMC statement."""
        results: Dict[MacroIndicator, float] = {}

        # Check for maintain / unchanged: "maintain the target range for the federal funds rate at 5 to 5-1/4 percent"
        m_maintain = re.search(
            r"maintain the target range for the federal funds rate\s+at\s+([\d\-/\.]+)\s*(?:to|-)\s*([\d\-/\.]+)\s*percent",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if m_maintain:
            upper_str = m_maintain.group(2)
            upper_val = MacroOracleAdapter._parse_fraction_or_decimal(upper_str)
            results[MacroIndicator.FED_FUNDS_TARGET_UPPER] = upper_val
            results[MacroIndicator.FOMC_RATE_DECISION] = 0.0
            return results

        # Check for lower / cut: "lower the target range for the federal funds rate by 1/4 percentage point to 4-1/2 to 4-3/4 percent"
        m_lower = re.search(
            r"lower the target range for the federal funds rate\s+by\s+([\d\-/\.]+\s*(?:percentage point|basis points|bps))?.*?to\s+([\d\-/\.]+)\s*(?:to|-)\s*([\d\-/\.]+)\s*percent",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if m_lower:
            cut_str = m_lower.group(1) or ""
            upper_str = m_lower.group(3)
            upper_val = MacroOracleAdapter._parse_fraction_or_decimal(upper_str)
            results[MacroIndicator.FED_FUNDS_TARGET_UPPER] = upper_val

            # Determine cut amount
            if "50" in cut_str or "1/2" in cut_str:
                results[MacroIndicator.FOMC_RATE_DECISION] = -50.0
            else:
                results[MacroIndicator.FOMC_RATE_DECISION] = -25.0
            return results

        # Check for raise / hike
        m_raise = re.search(
            r"raise the target range for the federal funds rate\s+by\s+([\d\-/\.]+\s*(?:percentage point|basis points|bps))?.*?to\s+([\d\-/\.]+)\s*(?:to|-)\s*([\d\-/\.]+)\s*percent",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if m_raise:
            hike_str = m_raise.group(1) or ""
            upper_str = m_raise.group(3)
            upper_val = MacroOracleAdapter._parse_fraction_or_decimal(upper_str)
            results[MacroIndicator.FED_FUNDS_TARGET_UPPER] = upper_val

            if "50" in hike_str or "1/2" in hike_str:
                results[MacroIndicator.FOMC_RATE_DECISION] = 50.0
            else:
                results[MacroIndicator.FOMC_RATE_DECISION] = 25.0
            return results

        return results

    @staticmethod
    def parse_treasury_xml(xml_content: str) -> Dict[str, Dict[MacroIndicator, float]]:
        """Parse Daily Treasury Par Yield Curve XML feed into date -> indicator map."""
        rates_by_date: Dict[str, Dict[MacroIndicator, float]] = {}
        root = ET.fromstring(xml_content)

        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
            "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
        }

        for entry in root.findall("atom:entry", ns):
            content = entry.find("atom:content", ns)
            if content is None:
                continue
            props = content.find("m:properties", ns)
            if props is None:
                continue

            date_el = props.find("d:NEW_DATE", ns)
            if date_el is None or not date_el.text:
                continue

            date_str = date_el.text.split("T")[0]
            rates_by_date[date_str] = {}

            tenors = [
                ("d:BC_10YEAR", MacroIndicator.TREASURY_YIELD_10Y),
                ("d:BC_2YEAR", MacroIndicator.TREASURY_YIELD_2Y),
                ("d:BC_3MONTH", MacroIndicator.TREASURY_YIELD_3M),
                ("d:BC_30YEAR", MacroIndicator.TREASURY_YIELD_30Y),
            ]
            for tag, ind in tenors:
                el = props.find(tag, ns)
                if el is not None and el.text:
                    try:
                        rates_by_date[date_str][ind] = float(el.text)
                    except ValueError:
                        pass

        return rates_by_date

    # -------------------------------------------------------------------------
    # FRED & BLS API Integration
    # -------------------------------------------------------------------------

    def query_fred_series(
        self, series_id: str, observation_date: Optional[str] = None
    ) -> Optional[MacroReleaseData]:
        """Fetch latest observation from FRED API (requires FRED_API_KEY)."""
        if not self.fred_api_key:
            log.warning("query_fred_series called but FRED_API_KEY is not set.")
            return None

        params = {
            "series_id": series_id,
            "api_key": self.fred_api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 5,
        }
        url = f"{DEFAULT_FRED_API_URL}?{urllib.parse.urlencode(params)}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                obs = data.get("observations", [])
                if not obs:
                    return None

                latest = obs[0]
                val_str = latest.get("value")
                if val_str == "." or not val_str:
                    return None

                val = float(val_str)
                obs_date = latest.get("date", "")

                return MacroReleaseData(
                    indicator=self._map_fred_to_indicator(series_id),
                    period=obs_date[:7] if len(obs_date) >= 7 else obs_date,
                    value=val,
                    unit="%",
                    source=OracleFeedSource.FRED_API,
                    source_url=url,
                )
        except Exception as e:
            log.error(f"Error querying FRED API series {series_id}: {e}")
            return None

    def query_bls_v2_series(
        self, series_ids: List[str], start_year: str, end_year: str
    ) -> Dict[str, float]:
        """Fetch series from BLS API v2 (POST JSON, with optional BLS_API_KEY)."""
        payload: Dict[str, Any] = {
            "seriesid": series_ids,
            "startyear": start_year,
            "endyear": end_year,
        }
        if self.bls_api_key:
            payload["registrationKey"] = self.bls_api_key

        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

        try:
            data_bytes = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                DEFAULT_BLS_API_URL, data=data_bytes, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                res_json = json.loads(resp.read().decode("utf-8"))
                out: Dict[str, float] = {}

                if res_json.get("status") == "REQUEST_SUCCEEDED":
                    for s in res_json.get("Results", {}).get("series", []):
                        sid = s.get("seriesID")
                        d_items = s.get("data", [])
                        if d_items:
                            out[sid] = float(d_items[0].get("value"))
                return out
        except Exception as e:
            log.error(f"Error querying BLS API v2: {e}")
            return {}

    # -------------------------------------------------------------------------
    # Private Helper Functions
    # -------------------------------------------------------------------------

    def _fetch_live_indicator(
        self, indicator: MacroIndicator, period: str
    ) -> Optional[MacroReleaseData]:
        """Attempt to fetch live indicator from upstream zero-auth or API sources."""
        # Check FRED if key is available
        if self.fred_api_key:
            fred_id = None
            if indicator == MacroIndicator.CPI_HEADLINE_YOY:
                fred_id = FRED_SERIES_MAP["CPI_HEADLINE_YOY"]
            elif indicator == MacroIndicator.UNEMPLOYMENT_RATE:
                fred_id = FRED_SERIES_MAP["UNEMPLOYMENT_RATE"]
            elif indicator == MacroIndicator.FED_FUNDS_TARGET_UPPER:
                fred_id = FRED_SERIES_MAP["FED_FUNDS_UPPER"]
            elif indicator == MacroIndicator.TREASURY_YIELD_10Y:
                fred_id = FRED_SERIES_MAP["TREASURY_10Y"]

            if fred_id:
                rel = self.query_fred_series(fred_id)
                if rel:
                    return rel

        return None

    @staticmethod
    def _extract_period(text: str) -> str:
        """Extract standardized period (e.g. '2026-10') from market text."""
        months = {
            "january": "01", "jan": "01",
            "february": "02", "feb": "02",
            "march": "03", "mar": "03",
            "april": "04", "apr": "04",
            "may": "05",
            "june": "06", "jun": "06",
            "july": "07", "jul": "07",
            "august": "08", "aug": "08",
            "september": "09", "sep": "09", "sept": "09",
            "october": "10", "oct": "10",
            "november": "11", "nov": "11",
            "december": "12", "dec": "12",
        }
        # Look for "October 2026", "Oct 2026", "2026-10"
        m_iso = re.search(r"\b(202\d)-(0[1-9]|1[0-2])\b", text)
        if m_iso:
            return f"{m_iso.group(1)}-{m_iso.group(2)}"

        m_named = re.search(
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[,\s]+(202\d)\b",
            text,
            re.IGNORECASE,
        )
        if m_named:
            m_str = m_named.group(1).lower()
            y_str = m_named.group(2)
            m_num = months.get(m_str, "01")
            return f"{y_str}-{m_num}"

        # If only month name is provided (e.g. "Fed Decision in December?") default to 2026
        m_single = re.search(
            r"\b(?:in|for|of)\s+(January|February|March|April|May|June|July|August|September|October|November|December)\b",
            text,
            re.IGNORECASE,
        )
        if m_single:
            m_str = m_single.group(1).lower()
            return f"2026-{months.get(m_str, '10')}"

        return "2026-10"

    @staticmethod
    def _extract_numerical_threshold(text: str) -> Tuple[Optional[float], str]:
        """Extract comparison operator and numerical threshold from market text."""
        # 1. Match explicit operator followed by number (with optional % or k)
        m_op = re.search(
            r"(&gt;=|>=|&gt;|>|&lt;=|<=|&lt;|<|above|higher than|at least|below|lower than)\s*([\d\.]+)(%|k)?",
            text,
            re.IGNORECASE,
        )
        if m_op:
            op_str = (m_op.group(1) or ">=").lower()
            val = float(m_op.group(2))

            if "below" in op_str or "<" in op_str or "lower" in op_str:
                op = "<=" if ("<=" in op_str or "or lower" in text.lower()) else "<"
            else:
                op = ">=" if (">=" in op_str or "above" in op_str or "higher than" in op_str or "at least" in op_str or "or higher" in text.lower()) else ">"

            return val, op

        # 2. Match number with explicit percentage: e.g. "2.6%"
        m_pct = re.search(r"([\d\.]+)%", text)
        if m_pct:
            return float(m_pct.group(1)), ">="

        # 3. Match number with explicit k: e.g. "150k"
        m_k = re.search(r"([\d\.]+)k\b", text, re.IGNORECASE)
        if m_k:
            return float(m_k.group(1)), ">="

        # 4. Check decimal numbers, ignoring 4-digit years (e.g. 2024, 2026)
        for token in re.findall(r"\b([\d\.]+)\b", text):
            try:
                num = float(token)
                if 2020 <= num <= 2030 and "." not in token:
                    continue  # Skip year
                return num, ">="
            except ValueError:
                pass

        return None, ">="

    @staticmethod
    def _extract_range_bracket(text: str) -> Tuple[Optional[float], Optional[float]]:
        """Extract min and max for range brackets like '100k - 150k' or '< 100k'."""
        text_lower = text.lower()

        # Less than
        m_less = re.search(r"(?:<|less than|under)\s*([\d,]+)k?", text_lower)
        if m_less:
            val = float(m_less.group(1).replace(",", ""))
            return None, val

        # Greater than
        m_greater = re.search(r"(?:>|greater than|over|more than)\s*([\d,]+)k?", text_lower)
        if m_greater:
            val = float(m_greater.group(1).replace(",", ""))
            return val, None

        # Range: "100k - 150k" or "100,000 to 150,000"
        m_range = re.search(r"([\d,]+)k?\s*(?:-|to)\s*([\d,]+)k?", text_lower)
        if m_range:
            v1 = float(m_range.group(1).replace(",", ""))
            v2 = float(m_range.group(2).replace(",", ""))
            return min(v1, v2), max(v1, v2)

        return None, None

    @staticmethod
    def _extract_fed_choice(text: str) -> Optional[str]:
        """Detect Fed FOMC rate action choices."""
        text_lower = text.lower()
        if "50+ bps cut" in text_lower or "50 bps decrease" in text_lower or "> 25 bps cut" in text_lower:
            return "50+ bps cut"
        if "25 bps cut" in text_lower or "25 bps decrease" in text_lower:
            return "25 bps cut"
        if "no change" in text_lower or "pause" in text_lower or "maintain" in text_lower:
            return "No change"
        if "25 bps hike" in text_lower or "25 bps increase" in text_lower:
            return "25 bps hike"
        if "50+ bps hike" in text_lower or "50 bps increase" in text_lower:
            return "50+ bps hike"
        return None

    @staticmethod
    def _parse_fraction_or_decimal(val_str: str) -> float:
        """Parse '4-3/4' or '5-1/4' or '5.25' into float."""
        clean = val_str.strip()
        if "/" in clean:
            # E.g. "4-3/4" or "4 3/4"
            parts = re.split(r"[\-\s]", clean)
            if len(parts) == 2:
                whole = float(parts[0])
                num, denom = parts[1].split("/")
                return whole + float(num) / float(denom)
            elif len(parts) == 1:
                num, denom = parts[0].split("/")
                return float(num) / float(denom)
        return float(clean)

    def _map_fred_to_indicator(self, series_id: str) -> MacroIndicator:
        """Map FRED series id back to MacroIndicator."""
        mapping = {
            "CPIAUCNS": MacroIndicator.CPI_HEADLINE_YOY,
            "CPIAUCSL": MacroIndicator.CPI_HEADLINE_MOM,
            "CPILFENS": MacroIndicator.CPI_CORE_YOY,
            "CPILFESL": MacroIndicator.CPI_CORE_MOM,
            "PAYEMS": MacroIndicator.NONFARM_PAYROLLS,
            "UNRATE": MacroIndicator.UNEMPLOYMENT_RATE,
            "ICSA": MacroIndicator.INITIAL_JOBLESS_CLAIMS,
            "DFEDTARU": MacroIndicator.FED_FUNDS_TARGET_UPPER,
            "DFEDTARL": MacroIndicator.FED_FUNDS_TARGET_UPPER,
            "DGS10": MacroIndicator.TREASURY_YIELD_10Y,
            "DGS2": MacroIndicator.TREASURY_YIELD_2Y,
            "DGS3MO": MacroIndicator.TREASURY_YIELD_3M,
            "DGS30": MacroIndicator.TREASURY_YIELD_30Y,
        }
        return mapping.get(series_id, MacroIndicator.CPI_HEADLINE_YOY)
