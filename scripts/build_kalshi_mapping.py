"""Build the Polymarket -> Kalshi market mapping artifact.

The collector polls Kalshi by ticker, so it needs a list of tickers that mean
the same thing as a Polymarket condition. There is no shared id, so this script
derives one:

1. read the current Polymarket universe from the collector manifest;
2. crawl Kalshi's open *events* (titles, small pages) -- not the 20k+ open
   markets, which would be ~100 MB of payload for a few hundred hits;
3. keep the events whose title or structure plausibly matches a Polymarket
   question (office/state/district/party/year, else token overlap);
4. fetch the markets of only those events (one small request each);
5. score every Polymarket x Kalshi title pair with
   :mod:`polymarketbot.data.kalshi_mapping` and keep the best match per
   Polymarket market above the confidence floor.

Usage
    .venv/Scripts/python.exe scripts/build_kalshi_mapping.py
    .venv/Scripts/python.exe scripts/build_kalshi_mapping.py --min-confidence 0.6 --refresh

    # audited v2: anchored series matching with direction + confidence
    .venv/Scripts/python.exe scripts/build_kalshi_mapping.py --artifact v2
    .venv/Scripts/python.exe scripts/build_kalshi_mapping.py --artifact v2 --refresh --refresh-markets

The v1 artifact is ``data/derived/kalshi_mapping.parquet`` plus a JSON summary
next to it. Regenerate after the universe re-cuts (hourly) so the collector
polls live tickers.

``--artifact v2`` writes ``data/derived/kalshi_mapping_v2.parquet`` and
``data/derived/kalshi_mapping_v2_summary.json``. v2 routes every Polymarket
question through one *topic anchor* (Fed meeting, yield window, crypto
running max/min, election contest, …), matches at series/event granularity,
rejects interval-vs-threshold and composite-claim pairs, and writes an
explicit ``direction`` plus ``match_reason``. See ``docs/kalshi-mapping-audit.md``
for the measured v1 precision (16/25) and the failure modes v2 removes.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import queue
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from polymarketbot.data.kalshi_mapping import (
    MAPPING_PATH,
    build_mapping,
    extract_features,
    jaccard,
    normalise,
    score_pair,
    tokenise,
    write_mapping,
)
from polymarketbot.data.streams.kalshi import KalshiClient, MAX_PAGE_SIZE
from polymarketbot.logs import get_logger

log = get_logger("data.kalshi_mapping")

DEFAULT_MANIFEST = Path("data/tape/manifest_current.json")
DEFAULT_CACHE = Path("data/derived/kalshi_events_cache.json")
MARKETS_CACHE = Path("data/derived/kalshi_markets_cache.json")
SUMMARY_PATH = Path("data/derived/kalshi_mapping_summary.json")
EVENTS_PAGE = 200
DEFAULT_MAX_EVENTS = 40_000
DEFAULT_MAX_EVENT_FETCHES = 700
# Be a polite client: ~5 requests/second sustained (no 429 observed at 10/s).
REQUEST_SPACING = 0.2


def load_universe(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"manifest not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    markets = payload.get("markets") if isinstance(payload, dict) else payload
    if not isinstance(markets, list):
        raise SystemExit(f"unexpected manifest shape: {type(payload).__name__}")
    log.info(f"polymarket universe: {len(markets)} markets from {path}")
    return markets


def fetch_events(
    client: KalshiClient,
    *,
    cache: Path,
    refresh: bool,
    max_events: int,
    status: str,
) -> list[dict]:
    if cache.exists() and not refresh:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        log.info(f"events cache: {len(cached)} events from {cache} (--refresh to re-crawl)")
        return cached

    events: list[dict] = []
    cursor: str | None = None
    started = time.monotonic()
    while len(events) < max_events:
        params: dict[str, object] = {"limit": EVENTS_PAGE, "status": status}
        if cursor:
            params["cursor"] = cursor
        try:
            payload = client.get_json("/events", params)
        except Exception as exc:  # noqa: BLE001 - keep whatever arrived
            log.warning(f"events crawl stopped at {len(events)}: {type(exc).__name__}: {exc}")
            break
        page = [e for e in payload.get("events", []) if isinstance(e, dict)]
        events.extend(
            {
                "event_ticker": e.get("event_ticker"),
                "series_ticker": e.get("series_ticker"),
                "title": e.get("title"),
                "category": e.get("category"),
                "close_time": e.get("strike_date") or e.get("close_time"),
            }
            for e in page
        )
        cursor = payload.get("cursor")
        if len(events) % 2000 < EVENTS_PAGE:
            log.info(f"events: {len(events)} in {time.monotonic() - started:.0f}s")
        if not cursor or not page:
            break
        time.sleep(REQUEST_SPACING)

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(events), encoding="utf-8")
    log.info(f"events crawl: {len(events)} events in {time.monotonic() - started:.0f}s -> {cache}")
    return events


def _event_features(event: dict):
    return extract_features(
        event.get("title") or "",
        ticker=event.get("event_ticker"),
        end_date=event.get("close_time"),
    )


def prefilter_events(
    universe: list[dict], events: list[dict], *, min_shared_tokens: int = 2
) -> list[dict]:
    """Events whose structure or tokens could match at least one question."""
    features = [_event_features(event) for event in events]
    token_index: dict[str, list[int]] = {}
    structural_index: dict[tuple[str, ...], list[int]] = {}
    for position, feature in enumerate(features):
        for token in feature.tokens:
            token_index.setdefault(token, []).append(position)
        # Titles rarely share words ("MI-10" vs "Michigan's 10th District"), so
        # the contest itself is an index key.
        if feature.state and feature.district is not None:
            structural_index.setdefault(
                ("district", feature.state, str(feature.district), str(feature.local_legislature)),
                [],
            ).append(position)
        if feature.state and feature.office:
            structural_index.setdefault(
                ("office", feature.state, feature.office, str(feature.local_legislature)),
                [],
            ).append(position)

    # Two tiers: events that already satisfy the structural gates (a same
    # office/state/district/party contest) outrank title look-alikes, which are
    # mostly noise ("who will be the next secretary general"). Within a tier the
    # best match a question can reach is the fetch priority.
    gated: set[int] = set()
    lookalike: set[int] = set()
    structural: set[int] = set()
    priority: dict[int, float] = {}
    for market in universe:
        question = market.get("question") or ""
        pm = extract_features(question, end_date=market.get("end_date"))
        counts: dict[int, int] = {}
        for token in pm.tokens:
            for position in token_index.get(token, ()):
                counts[position] = counts.get(position, 0) + 1
        local = str(pm.local_legislature)
        if pm.state and pm.district is not None:
            for position in structural_index.get(
                ("district", pm.state, str(pm.district), local), ()
            ):
                counts[position] = counts.get(position, 0) + 1
                structural.add(position)
        if pm.state and pm.office:
            for position in structural_index.get(
                ("office", pm.state, pm.office, local), ()
            ):
                counts[position] = counts.get(position, 0) + 1
                structural.add(position)
        for position, shared in counts.items():
            event_feature = features[position]
            match = score_pair(pm, event_feature)
            if match is not None:
                gated.add(position)
                score = match.confidence
            elif shared >= min_shared_tokens and jaccard(pm.tokens, event_feature.tokens) >= 0.3:
                lookalike.add(position)
                score = 0.3 + 0.4 * jaccard(pm.tokens, event_feature.tokens)
            else:
                # A same-contest event whose title shares no words with the
                # question (a margin ladder) is still a candidate; its markets
                # are scored individually once fetched.
                if position in structural:
                    # Same contest, title words differ (a margin ladder or a
                    # per-district "HOUSEMI10" market): a strong candidate.
                    score = 0.75
                else:
                    continue
            if score > priority.get(position, 0.0):
                priority[position] = score

    ordered = sorted(
        gated | lookalike | structural,
        key=lambda position: (-priority.get(position, 0.0), position),
    )
    selected = [
        {**events[position], "priority": round(priority.get(position, 0.0), 3)}
        for position in ordered
        if events[position].get("event_ticker")
    ]
    log.info(
        f"prefilter: {len(gated)} gate-passing + "
        f"{len(structural - gated)} same-contest + "
        f"{len(lookalike - gated - structural)} title look-alikes = "
        f"{len(selected)} of {len(events)} events"
    )
    return selected


def _market_rows(payload: dict, event_ticker: str | None, series_ticker: str | None) -> list[dict]:
    rows: list[dict] = []
    for raw in payload.get("markets", []):
        if not isinstance(raw, dict) or not raw.get("ticker"):
            continue
        rows.append(
            {
                "ticker": raw.get("ticker"),
                "event_ticker": raw.get("event_ticker") or event_ticker,
                "series_ticker": raw.get("series_ticker") or series_ticker,
                "title": raw.get("title"),
                "close_time": raw.get("close_time"),
                "status": raw.get("status"),
            }
        )
    return rows


def fetch_event_markets(
    clients: list[KalshiClient],
    events: list[dict],
    *,
    max_fetches: int,
    series_min_group: int = 8,
    series_pages: int = 2,
    concurrency: int = 4,
) -> list[dict]:
    """Markets for the candidate events, batched by series where it pays.

    A series with many candidate events (``KXVOTEGENERAL``: 255) is one or two
    paginated ``?series_ticker=`` requests instead of 255 ``?event_ticker=``
    ones. Small groups stay per-event: a series page is ~3 MB, which is only
    worth paying when it replaces a hundred small requests.
    """
    client = clients[0]
    rows: list[dict] = []
    started = time.monotonic()
    budget = max(0, int(max_fetches))

    groups: dict[str, list[dict]] = {}
    loose: list[dict] = []
    for event in events[:budget]:
        series = event.get("series_ticker")
        if series:
            groups.setdefault(str(series), []).append(event)
        else:
            loose.append(event)

    big = {series: items for series, items in groups.items() if len(items) >= series_min_group}
    small = [
        event
        for series, items in groups.items()
        if series not in big
        for event in items
    ] + loose

    # Spend the budget on the most promising contests first: a series unit is
    # ranked by its best event, and the units are interleaved by priority so a
    # 200-event state-legislature series cannot crowd out the margin ladders.
    units: list[tuple[float, int, str, object]] = []
    for series, items in big.items():
        # The budget is counted in requests: a series costs its page allowance,
        # an event costs one request. A 2,000-event series is 2 requests, so it
        # must not consume 2,000 units of budget.
        units.append(
            (
                -max(float(e.get("priority", 0)) for e in items),
                max(1, int(series_pages)),
                series,
                items,
            )
        )
    for event in small:
        units.append(
            (-float(event.get("priority", 0)), 1, str(event["event_ticker"]), event)
        )
    units.sort(key=lambda unit: (unit[0], unit[2]))

    considered = 0
    series_units: list[tuple[str, list[dict]]] = []
    small = []
    for _, size, key, payload in units:
        if considered >= budget:
            break
        considered += size
        if isinstance(payload, list):
            series_units.append((key, payload))
        else:
            small.append(payload)
    big = dict(series_units)
    log.info(
        f"allocation: {len(big)} series + {len(small)} event requests "
        f"({considered} requests of {budget}) covering "
        f"{sum(len(items) for items in big.values()) + len(small)} of {len(events)} candidates"
    )

    for index, (series, items) in enumerate(big.items(), start=1):
        cursor: str | None = None
        for _ in range(max(1, int(series_pages))):
            params: dict[str, object] = {
                "series_ticker": series,
                "limit": MAX_PAGE_SIZE,
                "status": "open",
            }
            if cursor:
                params["cursor"] = cursor
            try:
                payload = client.get_json("/markets", params)
            except Exception as exc:  # noqa: BLE001 - one bad series is not fatal
                log.warning(f"{series}: {type(exc).__name__}: {exc}")
                break
            page = _market_rows(payload, None, series)
            rows.extend(page)
            cursor = payload.get("cursor")
            if not cursor or not page:
                break
            time.sleep(REQUEST_SPACING)
        if index % 10 == 0:
            log.info(
                f"markets: {len(rows)} rows from {index}/{len(big)} series "
                f"in {time.monotonic() - started:.0f}s"
            )
        time.sleep(REQUEST_SPACING)

    # One request per remaining event; a small worker pool keeps the loop under
    # the 5-minute budget while staying near 5 requests/second.
    pool: queue.Queue[KalshiClient] = queue.Queue()
    for extra in clients:
        pool.put(extra)

    def _fetch_one(event: dict) -> list[dict]:
        worker = pool.get()
        try:
            event_ticker = event["event_ticker"]
            try:
                payload = worker.get_json(
                    "/markets", {"event_ticker": event_ticker, "limit": MAX_PAGE_SIZE}
                )
            except Exception as exc:  # noqa: BLE001 - one bad event is not fatal
                log.warning(f"{event_ticker}: {type(exc).__name__}: {exc}")
                return []
            page = _market_rows(payload, event_ticker, event.get("series_ticker"))
            if not page and event.get("title"):
                # An event with no markets still names the market in its title.
                page = [
                    {
                        "ticker": event_ticker,
                        "event_ticker": event_ticker,
                        "series_ticker": event.get("series_ticker"),
                        "title": event.get("title"),
                        "close_time": None,
                        "status": "open",
                    }
                ]
            time.sleep(REQUEST_SPACING)
            return page
        finally:
            pool.put(worker)

    if small:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
            for index, page in enumerate(executor.map(_fetch_one, small), start=1):
                rows.extend(page)
                if index % 100 == 0:
                    log.info(
                        f"markets: {len(rows)} rows from {index}/{len(small)} events "
                        f"in {time.monotonic() - started:.0f}s"
                    )

    log.info(
        f"markets: {len(rows)} rows from {len(big)} series + {len(small)} events "
        f"in {time.monotonic() - started:.0f}s"
    )
    return rows


# ================================================================ v2 pipeline
#
# v1 (``--artifact v1``) keeps the original behaviour. v2 fixes the failure
# modes the precision audit found and adds anchored macro series:
#
# * every pair is *anchored* on an event/series contract (a Fed meeting, a
#   yield window, an election contest), not on question-string similarity;
# * interval-vs-threshold pairs (PM ``by 9%-12%`` against Kalshi ``>= 9%``) are
#   rejected: they are different claims, not different wordings;
# * a direction is recorded for every row. ``inverted`` is only emitted when
#   the two claim texts are identical apart from the polarity of the claim;
# * a confidence in [0, 1] and a human-readable ``match_reason`` are written.
#
# The v2 artifact keeps ``kalshi_ticker`` and the v1 column names, so
# :func:`polymarketbot.data.kalshi_mapping.load_mapping` reads it unchanged.

V2_PATH = Path("data/derived/kalshi_mapping_v2.parquet")
V2_SUMMARY_PATH = Path("data/derived/kalshi_mapping_v2_summary.json")
MACRO_CACHE = Path("data/derived/kalshi_macro_markets_cache.json")

V2_COLUMNS = (
    "kalshi_ticker",
    "kalshi_event",
    "polymarket_condition_id",
    "polymarket_yes_token",
    "polymarket_no_token",
    "direction",
    "confidence",
    "method",
    "match_reason",
    "kalshi_title",
    "kalshi_yes_sub_title",
    "series_ticker",
    "window",
    "polymarket_question",
    # v1 compat alias; streams/kalshi.py only reads kalshi_ticker, but
    # scripts/report_kalshi_basis.py reads the v1 column set.
    "kalshi_event_ticker",
)

V2_SCHEMA = [  # pyarrow field list, built lazily to keep the import cheap
    ("kalshi_ticker", "string"),
    ("kalshi_event", "string"),
    ("polymarket_condition_id", "string"),
    ("polymarket_yes_token", "string"),
    ("polymarket_no_token", "string"),
    ("direction", "string"),
    ("confidence", "float64"),
    ("method", "string"),
    ("match_reason", "string"),
    ("kalshi_title", "string"),
    ("kalshi_yes_sub_title", "string"),
    ("series_ticker", "string"),
    ("window", "string"),
    ("polymarket_question", "string"),
    ("kalshi_event_ticker", "string"),
]

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_WORD = "|".join(sorted(MONTHS, key=len, reverse=True))
_WINDOW_BY = re.compile(
    rf"\bby\s+({_MONTH_WORD})[a-z]*\.?\s+(\d{{1,2}}),?\s+(\d{{4}})", re.IGNORECASE
)
_WINDOW_BY_DAY = re.compile(
    rf"\bby\s+({_MONTH_WORD})[a-z]*\.?\s+(\d{{1,2}})\b", re.IGNORECASE
)
_WINDOW_ON_DAY = re.compile(
    rf"\bon\s+({_MONTH_WORD})[a-z]*\.?\s+(\d{{1,2}}),?\s+(\d{{4}})", re.IGNORECASE
)
_WINDOW_IN = re.compile(
    rf"\b(?:in|on|for)\s+({_MONTH_WORD})[a-z]*\.?\s+(\d{{4}})", re.IGNORECASE
)
_WINDOW_IN_MONTH = re.compile(
    rf"\b(?:in|on|for)\s+({_MONTH_WORD})[a-z]*\b", re.IGNORECASE
)
_WINDOW_IN_YEAR = re.compile(r"\bin\s+(\d{4})\b", re.IGNORECASE)
_WINDOW_BARE = re.compile(
    rf"\b({_MONTH_WORD})[a-z]*\.?\s+(\d{{4}})\b", re.IGNORECASE
)
_WINDOW_BEFORE_YEAR = re.compile(r"\bbefore\s+(\d{4})\b", re.IGNORECASE)
_WINDOW_BEFORE_DAY = re.compile(
    rf"\bbefore\s+({_MONTH_WORD})[a-z]*\.?\s+(\d{{1,2}}),?\s+(\d{{4}})",
    re.IGNORECASE,
)
_WINDOW_MONTH_YEAR = re.compile(
    rf"\b({_MONTH_WORD})[a-z]*\.?\s+(\d{{4}})", re.IGNORECASE
)
_PCT = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:%|pct\b)")
_BPS = re.compile(r"(\d+(?:\.\d+)?)\s*bps\b")
_JOBS = re.compile(r"(\d+(?:\.\d+)?)\s*k\b")

# Topic anchors. ``MACRO_TOPICS`` questions are only matched through their
# anchored rule; generic title similarity is not allowed to touch them.
TOPIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "fed_sequence",
        re.compile(r"\b(pause|hike|cut)[ -]+(pause|hike|cut)\b"),
    ),
    (
        "fed_decision",
        re.compile(r"\bfed(?:eral reserve)?\b.*\bmeeting\b|\bfed decision\b"),
    ),
    (
        "fed_count",
        re.compile(
            r"\brate (?:hike|cut|change)s?\b|\bfed rate (?:hike|cut)\b|"
            r"\b(?:cut|hike|raise) rates\b|\bnumber of rate changes\b"
        ),
    ),
    ("treasury_bound", re.compile(r"\btreasury yield\b")),
    ("cpi", re.compile(r"\bcpi\b|\bconsumer price\b")),
    ("pce", re.compile(r"\bpce\b")),
    ("payrolls", re.compile(r"\bjobs?\b|\bpayrolls?\b|\bnonfarm\b")),
    ("unemployment", re.compile(r"\bunemployment\b")),
    ("gdp", re.compile(r"\bgdp\b")),
    ("shutdown", re.compile(r"\bshutdowns?\b")),
    ("debt_limit", re.compile(r"\bdebt (?:ceiling|limit)\b")),
    ("tariff", re.compile(r"\btariff\b")),
    ("mortgage", re.compile(r"\bmortgage\b")),
    (
        "crypto",
        re.compile(r"\bbitcoin\b|\bbtc\b|\bethereum\b|\beth\b|\bsolana\b|\bsol\b|\bxrp\b|\bdogecoin\b|\bdoge\b"),
    ),
    ("approval", re.compile(r"\bapproval rating\b")),
    (
        "election",
        re.compile(
            r"\belection\b|\bprimary\b|\bnomination\b|\bpresident\b|\bgovernor\b|"
            r"\bsenate\b|\bhouse\b|\bcongress\b|\bmayor\b|\bmidterm\b"
        ),
    ),
)
MACRO_TOPICS = frozenset(
    {
        "fed_sequence", "fed_decision", "fed_count", "treasury_bound", "cpi",
        "pce", "payrolls", "unemployment", "gdp", "shutdown", "debt_limit",
        "tariff", "mortgage", "crypto", "approval",
    }
)
# v1 methods that encode a *claim* the audit could verify. The threshold
# methods are deliberately absent: PM "by 9%-12%" is an interval and Kalshi
# ">= 9%" is one-sided, so they are different claims.
V2_ELECTION_METHODS = frozenset({"title_exact", "structured_winner", "title_fuzzy"})

_POL_POS = frozenset(
    "increase rise rises up above hit hits higher more over gain gains win wins "
    "won pass passes approve approves exceed exceeds reach reaches beat beats".split()
)
_POL_NEG = frozenset(
    "decrease fall falls down below dip dips lower less under loss lose loses "
    "lost fail fails reject rejects miss misses".split()
)
_NEGATIONS = frozenset(
    "no not none never neither nor without cannot can't won't doesn't isn't".split()
)
_OFFICE_WORDS = ("president", "governor", "senate", "house", "mayor")
_OFFICE_SYNONYMS = {
    "president": r"president|presidential",
    "governor": r"governor|gubernatorial",
    "senate": r"senate|senator",
    "house": r"house|congressional|district",
    "mayor": r"mayor|mayoral",
}


def _office_word_present(office: str | None, title: str) -> bool:
    if not office:
        return True
    pattern = _OFFICE_SYNONYMS.get(office) or office
    return bool(re.search(rf"\b(?:{pattern})\b", normalise(title)))


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _resolve_year(month: int, end_date: str | None) -> int | None:
    """Year for a month written without one (``in November``, ``by Dec 31``)."""
    if not end_date:
        return None
    try:
        stamp = str(end_date)[:10]
        year, end_month = int(stamp[:4]), int(stamp[5:7])
    except (ValueError, IndexError):
        return None
    return year - 1 if month > end_month else year


def _window_impl(text: str, end_date: str | None = None) -> tuple[int, int, int] | None:
    """Canonical window end ``(year, month, day)`` for a question/event title.

    ``in November`` is resolved against ``end_date`` (the market's settlement
    date); ``before 2027`` and ``by Jan 1, 2027`` both normalise to 2027-01-01
    because they cover the same tape.
    """
    raw = text or ""
    match = _WINDOW_BY.search(raw)
    if match:
        return (int(match.group(3)), MONTHS[match.group(1).lower()], int(match.group(2)))
    match = _WINDOW_ON_DAY.search(raw)
    if match:
        return (int(match.group(3)), MONTHS[match.group(1).lower()], int(match.group(2)))
    match = _WINDOW_BY_DAY.search(raw)
    if match:
        month, day = MONTHS[match.group(1).lower()], int(match.group(2))
        year = _resolve_year(month, end_date)
        if year is not None:
            return (year, month, day)
    match = _WINDOW_BEFORE_DAY.search(raw)
    if match:
        return (int(match.group(3)), MONTHS[match.group(1).lower()], int(match.group(2)))
    match = _WINDOW_BEFORE_YEAR.search(raw)
    if match:
        return (int(match.group(1)), 1, 1)
    match = _WINDOW_IN.search(raw)
    if match:
        year, month = int(match.group(2)), MONTHS[match.group(1).lower()]
        return (year, month, _days_in_month(year, month))
    match = _WINDOW_BARE.search(raw)
    if match:
        year, month = int(match.group(2)), MONTHS[match.group(1).lower()]
        return (year, month, _days_in_month(year, month))
    match = _WINDOW_IN_MONTH.search(raw)
    if match:
        month = MONTHS[match.group(1).lower()]
        year = _resolve_year(month, end_date)
        if year is not None:
            return (year, month, _days_in_month(year, month))
    match = _WINDOW_IN_YEAR.search(raw)
    if match:
        return (int(match.group(1)), 12, 31)
    if end_date:
        try:
            stamp = str(end_date)[:10]
            year, month = int(stamp[:4]), int(stamp[5:7])
            return (year, month, _days_in_month(year, month))
        except (ValueError, IndexError):
            return None
    return None


def _window(text: str, end_date: str | None = None) -> tuple[int, int, int] | None:
    """``_window_impl`` plus the Jan-1 / Dec-31 equivalence.

    "by Jan 1, 2027 at 12:00am" and "by December 31, 2026 at 11:59pm" are the
    same instant, so both canonicalise to 2026-12-31.
    """
    window = _window_impl(text, end_date)
    if window and window[1] == 1 and window[2] == 1:
        return (window[0] - 1, 12, 31)
    return window


def _window_label(window: tuple[int, int, int] | None) -> str:
    if not window:
        return ""
    return f"{window[0]:04d}-{window[1]:02d}-{window[2]:02d}"


def _topic(text: str) -> str | None:
    lowered = normalise(text)
    for topic, pattern in TOPIC_PATTERNS:
        if pattern.search(lowered):
            return topic
    return None


def _pct(text: str) -> float | None:
    match = _PCT.search(normalise(text))
    return float(match.group(1)) if match else None


def _cents(value: float) -> int:
    return int(round(value * 100))


def _numbers(text: str) -> frozenset[int]:
    values = []
    for raw in re.findall(r"-?\d+(?:\.\d+)?", normalise(text)):
        try:
            number = float(raw)
        except ValueError:
            continue
        if 1900 <= abs(number) <= 2100 and number == int(number):
            continue
        values.append(int(round(number * 1000)))
    return frozenset(values)


def _claim_tokens(text: str) -> tuple[frozenset[str], int, int, bool]:
    """Tokens after folding polarity into one marker per claim.

    Returns ``(tokens, polarity, sign, has_polarity)`` where ``polarity`` is the
    count of negation words and ``sign`` is +1/-1/0 for positive/negative/
    unmarked direction words.
    """
    raw = normalise(text).split()
    negations = sum(1 for word in raw if word in _NEGATIONS)
    kept: set[str] = set()
    sign = 0
    for token in tokenise(text):
        if token in _POL_POS:
            kept.add("__dirpos")
            sign += 1
        elif token in _POL_NEG:
            kept.add("__dirneg")
            sign -= 1
        else:
            kept.add(token)
    return frozenset(kept), negations, sign, bool(sign)


def _claim_overlap(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def direction_for(a_text: str, b_text: str, *, min_normal: float = 0.5) -> str | None:
    """``normal`` / ``inverted`` for two claim texts, ``None`` when ambiguous.

    Inverted is only claimed when the two texts are the same claim with the
    polarity flipped (near-identical remainder tokens and equal numbers).
    """
    a, a_neg, a_sign, a_has = _claim_tokens(a_text)
    b, b_neg, b_sign, b_has = _claim_tokens(b_text)
    if _numbers(a_text) != _numbers(b_text):
        return None
    overlap = _claim_overlap(a, b)
    flipped = (a_has and b_has and a_sign * b_sign < 0) or ((a_neg % 2) != (b_neg % 2))
    if flipped:
        if (a_has and b_has and a_sign * b_sign < 0) and (a_neg % 2) != (b_neg % 2):
            return None  # double inversion: too easy to get wrong
        return "inverted" if overlap >= 0.85 else None
    return "normal" if overlap >= min_normal else None


def _is_combo(title: str) -> bool:
    """A joint claim ("governor and senate winner") is not a single contest."""
    lowered = normalise(title)
    if "combo" in lowered or "combined" in lowered or "both" in lowered:
        return True
    offices = {word for word in _OFFICE_WORDS if re.search(rf"\b{word}\b", lowered)}
    if len(offices) > 1:
        return True
    return False


def _fed_anchor(text: str, end_date: str | None = None):
    lowered = normalise(text)
    if not re.search(r"\bfed(?:eral reserve)?\b", lowered):
        return None
    if "meeting" not in lowered:
        return None
    if re.search(r"\b(pause|hike|cut)[ -]+(pause|hike|cut)\b", lowered):
        return None
    if re.search(r"\b(no change|maintain|hold|pause)\b", lowered):
        move, bps = "hold", 0.0
    elif re.search(r"\b(increase|hike|raise|raises)\b", lowered):
        move, bps = "hike", _BPS_OR_DEFAULT(lowered, 25.0)
    elif re.search(r"\b(decrease|cut|lower|lowers)\b", lowered):
        move, bps = "cut", _BPS_OR_DEFAULT(lowered, 25.0)
    else:
        return None
    window = _window(text, end_date)
    return {"topic": "fed_decision", "move": move, "bps": bps, "window": window}


def _BPS_OR_DEFAULT(text: str, default: float) -> float:
    match = _BPS.search(text)
    return float(match.group(1)) if match else default


def _fed_event_month(event_ticker: str | None) -> tuple[int, int] | None:
    if not event_ticker:
        return None
    match = re.search(
        r"-(\d{2})(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$",
        event_ticker,
        re.IGNORECASE,
    )
    if not match:
        return None
    year = 2000 + int(match.group(1))
    month = MONTHS[match.group(2).lower()]
    return (year, month)


def _match_fed(pm_anchor: dict, market: dict) -> tuple[float, str] | None:
    sub = (market.get("yes_sub_title") or "").strip()
    if sub == "Hike 25bps":
        move, bps = "hike", 25.0
    elif sub == "Cut 25bps":
        move, bps = "cut", 25.0
    elif sub.lower() == "fed maintains rate":
        move, bps = "hold", 0.0
    else:
        return None
    if (move, bps) != (pm_anchor["move"], pm_anchor["bps"]):
        return None
    month = _fed_event_month(market.get("event_ticker"))
    pm_window = pm_anchor["window"]
    if not month or not pm_window or (pm_window[0], pm_window[1]) != month:
        return None
    return (
        0.93,
        f"Fed meeting {month[0]}-{month[1]:02d}; PM move={move} {bps:g}bps == "
        f"Kalshi '{sub}'",
    )


def _treasury_anchor(text: str, end_date: str | None = None):
    lowered = normalise(text)
    if "treasury yield" not in lowered:
        return None
    match = re.search(r"\b(\d+)\s*-?\s*(?:year|yr)\b", lowered)
    if not match:
        match = re.search(r"\b(\d+)\s*y\b", lowered)
    if not match:
        return None
    tenor = int(match.group(1))
    if re.search(r"\b(hit|hits|above|exceed|exceeds|reach|reaches|high)\b", lowered):
        kind = "max"
    elif re.search(r"\b(dip|dips|below|fall|falls|drop|drops|low)\b", lowered):
        kind = "min"
    else:
        return None
    threshold = _pct(lowered)
    if threshold is None:
        return None
    return {
        "topic": "treasury_bound",
        "tenor": tenor,
        "kind": kind,
        "threshold": threshold,
        "window": _window(text, end_date),
    }


def _sub_bound(sub_title: str | None) -> tuple[str, int] | None:
    """``'5.06% or above'`` -> ``('max', 506)``; ``'4.75% or below'`` -> min."""
    if not sub_title:
        return None
    lowered = normalise(sub_title)
    value = _pct(lowered)
    if value is None:
        return None
    if "above" in lowered:
        return ("max", _cents(value))
    if "below" in lowered:
        return ("min", _cents(value))
    return None


def _match_treasury(pm_anchor: dict, market: dict) -> tuple[float, str] | None:
    title = normalise(market.get("title") or "")
    match = re.search(r"\b(\d+)\s*-?\s*(?:year|yr)\b", title) or re.search(
        r"\b(\d+)\s*y\b", title
    )
    if not match or int(match.group(1)) != pm_anchor["tenor"]:
        return None
    bound = _sub_bound(market.get("yes_sub_title"))
    if not bound:
        return None
    kind, kalshi_cents = bound
    if kind != pm_anchor["kind"]:
        return None
    if pm_anchor["kind"] == "max":
        pm_cents = _cents(pm_anchor["threshold"])  # "hit 5.06" == ">= 5.06"
    else:
        pm_cents = _cents(pm_anchor["threshold"]) - 1  # "dip below 4.76" == "<= 4.75"
    if pm_cents != kalshi_cents:
        return None
    if pm_anchor["window"] != _window(market.get("title") or ""):
        return None
    label = f"{pm_anchor['tenor']}Y {pm_anchor['kind']} {pm_anchor['threshold']:g}%"
    return (
        0.90,
        f"Treasury {label}, window {_window_label(pm_anchor['window'])}; "
        f"Kalshi sub '{market.get('yes_sub_title')}' resolves the same boundary",
    )


def _threshold_op(text: str) -> tuple[str, float] | tuple[str, float, float] | None:
    lowered = normalise(text)
    match = re.search(r"\bbetween\s+(\d+(?:\.\d+)?)\s*(k)?\s+and\s+(\d+(?:\.\d+)?)\s*(k)?", lowered)
    if match:
        low = float(match.group(1)) * (1000 if match.group(2) else 1)
        high = float(match.group(3)) * (1000 if match.group(4) else 1)
        return ("interval", low, high)
    for pattern, op in (
        (r"\bat least\s+(\d+(?:\.\d+)?)\s*(k)?\b", ">="),
        (r"\b(\d+(?:\.\d+)?)\s*(k)?\s*or more\b", ">="),
        (r"\bmore than\s+(\d+(?:\.\d+)?)\s*(k)?\b", ">"),
        (r"\babove\s+(\d+(?:\.\d+)?)\s*(k)?\b", ">"),
        (r"\bbelow\s+(\d+(?:\.\d+)?)\s*(k)?\b", "<"),
        (r"\bless than\s+(\d+(?:\.\d+)?)\s*(k)?\b", "<"),
        (r"\b(\d+(?:\.\d+)?)\s*(k)?\s*or fewer\b", "<="),
    ):
        found = re.search(pattern, lowered)
        if found:
            value = float(found.group(1)) * (1000 if found.group(2) else 1)
            return (op, value)
    exact = re.search(r"\bbe\s+(\d+(?:\.\d+)?)\s*(%|pct)", lowered)
    if exact:
        return ("exact", float(exact.group(1)))
    return None


def _match_payrolls(pm_row: dict, market: dict) -> tuple[float, str] | tuple[None, str] | None:
    question = pm_row.get("question") or ""
    lowered = normalise(question)
    if "jobs" not in lowered:
        return None
    title = normalise(market.get("title") or "")
    found = re.search(r"\babove\s+(\d+(?:\.\d+)?)\s+jobs\b", title)
    if not found:
        return None
    kalshi_value = float(found.group(1))
    if _window(question, pm_row.get("end_date")) != _window(market.get("title") or ""):
        return None
    pm_threshold = _threshold_op(question)
    if pm_threshold is None:
        return (None, "job threshold not parseable")
    if len(pm_threshold) == 3:
        return (None, "PM job bucket is an interval; Kalshi is one-sided 'above N'")
    op, value = pm_threshold
    if op == ">=":
        return (
            None,
            f"PM 'at least {value:,.0f}' is inclusive at the boundary; Kalshi "
            f"'above {kalshi_value:,.0f}' is strict",
        )
    if op == ">" and value == kalshi_value:
        return (0.82, f"Jobs threshold > {value:,.0f} identical on both venues")
    return (None, f"job threshold mismatch: PM {op} {value:,.0f} vs Kalshi > {kalshi_value:,.0f}")


def _fed_count_anchor(text: str, end_date: str | None = None):
    lowered = normalise(text)
    if not re.search(r"\bfed\b", lowered) or not re.search(r"\brate (?:cuts?|hikes?)\b", lowered):
        return None
    if "cut" not in lowered:
        return {"series": "hikes", "count": None, "window": _window(text, end_date)}
    match = re.search(r"\bexactly\s+(\d+)\b", lowered) or re.search(r"\b(\d+)\s+fed rate cuts?\b", lowered)
    if match:
        count = int(match.group(1))
    elif re.search(r"\b(no|zero)\b", lowered):
        count = 0
    else:
        return None
    return {"series": "cuts", "count": count, "window": _window(text, end_date)}


def _event_year(event_ticker: str | None) -> int | None:
    if not event_ticker:
        return None
    match = re.search(
        r"-(\d{2})(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(\d{2})?",
        event_ticker,
        re.IGNORECASE,
    )
    return 2000 + int(match.group(1)) if match else None


def _match_fed_count(anchor: dict, market: dict) -> tuple[float, str] | None:
    if anchor["series"] != "cuts" or anchor["count"] is None:
        return None
    series = str(market.get("series_ticker") or "").upper()
    if series != "KXRATECUTCOUNT":
        return None
    sub = market.get("yes_sub_title") or ""
    match = re.search(r"exactly\s+(\d+)\s+cuts", normalise(sub))
    if not match or int(match.group(1)) != anchor["count"]:
        return None
    if _event_year(market.get("event_ticker")) != anchor["window"][0]:
        return None
    return (
        0.90,
        f"Fed cuts in {anchor['window'][0]}: PM count={anchor['count']} == "
        f"Kalshi '{sub}'",
    )


_ASSETS = {
    "bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL", "xrp": "XRP",
    "dogecoin": "DOGE", "doge": "DOGE",
}
_DOLLARS = re.compile(r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*([km])?", re.IGNORECASE)


def _asset(text: str) -> str | None:
    lowered = normalise(text)
    for word, code in _ASSETS.items():
        if re.search(rf"\b{word}\b", lowered):
            return code
    return None


def _dollars(text: str) -> float | None:
    match = _DOLLARS.search(text or "")
    if not match:
        return None
    value = float(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return value


def _crypto_anchor(text: str, end_date: str | None = None):
    """Running max/min price claim for one asset and one deadline."""
    lowered = normalise(text)
    asset = _asset(lowered)
    if not asset:
        return None
    # These are not price-bound claims, or the observation time is ambiguous.
    if re.search(r"volatility|up or down|all time high|satoshi|will .* say|outperform", lowered):
        return None
    if re.search(r"\bprice of .* be (above|below)\b", lowered) and " by " not in f" {lowered} ":
        return None
    if re.search(r"\b(reach|hit|above|exceed)\b", lowered):
        kind = "max"
    elif re.search(r"\b(dip|below|fall|drop)\b", lowered):
        kind = "min"
    else:
        return None
    value = _dollars(text)
    if value is None:
        return None
    window = _window(text, end_date)
    if window is None:
        return None
    return {
        "topic": "crypto_bound",
        "asset": asset,
        "kind": kind,
        "threshold": value,
        "window": window,
    }


def _match_crypto(pm_anchor: dict, market: dict) -> tuple[float, str] | None:
    title = market.get("title") or ""
    lowered = normalise(title)
    if _asset(lowered) != pm_anchor["asset"]:
        return None
    if "trimmed mean" in lowered:
        return None
    series = str(market.get("series_ticker") or "").upper()
    if (
        series.endswith(("MAXY", "MAXMON"))
        or "how high" in lowered
        or "reach above" in lowered
    ):
        kind = "max"
    elif (
        series.endswith(("MINY", "MINMON"))
        or "how low" in lowered
        or "reach below" in lowered
    ):
        kind = "min"
    else:
        return None
    if kind != pm_anchor["kind"]:
        return None
    if _window(title) != pm_anchor["window"]:
        return None
    if kind == "max":
        boundary = market.get("floor_strike")
    else:
        boundary = market.get("cap_strike")
    if boundary is None:
        return None
    boundary_cents = int(round(float(boundary) * 100))
    pm_cents = int(round(pm_anchor["threshold"] * 100))
    if kind == "max":
        exact = boundary_cents + 1 == pm_cents
        adjacent = boundary_cents == pm_cents
    else:
        exact = boundary_cents == pm_cents + 1
        adjacent = boundary_cents == pm_cents
    if not (exact or adjacent):
        return None
    confidence = 0.90 if exact else 0.80
    note = "" if exact else " (Kalshi bound is strict, PM is inclusive: one cent)"
    label = f"{pm_anchor['asset']} {kind} ${pm_anchor['threshold']:,.0f}"
    return (
        confidence,
        f"Crypto {label}, window {_window_label(pm_anchor['window'])}; "
        f"Kalshi '{market.get('yes_sub_title')}'{note}",
    )


def _match_macro(pm_row: dict, markets: list[dict]) -> tuple[dict, str | None]:
    """Anchored macro match. Returns ``(match_dict_or_empty, rejection_reason)``.

    ``markets`` is pre-filtered to the question's topic by title; the anchored
    rules below then enforce window and threshold equality.
    """
    question = pm_row.get("question") or ""
    topic = _topic(question)
    if topic == "fed_decision":
        anchor = _fed_anchor(question, pm_row.get("end_date"))
        if anchor:
            for market in markets:
                result = _match_fed(anchor, market)
                if result:
                    return {
                        "market": market,
                        "confidence": result[0],
                        "reason": result[1],
                        "topic": "fed_decision",
                    }, None
            return {}, "no Fed meeting/bucket matches the question"
        return {}, "Fed question is not a single-meeting 25bp/hold bucket"
    if topic == "treasury_bound":
        anchor = _treasury_anchor(question, pm_row.get("end_date"))
        if anchor:
            for market in markets:
                result = _match_treasury(anchor, market)
                if result:
                    return {
                        "market": market,
                        "confidence": result[0],
                        "reason": result[1],
                        "topic": "treasury_bound",
                    }, None
            return {}, "no Kalshi yield rung resolves the same threshold and window"
        return {}, "yield question has no single tenor/threshold"
    if topic == "fed_count":
        anchor = _fed_count_anchor(question, pm_row.get("end_date"))
        if anchor:
            for market in markets:
                result = _match_fed_count(anchor, market)
                if result:
                    return {
                        "market": market,
                        "confidence": result[0],
                        "reason": result[1],
                        "topic": "fed_count",
                    }, None
            if anchor["series"] == "hikes":
                return {}, "no Kalshi series counts Fed hikes by year"
            return {}, "no exact cut-count rung for the year"
        return {}, "Fed count question is not an exact count"
    if topic == "crypto":
        anchor = _crypto_anchor(question, pm_row.get("end_date"))
        if anchor:
            for market in markets:
                result = _match_crypto(anchor, market)
                if result:
                    return {
                        "market": market,
                        "confidence": result[0],
                        "reason": result[1],
                        "topic": "crypto_bound",
                    }, None
            return {}, "no Kalshi rung resolves the same asset/kind/threshold/window"
        return {}, "crypto question is not a running max/min price bound"
    if topic == "payrolls":
        reasons = []
        for market in markets:
            result = _match_payrolls(pm_row, market)
            if result and result[0] is not None:
                return {
                    "market": market,
                    "confidence": result[0],
                    "reason": result[1],
                    "topic": "payrolls",
                }, None
            if result and result[1]:
                reasons.append(result[1])
        return {}, reasons[0] if reasons else "no payrolls series window match"
    if topic in MACRO_TOPICS:
        return {}, f"claim kind not equivalent on Kalshi ({topic})"
    return {}, "not a macro question"


def fetch_series_markets_v2(
    client: KalshiClient,
    series: list[str],
    *,
    pages: int = 2,
) -> list[dict]:
    rows: list[dict] = []
    for index, name in enumerate(series, start=1):
        cursor: str | None = None
        for _ in range(max(1, pages)):
            params: dict[str, object] = {
                "series_ticker": name,
                "status": "open",
                "limit": MAX_PAGE_SIZE,
            }
            if cursor:
                params["cursor"] = cursor
            try:
                payload = client.get_json("/markets", params)
            except Exception as exc:  # noqa: BLE001 - one bad series is not fatal
                log.warning(f"macro {name}: {type(exc).__name__}: {exc}")
                break
            for raw in payload.get("markets", []):
                if isinstance(raw, dict) and raw.get("ticker"):
                    rows.append(
                        {
                            "ticker": raw.get("ticker"),
                            "event_ticker": raw.get("event_ticker"),
                            "series_ticker": raw.get("series_ticker") or name,
                            "title": raw.get("title"),
                            "yes_sub_title": raw.get("yes_sub_title"),
                            "strike_type": raw.get("strike_type"),
                            "floor_strike": raw.get("floor_strike"),
                            "cap_strike": raw.get("cap_strike"),
                            "status": raw.get("status"),
                            "close_time": raw.get("close_time"),
                        }
                    )
            cursor = payload.get("cursor")
            if not cursor:
                break
            time.sleep(REQUEST_SPACING)
        if index % 10 == 0:
            log.info(f"macro series: {index}/{len(series)}, {len(rows)} markets")
        time.sleep(REQUEST_SPACING)
    return rows


def fetch_tickers_live(client: KalshiClient, tickers: list[str], chunk: int = 100) -> dict[str, dict]:
    """Authoritative state of the proposed tickers, 100 per request."""
    out: dict[str, dict] = {}
    ordered = sorted(set(tickers))
    for start in range(0, len(ordered), chunk):
        batch = ordered[start : start + chunk]
        try:
            payload = client.get_json(
                "/markets", {"tickers": ",".join(batch), "limit": MAX_PAGE_SIZE}
            )
        except Exception as exc:  # noqa: BLE001 - a failed batch is not fatal
            log.warning(f"tickers batch {start}: {type(exc).__name__}: {exc}")
            continue
        for raw in payload.get("markets", []):
            if isinstance(raw, dict) and raw.get("ticker"):
                out[str(raw["ticker"])] = raw
        time.sleep(REQUEST_SPACING)
    return out


def macro_series_for(universe: list[dict], events: list[dict]) -> tuple[list[str], dict[str, int], dict[str, int]]:
    """Series that host an event whose topic *and* window match a PM question."""
    topic_windows: dict[str, set] = {}
    for row in universe:
        question = row.get("question") or ""
        topic = _topic(question)
        if topic not in MACRO_TOPICS:
            continue
        window = _window(question, row.get("end_date"))
        if window:
            topic_windows.setdefault(topic, set()).add(window)
    if not topic_windows:
        return [], {}, {}
    series: set[str] = set()
    by_topic: dict[str, int] = {}
    for event in events:
        name = event.get("series_ticker")
        if not name:
            continue
        topic = _topic(event.get("title") or "")
        if topic not in topic_windows:
            continue
        window = _window(event.get("title") or "", event.get("close_time"))
        if window is None or window not in topic_windows[topic]:
            continue
        series.add(str(name))
        by_topic[str(name)] = 1
    return sorted(series), topic_windows, by_topic


def build_v2_election(
    universe: list[dict],
    kalshi_rows: list[dict],
    *,
    min_confidence: float,
) -> tuple[list[dict], dict[str, int]]:
    """Structural/title matches restricted to the audited methods."""
    from polymarketbot.data.kalshi_mapping import extract_features, score_pair

    index: dict[str, list[int]] = {}
    structural: dict[tuple, list[int]] = {}
    features = []
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

    rejected: dict[str, int] = {}
    out: list[dict] = []
    for pm_row in universe:
        condition_id = pm_row.get("condition_id")
        question = pm_row.get("question") or ""
        if not condition_id or not question:
            continue
        if _topic(question) in MACRO_TOPICS:
            continue  # anchored path only
        if re.search(r"\bperform best\b|\bbest among\b|\bany of\b", normalise(question)):
            rejected["composite_question"] = rejected.get("composite_question", 0) + 1
            continue
        pm = extract_features(question, end_date=pm_row.get("end_date"))
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
        best = None
        for position, _shared in ordered[:400]:
            match = score_pair(pm, features[position])
            if match is None or match.confidence < min_confidence:
                continue
            row = kalshi_rows[position]
            title = row.get("title") or ""
            if match.method not in V2_ELECTION_METHODS:
                rejected[f"method:{match.method}"] = rejected.get(f"method:{match.method}", 0) + 1
                continue
            if _is_combo(title):
                rejected["combo_claim"] = rejected.get("combo_claim", 0) + 1
                continue
            # A ticker suffix can fake structure: ``KXNFLGAME-...-NYG`` parses as
            # "New York governor". The claim has to name the office in words.
            if match.method == "structured_winner" and not _office_word_present(pm.office, title):
                rejected["office_word_missing"] = rejected.get("office_word_missing", 0) + 1
                continue
            if match.method in {"title_fuzzy", "title_exact"}:
                # Only windows written in the text are evidence; a settlement
                # date is not, because the two venues close at different times.
                pm_window = _window(question)
                kalshi_window = _window(title)
                if (pm_window or kalshi_window) and pm_window != kalshi_window:
                    rejected["window_mismatch"] = rejected.get("window_mismatch", 0) + 1
                    continue
            if match.method == "structured_winner":
                if _negations_in(question) or _negations_in(title):
                    direction = direction_for(question, title)
                    if direction is None:
                        rejected["direction_ambiguous"] = rejected.get("direction_ambiguous", 0) + 1
                        continue
                else:
                    direction = "normal"
            else:
                direction = direction_for(question, title)
                if direction is None:
                    rejected["direction_ambiguous"] = rejected.get("direction_ambiguous", 0) + 1
                    continue
            if best is None or match.confidence > best[1].confidence:
                best = (row, match, direction)
        if best is None:
            continue
        row, match, direction = best
        out.append(
            {
                "market": row,
                "confidence": match.confidence,
                "reason": _election_reason(pm, match, direction),
                "direction": direction,
                "method": match.method,
                "_pm_condition": condition_id,
            }
        )
    return out, rejected


def _negations_in(text: str) -> bool:
    return any(word in _NEGATIONS for word in normalise(text).split())


def _election_reason(pm, match, direction: str) -> str:
    parts = [f"method={match.method}"]
    if pm.office:
        parts.append(f"office={pm.office}")
    if pm.state:
        parts.append(f"state={pm.state}")
    if pm.district is not None:
        parts.append(f"district={pm.district}")
    if pm.party:
        parts.append(f"party={pm.party}")
    if pm.year:
        parts.append(f"year={pm.year}")
    if pm.kind:
        parts.append(f"kind={pm.kind}")
    parts.append(f"direction={direction}")
    return "; ".join(parts)


def write_v2(rows: list[dict], path: Path = V2_PATH) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema(
        [(name, pa.string() if kind == "string" else pa.float64()) for name, kind in V2_SCHEMA]
    )
    materialised = [{column: row.get(column) for column in V2_COLUMNS} for row in rows]
    table = pa.Table.from_pylist(materialised, schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")
    return table.num_rows


def main_v2(args) -> int:
    started = time.monotonic()
    universe = load_universe(args.manifest)
    clients = [KalshiClient(args.rest_url, retry_total=3) for _ in range(3)]
    client = clients[0]
    try:
        events = fetch_events(
            client,
            cache=args.cache,
            refresh=args.refresh,
            max_events=args.max_events,
            status=args.status,
        )
        macro_series, topic_windows, _ = macro_series_for(universe, events)
        if args.macro_series_limit:
            macro_series = macro_series[: args.macro_series_limit]
        log.info(f"v2 macro anchors: {len(macro_series)} series {macro_series}")

        macro_markets: list[dict] = []
        if macro_series:
            if MACRO_CACHE.exists() and not (args.refresh or args.refresh_markets):
                macro_markets = json.loads(MACRO_CACHE.read_text(encoding="utf-8"))
                log.info(f"macro cache: {len(macro_markets)} markets")
            else:
                macro_markets = fetch_series_markets_v2(client, macro_series)
                MACRO_CACHE.parent.mkdir(parents=True, exist_ok=True)
                MACRO_CACHE.write_text(json.dumps(macro_markets), encoding="utf-8")

        # Election-side universe: the cached market crawl plus any live,
        # structurally-gated series that the cache does not cover yet.
        kalshi_rows: list[dict] = []
        if MARKETS_CACHE.exists():
            kalshi_rows = json.loads(MARKETS_CACHE.read_text(encoding="utf-8"))
            log.info(f"markets cache: {len(kalshi_rows)} rows")
        else:
            candidates = prefilter_events(universe, events)
            kalshi_rows = fetch_event_markets(
                clients,
                candidates,
                max_fetches=args.max_event_fetches,
                series_min_group=args.series_min_group,
                series_pages=args.series_pages,
            )
            MARKETS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            MARKETS_CACHE.write_text(json.dumps(kalshi_rows), encoding="utf-8")

        cached_series = {str(row.get("series_ticker")) for row in kalshi_rows if row.get("series_ticker")}
        extra_series: list[str] = []
        if args.extra_series:
            candidates = prefilter_events(universe, events)
            wanted = [
                str(event["series_ticker"])
                for event in candidates
                if event.get("series_ticker")
                and str(event["series_ticker"]) not in cached_series
                and float(event.get("priority", 0)) >= 0.75
            ]
            seen: set[str] = set()
            for name in wanted:
                if name not in seen:
                    seen.add(name)
                    extra_series.append(name)
            extra_series = extra_series[: args.extra_series]
            if extra_series:
                log.info(f"v2 extra election series: {len(extra_series)} {extra_series}")
                kalshi_rows.extend(fetch_series_markets_v2(client, extra_series, pages=1))

        rows: list[dict] = []
        rejected: dict[str, int] = {}
        # ---- macro side
        for pm_row in universe:
            if _topic(pm_row.get("question") or "") not in MACRO_TOPICS:
                continue
            if not pm_row.get("condition_id"):
                continue
            topic = _topic(pm_row.get("question") or "")
            themed = [
                market
                for market in macro_markets
                if _topic(market.get("title") or "") == topic
            ]
            match, reason = _match_macro(pm_row, themed)
            if match:
                rows.append(
                    {
                        "market": match["market"],
                        "confidence": match["confidence"],
                        "reason": match["reason"],
                        "direction": "normal",
                        "method": match["topic"],
                        "_pm_condition": pm_row.get("condition_id"),
                    }
                )
            elif reason:
                rejected[f"macro:{reason}"] = rejected.get(f"macro:{reason}", 0) + 1
        # ---- election / other side
        election_rows, election_rejected = build_v2_election(
            universe, kalshi_rows, min_confidence=args.min_confidence
        )
        rows.extend(election_rows)
        for key, value in election_rejected.items():
            rejected[key] = rejected.get(key, 0) + value

        # One Polymarket question picks the strongest Kalshi market; one
        # Kalshi ticker answers one Polymarket question.
        rows = _associate_pm(rows, universe)
        best_by_pm: dict[str, dict] = {}
        for row in rows:
            key = str(row["_pm_row"].get("condition_id"))
            current = best_by_pm.get(key)
            if current is None or row["confidence"] > current["confidence"]:
                best_by_pm[key] = row
        final_rows = _finalise_rows(list(best_by_pm.values()), universe)

        # ---- live validation: only open markets, authoritative sub-titles
        live = fetch_tickers_live(client, [row["kalshi_ticker"] for row in final_rows])
        active = {"active", "open", "initialized"}
        validated: list[dict] = []
        dropped_closed = 0
        for row in final_rows:
            raw = live.get(row["kalshi_ticker"])
            if raw is None or str(raw.get("status", "")).lower() not in active:
                dropped_closed += 1
                continue
            row["kalshi_yes_sub_title"] = raw.get("yes_sub_title") or row.get("kalshi_yes_sub_title")
            row["kalshi_title"] = raw.get("title") or row["kalshi_title"]
            validated.append(row)
        rows = validated

        written = write_v2(rows)
        summary = _v2_summary(
            rows,
            universe,
            macro_series,
            macro_markets,
            kalshi_rows,
            written,
            dropped_closed,
            rejected,
            client.stats.requests,
            time.monotonic() - started,
        )
        V2_SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log.info(
            f"v2: {written} pairs, {summary['distinct_events']} events, "
            f"{summary['direction']} -> {V2_PATH}"
        )
        return 0
    finally:
        for item in clients:
            item.close()


def _associate_pm(rows: list[dict], universe: list[dict]) -> list[dict]:
    """Attach the Polymarket row to each match by condition id."""
    by_condition = {str(pm.get("condition_id")): pm for pm in universe if pm.get("condition_id")}
    out = []
    for row in rows:
        pm_row = by_condition.get(str(row.get("_pm_condition")))
        if pm_row is None:
            continue
        row["_pm_row"] = pm_row
        out.append(row)
    return out


def _finalise_rows(rows: list[dict], universe: list[dict]) -> list[dict]:
    final: list[dict] = []
    for row in rows:
        market = row["market"]
        pm_row = row["_pm_row"]
        tokens = pm_row.get("clob_tokens") or []
        final.append(
            {
                "kalshi_ticker": market.get("ticker"),
                "kalshi_event": market.get("event_ticker"),
                "polymarket_condition_id": pm_row.get("condition_id"),
                "polymarket_yes_token": tokens[0] if len(tokens) > 0 else None,
                "polymarket_no_token": tokens[1] if len(tokens) > 1 else None,
                "direction": row["direction"],
                "confidence": round(float(row["confidence"]), 4),
                "method": row["method"],
                "match_reason": row["reason"],
                "kalshi_title": row.get("kalshi_title") or market.get("title"),
                "kalshi_yes_sub_title": row.get("kalshi_yes_sub_title")
                or market.get("yes_sub_title"),
                "series_ticker": market.get("series_ticker"),
                "window": _window_label(_window(pm_row.get("question") or "", pm_row.get("end_date"))),
                "polymarket_question": pm_row.get("question"),
                "kalshi_event_ticker": market.get("event_ticker"),
            }
        )
    # one ticker -> the PM market with the strongest evidence
    best_by_ticker: dict[str, dict] = {}
    for row in final:
        ticker = row["kalshi_ticker"]
        current = best_by_ticker.get(ticker)
        if current is None or row["confidence"] > current["confidence"]:
            best_by_ticker[ticker] = row
    return sorted(
        best_by_ticker.values(),
        key=lambda row: (row["polymarket_condition_id"], row["kalshi_ticker"]),
    )


def _v2_summary(
    rows: list[dict],
    universe: list[dict],
    macro_series: list[str],
    macro_markets: list[dict],
    kalshi_rows: list[dict],
    written: int,
    dropped_closed: int,
    rejected: dict[str, int],
    requests: int,
    elapsed: float,
) -> dict:
    by_method: dict[str, int] = {}
    direction: dict[str, int] = {}
    buckets = {"0.5-0.6": 0, "0.6-0.7": 0, "0.7-0.8": 0, "0.8-0.9": 0, "0.9-1.0": 0}
    for row in rows:
        by_method[row["method"]] = by_method.get(row["method"], 0) + 1
        direction[row["direction"]] = direction.get(row["direction"], 0) + 1
        value = float(row["confidence"])
        if value < 0.6:
            buckets["0.5-0.6"] += 1
        elif value < 0.7:
            buckets["0.6-0.7"] += 1
        elif value < 0.8:
            buckets["0.7-0.8"] += 1
        elif value < 0.9:
            buckets["0.8-0.9"] += 1
        else:
            buckets["0.9-1.0"] += 1
    covered = {row["polymarket_condition_id"] for row in rows}
    pair_series = {row["series_ticker"] for row in rows if row["series_ticker"]}
    series_without = [name for name in macro_series if name not in pair_series]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "artifact": str(V2_PATH),
        "polymarket_markets": len(universe),
        "polymarket_mapped": len(covered),
        "coverage": round(len(covered) / max(1, len(universe)), 4),
        "pairs": written,
        "distinct_events": len({row["kalshi_event"] for row in rows}),
        "distinct_series": len(pair_series),
        "direction": dict(sorted(direction.items())),
        "confidence_buckets": buckets,
        "by_method": dict(sorted(by_method.items())),
        "macro_series_fetched": macro_series,
        "macro_series_without_pairs": series_without,
        "macro_markets_fetched": len(macro_markets),
        "election_markets_scanned": len(kalshi_rows),
        "dropped_not_live": dropped_closed,
        "rejected": dict(sorted(rejected.items())),
        "requests": requests,
        "elapsed_seconds": round(elapsed, 1),
    }


def main() -> int:
    global V2_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out", type=Path, default=MAPPING_PATH)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--refresh", action="store_true", help="re-crawl Kalshi events and markets")
    parser.add_argument(
        "--refresh-markets",
        action="store_true",
        help="re-fetch Kalshi markets (events crawl stays cached)",
    )
    parser.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS)
    parser.add_argument(
        "--max-event-fetches",
        type=int,
        default=DEFAULT_MAX_EVENT_FETCHES,
        help="budget in requests (a series costs its page allowance)",
    )
    parser.add_argument("--series-min-group", type=int, default=8)
    parser.add_argument("--series-pages", type=int, default=2)
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--min-shared-tokens", type=int, default=2)
    parser.add_argument("--rest-url", default="https://api.elections.kalshi.com/trade-api/v2")
    parser.add_argument("--status", default="open")
    parser.add_argument(
        "--artifact",
        choices=("v1", "v2"),
        default="v1",
        help="v1 keeps the legacy artifact; v2 writes kalshi_mapping_v2.parquet",
    )
    parser.add_argument(
        "--v2-out", type=Path, default=V2_PATH, help="v2 artifact path"
    )
    parser.add_argument(
        "--macro-series-limit",
        type=int,
        default=80,
        help="cap on macro series fetched for the v2 anchored matcher",
    )
    parser.add_argument(
        "--extra-series",
        type=int,
        default=30,
        help="v2: live election series not in the markets cache to fetch (0 disables)",
    )
    args = parser.parse_args()
    if args.artifact == "v2":
        V2_PATH = args.v2_out
        return main_v2(args)

    universe = load_universe(args.manifest)
    clients = [KalshiClient(args.rest_url, retry_total=4) for _ in range(4)]
    client = clients[0]
    try:
        events = fetch_events(
            client,
            cache=args.cache,
            refresh=args.refresh,
            max_events=args.max_events,
            status=args.status,
        )
        candidates = prefilter_events(
            universe, events, min_shared_tokens=args.min_shared_tokens
        )
        if MARKETS_CACHE.exists() and not (args.refresh or args.refresh_markets):
            kalshi_rows = json.loads(MARKETS_CACHE.read_text(encoding="utf-8"))
            log.info(f"markets cache: {len(kalshi_rows)} rows from {MARKETS_CACHE}")
        else:
            kalshi_rows = fetch_event_markets(
                clients,
                candidates,
                max_fetches=args.max_event_fetches,
                series_min_group=args.series_min_group,
                series_pages=args.series_pages,
            )
            MARKETS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            MARKETS_CACHE.write_text(json.dumps(kalshi_rows), encoding="utf-8")
    finally:
        for item in clients:
            item.close()

    if not kalshi_rows:
        raise SystemExit("no Kalshi markets fetched; nothing to map")

    rows = build_mapping(
        universe, kalshi_rows, min_confidence=args.min_confidence
    )
    written = write_mapping(rows, args.out)

    by_method: dict[str, int] = {}
    for row in rows:
        by_method[row["method"]] = by_method.get(row["method"], 0) + 1
    covered = {row["polymarket_condition_id"] for row in rows}
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "polymarket_markets": len(universe),
        "polymarket_mapped": len(covered),
        "coverage": round(len(covered) / max(1, len(universe)), 4),
        "kalshi_markets_scanned": len(kalshi_rows),
        "kalshi_events_scanned": len(events),
        "candidate_events": len(candidates),
        "events_considered": min(len(candidates), args.max_event_fetches),
        "min_confidence": args.min_confidence,
        "rows": written,
        "by_method": dict(sorted(by_method.items())),
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log.info(f"mapping: {written} rows, coverage {summary['coverage']:.1%} -> {args.out}")
    log.info(f"by method: {summary['by_method']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
