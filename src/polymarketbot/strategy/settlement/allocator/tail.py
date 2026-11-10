"""Tail-budget (expected-shortfall) optimizer."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Dict, Optional, Sequence

from polymarketbot.strategy.settlement.tail_evidence import TailEvidence, UnmeasuredTail

from .risk import ResolutionClusterBook, TailBudgetReport, shortfall_usd, union_bound_ok
from .risk import _TAIL_BISECTION_STEPS, _TAIL_BRACKET_STEPS
from .tail_report import TailReportMixin
from .types import (
    HeldExposure,
    SettlementCandidate,
    SettlementOptimizationResult,
)


class TailBudgetMixin(TailReportMixin):
    """Expected profit maximised inside a measured expected-shortfall budget."""

    # =========================================================================
    # Tail-budget (expected-shortfall) solver
    # =========================================================================

    def optimize_tail_budget(
        self,
        candidates: Sequence[SettlementCandidate],
        usable_capital: float,
        *,
        risk_appetite_usd: float,
        tail_confidence: float,
        tail_evidence: TailEvidence,
        prior: Sequence[HeldExposure] = (),
        equity_usd: Optional[float] = None,
        capital_hurdle_usd_per_usd_per_day: Optional[float] = None,
        sensitivity_factor: float = 2.0,
    ) -> SettlementOptimizationResult:
        """Allocate capital at maximum expected profit inside a tail-loss budget.

        Solves

            max_x  expected_profit(x)
            s.t.   sum_i all_in_i(x)  <= cash
                   ES_alpha(L(x))     <= risk_appetite_usd
                   0 <= x_i <= ladder depth_i

        with ``ES_alpha = sum_c q_c * L_c / (1 - alpha)`` over resolution
        clusters ``c`` (see :func:`shortfall_usd`), ``q_c`` the *measured* upper
        credible bound of the verdict-reversal rate
        (:mod:`polymarketbot.strategy.settlement.tail_evidence`) and ``L_c`` the
        all-in cost the failure branch loses.  There is no percentage cap on a
        market, an event or a sector: how the dollars divide is an output.

        ``risk_appetite_usd`` and ``tail_confidence`` are required and have no
        defaults - a risk appetite is an input a human states, so the code
        refuses rather than picking one.  ``tail_evidence`` is required for the
        same reason: an unmeasured tail is not a zero.

        Two solves, one model:

        * When every cluster carries the same measured ``q`` (one reversal
          process - the case the evidence supports) the shortfall is *linear* in
          notional, so the budget is exactly a second spending cap of
          ``(1 - alpha) * appetite / q`` dollars.  The solve is then the ordinary
          ladder water-fill against the smaller of cash and that cap, which
          lands on the budget instead of near it.
        * When cluster tail probabilities differ, or the held book is hedged, the
          budget is priced at a shadow price ``mu`` (USD per USD of shortfall):
          the inner problem at a fixed ``mu`` is the exact ladder water-fill with
          each candidate's marginal edge haircut by ``mu * q_c / (1 - alpha)``
          per all-in dollar, and ``mu`` is bisected until the book sits on the
          budget.

        Either way every tranche is a real ladder level, so the result is
        executable at the depth it was sized on.
        """
        usable = float(usable_capital)
        if risk_appetite_usd is None:
            raise ValueError(
                "tail-budget allocation needs risk_appetite_usd: the maximum loss "
                "the book may carry at the stated confidence is an explicit input, "
                "and this code will not invent one"
            )
        appetite = float(risk_appetite_usd)
        if not math.isfinite(appetite) or appetite <= 0.0:
            raise ValueError(f"risk appetite must be a positive USD amount, got {risk_appetite_usd!r}")
        if tail_confidence is None:
            raise ValueError(
                "tail-budget allocation needs tail_confidence: the confidence band "
                "the loss is stated at is an explicit input, not a default"
            )
        conf = float(tail_confidence)
        if not 0.0 < conf < 1.0:
            raise ValueError(f"tail confidence must be in (0, 1), got {tail_confidence!r}")
        if tail_evidence is None:
            raise UnmeasuredTail(
                "tail-budget allocation needs measured verdict-reversal evidence: "
                "run `uv run python data/derived/uma/outcome_accuracy.py`"
            )
        if usable <= 0.0 or not candidates:
            return self._empty_result(usable)

        book = ResolutionClusterBook(candidates)
        cluster_of = {
            cand.condition_id: book.cluster_by_index[i] for i, cand in enumerate(candidates)
        }
        prior_claimed, prior_hedge, prior_undrawn = book.prior_split(prior)

        def _cluster_loss(new_notional: Dict[str, float]) -> Dict[str, float]:
            clusters = set(new_notional) | set(prior_claimed) | set(prior_hedge)
            return {
                c: max(
                    0.0,
                    prior_claimed.get(c, 0.0)
                    + new_notional.get(c, 0.0)
                    - prior_hedge.get(c, 0.0),
                )
                for c in clusters
            }

        def _solve(q: float, mu: float):
            price_per_usd = mu * q / (1.0 - conf)
            priced = [replace(cand, risk_price_per_usd=price_per_usd) for cand in eligible]
            result = self.optimize_water_filling(priced, usable)
            new_notional: Dict[str, float] = {}
            for row in result.allocations:
                cluster = cluster_of[row.condition_id]
                new_notional[cluster] = new_notional.get(cluster, 0.0) + row.all_in_usd
            loss = _cluster_loss(new_notional)
            expected = sum(q * amount for amount in loss.values())
            return result, shortfall_usd(expected, conf), expected, loss, new_notional

        q = tail_evidence.q_loss_upper(conf)
        if not 0.0 < q < 1.0:
            raise UnmeasuredTail(f"measured loss bound {q!r} is not a probability")

        prior_expected = sum(q * amount for amount in _cluster_loss({}).values())
        prior_shortfall = shortfall_usd(prior_expected, conf)
        if prior_shortfall > appetite:
            return self._tail_refusal(
                usable,
                f"the book already held carries ${prior_shortfall:,.2f} of shortfall at "
                f"{conf:.0%} (appetite ${appetite:,.2f}): no new exposure is admissible",
                tail_confidence=conf,
                tail_evidence=tail_evidence,
                q=q,
                appetite=appetite,
                prior_shortfall=prior_shortfall,
                prior_expected=prior_expected,
                prior_undrawn=prior_undrawn,
                book=book,
                cluster_loss=_cluster_loss({}),
            )

        dropped: Dict[str, str] = {
            cand.condition_id: "smallest venue ticket does not clear gas plus fees"
            for cand in candidates
            if not cand.is_fundable()
        }
        if capital_hurdle_usd_per_usd_per_day is not None:
            hurdle = float(capital_hurdle_usd_per_usd_per_day)
            for cand in candidates:
                if cand.condition_id in dropped:
                    continue
                shares = cand.min_ticket_shares
                cost, fills = cand.fills_for_shares(shares)
                if cost <= 0.0:
                    continue
                net = cand.p_win * shares - cost - cand.fee_for_fills(fills) - cand.gas_usd
                rate = self._profit_per_dollar_day(
                    net, cost + cand.fee_for_fills(fills), cand.target_settle_sec
                )
                if rate < hurdle:
                    dropped[cand.condition_id] = (
                        f"profit per capital-day ${rate:.5f} is below the measured "
                        f"alternative ${hurdle:.5f}"
                    )

        # A cluster already over its own share of the budget cannot be fed at
        # all; that is arithmetic on the appetite, not a cap.
        blocked = {
            c
            for c, loss in _cluster_loss({}).items()
            if q * loss / (1.0 - conf) >= appetite
        }
        eligible = [
            cand
            for cand in candidates
            if cand.condition_id not in dropped and cluster_of[cand.condition_id] not in blocked
        ]
        for cand in candidates:
            cluster = cluster_of[cand.condition_id]
            if cluster in blocked and cand.condition_id not in dropped:
                dropped[cand.condition_id] = (
                    f"resolution cluster {cluster[:10]} already consumes the tail budget"
                )

        # The budget in dollars of notional.  With one measured tail rate for
        # every cluster (the case the evidence supports: one reversal process)
        # the shortfall is linear in notional, so the constraint *is* a spending
        # cap of (1 - alpha) * appetite / q dollars.  The exact solve is then the
        # ordinary ladder water-fill against the smaller of cash and that cap -
        # which lands on the budget instead of near it, as a shadow-price
        # iteration would on a discrete ladder.
        cluster_tail_pd = {cluster: q for cluster in set(cluster_of.values())}
        uniform_q = len(set(cluster_tail_pd.values())) == 1
        hedged_book = any(amount > 0.0 for amount in prior_hedge.values())

        def _run(qq: float):
            """One complete budget solve at tail rate ``qq``.

            Returns the fill, its shortfall, the cluster losses and the shadow
            price (0 on the exact single-rate path, where the budget acts as a
            dollar cap).
            """
            capacity = (appetite * (1.0 - conf) - sum(qq * a for a in _cluster_loss({}).values())) / qq
            if uniform_q and not hedged_book:
                budget = min(usable, max(0.0, capacity))
                priced = [replace(cand, risk_price_per_usd=0.0) for cand in eligible]
                fill = self.optimize_water_filling(priced, budget)
                notional: Dict[str, float] = {}
                for row in fill.allocations:
                    cluster = cluster_of[row.condition_id]
                    notional[cluster] = notional.get(cluster, 0.0) + row.all_in_usd
                cluster_loss = _cluster_loss(notional)
                expected = sum(qq * amount for amount in cluster_loss.values())
                return fill, shortfall_usd(expected, conf), expected, cluster_loss, 0.0
            fill, es_now, expected_now, loss_now, _n = _solve(qq, 0.0)
            if not union_bound_ok([qq] * max(1, len(loss_now)), conf):
                return fill, float("nan"), expected_now, loss_now, 0.0
            if es_now <= appetite:
                return fill, es_now, expected_now, loss_now, 0.0
            lo, hi = 0.0, 1.0
            bracketed = False
            for _ in range(_TAIL_BRACKET_STEPS):
                _rh, es_hi, _eh, _lh, _nh = _solve(qq, hi)
                if es_hi <= appetite:
                    bracketed = True
                    break
                lo, hi = hi, hi * 2.0
            if not bracketed:
                return self._empty_result(usable), 0.0, 0.0, {}, float("inf")
            for _ in range(_TAIL_BISECTION_STEPS):
                mid = 0.5 * (lo + hi)
                _rm, es_mid, _em, _lm, _nm = _solve(qq, mid)
                if es_mid <= appetite:
                    hi = mid
                else:
                    lo = mid
            fill, es_now, expected_now, loss_now, _n = _solve(qq, hi)
            return fill, es_now, expected_now, loss_now, hi

        result, es, expected, loss, shadow = _run(q)
        if math.isnan(es):
            return self._tail_refusal(
                usable,
                f"measured tail q={q:.6f} over {len(loss)} cluster(s) exceeds the "
                f"{1.0 - conf:.0%} band: the shortfall formula would not be exact",
                tail_confidence=conf,
                tail_evidence=tail_evidence,
                q=q,
                appetite=appetite,
                prior_shortfall=prior_shortfall,
                prior_expected=prior_expected,
                prior_undrawn=prior_undrawn,
                book=book,
                cluster_loss=loss,
            )
        if math.isinf(shadow):
            return self._tail_refusal(
                usable,
                f"no shadow price brings the {conf:.0%} shortfall under "
                f"${appetite:,.2f}",
                tail_confidence=conf,
                tail_evidence=tail_evidence,
                q=q,
                appetite=appetite,
                prior_shortfall=prior_shortfall,
                prior_expected=prior_expected,
                prior_undrawn=prior_undrawn,
                book=book,
                cluster_loss=loss,
            )
        tail_capacity = max(0.0, (appetite * (1.0 - conf) - prior_expected) / q)

        # Sensitivity: the same book at twice the measured tail rate.  The tail
        # is the least-measured input, so the statement has to carry the error.
        factor = float(sensitivity_factor)
        sens_es = float("nan")
        sens_total = 0.0
        sens_profit = 0.0
        if factor > 0.0:
            q2 = min(1.0, factor * q)
            s_result, s_es, _s_exp, s_loss, _s_shadow = _run(q2)
            if not math.isnan(s_es) and not math.isinf(_s_shadow):
                sens_es = s_es
                sens_total = s_result.total_allocated_usd
                sens_profit = s_result.expected_portfolio_profit_usd

        binding = self._binding_constraint(result, es, appetite, usable, eligible, cluster_of)
        report = TailBudgetReport(
            tail_confidence=conf,
            tail_pd=q,
            risk_appetite_usd=appetite,
            expected_shortfall_usd=es,
            expected_loss_usd=expected,
            prior_shortfall_usd=prior_shortfall,
            prior_expected_loss_usd=prior_expected,
            prior_undrawn_usd=prior_undrawn,
            cluster_notional_usd=dict(loss),
            cluster_q={c: q for c in loss},
            cluster_shortfall_usd={c: q * amount / (1.0 - conf) for c, amount in loss.items()},
            cluster_expected_loss_usd={c: q * amount for c, amount in loss.items()},
            cluster_tokens={c: sum(1 for v in cluster_of.values() if v == c) for c in loss},
            shadow_price=shadow,
            tail_capacity_usd=tail_capacity,
            capital_hurdle_usd_per_usd_per_day=(
                None if capital_hurdle_usd_per_usd_per_day is None
                else float(capital_hurdle_usd_per_usd_per_day)
            ),
            binding_constraint=binding,
            evidence=tail_evidence.describe(),
            sensitivity_2x_shortfall_usd=sens_es,
            sensitivity_2x_total_allocated_usd=sens_total,
            sensitivity_2x_expected_profit_usd=sens_profit,
            verdict_implied_loss_pd=max(
                (1.0 - float(cand.p_win) for cand in eligible), default=0.0
            ),
            fee_source=(
                "Gamma's per-market feeSchedule; the trade tape cannot calibrate it "
                "(fee_rate_bps is 0 on every recorded trade since the V2 cutover)"
            ),
        )
        self._report_tail(result, report, eligible, cluster_of, equity_usd)
        self._log_allocation(result, report, candidates, dropped)
        return result
