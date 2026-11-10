"""Statement and logging helpers of the tail-budget solve."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from polymarketbot.trading.domain2.tail_evidence import TailEvidence

from ._shared import log
from .risk import (
    _TAIL_BINDING_RTOL,
    LOSS_GIVEN_FAILURE,
    ResolutionClusterBook,
    TailBudgetReport,
)
from .types import SettlementCandidate, SettlementOptimizationResult


class TailReportMixin:
    """What stopped the book, and the auditable statement of the result."""

    @staticmethod
    def _binding_constraint(
        result: SettlementOptimizationResult,
        shortfall: float,
        appetite: float,
        usable: float,
        candidates: Sequence[SettlementCandidate],
        cluster_of: Dict[str, str],
    ) -> str:
        """What stopped the book, as one or more of the four real limits.

        Cash is exhausted when the dollars left cannot buy one share at the
        cheapest funded ask - the smallest tradable unit, not a tolerance.
        Two limits can bind at once (a ladder that ends exactly at the cash
        boundary), so the answer can name both.
        """
        limits: List[str] = []
        if abs(appetite - shortfall) <= max(_TAIL_BINDING_RTOL * appetite, 1e-9):
            limits.append("tail budget")
        all_in = sum(row.all_in_usd for row in result.allocations)
        if result.allocations:
            tick = min(row.effective_avg_price or 1.0 for row in result.allocations)
            if usable - all_in <= max(tick, 1e-9):
                limits.append("cash")
        funded = {row.condition_id for row in result.allocations}
        unfunded_depth = any(
            cand.condition_id not in funded and cand.is_fundable() for cand in candidates
        )
        if not unfunded_depth and result.allocations:
            consumed = sum(row.shares for row in result.allocations)
            depth = sum(sum(sz for _px, sz in cand.ask_ladder) for cand in candidates)
            if depth > 0.0 and consumed >= depth * (1.0 - 1e-9):
                limits.append("ladder depth")
        return " + ".join(limits) if limits else "expected profit"

    def _tail_refusal(
        self,
        usable: float,
        reason: str,
        *,
        tail_confidence: float,
        tail_evidence: TailEvidence,
        q: float,
        appetite: float,
        prior_shortfall: float,
        prior_expected: float,
        prior_undrawn: float,
        book: ResolutionClusterBook,
        cluster_loss: Dict[str, float],
    ) -> SettlementOptimizationResult:
        """No allocation, with the arithmetic that made it inadmissible."""
        report = TailBudgetReport(
            tail_confidence=tail_confidence,
            tail_pd=q,
            risk_appetite_usd=appetite,
            expected_shortfall_usd=prior_shortfall,
            expected_loss_usd=prior_expected,
            prior_shortfall_usd=prior_shortfall,
            prior_expected_loss_usd=prior_expected,
            prior_undrawn_usd=prior_undrawn,
            cluster_notional_usd=dict(cluster_loss),
            cluster_q={c: q for c in cluster_loss},
            cluster_shortfall_usd={c: q * a / (1.0 - tail_confidence) for c, a in cluster_loss.items()},
            cluster_expected_loss_usd={c: q * a for c, a in cluster_loss.items()},
            cluster_tokens={c: 1 for c in cluster_loss},
            shadow_price=0.0,
            binding_constraint="tail budget",
            evidence=tail_evidence.describe(),
            refusal=reason,
        )
        result = self._empty_result(usable)
        result.tail_budget = report
        log.warning(f"tail-alloc refused: {reason} | {book.describe(cluster_loss)}")
        return result

    @staticmethod
    def _report_tail(
        result: SettlementOptimizationResult,
        report: TailBudgetReport,
        candidates: Sequence[SettlementCandidate],
        cluster_of: Dict[str, str],
        equity_usd: Optional[float],
    ) -> None:
        """Attach the per-position tail statement and the report to ``result``."""
        by_cid = {row.condition_id: row for row in result.allocations}
        total_shortfall = report.expected_shortfall_usd
        equity = float(equity_usd) if equity_usd else 0.0
        for cand in candidates:
            row = by_cid.get(cand.condition_id)
            if row is None:
                continue
            cluster = cluster_of[cand.condition_id]
            row.tail_pd = report.cluster_q.get(cluster, report.tail_pd)
            row.notional_at_risk_usd = row.all_in_usd * LOSS_GIVEN_FAILURE
            row.tail_charge_usd = (
                row.notional_at_risk_usd * row.tail_pd / (1.0 - report.tail_confidence)
            )
            row.mu_per_usd = row.net_profit_usd / row.all_in_usd if row.all_in_usd > 0 else 0.0
            row.weight = row.all_in_usd / equity if equity > 0.0 else 0.0
            row.risk_contribution = (
                row.tail_charge_usd / total_shortfall if total_shortfall > 0.0 else 0.0
            )
            row.risk_group = cluster
            row.risk_links = (
                "mutually exclusive legs of one event" if cand.event_exhaustive
                else ("shared event, joint outcome unmeasured" if cand.event_key
                      else "own resolution source")
            )
        result.tail_budget = report

    @staticmethod
    def _log_allocation(
        result: SettlementOptimizationResult,
        report: TailBudgetReport,
        candidates: Sequence[SettlementCandidate],
        dropped_reasons: Dict[str, str],
    ) -> None:
        """One auditable line per position, per cluster, and per dropped market.

        The shape of the book has to be provable from the log: each line carries
        the profit, the measured tail probability, the money at risk and the
        shortfall it consumes, so a reader can recompute the budget by hand.
        """
        for row in result.allocations:
            log.info(
                f"tail-alloc {row.condition_id[:10]} E[profit]=${row.net_profit_usd:+.4f} "
                f"mu=${row.mu_per_usd:+.5f}/$ q={row.tail_pd:.6f} "
                f"notional=${row.notional_at_risk_usd:,.2f} "
                f"es=${row.tail_charge_usd:,.2f} "
                f"${row.all_in_usd:,.2f} all-in @ {row.effective_avg_price:.4f} "
                f"cluster={str(row.risk_group)[:10]}"
            )
        seen: set[str] = set()
        for cluster, notional in sorted(report.cluster_notional_usd.items()):
            if cluster in seen:
                continue
            seen.add(cluster)
            log.info(
                f"tail-cluster {str(cluster)[:10]}: q={report.cluster_q.get(cluster, 0.0):.6f} "
                f"notional=${notional:,.2f} "
                f"E[loss]=${report.cluster_expected_loss_usd.get(cluster, 0.0):,.2f} "
                f"shortfall=${report.cluster_shortfall_usd.get(cluster, 0.0):,.2f}"
            )
        funded = {row.condition_id for row in result.allocations}
        for cand in candidates:
            if cand.condition_id in funded:
                continue
            dropped_reasons.setdefault(
                cand.condition_id,
                "no tranche left whose profit per dollar pays the tail charge",
            )
        for cid, reason in sorted(dropped_reasons.items()):
            log.info(f"tail-alloc drop {cid[:10]}: {reason}")
        if report.refusal:
            return
        log.info(
            f"tail-alloc portfolio: {len(result.allocations)} position(s) over "
            f"{len(report.cluster_notional_usd)} resolution-source cluster(s), "
            f"${result.total_allocated_usd:,.2f} cost of ${result.usable_capital_usd:,.2f} cash, "
            f"E[profit]=${result.expected_portfolio_profit_usd:,.2f}, "
            f"E[loss]=${report.expected_loss_usd:,.2f}, "
            f"shortfall@{(1.0 - report.tail_confidence):.2%} band=${report.expected_shortfall_usd:,.2f} "
            f"of ${report.risk_appetite_usd:,.2f} appetite, "
            f"binding={report.binding_constraint}, mu={report.shadow_price:.6g}, "
            f"2x-tail: ${report.sensitivity_2x_total_allocated_usd:,.2f} cost / "
            f"E[profit]=${report.sensitivity_2x_expected_profit_usd:,.2f} / "
            f"shortfall=${report.sensitivity_2x_shortfall_usd:,.2f} "
            f"| opportunity cost: "
            + (
                f"${report.capital_hurdle_usd_per_usd_per_day:.5f}/$/day measured "
                f"shadow yield"
                if report.capital_hurdle_usd_per_usd_per_day is not None
                else "UNPRICED (no measured shadow yield supplied)"
            )
            + f" | fees: {report.fee_source} | {report.evidence}"
        )
        if report.verdict_implied_loss_pd > report.tail_pd:
            log.warning(
                f"tail-alloc: the pricing model implies a {report.verdict_implied_loss_pd:.6f} "
                f"loss probability, above the measured tail {report.tail_pd:.6f}; the budget "
                f"uses the measured tail and the expected profit is therefore the "
                f"conservative half of the statement"
            )
