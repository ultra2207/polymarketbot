"""Weather & Climate Thresholds Oracle Adapter (Cluster 4).

Resolves Polymarket weather contracts (daily city high/low temperatures,
NOAA stations, precipitation) using multi-tiered real-time meteorological feeds:
1. Aviation Weather Center (AWC) METAR API (aviationweather.gov) - Open, 0-auth, primary Polymarket citation
2. NOAA National Weather Service (NWS) ASOS API (api.weather.gov) - 5-min cadence, powers wrh/timeseries
3. Open-Meteo API (api.open-meteo.com) - Open, 0-auth, global coordinates
4. WeatherAPI.com (api.weatherapi.com) - High-throughput CDN with Google Sign-in key

Implements the In-Play Monotonic Ratchet:
- As running max temperature breaches a bracket, all lower brackets are eliminated with p_hat=0.000.
- When target day physically concludes, winning bracket is locked with p_hat=1.000, h_reversal=0.000.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING, TYPE_CHECKING
from zoneinfo import ZoneInfo

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

log = get_logger("weather_oracle")


class WeatherMetric(str, Enum):
    """Meteorological measurement target."""
    HIGH_TEMP = "high_temperature"
    LOW_TEMP = "low_temperature"
    PRECIPITATION = "precipitation"


@dataclass(frozen=True)
class StationMetadata:
    """Metadata and geographical location for an authoritative weather reporting station."""
    icao: str
    city: str
    timezone_name: str
    default_unit: str  # "F" or "C"
    lat: float
    lon: float
    nws_id: Optional[str] = None


# Authoritative stations for top Polymarket weather markets
POPULAR_STATIONS: Dict[str, StationMetadata] = {
    # New York City
    "KLGA": StationMetadata("KLGA", "New York", "America/New_York", "F", 40.7769, -73.8740, "KLGA"),
    "KNYC": StationMetadata("KNYC", "New York", "America/New_York", "F", 40.7829, -73.9654, "KNYC"),
    "KJFK": StationMetadata("KJFK", "New York", "America/New_York", "F", 40.6413, -73.7781, "KJFK"),
    # Chicago
    "KORD": StationMetadata("KORD", "Chicago", "America/Chicago", "F", 41.9742, -87.9073, "KORD"),
    "KMDW": StationMetadata("KMDW", "Chicago", "America/Chicago", "F", 41.7868, -87.7522, "KMDW"),
    # Los Angeles
    "KLAX": StationMetadata("KLAX", "Los Angeles", "America/Los_Angeles", "F", 33.9416, -118.4085, "KLAX"),
    # San Francisco
    "KSFO": StationMetadata("KSFO", "San Francisco", "America/Los_Angeles", "F", 37.6213, -122.3790, "KSFO"),
    # Miami
    "KMIA": StationMetadata("KMIA", "Miami", "America/New_York", "F", 25.7959, -80.2870, "KMIA"),
    # Austin
    "KAUS": StationMetadata("KAUS", "Austin", "America/Chicago", "F", 30.1975, -97.6664, "KAUS"),
    # Seattle
    "KSEA": StationMetadata("KSEA", "Seattle", "America/Los_Angeles", "F", 47.4502, -122.3088, "KSEA"),
    # Las Vegas
    "KLAS": StationMetadata("KLAS", "Las Vegas", "America/Los_Angeles", "F", 36.0840, -115.1537, "KLAS"),
    # Denver
    "KDEN": StationMetadata("KDEN", "Denver", "America/Denver", "F", 39.8561, -104.6737, "KDEN"),
    # Atlanta
    "KATL": StationMetadata("KATL", "Atlanta", "America/New_York", "F", 33.6407, -84.4277, "KATL"),
    # Dallas
    "KDFW": StationMetadata("KDFW", "Dallas", "America/Chicago", "F", 32.8998, -97.0403, "KDFW"),
    # Houston
    "KIAH": StationMetadata("KIAH", "Houston", "America/Chicago", "F", 29.9902, -95.3368, "KIAH"),
    # Phoenix
    "KPHX": StationMetadata("KPHX", "Phoenix", "America/Phoenix", "F", 33.4373, -112.0078, "KPHX"),
    # Boston
    "KBOS": StationMetadata("KBOS", "Boston", "America/New_York", "F", 42.3656, -71.0096, "KBOS"),
    # Washington DC
    "KDCA": StationMetadata("KDCA", "Washington DC", "America/New_York", "F", 38.8512, -77.0402, "KDCA"),
    # International (Celsius)
    "EGLL": StationMetadata("EGLL", "London", "Europe/London", "C", 51.4700, -0.4543),
    "LFPG": StationMetadata("LFPG", "Paris", "Europe/Paris", "C", 49.0097, 2.5479),
    "RJTT": StationMetadata("RJTT", "Tokyo", "Asia/Tokyo", "C", 35.5494, 139.7798),
    "RKSI": StationMetadata("RKSI", "Seoul", "Asia/Seoul", "C", 37.4602, 126.4407),
    "EDDB": StationMetadata("EDDB", "Berlin", "Europe/Berlin", "C", 52.3667, 13.5033),
    "OMDB": StationMetadata("OMDB", "Dubai", "Asia/Dubai", "C", 25.2532, 55.3657),
    "WSSS": StationMetadata("WSSS", "Singapore", "Asia/Singapore", "C", 1.3644, 103.9915),
    "YSSY": StationMetadata("YSSY", "Sydney", "Australia/Sydney", "C", -33.9399, 151.1753),
    "VHHH": StationMetadata("VHHH", "Hong Kong", "Asia/Hong_Kong", "C", 22.3080, 113.9185),
    "CYYZ": StationMetadata("CYYZ", "Toronto", "America/Toronto", "C", 43.6777, -79.6248),
}

# City name alias mapping to canonical station
CITY_TO_DEFAULT_STATION: Dict[str, str] = {
    "new york": "KLGA",
    "new york city": "KLGA",
    "nyc": "KLGA",
    "manhattan": "KNYC",
    "central park": "KNYC",
    "jfk": "KJFK",
    "chicago": "KORD",
    "los angeles": "KLAX",
    "la": "KLAX",
    "san francisco": "KSFO",
    "miami": "KMIA",
    "austin": "KAUS",
    "seattle": "KSEA",
    "las vegas": "KLAS",
    "vegas": "KLAS",
    "denver": "KDEN",
    "atlanta": "KATL",
    "dallas": "KDFW",
    "dfw": "KDFW",
    "houston": "KIAH",
    "phoenix": "KPHX",
    "boston": "KBOS",
    "washington": "KDCA",
    "dc": "KDCA",
    "london": "EGLL",
    "paris": "LFPG",
    "tokyo": "RJTT",
    "seoul": "RKSI",
    "berlin": "EDDB",
    "dubai": "OMDB",
    "singapore": "WSSS",
    "sydney": "YSSY",
    "hong kong": "VHHH",
    "toronto": "CYYZ",
}


@dataclass
class WeatherBracket:
    """A temperature or precipitation outcome interval."""
    raw_str: str
    min_val: float
    max_val: float
    unit: str
    inclusive_min: bool = True
    inclusive_max: bool = True

    def contains(self, val: float) -> bool:
        """True if val falls inside the bracket [min_val, max_val]."""
        above_min = (val >= self.min_val) if self.inclusive_min else (val > self.min_val)
        below_max = (val <= self.max_val) if self.inclusive_max else (val < self.max_val)
        return above_min and below_max

    def is_strictly_exceeded_by(self, val: float) -> bool:
        """True if val is strictly higher than the bracket's highest possible value.
        
        If val exceeds max_val, this bracket can NEVER win a highest-temperature market!
        """
        if math.isinf(self.max_val):
            return False
        return val > self.max_val

    def is_strictly_subceded_by(self, val: float) -> bool:
        """True if val is strictly lower than the bracket's lowest possible value.
        
        If val is below min_val, this bracket can NEVER win a lowest-temperature market!
        """
        if math.isinf(self.min_val):
            return False
        return val < self.min_val


@dataclass
class StationObservation:
    """A single real-world surface weather observation."""
    station_id: str
    timestamp: datetime
    temp_c: Optional[float] = None
    temp_f: Optional[float] = None
    precip_in: Optional[float] = None
    raw_metar: Optional[str] = None
    source: str = "AWC"

    @property
    def effective_temp_f(self) -> Optional[float]:
        if self.temp_f is not None:
            return self.temp_f
        if self.temp_c is not None:
            return round(self.temp_c * 1.8 + 32.0, 2)
        return None

    @property
    def effective_temp_c(self) -> Optional[float]:
        if self.temp_c is not None:
            return self.temp_c
        if self.temp_f is not None:
            return round((self.temp_f - 32.0) / 1.8, 2)
        return None


@dataclass
class StationDaySummary:
    """Aggregated ground-truth observations for a calendar date at a station."""
    station_id: str
    target_date: str  # YYYY-MM-DD
    observations: List[StationObservation] = field(default_factory=list)
    max_temp_c: Optional[float] = None
    max_temp_f: Optional[float] = None
    min_temp_c: Optional[float] = None
    min_temp_f: Optional[float] = None
    total_precip_in: float = 0.0
    is_day_concluded: bool = False
    last_observation_time: Optional[datetime] = None


@dataclass
class WeatherMarketSpec:
    """Parsed specification of a Polymarket weather prediction contract."""
    station_id: str
    station_meta: StationMetadata
    metric: WeatherMetric
    target_date: str  # YYYY-MM-DD
    unit: str  # "F" or "C"
    bracket: Optional[WeatherBracket] = None
    is_binary_rain: bool = False
    raw_question: str = ""
    resolution_source_url: str = ""


class WeatherOracleAdapter(BaseOracleAdapter):
    """Exogenous Oracle Adapter for Cluster 4: Weather & Climate Thresholds."""

    def __init__(
        self,
        name: str = "WeatherOracleAdapter",
        cache_ttl_seconds: float = 60.0,
        request_timeout: float = 5.0,
        weather_api_key: Optional[str] = None,
    ) -> None:
        super().__init__(name=name, category=OracleCategory.WEATHER)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.request_timeout = request_timeout
        # Read API key from parameter or environment
        self.weather_api_key = (
            weather_api_key
            or os.environ.get("WEATHER_API_KEY", "")
            or os.environ.get("WEATHERAPI_KEY", "")
        )
        # In-memory cache: (station_id, target_date) -> (timestamp, StationDaySummary)
        self._summary_cache: Dict[Tuple[str, str], Tuple[float, StationDaySummary]] = {}
        log.info(f"Initialized {name} (weather_api_key_set={bool(self.weather_api_key)})")

    def can_handle(
        self,
        question: str,
        resolution_source: Optional[str] = None,
        category_hint: Optional[str] = None,
    ) -> bool:
        """Check if this adapter can resolve the given market question or resolution source."""
        if category_hint and category_hint.strip().lower() == "weather":
            return True

        res_str = (resolution_source or "").lower()
        if any(domain in res_str for domain in [
            "weather.gov",
            "aviationweather.gov",
            "weatherapi.com",
            "open-meteo.com",
            "noaa.gov",
            "wunderground.com",
        ]):
            return True

        q_lower = question.lower()
        weather_keywords = [
            "highest temperature",
            "lowest temperature",
            "high temperature",
            "low temperature",
            "temperature in",
            "temp in",
            "will it rain",
            "will it snow",
            "precipitation in",
            "rainfall in",
        ]
        return any(kw in q_lower for kw in weather_keywords)

    def parse_market_spec(
        self,
        question: str,
        resolution_source: Optional[str] = None,
    ) -> Optional[WeatherMarketSpec]:
        """Extract structured station, date, metric, and bracket from market metadata."""
        q_clean = question.strip()
        q_lower = q_clean.lower()
        res_url = resolution_source or ""

        # If resolution_source not given, check if a URL is embedded in question/ancillary text
        if not res_url:
            match_url = re.search(r"https?://[^\s,\'\"]+", q_clean)
            if match_url:
                res_url = match_url.group(0)

        # 1. Identify Station ICAO
        station_id: Optional[str] = None

        # Look in resolution source URL first (e.g. site=KLGA or ids=KLGA)
        if res_url:
            match_site = re.search(r"[?&](?:site|ids|station)=([A-Za-z0-9]{4})\b", res_url, re.IGNORECASE)
            if match_site:
                station_id = match_site.group(1).upper()

        # If not in URL, look for 4-letter ICAO code in parenthesis, e.g. "LaGuardia Airport (KLGA)"
        if not station_id:
            match_icao = re.search(r"\(([A-Z]{4})\)", q_clean)
            if match_icao and match_icao.group(1) in POPULAR_STATIONS:
                station_id = match_icao.group(1)

        # If still not found, match city alias
        if not station_id:
            for city_alias, default_stn in sorted(CITY_TO_DEFAULT_STATION.items(), key=lambda x: -len(x[0])):
                if city_alias in q_lower:
                    station_id = default_stn
                    break

        if not station_id:
            # Fallback: check if any popular station ICAO is directly in question
            for icao in POPULAR_STATIONS:
                if icao in q_clean.upper():
                    station_id = icao
                    break

        if not station_id:
            log.debug(f"Could not identify weather station for question: '{question}'")
            return None

        # Get or synthesize StationMetadata
        if station_id in POPULAR_STATIONS:
            meta = POPULAR_STATIONS[station_id]
        else:
            # Synthesize generic metadata
            is_us = station_id.startswith("K") or station_id.startswith("P")
            meta = StationMetadata(
                icao=station_id,
                city="Unknown",
                timezone_name="America/New_York" if is_us else "UTC",
                default_unit="F" if is_us else "C",
                lat=0.0,
                lon=0.0,
            )

        # 2. Identify Metric
        if "rain" in q_lower or "precipitation" in q_lower or "snow" in q_lower:
            metric = WeatherMetric.PRECIPITATION
            is_binary_rain = "will it rain" in q_lower or "will it snow" in q_lower
        elif "lowest" in q_lower or "low temp" in q_lower or "minimum temp" in q_lower:
            metric = WeatherMetric.LOW_TEMP
            is_binary_rain = False
        else:
            metric = WeatherMetric.HIGH_TEMP
            is_binary_rain = False

        # 3. Identify Target Date
        target_date_str = self._extract_target_date(q_clean, meta.timezone_name)

        # 4. Identify Unit (°F vs °C)
        unit = meta.default_unit
        if "°f" in q_lower or "fahrenheit" in q_lower:
            unit = "F"
        elif "°c" in q_lower or "celsius" in q_lower:
            unit = "C"

        # 5. Identify Bracket Range
        bracket = self._extract_bracket(q_clean, unit)

        return WeatherMarketSpec(
            station_id=station_id,
            station_meta=meta,
            metric=metric,
            target_date=target_date_str,
            unit=unit,
            bracket=bracket,
            is_binary_rain=is_binary_rain,
            raw_question=question,
            resolution_source_url=res_url,
        )

    def _extract_target_date(self, text: str, tz_name: str) -> str:
        """Extract or infer target observation date formatted as YYYY-MM-DD."""
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        now_local = datetime.now(tz)

        text_lower = text.lower()
        if "today" in text_lower:
            return now_local.strftime("%Y-%m-%d")
        if "tomorrow" in text_lower:
            return (now_local + timedelta(days=1)).strftime("%Y-%m-%d")
        if "yesterday" in text_lower:
            return (now_local - timedelta(days=1)).strftime("%Y-%m-%d")

        # Regex for Month Day, Year or Month Day (e.g. "November 7", "Nov 7, 2026", "2026-11-07")
        # ISO format:
        match_iso = re.search(r"\b(202\d-[0-1]\d-[0-3]\d)\b", text)
        if match_iso:
            return match_iso.group(1)

        months = {
            "january": 1, "jan": 1,
            "february": 2, "feb": 2,
            "march": 3, "mar": 3,
            "april": 4, "apr": 4,
            "may": 5,
            "june": 6, "jun": 6,
            "july": 7, "jul": 7,
            "august": 8, "aug": 8,
            "september": 9, "sep": 9, "sept": 9,
            "october": 10, "oct": 10,
            "november": 11, "nov": 11,
            "december": 12, "dec": 12,
        }
        pattern = r"\b(" + "|".join(months.keys()) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(202\d))?\b"
        match_date = re.search(pattern, text_lower)
        if match_date:
            m_str = match_date.group(1)
            day = int(match_date.group(2))
            year = int(match_date.group(3)) if match_date.group(3) else now_local.year
            month = months[m_str]
            return f"{year:04d}-{month:02d}-{day:02d}"

        # Default fallback to today's local date
        return now_local.strftime("%Y-%m-%d")

    def _extract_bracket(self, text: str, unit: str) -> Optional[WeatherBracket]:
        """Extract temperature bracket or threshold from outcome string or question."""
        raw = text.strip()

        # Below / Less than / Under, e.g. "Below 60°F", "<60°F", "Under 15°C", "60°F or lower"
        match_below = re.search(r"(?:below|under|<|less than)\s*([+-]?\d+(?:\.\d+)?)\s*°?[FC]?", text, re.IGNORECASE)
        if match_below:
            bound = float(match_below.group(1))
            return WeatherBracket(raw_str=raw, min_val=-math.inf, max_val=bound - 0.01, unit=unit)

        match_or_lower = re.search(r"([+-]?\d+(?:\.\d+)?)\s*°?[FC]?\s*(?:or lower|or below|or less|<=)", text, re.IGNORECASE)
        if match_or_lower:
            bound = float(match_or_lower.group(1))
            return WeatherBracket(raw_str=raw, min_val=-math.inf, max_val=bound, unit=unit)

        # Above / Greater than / Over, e.g. "68°F or higher", ">=68°F", "Over 20°C"
        match_or_higher = re.search(r"([+-]?\d+(?:\.\d+)?)\s*°?[FC]?\s*(?:or higher|or above|or more|>=|\+)", text, re.IGNORECASE)
        if match_or_higher:
            bound = float(match_or_higher.group(1))
            return WeatherBracket(raw_str=raw, min_val=bound, max_val=math.inf, unit=unit)

        match_above = re.search(r"(?:above|over|>|greater than)\s*([+-]?\d+(?:\.\d+)?)\s*°?[FC]?", text, re.IGNORECASE)
        if match_above:
            bound = float(match_above.group(1))
            return WeatherBracket(raw_str=raw, min_val=bound + 0.01, max_val=math.inf, unit=unit)

        # Range interval, e.g. "64-65°F", "64 to 65", "18 - 19°C"
        match_range = re.search(r"([+-]?\d+(?:\.\d+)?)\s*(?:-|to)\s*([+-]?\d+(?:\.\d+)?)\s*°?[FC]?", text, re.IGNORECASE)
        if match_range:
            low = float(match_range.group(1))
            high = float(match_range.group(2))
            # Typical bracket integer rounding: [low, high + 0.99]
            return WeatherBracket(raw_str=raw, min_val=low, max_val=high + 0.99, unit=unit)

        # Single integer or float degree, e.g. "17°C" or "64°F"
        match_single = re.search(r"\b([+-]?\d+(?:\.\d+)?)\s*°[FC]\b", text, re.IGNORECASE)
        if match_single:
            val = float(match_single.group(1))
            return WeatherBracket(raw_str=raw, min_val=val - 0.49, max_val=val + 0.49, unit=unit)

        return None

    def fetch_station_day(
        self,
        station_id: str,
        target_date: str,
        force_refresh: bool = False,
    ) -> Optional[StationDaySummary]:
        """Fetch and aggregate observations for station_id on target_date across available APIs."""
        cache_key = (station_id.upper(), target_date)
        now_ts = time.time()

        if not force_refresh and cache_key in self._summary_cache:
            ts, summary = self._summary_cache[cache_key]
            if now_ts - ts < self.cache_ttl_seconds:
                return summary

        # Multi-tiered fetch hierarchy
        observations: List[StationObservation] = []

        # Tier 1: Aviation Weather Center (AWC) METAR API (Zero Auth, Primary Citation)
        try:
            awc_obs = self._fetch_awc_metar(station_id=station_id, hours=36)
            if awc_obs:
                observations.extend(awc_obs)
        except Exception as e:
            log.warning(f"AWC fetch failed for {station_id}: {e}")

        # Tier 2: NWS ASOS API (Zero Auth, 5-min cadence for US stations)
        if not observations and (station_id.startswith("K") or station_id.startswith("P")):
            try:
                nws_obs = self._fetch_nws_observations(station_id=station_id, limit=50)
                if nws_obs:
                    observations.extend(nws_obs)
            except Exception as e:
                log.warning(f"NWS fetch failed for {station_id}: {e}")

        # Tier 3: Open-Meteo API (Zero Auth global fallback)
        if not observations and station_id in POPULAR_STATIONS:
            meta = POPULAR_STATIONS[station_id]
            try:
                om_obs = self._fetch_open_meteo(lat=meta.lat, lon=meta.lon, target_date=target_date)
                if om_obs:
                    observations.extend(om_obs)
            except Exception as e:
                log.warning(f"Open-Meteo fetch failed for {station_id}: {e}")

        # Tier 4: WeatherAPI.com (if key is set)
        if not observations and self.weather_api_key:
            try:
                wapi_obs = self._fetch_weatherapi(query=station_id, target_date=target_date)
                if wapi_obs:
                    observations.extend(wapi_obs)
            except Exception as e:
                log.warning(f"WeatherAPI.com fetch failed for {station_id}: {e}")

        if not observations:
            log.debug(f"No observations retrieved for station {station_id} on {target_date}")
            return None

        # Filter observations to those falling on target_date in station's local timezone
        meta = POPULAR_STATIONS.get(station_id.upper())
        tz_name = meta.timezone_name if meta else "America/New_York"
        try:
            stn_tz = ZoneInfo(tz_name)
        except Exception:
            stn_tz = timezone.utc

        day_obs: List[StationObservation] = []
        for obs in observations:
            dt_local = obs.timestamp.astimezone(stn_tz)
            if dt_local.strftime("%Y-%m-%d") == target_date:
                day_obs.append(obs)

        if not day_obs:
            log.debug(f"Zero observations matched target date {target_date} in {tz_name} for {station_id}")
            return None

        # Sort chronologically
        day_obs.sort(key=lambda x: x.timestamp)

        # Aggregate stats
        temps_c = [o.effective_temp_c for o in day_obs if o.effective_temp_c is not None]
        temps_f = [o.effective_temp_f for o in day_obs if o.effective_temp_f is not None]
        precip_vals = [o.precip_in for o in day_obs if o.precip_in is not None]

        max_c = max(temps_c) if temps_c else None
        min_c = min(temps_c) if temps_c else None
        max_f = max(temps_f) if temps_f else None
        min_f = min(temps_f) if temps_f else None
        tot_precip = sum(precip_vals) if precip_vals else 0.0

        # Check if day is physically concluded:
        # Station local time has moved into next calendar day AND last observation is past 23:00
        now_local = datetime.now(stn_tz)
        target_d = datetime.strptime(target_date, "%Y-%m-%d").date()
        is_concluded = (now_local.date() > target_d) and (day_obs[-1].timestamp.astimezone(stn_tz).hour >= 23)

        summary = StationDaySummary(
            station_id=station_id.upper(),
            target_date=target_date,
            observations=day_obs,
            max_temp_c=max_c,
            max_temp_f=max_f,
            min_temp_c=min_c,
            min_temp_f=min_f,
            total_precip_in=tot_precip,
            is_day_concluded=is_concluded,
            last_observation_time=day_obs[-1].timestamp,
        )

        # Store in cache
        self._summary_cache[cache_key] = (now_ts, summary)
        return summary

    def _fetch_awc_metar(self, station_id: str, hours: int = 36) -> List[StationObservation]:
        """Fetch METAR records from Aviation Weather Center API (Open, 0-auth)."""
        url = f"https://aviationweather.gov/api/data/metar?ids={station_id.upper()}&hours={hours}&format=json"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "PolymarketBot-Oracle/1.0 (weather_specialist; automated)"},
        )
        with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        obs_list: List[StationObservation] = []
        for item in data:
            obs_ts = item.get("obsTime")
            if not obs_ts:
                continue
            dt = datetime.fromtimestamp(obs_ts, tz=timezone.utc)
            temp_c = item.get("temp")
            raw_ob = item.get("rawOb", "")

            # Check precip in METAR string (-RA, +RA, RA, -SN, +SN, SN, -DZ, TSRA, SHRA, etc.)
            precip_in = 0.0
            if re.search(r"(?:^|\s)(?:[-+]?(?:RA|DZ|SN|SG|PL|GR|GS|UP)|TSRA|SHRA|FZRA|TSSN|SHSN)(?:\s|$)", raw_ob):
                precip_in = 0.01  # Detectable precipitation event

            obs_list.append(
                StationObservation(
                    station_id=station_id.upper(),
                    timestamp=dt,
                    temp_c=float(temp_c) if temp_c is not None else None,
                    precip_in=precip_in,
                    raw_metar=raw_ob,
                    source="AWC_METAR",
                )
            )
        return obs_list

    def _fetch_nws_observations(self, station_id: str, limit: int = 50) -> List[StationObservation]:
        """Fetch ASOS observations from NOAA National Weather Service API (Zero Auth)."""
        url = f"https://api.weather.gov/stations/{station_id.upper()}/observations?limit={limit}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "(polymarketbot, oracle-specialist@polymarketbot.local)"},
        )
        with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        obs_list: List[StationObservation] = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            ts_str = props.get("timestamp")
            if not ts_str:
                continue
            # Parse ISO timestamp
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            t_obj = props.get("temperature", {})
            temp_c = t_obj.get("value")
            precip_obj = props.get("precipitationLastHour", {})
            precip_val = precip_obj.get("value")  # in meters
            precip_in = (precip_val * 39.3701) if precip_val is not None else 0.0

            obs_list.append(
                StationObservation(
                    station_id=station_id.upper(),
                    timestamp=dt,
                    temp_c=float(temp_c) if temp_c is not None else None,
                    precip_in=precip_in,
                    raw_metar=props.get("rawMessage"),
                    source="NOAA_NWS",
                )
            )
        return obs_list

    def _fetch_open_meteo(self, lat: float, lon: float, target_date: str) -> List[StationObservation]:
        """Fetch hourly temperature history from Open-Meteo (Open API, 0-auth)."""
        url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat:.4f}&longitude={lon:.4f}"
            f"&start_date={target_date}&end_date={target_date}&hourly=temperature_2m,precipitation"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "PolymarketBot-Oracle/1.0"},
        )
        with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        precips = hourly.get("precipitation", [])

        obs_list: List[StationObservation] = []
        for t_str, t_val, p_val in zip(times, temps, precips):
            dt = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
            precip_in = (p_val / 25.4) if p_val is not None else 0.0
            obs_list.append(
                StationObservation(
                    station_id="OPEN_METEO",
                    timestamp=dt,
                    temp_c=float(t_val) if t_val is not None else None,
                    precip_in=precip_in,
                    source="OPEN_METEO",
                )
            )
        return obs_list

    def _fetch_weatherapi(self, query: str, target_date: str) -> List[StationObservation]:
        """Fetch historical day data from WeatherAPI.com (requires Google Sign-in key)."""
        if not self.weather_api_key:
            return []

        url = f"https://api.weatherapi.com/v1/history.json?key={self.weather_api_key}&q={query}&dt={target_date}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "PolymarketBot-Oracle/1.0"},
        )
        with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        forecast_days = data.get("forecast", {}).get("forecastday", [])
        obs_list: List[StationObservation] = []
        for fday in forecast_days:
            for hour in fday.get("hour", []):
                t_epoch = hour.get("time_epoch")
                if not t_epoch:
                    continue
                dt = datetime.fromtimestamp(t_epoch, tz=timezone.utc)
                obs_list.append(
                    StationObservation(
                        station_id=query.upper(),
                        timestamp=dt,
                        temp_c=hour.get("temp_c"),
                        temp_f=hour.get("temp_f"),
                        precip_in=hour.get("precip_in"),
                        source="WEATHERAPI_COM",
                    )
                )
        return obs_list

    def evaluate(self, candidate: Domain2Candidate) -> Optional[GroundTruthOutcome]:
        """Evaluate a Domain2Candidate and return ground truth if conclusion or live state is detected.
        
        Applies the In-Play Monotonic Ratchet:
        - If candidate bracket is already exceeded by running max temp, p_hat = 0.000 (deterministic loser).
        - If target calendar day is completed, winning bracket is identified with p_hat = 1.000.
        """
        spec = self.parse_market_spec(
            question=candidate.question,
            resolution_source=getattr(candidate, "resolution_source", None),
        )
        if not spec:
            return None

        # Fetch observations
        summary = self.fetch_station_day(
            station_id=spec.station_id,
            target_date=spec.target_date,
        )
        if not summary or not summary.observations:
            return None

        # Target temperature / metric values
        if spec.unit == "F":
            running_max = summary.max_temp_f
            running_min = summary.min_temp_f
        else:
            running_max = summary.max_temp_c
            running_min = summary.min_temp_c

        # Case 1: Binary Precipitation / Rain Threshold
        if spec.metric == WeatherMetric.PRECIPITATION:
            precip_threshold = 0.01  # Trace amount (0.01 inches) constitutes rain
            rain_occurred = summary.total_precip_in >= precip_threshold

            if rain_occurred:
                # Monotonic: Once rain has fallen, it cannot be undone!
                log.info(
                    f"[WEATHER MONOTONIC RATIO] Rain confirmed at {spec.station_id} on {spec.target_date}! "
                    f"Precip={summary.total_precip_in:.2f} in >= {precip_threshold} in. p_hat=1.000"
                )
                return GroundTruthOutcome(
                    event_id=candidate.condition_id,
                    category=self.category,
                    status=OracleStatus.CONCLUDED,
                    concluded=True,
                    winning_outcome="Yes",
                    p_hat=1.000,
                    h_reversal=0.000,
                    confidence=1.000,
                    resolution_source_url=spec.resolution_source_url or "https://aviationweather.gov",
                    ancillary_details={
                        "station_id": spec.station_id,
                        "date": spec.target_date,
                        "total_precip_in": summary.total_precip_in,
                    },
                )
            elif summary.is_day_concluded:
                # Day completed with no rain
                return GroundTruthOutcome(
                    event_id=candidate.condition_id,
                    category=self.category,
                    status=OracleStatus.CONCLUDED,
                    concluded=True,
                    winning_outcome="No",
                    p_hat=0.000,
                    h_reversal=0.000,
                    confidence=1.000,
                    resolution_source_url=spec.resolution_source_url or "https://aviationweather.gov",
                    ancillary_details={
                        "station_id": spec.station_id,
                        "date": spec.target_date,
                        "total_precip_in": summary.total_precip_in,
                    },
                )
            else:
                # Still in-play
                return GroundTruthOutcome(
                    event_id=candidate.condition_id,
                    category=self.category,
                    status=OracleStatus.IN_PLAY,
                    concluded=False,
                    p_hat=candidate.price,
                    confidence=0.500,
                    ancillary_details={
                        "station_id": spec.station_id,
                        "date": spec.target_date,
                        "total_precip_in": summary.total_precip_in,
                    },
                )

        # Case 2: Daily High Temperature Brackets
        if spec.metric == WeatherMetric.HIGH_TEMP and spec.bracket is not None and running_max is not None:
            # Check 2A: The In-Play Monotonic Ratchet (Dead Bracket Elimination)
            # If the current running max already exceeds the bracket's ceiling, this bracket CAN NEVER WIN!
            if spec.bracket.is_strictly_exceeded_by(running_max):
                log.info(
                    f"[WEATHER DEAD BRACKET DETECTED] {spec.station_id} on {spec.target_date}: "
                    f"Bracket '{spec.bracket.raw_str}' (max {spec.bracket.max_val}{spec.unit}) "
                    f"is EXCEEDED by running max {running_max:.1f}{spec.unit}! Setting p_hat=0.000"
                )
                return GroundTruthOutcome(
                    event_id=candidate.condition_id,
                    category=self.category,
                    status=OracleStatus.CONCLUDED,
                    concluded=True,
                    winning_outcome="No",
                    p_hat=0.000,
                    h_reversal=0.000,
                    confidence=1.000,
                    resolution_source_url=spec.resolution_source_url or "https://aviationweather.gov",
                    ancillary_details={
                        "station_id": spec.station_id,
                        "date": spec.target_date,
                        "running_max": running_max,
                        "bracket": spec.bracket.raw_str,
                        "reason": "bracket_ceiling_exceeded_monotonically",
                    },
                )

            # Check 2B: Day Concluded (Final Ground Truth Winner)
            if summary.is_day_concluded:
                winner = spec.bracket.contains(running_max)
                p_win = 1.000 if winner else 0.000
                outcome_str = "Yes" if winner else "No"
                log.info(
                    f"[WEATHER DAY CONCLUDED] {spec.station_id} on {spec.target_date}: Final High = {running_max:.1f}{spec.unit}. "
                    f"Bracket '{spec.bracket.raw_str}' -> {outcome_str} (p_hat={p_win:.3f})"
                )
                return GroundTruthOutcome(
                    event_id=candidate.condition_id,
                    category=self.category,
                    status=OracleStatus.CONCLUDED,
                    concluded=True,
                    winning_outcome=outcome_str,
                    p_hat=p_win,
                    h_reversal=0.000,
                    confidence=1.000,
                    resolution_source_url=spec.resolution_source_url or "https://aviationweather.gov",
                    ancillary_details={
                        "station_id": spec.station_id,
                        "date": spec.target_date,
                        "final_high": running_max,
                        "bracket": spec.bracket.raw_str,
                    },
                )

            # Check 2C: In-Play Live State
            return GroundTruthOutcome(
                event_id=candidate.condition_id,
                category=self.category,
                status=OracleStatus.IN_PLAY,
                concluded=False,
                p_hat=candidate.price,
                confidence=0.500,
                ancillary_details={
                    "station_id": spec.station_id,
                    "date": spec.target_date,
                    "running_max": running_max,
                    "obs_count": len(summary.observations),
                },
            )

        # Case 3: Daily Low Temperature Brackets
        if spec.metric == WeatherMetric.LOW_TEMP and spec.bracket is not None and running_min is not None:
            # Monotonic ratchet for low temp: if running min is already below bracket floor, it can never win
            if spec.bracket.is_strictly_subceded_by(running_min):
                return GroundTruthOutcome(
                    event_id=candidate.condition_id,
                    category=self.category,
                    status=OracleStatus.CONCLUDED,
                    concluded=True,
                    winning_outcome="No",
                    p_hat=0.000,
                    h_reversal=0.000,
                    confidence=1.000,
                    resolution_source_url=spec.resolution_source_url or "https://aviationweather.gov",
                    ancillary_details={
                        "station_id": spec.station_id,
                        "date": spec.target_date,
                        "running_min": running_min,
                        "bracket": spec.bracket.raw_str,
                        "reason": "bracket_floor_subceded_monotonically",
                    },
                )

            if summary.is_day_concluded:
                winner = spec.bracket.contains(running_min)
                p_win = 1.000 if winner else 0.000
                return GroundTruthOutcome(
                    event_id=candidate.condition_id,
                    category=self.category,
                    status=OracleStatus.CONCLUDED,
                    concluded=True,
                    winning_outcome="Yes" if winner else "No",
                    p_hat=p_win,
                    h_reversal=0.000,
                    confidence=1.000,
                    resolution_source_url=spec.resolution_source_url or "https://aviationweather.gov",
                    ancillary_details={
                        "station_id": spec.station_id,
                        "date": spec.target_date,
                        "final_low": running_min,
                        "bracket": spec.bracket.raw_str,
                    },
                )

        return None

    def poll_active_events(self) -> Dict[str, GroundTruthOutcome]:
        """Poll upstream API for all currently tracked major city stations."""
        active_outcomes: Dict[str, GroundTruthOutcome] = {}
        target_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for icao in list(POPULAR_STATIONS.keys())[:6]:  # Batch first 6 primary airports
            try:
                summary = self.fetch_station_day(station_id=icao, target_date=target_today)
                if summary:
                    active_outcomes[f"WEATHER_{icao}_{target_today}"] = GroundTruthOutcome(
                        event_id=f"WEATHER_{icao}_{target_today}",
                        category=self.category,
                        status=OracleStatus.CONCLUDED if summary.is_day_concluded else OracleStatus.IN_PLAY,
                        concluded=summary.is_day_concluded,
                        winning_outcome=f"High={summary.max_temp_f}°F" if summary.max_temp_f else "",
                        observed_at=summary.last_observation_time or datetime.now(timezone.utc),
                        ancillary_details={
                            "station": icao,
                            "running_max_f": summary.max_temp_f,
                            "running_min_f": summary.min_temp_f,
                            "total_precip_in": summary.total_precip_in,
                            "obs_count": len(summary.observations),
                        },
                    )
            except Exception as e:
                log.warning(f"Error polling station {icao}: {e}")

        return active_outcomes

    def parse_ancillary_data(self, ancillary: Any) -> ParsedAncillaryData:
        """Parse raw UMA ancillary data bytes/string into structured parameters."""
        text = ancillary.decode("utf-8", errors="ignore") if isinstance(ancillary, bytes) else str(ancillary)
        spec = self.parse_market_spec(question=text)
        strike_val = spec.bracket.max_val if (spec and spec.bracket) else None
        return ParsedAncillaryData(
            raw_ancillary=text,
            title=text[:100],
            asset=spec.station_id if spec else "WEATHER",
            strike_price=strike_val,
            source_feed=spec.resolution_source_url if spec else "https://aviationweather.gov",
            raw_fields={"spec": spec},
        )

    def evaluate_market_resolution(
        self,
        market_info: Dict[str, Any],
        current_prices: Optional[Dict[str, float]] = None,
        as_of_timestamp: Optional[float] = None,
    ) -> ResolutionVerdict:
        """Evaluate real-time or prospective resolution state for a weather market."""
        question = market_info.get("question", "")
        cid = market_info.get("condition_id", "weather_market")
        res_source = market_info.get("resolution_source")

        spec = self.parse_market_spec(question, resolution_source=res_source)
        if not spec:
            return ResolutionVerdict(
                condition_id=cid,
                outcome=ResolutionOutcome.UNKNOWN,
                p_yes=0.5,
                rationale="Could not identify weather station or criteria",
            )

        summary = self.fetch_station_day(spec.station_id, spec.target_date)
        if not summary or not summary.observations:
            return ResolutionVerdict(
                condition_id=cid,
                outcome=ResolutionOutcome.PENDING,
                p_yes=0.5,
                rationale="Awaiting observation reports",
            )

        running_peak = summary.max_temp_f if spec.unit == "F" else summary.max_temp_c
        if spec.bracket and running_peak is not None:
            if spec.bracket.is_strictly_exceeded_by(running_peak):
                return ResolutionVerdict(
                    condition_id=cid,
                    outcome=ResolutionOutcome.NO,
                    p_yes=0.0,
                    settlement_price=running_peak,
                    is_deterministic=True,
                    rationale=f"Bracket ceiling {spec.bracket.max_val}{spec.unit} exceeded by running max {running_peak:.1f}{spec.unit}",
                )
            if summary.is_day_concluded:
                winner = spec.bracket.contains(running_peak)
                return ResolutionVerdict(
                    condition_id=cid,
                    outcome=ResolutionOutcome.YES if winner else ResolutionOutcome.NO,
                    p_yes=1.0 if winner else 0.0,
                    settlement_price=running_peak,
                    is_deterministic=True,
                    rationale=f"Day concluded with high {running_peak:.1f}{spec.unit}",
                )

        return ResolutionVerdict(
            condition_id=cid,
            outcome=ResolutionOutcome.PENDING,
            p_yes=0.5,
            settlement_price=running_peak,
            rationale="In-play observations ongoing",
        )

    async def get_latest_price(self, asset: str) -> PricePoint:
        """Fetch latest surface temperature observation as a PricePoint."""
        station_id = asset.upper()
        target_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        summary = self.fetch_station_day(station_id, target_today)
        if summary and summary.observations:
            latest_obs = summary.observations[-1]
            temp = latest_obs.effective_temp_f or 0.0
            return PricePoint(
                asset=station_id,
                price=temp,
                timestamp_ms=int(latest_obs.timestamp.timestamp() * 1000),
                source=OracleFeedSource.AWC_METAR,
                confidence=1.0,
            )
        return PricePoint(
            asset=station_id,
            price=0.0,
            timestamp_ms=int(time.time() * 1000),
            source=OracleFeedSource.AWC_METAR,
            confidence=0.0,
        )

    async def get_historical_price(
        self, asset: str, timestamp_s: float
    ) -> Optional[PricePoint]:
        """Fetch historical observation nearest to timestamp."""
        station_id = asset.upper()
        dt = datetime.fromtimestamp(timestamp_s, tz=timezone.utc)
        target_date = dt.strftime("%Y-%m-%d")
        summary = self.fetch_station_day(station_id, target_date)
        if summary and summary.observations:
            closest = min(summary.observations, key=lambda o: abs(o.timestamp.timestamp() - timestamp_s))
            temp = closest.effective_temp_f or 0.0
            return PricePoint(
                asset=station_id,
                price=temp,
                timestamp_ms=int(closest.timestamp.timestamp() * 1000),
                source=OracleFeedSource.AWC_METAR,
                confidence=1.0,
            )
        return None
