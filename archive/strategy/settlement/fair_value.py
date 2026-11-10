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

import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from polymarketbot.logs import get_logger
from polymarketbot.strategy.settlement.hazard import BayesianDisputeTracker
from polymarketbot.strategy.settlement.uma_overlay import UMA_LIVE_VERDICTS_PATH
from polymarketbot.strategy.settlement.uma_tape import (
    TAPE_LOOKBACK_SEC,
    UMA_TAPE_GLOB,
    UmaVerdictTape,
)
from polymarketbot.strategy.settlement.verdict_index import (
    BIND_AGED_OUT,
    BIND_AMBIGUOUS,
    BIND_BOUND,
    BIND_BOUND_NORMALISED,
    BIND_NO_OPEN_FIXTURE,
    BIND_NO_QUESTION,
    BIND_UNMATCHED_QUESTION,
    BIND_WOULD_BIND,
    BIND_WOULD_BIND_NORMALISED,
    TAPE_VERDICT_TAIL_SEC,
    BindingAudit,
    BindingRow,
    FixtureIndex,
    TapeProposal,
    fixture_matches,
    normalize_question,
    proposal_deadline,
    question_sides,
    side_matches,
)

log = get_logger("domain2_settlement_fair_value")

__all__ = [
    "BIND_AGED_OUT",
    "BIND_AMBIGUOUS",
    "BIND_BOUND",
    "BIND_BOUND_NORMALISED",
    "BIND_NO_OPEN_FIXTURE",
    "BIND_NO_QUESTION",
    "BIND_UNMATCHED_QUESTION",
    "BIND_WOULD_BIND",
    "BIND_WOULD_BIND_NORMALISED",
    "BindingAudit",
    "BindingRow",
    "FINAL_UMA_STATUSES",
    "FixtureIndex",
    "NO_OUTCOMES",
    "OracleVerdict",
    "SettlementFairValueModel",
    "TAPE_LOOKBACK_SEC",
    "TAPE_VERDICT_TAIL_SEC",
    "TapeProposal",
    "UMA_LIVE_VERDICTS_PATH",
    "UMA_TAPE_GLOB",
    "UmaVerdictTape",
    "YES_OUTCOMES",
    "fixture_matches",
    "normalize_question",
    "proposal_deadline",
    "question_sides",
    "side_matches",
]

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
