"""Matcher tests for the Polymarket -> Kalshi mapping (fixtures, no network)."""

from __future__ import annotations

from pathlib import Path

from polymarketbot.data.kalshi_mapping import (
    build_mapping,
    extract_features,
    load_mapping,
    read_mapping,
    score_pair,
    tokenise,
    write_mapping,
)


def _pair(pm: str, kalshi: str, ticker: str | None = None):
    return score_pair(
        extract_features(pm), extract_features(kalshi, ticker=ticker)
    )


# ------------------------------------------------------------------ features
def test_features_from_polymarket_question() -> None:
    feature = extract_features(
        "Will the Democratic Party candidate win the 2026 MI-10 House election by 9%-12%?"
    )
    assert feature.office == "house"
    assert feature.state == "MI"
    assert feature.district == 10
    assert feature.year == 2026
    assert feature.party == "dem"
    assert feature.kind == "margin"
    assert feature.bucket == (9.0, 12.0)


def test_features_from_kalshi_ticker() -> None:
    feature = extract_features(
        "Will the margin of victory for Republicans in the governor election "
        "in Wisconsin be at least 5 percentage points?",
        ticker="KXMIDTERMMOV-WIGOVR-P5",
    )
    assert feature.office == "governor"
    assert feature.state == "WI"
    assert feature.party == "rep"
    assert feature.kind == "margin"
    assert feature.thresholds == (5.0,)


def test_tokenise_folds_plurals_and_suffixes() -> None:
    assert "nomin" in tokenise("nomination")
    assert tokenise("nomination") == tokenise("nominate")
    assert tokenise("presidential") == tokenise("president")
    assert tokenise("seats") == tokenise("seat")


def test_state_abbreviation_needs_contest_context() -> None:
    """``US 10`` is a number, not a district; ``MI 10 House`` is."""
    assert extract_features("Will US 10 year yields rise?").district is None
    assert extract_features("Will the Democrat win MI 10 House seat?").district == 10


# ----------------------------------------------------------------- matching
def test_identical_titles_match_exactly() -> None:
    match = _pair(
        "Will the Democratic Party win the MI-10 House seat?",
        "Will the Democratic Party win the MI-10 House seat?",
    )
    assert match is not None
    assert match.method == "title_exact"
    assert match.confidence >= 0.95


def test_structured_winner_match() -> None:
    match = _pair(
        "Will the Democratic Party win the AZ-04 House seat?",
        "Will the Democratic Party win Arizona 4 House seat?",
    )
    assert match is not None
    assert match.confidence >= 0.5


def test_margin_bucket_matches_threshold_rung() -> None:
    match = _pair(
        "Will the Republican Party candidate win the 2026 MI-10 House election by 9%-12%?",
        "Will the margin of victory for Republicans in the MI 10 House election "
        "be at least 9 percentage points?",
    )
    assert match is not None
    assert match.method == "margin_threshold"
    assert 0.5 <= match.confidence <= 0.7


def test_margin_bucket_rejects_the_wrong_rung() -> None:
    assert (
        _pair(
            "Will the Republican Party candidate win the 2026 MI-10 House election by 9%-12%?",
            "Will the margin of victory for Republicans in the MI 10 House election "
            "be at least 15 percentage points?",
        )
        is None
    )


def test_party_mismatch_does_not_match() -> None:
    assert (
        _pair(
            "Will the Democratic Party win the MI-10 House seat?",
            "Will the Republican Party win the MI-10 House seat?",
        )
        is None
    )


def test_state_and_district_mismatch_do_not_match() -> None:
    assert (
        _pair(
            "Will the Democratic Party win the MI-10 House seat?",
            "Will the Democratic Party win the MI-11 House seat?",
        )
        is None
    )
    assert (
        _pair(
            "Will the Democratic Party win the MI-10 House seat?",
            "Will the Democratic Party win the OH-10 House seat?",
        )
        is None
    )


def test_office_mismatch_does_not_match() -> None:
    assert (
        _pair(
            "Will the Democrat win the Michigan Senate race in 2026?",
            "Will the Democratic Party win the Michigan House seat in 2026?",
        )
        is None
    )


def test_turnout_bucket_matches_threshold_inside_the_range() -> None:
    match = _pair(
        "Will total votes cast in the 2026 midterm election for Senator from Montana "
        "be between 450,000 and 500,000?",
        "Will the total vote count for all participants in Montana Senate General "
        "Election be above 480000?",
    )
    assert match is not None
    assert match.method == "turnout_threshold"

    assert (
        _pair(
            "Will total votes cast in the 2026 midterm election for Senator from Montana "
            "be between 450,000 and 500,000?",
            "Will the total vote count for all participants in Montana Senate General "
            "Election be above 900000?",
        )
        is None
    )


def test_seat_count_ladders_must_share_a_rung() -> None:
    pm = "Will the Republican Party hold below 190 House seats after the 2026 midterm elections?"
    assert (
        _pair(
            pm,
            "Will Republicans hold at least 218 House seats after the 2026 midterm elections?",
        )
        is None
    )
    assert (
        _pair(
            pm,
            "Will Republicans hold below 190 House seats after the 2026 midterm elections?",
        )
        is not None
    )


