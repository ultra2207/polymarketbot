"""Tests for the settlement pillar's oracle-derived fair value.

The pillar's gate is ``fair_value - ask - fee - measured_onchain_cost > 0``.  The
fair value must come from the oracle (a final payout vector or an undisputed UMA
proposal on the tape) and never from the market's own traded price: a
price-derived probability cannot beat the book that printed it, which is exactly
why the 2026-11-09 soak queued 3,564 candidates and bought nothing.
"""

from __future__ import annotations

import time

import pytest

from polymarketbot.trading.domain2.settlement_fair_value import (
    BIND_AGED_OUT,
    BIND_AMBIGUOUS,
    BIND_BOUND,
    BIND_BOUND_NORMALISED,
    BIND_NO_OPEN_FIXTURE,
    BIND_UNMATCHED_QUESTION,
    FixtureIndex,
    SettlementFairValueModel,
    TapeProposal,
    UmaVerdictTape,
    fixture_matches,
    normalize_question,
    question_sides,
)

LIVENESS_SEC = 7200.0  # measured on the uma_events tape (235 proposals)


class _FixedHazardTracker:
    """Dispute tracker stub: the model only needs the upper-95 hazard."""

    def __init__(self, hazard: float) -> None:
        self.hazard = hazard

    def get_credible_upper_hazard(self, category: str) -> float:  # noqa: ARG002
        return self.hazard


def _tape(proposals, liveness: float = LIVENESS_SEC) -> UmaVerdictTape:
    tape = UmaVerdictTape(overlay_path="")
    tape._read_tape = lambda now_ts: (list(proposals), liveness)  # type: ignore[assignment]
    tape.refresh(force=True)
    return tape


def _model(proposals, hazard: float = 0.01) -> SettlementFairValueModel:
    return SettlementFairValueModel(
        dispute_tracker=_FixedHazardTracker(hazard),
        tape=_tape(proposals),
    )


def _proposal(question: str, yes: bool, cid: str = "", age_sec: float = 60.0) -> TapeProposal:
    return TapeProposal(
        question=question,
        condition_id=cid,
        proposed_price=1.0 if yes else 0.0,
        ts=time.time() - age_sec,
        liveness_sec=LIVENESS_SEC,
    )


def test_normalized_question_join_is_punctuation_insensitive():
    assert normalize_question("Will BTC close Above $100k?") == "will btc close above 100k"
    assert normalize_question("  WILL btc CLOSE above 100K  ") == normalize_question(
        "Will BTC close Above $100k?"
    )


def test_uma_yes_proposal_prices_the_yes_token():
    model = _model([_proposal("Will the Fed cut rates?", yes=True)], hazard=0.0126)
    verdict = model.fair_value(
        condition_id="0xabc",
        question="Will the Fed cut rates?",
        outcomes=["Yes", "No"],
        prices=[0.42, 0.58],  # a live last-trade print: must be ignored
        closed=False,
        uma_status="proposed",
        category="politics",
    )
    assert verdict is not None
    assert verdict.source == "uma_tape"
    assert verdict.winning_index == 0
    assert verdict.p_win == pytest.approx(1.0 - 0.0126)


def test_uma_no_proposal_prices_the_no_token():
    model = _model([_proposal("Will the Fed cut rates?", yes=False)], hazard=0.0126)
    verdict = model.fair_value(
        condition_id="",
        question="Will the Fed cut rates?",
        outcomes=["Yes", "No"],
        prices=[0.30, 0.70],
        uma_status="proposed",
        category="politics",
    )
    assert verdict is not None
    assert verdict.winning_index == 1


def test_condition_id_join_takes_priority_over_question():
    model = _model(
        [
            _proposal("Some other phrasing", yes=False, cid="0xdeadbeef"),
            _proposal("Will it rain?", yes=True),
        ]
    )
    verdict = model.fair_value(
        condition_id="0xDEADBEEF",
        question="Will it rain?",
        outcomes=["Yes", "No"],
        uma_status="proposed",
    )
    assert verdict is not None
    assert verdict.winning_index == 1  # the condition-id proposal (NO) wins


def test_no_oracle_evidence_means_no_fair_value():
    """A live market with no payout and no proposal is NOT priced off its print."""
    model = _model([])
    assert (
        model.fair_value(
            condition_id="0xabc",
            question="Will something happen?",
            outcomes=["Yes", "No"],
            prices=[0.985, 0.015],
            closed=False,
            uma_status="",
        )
        is None
    )


