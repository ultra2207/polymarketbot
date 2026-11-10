"""Tail-risk model of the settlement allocator.

Resolution-source clustering, the measured PD x LGD loss and the exact
expected-shortfall formula the tail-budget solve is built on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .types import HeldExposure, Position, SettlementCandidate


# ---------------------------------------------------------------------------
# Tail budget: clusters, measured PD, loss given failure
# ---------------------------------------------------------------------------

# Bisection steps for the budget's shadow price.  The shortfall is monotone in
# the price, so this only bounds how precisely the budget boundary is located.
_TAIL_BISECTION_STEPS = 80
# Doubling steps used to bracket the tail budget's shadow price from above.
_TAIL_BRACKET_STEPS = 60
# A solve sits "on" the budget when it uses this fraction of it; a numerical
# tolerance on the constraint, not a policy.
_TAIL_BINDING_RTOL = 1e-6

# The weight the failure branch puts on an at-risk dollar.  LGD = 1 is the
# payoff structure, not an estimate: a token whose verdict is reversed pays
# 0.00 against a cost of ``price``, so the whole all-in cost is lost.
LOSS_GIVEN_FAILURE = 1.0

# How two positions are linked inside one resolution source.
LINK_SAME_TOKEN = "same_token"          # identical token: rho = +1, full double charge
LINK_COMPLEMENT = "complementary_token"  # other token of one binary condition: pays par
LINK_SAME_SOURCE = "same_resolution_source"  # same UMA question / negRisk event
LINK_DISJOINT = "disjoint_source"       # different condition and event


@dataclass(frozen=True)
class TailBudgetReport:
    """Everything the tail solve was told and everything it decided.

    ``shortfall_usd`` is the loss in the worst ``1 - tail_confidence`` band
    (the expected shortfall), ``expected_loss_usd`` is the unconditional
    expected loss, and ``binding_constraint`` names what stopped the book.
    ``sensitivity_2x_*`` re-solves with the measured tail rate doubled.
    """

    tail_confidence: float
    tail_pd: float
    """``q`` - upper credible bound of the measured verdict-reversal rate."""
    risk_appetite_usd: float
    expected_shortfall_usd: float
    expected_loss_usd: float
    prior_shortfall_usd: float
    prior_expected_loss_usd: float
    prior_undrawn_usd: float
    cluster_notional_usd: Dict[str, float]
    cluster_q: Dict[str, float]
    cluster_shortfall_usd: Dict[str, float]
    cluster_expected_loss_usd: Dict[str, float]
    cluster_tokens: Dict[str, int]
    shadow_price: float
    """``mu`` - the budget's price per unit of shortfall (0 = not binding)."""
    binding_constraint: str
    evidence: str
    """One line: the measured counts behind ``tail_pd``."""
    tail_capacity_usd: float = 0.0
    """Notional the appetite pays for at ``tail_pd``: ``(1-alpha)A/q``, less the
    notional the held book already consumes.  The second spending limit."""
    capital_hurdle_usd_per_usd_per_day: Optional[float] = None
    """The measured opportunity cost the deployment had to clear, or ``None``.

    ``None`` means no measured shadow yield was supplied, so the capital's
    alternative use was **not** priced - a fact the log states rather than
    assumes away with a zero.
    """
    sensitivity_2x_shortfall_usd: float = 0.0
    sensitivity_2x_total_allocated_usd: float = 0.0
    sensitivity_2x_expected_profit_usd: float = 0.0
    verdict_implied_loss_pd: float = 0.0
    """``max(1 - p_win)`` over the candidates: what the pricing model implies.

    Printed next to the measured tail so a divergence is visible rather than
    silently resolved in either direction.  The budget uses the *measured* tail;
    this number only says whether the expected profit is the more or the less
    conservative half of the statement.
    """
    fee_source: str = ""
    refusal: str = ""

    @property
    def headroom_usd(self) -> float:
        return self.risk_appetite_usd - self.expected_shortfall_usd


def shortfall_usd(
    expected_loss_usd: float, tail_confidence: float
) -> float:
    """Expected shortfall of a rare-loss book at ``tail_confidence``.

    For independent clusters with loss-given-failure 1 and ``sum_c q_c <= 1 -
    alpha`` the entire failure region lies inside the worst ``1 - alpha`` band,
    so the band's conditional mean loss is the unconditional expected loss
    divided by the band's probability mass:

        ES_alpha = sum_c q_c L_c / (1 - alpha)

    Exact under the stated condition.  The condition itself is checked by
    :func:`union_bound_ok`; outside it the linear form would understate the band
    and the allocator refuses rather than using it.
    """
    if not 0.0 < tail_confidence < 1.0:
        raise ValueError(f"tail confidence must be in (0, 1), got {tail_confidence!r}")
    return float(expected_loss_usd) / (1.0 - float(tail_confidence))


