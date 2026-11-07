"""Base Oracle Abstraction and Resolution Lifecycle Interfaces.

Defines the core domain contracts for Polymarket oracle data collection,
ancillary data parsing, multi-venue reference price aggregation, and
probabilistic/deterministic resolution evaluation across all market clusters.
"""

from __future__ import annotations

import math
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from polymarketbot.logs import get_logger


class OracleCategory(str, Enum):
    """Categorization of Polymarket prediction market clusters."""
    CRYPTO = "CRYPTO"       # Cluster 1: Crypto Fast-Expiry & Strike Markets
    SPORTS = "SPORTS"       # Cluster 3: Sports In-Play & Match Finality
    WEATHER = "WEATHER"     # Cluster 4: Meteorological & NOAA Thresholds
    POLITICS = "POLITICS"   # Cluster 2: Elections, Legislative, AP/Decision Desk
    MACRO = "MACRO"         # Cluster 5: Fed Rate, CPI, BLS, Economic Releases
    OTHER = "OTHER"


class OracleStatus(str, Enum):
    """Real-world lifecycle status of an event."""
    PENDING = "PENDING"     # Pre-event or scheduled
    IN_PLAY = "IN_PLAY"     # Event currently underway / active
    CONCLUDED = "CONCLUDED" # Event physically concluded / final whistle / candle closed
    DISPUTED = "DISPUTED"   # Under UMA dispute or official review
    UNKNOWN = "UNKNOWN"


class OracleFeedSource(str, Enum):
    """External oracle and market data reference feeds."""
    BINANCE_SPOT = "BINANCE_SPOT"
    BINANCE_FUTURES = "BINANCE_FUTURES"
    PYTH_HERMES = "PYTH_HERMES"
    COINBASE_SPOT = "COINBASE_SPOT"
    CHAINLINK_ONCHAIN = "CHAINLINK_ONCHAIN"
    CHAINLINK_DATA_STREAMS = "CHAINLINK_DATA_STREAMS"
    UMA_OPTIMISTIC = "UMA_OPTIMISTIC"
    ESPN = "ESPN"
    NOAA = "NOAA"
    AWC_METAR = "AWC_METAR"
    AWC = "AWC"
    OPEN_METEO = "OPEN_METEO"
    WEATHERAPI_COM = "WEATHERAPI_COM"
    NWS = "NWS"
    FRED_API = "FRED_API"
    BLS_GOV = "BLS_GOV"
    FEDERAL_RESERVE = "FEDERAL_RESERVE"
    US_TREASURY = "US_TREASURY"
    US_DOL = "US_DOL"
    SYNTHETIC_MOCK = "SYNTHETIC_MOCK"


class ResolutionOutcome(str, Enum):
    """Canonical resolution verdicts for binary and strike markets."""
    YES = "YES"             # Up / Yes / High / True -> Payout $1.00 to YES
    NO = "NO"               # Down / No / Low / False -> Payout $1.00 to NO
    TIE = "TIE"             # Exact tie (Polymarket standard resolves to YES/UP)
    PENDING = "PENDING"     # Market active, outcome undecided
    DISPUTED = "DISPUTED"   # UMA proposal disputed, pending DVM vote
    UNKNOWN = "UNKNOWN"     # Unparseable / insufficient data


@dataclass(frozen=True)
class PricePoint:
    """Standardized price observation from an oracle feed."""
    asset: str
    price: float
    timestamp_ms: int
    source: OracleFeedSource
    confidence: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def timestamp_s(self) -> float:
        return self.timestamp_ms / 1000.0


@dataclass
class ParsedAncillaryData:
    """Structured representation of UMA or Polymarket resolution criteria."""
    raw_ancillary: str
    title: str = ""
    asset: Optional[str] = None
    strike_price: Optional[float] = None
    comparison_operator: str = ">="  # '>=', '>', '<=', '<', 'UP_OR_DOWN'
    resolution_timestamp: Optional[float] = None
    benchmark_timestamp: Optional[float] = None
    source_feed: Optional[str] = None
    is_twap: bool = False
    twap_window_seconds: float = 60.0
    early_resolution_allowed: bool = False
    raw_fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolutionVerdict:
    """Evaluation result for a market resolution state."""
    condition_id: str
    outcome: ResolutionOutcome
    p_yes: float                            # Probability [0.0, 1.0] that YES/UP pays out
    settlement_price: Optional[float] = None # Current/final settlement price observed
    benchmark_price: Optional[float] = None  # Opening strike / baseline price
    confidence: float = 1.0                 # Statistical confidence [0.0, 1.0]
    rationale: str = ""
    is_deterministic: bool = False          # True if outcome is mathematically locked
    seconds_remaining: float = 0.0          # Time to market expiry


