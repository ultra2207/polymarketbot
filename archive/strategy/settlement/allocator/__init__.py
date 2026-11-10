"""Production Marginal Capital Allocator for Domain 2 Settlement Harvester.

Formulates and solves the non-linear orderbook consumption problem for pre-settlement
par discount harvesting.

The allocator owns both *how many markets* and *how much capital per market*.  It
consumes the full CLOB ask ladder of every candidate under the available cash and
only the venue's real constraints: minimum order size, gas, the market's taker fee
schedule, and the hazard-model survival probability.  There is no target slot count,
no bankroll-percentage concentration cap, and no static minimum ticket.

Expected profit of consuming capital C on market i:

    Profit_i(C) = p_win_i * Shares_i(C) - Cost_i(C) - Fees_i(C) - Gas_i

where ``Shares_i(C)`` is piecewise-linear and concave over the ask ladder, ``p_win``
is the conservative survival probability from ``RobustHazardModel``, ``Fees_i`` are
exact per-level taker fees from the market's Gamma fee schedule, and ``Gas_i`` is the
single on-chain redemption cost.

Annualised marginal velocity (expected edge per dollar per year):

    mu_i(C) = d(Profit_i * Sec_Per_Year / Duration_i) / dC
            = ((p_win_i - P_marginal - fee_per_share) / P_marginal)
              * (Sec_Per_Year / Duration_i)

which is strictly decreasing in capital as ask prices escalate up the ladder.
Water-filling equalises ``mu_i(C_i) = lambda*`` across funded markets subject only
to the budget and real ladder depth.  A market whose post-allocation expected net
profit is non-positive, or whose fill is below the venue minimum order size, is
pruned and its capital is re-offered to the next-best tranches until a fixed point.

Implements both:
- Google OR-Tools SCIP Mixed-Integer Linear Program (fixed-charge gas activation).
- Exact analytical Greedy Water-Filling Priority Queue solver.

Tail-budget allocation (``optimize_tail_budget``)
=================================================

Water-filling above is *risk-neutral*: it ranks a tranche by expected edge and
therefore devotes the treasury to whichever single condition has the deepest
cheap ladder.  Measured on the 2026-11-10 14:25 window, that put 1,049.1 shares
and the entire $1,000.00 treasury into **one** condition
(``0xbcd8bf70c6...``) in one tick, at prices 0.81-0.98 - a single UMA verdict
away from losing the whole pillar.

Concentration is not controlled here by a cap on any market, event or sector.
A cap is a guess about the answer.  What a systematic desk does is *price* the
risk and let the optimiser decide.  The frameworks, and what each needs:

=============================================  ==============================  ==========================
framework                                      inputs needed                   usable here?
=============================================  ==============================  ==========================
fractional Kelly ``f* = q - (1-q)/b``          ``q`` and the payoff odds ``b``  **no**: ``f*`` sign-flips
                                                                               when the least-measured
                                                                               input moves 2x.  At
                                                                               b=48.0, q=0.9895 it is
                                                                               +0.449; at q=0.979 it is
                                                                               -0.04.  A sizing rule that
                                                                               changes the *sign* of the
                                                                               decision on a factor-2 error
                                                                               in its own input is not a
                                                                               sizing rule.
mean-variance ``argmax mu'x - x'Qx/2B``        ``mu``, ``Sigma``, ``B``         **no**: ``B`` is fine
                                                                               (equity) but the charge is
                                                                               second-order in a loss that
                                                                               is *total*, and its
                                                                               risk-aversion is only pinned
                                                                               by the log-utility identity
                                                                               the Kelly row already
                                                                               rejects.  Kept as a
                                                                               reported number, never as
                                                                               the objective.
equal-risk-contribution (risk parity)          a covariance estimate only      **no**: ignores ``mu``, so
                                                                               it funds a zero-edge market.
CVaR / expected shortfall at ``alpha``         joint loss distribution         **yes**, and it is the
                                                                               coherent measure: with LGD
                                                                               = 1 and rare independent
                                                                               cluster failures,
                                                                               ``ES_alpha(L)`` is exact
                                                                               and linear.
PD x LGD credit loss budget                    per-obligor default rate and    **yes**: the same object as
                                               loss given default             the row above in the
                                                                               rare-event limit, and both
                                                                               inputs are measurable here
                                                                               (``q`` from the proposal
                                                                               ledger, LGD = 1 by the
                                                                               payoff structure).
drawdown tolerance                             a measured drawdown tolerance   **no**: no such measurement
                                                                               exists in this repo, and
                                                                               inventing one is the
                                                                               rejected constant.

The implemented formulation is therefore an **expected-shortfall budget** whose
loss distribution is the measured one:

    max_x   expected_profit(x)
    s.t.    sum_i all_in_i(x) <= cash                (spending)
            ES_alpha(L)      <= risk_appetite        (tail)
            0 <= x_i <= ladder depth_i               (market)

with, for a resolution cluster ``c`` (one UMA question, or one negRisk event -
every position on it fails *together*, so the diversification axis is distinct
resolution *sources*, not distinct markets),

    L_c   = loss in the failure branch of cluster c   (LGD = 1: all-in cost, less
                                                        any leg of the same
                                                        binary condition that is
                                                        paid par in that branch)
    q_c   = upper credible bound of P(cluster c's verdict is reversed)
    ES    = sum_c q_c * L_c / (1 - alpha)

The last line is **exact**, not an approximation, whenever ``sum_c q_c <= 1 -
alpha``: the whole failure region then lies inside the worst ``1 - alpha`` band,
so the band's conditional expectation is the expected loss divided by its mass.
That condition is checked, not assumed.  It is also why diversification is an
*output*: a cluster with a higher measured ``q`` consumes the budget faster and
is fed less, and no market, event or sector carries a percentage cap.

Two consequences worth stating, because they are what makes this fit for a
convergence trade rather than for a directional bet:

* The tail probability is the measured **outcome error** (final payout != the
  verdict the proposal asserted, ``polymarketbot.strategy.settlement.tail_evidence``),
  not the dispute hazard.  A wrong proposal that nobody disputes finalises
  wrongly; a disputed one can be upheld.  The two are different events and only
  the first one loses money.
* The charge is linear in the tail probability, so a 2x error in the least
  measured input halves the size and never flips the sign of the decision.  The
  allocation reports that 2x case on every solve.

The solver is the same exact ladder oracle: the tail budget is priced at a
shadow price ``mu`` per unit of shortfall, each candidate's marginal edge is
haircut by ``mu * q_c / (1 - alpha) * all_in`` per dollar, and ``mu`` is found by
bisection so the resulting book sits on the budget.  A tranche the market cannot
fill is never assumed filled: the ladder is consumed level by level.

The implementation is split by concern: :mod:`.types` (the candidate, its ladder
mathematics, the output records and held inventory), :mod:`.risk` (resolution
clusters, the measured PD x LGD shortfall and its tolerances), :mod:`.ladder`
(closed-form ladder and marginal-velocity primitives), :mod:`.marginal`
(``MarginalSettlementAllocator`` itself: pruning, water-filling and the shared
result helpers), :mod:`.tail` (the expected-shortfall optimizer), :mod:`.tail_report`
(its statement and logging helpers), :mod:`.scip` (the MILP backend) and
:mod:`.allocate` (backend dispatch), with the tail tolerances kept in one home
in :mod:`.risk` and the logger plus the year constant in :mod:`._shared`.
Every name the single module exported is re-exported here.
"""

