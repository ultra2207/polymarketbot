"""Polymarket -> Kalshi market mapping.

Both venues list overlapping questions (US elections, index levels, sports) but
with different titles, tickers and dates. There is no shared id, so the mapping
is derived from two signals:

* **structure** -- office (house/senate/governor/president), state, district,
  party, year and claim kind (winner / margin bucket / turnout / seat count),
  extracted from the question text *and* the Kalshi ticker (``MI10``,
  ``WAGOVR``, ``TN05``);
* **tokens** -- the significant words of both titles, compared with Jaccard
  overlap after a normalisation that folds case, punctuation, plurals and
  number words.

Matching is deterministic (no model calls) so a mapping can be rebuilt offline,
diffed between universes and audited row by row. Every row carries the
``method`` that produced it and a ``confidence`` in ``[0, 1]``:

=====================  ======  ===========================================
method                 conf    meaning
=====================  ======  ===========================================
``title_exact``        0.97    identical normalised titles
``structured_winner`` 0.85-95  same office/state/district/party/year, winner
``title_fuzzy``        0.45-85  token Jaccard >= 0.62, no numeric conflict
``margin_threshold``   0.55-65  PM ``by 9%-12%`` bucket vs Kalshi ``>= 9%``
``turnout_threshold``  0.50-60  PM vote-count bucket vs Kalshi ``above N``
=====================  ======  ===========================================

The thresholds are deliberately conservative: a wrong cross-venue join is
worse than a missing one, because downstream code treats the pair as the same
economic event.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

MAPPING_PATH = Path("data/derived/kalshi_mapping.parquet")

# Column order of the artifact. The first four are the contract; the rest make
# a row auditable without re-running the matcher.
COLUMNS = (
    "polymarket_condition_id",
    "kalshi_ticker",
    "confidence",
    "method",
    "kalshi_event_ticker",
    "polymarket_question",
    "kalshi_title",
)

# Filled by the CLI so a stale artifact is obvious.
DEFAULT_MIN_CONFIDENCE = 0.5

STATE_NAMES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska",
    "NV": "nevada", "NH": "new hampshire", "NJ": "new jersey",
    "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon",
    "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming",
    "DC": "district of columbia",
}

# Long names first so "west virginia" wins over "virginia".
_STATE_BY_NAME = sorted(
    ((name, code) for code, name in STATE_NAMES.items()),
    key=lambda pair: -len(pair[0]),
)

STOPWORDS = frozenset(
    """
    a an the will would be is are was were do does did to of in on at for by
    with from and or not no yes than then that this these those it its as if
    any all more less most least over under above below between into out up
    down about after before during since until while who whom whose which what
    when where why how there their his her they them we you i he she
    total win wins won winner market question event contract end date year
    """.split()
)

_OFFICE_PATTERNS = (
    ("president", re.compile(r"\bpresident(?:ial|ial nomination)?\b")),
    ("governor", re.compile(r"\bgovernor\b|\bgubernatorial\b")),
    ("senate", re.compile(r"\bsenate\b|\bsenator\b|\bsenate seat\b")),
    ("house", re.compile(r"\bhouse\b|\bcongressional district\b|\bcongress\b")),
)

_KIND_PATTERNS = (
    (
        "margin",
        re.compile(
            r"\bmargin of victory\b|\bwin by\b|\belection by \d|"
            r"\bby \d+(?:\.\d+)? pct\b|\bwithin \d+(?:\.\d+)? pct\b|"
            r"\bbetween \d+(?:\.\d+)? and \d+(?:\.\d+)? pct\b"
        ),
    ),
    ("turnout", re.compile(r"\btotal vote|\bvotes cast\b|\bturnout\b|\bvote count\b")),
    (
        "share",
        re.compile(r"\bpct of the vote\b|\bvote share\b|\bpercent of the vote\b"),
    ),
    ("seats", re.compile(r"\b\d+ (?:house|senate) seats\b|\bseats after\b|\bhouse seats\b")),
    (
        "winner",
        re.compile(r"\bwinner\b|\bwin the\b|\bwho will win\b|\bwins?\b"),
    ),
)

_LOCAL_LEG = re.compile(
    r"\bstate (?:house|senate|assembly|legislature)\b|\bstate legislative\b"
)

_PARTY_DEM = re.compile(r"\bdemocrat(?:ic|s)?\b|\bdem\b|\bdfl\b")
_PARTY_REP = re.compile(r"\brepublican(?:s)?\b|\bgop\b|\brep\b")

_DISTRICT_DASHED = re.compile(r"\b([A-Z]{2})\s?[-–]\s?(\d{1,2})\b")
# "MI 10 House election" -- the abbreviation is only trusted next to a contest
# word, otherwise "US 10" or "AI 5" would look like a district.
_DISTRICT_SPACED = re.compile(r"\b([A-Z]{2})\s(\d{1,2})\b(?!\d)")
_CONTEST_WORDS = ("house", "district", "congress", "senate", "seat", "race", "election")
_DISTRICT_ORDINAL = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)\s+(?:congressional\s+)?district\b"
)
_DISTRICT_PLAIN = re.compile(r"\bdistrict\s+(\d{1,2})\b")
_DISTRICT_TICKER = re.compile(r"\b([A-Z]{2})[HS]?(\d{2,3})\b")
_PCT_BUCKET = re.compile(
    r"\b(\d+(?:\.\d+)?) pct (?:to |and )?(\d+(?:\.\d+)?) pct\b"
)
_VOTE_BUCKET = re.compile(r"\bbetween (\d+) and (\d+)\b")
_BELOW = re.compile(r"\bbelow (\d+(?:\.\d+)?)")
_PCT_SINGLE = re.compile(r"\b(\d+(?:\.\d+)?)\s?%")
_AT_LEAST = re.compile(r"\bat least\s+(\d+(?:\.\d+)?)")
_ABOVE = re.compile(r"\babove\s+(\d+(?:\.\d+)?)")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")

# Naive plural folding: enough for "seats"/"seat", "democrats"/"democrat".
_PLURAL = re.compile(r"(?<=[a-z]{3})s\b")

# Light suffix folding so "nomination"/"nominate", "presidential"/"president"
# and "election"/"elect" compare equal without a full stemmer dependency.
_SUFFIXES = ("ation", "ition", "ion", "ial", "ate", "ing", "ed")
_SYNONYMS = {
    "gubernatorial": "governor",
    "congressional": "congress",
    "presidential": "president",
}


def _stem(word: str) -> str:
    word = _SYNONYMS.get(word, word)
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def normalise(text: str) -> str:
    """Lower-case ASCII with numbers and separators made explicit."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", str(text))
    folded = folded.encode("ascii", "ignore").decode()
    # "450,000" is one number, not two tokens.
    folded = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", folded)
    folded = folded.lower()
    folded = folded.replace("&", " and ")
    folded = folded.replace("%", " pct ")
    folded = folded.replace("$", " usd ")
    folded = folded.replace("-", " ")
    folded = re.sub(r"[^a-z0-9. ]+", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def tokenise(text: str) -> frozenset[str]:
    """Significant tokens, singularised and stop-worded."""
    words = []
    for word in normalise(text).split():
        if word in STOPWORDS or word.isdigit():
            continue
        word = _stem(_PLURAL.sub("", word))
        if len(word) < 3 or word in STOPWORDS:
            continue
        words.append(word)
    return frozenset(words)


def _state_from_text(text: str) -> str | None:
    lowered = " " + normalise(text) + " "
    for name, code in _STATE_BY_NAME:
        if f" {name} " in lowered:
            return code
    for match in re.finditer(r"\b([A-Z]{2})\b", text or ""):
        code = match.group(1)
        if code in STATE_NAMES and (
            re.search(r"\b(?:house|senate|governor|district|seat|race)\b", lowered)
            or " district" in lowered
        ):
            return code
    return None


def _state_from_dashed_district(text: str) -> tuple[str, int] | None:
    match = _DISTRICT_DASHED.search(text or "")
    if match and match.group(1) in STATE_NAMES:
        return match.group(1), int(match.group(2))
    for match in _DISTRICT_SPACED.finditer(text or ""):
        if match.group(1) not in STATE_NAMES:
            continue
        tail = (text or "")[match.end() : match.end() + 40].lower()
        head = (text or "")[max(0, match.start() - 20) : match.start()].lower()
        if any(word in tail for word in _CONTEST_WORDS) or "district" in head:
            return match.group(1), int(match.group(2))
    return None


def _district(text: str) -> int | None:
    lowered = normalise(text)
    dashed = _state_from_dashed_district(text)
    if dashed:
        return dashed[1]
    for pattern in (_DISTRICT_ORDINAL, _DISTRICT_PLAIN):
        match = pattern.search(lowered)
        if match:
            return int(match.group(1))
    match = _DISTRICT_TICKER.search(text or "")
    if match and match.group(1) in STATE_NAMES:
        return int(match.group(2))
    return None


def _office(text: str) -> str | None:
    lowered = normalise(text)
    if "white house" in lowered:
        return None
    for office, pattern in _OFFICE_PATTERNS:
        if pattern.search(lowered):
            return office
    return None


def _kind(text: str) -> str:
    lowered = normalise(text)
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(lowered):
            return kind
    return "other"


def _party(text: str) -> str | None:
    lowered = normalise(text)
    if _PARTY_DEM.search(lowered):
        return "dem"
    if _PARTY_REP.search(lowered):
        return "rep"
    return None


def _year(text: str) -> int | None:
    years = [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", text or "")]
    return years[0] if years else None


def _bucket(text: str) -> tuple[float, float] | None:
    lowered = normalise(text)
    match = _PCT_BUCKET.search(lowered) or _VOTE_BUCKET.search(lowered)
    if not match:
        return None
    low, high = float(match.group(1)), float(match.group(2))
    return (low, high) if low <= high else (high, low)


def _thresholds(text: str) -> tuple[float, ...]:
    lowered = normalise(text)
    found: list[float] = []
    for pattern in (_AT_LEAST, _ABOVE, _BELOW):
        found.extend(float(m) for m in pattern.findall(lowered))
    if not found:
        # Kalshi margin ladders also appear as "1+ pts" / "2+ points".
        found.extend(float(m) for m in re.findall(r"\b(\d+(?:\.\d+)?)\+ (?:pts?|point)", lowered))
    return tuple(sorted(found))


@dataclass(frozen=True)
class Features:
    """Everything the matcher is allowed to look at."""

    text: str
    tokens: frozenset[str]
    office: str | None = None
    state: str | None = None
    district: int | None = None
    year: int | None = None
    party: str | None = None
    kind: str = "other"
    bucket: tuple[float, float] | None = None
    thresholds: tuple[float, ...] = ()
    end_date: str | None = None
    local_legislature: bool = False

    @property
    def structural(self) -> bool:
        """True when the row describes a US election contest."""
        return bool(self.office or self.district)


def extract_features(
    text: str,
    *,
    ticker: str | None = None,
    end_date: str | None = None,
) -> Features:
    """Extract structural features from a title plus an optional Kalshi ticker."""
    combined = text or ""
    state = _state_from_dashed_district(combined)
    district = _district(combined)
    state_code = state[0] if state else None
    office = _office(combined)

    party = _party(combined)
    local_legislature = bool(_LOCAL_LEG.search(normalise(combined)))
    if ticker:
        if str(ticker).upper().startswith("KXSTATELEG"):
            local_legislature = True
        # Party is often encoded only in the ticker: ``-D`` / ``-R``,
        # ``-DEMDEM`` / ``-REPDEM``.
        for segment in str(ticker).split("-")[1:]:
            upper = segment.upper()
            if upper in {"D", "DEM", "DEMDEM"} or upper.startswith("DEM"):
                party = party or "dem"
            elif upper in {"R", "REP", "REPDEM"} or upper.startswith("REP"):
                party = party or "rep"
        dashed = _state_from_dashed_district(ticker)
        if dashed:
            state_code = state_code or dashed[0]
            district = district if district is not None else dashed[1]
        # Event suffixes such as ``WAGOVR``, ``MIH010``, ``TN05``.
        suffix = ticker.split("-")[-1]
        state_match = re.match(r"^([A-Z]{2})([HSG])", suffix)
        if state_match and state_match.group(1) in STATE_NAMES:
            state_code = state_code or state_match.group(1)
            office = office or {
                "H": "house",
                "S": "senate",
                "G": "governor",
            }[state_match.group(2)]
        district_match = _DISTRICT_TICKER.search(suffix)
        if district_match and district_match.group(1) in STATE_NAMES:
            state_code = state_code or district_match.group(1)
            district = district if district is not None else int(district_match.group(2))

    state_code = state_code or _state_from_text(text or "")
    # "Michigan's 10th District margin of victory" says the office only
    # implicitly: a numbered district is a House seat. ``local_legislature``
    # still separates the state chamber from the US House.
    if district is not None and office is None:
        office = "house"
    return Features(
        text=text or "",
        tokens=tokenise(text or ""),
        office=office,
        state=state_code,
        district=district,
        # A title without a year still has a settlement date; using it keeps a
        # 2026 federal race from matching a 2028 state-legislature look-alike.
        year=_year(combined) or _year(end_date or ""),
        party=party,
        kind=_kind(combined),
        bucket=_bucket(combined),
        thresholds=_thresholds(combined),
        end_date=end_date,
        local_legislature=local_legislature,
    )


@dataclass(frozen=True)
class Match:
    confidence: float
    method: str

    def as_row(
        self,
        *,
        condition_id: str,
        ticker: str,
        event_ticker: str | None,
        question: str,
        kalshi_title: str | None,
    ) -> dict[str, Any]:
        return {
            "polymarket_condition_id": condition_id,
            "kalshi_ticker": ticker,
            "confidence": round(float(self.confidence), 4),
            "method": self.method,
            "kalshi_event_ticker": event_ticker,
            "polymarket_question": question,
            "kalshi_title": kalshi_title,
        }


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _integers(text: str) -> frozenset[int]:
    """Every integer in a title except calendar years (dates rarely conflict)."""
    values = []
    for raw in _NUMBER.findall(normalise(text)):
        try:
            value = int(float(raw))
        except ValueError:
            continue
        if 1900 <= value <= 2100:
            continue
        values.append(value)
    return frozenset(values)


def _contradicts_numerics(pm: Features, kalshi: Features) -> bool:
    """Two threshold ladders for the same contest only match on shared rungs."""
    if not pm.thresholds or not kalshi.thresholds:
        return False
    if pm.kind != kalshi.kind or pm.kind not in {"turnout", "margin", "seats"}:
        return False
    return not set(pm.thresholds) & set(kalshi.thresholds)


MIN_TITLE_OVERLAP = 0.62


def score_pair(pm: Features, kalshi: Features) -> Match | None:
    """Confidence that ``pm`` and ``kalshi`` are the same economic claim."""
    # ---------------------------------------------------------- hard gates
    if pm.office and kalshi.office and pm.office != kalshi.office:
        return None
    if pm.state and kalshi.state and pm.state != kalshi.state:
        return None
    if pm.district is not None and kalshi.district is not None:
        if pm.district != kalshi.district:
            return None
    if pm.year and kalshi.year and abs(pm.year - kalshi.year) > 1:
        return None
    # A state-legislature district is not the same contest as a US House seat,
    # even when state and district number agree.
    if pm.local_legislature != kalshi.local_legislature:
        return None
    if _contradicts_numerics(pm, kalshi):
        return None

    overlap = jaccard(pm.tokens, kalshi.tokens)
    same_text = bool(normalise(pm.text)) and normalise(pm.text) == normalise(kalshi.text)

    if same_text:
        return Match(0.97, "title_exact")

    # ------------------------------------------------- structured contests
    if pm.structural and kalshi.structural:
        office_ok = pm.office is not None and pm.office == kalshi.office
        state_ok = pm.state is not None and pm.state == kalshi.state
        district_ok = (
            pm.office != "house"
            or (pm.district is not None and pm.district == kalshi.district)
        )
        party_known = pm.party is not None and kalshi.party is not None
        party_ok = not party_known or pm.party == kalshi.party
        # Claim types must agree. "other" only bridges when the wording is
        # close: a turnout market must never satisfy a "who wins" question.
        # Claim types must agree exactly. A margin ladder, a vote-share market
        # and a "who wins" market are three different claims on one contest.
        kind_ok = pm.kind == kalshi.kind

        if office_ok and state_ok and district_ok and party_ok and kind_ok:
            if pm.kind == kalshi.kind == "seats" and pm.bucket and kalshi.thresholds:
                low, high = pm.bucket
                if any(low <= v <= high for v in kalshi.thresholds):
                    return Match(0.50 + 0.1 * overlap, "seats_threshold")
            if pm.kind == kalshi.kind == "margin" and pm.bucket and kalshi.thresholds:
                low = pm.bucket[0]
                nearest = min(kalshi.thresholds, key=lambda v: abs(v - low))
                if abs(nearest - low) <= 0.51:
                    return Match(0.55 + 0.1 * overlap, "margin_threshold")
            if pm.kind == kalshi.kind == "turnout" and pm.bucket and kalshi.thresholds:
                # PM bucket is a vote range; Kalshi is "above N votes".
                low, high = pm.bucket
                if any(low <= v <= high for v in kalshi.thresholds):
                    return Match(0.50 + 0.1 * overlap, "turnout_threshold")
            if pm.kind == kalshi.kind == "other":
                # Structure without a named claim is weaker evidence: "perform
                # best among tossups" and "finish within 1 %" both land here.
                return Match(min(0.7, 0.6 + 0.1 * overlap), "structured_contest")
            if pm.kind in {"winner", "seats"}:
                confidence = 0.85 + 0.10 * min(1.0, overlap / 0.6)
                # Both sides naming the party is the strongest structural cue;
                # with one side silent the pair is plausible but unverified.
                confidence += 0.03 if party_known else -0.05
                return Match(min(0.95, confidence), "structured_winner")

    # ------------------------------------------------------ title similarity
    # Wording is the only evidence here, so conflicting numbers (a different
    # rung, a different seat count) are disqualifying rather than penalised.
    if overlap >= MIN_TITLE_OVERLAP:
        pm_numbers = _integers(pm.text)
        kalshi_numbers = _integers(kalshi.text)
        if pm_numbers and kalshi_numbers and not (pm_numbers & kalshi_numbers):
            return None
        return Match(min(0.85, 0.45 + 0.45 * overlap), "title_fuzzy")
    return None


def build_mapping(
    polymarket_rows: Sequence[dict[str, Any]],
    kalshi_rows: Sequence[dict[str, Any]],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    max_candidates: int = 400,
) -> list[dict[str, Any]]:
    """Best Kalshi market for each Polymarket market.

    ``kalshi_rows`` need ``ticker``, ``title``, ``event_ticker`` and optionally
    ``series_ticker``/``close_time``. The inverted token index keeps this linear
    in the universe size: a district question only ever looks at the handful of
    Kalshi markets that share a rare token.
    """
    index: dict[str, list[int]] = {}
    structural: dict[tuple[Any, ...], list[int]] = {}
    features: list[Features] = []
    for position, row in enumerate(kalshi_rows):
        feat = extract_features(
            row.get("title") or "",
            ticker=row.get("ticker") or row.get("event_ticker"),
            end_date=row.get("close_time"),
        )
        features.append(feat)
        for token in feat.tokens:
            index.setdefault(token, []).append(position)
        if feat.state and feat.district is not None:
            structural.setdefault(
                ("district", feat.state, feat.district, feat.local_legislature), []
            ).append(position)
        if feat.state and feat.office:
            structural.setdefault(
                ("office", feat.state, feat.office, feat.local_legislature), []
            ).append(position)

    out: list[dict[str, Any]] = []
    for pm_row in polymarket_rows:
        condition_id = pm_row.get("condition_id") or pm_row.get("polymarket_condition_id")
        question = pm_row.get("question") or pm_row.get("polymarket_question") or ""
        if not condition_id or not question:
            continue
        pm = extract_features(question, end_date=pm_row.get("end_date"))

        # Structural candidates first: a margin ladder titled "Michigan's 10th
        # District" shares almost no words with "MI-10 House election by 9%-12%",
        # so a token-count ranking would drop the only correct candidate.
        counts: dict[int, int] = {}
        for token in pm.tokens:
            for position in index.get(token, ()):
                counts[position] = counts.get(position, 0) + 1
        primary: set[int] = set()
        if pm.state and pm.district is not None:
            primary.update(
                structural.get(("district", pm.state, pm.district, pm.local_legislature), ())
            )
        if pm.state and pm.office:
            primary.update(
                structural.get(("office", pm.state, pm.office, pm.local_legislature), ())
            )
        if not counts and not primary:
            continue
        ordered = [(position, 1) for position in sorted(primary)]
        ordered += [
            item
            for item in sorted(counts.items(), key=lambda item: -item[1])
            if item[0] not in primary
        ]
        ordered = ordered[:max_candidates]

        best: tuple[float, int, Match] | None = None
        for position, _shared in ordered:
            match = score_pair(pm, features[position])
            if match is None or match.confidence < min_confidence:
                continue
            if best is None or match.confidence > best[0]:
                best = (match.confidence, position, match)
        if best is None:
            continue
        _, position, match = best
        row = kalshi_rows[position]
        out.append(
            match.as_row(
                condition_id=str(condition_id),
                ticker=str(row.get("ticker")),
                event_ticker=row.get("event_ticker"),
                question=question,
                kalshi_title=row.get("title"),
            )
        )

    # One ticker, one Polymarket market. Two questions sharing a Kalshi ticker
    # means at least one of them is wrong (typically "-D" and "-R" seats, or a
    # bucket market and its ladder), so the strongest match keeps it.
    best_by_ticker: dict[str, dict[str, Any]] = {}
    for row in out:
        ticker = row["kalshi_ticker"]
        current = best_by_ticker.get(ticker)
        if current is None or row["confidence"] > current["confidence"]:
            best_by_ticker[ticker] = row
    return sorted(
        best_by_ticker.values(),
        key=lambda row: (row["polymarket_condition_id"], row["kalshi_ticker"]),
    )


# ------------------------------------------------------------------- storage
def write_mapping(rows: Iterable[dict[str, Any]], path: Path | str = MAPPING_PATH) -> int:
    """Write the artifact as Parquet. Returns the row count."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    materialised = [{column: row.get(column) for column in COLUMNS} for row in rows]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        materialised,
        schema=pa.schema(
            [
                ("polymarket_condition_id", pa.string()),
                ("kalshi_ticker", pa.string()),
                ("confidence", pa.float64()),
                ("method", pa.string()),
                ("kalshi_event_ticker", pa.string()),
                ("polymarket_question", pa.string()),
                ("kalshi_title", pa.string()),
            ]
        ),
    )
    pq.write_table(table, path, compression="zstd")
    return table.num_rows


def read_mapping(path: Path | str = MAPPING_PATH) -> list[dict[str, Any]]:
    """Load the artifact, tolerating a missing or unreadable file."""
    import pyarrow.parquet as pq

    path = Path(path)
    if not path.exists():
        return []
    try:
        return pq.read_table(path).to_pylist()
    except Exception:  # noqa: BLE001 - a bad artifact must not kill the stream
        return []


def load_mapping(path: Path | str = MAPPING_PATH) -> tuple[str, ...]:
    """Sorted, de-duplicated Kalshi tickers to poll, or ``()`` when absent."""
    rows = read_mapping(path)
    return tuple(sorted({str(row["kalshi_ticker"]) for row in rows if row.get("kalshi_ticker")}))