def test_final_payout_vector_is_a_verdict():
    model = _model([])
    verdict = model.fair_value(
        condition_id="0xabc",
        question="Resolved market",
        outcomes=["Yes", "No"],
        prices=["1", "0"],
        closed=True,
        uma_status="resolved",
    )
    assert verdict is not None
    assert verdict.source == "gamma_payout"
    assert verdict.winning_index == 0
    assert verdict.p_win == 1.0


def test_non_binary_prices_are_not_a_payout():
    model = _model([])
    assert (
        model.fair_value(
            condition_id="0xabc",
            question="Closed but still quoting",
            outcomes=["Yes", "No"],
            prices=["0.97", "0.03"],
            closed=True,
            uma_status="resolved",
        )
        is None
    )


def test_proposed_market_with_a_1_0_print_is_not_a_payout():
    """``outcomePrices == 1`` on a merely proposed market is a print, not a payout."""
    model = _model([])
    assert (
        model.fair_value(
            condition_id="0xabc",
            question="Proposed but not resolved",
            outcomes=["Yes", "No"],
            prices=["1", "0"],
            closed=False,
            uma_status="proposed",
        )
        is None
    )


def test_third_outcome_market_cannot_be_mapped_from_a_yes_no_proposal():
    model = _model([_proposal("Who wins the election?", yes=True)])
    assert (
        model.fair_value(
            condition_id="0xabc",
            question="Who wins the election?",
            outcomes=["Alice", "Bob", "Carol"],
            uma_status="proposed",
        )
        is None
    )


def test_tape_reports_the_measured_liveness_window():
    tape = _tape([_proposal("q", yes=True)], liveness=LIVENESS_SEC)
    assert tape.measured_liveness_sec == pytest.approx(LIVENESS_SEC)
    assert len(tape) == 1


def test_a_proposal_past_its_payout_tail_is_not_a_verdict():
    """The 7-day tape lookback (and the persisted overlay) must not price stale rows.

    A proposal is evidence while its liveness window plus the relayer/redeem legs
    can still be pending. 30 h past the request with a 2 h window is 28 h past the
    oracle's own deadline: the payout has long since been redeemed and the
    proposal says nothing about today's ask.
    """
    model = _model([_proposal("Will the Fed cut rates?", yes=True, age_sec=30 * 3600.0)])
    assert (
        model.fair_value(
            condition_id="",
            question="Will the Fed cut rates?",
            outcomes=["Yes", "No"],
            prices=[0.42, 0.58],
            uma_status="proposed",
        )
        is None
    )