@dataclass
class GroundTruthOutcome:
    """Ground truth resolution state for Domain 2 / In-Play harvesting."""
    event_id: str
    category: OracleCategory
    status: OracleStatus
    concluded: bool
    winning_outcome: Optional[str] = None
    p_hat: float = 0.5
    h_reversal: float = 0.0
    confidence: float = 1.0
    resolution_source_url: str = ""
    ancillary_details: Dict[str, Any] = field(default_factory=dict)
    timestamp_s: float = field(default_factory=time.time)
    observed_at: Optional[Any] = None

    @property
    def is_deterministic_conclusion(self) -> bool:
        """True if the event has physically concluded and winner is certain."""
        return (
            self.status == OracleStatus.CONCLUDED
            and self.concluded
            and (self.p_hat >= 0.999 or self.p_hat <= 0.001)
        )


class BaseOracleAdapter(ABC):
    """Abstract Base Class for Polymarket resolution adapters."""

    def __init__(
        self,
        name: str = "base_oracle",
        category: OracleCategory = OracleCategory.CRYPTO,
    ) -> None:
        self.name = name
        self.category = category
        self.logger = get_logger(f"oracle.{name}")

    def can_handle(
        self,
        question: str,
        resolution_source: Optional[str] = None,
        category: Optional[str] = None,
    ) -> bool:
        """Return True if this adapter can resolve the given question/market."""
        return False

    def evaluate(self, candidate: Any) -> Optional[GroundTruthOutcome]:
        """Evaluate a Domain2Candidate or market candidate against exogenous truth."""
        return None

    async def get_latest_price(self, asset: str) -> PricePoint:
        """Fetch latest real-time reference price for asset (e.g. BTC, ETH)."""
        raise NotImplementedError("Price lookup not supported by this oracle adapter")

    async def get_historical_price(
        self, asset: str, timestamp_s: float
    ) -> Optional[PricePoint]:
        """Fetch historical price at exact unix timestamp."""
        raise NotImplementedError("Historical price lookup not supported by this oracle adapter")

    def parse_ancillary_data(self, ancillary: Union[str, bytes]) -> ParsedAncillaryData:
        """Parse raw UMA ancillary data bytes/string into structured parameters."""
        raise NotImplementedError

    def evaluate_market_resolution(
        self,
        market_info: Dict[str, Any],
        current_prices: Optional[Dict[str, float]] = None,
        as_of_timestamp: Optional[float] = None,
    ) -> ResolutionVerdict:
        """Evaluate real-time or prospective resolution state for a market."""
        raise NotImplementedError

    @staticmethod
    def calculate_twap(
        samples: Sequence[Tuple[float, float]],
        start_ts: float,
        end_ts: float,
    ) -> Optional[float]:
        """Compute exact Time-Weighted Average Price (TWAP) over [start_ts, end_ts].
        
        Uses linear interpolation at boundaries and trapezoidal numerical integration.

        Args:
            samples: List of (unix_timestamp_s, price) tuples.
            start_ts: Window start unix timestamp.
            end_ts: Window end unix timestamp (end_ts > start_ts).

        Returns:
            Computed TWAP float, or None if insufficient samples.
        """
        if end_ts <= start_ts or not samples:
            return None

        # Sort samples chronologically
        sorted_samples = sorted(samples, key=lambda s: s[0])
        window_duration = end_ts - start_ts

        def interp_price(t_target: float) -> float:
            """Interpolate price at target timestamp."""
            if t_target <= sorted_samples[0][0]:
                return sorted_samples[0][1]
            if t_target >= sorted_samples[-1][0]:
                return sorted_samples[-1][1]
            for i in range(len(sorted_samples) - 1):
                t1, p1 = sorted_samples[i]
                t2, p2 = sorted_samples[i + 1]
                if t1 <= t_target <= t2:
                    if t2 == t1:
                        return p1
                    alpha = (t_target - t1) / (t2 - t1)
                    return p1 + alpha * (p2 - p1)
            return sorted_samples[-1][1]

        # Assemble effective integration points strictly within [start_ts, end_ts]
        effective_points: List[Tuple[float, float]] = [(start_ts, interp_price(start_ts))]
        for t, p in sorted_samples:
            if start_ts < t < end_ts:
                effective_points.append((t, p))
        effective_points.append((end_ts, interp_price(end_ts)))

        # Trapezoidal numerical integration
        accumulated_area = 0.0
        for i in range(len(effective_points) - 1):
            t1, p1 = effective_points[i]
            t2, p2 = effective_points[i + 1]
            dt = t2 - t1
            if dt > 0:
                accumulated_area += 0.5 * (p1 + p2) * dt

        return accumulated_area / window_duration