def union_bound_ok(q_by_cluster: Sequence[float], tail_confidence: float) -> bool:
    """True when the failure region still fits inside the worst band.

    ``sum_c q_c <= 1 - alpha`` is what makes the shortfall formula exact: with
    more probability mass in failures than the band holds, joint cluster
    failures sit inside the band and the linear sum of PDs stops describing it.
    """
    if not 0.0 < tail_confidence < 1.0:
        raise ValueError(f"tail confidence must be in (0, 1), got {tail_confidence!r}")
    return sum(float(q) for q in q_by_cluster) <= (1.0 - float(tail_confidence)) + 1e-12


class ResolutionClusterBook:
    """Loss-given-failure per resolution source, from the payoff structure.

    Every position on one resolution source fails together, so the
    diversification axis is distinct resolution *sources*: two legs of one
    negRisk event are mutually exclusive, and two markets sharing one UMA
    question are perfect substitutes, not diversification.  The cluster keys
    are the candidate's ``event_key`` (Gamma ``negRiskMarketID`` or the event
    id) and, absent one, its own condition - never a fabricated grouping.

    The loss charged to a cluster in its failure branch is:

    * the all-in cost of every position on the token the verdict claims - a
      reversal pays 0.00 against that cost (LGD = 1);
    * **less** the par the complementary token of the *same binary condition*
      pays in that branch, ``1.00 * shares`` - protocol arithmetic;
    * with any other leg of the same event charged in full, because its
      conditional payout is not measured anywhere and an unmeasured hedge is
      not a hedge.

    Two lots on one token therefore add their notionals (rho = +1 by identity of
    the asset) instead of cancelling as if they were a hedge.
    """

    def __init__(self, candidates: Sequence[SettlementCandidate]) -> None:
        self.candidates = list(candidates)
        self.cluster_by_index: List[str] = [
            self.cluster_key(c) for c in self.candidates
        ]
        self.claimed_token_by_condition: Dict[str, str] = {
            c.condition_id: c.token_id for c in self.candidates
        }

    @staticmethod
    def cluster_key(candidate: SettlementCandidate) -> str:
        """The resolution source an exposure is charged to."""
        key = candidate.event_key
        return str(key) if key else candidate.condition_id

    @staticmethod
    def link(a: "Position", b: "Position") -> str:
        """How two positions' payoffs are linked by the protocol.

        ``LINK_SAME_TOKEN`` is rho = +1 by identity of the asset: two lots on
        one token are one exposure and are never credited as a hedge against
        each other.  ``LINK_COMPLEMENT`` is the only pair whose joint outcome is
        fixed by protocol (exactly one of the two pays), which is the only pair
        where a hedge is credited.
        """
        if a.token_id == b.token_id:
            return LINK_SAME_TOKEN
        if a.condition_id == b.condition_id:
            return LINK_COMPLEMENT
        if a.cluster == b.cluster:
            return LINK_SAME_SOURCE
        return LINK_DISJOINT

    def prior_split(
        self, prior: Sequence[HeldExposure]
    ) -> Tuple[Dict[str, float], Dict[str, float], float]:
        """The held book split into at-risk cost and credited par, per cluster.

        Returns ``(claimed_cost, hedge_par, undrawn_usd)``.  The classification
        is :meth:`link` on the *token identities*: a lot on the token the
        verdict claims is at risk in full (and two such lots add, rho = +1 by
        identity of the asset), the complementary token of the same binary
        condition is credited at par, and anything else is charged in full
        because its conditional payout is not measured.
        """
        claimed: Dict[str, float] = {}
        hedge: Dict[str, float] = {}
        undrawn: float = 0.0
        for lot in prior:
            cluster = lot.cluster
            claimed_token = lot.claimed_token_id or self.claimed_token_by_condition.get(
                lot.condition_id
            )
            if claimed_token is None:
                # No verdict this tick for this condition: the side is unknown,
                # so no hedge is credited.
                claimed[cluster] = claimed.get(cluster, 0.0) + float(lot.cost_usd)
                undrawn += float(lot.cost_usd)
                continue
            kind = self.link(
                Position(lot.condition_id, lot.token_id, cluster),
                Position(lot.condition_id, claimed_token, cluster),
            )
            if kind == LINK_COMPLEMENT:
                hedge[cluster] = hedge.get(cluster, 0.0) + float(lot.shares)
            else:
                claimed[cluster] = claimed.get(cluster, 0.0) + float(lot.cost_usd)
                if kind != LINK_SAME_TOKEN:
                    undrawn += float(lot.cost_usd)
        return dict(claimed), dict(hedge), undrawn

    def describe(self, prior_by_cluster: Dict[str, float]) -> str:
        parts = [
            f"{len(self.candidates)} candidate(s) over "
            f"{len(set(self.cluster_by_index))} resolution-source cluster(s)"
        ]
        if prior_by_cluster:
            parts.append(
                f"{len(prior_by_cluster)} cluster(s) already carry "
                f"${sum(prior_by_cluster.values()):,.2f} of loss-given-failure"
            )
        return "; ".join(parts)