def test_title_fuzzy_matches_reworded_nomination_market() -> None:
    match = _pair(
        "Will Gavin Newsom win the 2028 Democratic presidential nomination?",
        "Will the Democratic Party nominate Gavin Newsom for president in 2028?",
    )
    assert match is not None
    assert match.method == "title_fuzzy"
    assert 0.7 <= match.confidence <= 0.85


def test_unrelated_markets_do_not_match() -> None:
    assert _pair("Will Bitcoin close above $150,000 in 2026?", "Will Miami win the 2026 NBA Finals?") is None


# ------------------------------------------------------------------- builder
_KALSHI_ROWS = [
    {
        "ticker": "KXMIDTERMWIN-MI10-D",
        "event_ticker": "KXMIDTERMWIN-MI10",
        "title": "Will the Democratic Party win the MI-10 House seat?",
        "close_time": "2027-11-03T15:00:00Z",
    },
    {
        "ticker": "KXMIDTERMWIN-MI10-R",
        "event_ticker": "KXMIDTERMWIN-MI10",
        "title": "Will the Republican Party win the MI-10 House seat?",
        "close_time": "2027-11-03T15:00:00Z",
    },
    {
        "ticker": "KXMIDTERMWIN-OH10-D",
        "event_ticker": "KXMIDTERMWIN-OH10",
        "title": "Will the Democratic Party win the OH-10 House seat?",
        "close_time": "2027-11-03T15:00:00Z",
    },
    {
        "ticker": "KXNFLGAME-KC",
        "event_ticker": "KXNFLGAME",
        "title": "Will Kansas City win the Super Bowl?",
        "close_time": "2027-02-08T00:00:00Z",
    },
]


def test_build_mapping_picks_the_right_ticker_per_market() -> None:
    universe = [
        {
            "condition_id": "0xaaa",
            "question": "Will the Democratic Party win the MI-10 House seat?",
            "end_date": "2026-11-04T05:00:00Z",
        },
        {
            "condition_id": "0xbbb",
            "question": "Will the Democratic Party win the OH-10 House seat?",
            "end_date": "2026-11-04T05:00:00Z",
        },
        {
            "condition_id": "0xccc",
            "question": "Will the price of saffron double in 2026?",
            "end_date": "2026-11-04T05:00:00Z",
        },
    ]
    rows = build_mapping(universe, _KALSHI_ROWS, min_confidence=0.5)

    by_condition = {row["polymarket_condition_id"]: row for row in rows}
    assert set(by_condition) == {"0xaaa", "0xbbb"}  # the saffron market is unmapped
    assert by_condition["0xaaa"]["kalshi_ticker"] == "KXMIDTERMWIN-MI10-D"
    assert by_condition["0xbbb"]["kalshi_ticker"] == "KXMIDTERMWIN-OH10-D"
    assert by_condition["0xaaa"]["confidence"] > 0.8
    assert by_condition["0xaaa"]["method"] == "title_exact"


def test_build_mapping_respects_the_confidence_floor() -> None:
    universe = [
        {
            "condition_id": "0xnewsom",
            "question": "Will Gavin Newsom win the 2028 Democratic presidential nomination?",
        }
    ]
    kalshi = [
        {
            "ticker": "KXPRESNOMD-28-GN",
            "title": "Will the Democratic Party nominate Gavin Newsom for president in 2028?",
        }
    ]
    assert build_mapping(universe, kalshi, min_confidence=0.9) == []
    assert len(build_mapping(universe, kalshi, min_confidence=0.5)) == 1


def test_mapping_artifact_roundtrip(tmp_path: Path) -> None:
    rows = [
        {
            "polymarket_condition_id": "0xaaa",
            "kalshi_ticker": "KXMIDTERMWIN-MI10-D",
            "confidence": 0.93,
            "method": "structured_winner",
            "kalshi_event_ticker": "KXMIDTERMWIN-MI10",
            "polymarket_question": "Will the Democratic Party win the MI-10 House seat?",
            "kalshi_title": "Will the Democratic Party win the MI-10 House seat?",
        },
        {
            "polymarket_condition_id": "0xbbb",
            "kalshi_ticker": "KXMIDTERMWIN-MI10-D",
            "confidence": 0.6,
            "method": "title_fuzzy",
            "kalshi_event_ticker": "KXMIDTERMWIN-MI10",
            "polymarket_question": "Democrats take MI-10?",
            "kalshi_title": "Will the Democratic Party win the MI-10 House seat?",
        },
    ]
    path = tmp_path / "kalshi_mapping.parquet"
    assert write_mapping(rows, path) == 2
    loaded = read_mapping(path)
    assert {row["polymarket_condition_id"] for row in loaded} == {"0xaaa", "0xbbb"}
    # The collector only needs the ticker list, de-duplicated.
    assert load_mapping(path) == ("KXMIDTERMWIN-MI10-D",)
    assert load_mapping(tmp_path / "missing.parquet") == ()
