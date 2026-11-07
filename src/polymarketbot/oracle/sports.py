"""Sports In-Play & Final Outcome Oracle Adapter for Domain 2.

Integrates real-world sports feeds (NBA, NFL, MLB, EPL/Soccer, Tennis, UFC/MMA, Esports)
to harvest pre-resolution and settlement discounts before on-chain UMA proposals.

Key Sources:
1. ESPN Hidden Public Scoreboard API (Zero auth, 100% free, real-time status/clock/scores)
2. BallDontLie API (NBA/NFL/MLB, requires free signup/Google sign-in)
3. TheSportsDB API (Multi-sport metadata, requires free/patreon key)
4. PandaScore API (Esports CS2/LoL/Dota2, requires free signup/Google sign-in)
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

import requests

from polymarketbot.logs import get_logger
from polymarketbot.oracle.base import (
    BaseOracleAdapter,
    GroundTruthOutcome,
    OracleCategory,
    OracleStatus,
    ParsedAncillaryData,
    PricePoint,
    ResolutionOutcome,
    ResolutionVerdict,
)

if TYPE_CHECKING:
    from polymarketbot.trading.domain2.ortools_allocator import Domain2Candidate

log = get_logger("oracle.sports")

# --------------------------------------------------------------------------- #
# Supported Sport & League Configuration
# --------------------------------------------------------------------------- #

ESPN_BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_FALLBACK_URL = "https://site.web.api.espn.com/apis/site/v2/sports"

# Sport & League path mapping for ESPN Scoreboard API
LEAGUE_PATH_MAP: Dict[str, str] = {
    # Basketball
    "nba": "basketball/nba",
    "wnba": "basketball/wnba",
    "mens-college-basketball": "basketball/mens-college-basketball",
    # American Football
    "nfl": "football/nfl",
    "college-football": "football/college-football",
    # Baseball
    "mlb": "baseball/mlb",
    # Soccer
    "epl": "soccer/eng.1",
    "premier-league": "soccer/eng.1",
    "uefa.champions": "soccer/uefa.champions",
    "champions-league": "soccer/uefa.champions",
    "la-liga": "soccer/esp.1",
    "bundesliga": "soccer/ger.1",
    "serie-a": "soccer/ita.1",
    "mls": "soccer/usa.1",
    # Tennis
    "tennis-atp": "tennis/atp",
    "tennis-wta": "tennis/wta",
    "atp": "tennis/atp",
    "wta": "tennis/wta",
    # MMA / Combat Sports
    "ufc": "mma/ufc",
    "mma": "mma/ufc",
}

# Standard final status names across ESPN feeds
FINAL_STATUS_NAMES = frozenset({
    "STATUS_FINAL",
    "STATUS_FINAL_OT",
    "STATUS_FULL_TIME",
    "STATUS_FINAL_PEN",
    "STATUS_END_OF_EXTRATIME",
})

IN_PLAY_STATUS_NAMES = frozenset({
    "STATUS_IN_PROGRESS",
    "STATUS_HALFTIME",
    "STATUS_END_OF_PERIOD",
    "STATUS_FIRST_HALF",
    "STATUS_SECOND_HALF",
})

POSTPONED_OR_CANCELED_STATUS_NAMES = frozenset({
    "STATUS_POSTPONED",
    "STATUS_CANCELED",
    "STATUS_SUSPENDED",
    "STATUS_ABANDONED",
    "STATUS_RAIN_DELAY",
})

# Known team aliases and abbreviations for fuzzy matching
TEAM_ALIASES: Dict[str, str] = {
    # NBA
    "celtics": "boston celtics",
    "lakers": "los angeles lakers",
    "warriors": "golden state warriors",
    "knicks": "new york knicks",
    "heat": "miami heat",
    "bucks": "milwaukee bucks",
    "sixers": "philadelphia 76ers",
    "76ers": "philadelphia 76ers",
    "mavs": "dallas mavericks",
    "mavericks": "dallas mavericks",
    "nuggets": "denver nuggets",
    "suns": "phoenix suns",
    "clippers": "la clippers",
    "timberwolves": "minnesota timberwolves",
    "wolves": "minnesota timberwolves",
    "thunder": "oklahoma city thunder",
    "okc": "oklahoma city thunder",
    "cavs": "cleveland cavaliers",
    "cavaliers": "cleveland cavaliers",
    "pacers": "indiana pacers",
    "magic": "orlando magic",
    "pistons": "detroit pistons",
    "hornets": "charlotte hornets",
    "hawks": "atlanta hawks",
    "bulls": "chicago bulls",
    "nets": "brooklyn nets",
    "raptors": "toronto raptors",
    "wizards": "washington wizards",
    "pelicans": "new orleans pelicans",
    "rockets": "houston rockets",
    "grizzlies": "memphis grizzlies",
    "spurs": "san antonio spurs",
    "jazz": "utah jazz",
    "kings": "sacramento kings",
    "trail blazers": "portland trail blazers",
    "blazers": "portland trail blazers",
    # NFL
    "chiefs": "kansas city chiefs",
    "eagles": "philadelphia eagles",
    "49ers": "san francisco 49ers",
    "niners": "san francisco 49ers",
    "ravens": "baltimore ravens",
    "bills": "buffalo bills",
    "cowboys": "dallas cowboys",
    "lions": "detroit lions",
    "packers": "green bay packers",
    "texans": "houston texans",
    "dolphins": "miami dolphins",
    "steelers": "pittsburgh steelers",
    "bengals": "cincinnati bengals",
    "rams": "los angeles rams",
    "chargers": "los angeles chargers",
    "patriots": "new england patriots",
    "pats": "new england patriots",
    "vikings": "minnesota vikings",
    "falcons": "atlanta falcons",
    "saints": "new orleans saints",
    "buccaneers": "tampa bay buccaneers",
    "bucs": "tampa bay buccaneers",
    "commanders": "washington commanders",
    "bears": "chicago bears",
    "broncos": "denver broncos",
    "raiders": "las vegas raiders",
    "seahawks": "seattle seahawks",
    "cardinals": "arizona cardinals",
    "titans": "tennessee titans",
    "colts": "indianapolis colts",
    "jaguars": "jacksonville jaguars",
    "jags": "jacksonville jaguars",
    "jets": "new york jets",
    "giants": "new york giants",
    "browns": "cleveland browns",
    "panthers": "carolina panthers",
    # EPL / European Soccer
    "arsenal": "arsenal",
    "chelsea": "chelsea",
    "liverpool": "liverpool",
    "man city": "manchester city",
    "manchester city": "manchester city",
    "man utd": "manchester united",
    "manchester united": "manchester united",
    "tottenham": "tottenham hotspur",
    "spurs": "tottenham hotspur",
    "newcastle": "newcastle united",
    "aston villa": "aston villa",
    "west ham": "west ham united",
    "brighton": "brighton & hove albion",
    "real madrid": "real madrid",
    "barcelona": "barcelona",
    "barca": "barcelona",
    "bayern": "bayern munich",
    "bayern munich": "bayern munich",
    "psg": "paris saint-germain",
    "juventus": "juventus",
    "inter": "inter milan",
    "milan": "ac milan",
}


@dataclass
class NormalizedMatchEvent:
    """Internal normalized representation of a real-world sports match or bout."""
    event_id: str
    sport_league: str
    competitor_a: str
    competitor_b: str
    score_a: float = 0.0
    score_b: float = 0.0
    status: OracleStatus = OracleStatus.PENDING
    status_detail: str = "STATUS_SCHEDULED"
    completed: bool = False
    winning_competitor: Optional[str] = None
    clock_detail: str = ""
    period: int = 0
    start_time: Optional[datetime] = None
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_payload: Dict[str, Any] = field(default_factory=dict)


def normalize_name(text: str) -> str:
    """Normalize a team or athlete name for fuzzy alias lookup."""
    clean = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
    clean = re.sub(r"\s+", " ", clean).strip()
    return TEAM_ALIASES.get(clean, clean)


def match_competitor(query: str, target_name: str) -> bool:
    """Check if query string matches target name via exact, substring, or alias matching."""
    norm_query = normalize_name(query)
    norm_target = normalize_name(target_name)

    if norm_query == norm_target:
        return True
    if norm_query in norm_target or norm_target in norm_query:
        return True

    # Check individual token overlap for multi-word team/athlete names
    q_tokens = set(norm_query.split())
    t_tokens = set(norm_target.split())
    # Exclude trivial stop words
    stop_words = {"fc", "united", "city", "de", "la", "st", "los", "angeles", "new", "york"}
    sig_q = q_tokens - stop_words
    sig_t = t_tokens - stop_words

    if sig_q and sig_t and (sig_q.issubset(sig_t) or sig_t.issubset(sig_q)):
        return True

    return False


# --------------------------------------------------------------------------- #
# Sports Oracle Adapter Implementation
# --------------------------------------------------------------------------- #

class SportsOracleAdapter(BaseOracleAdapter):
    """Production real-world oracle adapter for sports markets.
    
    Supports:
    - Basketball (NBA, WNBA, NCAA)
    - American Football (NFL, CFB)
    - Baseball (MLB)
    - Soccer (EPL, Champions League, La Liga, Bundesliga, Serie A, MLS)
    - Tennis (ATP, WTA)
    - MMA / UFC
    - Esports (CS2, LoL, Dota2 via PandaScore API / mock fallback)
    """

    def __init__(
        self,
        use_live_network: bool = True,
        cache_ttl_seconds: int = 15,
        request_timeout_seconds: float = 4.0,
        espn_base_url: str = ESPN_BASE_URL,
    ) -> None:
        super().__init__(name="sports_oracle", category=OracleCategory.SPORTS)
        self.use_live_network = use_live_network
        self.cache_ttl_seconds = cache_ttl_seconds
        self.request_timeout = request_timeout_seconds
        self.espn_base_url = espn_base_url

        # Cache of parsed normalized matches: key is f"{league}:{event_id}"
        self._matches_cache: Dict[str, NormalizedMatchEvent] = {}
        self._cache_timestamp: float = 0.0

        # Session with custom user agent and timeout settings
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "PolymarketBot/1.0 (SportsOracle; +https://github.com/polymarketbot)",
            "Accept": "application/json",
        })

        log.info(
            f"Initialized SportsOracleAdapter (live_network={self.use_live_network}, "
            f"cache_ttl={self.cache_ttl_seconds}s)"
        )

    def can_handle(
        self,
        question: str,
        resolution_source: Optional[str] = None,
        category_hint: Optional[str] = None,
    ) -> bool:
        """Check if this adapter can resolve the given market question or resolution source."""
        if category_hint and category_hint.lower() in ("sports", "sports:nba", "sports:nfl", "sports:soccer"):
            return True

        res_src = (resolution_source or "").lower()
        sports_sources = (
            "espn",
            "nba.com",
            "nfl.com",
            "mlb.com",
            "premierleague",
            "uefa.com",
            "ufc.com",
            "atptour.com",
            "wtatennis.com",
            "thesportsdb",
            "balldontlie",
            "sportradar",
            "pandascore",
            "hltv.org",
            "liquipedia",
        )
        if any(src in res_src for src in sports_sources):
            return True

        # Check question content for major leagues and sports terms
        q_lower = question.lower()
        league_terms = (
            "nba", "nfl", "mlb", "nhl", "epl", "premier league", "champions league",
            "la liga", "bundesliga", "serie a", "mls", "ufc", "mma", "atp", "wta",
            "grand slam", "wimbledon", "roland garros", "super bowl", "world cup",
            "cs:go", "cs2", "league of legends", "dota 2", "valorant", "esports",
        )
        if any(term in q_lower for term in league_terms):
            return True

        # Check for sports action verbs coupled with known teams or "vs"
        action_verbs = (
            "defeat", "beat", "win against", "win over", "match winner",
            "game winner", " vs ", " vs. ", " @ ", " win ",
        )
        has_action = any(verb in q_lower for verb in action_verbs)
        has_team = any(team in q_lower for team in TEAM_ALIASES)

        if has_action and has_team:
            return True

        if (" vs " in q_lower or " vs. " in q_lower) and (
            "cup" in q_lower or "game" in q_lower or "match" in q_lower or "tournament" in q_lower
        ):
            return True

        return False

    def fetch_espn_scoreboard(
        self,
        league_key: str,
        dates_param: Optional[str] = None,
    ) -> List[NormalizedMatchEvent]:
        """Fetch and parse live/final events from the ESPN Hidden Public Scoreboard API."""
        path = LEAGUE_PATH_MAP.get(league_key.lower())
        if not path:
            log.warning(f"Unsupported league key: {league_key}")
            return []

        url = f"{self.espn_base_url}/{path}/scoreboard"
        params: Dict[str, str] = {}
        if dates_param:
            params["dates"] = dates_param

        try:
            resp = self._session.get(url, params=params, timeout=self.request_timeout)
            if resp.status_code != 200:
                log.warning(f"ESPN returned status {resp.status_code} for {url}")
                return []

            data = resp.json()
            return self._parse_espn_data(league_key, data)
        except Exception as e:
            log.error(f"Failed to fetch ESPN scoreboard for {league_key}: {e}")
            return []

    def _parse_espn_data(
        self,
        league_key: str,
        data: Dict[str, Any],
    ) -> List[NormalizedMatchEvent]:
        """Parse raw ESPN JSON payload into standardized NormalizedMatchEvent objects."""
        results: List[NormalizedMatchEvent] = []
        events = data.get("events", [])

        is_mma = "mma" in league_key.lower() or "ufc" in league_key.lower()
        is_tennis = "tennis" in league_key.lower() or "atp" in league_key.lower() or "wta" in league_key.lower()

        for event in events:
            event_id = str(event.get("id", ""))
            event_status = event.get("status", {})

            # 1. MMA / UFC structure: event contains multiple fight card competitions
            if is_mma:
                competitions = event.get("competitions", [])
                for comp in competitions:
                    comp_id = str(comp.get("id", event_id))
                    status_dict = comp.get("status", event_status)
                    match_event = self._build_bout_event(league_key, comp_id, comp, status_dict)
                    if match_event:
                        results.append(match_event)
                continue

            # 2. Tennis structure: event (tournament) contains groupings -> competitions
            if is_tennis:
                groupings = event.get("groupings", [])
                for grp in groupings:
                    competitions = grp.get("competitions", [])
                    for comp in competitions:
                        comp_id = str(comp.get("id", event_id))
                        status_dict = comp.get("status", event_status)
                        match_event = self._build_bout_event(league_key, comp_id, comp, status_dict)
                        if match_event:
                            results.append(match_event)
                # Fallback if competitions exist directly at event level
                for comp in event.get("competitions", []):
                    comp_id = str(comp.get("id", event_id))
                    match_event = self._build_bout_event(league_key, comp_id, comp, comp.get("status", event_status))
                    if match_event:
                        results.append(match_event)
                continue

            # 3. Standard Team Sports (NBA, NFL, MLB, EPL, MLS, etc.)
            competitions = event.get("competitions", [])
            if not competitions:
                continue

            comp = competitions[0]
            status_dict = comp.get("status", event_status)
            match_event = self._build_team_match_event(league_key, event_id, comp, status_dict)
            if match_event:
                results.append(match_event)

        return results

    def _build_team_match_event(
        self,
        league_key: str,
        event_id: str,
        comp: Dict[str, Any],
        status_dict: Dict[str, Any],
    ) -> Optional[NormalizedMatchEvent]:
        """Construct a NormalizedMatchEvent for a standard two-team game."""
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        c0, c1 = competitors[0], competitors[1]
        team0_name = c0.get("team", {}).get("displayName", "")
        team1_name = c1.get("team", {}).get("displayName", "")

        try:
            score0 = float(c0.get("score", 0.0))
        except (ValueError, TypeError):
            score0 = 0.0

        try:
            score1 = float(c1.get("score", 0.0))
        except (ValueError, TypeError):
            score1 = 0.0

        status_type = status_dict.get("type", {})
        status_name = status_type.get("name", "STATUS_SCHEDULED")
        is_completed = bool(status_type.get("completed", False)) or status_name in FINAL_STATUS_NAMES
        clock_detail = status_type.get("detail", "")
        period = int(status_dict.get("period", 0))

        # Determine winner
        winner_name: Optional[str] = None
        if is_completed:
            if c0.get("winner") is True:
                winner_name = team0_name
            elif c1.get("winner") is True:
                winner_name = team1_name
            elif score0 > score1:
                winner_name = team0_name
            elif score1 > score0:
                winner_name = team1_name
            elif score0 == score1:
                winner_name = "DRAW"

        # Determine status enum
        if is_completed:
            match_status = OracleStatus.CONCLUDED
        elif status_name in IN_PLAY_STATUS_NAMES or status_type.get("state") == "in":
            match_status = OracleStatus.IN_PLAY
        elif status_name in POSTPONED_OR_CANCELED_STATUS_NAMES:
            match_status = OracleStatus.DISPUTED
        else:
            match_status = OracleStatus.PENDING

        return NormalizedMatchEvent(
            event_id=event_id,
            sport_league=league_key,
            competitor_a=team0_name,
            competitor_b=team1_name,
            score_a=score0,
            score_b=score1,
            status=match_status,
            status_detail=status_name,
            completed=is_completed,
            winning_competitor=winner_name,
            clock_detail=clock_detail,
            period=period,
            raw_payload=comp,
        )

    def _build_bout_event(
        self,
        league_key: str,
        comp_id: str,
        comp: Dict[str, Any],
        status_dict: Dict[str, Any],
    ) -> Optional[NormalizedMatchEvent]:
        """Construct a NormalizedMatchEvent for an individual bout (MMA, Tennis)."""
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        c0, c1 = competitors[0], competitors[1]
        name0 = c0.get("athlete", {}).get("displayName") or c0.get("team", {}).get("displayName", "")
        name1 = c1.get("athlete", {}).get("displayName") or c1.get("team", {}).get("displayName", "")

        status_type = status_dict.get("type", {})
        status_name = status_type.get("name", "STATUS_SCHEDULED")
        is_completed = bool(status_type.get("completed", False)) or status_name in FINAL_STATUS_NAMES
        clock_detail = status_type.get("detail", "")
        period = int(status_dict.get("period", 0))

        winner_name: Optional[str] = None
        if is_completed:
            if c0.get("winner") is True:
                winner_name = name0
            elif c1.get("winner") is True:
                winner_name = name1

        if is_completed:
            match_status = OracleStatus.CONCLUDED
        elif status_name in IN_PLAY_STATUS_NAMES or status_type.get("state") == "in":
            match_status = OracleStatus.IN_PLAY
        elif status_name in POSTPONED_OR_CANCELED_STATUS_NAMES:
            match_status = OracleStatus.DISPUTED
        else:
            match_status = OracleStatus.PENDING

        return NormalizedMatchEvent(
            event_id=comp_id,
            sport_league=league_key,
            competitor_a=name0,
            competitor_b=name1,
            status=match_status,
            status_detail=status_name,
            completed=is_completed,
            winning_competitor=winner_name,
            clock_detail=clock_detail,
            period=period,
            raw_payload=comp,
        )

    def parse_market_question(
        self,
        question: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """Parse market question to extract (target_competitor, competitor_a, competitor_b, league_hint)."""
        q = question.strip()

        # Check for league prefix (e.g. "NBA: Celtics vs Lakers" or "EPL: Arsenal vs Chelsea")
        league_hint: Optional[str] = None
        for key in LEAGUE_PATH_MAP:
            if re.search(rf"\b{re.escape(key)}\b", q, re.IGNORECASE):
                league_hint = key
                break

        # Pattern 1: "Will [Team A] defeat / win against / beat [Team B]?"
        p1 = re.search(
            r"will\s+(the\s+)?(.+?)\s+(?:defeat|beat|win against|win over)\s+(the\s+)?(.+?)[\?\.]?$",
            q,
            re.IGNORECASE,
        )
        if p1:
            team_a = p1.group(2).strip()
            team_b = p1.group(4).strip()
            return team_a, team_a, team_b, league_hint

        # Pattern 2: "Will [Team A] win ...?" (single team proposition)
        p2 = re.search(r"will\s+(the\s+)?(.+?)\s+win\b", q, re.IGNORECASE)
        if p2:
            team_a = p2.group(2).strip()
            # Look for "against [Team B]" or "vs [Team B]"
            p2_vs = re.search(r"(?:against|vs\.?)\s+(the\s+)?(.+?)[\?\.]?$", q, re.IGNORECASE)
            team_b = p2_vs.group(2).strip() if p2_vs else None
            return team_a, team_a, team_b, league_hint

        # Pattern 3: "[Team A] vs. [Team B] ..." or "[Team A] @ [Team B]"
        p3 = re.search(
            r"(?:^|:\s*)([A-Za-z0-9\.\s]+?)\s+(?:vs\.?|@)\s+([A-Za-z0-9\.\s]+?)(?:\s*[\-\(:]|$)",
            q,
            re.IGNORECASE,
        )
        if p3:
            comp_a = p3.group(1).strip()
            comp_b = p3.group(2).strip()
            # Check if a specific winner is nominated after hyphen: e.g. "Celtics vs Lakers - Celtics"
            target = None
            if "-" in q:
                suffix = q.split("-")[-1].strip()
                if match_competitor(suffix, comp_a):
                    target = comp_a
                elif match_competitor(suffix, comp_b):
                    target = comp_b
            if not target:
                target = comp_a  # Default candidate target to first listed
            return target, comp_a, comp_b, league_hint

        return None, None, None, league_hint

    def evaluate(self, candidate: Domain2Candidate) -> Optional[GroundTruthOutcome]:
        """Evaluate a Domain2Candidate against live sports ground truth."""
        target, comp_a, comp_b, league_hint = self.parse_market_question(candidate.question)
        if not target and not comp_a:
            return None

        # Determine which leagues to check
        leagues_to_search = [league_hint] if league_hint else list(LEAGUE_PATH_MAP.keys())[:6]

        matched_event: Optional[NormalizedMatchEvent] = None

        # 1. Search cached matches first
        now = time.time()
        for match in self._matches_cache.values():
            if self._is_matching_event(match, comp_a, comp_b):
                matched_event = match
                break

        # 2. If not found and network is enabled and cache expired, poll active scoreboards
        if matched_event is None and self.use_live_network:
            if now - self._cache_timestamp > self.cache_ttl_seconds:
                for lk in leagues_to_search:
                    if not lk:
                        continue
                    events = self.fetch_espn_scoreboard(lk)
                    for ev in events:
                        self._matches_cache[f"{ev.sport_league}:{ev.event_id}"] = ev
                        if self._is_matching_event(ev, comp_a, comp_b):
                            matched_event = ev
                self._cache_timestamp = now

        if matched_event is None:
            return None

        # Build GroundTruthOutcome from the matched event
        return self._create_outcome_from_match(candidate, matched_event, target)

    def _is_matching_event(
        self,
        event: NormalizedMatchEvent,
        comp_a: Optional[str],
        comp_b: Optional[str],
    ) -> bool:
        """Check if a normalized match event corresponds to the requested competitors."""
        if not comp_a:
            return False

        ev_a = event.competitor_a
        ev_b = event.competitor_b

        # If both competitors are known:
        if comp_b:
            match_direct = match_competitor(comp_a, ev_a) and match_competitor(comp_b, ev_b)
            match_reverse = match_competitor(comp_a, ev_b) and match_competitor(comp_b, ev_a)
            return match_direct or match_reverse

        # If only one competitor is known:
        return match_competitor(comp_a, ev_a) or match_competitor(comp_a, ev_b)

    def _create_outcome_from_match(
        self,
        candidate: Domain2Candidate,
        match: NormalizedMatchEvent,
        target_competitor: Optional[str],
    ) -> GroundTruthOutcome:
        """Translate a NormalizedMatchEvent into an actionable GroundTruthOutcome."""
        target = target_competitor or match.competitor_a
        ancillary: Dict[str, Any] = {
            "sport_league": match.sport_league,
            "competitor_a": match.competitor_a,
            "competitor_b": match.competitor_b,
            "score_a": match.score_a,
            "score_b": match.score_b,
            "status_detail": match.status_detail,
            "clock_detail": match.clock_detail,
            "period": match.period,
        }

        # 1. Deterministic Conclusion (Game physically final)
        if match.status == OracleStatus.CONCLUDED and match.winning_competitor:
            is_target_winner = match_competitor(target, match.winning_competitor)
            is_draw = match.winning_competitor == "DRAW"

            if is_target_winner:
                p_hat = 1.000
            elif is_draw:
                p_hat = 0.500
            else:
                p_hat = 0.000

            return GroundTruthOutcome(
                event_id=f"{match.sport_league}_{match.event_id}",
                category=OracleCategory.SPORTS,
                status=OracleStatus.CONCLUDED,
                concluded=True,
                winning_outcome=match.winning_competitor,
                p_hat=p_hat,
                h_reversal=0.000,
                confidence=1.000,
                resolution_source_url=f"https://www.espn.com/{match.sport_league}",
                ancillary_details=ancillary,
            )

        # 2. In-Play Event (Game currently underway)
        if match.status == OracleStatus.IN_PLAY:
            # Estimate win probability from current point differential
            diff = match.score_a - match.score_b
            if not match_competitor(target, match.competitor_a):
                diff = -diff

            # Heuristic in-play win probability: logistic curve over score lead
            # Lead of 15 in NBA or 10 in NFL -> high confidence
            k_scale = 10.0 if "nba" in match.sport_league else 5.0
            in_play_p_hat = 1.0 / (1.0 + float(2.71828 ** (-diff / max(1.0, k_scale))))
            # Cap bounds between 0.05 and 0.95 during live play
            in_play_p_hat = max(0.05, min(0.95, in_play_p_hat))

            # Non-zero reversal hazard during active in-play game
            h_rev = max(0.02, 0.20 * (1.0 - abs(in_play_p_hat - 0.5) * 2.0))

            return GroundTruthOutcome(
                event_id=f"{match.sport_league}_{match.event_id}",
                category=OracleCategory.SPORTS,
                status=OracleStatus.IN_PLAY,
                concluded=False,
                winning_outcome=None,
                p_hat=in_play_p_hat,
                h_reversal=h_rev,
                confidence=0.85,
                resolution_source_url=f"https://www.espn.com/{match.sport_league}",
                ancillary_details=ancillary,
            )

        # 3. Disputed / Postponed / Canceled
        if match.status == OracleStatus.DISPUTED:
            return GroundTruthOutcome(
                event_id=f"{match.sport_league}_{match.event_id}",
                category=OracleCategory.SPORTS,
                status=OracleStatus.DISPUTED,
                concluded=False,
                winning_outcome=None,
                p_hat=0.500,
                h_reversal=1.000,
                confidence=0.500,
                resolution_source_url=f"https://www.espn.com/{match.sport_league}",
                ancillary_details=ancillary,
            )

        # 4. Pending / Pre-game
        return GroundTruthOutcome(
            event_id=f"{match.sport_league}_{match.event_id}",
            category=OracleCategory.SPORTS,
            status=OracleStatus.PENDING,
            concluded=False,
            winning_outcome=None,
            p_hat=0.500,
            h_reversal=0.05,
            confidence=0.500,
            resolution_source_url=f"https://www.espn.com/{match.sport_league}",
            ancillary_details=ancillary,
        )

    def poll_active_events(self) -> Dict[str, GroundTruthOutcome]:
        """Poll upstream APIs across all core leagues and return active GroundTruthOutcomes."""
        outcomes: Dict[str, GroundTruthOutcome] = {}
        if not self.use_live_network:
            return outcomes

        core_leagues = ["nba", "nfl", "epl", "mlb", "ufc", "atp"]
        for league in core_leagues:
            try:
                events = self.fetch_espn_scoreboard(league)
                for ev in events:
                    self._matches_cache[f"{ev.sport_league}:{ev.event_id}"] = ev
                    key = f"{ev.sport_league}_{ev.event_id}"
                    outcomes[key] = GroundTruthOutcome(
                        event_id=key,
                        category=OracleCategory.SPORTS,
                        status=ev.status,
                        concluded=ev.completed,
                        winning_outcome=ev.winning_competitor,
                        p_hat=1.000 if ev.completed and ev.winning_competitor else 0.500,
                        h_reversal=0.000 if ev.completed else 0.05,
                        confidence=1.000 if ev.completed else 0.85,
                        resolution_source_url=f"https://www.espn.com/{ev.sport_league}",
                        ancillary_details={
                            "competitor_a": ev.competitor_a,
                            "competitor_b": ev.competitor_b,
                            "score_a": ev.score_a,
                            "score_b": ev.score_b,
                            "status_detail": ev.status_detail,
                        },
                    )
            except Exception as e:
                log.error(f"Error polling league {league}: {e}")

        self._cache_timestamp = time.time()
        log.info(f"Polled {len(outcomes)} active sports events across {len(core_leagues)} leagues.")
        return outcomes

    def inject_mock_event(self, match: NormalizedMatchEvent) -> None:
        """Inject a test mock event directly into cache for deterministic unit testing."""
        key = f"{match.sport_league}:{match.event_id}"
        self._matches_cache[key] = match
        self._cache_timestamp = time.time()

    async def get_latest_price(self, asset: str) -> PricePoint:
        """Fetch latest reference price - not applicable for sports oracle."""
        raise NotImplementedError("Sports oracle does not track crypto asset prices")

    async def get_historical_price(
        self, asset: str, timestamp_s: float
    ) -> Optional[PricePoint]:
        """Fetch historical reference price - not applicable for sports oracle."""
        raise NotImplementedError("Sports oracle does not track historical crypto prices")

    def parse_ancillary_data(self, ancillary: Any) -> ParsedAncillaryData:
        """Parse raw UMA ancillary data bytes/string for sports markets."""
        raw_text = ""
        if isinstance(ancillary, bytes):
            raw_text = ancillary.decode("utf-8", errors="replace")
        elif isinstance(ancillary, str):
            if ancillary.startswith("0x") or ancillary.startswith("0X"):
                try:
                    raw_text = bytes.fromhex(ancillary[2:]).decode("utf-8", errors="replace")
                except Exception:
                    raw_text = ancillary
            else:
                raw_text = ancillary
        else:
            raw_text = str(ancillary or "")

        parsed = ParsedAncillaryData(raw_ancillary=raw_text)

        # Extract title / question
        title_m = re.search(r'title:\s*["\']?([^,"\']+)["\']?', raw_text, re.IGNORECASE)
        if title_m:
            parsed.title = title_m.group(1).strip()
        else:
            # Fallback to q field
            q_m = re.search(r'q:\s*["\']?([^,"\']+)["\']?', raw_text, re.IGNORECASE)
            if q_m:
                parsed.title = q_m.group(1).strip()

        # Extract source feed
        for src in ("espn", "nba.com", "nfl.com", "mlb.com", "premierleague.com", "ufc.com", "atptour.com", "wtatennis.com"):
            if src in raw_text.lower():
                parsed.source_feed = src
                break

        # Check for early resolution flag (game finish immediately resolves)
        if "early" in raw_text.lower() or "final" in raw_text.lower() or "official" in raw_text.lower():
            parsed.early_resolution_allowed = True

        return parsed

    def evaluate_market_resolution(
        self,
        market_info: Dict[str, Any],
        current_prices: Optional[Dict[str, float]] = None,
        as_of_timestamp: Optional[float] = None,
    ) -> ResolutionVerdict:
        """Evaluate prospective or real-time resolution state for a sports market."""
        condition_id = str(market_info.get("condition_id", ""))
        question = str(market_info.get("question") or market_info.get("title", ""))
        ancillary_data = market_info.get("ancillary_data")

        # Parse ancillary if provided
        if ancillary_data and not question:
            parsed_anc = self.parse_ancillary_data(ancillary_data)
            question = parsed_anc.title

        from polymarketbot.trading.domain2.ortools_allocator import Domain2Candidate
        candidate = Domain2Candidate(
            condition_id=condition_id,
            token_id=str(market_info.get("token_id", "")),
            question=question,
            category=str(market_info.get("category", "sports")),
            price=float(market_info.get("price", 0.50)),
            hours_to_resolution=float(market_info.get("hours_to_resolution", 1.0)),
            depth_usd=float(market_info.get("depth_usd", 1000.0)),
        )

        outcome = self.evaluate(candidate)
        if outcome is None:
            return ResolutionVerdict(
                condition_id=condition_id,
                outcome=ResolutionOutcome.PENDING,
                p_yes=0.50,
                confidence=0.50,
                rationale="No active or past game match found in sports oracles.",
                is_deterministic=False,
            )

        if outcome.status == OracleStatus.CONCLUDED:
            if outcome.p_hat >= 0.999:
                return ResolutionVerdict(
                    condition_id=condition_id,
                    outcome=ResolutionOutcome.YES,
                    p_yes=1.000,
                    confidence=outcome.confidence,
                    rationale=f"Event final: winning outcome {outcome.winning_outcome}",
                    is_deterministic=True,
                )
            elif outcome.p_hat <= 0.001:
                return ResolutionVerdict(
                    condition_id=condition_id,
                    outcome=ResolutionOutcome.NO,
                    p_yes=0.000,
                    confidence=outcome.confidence,
                    rationale=f"Event final: winning outcome {outcome.winning_outcome} (opponent won)",
                    is_deterministic=True,
                )
            else:
                return ResolutionVerdict(
                    condition_id=condition_id,
                    outcome=ResolutionOutcome.TIE,
                    p_yes=0.500,
                    confidence=outcome.confidence,
                    rationale="Event ended in a draw/tie or 50-50 void condition.",
                    is_deterministic=True,
                )

        if outcome.status == OracleStatus.IN_PLAY:
            return ResolutionVerdict(
                condition_id=condition_id,
                outcome=ResolutionOutcome.PENDING,
                p_yes=outcome.p_hat,
                confidence=outcome.confidence,
                rationale=f"In-play game underway. Current score: {outcome.ancillary_details.get('score_a')} - {outcome.ancillary_details.get('score_b')}",
                is_deterministic=False,
            )

        if outcome.status == OracleStatus.DISPUTED:
            return ResolutionVerdict(
                condition_id=condition_id,
                outcome=ResolutionOutcome.DISPUTED,
                p_yes=0.50,
                confidence=0.50,
                rationale="Game postponed, suspended, or canceled.",
                is_deterministic=False,
            )

        return ResolutionVerdict(
            condition_id=condition_id,
            outcome=ResolutionOutcome.PENDING,
            p_yes=0.50,
            confidence=0.50,
            rationale="Game scheduled / pending start.",
            is_deterministic=False,
        )


# --------------------------------------------------------------------------- #
# Secondary / Free Google-Sign-In API Adapters
# --------------------------------------------------------------------------- #

class BallDontLieClient:
    """Client for BallDontLie API (NBA, NFL, MLB).
    
    Signup URL: https://dash.balldontlie.io (Google SSO supported)
    Env var: BALLDONTLIE_API_KEY
    Rate limit: 30 requests/minute on free tier
    """

    BASE_URL = "https://api.balldontlie.io/v1"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.getenv("BALLDONTLIE_API_KEY")
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({"Authorization": self.api_key})

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def get_games(self, date_str: str) -> List[Dict[str, Any]]:
        """Fetch games for date YYYY-MM-DD."""
        if not self.is_configured:
            return []
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/games",
                params={"dates[]": date_str},
                timeout=5.0,
            )
            if resp.status_code == 200:
                return resp.json().get("data", [])
        except Exception as e:
            log.warning(f"BallDontLie API error: {e}")
        return []


class TheSportsDBClient:
    """Client for TheSportsDB API.
    
    Signup URL: https://www.thesportsdb.com/api.php (Google / Email signup)
    Env var: THESPORTSDB_API_KEY (Defaults to '3' for basic tests, Patreon for livescore)
    Rate limit: Standard API access, Patreon for websocket/livescore push
    """

    BASE_URL = "https://www.thesportsdb.com/api/v1/json"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.getenv("THESPORTSDB_API_KEY", "3")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "PolymarketBot/1.0"})

    def search_events(self, query: str) -> List[Dict[str, Any]]:
        """Search events by event name query."""
        try:
            url = f"{self.BASE_URL}/{self.api_key}/searchevents.php"
            resp = self.session.get(url, params={"e": query}, timeout=5.0)
            if resp.status_code == 200:
                return resp.json().get("event", []) or []
        except Exception as e:
            log.warning(f"TheSportsDB API error: {e}")
        return []


class PandaScoreClient:
    """Client for PandaScore Esports API (CS2, LoL, Dota 2, Valorant).
    
    Signup URL: https://pandascore.co (Google SSO supported)
    Env var: PANDASCORE_API_KEY
    Rate limit: 1,000 requests/hour on free tier
    """

    BASE_URL = "https://api.pandascore.co"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.getenv("PANDASCORE_API_KEY")
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({"Authorization": f"Bearer {self.api_key}"})

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def get_running_matches(self, videogame: str = "cs-go") -> List[Dict[str, Any]]:
        """Fetch currently running esports matches."""
        if not self.is_configured:
            return []
        try:
            url = f"{self.BASE_URL}/{videogame}/matches/running"
            resp = self.session.get(url, timeout=5.0)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            log.warning(f"PandaScore API error: {e}")
        return []

