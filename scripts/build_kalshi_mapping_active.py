"""Build the Polymarket -> Kalshi mapping from the *active* Polymarket universe.

The v1/v2 artifacts were built from the Kalshi side: every open Kalshi series
was matched to a Polymarket question, which lands on reward-tail markets that
carry a pool but barely trade (median 2 ``book`` events per condition over six
hours, 47 of 107 with zero).  This script inverts the direction:

1. rank every Polymarket condition by its book-event count over the last six
   hours of the tape (``clob_events``, one hour partition at a time), keep the
   top 200;
2. attach metadata (question, slug, tokens, end date) from the universe
   snapshots -- the union of every snapshot in ``data/tape/universe``, because
   a condition can leave the current universe before the window closes;
3. match each condition to a Kalshi market by parsing the question into
   (subject, comparator, threshold, window) and testing that structure against
   the Kalshi catalog (``kalshi_markets_cache.json`` +
   ``kalshi_macro_markets_cache.json`` + the polled tickers in
   ``data/tape/kalshi_markets``).  Only pairs whose claim shape, threshold and
   window all agree are emitted; each carries a one-sentence justification;
4. measure the deliverable: book events per condition over the same window and
   the staleness of the Polymarket mid against the Kalshi book.

``pm_events_6h`` counts **condition-level** rows in ``clob_events`` -- the rows
the replay harness actually joins (``iter_events`` joins on ``condition_id``).
``price_change`` rows carry a NULL ``condition_id`` (the payload is a list of
per-asset changes) and cannot be attributed, so they are excluded on both sides
of the comparison; ``pm_book_snapshots_6h`` is the ``event_type='book'`` count
the earlier audit quoted, kept so the two measures stay comparable.

Usage
    .venv/Scripts/python.exe scripts/build_kalshi_mapping_active.py
    .venv/Scripts/python.exe scripts/build_kalshi_mapping_active.py --top 200 --date 2026-11-06
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from datetime import date as _date
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pandas as pd

TAPE = Path("data/tape")
DERIVED = Path("data/derived")
UNIVERSE_DIR = TAPE / "universe"
MARKETS_CACHE = DERIVED / "kalshi_markets_cache.json"
MACRO_CACHE = DERIVED / "kalshi_macro_markets_cache.json"
EVENTS_CACHE = DERIVED / "kalshi_events_cache.json"
OUT_PARQUET = DERIVED / "kalshi_mapping_active_v2.parquet"

V2_COLUMNS = [
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
    "kalshi_event_ticker",
]
NEW_COLUMNS = ["pm_events_6h", "match_method", "match_note"]
MEASURE_COLUMNS = ["pm_book_snapshots_6h", "kalshi_has_books", "pm_staleness_median_s", "pm_staleness_p90_s"]

# v2 compatibility: ``method`` keeps the coarse family v2 used, ``match_method``
# names the exact rule that fired.
METHOD_FAMILY = {
    "treasury_bound": "treasury_bound",
    "treasury_bound_quantised": "treasury_bound",
    "fed_decision": "fed_decision",
    "house_seats_exact": "structured_seats",
    "house_race_winner": "structured_winner",
    "removal_by_date": "structured_removal",
    "crypto_annual_bound": "crypto_bound",
    "margin_threshold": "structured_margin",
    "title_exact": "title_exact",
    "title_fuzzy": "title_fuzzy",
}

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
MONTH_RE = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


# --------------------------------------------------------------------------- #
# 1. rank the active universe
# --------------------------------------------------------------------------- #
def hour_partitions(day: str, hours: list[str]) -> list[Path]:
    out = []
    for h in hours:
        d = TAPE / "clob_events" / f"date={day}" / f"hour={h}"
        files = sorted(d.glob("*.parquet"))
        if not files:
            raise SystemExit(f"missing partition {d}")
        out.append(d)
    return out


def rank_conditions(day: str, hours: list[str]) -> tuple[Counter, Counter, dict[str, dict]]:
    """Count condition-level rows and book snapshots, one hour at a time."""
    events: Counter = Counter()
    snapshots: Counter = Counter()
    per_hour: dict[str, dict] = {}
    for part in hour_partitions(day, hours):
        expected = sum(pq.ParquetFile(f).metadata.num_rows for f in part.glob("*.parquet"))
        got = 0
        bba = 0
        for f in sorted(part.glob("*.parquet")):
            table = pq.read_table(f, columns=["condition_id", "event_type"])
            got += table.num_rows
            frame = table.to_pandas()
            bba += int((frame["event_type"] == "best_bid_ask").sum())
            frame = frame[frame["condition_id"].notna()]
            events.update(frame["condition_id"].value_counts().to_dict())
            snapshots.update(
                frame.loc[frame["event_type"] == "book", "condition_id"].value_counts().to_dict()
            )
        if got != expected:
            raise SystemExit(f"{part}: read {got} rows, metadata promised {expected}")
        per_hour[part.name] = {"rows": got, "expected": expected, "best_bid_ask": bba}
        print(f"  {part.name}: expected {expected:,} rows, read {got:,}; "
              f"best_bid_ask rows {bba:,}")
    total = sum(v["rows"] for v in per_hour.values())
    print(f"  six-hour window: {total:,} rows, {len(events):,} conditions with an event")
    return events, snapshots, per_hour


# --------------------------------------------------------------------------- #
# 2. metadata from the universe snapshots
# --------------------------------------------------------------------------- #
def load_universe() -> dict[str, dict]:
    markets: dict[str, dict] = {}
    for f in sorted(UNIVERSE_DIR.glob("*.json")):
        payload = json.loads(f.read_text(encoding="utf-8"))
        for m in payload.get("markets", []):
            # later snapshots win: they carry the freshest end_date / tokens
            markets[m["condition_id"]] = m
    print(f"  universe union: {len(markets):,} conditions over {len(list(UNIVERSE_DIR.glob('*.json')))} snapshots")
    return markets


# --------------------------------------------------------------------------- #
# 3. Kalshi catalog + structural matcher
# --------------------------------------------------------------------------- #
def load_kalshi_catalog(day: str) -> list[dict]:
    catalog: dict[str, dict] = {}

    def add(ticker: str, title: str, close: str | None, series: str | None, event: str | None,
            sub: str | None = None) -> None:
        if not ticker or not title:
            return
        prev = catalog.get(ticker)
        if prev is None or (not prev.get("close_time") and close):
            catalog[ticker] = {
                "ticker": ticker,
                "title": title,
                "close_time": close,
                "series_ticker": series or (ticker.split("-")[0] if "-" in ticker else ticker),
                "event_ticker": event or "-".join(ticker.split("-")[:2]),
                "yes_sub_title": sub,
            }

    for path in (MARKETS_CACHE, MACRO_CACHE):
        for m in json.loads(path.read_text(encoding="utf-8")):
            add(m.get("ticker"), m.get("title"), m.get("close_time"), m.get("series_ticker"),
                m.get("event_ticker"), m.get("yes_sub_title"))
    for part_dir in sorted((TAPE / "kalshi_markets" / f"date={day}").glob("hour=*")):
        for f in sorted(part_dir.glob("*.parquet")):
            t = pq.read_table(f, columns=["ticker", "title", "event_ticker", "series_ticker"]).to_pandas()
            for row in t.drop_duplicates("ticker").itertuples(index=False):
                add(row.ticker, row.title, None, row.series_ticker, row.event_ticker)
    # yesterday's polled set still has proper titles and can be matched against
    for part_dir in sorted((TAPE / "kalshi_markets" / "date=2026-11-05").glob("hour=*")):
        for f in sorted(part_dir.glob("*.parquet")):
            t = pq.read_table(f, columns=["ticker", "title"]).to_pandas()
            for row in t.drop_duplicates("ticker").itertuples(index=False):
                add(row.ticker, row.title, None, None, None)
    print(f"  kalshi catalog: {len(catalog):,} distinct tickers")
    return list(catalog.values())


def month_key(text: str) -> int | None:
    t = text.strip().lower()[:3]
    return MONTHS.get(t)


def parse_pm(question: str) -> dict:
    """Parse a Polymarket question into the text and content-token set."""
    q = question.strip().rstrip("?").strip()
    low = q.lower()
    stop = {"will", "the", "be", "a", "an", "of", "in", "on", "by", "to", "and", "or", "for",
            "at", "is", "from", "with", "this", "that", "there", "than", "more", "less", "least",
            "most", "after", "before", "next", "market", "win", "winning", "party", "candidate",
            "as"}
    tokens = {w for w in re.findall(r"[a-z0-9$%]+", low) if w not in stop and len(w) > 1}
    return {"q": q, "low": low, "tokens": tokens}


def close_date(entry: dict) -> _date | None:
    ct = entry.get("close_time")
    if not ct:
        return None
    try:
        return _date.fromisoformat(ct[:10])
    except ValueError:
        return None


def same_window(entry: dict, month: int, year: int | None) -> bool:
    """Kalshi close month must equal the Polymarket resolution month."""
    cd = close_date(entry)
    if cd is None:
        return False
    if year is not None and cd.year != year and not (year == cd.year - 1 and cd.month == 1 and month == 12):
        return False
    return cd.month == month or (month == 12 and cd.month == 1)


def match_treasury(pm: dict, catalog: list[dict]) -> list[dict]:
    m = re.search(
        rf"(\d+)-year .*?treasury yield (dip below|be below|be above|hit) \$?([\d.]+)% in {MONTH_RE}(?:\s+(\d{{4}}))?",
        pm["q"], re.I)
    if not m:
        return []
    tenor, comp, level, mon, year = int(m.group(1)), m.group(2).lower(), float(m.group(3)), month_key(m.group(4)), (
        int(m.group(5)) if m.group(5) else 2026)
    prefix = f"KX{tenor}YRDIR"
    out = []
    for e in catalog:
        if not e["ticker"].startswith(prefix):
            continue
        em = re.search(r"-T([\d.]+)$", e["ticker"])
        if not em:
            continue
        strike = float(em.group(1))
        is_high = "DIRHM" in e["ticker"]
        if not same_window(e, mon, year):
            continue
        if comp in ("dip below", "be below"):
            if not is_high and abs(strike - level) < 1e-9:
                out.append({"entry": e, "method": "treasury_bound", "confidence": 0.92,
                            "note": f"Same boundary: PM '{comp} {level}%' in {m.group(4)} {year} is Kalshi 'below {strike}%' closing in the same month.",
                            "pool": 10})
        elif comp == "be above":
            if is_high and abs(strike - level) < 1e-9:
                out.append({"entry": e, "method": "treasury_bound", "confidence": 0.92,
                            "note": f"Same boundary: PM 'above {level}%' in {m.group(4)} {year} is Kalshi 'above {strike}%' closing in the same month.",
                            "pool": 10})
        else:  # "hit X%" -- Kalshi strikes are quantised one basis point down
            if is_high and abs((level - 0.01) - strike) < 1e-6:
                out.append({"entry": e, "method": "treasury_bound_quantised", "confidence": 0.8,
                            "note": f"PM 'hit {level}%' equals Kalshi 'above {strike}%' because Kalshi strikes are the basis point below the touch level.",
                            "pool": 8})
    return out


def match_fed(pm: dict, catalog: list[dict]) -> list[dict]:
    if "fed" not in pm["low"] or "meeting" not in pm["low"]:
        return []
    m = re.search(rf"{MONTH_RE}\s+(\d{{4}}) meeting", pm["q"], re.I)
    if not m:
        return []
    mon, year = month_key(m.group(1)), int(m.group(2))
    if "no change" in pm["low"] or "maintain" in pm["low"]:
        move, bps = "hold", 0
    elif "increase" in pm["low"] or "hike" in pm["low"]:
        move = "hike"
        b = re.search(r"by (\d+)", pm["low"])
        bps = int(b.group(1)) if b else 25
    elif "decrease" in pm["low"] or "cut" in pm["low"]:
        move = "cut"
        b = re.search(r"by (\d+)", pm["low"])
        bps = int(b.group(1)) if b else 25
    else:
        return []
    code = f"{year % 100:02d}{['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'][mon - 1]}"
    want = {"hold": "H0", "hike": f"H{bps}", "cut": f"C{bps}"}[move]
    out = []
    for e in catalog:
        if e["ticker"] != f"KXFEDDECISION-{code}-{want}":
            continue
        out.append({"entry": e, "method": "fed_decision", "confidence": 0.88,
                    "note": f"Same meeting and same move: PM '{move} {bps} bps' after {m.group(1)} {year} is Kalshi 'Hike rates by 0bps' (Kalshi frames the hold as a zero hike).",
                    "pool": 9})
    return out


def match_house_seats(pm: dict, catalog: list[dict]) -> list[dict]:
    m = re.search(r"will (democrats|republicans) win exactly (\d+) house seats in (.+?) in the (\d{4}) midterm", pm["q"], re.I)
    if not m:
        return []
    party, n, state, year = m.group(1).lower(), int(m.group(2)), m.group(3).strip().lower(), int(m.group(4))
    abbr = STATE_ABBR.get(state)
    if not abbr:
        return []
    label = "Democrats" if party == "democrats" else "Republicans"
    want = f"KXHOUSEWINSTATE-{abbr}{'D' if party == 'democrats' else 'R'}-E{n}"
    out = []
    for e in catalog:
        if e["ticker"] != want:
            continue
        out.append({"entry": e, "method": "house_seats_exact", "confidence": 0.95,
                    "note": f"Identical claim: exactly {n} {label} House seats in {m.group(3)} in the {year} midterms.",
                    "pool": 12})
    return out


def match_house_race(pm: dict, catalog: list[dict]) -> list[dict]:
    m = re.search(r"will the (democratic|republican) party win the ([a-z]{2})-(\d{2}) house seat", pm["q"], re.I)
    if not m:
        return []
    party = "D" if m.group(1).lower() == "democratic" else "R"
    state, district = m.group(2).upper(), m.group(3)
    cands = {
        f"KXHOUSERACE-{state}{district}-26-{party}",
        f"HOUSE{state}{int(district)}-26-{party}",
    }
    out = []
    for e in catalog:
        if e["ticker"] not in cands:
            continue
        out.append({"entry": e, "method": "house_race_winner", "confidence": 0.93,
                    "note": f"Same race and same side: PM '{m.group(1)} Party' in {state}-{district} is Kalshi '{e['title']}'.",
                    "pool": 10})
    return out


def match_removal_by(pm: dict, catalog: list[dict]) -> list[dict]:
    m = re.search(r"will (.+?) out as (.+?) by " + MONTH_RE + r"\s+(\d{1,2})(?:,?\s+(\d{4}))?", pm["q"], re.I)
    if not m:
        return []
    person = m.group(1).strip().lower()
    mon, day = month_key(m.group(3)), int(m.group(4))
    year = int(m.group(5)) if m.group(5) else 2026
    target = _date(year, mon, day)
    out = []
    for e in catalog:
        title = (e["title"] or "").lower()
        if "out as" not in title or person not in title:
            continue
        cd = close_date(e)
        if cd is None:
            continue
        # "out before <date+1>" is the same deadline as "out by <date>"
        if (cd - target).days == 1:
            out.append({"entry": e, "method": "removal_by_date", "confidence": 0.85,
                        "note": f"Same deadline: PM 'out by {m.group(3)} {day}, {year}' is Kalshi 'before {cd.isoformat()}'.",
                        "pool": 8})
    return out


def match_crypto_annual(pm: dict, catalog: list[dict]) -> list[dict]:
    m = re.search(
        r"will (bitcoin|ethereum|solana) (?:be above|reach|hit|dip to|be below) \$?([\d,.]+)k? by december 31, (\d{4})",
        pm["q"], re.I)
    if not m:
        return []
    asset, raw, year = m.group(1).lower(), m.group(2), int(m.group(3))
    level = float(raw.replace(",", "").rstrip("k"))
    upper = any(w in pm["low"] for w in ("reach", "hit", "be above"))
    prefix = {"bitcoin": "KXBTCMAXY" if upper else "KXBTCMINY",
              "ethereum": "KXETHMAXY" if upper else "KXETHMINY",
              "solana": "KXSOLMAXY" if upper else "KXSOLMINY"}[asset]
    out = []
    for e in catalog:
        if not e["ticker"].startswith(prefix):
            continue
        cd = close_date(e)
        if cd is None or cd.year != year + 1 or cd.month != 1:
            continue
        em = re.search(r"-([\d.]+)$", e["ticker"])
        if not em:
            continue
        strike = float(em.group(1))
        # Kalshi max strikes sit one cent below the round Polymarket level
        if abs(strike - level) < 1e-9 or (upper and abs(strike - (level - 0.01)) < 1e-6) or \
                (not upper and abs(strike - level) < 1e-9):
            out.append({"entry": e, "method": "crypto_annual_bound", "confidence": 0.85,
                        "note": f"Same level and window: PM ${level:,.0f} by {year}-12-31 is Kalshi '{e['title']}'.",
                        "pool": 8})
    return out


def match_margin_threshold(pm: dict, catalog: list[dict]) -> list[dict]:
    m = re.search(r"win the (\d{4}) ([a-z]{2})-(\d{2}) house election by (\d+)% or more", pm["q"], re.I)
    if not m:
        return []
    side = "D" if "democratic party candidate" in pm["low"] else "R"
    state, dist, pct = m.group(2).upper(), m.group(3), int(m.group(4))
    want = f"KXMIDTERMMOV-{state}{dist}{side}-P{pct}"
    out = []
    for e in catalog:
        if e["ticker"] == want:
            out.append({"entry": e, "method": "margin_threshold", "confidence": 0.85,
                        "note": f"Same threshold: PM 'by {pct}% or more' is Kalshi 'at least {pct} percentage points'.",
                        "pool": 7})
    return out


def match_title_exact(pm: dict, catalog: list[dict]) -> list[dict]:
    def norm(text: str) -> str:
        t = text.lower().rstrip("?").replace("will ", "", 1)
        t = re.sub(r"\bthe\b\s+", "", t)
        t = re.sub(r"[\$,%]", " ", t)
        t = re.sub(r"[^a-z0-9. ]+", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        t = t.replace("midterm elections", "house of representatives elections")
        return t

    target = norm(pm["q"])
    out = []
    for e in catalog:
        title = norm(e["title"] or "")
        if not title:
            continue
        if target == title:
            out.append({"entry": e, "method": "title_exact", "confidence": 0.9,
                        "note": "Normalised titles are identical after number/word normalisation.",
                        "pool": 9})
    return out


def _number_set(text: str) -> set[float]:
    out = set()
    for raw in re.findall(r"\$?\d[\d,]*(?:\.\d+)?\s*[kmb]?", text.lower()):
        s = raw.replace("$", "").replace(",", "").strip()
        mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get(s[-1], 1)
        try:
            out.add(round(float(s.rstrip("kmb")) * mult, 6))
        except ValueError:
            continue
    return out


def match_title_fuzzy(pm: dict, catalog: list[dict]) -> list[dict]:
    """Same sentence after stop-word removal, same numbers, no missing date.

    Deliberately strict: a pair only qualifies when the content-word Jaccard is
    at least 0.85 *and* neither side carries a number the other lacks, so a
    threshold or a count can never drift between venues.
    """
    pm_nums = _number_set(pm["q"])
    pm_tokens = pm["tokens"] - {"announced", "next"}
    out = []
    for e in catalog:
        title = (e["title"] or "").lower()
        if not title:
            continue
        if _number_set(title) != pm_nums:
            continue
        k_tokens = {w for w in re.findall(r"[a-z0-9$%]+", title)
                    if w not in {"will", "the", "be", "a", "an", "of", "in", "on", "by", "to",
                                 "and", "or", "for", "at", "is", "from", "with", "as"}} - {"announced", "next"}
        if not pm_tokens or not k_tokens:
            continue
        jac = len(pm_tokens & k_tokens) / len(pm_tokens | k_tokens)
        if jac < 0.85:
            continue
        out.append({"entry": e, "method": "title_fuzzy", "confidence": 0.72,
                    "note": f"Same claim after stop-word removal (token Jaccard {jac:.2f}) and identical numbers; only the resolution wording differs.",
                    "pool": 6})
    return out


MATCHERS = [
    match_house_seats,
    match_house_race,
    match_treasury,
    match_fed,
    match_removal_by,
    match_crypto_annual,
    match_margin_threshold,
    match_title_exact,
    match_title_fuzzy,
]


def match_one(pm: dict, catalog: list[dict]) -> dict | None:
    hits: list[dict] = []
    for fn in MATCHERS:
        hits.extend(fn(pm, catalog))
    if not hits:
        return None
    # prefer the strongest rule, then the tightest window (earliest close)
    hits.sort(key=lambda h: (-h["confidence"], -h["pool"], close_date(h["entry"]) or _date(2099, 1, 1)))
    return hits[0]


# --------------------------------------------------------------------------- #
# 4. measurement
# --------------------------------------------------------------------------- #
def book_events_per_condition(day: str, hours: list[str], conditions: set[str]) -> Counter:
    """Re-count only the conditions we care about (same window, same rule)."""
    counts: Counter = Counter()
    for part in hour_partitions(day, hours):
        for f in sorted(part.glob("*.parquet")):
            t = pq.read_table(f, columns=["condition_id"]).to_pandas().dropna()
            sub = t[t["condition_id"].isin(conditions)]
            counts.update(sub["condition_id"].value_counts().to_dict())
    return counts


def staleness(
    day: str, hours: list[str], pairs: list[tuple[str, str]]
) -> dict[str, dict]:
    """Gap between each Kalshi snapshot and the last PM book update.

    ``pairs`` is (ticker, condition_id).  The clock is the Kalshi row's
    ``recv_ts_ns`` against the PM row's ``venue_ts_ms`` -- the same join the
    cross-venue ``prepare.sql`` uses.  Pairing is done **inside one hour
    partition**: a Kalshi row with no Polymarket event in its own hour is
    dropped instead of being paired with an update from a previous hour, which
    is what a collector outage would otherwise turn into an hour of fake
    staleness.  Gaps are censored from below at 0.
    """
    tickers = sorted({t for t, _ in pairs})
    conds = {c for _, c in pairs}
    ticker_of = dict(pairs)

    out: dict[str, dict] = {}
    all_gaps: list[int] = []
    for h in hours:
        ks: dict[str, list[int]] = {}
        d = TAPE / "kalshi_books" / f"date={day}" / f"hour={h}"
        for f in sorted(d.glob("*.parquet")):
            t = pq.read_table(f, columns=["ticker", "recv_ts_ns"]).to_pandas()
            sub = t[t["ticker"].isin(tickers)]
            for ticker, ts in sub.groupby("ticker")["recv_ts_ns"]:
                ks.setdefault(str(ticker), []).extend(int(x) for x in ts)
        pms: dict[str, list[int]] = {}
        for f in sorted((TAPE / "clob_events" / f"date={day}" / f"hour={h}").glob("*.parquet")):
            t = pq.read_table(f, columns=["condition_id", "venue_ts_ms"]).to_pandas().dropna()
            sub = t[t["condition_id"].isin(conds)]
            for cond, ts in sub.groupby("condition_id")["venue_ts_ms"]:
                pms.setdefault(str(cond), []).extend(int(x) for x in ts)
        for ticker, k_list in ks.items():
            cond = ticker_of.get(ticker)
            p_list = pms.get(cond, []) if cond else []
            if not p_list:
                continue
            k_arr = np.sort(np.asarray(k_list, dtype="int64")) // 1_000_000
            p_arr = np.sort(np.asarray(p_list, dtype="int64"))
            idx = np.searchsorted(p_arr, k_arr, side="right") - 1
            ok = idx >= 0
            if not ok.any():
                continue
            gaps = (k_arr[ok] - p_arr[idx[ok]]).astype("float64")
            gaps = np.maximum(gaps, 0.0)
            slot = out.setdefault(ticker, {"gaps": [], "n": 0, "median": None,
                                           "p90": None, "aligned_60s": None})
            slot["gaps"].extend(float(g) for g in gaps)
    for ticker, slot in out.items():
        valid = sorted(slot.pop("gaps"))
        all_gaps.extend(valid)
        p90 = valid[min(len(valid) - 1, int(round(0.9 * (len(valid) - 1))))]
        slot.update({
            "n": len(valid),
            "median": statistics.median(valid) / 1000.0,
            "p90": p90 / 1000.0,
            "aligned_60s": sum(1 for g in valid if g <= 60_000) / len(valid),
        })
    for ticker, cond in pairs:
        out.setdefault(ticker, {"n": 0, "median": None, "p90": None, "aligned_60s": None})
    if all_gaps:
        all_gaps.sort()
        out["__pooled__"] = {
            "n": len(all_gaps),
            "median": statistics.median(all_gaps) / 1000.0,
            "p90": all_gaps[min(len(all_gaps) - 1, int(round(0.9 * (len(all_gaps) - 1))))] / 1000.0,
            "aligned_60s": sum(1 for g in all_gaps if g <= 60_000) / len(all_gaps),
        }
    return out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-11-06")
    ap.add_argument("--hours", default="00,01,02,03,04,05",
                    help="six complete hour partitions = the last six hours of the tape")
    ap.add_argument("--top", type=int, default=200)
    ap.add_argument("--min-events", type=int, default=200)
    args = ap.parse_args()
    hours = [h.strip() for h in args.hours.split(",") if h.strip()]

    print(f"[1/5] ranking conditions in {len(hours)} hour partitions of {args.date}")
    events, snapshots, per_hour = rank_conditions(args.date, hours)
    ranked = events.most_common(args.top)
    top_ids = [c for c, _ in ranked]
    print(f"  top {len(top_ids)}: >= {ranked[-1][1]:,} events; "
          f"{sum(1 for _, n in ranked if n >= args.min_events)} at or above {args.min_events}")

    # A collector outage that stops best_bid_ask for a whole hour would turn
    # every gap crossing it into a fake hour of staleness, so those hours are
    # excluded from the staleness sample and named in the doc.
    bba = [v["best_bid_ask"] for v in per_hour.values()]
    typical = statistics.median(bba) if bba else 0
    stale_hours = [h for h in hours
                   if per_hour[f"hour={h}"]["best_bid_ask"] >= max(100, 0.05 * typical)]
    outage_hours = [h for h in hours if h not in stale_hours]
    if outage_hours:
        print(f"  best_bid_ask outage in hours {outage_hours}: excluded from the staleness sample")

    print("[2/5] metadata from the universe snapshots")
    universe = load_universe()
    missing = [c for c in top_ids if c not in universe]
    print(f"  metadata found for {len(top_ids) - len(missing)}/{len(top_ids)}"
          + (f"; missing {len(missing)}" if missing else ""))

    print("[3/5] matching against the Kalshi catalog")
    catalog = load_kalshi_catalog(args.date)
    print(f"  catalog window scan done")
    rows = []
    unmatched = []
    for cond, n in ranked:
        meta = universe.get(cond)
        if meta is None:
            unmatched.append((cond, n, "no metadata"))
            continue
        pm = parse_pm(meta["question"])
        hit = match_one(pm, catalog)
        if hit is None:
            unmatched.append((cond, n, "no kalshi counterpart"))
            continue
        e = hit["entry"]
        cd = close_date(e)
        tokens = meta.get("clob_tokens") or []
        rows.append({
            "kalshi_ticker": e["ticker"],
            "kalshi_event": e.get("event_ticker") or "-".join(e["ticker"].split("-")[:2]),
            "polymarket_condition_id": cond,
            "polymarket_yes_token": tokens[0] if len(tokens) > 0 else None,
            "polymarket_no_token": tokens[1] if len(tokens) > 1 else None,
            "direction": "normal",
            "confidence": hit["confidence"],
            "method": METHOD_FAMILY.get(hit["method"], hit["method"]),
            "match_reason": f"method={hit['method']}; window={cd.isoformat() if cd else 'n/a'}; conf={hit['confidence']:.2f}",
            "kalshi_title": e["title"],
            "kalshi_yes_sub_title": e.get("yes_sub_title"),
            "series_ticker": e.get("series_ticker"),
            "window": cd.isoformat() if cd else None,
            "polymarket_question": meta["question"],
            "kalshi_event_ticker": e.get("event_ticker"),
            "pm_events_6h": int(n),
            "match_method": hit["method"],
            "match_note": hit["note"],
            "pm_book_snapshots_6h": int(snapshots.get(cond, 0)),
        })
    frame = pd.DataFrame(rows)
    print(f"  matched {len(frame)}/{len(top_ids)} conditions; {len(unmatched)} without a defensible counterpart")

    print("[4/5] measuring book events and PM-mid staleness")
    match_ids = set(frame["polymarket_condition_id"]) if len(frame) else set()
    recount = book_events_per_condition(args.date, hours, match_ids) if match_ids else Counter()
    if match_ids and recount != Counter({r["polymarket_condition_id"]: r["pm_events_6h"] for r in rows}):
        raise SystemExit("event recount disagrees with the ranking pass")
    pairs = [(r["kalshi_ticker"], r["polymarket_condition_id"]) for r in rows]
    stale = staleness(args.date, stale_hours, pairs) if pairs else {}
    frame["kalshi_has_books"] = [bool(stale.get(t, {}).get("n")) for t in frame["kalshi_ticker"]]
    frame["pm_staleness_median_s"] = [stale.get(t, {}).get("median") for t in frame["kalshi_ticker"]]
    frame["pm_staleness_p90_s"] = [stale.get(t, {}).get("p90") for t in frame["kalshi_ticker"]]

    print("[5/5] writing artifacts")
    frame = frame.reindex(columns=V2_COLUMNS + NEW_COLUMNS + MEASURE_COLUMNS)
    frame = frame.sort_values("pm_events_6h", ascending=False)
    frame.reindex(columns=V2_COLUMNS + NEW_COLUMNS).to_parquet(OUT_PARQUET, index=False)
    print(f"  wrote {OUT_PARQUET} ({len(frame)} rows, "
          f"{len(V2_COLUMNS + NEW_COLUMNS)} columns)")

    qualified = frame[frame["pm_events_6h"] >= args.min_events]
    print("\n=== deliverable ===")
    print(f"ranked            {len(top_ids)}")
    print(f"matched           {len(frame)}")
    print(f"qualified (>= {args.min_events} events) {len(qualified)}")
    print(f"with kalshi books {int(frame['kalshi_has_books'].sum())}")
    ev = frame["pm_events_6h"]
    print(f"events per matched condition: min {ev.min():.0f}, p25 {ev.quantile(.25):.0f}, "
          f"median {ev.median():.0f}, p75 {ev.quantile(.75):.0f}, p90 {ev.quantile(.9):.0f}, max {ev.max():.0f}")
    buckets = [(args.min_events, 500), (500, 1000), (1000, 5000), (5000, 10**9)]
    for lo, hi in buckets:
        n = int(((ev >= lo) & (ev < hi)).sum())
        print(f"  events {lo:,}-{hi:,}: {n}")
    with_books = frame[frame["kalshi_has_books"]]
    if len(with_books):
        all_med = with_books["pm_staleness_median_s"].dropna()
        all_p90 = with_books["pm_staleness_p90_s"].dropna()
        pooled = stale.get("__pooled__", {})
        print(f"PM-mid staleness (pooled over {pooled.get('n', 0):,} aligned snapshots): "
              f"median {pooled.get('median', float('nan')):.0f}s, p90 {pooled.get('p90', float('nan')):.0f}s, "
              f"within 60s {100 * pooled.get('aligned_60s', 0):.1f}%")
        print(f"PM-mid staleness (per-condition medians): min {all_med.min():.0f}s, "
              f"median {all_med.median():.0f}s, p90 {all_med.quantile(0.9):.0f}s, max {all_med.max():.0f}s")
        print(f"PM-mid staleness (per-condition p90s):    min {all_p90.min():.0f}s, "
              f"median {all_p90.median():.0f}s, p90 {all_p90.quantile(0.9):.0f}s, max {all_p90.max():.0f}s")
        for lo, hi in [(0, 60), (60, 300), (300, 900), (900, 3600), (3600, 10**9)]:
            n = int(((all_med >= lo) & (all_med < hi)).sum())
            print(f"  per-condition median staleness {lo}-{hi}s: {n}")

    print("\n=== markdown (doc table) ===")
    print("| PM question | Kalshi ticker | events 6h | median gap | p90 gap | conf | rule |")
    print("|---|---|---:|---:|---:|---:|---|")
    for r in frame.itertuples(index=False):
        med = "-" if pd.isna(r.pm_staleness_median_s) else f"{r.pm_staleness_median_s:.0f}s"
        p90 = "-" if pd.isna(r.pm_staleness_p90_s) else f"{r.pm_staleness_p90_s:.0f}s"
        q = r.polymarket_question.replace("|", "/")
        print(f"| {q} | `{r.kalshi_ticker}` | {r.pm_events_6h:,} | {med} | {p90} | "
              f"{r.confidence:.2f} | {r.match_method} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
