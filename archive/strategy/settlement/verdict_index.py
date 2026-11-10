"""The verdict index and its join to tradable condition ids.

Normalisation, the fixture join, the join-reason vocabulary and the proposal /
audit records live here. Everything is an exact membership or containment test:
a match is proven or refused, never scored, because a coincidence in this join
buys the wrong side at par.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

# A proposal is still evidence after its liveness window closes - the relayer
# and redeem legs are what remain - but not forever. This tail bounds how long
# past the oracle's own deadline an indexed proposal may still be priced; the
# daemon's measured settlement horizon is the stricter gate on funding. Without
# it the 7-day tape lookback (and the persisted live overlay) could price a
# market off a proposal whose outcome has long since been redeemed.
TAPE_VERDICT_TAIL_SEC: float = 6 * 3600.0


_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Latin letters NFKD does *not* decompose into a base + combining mark, so the
# fold below cannot strip them and ``_NON_ALNUM`` would cut the name in two.
# Measured on the live proposals of the 2026-11-10 12:53 window: the Colombian
# fixture "CDP Junior FC vs. Cúcuta Deportivo FC" keyed as
# ``cdp junior fc vs c cuta deportivo fc`` - the accent split "cúcuta" into the
# tokens "c" and "cuta", so neither the containment rule nor the initials rule
# could ever match the ancillary title's "Cucuta".
_TRANSLIT = str.maketrans({
    "ß": "ss", "ø": "o", "æ": "ae", "œ": "oe", "đ": "d", "ð": "d",
    "ł": "l", "þ": "th", "ı": "i",
})


def _fold(text: str) -> str:
    """Strip diacritics so "Cúcuta"/"Munchen" join their ASCII spellings.

    NFD splits an accented Latin letter into base + combining mark, and the
    marks are dropped by the ``[a-z0-9]`` filter that follows.  The non-ASCII
    test keeps the 185k-key universe build off the normaliser: only the few
    keys that carry a non-ASCII character pay for the decomposition.
    """
    if text.isascii():
        return text
    decomposed = unicodedata.normalize("NFD", text.translate(_TRANSLIT))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_question(text: Any) -> str:
    """Case/punctuation/diacritic-insensitive key joining a Gamma market to the tape."""
    if not isinstance(text, str):
        return ""
    return _NON_ALNUM.sub(" ", _fold(text.lower())).strip()


# -- the fixture join -------------------------------------------------------
# A UMA ancillary title spells a fixture's competitors out in full while the
# Gamma market question abbreviates *and* reorders them.  Measured 2026-11-10
# on the four live sports proposals of the 11:48 soak window:
#
#   ancillary "San Jose Sharks vs New York Islanders"  <- Gamma "Islanders vs. Sharks"
#   ancillary "Vancouver Canucks vs Philadelphia Flyers" <- Gamma "Flyers vs. Canucks"
#   ancillary "Edmonton Oilers vs New York Rangers"    <- Gamma "Rangers vs. Oilers"
#
# So the join needs exactly three transforms, all exact:
#
#   1. punctuation/case/date-suffix normalisation (Gamma stamps the day into
#      soccer questions: "Will Junior win 2026-11-10 ?");
#   2. order-insensitivity - the two competitors are a *set*, because the two
#      venues disagree about home/away ordering;
#   3. one side naming a subset of the other's tokens ("Sharks" is a subset of
#      "San Jose Sharks", "Red Sox" of "Boston Red Sox") or its initials ("NYR"
#      is the initials of "New York Rangers").
#
# Every rule is a membership test, so a match is *proven* or refused.  There is
# no edit-distance threshold: an approximate score would price markets at par on


_FIXTURE_SPLIT = re.compile(r"\s+(?:vs|versus|v)\s+")
# A trailing date, in the normalised (all-digits-are-tokens) form.
_DATE_TOKEN = re.compile(r"\b(?:19|20)\d{2}(?: \d{1,2}){1,2}\b")
# Cheap gate before the split: 185,792 walked questions are 99.3 % non-fixture
# (measured 2026-11-10: 1,259 of 3,938 open questions own a fixture shape in the
# window's range), and the bind runs on each universe change.
_FIXTURE_HINT = re.compile(r"(?:^|\s)(?:vs|versus|v)(?:\s|$)")


def _sides_from_key(key: str) -> Tuple[Tuple[str, ...], ...]:
    """``question_sides`` for text ``normalize_question`` has already produced.

    The index is built from 185,792 already-normalised universe keys, so that
    pass must not re-normalise them: the normalisation is the whole cost.
    """
    if not key or not _FIXTURE_HINT.search(key):
        return ()
    parts = [part.strip() for part in _FIXTURE_SPLIT.split(_DATE_TOKEN.sub(" ", key))]
    if len(parts) != 2:
        return ()
    sides = tuple(tuple(part.split()) for part in parts)
    if any(not side for side in sides):
        return ()
    return sides


def question_sides(text: Any) -> Tuple[Tuple[str, ...], ...]:
    """The two competitors of a fixture question, as normalised token tuples.

    ``()`` for anything that is not an "A vs B" question: a yes/no proposition
    ("Will the Republican Party win the AZ-02 House seat?") has no second side,
    so it can never be joined by this rule and is left to the exact key.
    """
    return _sides_from_key(normalize_question(text))


def side_matches(narrow: Sequence[str], wide: Sequence[str]) -> bool:
    """True when ``narrow`` names the same competitor as ``wide``, exactly.

    Two measured spellings and no scoring:

    * token containment - Gamma's "Sharks" is a subset of the ancillary title's
      "San Jose Sharks", "Red Sox" of "Boston Red Sox";
    * initials - a code such as "nyr" equals the initials of "New York Rangers".
    """
    if not narrow or not wide:
        return False
    if set(narrow) <= set(wide):
        return True
    return len(narrow) == 1 and narrow[0] == "".join(token[0] for token in wide)


def fixture_matches(
    first: Sequence[Sequence[str]], second: Sequence[Sequence[str]]
) -> bool:
    """True when two two-sided questions name the same fixture.

    Orientation-insensitive: either venue may list either competitor first.  A
    one-sided question (a yes/no proposition) is never a fixture.
    """
    if len(first) != 2 or len(second) != 2:
        return False

    def side_ok(left: Sequence[str], right: Sequence[str]) -> bool:
        return side_matches(left, right) or side_matches(right, left)

    return (side_ok(first[0], second[0]) and side_ok(first[1], second[1])) or (
        side_ok(first[0], second[1]) and side_ok(first[1], second[0])
    )


class FixtureIndex:
    """The scanned universe's fixture questions, indexed for the normalised join.

    An exact key lookup cannot reach a spelled-out title, and scanning 185,792
    walked markets per proposal is not affordable on the tick path.  Each
    universe fixture is therefore indexed under each side's nickname (last
    token) and, for multi-token sides, its initials - the two forms a Gamma
    question uses - so a proposal's lookup touches a handful of candidates and
    each candidate is then verified by ``fixture_matches``, not by the index.
    """

    def __init__(self, by_question: Dict[str, str]) -> None:
        self._sides: Dict[str, Tuple[Tuple[str, ...], ...]] = {}
        self._by_token: Dict[str, List[str]] = {}
        for key in by_question:
            sides = _sides_from_key(key)
            if len(sides) != 2:
                continue
            self._sides[key] = sides
            for side in sides:
                tokens = set(side)
                if len(side) > 1:
                    tokens.add("".join(token[0] for token in side))
                for token in tokens:
                    self._by_token.setdefault(token, []).append(key)

    def __len__(self) -> int:
        return len(self._sides)

    def candidates(self, question: Any) -> List[str]:
        """Universe keys naming the same fixture as ``question``, sorted.

        Every token of a side is a lookup key, not just its last one: a soccer
        draw market is titled "Will Junior vs. Cucuta end in a draw?", so the
        tail tokens ("end", "in", "a", "draw") sit on the same side as the
        competitor and a last-token lookup would search the universe for
        "draw".  The candidate set is *only* a generator - ``fixture_matches``
        proves each one by full-side containment - so a common token such as
        "new" costs a rejected candidate and can never bind a wrong market.

        More than one entry is an ambiguity for the caller to refuse, not a
        ranking problem: two open fixtures that both match cannot be told apart
        by token evidence.
        """
        sides = _sides_from_key(normalize_question(question))
        if len(sides) != 2:
            return []
        lookup: List[str] = []
        for side in sides:
            lookup.extend(side)
            if len(side) > 1:
                lookup.append("".join(token[0] for token in side))
        seen: List[str] = []
        for token in dict.fromkeys(lookup):
            seen.extend(self._by_token.get(token, ()))
        return sorted(
            key for key in dict.fromkeys(seen) if fixture_matches(sides, self._sides[key])
        )




# Why one proposal in the verdict index could or could not be joined to a
# tradable condition id.  The radar's reach is exactly ``bound / proposals``, so
# a zero-fill window is only diagnosable when every proposal carries its reason.
BIND_BOUND = "bound"
BIND_BOUND_NORMALISED = "bound_normalised"
BIND_WOULD_BIND = "would_bind"
BIND_WOULD_BIND_NORMALISED = "would_bind_normalised"
BIND_UNMATCHED_QUESTION = "unmatched_question"
# Two or more open fixtures matched the ancillary title with the same standing,
# so the join cannot say which market the verdict prices.  Refused by rule: a
# wrong bind prices a market at par and buys the wrong side, which is a strictly
# worse error than leaving the verdict unbound.
BIND_AMBIGUOUS = "ambiguous_match"
# The title names a fixture and no *open* market carries it.  Measured (see
# ``question_sides``), a match market stops accepting orders at its ``endDate``
# and a sports UMA proposal lands after the game, so this is the structural
# disposition of a post-game proposal, not a join failure.
BIND_NO_OPEN_FIXTURE = "no_open_fixture"
BIND_AGED_OUT = "aged_out"
BIND_NO_QUESTION = "no_question"


def proposal_deadline(
    proposal: "TapeProposal", measured_liveness_sec: float = 0.0
) -> float:
    """Epoch seconds when a proposal stops being a live verdict input.

    The proposal's own ``expiry`` is the oracle's number and wins; otherwise the
    liveness window is the proposal's measured span, the tape's measured median,
    or - when neither exists - one verdict tail.  One definition, because three
    copies of this formula is how the audit and the pricing rule drift apart.
    """
    return proposal.expiry_ts or (
        proposal.ts
        + (proposal.liveness_sec or measured_liveness_sec or TAPE_VERDICT_TAIL_SEC)
    )


@dataclass(frozen=True)
class TapeProposal:
    """One ``ProposePrice`` row, reduced to what a fair value needs."""

    question: str
    condition_id: str
    proposed_price: float
    """Proposed price in [0, 1]; 1.0 means the YES side, 0.0 the NO side."""
    ts: float
    liveness_sec: float
    expiry_ts: float = 0.0
    """Epoch seconds when the proposal's liveness window closes (0 = unknown).

    The deadline is the oracle's own number, so a proposal whose liveness has
    *already* elapsed is still a live verdict while the relayer/redeem legs run:
    ``ts + liveness_sec`` cannot express that once the window is in the past.
    """


@dataclass(frozen=True)
class BindingRow:
    """One proposal's disposition in the verdict -> condition-id join."""

    question: str
    condition_id: str
    reason: str
    age_sec: float
    live: bool
    match: str = ""
    """``exact`` (same normalised title), ``normalised`` (fixture join) or
    ``ambiguous`` (two or more fixtures with equal standing)."""
    candidate: str = ""
    """What the row bound to, or - when ambiguous - every candidate, so the
    next window can see the collision instead of re-deriving it."""


@dataclass(frozen=True)
class BindingAudit:
    """How far the verdict index reaches, and where it stops.

    ``proposals`` counts distinct questions in the index (the join key space),
    ``live`` those still inside the verdict tail, ``bound`` those that carry a
    condition id (already or from this call).  ``reasons`` is the per-proposal
    tally of *why* each row ended where it did, so a window with zero fill can
    distinguish "no open market carries a verdict" from "the join is broken".
    """

    proposals: int
    live: int
    bound: int
    unbound: int
    reasons: Dict[str, int]
    rows: Tuple[BindingRow, ...] = ()

    def summary(self) -> str:
        """One-line accounting, ordered so the first number read is the reach."""
        tally = ", ".join(
            f"{reason}={count}" for reason, count in sorted(self.reasons.items())
        )
        return (
            f"{self.proposals} proposal(s) in, {self.bound} bound "
            f"({self.unbound} unbound), {self.live} live; reasons: {tally}"
        )

    def unbound_rows(self) -> Tuple[BindingRow, ...]:
        """The proposals the radar cannot price, for a per-proposal log line."""
        return tuple(
            row
            for row in self.rows
            if row.reason not in (BIND_BOUND, BIND_BOUND_NORMALISED)
        )