def test_ingest_keeps_the_latest_proposal_and_drops_disputed_ones():
    """The live poller merges into the same index; a dispute strips the verdict."""
    tape = _tape([])
    old = _proposal("Will the Fed cut rates?", yes=False, cid="0xfeed", age_sec=600.0)
    new = _proposal("Will the Fed cut rates?", yes=True, cid="0xfeed", age_sec=5.0)
    assert tape.ingest([old]) == 1
    assert tape.proposal_for("0xfeed", "").proposed_price == 0.0
    assert tape.ingest([new]) == 1
    assert tape.proposal_for("", "Will the Fed cut rates?").proposed_price == 1.0
    # An older re-ingest must not roll the verdict back.
    assert tape.ingest([old]) == 0
    assert tape.proposal_for("0xfeed", "").proposed_price == 1.0
    # A dispute removes the verdict, so nothing can be priced from it.
    tape.ingest([], disputed_questions=["Will the Fed cut rates?"])
    assert tape.proposal_for("0xfeed", "Will the Fed cut rates?") is None
    model = SettlementFairValueModel(dispute_tracker=_FixedHazardTracker(0.01), tape=tape)
    assert (
        model.fair_value(
            condition_id="0xfeed",
            question="Will the Fed cut rates?",
            outcomes=["Yes", "No"],
            uma_status="proposed",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Verdict -> condition-id binding audit
#
# The 2026-11-10 10:10 soak logged "bound 2 proposal(s)" and then "bound 1" while
# the poller reported 17 live proposals: 15 verdicts reached no market and
# nothing said why, so a zero-fill window could not be told apart from a blind
# radar.  These tests pin the accounting that answers it.
# ---------------------------------------------------------------------------
def test_binding_audit_names_the_reason_every_proposal_cannot_be_priced():
    """4 proposals, 4 dispositions: bound, bound-now, unmatched, aged out."""
    now = time.time()
    already = _proposal("Will the bound market resolve yes?", True, cid="0xbound")
    open_market = _proposal("Will the open market resolve yes?", True)
    closed_market = _proposal("Will the closed market resolve yes?", True)
    aged = _proposal("Will the aged market resolve yes?", True, age_sec=40_000.0)
    tape = _tape([already, open_market, closed_market, aged])
    universe = {normalize_question(open_market.question): "0xopen"}

    audit = tape.binding_audit(universe, now=now)

    assert audit.proposals == 4
    # 7,200 s liveness + 21,600 s verdict tail = 28,800 s; 40,000 s is past it.
    assert audit.live == 3
    assert audit.bound == 2
    assert audit.unbound == 2
    assert audit.reasons == {
        BIND_BOUND: 2,
        BIND_AGED_OUT: 1,
        BIND_UNMATCHED_QUESTION: 1,
    }
    reasons = {row.question: row.reason for row in audit.rows}
    assert reasons[closed_market.question] == BIND_UNMATCHED_QUESTION
    assert reasons[aged.question] == BIND_AGED_OUT
    # The tally is what a log line prints; the counts are in reading order.
    assert audit.summary().startswith("4 proposal(s) in, 2 bound (2 unbound), 3 live")
    # Only live rows are worth a per-proposal warning.
    assert [row.question for row in audit.unbound_rows() if row.live] == [
        closed_market.question
    ]

    # The audit and the bind agree: exactly the "would bind" row gets an id.
    assert tape.bind_condition_ids(universe) == 1
    assert tape.proposal_for("0xopen", open_market.question) is not None
    after = tape.binding_audit(universe, now=now)
    assert after.bound == 2
    assert after.reasons == {BIND_BOUND: 2, BIND_AGED_OUT: 1, BIND_UNMATCHED_QUESTION: 1}


def test_a_cid_less_reproposal_keeps_the_bound_condition_id():
    """Re-polling an already-bound proposal must not un-bind it.

    The live poller emits ``ProposePrice`` rows with no condition id (it has no
    universe manifest), and it re-reads the same log rows on every restart. The
    binding is a property of the question, not of the log row, so a cid-less
    re-proposal has to inherit it - otherwise the radar loses the market it just
    found and the window collapses 17 -> 2 -> 1 bound markets.
    """
    question = "Will the verdict market resolve yes?"
    tape = _tape([_proposal(question, True)])
    universe = {normalize_question(question): "0xabc"}
    assert tape.bind_condition_ids(universe) == 1
    assert tape.live_verdict_condition_ids() == ["0xabc"]

    # The poller re-reads the same event: same question, no condition id.
    assert tape.ingest([_proposal(question, True)]) == 1

    assert tape.proposal_for("0xabc", question).condition_id == "0xabc"
    assert tape.live_verdict_condition_ids() == ["0xabc"]
    # What is persisted must carry the id too, or a restart loses the market.
    assert tape._overlay[normalize_question(question)].condition_id == "0xabc"


def test_a_fresher_proposal_for_another_question_stays_unbound():
    """The carry-forward is per question: a new market still needs its own bind."""
    tape = _tape([_proposal("Will the first market resolve yes?", True)])
    universe = {normalize_question("Will the first market resolve yes?"): "0x1"}
    assert tape.bind_condition_ids(universe) == 1
    assert tape.ingest([_proposal("Will the second market resolve yes?", True)]) == 1
    second = tape.proposal_for("", "Will the second market resolve yes?")
    assert second.condition_id == ""
    audit = tape.binding_audit(universe)
    assert audit.bound == 1
    assert audit.reasons[BIND_UNMATCHED_QUESTION] == 1


# ---------------------------------------------------------------------------
# The fixture join
#
# Measured 2026-11-10 11:48-12:19: four live sports proposals could not bind
# because the ancillary title spells the teams out and lists them in the other
# order while Gamma abbreviates:
#
#   ancillary "San Jose Sharks vs New York Islanders" <- Gamma "Islanders vs. Sharks"
#   ancillary "Edmonton Oilers vs New York Rangers"   <- Gamma "Rangers vs. Oilers"
#
# The rules below are membership tests over normalised token sets, not scores,
# so each vector is either proven or refused.  Pinning them is what stops a
# later "small improvement" from turning a refusal into a wrong-market bind.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("ancillary", "gamma"),
    [
        # word order: the two venues disagree about which team is listed first
        ("San Jose Sharks vs New York Islanders", "Islanders vs. Sharks"),
        ("Edmonton Oilers vs New York Rangers", "Rangers vs. Oilers"),
        ("Vancouver Canucks vs Philadelphia Flyers", "Flyers vs. Canucks"),
        # punctuation: "vs." in Gamma, "vs" in the ancillary title
        ("Minnesota Vikings vs Buffalo Bills", "Bills vs. Vikings"),
        # a multi-token nickname survives containment
        ("Boston Red Sox vs New York Yankees", "Red Sox vs. Yankees"),
        # abbreviation: the Gamma side is the ancillary side's initials
        ("San Jose Sharks vs New York Rangers", "NYR vs. SJS"),
        # date suffix (Gamma stamps the day into soccer questions)
        ("Junior vs Cucuta", "Junior vs. Cucuta 2026-11-10"),
    ],
)
def test_the_fixture_join_pins_every_measured_spelling(ancillary, gamma):
    """One normalised rule covers order, punctuation, nickname and initials."""
    assert fixture_matches(question_sides(ancillary), question_sides(gamma))


@pytest.mark.parametrize(
    ("ancillary", "gamma"),
    [
        # A different fixture is never a match, however many tokens overlap.
        ("San Jose Sharks vs New York Islanders", "Sharks vs. Oilers"),
        # An over/under market on the same fixture is a different market: the
        # join must not bind a verdict to it (its side carries the line).
        ("San Jose Sharks vs New York Islanders", "Islanders vs. Sharks: O/U 4.5"),
        # A yes/no proposition has no second side, so it is not fixture-shaped.
        ("Will the Republican Party win the AZ-02 House seat?", "AZ-02 House seat"),
        # A single shared token is not a fixture match.
        ("Edmonton Oilers vs New York Rangers", "New York Rangers"),
    ],
)
def test_the_fixture_join_refuses_every_near_miss(ancillary, gamma):
    """Precision first: a near miss must stay unbound, not become a bind."""
    assert not fixture_matches(question_sides(ancillary), question_sides(gamma))


def test_the_fixture_join_binds_a_spelled_out_sports_title():
    """The measured 2026-11-10 blindness: 4 live proposals, 0 could bind."""
    now = time.time()
    proposal = _proposal("San Jose Sharks vs New York Islanders", True)
    tape = _tape([proposal])
    universe = {normalize_question("Islanders vs. Sharks"): "0xsharks"}

    audit = tape.binding_audit(universe, now=now)
    assert audit.reasons == {BIND_BOUND_NORMALISED: 1}
    assert audit.rows[0].match == "normalised"
    assert audit.rows[0].condition_id == "0xsharks"
    assert audit.unbound_rows() == ()

    # The bind path agrees with the audit, and the id survives into the overlay.
    assert tape.bind_condition_ids(universe) == 1
    assert tape.proposal_for("0xsharks", "").condition_id == "0xsharks"
    after = tape.binding_audit(universe, now=now)
    assert after.reasons == {BIND_BOUND: 1}
    assert after.bound == 1


def test_an_ambiguous_fixture_is_refused_with_its_own_reason():
    """Two open fixtures matching equally is a refusal, not a coin flip."""
    now = time.time()
    proposal = _proposal("New York Rangers vs Edmonton Oilers", True)
    tape = _tape([proposal])
    # Two open listings of the same fixture, abbreviated and in both orders.
    # Neither key is exact for the ancillary title, both match equally, so the
    # join must refuse rather than pick one.
    universe = {
        normalize_question("Rangers vs. Oilers"): "0xnhl",
        normalize_question("Oilers vs. Rangers"): "0xalt",
    }
    index = FixtureIndex(universe)
    assert len(index.candidates(proposal.question)) == 2

    audit = tape.binding_audit(universe, now=now, index=index)
    assert audit.reasons == {BIND_AMBIGUOUS: 1}
    row = audit.rows[0]
    assert row.match == "ambiguous"
    assert row.condition_id == ""
    # Both candidates are named, so the next window can see the collision.
    assert "0xnhl" in row.candidate and "0xalt" in row.candidate
    assert [r.question for r in audit.unbound_rows()] == [proposal.question]

    # Refused: nothing is bound, so no market can be priced off the wrong side.
    assert tape.bind_condition_ids(universe) == 0
    assert tape.proposal_for("", proposal.question).condition_id == ""
    assert tape.live_verdict_condition_ids(now=now) == []


def test_a_live_fixture_with_no_open_market_is_named_as_such():
    """The measured post-game case: the join is right, the market is closed.

    All four sports proposals of the 2026-11-10 11:48 window landed 165-203
    minutes after their market's ``endDate``, and a market stops accepting
    orders at that date (0 of 400 open markets sampled have ``endDate`` in the
    past).  The honest disposition is therefore "no open fixture", which is a
    different fact from "the title is spelled differently".
    """
    tape = _tape([_proposal("Edmonton Oilers vs New York Rangers", True)])
    universe = {normalize_question("Will the Fed cut rates?"): "0xfed"}

    audit = tape.binding_audit(universe)

    assert audit.reasons == {BIND_NO_OPEN_FIXTURE: 1}
    assert tape.bind_condition_ids(universe) == 0
