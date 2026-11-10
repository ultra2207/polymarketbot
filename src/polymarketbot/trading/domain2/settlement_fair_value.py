"""Independent fair value for settlement candidates - the oracle's verdict, not the book.

Why this module exists
----------------------
A settlement buy pays ``p_win - ask - fee - gas/shares``.  Until now ``p_win``
was derived from the traded price (Gamma's last-trade ``outcomePrices``,
discounted by a hazard) and then clamped to the same book's mid, so it could
never exceed the mid that the ask sits above: ``fair - ask - fee`` was
structurally negative and the pillar could not buy anything at all (2026-11-09
soak: 3,564 candidates, "none has a measured edge over its own live ask",
0 trades).

The fair value here is the *oracle's own verdict*, which is entirely outside the
prediction market's book:

1. ``gamma_payout`` - a market whose UMA resolution is *final* (status
   ``resolved``/``settled``, or unstamped on a pre-2023 market) and which
   carries a final payout vector (every outcome price exactly 0 or 1, summing
   to 1) pays that vector; ``p_win`` is 1.000 for the paying outcome.  A
   ``proposed``/``requested``/``disputed`` status prints the same 1/0 vector
   but is not a resolution, so it is priced by source 2 instead.
2. ``uma_tape`` - the UMA Optimistic Oracle proposal recorded in
   ``data/tape/uma_events`` (``ProposePrice`` with ``proposed_price`` 0 or 1e18
   for ``YES_OR_NO_QUERY`` markets).  An undisputed proposal *is* the
   resolution unless it is disputed inside the liveness window, so
   ``p_win = 1 - P(dispute)`` with the dispute probability taken from the same
   tape's measured dispute rate (``BayesianDisputeTracker``, upper 95 % bound).
   The proposal timestamp is also the measured start of the liveness window, so
   it is the clock for the remaining settlement duration.

Nothing in this module reads the candidate's traded price or its book.  A market
with neither a final payout nor a tape proposal has no independently measurable
value and is therefore *not fundable* - the honest answer, not a price-derived
guess.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, replace as dc_replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from polymarketbot.logs import get_logger
from polymarketbot.trading.domain2.robust_hazard import BayesianDisputeTracker

log = get_logger("domain2_settlement_fair_value")

# The UMA event tape written by ``data.uma`` (see its column set).
UMA_TAPE_GLOB: str = "data/tape/uma_events/**/*.parquet"
# A proposal is only relevant while its liveness window and the on-chain legs can
# still be pending; a bound on how far back the tape is read keeps the refresh
# cheap as the tape grows.  Seven days is far beyond any measured cycle.
TAPE_LOOKBACK_SEC: float = 7 * 24 * 3600.0
# Live-polled verdicts outlive the process. The overlay is memory state built
# from the chain, and a restart used to erase it: the parquet tape is only as
# fresh as the last collector run (measured 2026-11-09: 5,035 min stale while
# the chain carried 74 ``ProposePrice`` events in the previous 25 h), so every
# restart began with an empty verdict index and the radar reported "0
# oracle-priced" for its whole run. Persisting the overlay is what makes the
# on-chain proposals the ever-updating input instead of a per-process one-shot.
UMA_LIVE_VERDICTS_PATH: str = "data/derived/uma/uma_live_verdicts.json"
# A proposal is still evidence after its liveness window closes - the relayer
# and redeem legs are what remain - but not forever. This tail bounds how long
# past the oracle's own deadline an indexed proposal may still be priced; the
# daemon's measured settlement horizon is the stricter gate on funding. Without
# it the 7-day tape lookback (and the persisted live overlay) could price a
# market off a proposal whose outcome has long since been redeemed.
TAPE_VERDICT_TAIL_SEC: float = 6 * 3600.0
# 1.0 == 1e18 fixed-point int256 price encoding used by the Optimistic Oracle.
_ORACLE_PRICE_ONE = 1e18
# An outcome is the "yes side" of a YES_OR_NO_QUERY market only if it is named
# one of these; Polymarket uses both question phrasings.
YES_OUTCOMES = frozenset({"yes", "true", "up"})
NO_OUTCOMES = frozenset({"no", "false", "down"})
# ``umaResolutionStatus`` values that mean the oracle's answer is final.  The
# empty string is a pre-2023 market Gamma never stamped (measured 2026-11-10:
# 2,114 of 12,000 closed markets), ``settled`` is the legacy spelling and
# ``resolved`` the current one.  ``proposed`` / ``requested`` / ``disputed`` are
# pending and must never be read as a payout.
FINAL_UMA_STATUSES = frozenset({"", "resolved", "settled"})
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
# a coincidence, and a coincidence here is one share bought on the wrong side.
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


@dataclass(frozen=True)
class OracleVerdict:
    """The oracle's answer for one market, independent of the CLOB."""

    winning_index: int
    """Index into the market's ``outcomes``/``clobTokenIds`` list that pays 1.00."""
    p_win: float
    """P(the winning index pays 1.00) after the measured dispute hazard."""
    source: str
    """``uma_tape`` (undisputed proposal) or ``gamma_payout`` (final payout)."""
    observed_ts: float
    """When the oracle evidence was recorded (epoch seconds)."""
    detail: str = ""


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