from __future__ import annotations

from polymarketbot.strategy.settlement.tail_evidence import TailEvidence, UnmeasuredTail

from ._shared import SECONDS_PER_YEAR, log
from .marginal import MarginalSettlementAllocator
from .risk import (
    LINK_COMPLEMENT,
    LINK_DISJOINT,
    LINK_SAME_SOURCE,
    LINK_SAME_TOKEN,
    LOSS_GIVEN_FAILURE,
    ResolutionClusterBook,
    TailBudgetReport,
    shortfall_usd,
    union_bound_ok,
)
from .types import (
    HeldExposure,
    Position,
    SettlementAllocation,
    SettlementCandidate,
    SettlementOptimizationResult,
)

__all__ = [
    "SECONDS_PER_YEAR",
    "log",
    "SettlementCandidate",
    "SettlementAllocation",
    "SettlementOptimizationResult",
    "HeldExposure",
    "Position",
    "TailBudgetReport",
    "ResolutionClusterBook",
    "shortfall_usd",
    "union_bound_ok",
    "MarginalSettlementAllocator",
    "LOSS_GIVEN_FAILURE",
    "LINK_SAME_TOKEN",
    "LINK_COMPLEMENT",
    "LINK_SAME_SOURCE",
    "LINK_DISJOINT",
    "TailEvidence",
    "UnmeasuredTail",
]
