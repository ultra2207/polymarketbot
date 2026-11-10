"""Measured UMA proposals from the event tape, refreshed as the tape grows.

The tape is the ever-updating input: the soak's UMA stream appends rows while it
runs, so the index is re-read whenever a parquet file's mtime changes (and at
most once per ``refresh_sec``). Proposals are keyed both by ``condition_id`` and
by normalized question text, because the stream resolves the condition id by
exact question match against the universe manifest and leaves it null when the
market is not in that manifest - the question text is always present.

The join itself lives in :mod:`polymarketbot.strategy.settlement.verdict_join`,
the on-disk overlay in :mod:`polymarketbot.strategy.settlement.uma_overlay`, and
the index/fixture rules in
:mod:`polymarketbot.strategy.settlement.verdict_index`; the class binds them
into one tape.
"""

from __future__ import annotations

import time
from dataclasses import replace as dc_replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from polymarketbot.logs import get_logger
from polymarketbot.strategy.settlement.uma_overlay import (
    UMA_LIVE_VERDICTS_PATH,
    load_overlay,
    save_overlay,
)
from polymarketbot.strategy.settlement.uma_tape_read import read_tape
from polymarketbot.strategy.settlement.verdict_index import (
    TAPE_VERDICT_TAIL_SEC,
    TapeProposal,
    normalize_question,
    proposal_deadline,
)
from polymarketbot.strategy.settlement.verdict_join import VerdictJoinMixin

log = get_logger("domain2_settlement_fair_value")

# The UMA event tape written by ``data.uma`` (see its column set).
UMA_TAPE_GLOB: str = "data/tape/uma_events/**/*.parquet"
# A proposal is only relevant while its liveness window and the on-chain legs can
# still be pending; a bound on how far back the tape is read keeps the refresh
# cheap as the tape grows.  Seven days is far beyond any measured cycle.
TAPE_LOOKBACK_SEC: float = 7 * 24 * 3600.0


class UmaVerdictTape(VerdictJoinMixin):
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
        self._overlay = load_overlay(self.overlay_path)
        if self._overlay:
            for proposal in self._overlay.values():
                self._merge_proposal(proposal)

    # -- live-verdict persistence -------------------------------------------

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
            save_overlay(self.overlay_path, self._overlay)

    def _read_tape(self, now_ts: float) -> Tuple[List[TapeProposal], float]:
        """Delegate to the parquet reader."""
        return read_tape(self.glob, self.lookback_sec, now_ts)

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
            save_overlay(self.overlay_path, self._overlay)
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

