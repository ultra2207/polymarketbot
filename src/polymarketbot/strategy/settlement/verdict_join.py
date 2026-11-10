"""The verdict -> condition-id join, split out of the tape it walks.

Every row gets exactly one reason, and a fixture match that is not unique is
refused rather than bound: a wrong bind prices the wrong market, which is worse
than leaving the verdict unbound. The audit is the single join decision - the
bind reads the audit's rows, so the two can never disagree about a row.
"""

from __future__ import annotations

import time
from dataclasses import replace as dc_replace
from typing import Dict, List, Optional

from polymarketbot.logs import get_logger
from polymarketbot.strategy.settlement.uma_overlay import save_overlay
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
    normalize_question,
    proposal_deadline,
    question_sides,
)

log = get_logger("domain2_settlement_fair_value")


class VerdictJoinMixin:
    """``binding_audit`` / ``bind_condition_ids`` for :class:`UmaVerdictTape`."""

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
            save_overlay(self.overlay_path, self._overlay)
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

