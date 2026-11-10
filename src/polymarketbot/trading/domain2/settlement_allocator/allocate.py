"""Unified public entrypoint of :class:`MarginalSettlementAllocator`."""

from __future__ import annotations

from typing import Dict, Optional, Sequence, Tuple

from polymarketbot.trading.domain2.tail_evidence import TailEvidence

from ._shared import log
from .types import (
    HeldExposure,
    SettlementCandidate,
    SettlementOptimizationResult,
)


class AllocateMixin:
    """Backend dispatch of the allocator's public ``allocate`` call."""

    # =========================================================================
    # Unified Public Entrypoint
    # =========================================================================

    def allocate(
        self,
        candidates: Sequence[SettlementCandidate],
        usable_capital: float,
        *,
        risk_appetite_usd: Optional[float] = None,
        tail_confidence: Optional[float] = None,
        tail_evidence: Optional[TailEvidence] = None,
        prior: Sequence[HeldExposure] = (),
        prior_shares: Optional[Dict[Tuple[str, str], float]] = None,
        equity_usd: Optional[float] = None,
        capital_hurdle_usd_per_usd_per_day: Optional[float] = None,
    ) -> SettlementOptimizationResult:
        """Allocate usable capital across candidates using the selected solver backend.

        ``risk_appetite_usd`` + ``tail_confidence`` + ``tail_evidence`` switch the
        solve onto the **tail-budget** path: expected profit maximised inside the
        loss the book may carry in the worst ``1 - tail_confidence`` band, with
        the tail probability measured from the proposal ledger rather than
        asserted.  All three are required - a risk appetite and a confidence band
        are what a human states, and an unmeasured tail is not a zero - so the
        call refuses rather than defaulting when one is missing.

        ``prior`` is the book already held, as :class:`HeldExposure` records: it
        is charged for loss-given-failure but its dollars are never re-spent.
        ``prior_shares`` is the older shape of the same fact
        (``(condition_id, token_id) -> shares``) and is only usable when the
        caller has no cost basis to give; it is charged at the candidate's best
        ask, which the log states.  ``equity_usd`` is the firm bankroll used to
        report weights, never the risk denominator: the tail budget is absolute
        dollars, so the size does not move when the book does.
        """
        if self.solver_backend == "tail_budget" or (
            risk_appetite_usd is not None
            or tail_confidence is not None
            or tail_evidence is not None
        ):
            if risk_appetite_usd is None or tail_confidence is None or tail_evidence is None:
                raise ValueError(
                    "solver_backend='tail_budget' needs risk_appetite_usd, "
                    "tail_confidence and tail_evidence: the tail budget is an "
                    "explicit input and its probability is measured, not defaulted"
                )
            return self.optimize_tail_budget(
                candidates,
                usable_capital,
                risk_appetite_usd=risk_appetite_usd,
                tail_confidence=tail_confidence,
                tail_evidence=tail_evidence,
                prior=prior,
                equity_usd=equity_usd,
                capital_hurdle_usd_per_usd_per_day=capital_hurdle_usd_per_usd_per_day,
            )
        if self.solver_backend == "water_filling":
            return self.optimize_water_filling(candidates, usable_capital)
        if self.solver_backend == "scip":
            return self.optimize_scip(candidates, usable_capital)

        log.warning(
            "no risk appetite was supplied, so the settlement book's tail is NOT "
            f"priced in this solve (risk-neutral {self.solver_backend} path)"
        )
        try:
            return self.optimize_scip(candidates, usable_capital)
        except Exception as exc:
            log.warning(f"SCIP allocation failed ({exc}), falling back to Water-Filling.")
            return self.optimize_water_filling(candidates, usable_capital)