class UmaVerdictTape:
    """Measured UMA proposals from the event tape, refreshed as the tape grows.

    The tape is the ever-updating input: the soak's UMA stream appends rows while
    it runs, so the index is re-read whenever a parquet file's mtime changes (and
    at most once per ``refresh_sec``).  Proposals are keyed both by
    ``condition_id`` and by normalized question text, because the stream resolves
    the condition id by exact question match against the universe manifest and
    leaves it null when the market is not in that manifest - the question text is
    always present.
    """

    def __init__(
        self,
        glob: str = UMA_TAPE_GLOB,
        lookback_sec: float = TAPE_LOOKBACK_SEC,
        refresh_sec: float = 60.0,
        overlay_path: str = UMA_LIVE_VERDICTS_PATH,
    ) -> None:
        self.glob = glob
        self.lookback_sec = float(lookback_sec)
        self.refresh_sec = float(refresh_sec)
        self.overlay_path = overlay_path
        self._by_condition: Dict[str, TapeProposal] = {}
        self._by_question: Dict[str, TapeProposal] = {}
        self._proposals: List[TapeProposal] = []
        # Verdicts the live poller merged from the chain, keyed by normalized
        # question. They are held apart because a parquet reload rebuilds the
        # index from disk only: with a stale tape (no collector running) that
        # reload would erase every on-chain verdict once per refresh interval.
        self._overlay: Dict[str, TapeProposal] = {}
        self._last_refresh = 0.0
        self._last_signature: Optional[Tuple[Tuple[str, int, int], ...]] = None
        # Measured liveness window (proposal -> expiry), median over the tape.
        self.measured_liveness_sec: float = 0.0
        # Newest proposal timestamp on the tape: the pillar's data-plane gauge.
        # A tape that stops advancing cannot supply a verdict, and the radar
        # reports this age instead of pretending it priced the universe.
        self.newest_ts: float = 0.0
        self._load_overlay()

    # -- live-verdict persistence -------------------------------------------
    def _load_overlay(self) -> None:
        """Restore the verdicts a previous process polled off the chain.

        A verdict is the oracle's own statement and does not expire because the
        process restarted; the deadline check happens at decision time in
        ``fair_value`` (``expiry_ts`` plus ``TAPE_VERDICT_TAIL_SEC``), so a
        stale row here can never fund a trade.
        """
        if not self.overlay_path:
            return
        try:
            raw = json.loads(Path(self.overlay_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        rows = raw.get("proposals") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return
        restored = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                proposal = TapeProposal(
                    question=str(row.get("question") or ""),
                    condition_id=str(row.get("condition_id") or "").lower(),
                    proposed_price=float(row["proposed_price"]),
                    ts=float(row["ts"]),
                    liveness_sec=float(row.get("liveness_sec") or 0.0),
                    expiry_ts=float(row.get("expiry_ts") or 0.0),
                )
            except (KeyError, TypeError, ValueError):
                continue
            key = normalize_question(proposal.question)
            if not key or proposal.ts <= 0.0:
                continue
            self._overlay[key] = proposal
            restored += 1
        if restored:
            for proposal in self._overlay.values():
                self._merge_proposal(proposal)
            log.info(
                f"UMA verdict overlay: {restored} live-polled proposal(s) restored "
                f"from {self.overlay_path}"
            )

    def _save_overlay(self) -> None:
        """Atomically persist the live overlay. Best effort by design."""
        if not self.overlay_path:
            return
        path = Path(self.overlay_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "updated_ts": time.time(),
                        "proposals": [
                            {
                                "question": p.question,
                                "condition_id": p.condition_id,
                                "proposed_price": p.proposed_price,
                                "ts": p.ts,
                                "liveness_sec": p.liveness_sec,
                                "expiry_ts": p.expiry_ts,
                            }
                            for p in self._overlay.values()
                        ],
                    }
                ),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as exc:  # noqa: BLE001 - an unwritable path is a degraded cache
            log.warning(f"UMA verdict overlay unwritable ({exc}); memory only")

    # -- tape loading -------------------------------------------------------
    def _signature(self) -> Tuple[Tuple[str, int, int], ...]:
        """(path, mtime_ns, size) of every tape file, for change detection."""
        try:
            paths = sorted(Path().glob(self.glob))
        except (OSError, ValueError):
            return ()
        sig: List[Tuple[str, int, int]] = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            sig.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(sig)

    def refresh(self, now: Optional[float] = None, force: bool = False) -> None:
        """Re-read the tape when it changed or the refresh interval elapsed."""
        now_ts = time.time() if now is None else float(now)
        if not force and (now_ts - self._last_refresh) < self.refresh_sec:
            return
        self._last_refresh = now_ts
        signature = self._signature()
        if not force and signature == self._last_signature and self._proposals:
            return
        try:
            rows, liveness = self._read_tape(now_ts)
        except Exception as exc:  # noqa: BLE001 - a missing tape must not kill the pillar
            log.warning(f"UMA tape unreadable ({exc}); settlement fair value unavailable")
            self._last_signature = None
            self._proposals, self._by_condition, self._by_question = [], {}, {}
            self.measured_liveness_sec = 0.0
            self.newest_ts = 0.0
            self._reapply_overlay(now_ts)
            return
        by_condition: Dict[str, TapeProposal] = {}
        by_question: Dict[str, TapeProposal] = {}
        for proposal in rows:
            if proposal.condition_id:
                by_condition[proposal.condition_id.lower()] = proposal
            key = normalize_question(proposal.question)
            if key:
                by_question[key] = proposal
        self._proposals = rows
        self._by_condition = by_condition
        self._by_question = by_question
        self.measured_liveness_sec = liveness
        self.newest_ts = max((p.ts for p in rows), default=0.0)
        self._last_signature = signature
        self._reapply_overlay(now_ts)
        log.info(
            f"UMA verdict tape: {len(rows)} undisputed proposal(s) in the last "
            f"{self.lookback_sec / 3600.0:.0f}h, measured liveness {liveness:.0f}s"
        )

    def _reapply_overlay(self, now_ts: float) -> None:
        """Merge the live-polled verdicts back over a fresh parquet read."""
        cutoff = now_ts - self.lookback_sec
        dropped = 0
        for key, proposal in list(self._overlay.items()):
            if proposal.ts < cutoff:
                self._overlay.pop(key, None)
                dropped += 1
                continue
            self._merge_proposal(proposal)
        if dropped:
            self._save_overlay()

    def _read_tape(self, now_ts: float) -> Tuple[List[TapeProposal], float]:
        import duckdb

        cutoff_ms = int((now_ts - self.lookback_sec) * 1000.0)
        con = duckdb.connect()
        try:
            rows = con.execute(
                "SELECT question, condition_id, proposed_price, ts, expiry "
                "FROM read_parquet(?) "
                "WHERE event = 'ProposePrice' "
                "  AND identifier_text = 'YES_OR_NO_QUERY' "
                "  AND proposed_price IS NOT NULL "
                "  AND coalesce(disputed, false) = false "
                "  AND ts >= ? "
                "ORDER BY ts ASC",
                [self.glob, cutoff_ms],
            ).fetchall()
        finally:
            con.close()
        proposals: List[TapeProposal] = []
        liveness: List[float] = []
        for question, condition_id, price_raw, ts_raw, expiry_raw in rows:
            try:
                price = float(price_raw) / _ORACLE_PRICE_ONE
            except (TypeError, ValueError):
                continue
            # Only a decisive binary proposal is a verdict; a fractional
            # (scalar) proposal says nothing about which outcome pays.
            if price >= 0.999:
                price = 1.0
            elif price <= 0.001:
                price = 0.0
            else:
                continue
            try:
                ts = float(ts_raw) / 1000.0
            except (TypeError, ValueError):
                continue
            if not (0.0 < ts <= now_ts + 3600.0):
                continue
            span = 0.0
            expiry_ts = 0.0
            if expiry_raw is not None:
                try:
                    # ``expiry`` is an epoch in seconds while ``ts`` is in ms.
                    expiry_ts = float(expiry_raw)
                    candidate_span = expiry_ts - ts
                except (TypeError, ValueError):
                    candidate_span = 0.0
                    expiry_ts = 0.0
                if 0.0 < candidate_span <= 6 * 3600.0:
                    span = candidate_span
                    liveness.append(candidate_span)
                elif not (0.0 < expiry_ts <= now_ts + 6 * 3600.0):
                    expiry_ts = 0.0
            proposals.append(
                TapeProposal(
                    question=str(question or ""),
                    condition_id=str(condition_id or "").lower(),
                    proposed_price=price,
                    ts=ts,
                    liveness_sec=span,
                    expiry_ts=expiry_ts,
                )
            )
        liveness_sorted = sorted(liveness)
        measured = (
            liveness_sorted[len(liveness_sorted) // 2] if liveness_sorted else 0.0
        )
        return proposals, measured

    # -- lookups ------------------------------------------------------------
    def ingest(
        self,
        proposals: Sequence[TapeProposal],
        disputed_questions: Sequence[str] = (),
    ) -> int:
        """Merge live proposals (and drop disputed ones) into the index.

        The deployed service runs the soak without the data collector, so the
        on-chain tape is only as fresh as the last collector run. The pillar
        polls the same ``ProposePrice``/``DisputePrice`` logs itself and merges
        them here, so the verdict source advances with the chain. Returns the
        number of proposals accepted.
        """
        removed = 0
        for question in disputed_questions:
            key = normalize_question(question)
            if not key:
                continue
            self._overlay.pop(key, None)
            had = self._by_question.pop(key, None)
            if had is not None and had.condition_id:
                if self._by_condition.get(had.condition_id.lower()) is had:
                    self._by_condition.pop(had.condition_id.lower(), None)
            before = len(self._proposals)
            self._proposals = [
                p for p in self._proposals if normalize_question(p.question) != key
            ]
            removed += int(had is not None) or (before - len(self._proposals))
        accepted = 0
        for proposal in proposals:
            key = normalize_question(proposal.question)
            if not key:
                continue
            if not self._merge_proposal(proposal):
                continue
            # Store the *indexed* row, not the argument: a cid-less re-proposal
            # of an already-bound question carries the binding forward (see
            # ``_merge_proposal``), and persisting the argument would drop it
            # again on the next restart.
            self._overlay[key] = self._by_question.get(key) or proposal
            accepted += 1
        if removed:
            log.info(f"UMA tape: {removed} disputed proposal(s) dropped from the verdict index")
        if accepted or removed:
            self._save_overlay()
        return accepted

    def live_verdict_condition_ids(
        self, now: Optional[float] = None, tail_sec: float = TAPE_VERDICT_TAIL_SEC
    ) -> List[str]:
        """Condition ids of every proposal that can still be a live verdict.

        The radar sweeps the date/volume strata, and Gamma caps offset
        pagination (HTTP 422 observed at offset 2200-2300, measured 2026-11-09), so a market
        whose ``endDate`` sits at the oldest edge of a stratum can fall outside
        the swept prefix on one sweep and back in on the next. A verdict market
        must instead be discovered by its own condition id: this list is the
        key for that stratum, ordered newest proposal first.
        """
        now_ts = time.time() if now is None else float(now)
        newest: Dict[str, float] = {}
        for proposal in self._proposals:
            cid = str(proposal.condition_id or "").lower()
            if not cid:
                continue
            deadline = proposal_deadline(proposal, self.measured_liveness_sec)
            if deadline > 0.0 and now_ts > deadline + tail_sec:
                continue
            newest[cid] = max(newest.get(cid, 0.0), proposal.ts)
        return [cid for cid, _ts in sorted(newest.items(), key=lambda kv: kv[1], reverse=True)]

    def binding_audit(
        self,
        by_question: Dict[str, str],
        now: Optional[float] = None,
        tail_sec: float = TAPE_VERDICT_TAIL_SEC,
        index: Optional["FixtureIndex"] = None,
    ) -> BindingAudit:
        """Count how many indexed proposals can reach a tradable condition id.

        ``by_question`` is the scanned universe's ``{normalized question:
        condition id}`` map. The walker lists *open, order-accepting* markets, so
        a question found there is a market that can actually be bought; a
        proposal absent from it has no CLOB book to price against, which is the
        dominant reason a verdict window produces zero fills.

        Every row gets exactly one reason, in this precedence:

        * ``no_question`` - the ancillary title normalised to nothing: no join key
          exists, so the proposal is unpricable by construction.
        * ``bound`` / ``bound_normalised`` - carries a condition id, from an
          earlier call or from the exact key / the fixture join.
        * ``aged_out`` - past ``deadline + tail``: the payout legs can no longer be
          pending, so the verdict cannot fund a trade however it binds.
        * ``would_bind`` - live and its exact title is in the scanned universe;
          this call attaches the id.
        * ``ambiguous_match`` - live, and two or more open fixtures match the
          title with equal standing: the join refuses rather than price the
          wrong market.
        * ``no_open_fixture`` - live, the title names a fixture (``A vs B``) and
          no open market carries it: the market closed before the verdict, the
          measured disposition of a post-game sports proposal.
        * ``unmatched_question`` - live, not a fixture, and no open market carries
          the title (a closed market, or a title Gamma spells differently).
        """
        now_ts = time.time() if now is None else float(now)
        if index is None:
            index = FixtureIndex(by_question)
        rows: List[BindingRow] = []
        reasons: Dict[str, int] = {}
        live_count = 0
        bound_count = 0
        for key, proposal in self._by_question.items():
            cid = str(proposal.condition_id or "").lower()
            deadline = proposal_deadline(proposal, self.measured_liveness_sec)
            live = not (deadline > 0.0 and now_ts > deadline + tail_sec)
            live_count += int(live)
            match = ""
            candidate = ""
            if cid:
                reason = BIND_BOUND
                match = "exact"
                candidate = cid
            elif not key:
                reason = BIND_NO_QUESTION
            elif not live:
                reason = BIND_AGED_OUT
            elif str(by_question.get(key) or ""):
                reason = BIND_WOULD_BIND
                match = "exact"
                candidate = str(by_question[key]).lower()
            else:
                matches = index.candidates(proposal.question)
                if len(matches) == 1:
                    reason = BIND_WOULD_BIND_NORMALISED
                    match = "normalised"
                    candidate = str(by_question[matches[0]] or "").lower()
                elif matches:
                    reason = BIND_AMBIGUOUS
                    match = "ambiguous"
                    candidate = " | ".join(
                        f"{key_}->{str(by_question[key_] or '').lower()}"
                        for key_ in matches
                    )
                elif question_sides(proposal.question):
                    reason = BIND_NO_OPEN_FIXTURE
                else:
                    reason = BIND_UNMATCHED_QUESTION
            if reason in (BIND_BOUND, BIND_WOULD_BIND, BIND_WOULD_BIND_NORMALISED):
                bound_count += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            rows.append(
                BindingRow(
                    question=proposal.question,
                    condition_id=cid or ("" if match == "ambiguous" else candidate),
                    reason=reason,
                    age_sec=max(0.0, now_ts - proposal.ts),
                    live=live,
                    match=match,
                    candidate=candidate,
                )
            )
        # This call attaches the would-bind rows, so report the post-call state.
        for would, done in (
            (BIND_WOULD_BIND, BIND_BOUND),
            (BIND_WOULD_BIND_NORMALISED, BIND_BOUND_NORMALISED),
        ):
            if would in reasons:
                reasons[done] = reasons.get(done, 0) + reasons.pop(would)
                rows = [
                    dc_replace(row, reason=done) if row.reason == would else row
                    for row in rows
                ]
        return BindingAudit(
            proposals=len(self._by_question),
            live=live_count,
            bound=bound_count,
            unbound=len(self._by_question) - bound_count,
            reasons=reasons,
            rows=tuple(rows),
        )

    def bind_condition_ids(self, by_question: Dict[str, str]) -> int:
        """Attach condition ids from the scanned universe to live verdicts.

        ``ProposePrice`` carries no condition id - it is an Optimistic Oracle
        event keyed by the request id, and neither the ancillary bytes nor the
        log topics hash to a Polymarket ``questionID`` (measured 2026-11-09:
        ``keccak256(ancillaryData)`` for "Bank of England rate hike in 2026?"
        is ``0x8a82b45c..7750`` while Gamma's ``questionID`` is
        ``0xd60d7aa4..71f5``). The only exact join is the ancillary *title*,
        which is the market's Gamma ``question``; the radar already holds every
        scanned market, so binding here turns the question fallback into an
        exact condition-id index and makes the id survive in the persisted
        overlay instead of being stored as null.

        A title that Gamma spells differently - the measured sports case, where
        the ancillary title names the teams in full and in the other order - is
        joined by ``FixtureIndex``: the exact key first, then the fixture rule.
        A fixture match that is not unique is *refused* and audited as
        ``ambiguous_match``, because a wrong bind prices the wrong market.

        The whole question index is bound, not just the persisted overlay: a
        verdict the parquet tape supplied lives only in ``_by_question``, and a
        ``refresh`` that rejects the overlay row against a newer tape row would
        otherwise drop its condition id and with it the exact join. Binding is
        idempotent, so a later sweep re-attaching an id costs nothing.

        The audit is computed *before* binding and logged after it, so one line
        states how many proposals were in, how many bound, and a per-reason tally
        for the rest - the only way a zero-fill window can be told apart from a
        broken join.
        """
        index = FixtureIndex(by_question)
        audit = self.binding_audit(by_question, index=index)
        # The join decision is the audit's: a row the audit refused as ambiguous
        # must not be bound here, or the two would disagree about the same row.
        join_by_key: Dict[str, str] = {}
        for row in audit.rows:
            if row.match in ("exact", "normalised") and row.condition_id:
                join_by_key[normalize_question(row.question)] = row.condition_id
        bound = 0
        overlay_bound = 0
        for key, proposal in list(self._by_question.items()):
            if proposal.condition_id:
                continue
            cid = str(join_by_key.get(key) or "").lower()
            if not cid:
                continue
            updated = dc_replace(proposal, condition_id=cid)
            self._by_question[key] = updated
            self._by_condition[cid] = updated
            self._proposals = [
                updated if normalize_question(p.question) == key else p
                for p in self._proposals
            ]
            if key in self._overlay:
                self._overlay[key] = updated
                overlay_bound += 1
            bound += 1
        if overlay_bound:
            self._save_overlay()
        # Log on change, not every call: the radar binds on every tick, and a
        # steady state of 14 live unbound proposals wrote 12 lines per tick
        # (measured window 6: 2,160 lines for one 30-minute run, which buries
        # the state change that actually matters).
        signature = (
            audit.bound,
            audit.unbound,
            audit.live,
            tuple(sorted(audit.reasons.items())),
        )
        if bound or signature != getattr(self, "_audit_signature", None):
            self._audit_signature = signature
            log.info(f"verdict binding: {audit.summary()}")
        reported = getattr(self, "_reported_unbound", None)
        if reported is None:
            reported = self._reported_unbound = set()
        for row in audit.unbound_rows():
            if not row.live or row.question in reported:
                continue
            reported.add(row.question)
            detail = f" ({row.candidate})" if row.candidate else ""
            log.info(
                f"verdict binding: unbound live proposal "
                f"{row.age_sec / 60.0:.1f}min old, reason={row.reason}{detail}: "
                f"{row.question[:70]}"
            )
        return bound

    def _merge_proposal(self, proposal: TapeProposal) -> bool:
        """Index one proposal, newest wins. False when a fresher row exists.

        A re-proposal of the same question keeps the condition id already bound
        to that question: the id is a property of the *market*, not of the log
        row, and the live poller's rows carry no id (it has no universe
        manifest). Without this carry-forward every re-poll of an already-bound
        proposal un-binds it, which is how a 17-proposal index collapsed to 2 and
        then 1 bound markets within 20 seconds of the 2026-11-10 10:10 soak.
        """
        key = normalize_question(proposal.question)
        if not key:
            return False
        current = self._by_question.get(key)
        if current is not None and current.ts > proposal.ts:
            return False
        if not proposal.condition_id and current is not None and current.condition_id:
            proposal = dc_replace(proposal, condition_id=current.condition_id)
        self._by_question[key] = proposal
        if proposal.condition_id:
            self._by_condition[proposal.condition_id.lower()] = proposal
        self._proposals = [
            p for p in self._proposals if normalize_question(p.question) != key
        ]
        self._proposals.append(proposal)

        self.newest_ts = max(self.newest_ts, proposal.ts)
        return True

    def proposal_for(
        self, condition_id: str, question: str
    ) -> Optional[TapeProposal]:
        """Latest undispuded proposal for a market, by condition id then question.

        The condition id is the exact join (the stream resolves it from the
        universe manifest) and a dispute clears *both* indexes, so a re-proposal
        after a challenge can never be shadowed by the row it replaced. The
        question is the fallback for a row the manifest could not resolve.
        """
        cid = str(condition_id or "").lower()
        if cid:
            found = self._by_condition.get(cid)
            if found is not None:
                return found
        return self._by_question.get(normalize_question(question))

    def __len__(self) -> int:
        return len(self._proposals)


class SettlementFairValueModel:
    """Oracle-derived fair value for a settlement candidate.

    ``fair_value`` returns ``None`` when no independent value exists.  The caller
    must treat ``None`` as "cannot price this trade", never as "priced at the
    book's mid": that substitution is exactly what made the pillar unable to
    trade while pretending it had an estimate.
    """

    def __init__(
        self,
        dispute_tracker: Optional[BayesianDisputeTracker] = None,
        tape: Optional[UmaVerdictTape] = None,
        tape_refresh_sec: float = 60.0,
    ) -> None:
        # The hazard is a *measured* number: the Beta posterior is seeded from the
        # observed proposal/dispute counts on the UMA tape, so the haircut applied
        # to every verdict is the Clopper-Pearson upper-95 bound of the data, not
        # the fallback prior's invented 1-in-1,000 dispute rate. Measured
        # 2026-11-09: the tape held 235 ``YES_OR_NO_QUERY`` proposals with 0
        # disputes, whose 95 % upper bound is 1.26 % (p_win 0.9874), against the
        # unseeded prior's 0.30 % (p_win 0.9970) - i.e. the hardcoded prior was
        # crediting 1,000 pseudo-observations of clean settlement and pricing
        # verdicts ~1 point too rich. ``from_measured_sources`` keeps the
        # documented fallback only when no measurement exists at all.
        self.dispute_tracker = (
            dispute_tracker
            if dispute_tracker is not None
            else BayesianDisputeTracker.from_measured_sources()
        )
        # ``or`` would replace an *empty* tape (len 0, falsy) with a fresh
        # default-glob one: the live poller merges into the tape it was given.
        self.tape = tape if tape is not None else UmaVerdictTape(refresh_sec=tape_refresh_sec)

    def refresh(self, now: Optional[float] = None, force: bool = False) -> None:
        self.tape.refresh(now=now, force=force)

    def reversal_hazard(self, category: str) -> float:
        """Measured upper-95 P(a proposed verdict does not stand)."""
        return float(self.dispute_tracker.get_credible_upper_hazard(category))

    # -- verdict extraction -------------------------------------------------
    @staticmethod
    def _outcome_index(outcomes: Sequence[Any], wanted: Iterable[str]) -> Optional[int]:
        wanted_set = {w.lower() for w in wanted}
        for idx, name in enumerate(outcomes):
            if isinstance(name, str) and name.strip().lower() in wanted_set:
                return idx
        return None

    @staticmethod
    def _binary_slot(outcomes: Sequence[Any], proposed_yes: bool) -> Optional[int]:
        """The outcome slot the proposal answers YES/NO about, or ``None``.

        An OO ``proposePrice`` is a *binary* claim: 1e18 answers the request's
        YES side, 0 answers NO. A Gamma market whose outcomes are labels rather
        than YES/NO literals - every sports moneyline ("Sharks"/"Islanders"),
        every totals market ("Over"/"Under") - still has that same binary pair:
        a CTF binary condition holds exactly two index sets, and slot 0 is YES.
        That is a protocol constant, not a guess: ``outcomeIndex = positionId &
        0xff`` with ``0 = YES, 1 = NO`` and ``positionIds[0] == token1`` was
        proven on chain for 934,889 of 934,957 conditions
        (``docs/polymarket-mechanics.md``, ``data/builders/build_labels.py``),
        and ``outcomes``/``clobTokenIds`` are the parallel arrays of those
        slots.

        The refusal this replaces was the measured zero-fill cause of the
        2026-11-10 12:53 window: ``Sharks vs. Islanders`` (proposed YES) and
        ``Canucks vs. Flyers`` (proposed NO) were open, order-accepting, bound
        to their condition id, and both refused as ``no_live_verdict`` because
        neither outcome is spelled "Yes". Returns ``None`` for any market that
        has more than two outcomes: a neg-risk or multi-outcome condition is
        not this pair, and its slot mapping needs the ancillary outcome map.
        """
        if len(outcomes) != 2:
            return None
        return 0 if proposed_yes else 1

    @staticmethod
    def final_payout(prices: Sequence[Any], closed: bool, uma_status: str) -> Optional[int]:
        """Index that pays 1.00 from a *final* payout vector, else ``None``.

        ``outcomePrices`` on a live market is the last traded price, not a
        payout.  Only a closed/resolved market whose prices are exactly 0/1 and
        sum to 1 is a payout vector; anything else is a market print and is
        rejected here by construction.

        The vector is only final when the oracle's *status* is final.
        ``proposed``, ``requested`` and ``disputed`` are pending states that
        Gamma nevertheless prints a 1/0 indicative vector for once trading
        stops, so ``closed`` alone is not evidence: measured 2026-11-10 over
        12,000 closed markets, 472 carried ``proposed`` with an exact 1/0
        vector, 2 ``disputed`` and 2 ``requested``.  Treating those as payouts
        pays par on an answer the measured 1.26% dispute hazard says can still
        flip, and it silently bypasses the tape verdict that carries that
        haircut.  A pending market therefore falls through to the proposal
        path (``p_win = 1 - hazard``) and a disputed one has no verdict at all.
        """
        status = str(uma_status or "").strip().lower()
        if status not in FINAL_UMA_STATUSES:
            return None
        if not (closed or status in ("resolved", "settled")):
            return None
        try:
            values = [float(p) for p in prices]
        except (TypeError, ValueError):
            return None
        if len(values) < 2:
            return None
        if any(not (v <= 1e-6 or v >= 1.0 - 1e-6) for v in values):
            return None
        if abs(sum(values) - 1.0) > 1e-6:
            return None
        winners = [i for i, v in enumerate(values) if v >= 1.0 - 1e-6]
        if len(winners) != 1:
            return None
        return winners[0]

    def fair_value(
        self,
        *,
        condition_id: str,
        question: str,
        outcomes: Sequence[Any],
        prices: Sequence[Any] = (),
        closed: bool = False,
        uma_status: str = "",
        category: str = "settlement",
        now: Optional[float] = None,
    ) -> Optional[OracleVerdict]:
        """Independent fair value for one market, or ``None`` if there is none."""
        now_ts = time.time() if now is None else float(now)
        hazard = self.reversal_hazard(category)

        payout_index = self.final_payout(prices, closed, str(uma_status or "").lower())
        if payout_index is not None:
            return OracleVerdict(
                winning_index=payout_index,
                p_win=1.0,
                source="gamma_payout",
                observed_ts=now_ts,
                detail=f"final payout vector {list(prices)}",
            )

        proposal = self.tape.proposal_for(condition_id, question)
        if proposal is None:
            return None
        deadline = proposal_deadline(proposal, self.tape.measured_liveness_sec)
        if deadline > 0.0 and now_ts > deadline + TAPE_VERDICT_TAIL_SEC:
            # Aged out: the window closed too long ago for the payout legs to
            # still be pending, so this proposal cannot be the live verdict.
            return None
        proposed_yes = proposal.proposed_price >= 0.5
        index = self._outcome_index(
            outcomes, YES_OUTCOMES if proposed_yes else NO_OUTCOMES
        )
        if index is None:
            index = self._binary_slot(outcomes, proposed_yes)
        if index is None:
            # Not a two-outcome market *and* no YES/NO literal: the proposed
            # price cannot be mapped onto an outcome without the ancillary
            # outcome mapping, so there is no verdict to trade.
            return None
        return OracleVerdict(
            winning_index=index,
            p_win=max(0.0, 1.0 - hazard),
            source="uma_tape",
            observed_ts=proposal.ts,
            detail=(
                f"UMA proposed {'YES' if proposal.proposed_price >= 0.5 else 'NO'} "
                f"at {proposal.ts:.0f}, dispute hazard {hazard:.4%}"
            ),
        )
